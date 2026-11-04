"""ASGI 层请求体大小限制。

为什么必须在 ASGI 层做：`UploadFile` 在 Starlette 里会先把整个 multipart 解析完
（落临时文件）才把控制权交给处理函数，等处理函数看到 `file.read()` 时请求体早就被
收完了。只在处理函数里判断 `len(data)`，超大请求体仍然会先被完整读进内存/磁盘。

为什么不用 `BaseHTTPMiddleware`：它会把响应包成流并插入额外的任务，SSE 流式响应
（本项目大量使用）会被 chunk 卡住甚至被整体缓冲。这里用纯 ASGI 中间件，只在
`receive` 上计数、在 `send` 上按需丢弃，响应体一律透传。

判断顺序（对应任务单 2.1.3）：
1. 有 `Content-Length`：不是合法非负整数 → 400；超过上限 → 413，且一个字节都不读；
2. 没有该头（chunked）或头与实际不符：包装 `receive` 累计实际收到的字节，
   一超限立即回 413 并停止继续读——以实际字节为准，不信任客户端自报的头。

注册顺序（见 main.py）：本中间件必须加在 `CORSMiddleware` **之前**，这样 CORS 才是
外层，413 响应也能带上 CORS 头，浏览器 JS 才读得到状态码。
"""

from __future__ import annotations

import json
import re
from typing import Any, Awaitable, Callable

from config.settings import settings

# multipart 边界、字段名、CRLF 等协议开销的余量；请求体限制要给它留出空间，
# 否则“刚好 5MB 的图片”会因为 multipart 包装而超限。
MULTIPART_OVERHEAD_BYTES = 64 * 1024

# 只有这些方法可能有请求体。GET/HEAD/OPTIONS 直接透传（SSE 全是 GET）。
_BODY_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# 规则表：方法 + 路径正则 → 用哪个配置项做上限。
# 路径全部抄自 api/routes.py 的实际路由定义（router prefix = "/api"）。
_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    # POST /api/projects/{project_id}/assets/images —— 图片上传（api/routes.py:1057）
    ("POST", re.compile(r"^/api/projects/[^/]+/assets/images/?$"), "MAX_UPLOAD_BYTES"),
    # POST /api/projects/import-txt —— TXT 整本导入（api/routes.py:1138）
    ("POST", re.compile(r"^/api/projects/import-txt/?$"), "MAX_TXT_IMPORT_BYTES"),
    # POST /api/projects/{project_id}/knowledge/upload —— 知识库上传（api/routes.py:7736）
    ("POST", re.compile(r"^/api/projects/[^/]+/knowledge/upload/?$"), "KNOWLEDGE_UPLOAD_MAX_BYTES"),
)


def _format_limit(size: int) -> str:
    """把字节上限格式化成 MB/KB（错误信息里给人看）。"""
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:g}MB"
    if size >= 1024:
        return f"{size / 1024:g}KB"
    return f"{size}B"


def resolve_body_limit(method: str, path: str) -> tuple[int, str]:
    """返回（硬上限字节, 展示给用户的业务上限）。

    硬上限 = 业务上限 + multipart 余量；展示值用业务上限，避免出现 "5.06MB" 这种
    让用户困惑的数字。
    """
    for rule_method, pattern, attr in _RULES:
        if method == rule_method and pattern.match(path):
            business_limit = int(getattr(settings, attr))
            return business_limit + MULTIPART_OVERHEAD_BYTES, _format_limit(business_limit)

    business_limit = int(settings.MAX_REQUEST_BODY_BYTES)
    return business_limit, _format_limit(business_limit)


def _header(scope: dict[str, Any], name: bytes) -> str | None:
    for key, value in scope.get("headers") or []:
        if key.lower() == name:
            return value.decode("latin-1")
    return None


async def _send_json(
    send: Callable[[dict[str, Any]], Awaitable[None]],
    status: int,
    detail: str,
) -> None:
    """输出与 FastAPI 默认错误一致的 JSON 体（{"detail": "..."}）。"""
    body = json.dumps({"detail": detail}, ensure_ascii=False).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("latin-1")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class RequestBodyLimitMiddleware:
    """按“方法 + 路径”限制请求体大小；超限返回 413，不把超大体读完。"""

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

        method = str(scope.get("method", "GET")).upper()
        if method not in _BODY_METHODS:
            # GET/HEAD/OPTIONS：可能是 SSE，原样透传，不碰 receive/send
            await self.app(scope, receive, send)
            return

        limit, display_limit = resolve_body_limit(method, scope.get("path", ""))

        # 1) Content-Length 预检：能提前拒绝就一个字节都不读
        declared_raw = _header(scope, b"content-length")
        if declared_raw is not None:
            try:
                declared = int(declared_raw.strip())
            except (TypeError, ValueError):
                await _send_json(send, 400, "无效的 Content-Length 请求头")
                return
            if declared < 0:
                await _send_json(send, 400, "无效的 Content-Length 请求头")
                return
            if declared > limit:
                await _send_json(send, 413, f"请求体过大，上限 {display_limit}")
                return

        # 2) 逐块计数：头缺失（chunked）或头与实际不符时以实际字节为准
        received = 0
        overflowed = False

        async def send_wrapper(message: dict[str, Any]) -> None:
            # 已经发过 413，下游 app 再吐什么都丢掉（它可能已经进入异常处理）
            if overflowed:
                return
            await send(message)

        async def receive_wrapper() -> dict[str, Any]:
            nonlocal received, overflowed
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body") or b"")
                if received > limit:
                    overflowed = True
                    await _send_json(send, 413, f"请求体过大，上限 {display_limit}")
                    # 让下游尽快结束读体：收到 disconnect 后应用会中止读取并退出
                    return {"type": "http.disconnect"}
            return message

        try:
            await self.app(scope, receive_wrapper, send_wrapper)
        except Exception:
            if not overflowed:
                raise
