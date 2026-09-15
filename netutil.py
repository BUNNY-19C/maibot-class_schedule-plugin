"""从 http/https 地址抓取文本资源，并做 SSRF 防护。

课表 ics 与法定节假日 JSON 都走这里，共用同一套校验。

安全约束（按项目规定实现，不提供绕过开关）：

1. 只允许 ``http`` / ``https``，其余协议（``file`` / ``ftp`` / ``gopher`` …）一律拒绝；
2. 发请求前解析主机名，只要解析出的任一地址是
   回环 / 私有 / 链路本地 / 保留 / 组播 / 未指定地址，就拒绝；
3. 每一跳重定向都重新校验，防止「公网地址 302 到内网」；
4. 限制响应体大小，避免被超大文件拖垮。

因此校园**内网**教务地址无法直接导入——那类课表请导出成文件后放进
本地 ics 目录（见 README「导入课表的三种方式」）。
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import urllib.error
import urllib.request
from collections.abc import Iterable
from urllib.parse import urlsplit

from .ics_parser import decode_ics_bytes

__all__ = [
    "FetchError",
    "UnsafeUrlError",
    "fetch_ics",
    "fetch_text",
    "validate_url",
]

ALLOWED_SCHEMES = ("http", "https")
MAX_REDIRECTS = 3
DEFAULT_TIMEOUT = 20
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
USER_AGENT = "MaiBot-ClassSchedule/1.0 (+ics-import)"
_ACCEPT_HEADER = "text/calendar, application/json, text/plain, */*"

#: 明确指向本机/内网的主机名后缀
_BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa")
_BLOCKED_HOSTS = frozenset({"localhost", "ip6-localhost", "ip6-loopback"})


class UnsafeUrlError(ValueError):
    """URL 未通过安全校验。"""


class FetchError(RuntimeError):
    """抓取或解码远端文本失败。"""


def _blocked_reason(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    """返回该地址被禁止的原因，安全时返回空串。"""
    if ip.is_loopback:
        return "回环地址"
    if ip.is_private:
        return "私有地址"
    if ip.is_link_local:
        return "链路本地地址"
    if ip.is_multicast:
        return "组播地址"
    if ip.is_reserved:
        return "保留地址"
    if ip.is_unspecified:
        return "未指定地址"
    # 兜底：运营商级 NAT（100.64/10）等"非公网"段，is_private 在不同
    # Python 版本里口径不一致，用 is_global 收口
    if not ip.is_global:
        return "非公网地址"
    return ""


class _NotAnIpLiteral(ValueError):
    """内部信号：该字符串不是 IP 字面量，需要走 DNS 解析。"""


def _check_address(raw: str) -> None:
    """校验单个 IP 字面量；不是 IP 字面量时按域名交给调用方解析。"""
    text = raw.split("%", 1)[0]  # 去掉 IPv6 的 scope id
    try:
        ip: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address(text)
    except ValueError as exc:
        raise _NotAnIpLiteral(str(exc)) from exc

    # ::ffff:127.0.0.1 这类 IPv4 映射地址要先还原，否则 is_loopback 判断会漏
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped

    reason = _blocked_reason(ip)
    if reason:
        raise UnsafeUrlError(f"目标地址被拒绝（{reason}）: {raw}")


def _matches_allow_host(host: str, port: int, allow_hosts: Iterable[str]) -> bool:
    """主机:端口是否命中显式白名单（形如 ``127.0.0.1`` 或 ``127.0.0.1:3000``）。

    白名单是**窄口径**的例外：只放开点名的那个地址，不是"允许内网"这种开关。
    """
    candidates = {host.lower().rstrip("."), f"{host.lower().rstrip('.')}:{port}"}
    for entry in allow_hosts or ():
        text = str(entry or "").strip().lower()
        if text and text in candidates:
            return True
    return False


def validate_url(url: str, *, allow_hosts: Iterable[str] = ()) -> str:
    """校验 URL 是否可以安全请求，返回原 URL，不合法则抛异常。

    Args:
        url: 待校验地址。
        allow_hosts: 显式放行的 ``host`` 或 ``host:port`` 列表。默认空 = 严格模式
            （私网/回环一律拒绝）。只有平台自己给出的文件地址才需要用到它，
            且必须由使用者在配置里点名，避免退化成"允许内网"的通用开关。

    会做 DNS 解析，因此包含阻塞 IO；在事件循环中请通过
    :func:`fetch_text` 调用（内部走线程池）。
    """
    raw = str(url or "").strip()
    if not raw:
        raise UnsafeUrlError("URL 为空")

    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UnsafeUrlError(f"只允许 http/https 协议，收到: {scheme or '<空>'}")

    host = parts.hostname
    if not host:
        raise UnsafeUrlError("URL 缺少主机名")

    default_port = 443 if scheme == "https" else 80
    try:
        port = parts.port or default_port
    except ValueError as exc:
        raise UnsafeUrlError(f"端口不合法: {raw}") from exc

    # 命中白名单的地址跳过地址类限制（协议与端口合法性仍然要过）
    if _matches_allow_host(host, port, allow_hosts):
        return raw

    lowered = host.lower().rstrip(".")
    if lowered in _BLOCKED_HOSTS or lowered.endswith(_BLOCKED_HOST_SUFFIXES):
        raise UnsafeUrlError(f"主机名被拒绝: {host}")

    # 字面量 IP（含 [::1] 这类带方括号的 IPv6）直接校验，不必解析
    try:
        _check_address(lowered.strip("[]"))
        return raw
    except _NotAnIpLiteral:
        pass

    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise FetchError(f"域名解析失败: {host}（{exc}）") from exc

    if not infos:
        raise FetchError(f"域名没有解析到任何地址: {host}")

    for info in infos:
        try:
            _check_address(str(info[4][0]))
        except _NotAnIpLiteral as exc:
            raise FetchError(f"域名解析到无法识别的地址: {info[4][0]}") from exc
    return raw


class _GuardedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """每一跳重定向都重新做安全校验。"""

    max_redirections = MAX_REDIRECTS

    def __init__(self, allow_hosts: Iterable[str] = ()) -> None:
        super().__init__()
        self._allow_hosts = tuple(allow_hosts or ())

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        # 白名单要一起带上：否则平台给的本地地址一旦经过重定向就会被拦下
        validate_url(newurl, allow_hosts=self._allow_hosts)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fetch_sync(
    url: str, timeout: int, max_bytes: int, allow_hosts: Iterable[str] = ()
) -> str:
    """在线程中执行的同步抓取（含校验）。"""
    allow_hosts = tuple(allow_hosts or ())
    validate_url(url, allow_hosts=allow_hosts)

    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": _ACCEPT_HEADER},
    )
    opener = urllib.request.build_opener(_GuardedRedirectHandler(allow_hosts))

    try:
        with opener.open(request, timeout=timeout) as response:
            # 重定向后的最终地址再校验一次
            validate_url(response.geturl(), allow_hosts=allow_hosts)
            data = response.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise FetchError(
                    f"文件超过大小上限 {max_bytes // 1024} KB，已中止导入"
                )
            charset = response.headers.get_content_charset() or ""
    except UnsafeUrlError:
        raise
    except urllib.error.HTTPError as exc:
        raise FetchError(f"服务器返回 HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise FetchError(f"网络请求失败: {exc.reason}") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise FetchError("网络请求超时") from exc

    if not data.strip():
        raise FetchError("服务器返回了空内容")
    return decode_ics_bytes(data, charset)


async def fetch_text(
    url: str,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    max_bytes: int = DEFAULT_MAX_BYTES,
    allow_hosts: Iterable[str] = (),
) -> str:
    """异步抓取远端文本（ics 或 json 都走这里）。

    所有阻塞操作（DNS 校验、连接、读体）都在线程池里完成，
    并且**发请求前**已经过 :func:`validate_url` 的协议与地址校验。

    ``allow_hosts`` 是窄口径例外，默认空即严格模式；只有"平台自己给出的
    文件地址"才需要它，且必须由使用者在配置里点名。
    """
    return await asyncio.to_thread(_fetch_sync, url, timeout, max_bytes, allow_hosts)


async def fetch_ics(
    url: str,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> str:
    """异步抓取 ICS 文本；与 :func:`fetch_text` 同一实现，保留语义化的名字。"""
    return await fetch_text(url, timeout=timeout, max_bytes=max_bytes)
