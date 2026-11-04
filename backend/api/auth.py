"""认证路由 — JWT 认证 + 用户注册。

功能：
- POST /auth/register — 注册新用户（bcrypt 哈希密码）
- POST /auth/login — 登录（验证密码）
- GET  /auth/me — 获取当前用户

两个安全开关（见 config/settings.py）：
- ALLOW_REGISTRATION：关闭时 /auth/register 直接 403，服务端部署默认关闭，
  账号由 `python scripts/create_admin.py` 创建。
- ALLOW_FIRST_USER_BOOTSTRAP：仅开发/桌面端（默认开启）在 User 表为空时允许
  首次登录自动建号，避免本地第一次启动打不开；生产环境默认关闭，空表登录一律 401。

JWT_SECRET 未配置时开发环境会自动生成随机密钥（重启后 token 失效），生产环境
启动阶段即失败，见 config/settings.py 的 validate_security_config。

本模块只处理“谁在访问”的问题，不处理项目权限。具体项目是否属于当前用户，
由 api/routes.py 里的 _verify_project_owner 在每个业务接口里校验。
"""

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from schemas.api import LoginRequest, LoginResponse, AuthUser, RegisterRequest, RegisterResponse

from config.settings import settings
from db.session import get_db
from models.user import User
from api.rate_limiter import auth_ip_limiter, auth_limiter, get_client_ip

router = APIRouter(prefix="/api/auth")

# 媒体 Cookie 的名字、路径与 token 用途标记。所有用到的地方都从这里取，
# 避免 login（下发）、logout（清除）、api/media.py（校验）三处写死不一致。
MEDIA_COOKIE_NAME = "media_token"
MEDIA_COOKIE_PATH = "/media"
MEDIA_TOKEN_SCOPE = "media"

_jwt_secret = settings.JWT_SECRET
if not _jwt_secret:
    _jwt_secret = str(uuid.uuid4())
    logging.getLogger(__name__).warning(
        "JWT_SECRET 未配置，已自动生成。重启后所有 token 将失效，生产环境请设置 JWT_SECRET 环境变量"
    )


def _hash_password(password: str) -> str:
    """用 bcrypt 哈希密码。

    数据库只保存 hash，不保存明文密码。bcrypt 自带 salt 和计算成本，
    比直接 SHA-256 更适合密码场景。
    """
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _verify_password(password: str, hashed: str) -> bool:
    """验证密码与哈希是否匹配。"""
    return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))


async def hash_password_async(password: str) -> str:
    """在线程池里做 bcrypt 哈希。

    bcrypt 单次耗时几十到几百毫秒，而且只吃 CPU。直接在 async 接口里调用会把整个
    事件循环卡住（其它请求、SSE 流一起等），所以放到 asyncio.to_thread 执行。
    同步版 _hash_password 保留：scripts/create_admin.py 这类命令行脚本不需要它。
    """
    return await asyncio.to_thread(_hash_password, password)


async def verify_password_async(password: str, hashed: str) -> bool:
    """在线程池里做 bcrypt 校验（理由同 hash_password_async）。"""
    return await asyncio.to_thread(_verify_password, password, hashed)


# 模块加载时生成的假哈希：账号不存在时也拿它跑一次等价耗时的校验，
# 让“用户不存在”和“密码错误”在耗时上不可区分（防用户名枚举）。
_DUMMY_PASSWORD_HASH = _hash_password(uuid.uuid4().hex)
_LOGIN_FAILED_DETAIL = "用户名或密码错误"

# bcrypt 只处理前 72 字节，新版（5.x）对更长的输入直接抛 ValueError 而不是截断。
# 这里显式设上限，把“密码过长”变回一个可预期的 400 / 401，而不是 500。
MAX_PASSWORD_BYTES = 72
PASSWORD_TOO_LONG_DETAIL = "密码过长：编码后最多 72 字节（约 24 个汉字或 72 个英文字符）"


def password_within_byte_limit(password: str) -> bool:
    """密码按 UTF-8 编码后是否在 bcrypt 的 72 字节上限内。

    按字节而不是按字符判断：一个汉字 3 字节，25 个汉字就已经超了。
    """
    return len(password.encode("utf-8")) <= MAX_PASSWORD_BYTES


async def _burn_password_check(password: str) -> None:
    """对假哈希做一次 bcrypt 校验，只为消耗与真实校验相当的时间。

    密码超过 72 字节时 bcrypt 会抛 ValueError，这里吞掉：这个分支的目的是不让
    响应时间泄露“账号是否存在”，不应该因为密码过长而变成 500。真实用户路径由
    password_within_byte_limit 提前拦下，不走这里。
    """
    try:
        await verify_password_async(password, _DUMMY_PASSWORD_HASH)
    except ValueError:
        pass


def _create_token(user_id: str, username: str) -> str:
    """签发 JWT token。

    token 中只放 user_id/username 和过期时间，不放密码、API Key 等敏感信息。
    后续请求通过 Authorization: Bearer <token> 进入 get_current_user。
    """
    now = datetime.now(timezone.utc)
    payload = {
        "user_id": user_id,
        "username": username,
        "exp": now + timedelta(hours=settings.JWT_EXPIRE_HOURS),
        "iat": now,
    }
    return jwt.encode(payload, _jwt_secret, algorithm="HS256")


def _create_media_token(user_id: str) -> str:
    """签发媒体专用 token（写进 media_token Cookie）。

    与登录 token 用同一签名密钥，但带 scope="media"：它只能访问 /media，
    不能当登录 token 调普通接口（_decode_token 会拒绝它）。
    """
    now = datetime.now(timezone.utc)
    payload = {
        "user_id": user_id,
        "scope": MEDIA_TOKEN_SCOPE,
        "exp": now + timedelta(hours=settings.JWT_EXPIRE_HOURS),
        "iat": now,
    }
    return jwt.encode(payload, _jwt_secret, algorithm="HS256")


def _parse_token(token: str) -> dict:
    """校验签名与有效期，返回 payload（不区分 token 用途）。"""
    try:
        return jwt.decode(token, _jwt_secret, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="登录已过期")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="登录已失效")


def _decode_token(token: str) -> dict:
    """验证并解码“登录 token”，失败时抛出 HTTPException。

    路由层收到 HTTPException 后会直接返回 401，前端据此跳回登录或提示登录过期。
    media_token（scope="media"）在这里一律拒绝，避免被拿来调普通 API。
    """
    payload = _parse_token(token)
    if payload.get("scope") == MEDIA_TOKEN_SCOPE:
        raise HTTPException(status_code=401, detail="登录已失效")
    return payload


def _decode_media_token(token: str) -> dict:
    """验证并解码“媒体 token”；登录 token 或 scope 不符一律 401。"""
    payload = _parse_token(token)
    if payload.get("scope") != MEDIA_TOKEN_SCOPE:
        raise HTTPException(status_code=401, detail="媒体访问凭证无效")
    return payload


async def get_current_user(request: Request, db: AsyncSession = Depends(get_db)) -> AuthUser:
    """依赖注入：从 Authorization header 提取并验证当前用户。

    验证 JWT payload 中的 user_id 对应 User 表中存在且 is_active=True 的用户。

    用法: async def handler(user: AuthUser = Depends(get_current_user)):
    """
    # 前端所有需要登录的接口都应带 Bearer token。这里不兼容 query token，
    # 是为了避免 token 出现在 URL、日志或浏览器历史里。
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未登录")
    token = auth[len("Bearer "):]
    payload = _decode_token(token)

    user_id = payload.get("user_id")
    if not isinstance(user_id, str):
        raise HTTPException(status_code=401, detail="登录已失效")

    try:
        uid = uuid.UUID(user_id)
    except (ValueError, TypeError, AttributeError):
        # token 即使签名合法，也不能让格式错误的 subject 触发 500。
        raise HTTPException(status_code=401, detail="登录已失效")

    # JWT 只证明 token 曾经由本服务签发；当前用户仍必须存在且处于启用状态。
    # 不再回退到 token 内的 username，避免删除用户后旧 token 在过期前继续生效。
    result = await db.execute(select(User).where(User.id == uid))
    db_user = result.scalar_one_or_none()
    if not db_user:
        raise HTTPException(status_code=401, detail="登录已失效")
    if not db_user.is_active:
        raise HTTPException(status_code=401, detail="用户已被禁用")

    return AuthUser(
        id=str(db_user.id),
        username=db_user.username,
        display_name=db_user.username,
    )


@router.post("/register", response_model=RegisterResponse)
async def register(
    req: RegisterRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """注册新用户。

    注册时先校验 username/email 唯一性，然后写入 bcrypt hash。这里不自动登录，
    前端可在注册后跳转登录页或复用 login 流程。

    生产环境（ALLOW_REGISTRATION=false）直接拒绝：自助注册会让任何人都能占用
    服务端资源，账号应由管理员用 scripts/create_admin.py 创建。

    限流两个维度都先过：同一 IP 的总量（防止换用户名绕过）+ 同一用户名的次数。
    """
    if not settings.ALLOW_REGISTRATION:
        raise HTTPException(status_code=403, detail="注册已关闭")

    auth_ip_limiter.check(f"auth_ip:{get_client_ip(request)}", with_retry_after=True)
    auth_limiter.check(f"auth:{req.username}", with_retry_after=True)

    # 先挡住 bcrypt 处理不了的长度，避免落到 hashpw 里变成 500。
    if not password_within_byte_limit(req.password):
        raise HTTPException(status_code=400, detail=PASSWORD_TOO_LONG_DETAIL)

    # 校验 username 不重复
    result = await db.execute(select(User).where(User.username == req.username))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="用户名已存在")

    # 校验 email 不重复（如果提供了 email）
    if req.email:
        result = await db.execute(select(User).where(User.email == req.email))
        if result.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="邮箱已被注册")

    hashed_password = await hash_password_async(req.password)
    user = User(
        username=req.username,
        email=req.email,
        hashed_password=hashed_password,
        is_active=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    return RegisterResponse(
        id=str(user.id),
        username=user.username,
        created_at=user.created_at,
    )


def _login_response(response: Response, user: User) -> LoginResponse:
    """构造登录响应，并下发 /media 专用 Cookie。

    <img> 标签请求图片时带不了 Authorization 头，所以这里额外发一个只能访问
    /media 的 token（scope="media"）放在 HttpOnly Cookie 里。响应体字段保持不变。
    """
    token = _create_token(str(user.id), user.username)
    media_token = _create_media_token(str(user.id))
    response.set_cookie(
        key=MEDIA_COOKIE_NAME,
        value=media_token,
        max_age=settings.JWT_EXPIRE_HOURS * 3600,
        path=MEDIA_COOKIE_PATH,
        httponly=True,
        samesite="lax",
        secure=settings.COOKIE_SECURE,
    )
    return LoginResponse(
        access_token=token,
        user=AuthUser(id=str(user.id), username=user.username, display_name=user.username),
    )


@router.post("/login", response_model=LoginResponse)
async def login(
    req: LoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """用户登录

    - 如果 User 表中有记录，则验证用户名和密码
    - 首次启动兼容：User 表为空且 ALLOW_FIRST_USER_BOOTSTRAP 开启时，允许用
      任意账号密码建立第一个用户（开发/桌面端）；生产环境关闭该开关，空表登录
      一律 401，账号只能由 scripts/create_admin.py 创建

    防用户名枚举：账号不存在、库里没有任何账号（且不允许首次建号）、密码超长这几种
    情况，都会先对固定假哈希跑一次 bcrypt 再做 401，响应体和耗时都与“密码错误”一致。
    账号被禁用不作为“密码错误”处理：必须先验密码，密码正确才告知“用户已被禁用”，
    否则不用密码就能探出“这个用户名存在且被禁用”。
    """
    identity = req.username or req.email
    auth_ip_limiter.check(f"auth_ip:{get_client_ip(request)}", with_retry_after=True)
    auth_limiter.check(f"auth:{identity}", with_retry_after=True)

    # 检查 User 表是否有记录。
    # 首次使用时自动创建用户，是为了降低本地演示/桌面端第一次启动门槛；
    # 一旦已有用户，就进入正常用户名密码校验。
    count_result = await db.execute(select(func.count()).select_from(User))
    user_count = count_result.scalar()

    if user_count == 0:
        if not settings.ALLOW_FIRST_USER_BOOTSTRAP:
            # 不创建用户、不透露“库里还没有账号”，避免变成任人取号的后门。
            # 但仍然要做一次等价耗时的校验，否则响应快慢就能暴露“库是空的”。
            await _burn_password_check(req.password)
            raise HTTPException(status_code=401, detail=_LOGIN_FAILED_DETAIL)

        # 开发/桌面端首次使用，自动创建用户。
        # 密码超长同样不能建号，否则 bcrypt 会在下面 hashpw 时抛 ValueError。
        if not password_within_byte_limit(req.password):
            raise HTTPException(status_code=400, detail=PASSWORD_TOO_LONG_DETAIL)

        user = User(
            username=identity,
            hashed_password=await hash_password_async(req.password),
            is_active=True,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)

        return _login_response(response, user)

    # 正常登录：查询用户并验证密码
    result = await db.execute(select(User).where(User.username == identity))
    user = result.scalar_one_or_none()

    # 也尝试用 email 查询
    if not user and req.email:
        result = await db.execute(select(User).where(User.email == req.email))
        user = result.scalar_one_or_none()

    if not user:
        # 账号不存在也跑一次 bcrypt，避免用响应时间枚举用户名。
        await _burn_password_check(req.password)
        raise HTTPException(status_code=401, detail=_LOGIN_FAILED_DETAIL)

    # 超过 72 字节的密码 bcrypt 无法校验（会抛 ValueError），一律按密码错误处理：
    # 不碰真实哈希，也不返回 400，避免“密码太长”成为一个可区分账号是否存在的信号。
    if not password_within_byte_limit(req.password):
        await _burn_password_check(req.password)
        raise HTTPException(status_code=401, detail=_LOGIN_FAILED_DETAIL)

    # 先验密码再看账号状态：密码错误时，禁用账号与不存在账号的响应必须完全一致。
    if not await verify_password_async(req.password, user.hashed_password):
        raise HTTPException(status_code=401, detail=_LOGIN_FAILED_DETAIL)

    if not user.is_active:
        raise HTTPException(status_code=401, detail="用户已被禁用")

    return _login_response(response, user)


@router.get("/me", response_model=AuthUser)
async def get_me(user: AuthUser = Depends(get_current_user)):
    return user


@router.post("/logout", status_code=204)
async def logout(response: Response) -> None:
    """清除 /media 专用 Cookie。

    只清 Cookie，不动登录 token（前端自己丢弃 access_token）。status_code=204 无响应体。
    清除指令的属性必须与下发时一致（尤其生产环境的 Secure），否则浏览器会认为
    不是同一个 Cookie 而拒绝删除。
    """
    response.delete_cookie(
        key=MEDIA_COOKIE_NAME,
        path=MEDIA_COOKIE_PATH,
        secure=settings.COOKIE_SECURE,
        httponly=True,
        samesite="lax",
    )
