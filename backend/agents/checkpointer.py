"""LangGraph checkpointer 生命周期管理。

LangGraph 的 SQLite/PostgreSQL ``from_conn_string`` 返回异步上下文管理器，
不能在模块导入时直接当作 saver 传给 ``graph.compile``。这里用一个稳定的
``BaseCheckpointSaver`` 代理跨请求共享实例，并在首次使用或应用 lifespan
启动时进入真实 saver 的连接上下文。
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import sqlite3
import threading
from typing import Any, AsyncIterator, Sequence

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)

from config.settings import settings

logger = logging.getLogger(__name__)


def _sanitize_checkpoint_value(value: Any) -> tuple[Any, bool]:
    """移除 checkpoint 中不该持久化的明文密钥。

    运行时 ``llm_config`` 仍需用于恢复调用，因此把旧 ``api_key`` 原位升级为
    ``encrypted_api_key``；纯审计用的 ``model_config_snapshot`` 则只保留非敏感
    元数据。函数不会改变章节正文、节点进度或 interrupt 状态。
    """
    if isinstance(value, dict):
        changed = False
        sanitized: dict[Any, Any] = {}
        plain_api_key = value.get("api_key")
        encrypted_api_key = value.get("encrypted_api_key")

        for key, item in value.items():
            if key == "api_key":
                changed = True
                continue
            if key == "model_config_snapshot" and isinstance(item, dict):
                from harness.run_manager import sanitize_model_config_snapshot

                safe_snapshot = sanitize_model_config_snapshot(item)
                sanitized[key] = safe_snapshot
                changed = changed or safe_snapshot != item
                continue
            clean_item, item_changed = _sanitize_checkpoint_value(item)
            sanitized[key] = clean_item
            changed = changed or item_changed

        if plain_api_key and not encrypted_api_key:
            from utils.crypto import encrypt_api_key

            sanitized["encrypted_api_key"] = encrypt_api_key(str(plain_api_key))
            changed = True
        return sanitized, changed

    if isinstance(value, list):
        changed = False
        result = []
        for item in value:
            clean_item, item_changed = _sanitize_checkpoint_value(item)
            result.append(clean_item)
            changed = changed or item_changed
        return result, changed

    if isinstance(value, tuple):
        changed = False
        result = []
        for item in value:
            clean_item, item_changed = _sanitize_checkpoint_value(item)
            result.append(clean_item)
            changed = changed or item_changed
        return tuple(result), changed

    return value, False


def migrate_sqlite_checkpoint_secrets(path: Path) -> int:
    """事务式升级旧 SQLite checkpoint，返回被改写的记录数。

    任一记录无法反序列化时整笔事务回滚并让应用启动失败，避免悄悄破坏可恢复
    任务。该迁移只处理密钥字段，不删除 checkpoint。
    """
    if not path.exists():
        return 0

    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    serde = JsonPlusSerializer()
    conn = sqlite3.connect(str(path), timeout=30)
    migrated = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        table_names = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

        if "checkpoints" in table_names:
            rows = conn.execute(
                "SELECT rowid, type, checkpoint, metadata FROM checkpoints"
            ).fetchall()
            for rowid, value_type, blob, metadata_blob in rows:
                checkpoint = serde.loads_typed((value_type, blob))
                checkpoint, changed = _sanitize_checkpoint_value(checkpoint)

                metadata_changed = False
                metadata = metadata_blob
                if metadata_blob:
                    decoded_metadata = json.loads(bytes(metadata_blob).decode("utf-8"))
                    decoded_metadata, metadata_changed = _sanitize_checkpoint_value(
                        decoded_metadata
                    )
                    if metadata_changed:
                        metadata = json.dumps(
                            decoded_metadata,
                            ensure_ascii=False,
                        ).encode("utf-8")

                if changed or metadata_changed:
                    new_type, new_blob = serde.dumps_typed(checkpoint)
                    conn.execute(
                        "UPDATE checkpoints "
                        "SET type = ?, checkpoint = ?, metadata = ? WHERE rowid = ?",
                        (new_type, new_blob, metadata, rowid),
                    )
                    migrated += 1

        if "writes" in table_names:
            rows = conn.execute(
                "SELECT rowid, type, value FROM writes"
            ).fetchall()
            for rowid, value_type, blob in rows:
                value = serde.loads_typed((value_type, blob))
                value, changed = _sanitize_checkpoint_value(value)
                if changed:
                    new_type, new_blob = serde.dumps_typed(value)
                    conn.execute(
                        "UPDATE writes SET type = ?, value = ? WHERE rowid = ?",
                        (new_type, new_blob, rowid),
                    )
                    migrated += 1

        conn.commit()
        return migrated
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


class ManagedAsyncCheckpointer(BaseCheckpointSaver):
    """延迟打开并托管 LangGraph 异步持久化 saver。

    代理对象可以安全地在模块导入时交给 ``graph.compile``；真实数据库连接在
    FastAPI lifespan 的 ``setup`` 中建立。未经过 lifespan 的单元测试首次调用
    异步方法时也会自动初始化。
    """

    def __init__(self, *, backend: str, connection_string: str):
        super().__init__()
        self.backend = backend
        self.connection_string = connection_string
        # Starlette TestClient 未作为 context manager 使用时会为不同请求建立不同
        # 事件循环；AsyncSqliteSaver 内部的 Lock 不能跨循环复用。生产环境只有
        # lifespan 的单一循环，这个映射也只有一项。
        self._entries: dict[int, tuple[asyncio.AbstractEventLoop, Any, Any]] = {}
        self._setup_locks: dict[int, asyncio.Lock] = {}
        self._migration_lock = threading.Lock()
        self._sqlite_migration_done = False

    def _migrate_legacy_sqlite_secrets(self) -> None:
        if self.backend != "sqlite" or self._sqlite_migration_done:
            return
        with self._migration_lock:
            if self._sqlite_migration_done:
                return
            migrated = migrate_sqlite_checkpoint_secrets(
                Path(self.connection_string)
            )
            if migrated:
                logger.info(
                    "已安全升级 %d 条旧 checkpoint 记录中的敏感配置",
                    migrated,
                )
            self._sqlite_migration_done = True

    def _delegate_is_usable(self, delegate: Any | None) -> bool:
        if delegate is None:
            return False
        if self.backend == "sqlite":
            connection = getattr(delegate, "conn", None)
            # TestClient 在未作为 context manager 使用时可能为每个请求创建/关闭
            # 独立事件循环，异步生成器会随循环关闭并关闭 aiosqlite 连接。生产
            # lifespan 使用单一循环，不会触发；这里允许测试和嵌入式调用自动重连。
            if connection is None or getattr(connection, "_connection", None) is None:
                return False
        return True

    async def _ensure_current_delegate(self) -> Any:
        loop = asyncio.get_running_loop()
        key = id(loop)
        entry = self._entries.get(key)
        if entry and entry[0] is loop and self._delegate_is_usable(entry[1]):
            return entry[1]

        lock = self._setup_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._setup_locks[key] = lock
        async with lock:
            entry = self._entries.get(key)
            if entry and entry[0] is loop and self._delegate_is_usable(entry[1]):
                return entry[1]

            if self.backend == "sqlite":
                await asyncio.to_thread(self._migrate_legacy_sqlite_secrets)
                try:
                    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
                except ImportError as exc:  # pragma: no cover - 依赖安装错误
                    raise RuntimeError(
                        "CHECKPOINTER_BACKEND=sqlite 但未安装 langgraph-checkpoint-sqlite"
                    ) from exc
                context_manager = AsyncSqliteSaver.from_conn_string(self.connection_string)
            elif self.backend == "postgres":
                try:
                    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
                except ImportError as exc:  # pragma: no cover - 依赖安装错误
                    raise RuntimeError(
                        "CHECKPOINTER_BACKEND=postgres 但未安装 langgraph-checkpoint-postgres"
                    ) from exc
                context_manager = AsyncPostgresSaver.from_conn_string(self.connection_string)
            else:  # pragma: no cover - 由工厂提前拦截
                raise RuntimeError(f"unknown managed checkpointer backend: {self.backend}")

            delegate = await context_manager.__aenter__()
            try:
                await delegate.setup()
            except Exception:
                await context_manager.__aexit__(None, None, None)
                raise

            self._entries[key] = (loop, delegate, context_manager)
            return delegate

    async def setup(self) -> None:
        await self._ensure_current_delegate()

    async def close(self) -> None:
        loop = asyncio.get_running_loop()
        key = id(loop)
        entry = self._entries.pop(key, None)
        self._setup_locks.pop(key, None)
        if entry and entry[0] is loop:
            await entry[2].__aexit__(None, None, None)
        # 其他已关闭循环的上下文无法跨循环 await 关闭；异步生成器在所属循环
        # 结束时会自行清理。这里只移除不可再使用的引用。
        for stale_key, stale_entry in list(self._entries.items()):
            if stale_entry[0].is_closed():
                self._entries.pop(stale_key, None)
                self._setup_locks.pop(stale_key, None)

    async def _get_delegate(self) -> Any:
        return await self._ensure_current_delegate()

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        delegate = await self._get_delegate()
        return await delegate.aget_tuple(config)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        delegate = await self._get_delegate()
        async for item in delegate.alist(
            config,
            filter=filter,
            before=before,
            limit=limit,
        ):
            yield item

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        delegate = await self._get_delegate()
        return await delegate.aput(config, checkpoint, metadata, new_versions)

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        delegate = await self._get_delegate()
        await delegate.aput_writes(config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id: str) -> None:
        delegate = await self._get_delegate()
        await delegate.adelete_thread(thread_id)


def get_checkpointer() -> BaseCheckpointSaver:
    """按配置返回可跨请求共享的 checkpointer。"""
    backend = (settings.CHECKPOINTER_BACKEND or "sqlite").lower()
    if backend == "memory":
        from langgraph.checkpoint.memory import MemorySaver

        return MemorySaver()
    if backend == "sqlite":
        configured_path = settings.CHECKPOINTER_PATH.strip()
        path = (
            Path(configured_path).expanduser()
            if configured_path
            else Path(__file__).resolve().parents[1] / "checkpoints.sqlite"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        return ManagedAsyncCheckpointer(
            backend="sqlite",
            # AsyncSqliteSaver 内部直接调用 aiosqlite.connect，需要文件路径而不是
            # SQLAlchemy 的 sqlite+aiosqlite:// URI。
            connection_string=str(path),
        )
    if backend == "postgres":
        return ManagedAsyncCheckpointer(
            backend="postgres",
            connection_string=settings.DATABASE_URL,
        )
    raise RuntimeError(f"unknown checkpointer backend: {backend}")


async def setup_checkpointer(checkpointer: BaseCheckpointSaver) -> None:
    """初始化持久化 checkpointer；失败由应用 lifespan fail-fast。"""
    setup = getattr(checkpointer, "setup", None)
    if setup is not None:
        await setup()


async def teardown_checkpointer(checkpointer: BaseCheckpointSaver) -> None:
    """释放由当前进程持有的 checkpointer 连接。"""
    close = getattr(checkpointer, "close", None)
    if close is None:
        return
    try:
        await close()
    except Exception:  # pragma: no cover - 关闭阶段只记录告警
        logger.warning("checkpoint 连接关闭失败", exc_info=True)
