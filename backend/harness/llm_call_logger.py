"""LoggedLLMProvider — 包装 LLMProvider，每次调用写 llm_call_logs 审计行。

不修改 llm_provider.py 抽象。实现与 LLMProvider 相同的 generate/generate_stream 接口，
内部委托给被包装的真实 provider。best-effort：写日志失败只记 warning，不中断生成。
token/cost 第一阶段拿不到（OpenAIProvider.generate 返回纯 str），写 NULL。
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from models.llm_call_log import LlmCallLog

logger = logging.getLogger(__name__)

# rendered_prompt_snapshot 最大字符数，超出截断并在 request 标记 truncated。
# 保护 DB 存储：长 prompt（含完整上下文）可能数万字符，截断到合理上限。
MAX_PROMPT_SNAPSHOT_CHARS = 8000


def _build_rendered_prompt(system_prompt: str, user_prompt: str) -> str:
    """把 system/user prompt 合并成便于审计查看的文本快照。"""
    return f"[system]\n{system_prompt}\n\n[user]\n{user_prompt}"


def _truncate(text: str) -> tuple[str, bool]:
    """截断到 MAX_PROMPT_SNAPSHOT_CHARS，返回 (截断后文本, 是否截断)。"""
    if len(text) <= MAX_PROMPT_SNAPSHOT_CHARS:
        return text, False
    return text[:MAX_PROMPT_SNAPSHOT_CHARS], True


class LoggedLLMProvider:
    """装饰 LLMProvider，记录每次调用的 prompt/latency/provider/model 到 llm_call_logs。

    被包装的 provider 须有 generate(system_prompt, user_prompt, temperature, max_tokens) -> str
    和 generate_stream(...) -> AsyncIterator[str]。provider/model 从被包装对象读取：
    OpenAIProvider 有 self.model；MockProvider 无 model 属性 → 用 provider_name 兜底。
    """

    def __init__(
        self,
        wrapped,
        *,
        run_id: str | uuid.UUID | None = None,
        step_id: str | uuid.UUID | None = None,
        agent_name: str | None = None,
        provider_name: str | None = None,
        context_snapshot: dict[str, Any] | None = None,
        model_config_snapshot: dict[str, Any] | None = None,
    ):
        self._wrapped = wrapped
        self._run_id = run_id
        self._step_id = step_id
        self._agent_name = agent_name
        self._provider_name = provider_name
        self._context_snapshot = context_snapshot
        self._model_config_snapshot = model_config_snapshot

    @property
    def model(self) -> str | None:
        return getattr(self._wrapped, "model", None)

    async def generate(self, system_prompt: str, user_prompt: str, temperature: float = 0.7, max_tokens: int = 4096) -> str:
        """记录非流式 LLM 调用。

        不改变返回值；只在调用前后计时，并在成功/失败后写 llm_call_logs。
        """
        rendered = _build_rendered_prompt(system_prompt, user_prompt)
        started = time.perf_counter()
        try:
            result = await self._wrapped.generate(system_prompt, user_prompt, temperature, max_tokens)
            latency_ms = int((time.perf_counter() - started) * 1000)
            await self._write_log(
                rendered_prompt=rendered,
                latency_ms=latency_ms,
                error_message=None,
            )
            return result
        except Exception as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            await self._write_log(
                rendered_prompt=rendered,
                latency_ms=latency_ms,
                error_message=str(exc),
            )
            raise

    async def generate_stream(self, system_prompt: str, user_prompt: str, temperature: float = 0.7, max_tokens: int = 4096) -> AsyncIterator[str]:
        """记录流式 LLM 调用。

        只有 stream 正常迭代结束后才写成功日志；中途异常则写 error_message 后继续抛出，
        让上层 SSE 能返回 error 事件。
        """
        rendered = _build_rendered_prompt(system_prompt, user_prompt)
        started = time.perf_counter()
        try:
            async for chunk in self._wrapped.generate_stream(system_prompt, user_prompt, temperature, max_tokens):
                yield chunk
            latency_ms = int((time.perf_counter() - started) * 1000)
            await self._write_log(
                rendered_prompt=rendered,
                latency_ms=latency_ms,
                error_message=None,
            )
        except Exception as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            await self._write_log(
                rendered_prompt=rendered,
                latency_ms=latency_ms,
                error_message=str(exc),
            )
            raise

    async def _write_log(self, *, rendered_prompt: str, latency_ms: int, error_message: str | None) -> None:
        """best-effort 写入 llm_call_logs。

        日志写入不能影响生成主链路，所以所有异常都被吞掉并写 warning。
        这里使用独立 session，是因为 LangGraph 节点执行时不适合把请求 session
        放进 state，也不希望日志事务和业务事务互相影响。
        """
        try:
            snapshot, truncated = _truncate(rendered_prompt)
            request_meta = {"prompt_snapshot_truncated": True} if truncated else None
            # 解析 step_id：不依赖 aupdate_state 注入（on_chain_start 的 aupdate_state
            # 在并行/动态节点下有竞态，节点读到 None）。改为查 run 下最新 RUNNING step。
            step_id = self._step_id
            if step_id is None and self._run_id is not None:
                step_id = await self._resolve_running_step_id()
            log = LlmCallLog(
                run_id=self._run_id,
                step_id=step_id,
                agent_name=self._agent_name,
                provider=self._provider_name,
                model=self.model,
                rendered_prompt_snapshot=snapshot,
                context_package_snapshot=self._context_snapshot,
                model_config_snapshot=self._model_config_snapshot,
                latency_ms=latency_ms,
                input_tokens=None,
                output_tokens=None,
                total_tokens=None,
                cost=None,
                error_message=error_message,
                request=request_meta,
            )
            # 用独立 session 写日志：节点在 LangGraph 执行上下文内运行，请求 session
            # 此时可能处于 flush/事务中间态。从全局 engine 新开 session 写入 + commit，
            # 完全隔离（engine 由 db.session.get_engine() 提供，避免把 AsyncSession 放进
            # graph state 导致 MemorySaver msgpack 序列化失败）。
            from db.session import get_engine
            from sqlalchemy import select as sa_select
            from sqlalchemy.ext.asyncio import async_sessionmaker
            from models.ai_run_step import AiRunStep
            engine = get_engine()
            log_session_factory = async_sessionmaker(engine, expire_on_commit=False)
            async with log_session_factory() as log_session:
                log_session.add(log)
                await log_session.commit()
        except Exception:
            logger.warning("harness llm_call_log 写入失败 agent=%s", self._agent_name, exc_info=True)

    async def _resolve_running_step_id(self) -> str | uuid.UUID | None:
        """查 run 下最新 RUNNING 的 step，用于关联 llm_call_log.step_id。

        并行节点（critic || consistency）可能同时 RUNNING，此时按 agent_name 推断
        step_name 再匹配：writer/expert_* → generate_draft，critic → critique，
        consistency_checker → consistency_check。找不到则返回 None。
        """
        from db.session import get_engine
        from sqlalchemy import select as sa_select
        from sqlalchemy.ext.asyncio import async_sessionmaker
        from models.ai_run_step import AiRunStep
        from models.harness_enums import RunStepStatus

        # agent_name → step_name 映射
        agent = self._agent_name or ""
        if agent.startswith("expert_") or agent == "writer":
            target_step_name = "generate_draft"
        elif agent == "critic":
            target_step_name = "critique"
        elif agent == "consistency_checker":
            target_step_name = "consistency_check"
        else:
            target_step_name = None

        engine = get_engine()
        sf = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with sf() as s:
                if target_step_name:
                    row = await s.execute(
                        sa_select(AiRunStep)
                        .where(AiRunStep.run_id == self._run_id, AiRunStep.step_name == target_step_name)
                        .order_by(AiRunStep.started_at.desc().nulls_last())
                        .limit(1)
                    )
                else:
                    row = await s.execute(
                        sa_select(AiRunStep)
                        .where(AiRunStep.run_id == self._run_id, AiRunStep.status == RunStepStatus.RUNNING)
                        .order_by(AiRunStep.started_at.desc().nulls_last())
                        .limit(1)
                    )
                found = row.scalar_one_or_none()
                return str(found.id) if found else None
        except Exception:
            logger.warning("harness _resolve_running_step_id 失败 agent=%s", self._agent_name, exc_info=True)
            return None
