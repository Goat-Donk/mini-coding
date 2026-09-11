"""web 工具：`web_fetch` / `web_search`（M9-2）。

**来源**：任务形态照 TS 原版 `tools/web-fetch.ts` / `tools/web-search.ts`（后者走 DuckDuckGo Lite）。
**SSRF 拦截是我们补的，不是原版的设计** —— TS 的 `utils/web.ts`（506 行）里没有任何私网判据。
Python 移植版有 `_is_safe_url`，但那套 `hostname.startswith([...])` 判据有四个真漏洞
（见 `_blocked_reason` 的注释），所以这里**照它的意图重写，不照抄**。

**为什么这两个工具值得做**：`permissions.py` 的污染天花板收紧三类「不可逆动作」，
其中「网络外发」这一类在此之前**打的是一个不存在的动作** —— `ToolRegistry.default()` 里
没有任何联网工具，判据只能命中 bash 命令行里的 `curl|wget`。这两个工具让一条已有机制
第一次有了真实对象（判据见 `permissions._irreversible_kind`）。

分层的边界说清楚：

- **本模块**：这个 URL **能不能碰**（确定性，无论权限怎么判都生效）
- **权限层**：这次外发**要不要人点头**（会话级，high 污染时收紧）

两者不重叠，也不互为备份 —— 本模块拦的是"打内网"，权限层管的是"数据出去"。
"""
from __future__ import annotations

import gzip
import html as html_mod
import io
import ipaddress
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
import zlib

from pydantic import BaseModel, Field

from agent.tools.base import Tool, ToolContext, ToolResult

#: 单次抓取的响应体字节上限（**压缩前和解压后各算一次**，见 `_decompress`）。
#: 不封顶就是让一个 URL 决定我们读多少内容进内存（`Content-Length` 可以撒谎，
#: 所以边读边数）。
MAX_FETCH_BYTES = 2_000_000

#: 请求头里显式要求不压缩。**行为上等价于"别给我压缩"，但服务端可以不听** ——
#: 实测 `python.org` 就是在没被要求的情况下回了 `Content-Encoding: gzip`
#: （真跑验证时发现的：抓回来一片乱码，而 `Content-Type` 是 `text/html`，
#: 于是顺利通过了文本检查，2MB 的压缩字节被当正文喂给了模型）。
COMPRESSION_HINT = "identity"

#: 回给模型的正文默认上限（对齐 TS 的 12000）。
DEFAULT_MAX_CHARS = 12_000

#: 重定向跳数上限。`HTTPRedirectHandler.max_redirections` 会读这个类属性。
MAX_REDIRECTS = 5

FETCH_TIMEOUT = 30

#: 只允许这两种协议。`file://` 能读本地文件、`gopher://`/`ftp://` 能做协议走私，
#: 而这两个工具的全部用途就是取网页 —— 没有理由放开第三种。
ALLOWED_SCHEMES = frozenset({"http", "https"})

#: `ipaddress` 在 Python 3.12 里判 `is_private=False`（实测），得单列。
#: 100.64.0.0/10 是运营商级 NAT：从云主机上打过去常常能落到内网服务。
_CGNAT = ipaddress.ip_network("100.64.0.0/10")

#: 内嵌 IPv4 的转换前缀。**这几个前缀整段都被 `is_reserved`/`is_private` 覆盖**，
#: 所以不特殊处理的话，连"映射到一个正常公网地址"也会被拦。后果不是理论上的：
#: 纯 IPv6 + DNS64 的网络（手机网络常见）上 `web_fetch` 会**完全不可用**，
#: 而且报的理由是"目标是内网/本机地址"—— 一条**错误的**诊断。
#: 所以这里把内嵌的 IPv4 拆出来，判它本身（见 `_embedded_ipv4`）。
_NAT64 = ipaddress.ip_network("64:ff9b::/96")           # RFC 6052 标准前缀
_NAT64_LOCAL = ipaddress.ip_network("64:ff9b:1::/48")    # RFC 8215 本地前缀
_SIX_TO_FOUR = ipaddress.ip_network("2002::/16")         # RFC 3056

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 CodeAgent/1.0"
)


# ---------- SSRF 判据 ----------

def _embedded_ipv4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """转换前缀里内嵌的 IPv4；不是转换前缀则 None。

    取位方式由这两个 RFC 定死：NAT64 放在**低 32 位**，6to4 放在**第 16~48 位**
    （即前两组十六进制）。**位取错就等于开一个洞**，所以两种都单独钉了测试。
    """
    if ip in _NAT64 or ip in _NAT64_LOCAL:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if ip in _SIX_TO_FOUR:
        return ipaddress.IPv4Address((int(ip) >> 80) & 0xFFFFFFFF)
    return None


def _is_internal(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """这个地址是不是「本机 / 内网 / 保留」？

    几个必须显式处理的点（都在本机实测过，不是照抄文档）：

    - **IPv4-mapped IPv6**：必须把 `ipv4_mapped` 拆出来**递归判**，而且这条现在
      就承重、不是"防低版本 Python"：`::ffff:127.0.0.1` 靠通用判据也能拦住
      （它映射到回环），但 `::ffff:100.64.0.1` **不能** —— 它自己是
      `is_private=False`、`is_reserved=False`，只有拆开后让 `_CGNAT` 够得着它。
      反过来 `::ffff:8.8.8.8` 也必须放行，所以不能图省事把整段 `::ffff:` 拉黑。
    - **转换前缀**（NAT64 / 6to4）：整段是 reserved/private，直接判会把合法映射一起拦掉，
      所以先拆出内嵌的 IPv4 再判（见 `_embedded_ipv4`）。
    - **CGNAT**（100.64.0.0/10）：`is_private` 判 False，`is_reserved` 也 False。漏它
      等于给云环境留一条到内网的路。
    - `169.254.169.254`（云元数据端点）落在 `is_link_local` 里，已被覆盖 —— 单列注释是
      因为它才是这类攻击最常见的**具体目标**。
    """
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return _is_internal(ip.ipv4_mapped)
        embedded = _embedded_ipv4(ip)
        if embedded is not None:
            # 只深一层：内嵌的一定是 IPv4，不会再进这个分支。
            return _is_internal(embedded)
    if ip.version == 4 and ip in _CGNAT:
        return True
    return (
        ip.is_private        # 10/8 · 172.16/12 · 192.168/16 · 127/8 · 0.0.0.0 · fc00::/7
        or ip.is_loopback
        or ip.is_link_local  # 169.254.0.0/16（含元数据端点）
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _blocked_reason(url: str) -> str | None:
    """模型给的 URL 能不能碰？不能则返回原因，能则 None。

    **必须先解析域名再判结果 IP**，这是整个判据的支点。判 `hostname` 字面量是漏的：

    - `http://localtest.me/` —— 主机名不含任何内网字样，但它**解析到 127.0.0.1**
      （本机实测：`getaddrinfo('localtest.me') -> ['127.0.0.1']`）
    - `http://2130706433/` / `http://0x7f000001/` / `http://127.1/` —— 127.0.0.1 的
      十进制 / 十六进制 / 短写法。本机 Windows 的 `getaddrinfo` 直接拒掉这三种，
      但那是**操作系统的行为**，Linux 上会正常解析 —— 安全判据不能建在
      "目标平台恰好也拒绝"上面。先解析就能把这些表示法一次性归一化掉。
    - 内网 IPv6 的方括号写法（`http://[::1]/`）：`urlparse().hostname` 会去掉方括号。

    解析失败 → **拒绝**（fail-closed）：解析不了就无从判断，而无从判断时放行等于没有判据。
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        return f"只允许 http/https，收到 {parsed.scheme or '(无协议)'}"
    host = parsed.hostname
    if not host:
        return "URL 里没有主机名"
    try:
        infos = socket.getaddrinfo(
            host, parsed.port or (443 if parsed.scheme == "https" else 80),
            proto=socket.IPPROTO_TCP,
        )
    except socket.gaierror as exc:
        return f"域名解析失败（{host}）: {exc}"
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if _is_internal(ip):
            return f"目标是内网/本机地址（{host} → {ip}），已拦截"
    return None


class _GuardedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """每一跳都重新过 `_blocked_reason`。

    **只数跳数不校验目标是完全不设防的**：`https://某公网站/redirect?to=http://169.254.169.254/`
    会被 urllib 自动跟随，而云元数据端点就在那后面 —— 首跳是公网地址，判据在首跳上是通过的。
    Python 移植版把 `MAX_REDIRECTS` 的注释写成「限制重定向次数防止 SSRF」，
    但次数上限对 SSRF 一点用没有：**一次跳转就够了**。

    `max_redirections` 是基类 `http_error_302` 读的类属性，设成我们的常量即可拿到跳数上限。
    """

    max_redirections = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001 - 基类签名
        reason = _blocked_reason(newurl)
        if reason is not None:
            raise urllib.error.HTTPError(
                newurl, code, f"重定向目标被拦截: {reason}", headers, fp
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener() -> urllib.request.OpenerDirector:
    """带沙箱重定向处理的 opener。

    显式建 opener 而不是用 `urlopen`：`urlopen` 走全局 opener，没法只给这一次调用
    换掉重定向处理器。
    """
    return urllib.request.build_opener(_GuardedRedirectHandler())


# ---------- HTML → 文本 ----------

_COMMENT = re.compile(r"<!--.*?-->", re.S)
#: 整块丢弃的标签。**`header` 刻意不在这个表里**：博客/文档站常把文章标题
#: （`<h1>`）放在 `<header>` 里，丢掉它等于丢掉最有辨识度的一行。
#: 其余几个（导航、侧栏、页脚、表单、内嵌框架、表单控件）在实测的三个真实页面上
#: 都不含正文 —— 留着只会挤占 `max_chars` 预算。
_DROP_BLOCK = re.compile(
    r"<(script|style|noscript|template|svg|head|nav|aside|footer|form|iframe"
    r"|button|select|option)\b.*?</\1\s*>",
    re.I | re.S,
)
_BR = re.compile(r"<br\s*/?>", re.I)
_BLOCK_END = re.compile(
    r"</(p|div|li|tr|h[1-6]|section|article|header|footer|blockquote|pre|table)\s*>",
    re.I,
)
_TAG = re.compile(r"<[^>]+>")
_TITLE = re.compile(r"<title[^>]*>(.*?)</title\s*>", re.I | re.S)
_BLANK_RUN = re.compile(r"\n{3,}")


def _html_to_text(raw: str) -> tuple[str, str]:
    """HTML → `(标题, 正文)`。返回正文而不是整页源码。

    给模型整页 HTML 是三重浪费：标签本身占大量 token、`<script>` 里的代码
    对回答没用、而且真正的内容被埋在里面反而不显眼。这不是「解析网页」，
    只是把明显不是内容的几类块去掉 —— **不做 DOM 解析**，因为那要引依赖，
    而这里要的是"够用的可读文本"。

    顺带说明一个刻意的顺序：`_DROP_BLOCK` 里包含 `head`，所以 `_TITLE`
    必须在丢弃**之前**取，否则标题永远为空。
    """
    title_match = _TITLE.search(raw)
    title = ""
    if title_match:
        title = html_mod.unescape(_TAG.sub("", title_match.group(1))).strip()

    text = _COMMENT.sub("", raw)
    text = _DROP_BLOCK.sub(" ", text)
    text = _BR.sub("\n", text)
    text = _BLOCK_END.sub("\n", text)
    text = _TAG.sub("", text)
    text = html_mod.unescape(text)
    lines = [line.strip() for line in text.splitlines()]
    text = "\n".join(line for line in lines if line)
    return title, _BLANK_RUN.sub("\n\n", text).strip()


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + (
        f"\n... [内容被截断，共 {len(text)} 字符，仅显示前 {limit} 字符] ..."
    )


def _decode(body: bytes, content_type: str) -> str:
    """按响应头里的 charset 解码，拿不到就按 utf-8 宽容解码。

    `charset` 只取 `;` 前那段：`text/html; charset=utf-8; boundary=x` 直接切会带上垃圾。
    """
    charset = "utf-8"
    if "charset=" in content_type.lower():
        charset = content_type.lower().split("charset=")[1].split(";")[0].strip().strip('"')
    try:
        return body.decode(charset, errors="replace")
    except LookupError:      # 服务端给了个不存在的编码名
        return body.decode("utf-8", errors="replace")


def _decompress(body: bytes, content_encoding: str) -> tuple[bytes | None, str | None]:
    """按 `Content-Encoding` 解压，返回 `(数据, 错误)`。

    **不解压就等于把压缩字节当文本喂给模型**，而且这个失败是静默的：
    `Content-Type` 通常是 `text/html`，会顺利通过 `_TEXTUAL` 检查，于是模型拿到
    一大片乱码还以为自己读到了页面。真跑验证时就是这么发现的
    （`python.org` 在没被要求的情况下回了 gzip）。

    **解压也要有上限**：`MAX_FETCH_BYTES` 限的是压缩前的字节数，而一个几十 KB 的
    gzip 炸弹能解出几个 GB。所以这里用带 `max_length` 的流式解压，解压后同样
    封在 `MAX_FETCH_BYTES` —— 两道限额各管一段，缺一个都能被打穿。

    认不出的编码（`br` / `zstd`，标准库里没有）**如实报错**，不退回原文：
    宁可让模型看到"这个编码我解不了"，也不要让它看到一片看起来像正文的乱码。
    """
    encoding = (content_encoding or "").strip().lower()
    if not encoding or encoding == "identity":
        return body, None
    if encoding in ("gzip", "x-gzip"):
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(body)) as stream:
                return stream.read(MAX_FETCH_BYTES), None
        except (OSError, EOFError) as exc:
            return None, f"gzip 解压失败: {exc}"
    if encoding == "deflate":
        # `deflate` 在野外的两种实现都有：zlib 包装过的和裸的。
        # 逐个试，而不是猜一个 —— 猜错就是整页乱码。
        for wbits in (zlib.MAX_WBITS, -zlib.MAX_WBITS):
            try:
                return zlib.decompressobj(wbits).decompress(body, MAX_FETCH_BYTES), None
            except zlib.error:
                continue
        return None, "deflate 解压失败（zlib 包装与裸流都试过了）"
    return None, f"不支持的内容编码: {encoding}"


_TEXTUAL = ("text/", "application/json", "application/xml", "+json", "+xml")


# ---------- web_fetch ----------

class WebFetchInput(BaseModel):
    url: str
    max_chars: int = Field(default=DEFAULT_MAX_CHARS, ge=500, le=200_000)


class WebFetchTool(Tool):
    name = "web_fetch"
    description = (
        "抓取一个网页并提取可读正文（去掉标签与脚本）。"
        "用 web_search 拿到链接后再用它看具体内容。"
    )
    input_model = WebFetchInput

    @classmethod
    def is_read_only(cls) -> bool:
        """不改 workspace → 可与其它只读工具并发。

        注意「只读」在这里只表示**不改本地文件**，不表示"没有副作用"：它会把请求发出去。
        权限层看到的是「网络外发」这一类（见 `permissions._irreversible_kind`），
        两件事互不冲突 —— 并发与否是调度问题，要不要人点头是判定问题。
        """
        return True

    def execute(self, args: WebFetchInput, ctx: ToolContext) -> ToolResult:
        reason = _blocked_reason(args.url)
        if reason is not None:
            return ToolResult.fail(f"[SSRF 拦截] {reason}\nURL: {args.url}")

        request = urllib.request.Request(
            args.url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,text/plain;q=0.9,*/*;q=0.5",
                "Accept-Encoding": COMPRESSION_HINT,
            },
        )
        try:
            with _opener().open(request, timeout=FETCH_TIMEOUT) as response:
                status = response.status
                content_type = response.headers.get("Content-Type", "")
                content_encoding = response.headers.get("Content-Encoding", "")
                body = response.read(MAX_FETCH_BYTES)
                final_url = response.geturl()
        except urllib.error.HTTPError as exc:
            # 重定向被拦截也是从这条路出来的（我们在 redirect_request 里抛 HTTPError）
            detail = exc.reason if isinstance(exc.reason, str) else str(exc.reason)
            return ToolResult.fail(f"HTTP {exc.code}: {detail}\nURL: {args.url}")
        except urllib.error.URLError as exc:
            return ToolResult.fail(f"请求失败: {exc.reason}\nURL: {args.url}")
        except OSError as exc:
            return ToolResult.fail(f"请求失败: {type(exc).__name__}: {exc}\nURL: {args.url}")

        body, decode_error = _decompress(body, content_encoding)
        if decode_error is not None:
            # 宁可报错也不把压缩字节当正文交出去 —— 那是一片**看起来像内容**的乱码
            return ToolResult.fail(f"{decode_error}\nURL: {final_url}")

        if not any(kind in content_type.lower() for kind in _TEXTUAL):
            return ToolResult.fail(
                f"不支持的内容类型: {content_type or '(未声明)'}\n"
                f"这个工具只处理文本类响应，二进制内容（图片/PDF/压缩包）请让用户自行下载。"
            )

        text = _decode(body, content_type)
        title = ""
        if "html" in content_type.lower():
            title, text = _html_to_text(text)

        header = [
            f"URL: {final_url}",
            f"STATUS: {status}",
            f"CONTENT_TYPE: {content_type}",
        ]
        if title:
            header.append(f"TITLE: {title}")
        if final_url != args.url:
            header.append(f"（从 {args.url} 重定向而来）")

        return ToolResult.ok(
            "\n".join(header) + "\n\n" + _truncate(text, args.max_chars),
            data={
                "url": final_url,
                "status": status,
                "content_type": content_type,
                "title": title,
                "chars": len(text),
            },
        )


# ---------- web_search ----------

#: 搜索后端。**默认 `ddg` 是对齐 TS 原版的刻意选择**（原版 `web-search.ts` 走 DDG Lite），
#: 不是因为它在本机好用 —— 本机根本连不上，理由见下面 `_DDG_RESULT` 的说明。
#: 用 `CODEAGENT_SEARCH_BACKEND=bing` 切换。
DEFAULT_SEARCH_BACKEND = "ddg"

_BING_RESULT = re.compile(r'<li class="b_algo".*?</li>', re.S)
_BING_LINK = re.compile(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_BING_SNIPPET = re.compile(r"<p[^>]*>(.*?)</p>", re.S)

#: DuckDuckGo Lite 的结果结构。
#:
#: ⚠️ **这条解析路径在本机没有真跑验证过**：`lite.duckduckgo.com` 直连超时
#: （8.2s，timeout）、走本机代理 SSL EOF（7.6s）—— 两条路都不通，实测于 2026-09-11。
#: 它是按 DDG Lite 的已知页面结构写的，**没有对着真实响应校准过**。
#: 真跑验证走的是 `bing` 后端（见 m9verify/web/）。这一条如实写在这里，
#: 而不是让它看起来像验证过 —— 那正是本项目最忌讳的"机制在、但没人知道它没生效"。
#: 整段取 `<a ...class="result-link"...>标题</a>`，href 再单独从开标签里抽。
#: 不用一条正则同时抓 class 和 href —— 那会把两者的**出现顺序**写死
#: （`<a href=... class=...>` 就匹配不上），而属性顺序是 HTML 里最不该依赖的东西。
_DDG_RESULT = re.compile(
    r'(<a\b[^>]*class="[^"]*result-link[^"]*"[^>]*>)(.*?)</a>', re.I | re.S
)
_HREF = re.compile(r'href="([^"]+)"', re.I)
_DDG_SNIPPET = re.compile(r'<td[^>]*class="[^"]*result-snippet[^"]*"[^>]*>(.*?)</td>', re.I | re.S)

SEARCH_BACKENDS: dict[str, str] = {
    "ddg": "https://lite.duckduckgo.com/lite/?q={query}",
    "bing": "https://cn.bing.com/search?q={query}",
}


def _search_backend() -> str:
    name = os.environ.get("CODEAGENT_SEARCH_BACKEND", DEFAULT_SEARCH_BACKEND).strip().lower()
    return name if name in SEARCH_BACKENDS else DEFAULT_SEARCH_BACKEND


def _strip_tags(fragment: str) -> str:
    return html_mod.unescape(_TAG.sub("", fragment)).strip()


def _parse_bing(html: str, limit: int) -> list[dict[str, str]]:
    """Bing 结果 → `[{title, url, snippet}]`。

    结构是照着**真实响应**写的（`m9verify/web/bing_sample.html`，
    `cn.bing.com/search?q=...` 抓下来的 98KB 页面）：结果块是 `li.b_algo`，
    标题链接在 `h2 > a`，摘要是块内第一个 `<p>`。
    """
    results: list[dict[str, str]] = []
    for block in _BING_RESULT.findall(html):
        match = _BING_LINK.search(block)
        if not match:
            continue
        snippet = _BING_SNIPPET.search(block)
        results.append({
            "title": _strip_tags(match.group(2)),
            "url": html_mod.unescape(match.group(1)),
            "snippet": _strip_tags(snippet.group(1)) if snippet else "",
        })
        if len(results) >= limit:
            break
    return results


def _parse_ddg(html: str, limit: int) -> list[dict[str, str]]:
    """DDG Lite 结果 → `[{title, url, snippet}]`。**未经真跑校准**，见 `_DDG_RESULT` 的说明。"""
    links = _DDG_RESULT.findall(html)
    snippets = _DDG_SNIPPET.findall(html)
    results: list[dict[str, str]] = []
    for index, (open_tag, title) in enumerate(links[:limit]):
        href = _HREF.search(open_tag)
        if href is None:
            continue                      # 没有 href 的结果条目对模型没用，跳过而不是塞空串
        results.append({
            "title": _strip_tags(title),
            # DDG 的链接是 `//duckduckgo.com/l/?uddg=<百分号编码的真实地址>` 形态的跳转链接，
            # 这里**不还原**：还原逻辑依赖它的参数名，而这条路径本身就没校准过，
            # 两层未验证叠在一起只会更难查。如实留着，让模型自己决定要不要跟。
            "url": html_mod.unescape(href.group(1)),
            "snippet": _strip_tags(snippets[index]) if index < len(snippets) else "",
        })
    return results


class WebSearchInput(BaseModel):
    query: str
    max_results: int = Field(default=5, ge=1, le=20)


class WebSearchTool(Tool):
    name = "web_search"
    description = (
        "用搜索引擎查公开网络，返回标题/链接/摘要。"
        "需要当前信息或工作区之外的资料时用它，再用 web_fetch 看具体页面。"
    )
    input_model = WebSearchInput

    @classmethod
    def is_read_only(cls) -> bool:
        return True

    def execute(self, args: WebSearchInput, ctx: ToolContext) -> ToolResult:
        backend = _search_backend()
        url = SEARCH_BACKENDS[backend].format(query=urllib.parse.quote_plus(args.query))

        # 搜索后端的地址是**配置**不是模型输出，所以这里只查协议、不查内网 ——
        # 判据的边界是「这个 URL 从哪来」：模型给的（web_fetch）全查，
        # 运维配的（这个模板）只查协议。否则自建的 SearxNG 会被自己的防护挡在门外。
        # 但查询词来自模型，所以必须 quote_plus（上面已做），不能拼字符串。
        scheme = urllib.parse.urlparse(url).scheme.lower()
        if scheme not in ALLOWED_SCHEMES:
            return ToolResult.fail(f"搜索后端协议不支持: {scheme}")

        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,*/*;q=0.5",
                "Accept-Encoding": COMPRESSION_HINT,
            },
        )
        try:
            with _opener().open(request, timeout=FETCH_TIMEOUT) as response:
                body = response.read(MAX_FETCH_BYTES)
                content_type = response.headers.get("Content-Type", "")
                content_encoding = response.headers.get("Content-Encoding", "")
        except urllib.error.HTTPError as exc:
            return ToolResult.fail(f"搜索请求 HTTP {exc.code}: {args.query}")
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            return ToolResult.fail(
                f"搜索请求失败（后端 {backend}）: {reason}\n"
                f"可用 CODEAGENT_SEARCH_BACKEND 切换后端"
                f"（可选: {', '.join(sorted(SEARCH_BACKENDS))}）"
            )

        body, decode_error = _decompress(body, content_encoding)
        if decode_error is not None:
            # 同 web_fetch：宁可报错，也不把压缩字节当正文送去解析 ——
            # 那样只会"解析到 0 条结果"，把编码问题伪装成后端改版。
            return ToolResult.fail(f"{decode_error}（后端 {backend}）")

        parser = _parse_bing if backend == "bing" else _parse_ddg
        results = parser(_decode(body, content_type), args.max_results)

        if not results:
            return ToolResult.ok(
                f"没有解析到结果（后端 {backend}，查询 {args.query!r}）。\n"
                "可能是后端改了页面结构，或本次被反爬拦截。",
                data={"query": args.query, "backend": backend, "results": []},
            )

        lines = [f"搜索: {args.query}（后端 {backend}，{len(results)} 条）", ""]
        for index, item in enumerate(results, 1):
            lines.append(f"{index}. {item['title']}")
            lines.append(f"   {item['url']}")
            if item["snippet"]:
                lines.append(f"   {item['snippet']}")
        return ToolResult.ok(
            "\n".join(lines),
            data={"query": args.query, "backend": backend, "results": results},
        )


def build_web_tools() -> list[Tool]:
    """一处构造，多个入口共用（同 `build_ask_tool` / `build_skill_tools` 的惯例）。"""
    return [WebFetchTool(), WebSearchTool()]
