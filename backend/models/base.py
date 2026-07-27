"""SQLAlchemy 声明式基类和跨数据库字段类型。

项目同时支持服务端 PostgreSQL 和桌面端 SQLite。为了让模型层尽量复用同一套
字段声明，这里封装了 GUID、JSONValue、FloatList 等 TypeDecorator。
"""

import uuid
from datetime import datetime

from sqlalchemy import CHAR, JSON, DateTime, Float, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。

    db.session.init_db 会通过 Base.metadata.create_all 创建表。
    """
    pass


class GUID(TypeDecorator):
    """Portable UUID type: PostgreSQL UUID, SQLite CHAR(36).

    PostgreSQL 原生支持 UUID；SQLite 没有 UUID 类型，所以以字符串存储。
    对业务代码来说读写的都是 uuid.UUID。
    """

    impl = CHAR
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(UUID(as_uuid=True))
        return dialect.type_descriptor(CHAR(36))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if dialect.name == "postgresql":
            return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
        return str(value)

    def process_result_value(self, value, dialect):
        if value is None or isinstance(value, uuid.UUID):
            return value
        return uuid.UUID(str(value))


class JSONValue(TypeDecorator):
    """Portable JSON value: PostgreSQL JSONB, SQLite JSON."""

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(JSONB())
        return dialect.type_descriptor(JSON())


class FloatList(TypeDecorator):
    """Portable float array: PostgreSQL ARRAY(Float), SQLite JSON.

    主要用于 embedding 或分数数组一类字段。PostgreSQL 可用 ARRAY，
    SQLite 则退化为 JSON 存储。
    """

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(ARRAY(Float))
        return dialect.type_descriptor(JSON())


class TimestampMixin:
    """通用创建/更新时间字段 mixin。"""
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class UUIDMixin:
    """通用 UUID 主键 mixin。"""
    id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=uuid.uuid4
    )
