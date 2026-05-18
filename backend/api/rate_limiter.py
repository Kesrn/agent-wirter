"""内存限速器

基于滑动窗口的简单内存限速实现。
适用于单实例部署；多实例需替换为 Redis 方案。
"""

import time
from collections import defaultdict
from fastapi import HTTPException

from config.settings import settings


class _SlidingWindowLimiter:
    """滑动窗口限速器"""

    def __init__(self, max_requests: int, window_seconds: int = 60):
        self._max_requests = max_requests
        self._window_seconds = window_seconds
        # key -> list of timestamps
        self._windows: dict[str, list[float]] = defaultdict(list)
        self._last_cleanup = time.monotonic()

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

    def check(self, key: str) -> None:
        """检查是否超限，超限则抛出 429。

        Args:
            key: 限速标识，通常是 user_id 或 IP
        """
        self._cleanup_stale()
        now = time.monotonic()
        cutoff = now - self._window_seconds

        # 清理过期记录
        timestamps = self._windows[key]
        self._windows[key] = [ts for ts in timestamps if ts > cutoff]

        if len(self._windows[key]) >= self._max_requests:
            raise HTTPException(
                status_code=429,
                detail=f"请求过于频繁，每分钟最多 {self._max_requests} 次",
            )

        self._windows[key].append(now)


# 全局实例：Agent 相关端点限速
agent_limiter = _SlidingWindowLimiter(
    max_requests=settings.AGENT_RATE_LIMIT_PER_MINUTE,
    window_seconds=60,
)

# 全局实例：认证端点限速（login/register）
auth_limiter = _SlidingWindowLimiter(
    max_requests=settings.AUTH_RATE_LIMIT_PER_MINUTE,
    window_seconds=60,
)
