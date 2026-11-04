import asyncio
import inspect

import pytest

from api import routes
from api.routes import _astream_events_with_keepalive, _parse_directions


class _Request:
    async def is_disconnected(self):
        return False


@pytest.mark.asyncio
async def test_keepalive_does_not_cancel_pending_langgraph_next_event():
    async def stream():
        await asyncio.sleep(0.03)
        yield {"event": "on_chain_start", "name": "chapter_architect"}

    events = []
    async for event in _astream_events_with_keepalive(
        stream(),
        request=_Request(),
        heartbeat_message="still running",
        timeout_message="timed out",
        heartbeat_seconds=0.01,
        timeout_seconds=1,
        logger_context="test",
    ):
        events.append(event)
        if event.get("event") == "on_chain_start":
            break

    assert any(event.get("event") == "__sse_heartbeat__" for event in events)
    assert events[-1] == {"event": "on_chain_start", "name": "chapter_architect"}


@pytest.mark.asyncio
async def test_keepalive_emits_timeout_marker_after_total_idle_timeout():
    async def stream():
        await asyncio.sleep(1)
        yield {"event": "on_chain_start"}

    events = []
    async for event in _astream_events_with_keepalive(
        stream(),
        request=_Request(),
        heartbeat_message="still running",
        timeout_message="timed out",
        heartbeat_seconds=0.01,
        timeout_seconds=0.02,
        logger_context="test",
    ):
        events.append(event)
        if event.get("event") == "__workflow_timeout__":
            break

    assert events[-1]["event"] == "__workflow_timeout__"
    assert events[-1]["message"] == "timed out"


@pytest.mark.asyncio
async def test_keepalive_waits_for_pending_generator_before_closing():
    finalized = False

    async def stream():
        nonlocal finalized
        try:
            await asyncio.sleep(1)
            yield {"event": "too_late"}
        finally:
            finalized = True

    class DisconnectedRequest:
        async def is_disconnected(self):
            return True

    events = []
    async for event in _astream_events_with_keepalive(
        stream(),
        request=DisconnectedRequest(),
        heartbeat_message="still running",
        timeout_message="timed out",
        heartbeat_seconds=0.01,
        timeout_seconds=1,
        logger_context="disconnect-test",
    ):
        events.append(event)

    assert events[-1]["event"] == "__client_disconnected__"
    assert finalized is True


def test_empty_json_direction_array_uses_actionable_fallback():
    fallback = ["增强冲突", "补强动机", "检查衔接"]

    assert _parse_directions("[]", fallback=fallback) == fallback
    assert _parse_directions("```json\n[]\n```", fallback=fallback) == fallback


def test_task_card_resume_uses_non_cancelling_keepalive_and_rejects_empty_completion():
    """DeepSeek 等慢模型不得因 25 秒心跳取消底层生成并被误标完成。"""
    source = inspect.getsource(routes.resume_chapter_generation)
    block = source[
        source.index('if action == "approve_task_card"'):
        source.index('if action == "review"')
    ]

    assert "async for event in _astream_events_with_keepalive(" in block
    assert "asyncio.wait_for(resume_iter.__anext__(), timeout=25)" not in block
    assert "GENERATION_TRACE stage=heartbeat" in block
    assert "GENERATION_TRACE stage=empty_final_content" in block
    assert "正文生成提前结束且没有返回候选稿" in block


def test_revision_review_emits_progress_and_has_bounded_timeout():
    source = inspect.getsource(routes.resume_chapter_generation)
    block = source[
        source.index('if action == "review"'):
        source.index('if action == "revise"')
    ]

    assert "AI 正在分析候选稿并整理修改方向" in block
    assert "AI 仍在分析修改方向，请稍候" in block
    assert "elapsed >= 120" in block
    assert "fallback_directions" in block
