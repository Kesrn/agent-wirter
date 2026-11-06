"""限流器与客户端 IP 解析。

基于滑动窗口的简单内存实现，适用于单实例部署。

多实例部署替换位置：`_SlidingWindowLimiter.check(key)` 就是限流存储的全部接口，
换成 Redis 实现（例如 INCR + EXPIRE，或 Lua 脚本做滑动窗口）时保持这个方法签名和
“超限抛 HTTPException(429)”的语义即可，调用方（api/auth.py、api/llm_settings.py）
不需要改动。本任务不实现 Redis。
"""

import ipaddress
import time
from collections import defaultdict

from fastapi import HTTPException, Request

from config.settings import settings


class _SlidingWindowLimiter:
    """滑动窗口限速器（进程内内存实现，单实例语义）。"""

    def __init__(self, max_requests: int, window_seconds: int = 60):
        self._max_requests = max_requests
        self._window_seconds = window_seconds
        # key -> list of timestamps
        self._windows: dict[str, list[float]] = defaultdict(list)
        self._last_cleanup = time.monotonic()

    @property
    def window_seconds(self) -> int:
        """窗口长度，429 响应里的 Retry-After 用它。"""
        return self._window_seconds

    @property
    def max_requests(self) -> int:
        """窗口内允许的请求数（只读，便于诊断与测试断言）。"""
        return self._max_requests

    def _cleanup_stale(self) -> None:
        """Remove keys with no recent activity to prevent unbounded growth."""
        now = time.monotonic()
        if now - self._last_cleanup < self._window_seconds:
            return
        cutoff = now - self._window_seconds
        stale_keys = [
            k for k, ts in self._windows.items()
            if not ts or ts[-1] < cutoff
        ]
        for k in stale_keys:
            del self._windows[k]
        self._last_cleanup = now

    def check(self, key: str, *, with_retry_after: bool = False) -> None:
        """检查是否超限，超限则抛出 429。

        Args:
            key: 限速标识，通常是 user_id 或 IP
            with_retry_after: 超限时是否带 Retry-After 响应头（秒）。默认不加，
                保持既有调用方（如 api/llm_settings.py）的响应完全不变。
        """
        self._cleanup_stale()
        now = time.monotonic()
        cutoff = now - self._window_seconds

        # 清理过期记录
        timestamps = self._windows[key]
        self._windows[key] = [ts for ts in timestamps if ts > cutoff]

        if len(self._windows[key]) >= self._max_requests:
            headers = {"Retry-After": str(self._window_seconds)} if with_retry_after else None
            raise HTTPException(
                status_code=429,
                detail=f"请求过于频繁，每分钟最多 {self._max_requests} 次",
                headers=headers,
            )

        self._windows[key].append(now)

    def reset(self) -> None:
        """清空所有窗口。

        供测试隔离使用（限流器是进程级全局状态），也便于运维在排查时手工放行。
        换成 Redis 实现时对应“删除这些 key”。
        """
        self._windows.clear()
        self._last_cleanup = time.monotonic()


def _parse_forwarded_ip(value: str) -> str | None:
    """把 X-Forwarded-For 里的单个条目解析成 IP，无法解析返回 None。"""
    candidate = value.strip()
    if not candidate:
        return None
    # 部分代理会写成 "1.2.3.4:5678" 或 "[2001:db8::1]:443"
    if candidate.startswith("["):
        candidate = candidate[1:candidate.find("]")] if "]" in candidate else candidate[1:]
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


def get_client_ip(request: Request) -> str:
    """取用于限流的客户端 IP。

    X-Forwarded-For 是客户端可以伪造的，只有在明确知道前面有几层自己控制的代理时
    才能用：

    - TRUSTED_PROXY_COUNT == 0（默认）：完全忽略该头，只用 TCP 对端地址；
    - TRUSTED_PROXY_COUNT == N > 0：取该头从右往左第 N 个地址（右侧由我们自己的
      代理追加，左侧可能是伪造值），头缺失或取到的条目不是合法 IP 时回退到对端地址。

    回退到对端地址是安全兜底：宁可把多个真实客户端算成同一个 key（更严），
    也不要用伪造值把攻击流量分散成无数个 key（更松）。
    """
    peer = request.client.host if request.client else "unknown"

    trusted_proxy_count = settings.TRUSTED_PROXY_COUNT
    if trusted_proxy_count <= 0:
        return peer

    forwarded = request.headers.get("x-forwarded-for", "")
    if not forwarded:
        return peer

    entries = [entry for entry in forwarded.split(",") if entry.strip()]
    if len(entries) < trusted_proxy_count:
        return peer

    parsed = _parse_forwarded_ip(entries[-trusted_proxy_count])
    return parsed or peer


# 全局实例：Agent 相关端点限速
agent_limiter = _SlidingWindowLimiter(
    max_requests=settings.AGENT_RATE_LIMIT_PER_MINUTE,
    window_seconds=60,
)

# 全局实例：认证端点按用户名/身份限速（login/register），
# 同时被 api/llm_settings.py 复用于 llm_settings:{user_id}。上限与行为都不要改。
auth_limiter = _SlidingWindowLimiter(
    max_requests=settings.AUTH_RATE_LIMIT_PER_MINUTE,
    window_seconds=60,
)

# 全局实例：认证端点的 IP 维度限速（login/register 共用同一个计数）。
# 独立实例，避免和上面的按身份限流互相影响。
auth_ip_limiter = _SlidingWindowLimiter(
    max_requests=settings.AUTH_IP_RATE_LIMIT_PER_MINUTE,
    window_seconds=60,
)
