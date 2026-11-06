"""受保护的 /media 路由。

为什么要自己实现而不是 `StaticFiles` 挂载：上传的图片是项目私有资源，直接静态挂载
等于任何人拿到路径就能下载。但图片是写在章节 Markdown 里的 `<img src="/media/...">`，
而 `<img>` 请求带不了 `Authorization` 头，所以分两条认证路径：

1. `Authorization: Bearer <登录 token>`——桌面端（Electron 页面是 file://，没有 Cookie）
   以及任何主动发请求的场景；
2. `media_token` Cookie——网页端 `<img>` 标签用，里面是 scope="media" 的专用 token，
   只能访问本路由，不能调普通 API（见 api/auth.py 的 _decode_token）。

所有“无权限 / 不存在 / 路径非法 / 类型不允许”的情况统一返回 404（未登录是 401），
避免通过状态码推断“这个项目或文件是否存在”。
"""

import os
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import MEDIA_COOKIE_NAME, _decode_media_token, get_current_user
from config.settings import settings
from db.session import get_db
from models.project import Project
from models.user import User

router = APIRouter()

# 只服务这几类图片。SVG/HTML 不在这里：它们可以携带脚本，同源打开等于 XSS。
_ALLOWED_EXTENSIONS: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def _not_found() -> HTTPException:
    """统一的 404，不区分“不存在”和“不属于你”。"""
    return HTTPException(status_code=404, detail="文件不存在")


def _unauthorized() -> HTTPException:
    return HTTPException(status_code=401, detail="未登录")


async def _viewer_user_id(request: Request, db: AsyncSession) -> str:
    """确定当前访问者：优先 Authorization 头，其次 media_token Cookie。

    带 Authorization 头时复用 get_current_user（会校验用户仍存在且启用）；
    只带 Cookie 时用 media token 解析，并同样确认用户有效。
    """
    if request.headers.get("authorization", "").startswith("Bearer "):
        user = await get_current_user(request, db)
        return user.id

    cookie = request.cookies.get(MEDIA_COOKIE_NAME)
    if not cookie:
        raise _unauthorized()

    payload = _decode_media_token(cookie)
    raw_user_id = payload.get("user_id")
    if not isinstance(raw_user_id, str):
        raise _unauthorized()
    try:
        user_uuid = uuid.UUID(raw_user_id)
    except (ValueError, TypeError, AttributeError):
        raise _unauthorized()

    active = (
        await db.execute(
            select(User.id).where(User.id == user_uuid, User.is_active.is_(True))
        )
    ).scalar_one_or_none()
    if active is None:
        raise _unauthorized()
    return str(user_uuid)


def _resolve_image_path(project_id: uuid.UUID, subpath: str) -> tuple[Path, str]:
    """把请求子路径解析成本地文件，任何不合规的输入都当 404。

    规则：只能是 `images/<单层文件名>`，扩展名在白名单内，realpath 解析后仍在
    该项目的媒体目录里，且必须是普通文件（软链接指到目录外会被 realpath 暴露）。
    """
    # %：拒绝残留的百分号编码（%2e%2e、%2f 之类），避免二次解码绕过
    # \ 与 \x00：Windows 分隔符和空字节注入
    # ..：任何形式的上级目录引用
    if not subpath or "%" in subpath or "\\" in subpath or "\x00" in subpath or ".." in subpath:
        raise _not_found()

    parts = [part for part in subpath.split("/") if part]
    if len(parts) != 2 or parts[0] != "images":
        raise _not_found()

    filename = parts[1]
    if filename != os.path.basename(filename) or filename.startswith("."):
        raise _not_found()

    media_type = _ALLOWED_EXTENSIONS.get(os.path.splitext(filename)[1].lower())
    if media_type is None:
        raise _not_found()

    project_root = os.path.realpath(
        os.path.join(settings.UPLOAD_DIR, "projects", str(project_id))
    )
    candidate = os.path.realpath(os.path.join(project_root, "images", filename))
    if not candidate.startswith(project_root + os.sep):
        raise _not_found()
    if not os.path.isfile(candidate):
        raise _not_found()

    return Path(candidate), media_type


@router.get("/media/projects/{project_id}/{subpath:path}")
async def get_project_media(
    project_id: str,
    subpath: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """返回项目图片；需要登录且必须是项目所有者。"""
    user_id = await _viewer_user_id(request, db)

    try:
        project_uuid = uuid.UUID(project_id)
    except (ValueError, TypeError, AttributeError):
        # 非法 project_id 也按 404 处理，不暴露格式校验细节
        raise _not_found()

    # 项目不存在与非本人所有都回 404（与 api/routes.py 的 _verify_project_owner 一致）。
    # 这里直接查表而不 import routes：避免 media ↔ routes 的循环依赖。
    owner_id = (
        await db.execute(select(Project.owner_id).where(Project.id == project_uuid))
    ).scalar_one_or_none()
    if owner_id is None or owner_id != user_id:
        raise _not_found()

    path, media_type = _resolve_image_path(project_uuid, subpath)
    return FileResponse(
        path,
        media_type=media_type,
        headers={
            # 不猜测类型，避免浏览器把图片当别的类型渲染
            "X-Content-Type-Options": "nosniff",
            # 允许内联展示（图片默认就是 inline，这里显式写死）
            "Content-Disposition": "inline",
            # 私有资源：只允许浏览器私有缓存，不落到共享代理
            "Cache-Control": "private, max-age=3600",
        },
    )
