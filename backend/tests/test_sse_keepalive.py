import asyncio

import pytest

from api.routes import _astream_events_with_keepalive


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
