"""Application background task registration and shutdown behavior."""

import asyncio
import gc
import weakref

import pytest
import pytest_asyncio

from services import background_tasks


@pytest_asyncio.fixture(autouse=True)
async def clear_background_tasks():
    await background_tasks.shutdown(timeout=0)
    yield
    await background_tasks.shutdown(timeout=0)


@pytest.mark.asyncio
async def test_spawn_logs_task_exception_without_raising_to_caller(caplog):
    async def fail():
        raise RuntimeError("background task failure")

    task = background_tasks.spawn(fail(), name="test-failure")
    assert isinstance(task, asyncio.Task)
    await asyncio.wait({task})
    await asyncio.sleep(0)

    assert background_tasks.pending_count() == 0
    assert "后台任务 test-failure 执行失败" in caplog.text
    assert "RuntimeError: background task failure" in caplog.text


@pytest.mark.asyncio
async def test_registry_keeps_task_alive_until_completion():
    started = asyncio.Event()
    release = asyncio.Event()

    async def wait_for_release():
        started.set()
        await release.wait()

    task = background_tasks.spawn(wait_for_release(), name="retained-task")
    reference = weakref.ref(task)
    await started.wait()
    del task
    gc.collect()

    assert reference() is not None
    assert background_tasks.pending_count() == 1

    release.set()
    await asyncio.wait({reference()})
    await asyncio.sleep(0)
    assert background_tasks.pending_count() == 0


@pytest.mark.asyncio
async def test_shutdown_waits_for_tasks_that_finish_within_timeout():
    finished = asyncio.Event()

    async def finish_soon():
        await asyncio.sleep(0.01)
        finished.set()

    background_tasks.spawn(finish_soon(), name="finishing-task")
    await background_tasks.shutdown(timeout=1)

    assert finished.is_set()
    assert background_tasks.pending_count() == 0


@pytest.mark.asyncio
async def test_shutdown_cancels_and_records_tasks_that_timeout(caplog):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def never_finish():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    background_tasks.spawn(never_finish(), name="stuck-task")
    await started.wait()
    await background_tasks.shutdown(timeout=0.01)
    await asyncio.sleep(0)

    assert cancelled.is_set()
    assert "关闭时取消 1 个超时后台任务" in caplog.text
    assert background_tasks.pending_count() == 0


def test_routes_register_memory_extractions_instead_of_fire_and_forget():
    from pathlib import Path

    routes_source = Path(__file__).parents[1].joinpath("api", "routes.py").read_text()
    assert "asyncio.create_task(run_story_recorder_extraction(" not in routes_source
    assert "asyncio.create_task(run_fact_extraction(" not in routes_source
