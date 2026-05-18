"""认证路由 — JWT 认证 + 用户注册

功能：
- POST /auth/register — 注册新用户（bcrypt 哈希密码）
- POST /auth/login — 登录（验证密码）
- GET  /auth/me — 获取当前用户

MVP 兼容：如果 User 表为空（首次使用），仍然允许任意账号密码登录并自动创建用户。
生产环境务必设置 JWT_SECRET 环境变量，否则重启后所有 token 失效。
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from schemas.api import LoginRequest, LoginResponse, AuthUser, RegisterRequest, RegisterResponse

from config.settings import settings
from db.session import get_db
from models.user import User
from api.rate_limiter import auth_limiter

router = APIRouter(prefix="/api/auth")

_jwt_secret = settings.JWT_SECRET
if not _jwt_secret:
    _jwt_secret = str(uuid.uuid4())
    logging.getLogger(__name__).warning(
        "JWT_SECRET 未配置，已自动生成。重启后所有 token 将失效，生产环境请设置 JWT_SECRET 环境变量"
    )


def _hash_password(password: str) -> str:
    """用 bcrypt 哈希密码"""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _verify_password(password: str, hashed: str) -> bool:
    """验证密码与哈希是否匹配"""
    return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))


def _create_token(user_id: str, username: str) -> str:
    """签发 JWT token"""
    now = datetime.now(timezone.utc)
    payload = {
        "user_id": user_id,
        "username": username,
        "exp": now + timedelta(hours=settings.JWT_EXPIRE_HOURS),
        "iat": now,
    }
    return jwt.encode(payload, _jwt_secret, algorithm="HS256")


def _decode_token(token: str) -> dict:
    """验证并解码 JWT token，失败时抛出 HTTPException"""
    try:
        return jwt.decode(token, _jwt_secret, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="登录已过期")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="登录已失效")


async def get_current_user(request: Request, db: AsyncSession = Depends(get_db)) -> AuthUser:
    """依赖注入：从 Authorization header 提取并验证当前用户。

    验证 JWT payload 中的 user_id 对应 User 表中存在且 is_active=True 的用户。

    用法: async def handler(user: AuthUser = Depends(get_current_user)):
    """
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未登录")
    token = auth[len("Bearer "):]
    payload = _decode_token(token)

    user_id = payload.get("user_id")
    if user_id:
        # 验证用户存在且 active
        uid = uuid.UUID(user_id)
        result = await db.execute(select(User).where(User.id == uid))
        db_user = result.scalar_one_or_none()
        if db_user and not db_user.is_active:
            raise HTTPException(status_code=401, detail="用户已被禁用")
        # 如果用户存在于 DB，用 DB 数据
        if db_user:
            return AuthUser(
                id=str(db_user.id),
                username=db_user.username,
                display_name=db_user.username,
            )

    # fallback：JWT 中的信息（兼容旧 token / MVP 模式）
    return AuthUser(
        id=payload["user_id"],
        username=payload["username"],
        display_name=payload["username"],
    )


@router.post("/register", response_model=RegisterResponse)
async def register(
    req: RegisterRequest,
    db: AsyncSession = Depends(get_db),
):
    """注册新用户"""
    auth_limiter.check(f"auth:{req.username}")
    # 校验 username 不重复
    result = await db.execute(select(User).where(User.username == req.username))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="用户名已存在")

    # 校验 email 不重复（如果提供了 email）
    if req.email:
        result = await db.execute(select(User).where(User.email == req.email))
        if result.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="邮箱已被注册")

    hashed_password = _hash_password(req.password)
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


@router.post("/login", response_model=LoginResponse)
async def login(req: LoginRequest, db: AsyncSession = Depends(get_db)):
    """用户登录

    - 如果 User 表中有记录，则验证用户名和密码
    - MVP 兼容：如果 User 表为空（首次使用），允许任意账号密码登录并自动创建用户
    """
    identity = req.username or req.email
    auth_limiter.check(f"auth:{identity}")

    # 检查 User 表是否有记录
    count_result = await db.execute(select(func.count()).select_from(User))
    user_count = count_result.scalar()

    if user_count == 0:
        # MVP 兼容：首次使用，自动创建用户
        user = User(
            username=identity,
            hashed_password=_hash_password(req.password),
            is_active=True,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)

        token = _create_token(str(user.id), user.username)
        return LoginResponse(
            access_token=token,
            user=AuthUser(id=str(user.id), username=user.username, display_name=user.username),
        )

    # 正常登录：查询用户并验证密码
    result = await db.execute(select(User).where(User.username == identity))
    user = result.scalar_one_or_none()

    # 也尝试用 email 查询
    if not user and req.email:
        result = await db.execute(select(User).where(User.email == req.email))
        user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    if not user.is_active:
        raise HTTPException(status_code=401, detail="用户已被禁用")

    if not _verify_password(req.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    token = _create_token(str(user.id), user.username)
    return LoginResponse(
        access_token=token,
        user=AuthUser(id=str(user.id), username=user.username, display_name=user.username),
    )


@router.get("/me", response_model=AuthUser)
async def get_me(user: AuthUser = Depends(get_current_user)):
    return user
