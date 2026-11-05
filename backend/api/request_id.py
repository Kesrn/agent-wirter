"""请求级追踪 ID 的纯 ASGI 中间件与上下文。"""

from __future__ import annotations

import re
import uuid
from contextvars import ContextVar
from typing import Any, Awaitable, Callable


request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _request_id_from_scope(scope: dict[str, Any]) -> str:
    for name, value in scope.get("headers") or []:
        if name.lower() != b"x-request-id":
            continue
        try:
            candidate = value.decode("ascii")
        except UnicodeDecodeError:
            break
        if _VALID_REQUEST_ID.fullmatch(candidate):
            return candidate
        break
    return uuid.uuid4().hex


class RequestIdMiddleware:
    """校验/生成请求 ID，并保证包括提前返回在内的 HTTP 响应都带回该 ID。"""

    def __init__(self, app: Any):
        self.app = app

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        request_id = _request_id_from_scope(scope)
        token = request_id_var.set(request_id)

        async def send_with_request_id(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() != b"x-request-id"
                ]
                headers.append((b"x-request-id", request_id.encode("ascii")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            request_id_var.reset(token)
