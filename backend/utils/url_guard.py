"""出站 URL 安全校验（SSRF 防护）与受控 GET。

本项目有几处“服务器替用户去请求一个地址”的场景：网页搜索的结果页抓取、用户自填的
搜索服务地址、用户自填的大模型地址。这些地址都不可信，直接请求会把服务端变成内网
探针（云元数据 `169.254.169.254`、本机数据库、内网管理页）。

这里提供两层能力：

- `validate_url()`：解析并校验目标地址，拒绝非 http(s)、带用户信息、私网/回环/
  链路本地/元数据地址、以及不允许的端口；
- `guarded_get()`：在 `validate_url()` 之上手动处理重定向（每一跳都重新校验）、
  限制响应体大小与 Content-Type，并**把连接钉在校验过的 IP 上**（DNS 重绑定防护）。

错误信息只写原因类别，不回显解析出的内网 IP 或完整 URL；日志只记主机名。
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

logger = logging.getLogger(__name__)


class UnsafeURLError(Exception):
    """目标是不可访问地址。异常信息只描述原因类别，不含解析结果与完整 URL。"""


_ALLOWED_SCHEMES = frozenset({"http", "https"})
_DEFAULT_PORTS = {"http": 80, "https": 443}

# 云厂商元数据地址：无论 allow_private 是否开启都拒绝。
_METADATA_IPS = frozenset(
    {
        ipaddress.ip_address("169.254.169.254"),  # AWS / GCP / Azure / 阿里云等
        ipaddress.ip_address("fd00:ec2::254"),  # AWS IPv6 元数据
    }
)
_METADATA_HOSTNAMES = frozenset(
    {"metadata.google.internal", "metadata.goog", "instance-data"}
)

# 运营商级 NAT 段：不是全局可路由，按“私网”对待。
_SHARED_ADDRESS_SPACE = ipaddress.ip_network("100.64.0.0/10")

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


def _normalize_address(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """把字符串解析成 IP；IPv4 映射的 IPv6（::ffff:127.0.0.1）还原成 IPv4。"""
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def is_ip_allowed(ip: str, allow_private: bool = False) -> bool:
    """判断单个 IP 是否可访问（不涉及 DNS，便于单独测试）。

    - 元数据地址永远拒绝；
    - 组播、保留、未指定地址永远拒绝；
    - 回环 / 私网 / 链路本地 / CGNAT：`allow_private=True` 时放行；
    - 其余必须是全局可路由地址。
    """
    address = _normalize_address(ip)
    if address is None:
        return False
    if address in _METADATA_IPS:
        return False
    if address.is_multicast or address.is_unspecified or address.is_reserved:
        return False
    if (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or (address.version == 4 and address in _SHARED_ADDRESS_SPACE)
    ):
        return allow_private
    return address.is_global


async def _resolve_host(host: str, port: int) -> list[str]:
    """解析主机名的所有地址（异步，不阻塞事件循环）。

    单独抽成函数是为了测试可以 monkeypatch 它，从而完全不触发真实 DNS 查询。
    """
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses: list[str] = []
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        candidate = str(sockaddr[0])
        if candidate not in addresses:
            addresses.append(candidate)
    return addresses


@dataclass(frozen=True)
class ResolvedURL:
    """校验通过后的目标信息。"""

    url: str
    scheme: str
    hostname: str
    port: int
    addresses: tuple[str, ...]


async def validate_url(
    url: str,
    *,
    allow_private: bool = False,
    allowed_ports: set[int] | frozenset[int] | None = None,
) -> ResolvedURL:
    """校验出站 URL；不通过抛 `UnsafeURLError`。

    Args:
        url: 待校验地址。
        allow_private: 是否允许回环/私网/链路本地地址（桌面端连本机 Ollama 需要）。
            元数据地址与非法 scheme 不受此开关影响，永远拒绝。
        allowed_ports: 允许的端口集合；None 表示不限制。
    """
    if not isinstance(url, str) or not url.strip():
        raise UnsafeURLError("地址为空")

    candidate = url.strip()
    try:
        parts = urlsplit(candidate)
    except ValueError as exc:
        raise UnsafeURLError("地址格式不正确") from exc

    scheme = (parts.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise UnsafeURLError("仅允许 http/https 地址")

    # urlsplit 已经把 userinfo 单独解析出来：http://good.com@127.0.0.1/ 的真实
    # hostname 是 127.0.0.1，username 是 good.com。一律拒绝带用户信息的地址。
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise UnsafeURLError("地址不允许携带用户信息")

    host = (parts.hostname or "").strip()
    if not host:
        raise UnsafeURLError("地址缺少主机名")

    try:
        port = parts.port or _DEFAULT_PORTS[scheme]
    except ValueError as exc:
        raise UnsafeURLError("端口不合法") from exc
    if allowed_ports is not None and port not in allowed_ports:
        raise UnsafeURLError("目标端口不被允许")

    # 末尾点（localhost.）与大小写都要归一再判断
    normalized_host = host.rstrip(".").lower()
    if normalized_host in _METADATA_HOSTNAMES:
        logger.warning("outbound url rejected reason=metadata_host host=%s", normalized_host)
        raise UnsafeURLError("目标地址不被允许")

    literal = _normalize_address(normalized_host)
    if literal is not None:
        if not is_ip_allowed(str(literal), allow_private=allow_private):
            logger.warning("outbound url rejected reason=ip_not_allowed host=%s", normalized_host)
            raise UnsafeURLError("目标地址不被允许")
        return ResolvedURL(
            url=candidate,
            scheme=scheme,
            hostname=normalized_host,
            port=port,
            addresses=(str(literal),),
        )

    if normalized_host == "localhost" and not allow_private:
        logger.warning("outbound url rejected reason=loopback host=localhost")
        raise UnsafeURLError("目标地址不被允许")

    try:
        addresses = await _resolve_host(normalized_host, port)
    except Exception as exc:  # noqa: BLE001 - 解析失败统一按不可访问处理
        logger.warning("outbound url rejected reason=dns_failed host=%s error=%s", normalized_host, type(exc).__name__)
        raise UnsafeURLError("域名解析失败") from exc

    if not addresses:
        raise UnsafeURLError("域名解析失败")

    # 只要有一个解析结果不允许就整体拒绝（防止“一个公网 + 一个内网”的绕过）
    for address in addresses:
        if not is_ip_allowed(address, allow_private=allow_private):
            logger.warning(
                "outbound url rejected reason=resolved_to_blocked_address host=%s",
                normalized_host,
            )
            raise UnsafeURLError("目标地址不被允许")

    return ResolvedURL(
        url=candidate,
        scheme=scheme,
        hostname=normalized_host,
        port=port,
        addresses=tuple(addresses),
    )


def _pick_address(resolved: ResolvedURL) -> str:
    """选一个用于建连的地址；优先 IPv4（部署环境里 IPv6 出网不一定可用）。"""
    for address in resolved.addresses:
        normalized = _normalize_address(address)
        if normalized is not None and normalized.version == 4:
            return str(normalized)
    return resolved.addresses[0]


def _build_pinned_request(
    client: httpx.AsyncClient,
    resolved: ResolvedURL,
    *,
    headers: dict[str, str],
) -> httpx.Request:
    """把请求地址换成“校验过的 IP”，同时用 Host 头与 SNI 保持原主机名语义。

    这样即使攻击者在 DNS 校验之后把域名重新指向内网（DNS 重绑定），实际连接仍然是
    我们校验过的那个 IP。https 场景通过 httpx/httpcore 的 `sni_hostname` 扩展让 TLS
    仍按原主机名校验证书（已用本地自签证书验证过该机制在当前版本可用）。
    """
    address = _pick_address(resolved)
    parts = urlsplit(resolved.url)
    default_port = _DEFAULT_PORTS[resolved.scheme]

    url_host = f"[{address}]" if ":" in address else address
    netloc = url_host if resolved.port == default_port else f"{url_host}:{resolved.port}"
    pinned_url = urlunsplit(
        (resolved.scheme, netloc, parts.path or "/", parts.query, "")
    )

    host_header = (
        resolved.hostname
        if resolved.port == default_port
        else f"{resolved.hostname}:{resolved.port}"
    )
    request = client.build_request(
        "GET",
        pinned_url,
        headers={**headers, "Host": host_header},
    )
    if resolved.scheme == "https":
        request.extensions["sni_hostname"] = resolved.hostname
    return request


async def _read_limited(response: httpx.Response, max_bytes: int) -> bytes:
    """流式读响应体，超过 max_bytes 立即中止。"""
    buffer: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > max_bytes:
            raise UnsafeURLError("响应体超过大小上限")
        buffer.append(chunk)
    return b"".join(buffer)


async def guarded_get(
    url: str,
    *,
    allow_private: bool = False,
    max_bytes: int = 2 * 1024 * 1024,
    max_redirects: int = 0,
    allowed_content_types: set[str] | frozenset[str] | None = None,
    allowed_ports: set[int] | frozenset[int] | None = None,
    timeout: float = 10.0,
    headers: dict[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.Response:
    """带校验的 GET：逐跳校验重定向、限制大小与类型、把连接钉在校验过的 IP 上。

    Args:
        transport: 仅测试用，注入 `httpx.MockTransport` 以免发真实请求。
    """
    request_headers = dict(headers or {})
    current = url
    redirects = 0

    # trust_env=False：不继承 HTTP(S)_PROXY。否则代理会自己去解析域名并建连，
    # IP 固定与“每跳校验”都会失效，等于把 SSRF 防护交给代理的判断。
    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=timeout,
        trust_env=False,
        transport=transport,
    ) as client:
        while True:
            resolved = await validate_url(
                current,
                allow_private=allow_private,
                allowed_ports=allowed_ports,
            )
            request = _build_pinned_request(client, resolved, headers=request_headers)
            response = await client.send(request, stream=True)
            try:
                if response.status_code in _REDIRECT_STATUSES:
                    location = response.headers.get("location")
                    if not location:
                        raise UnsafeURLError("重定向缺少目标地址")
                    if redirects >= max_redirects:
                        raise UnsafeURLError("重定向次数超过上限")
                    redirects += 1
                    current = urljoin(current, location)
                    continue

                content_type = (
                    response.headers.get("content-type", "").split(";")[0].strip().lower()
                )
                if allowed_content_types is not None and content_type not in allowed_content_types:
                    raise UnsafeURLError("响应类型不被允许")

                body = await _read_limited(response, max_bytes)
                return httpx.Response(
                    status_code=response.status_code,
                    headers=response.headers,
                    content=body,
                    request=request,
                )
            finally:
                await response.aclose()
