"""Track fire-and-forget application tasks and shut them down cleanly."""

import asyncio
import logging

logger = logging.getLogger(__name__)
_TASKS: set[asyncio.Task] = set()


def _on_task_done(task: asyncio.Task) -> None:
    _TASKS.discard(task)
    if task.cancelled():
        return
    try:
        task.result()
    except asyncio.CancelledError:
        return
    except Exception:
        logger.warning("后台任务 %s 执行失败", task.get_name(), exc_info=True)


def spawn(coro, *, name: str) -> asyncio.Task:
    """Create and retain an application task until it completes."""
    task = asyncio.create_task(coro, name=name)
    _TASKS.add(task)
    task.add_done_callback(_on_task_done)
    return task


def pending_count() -> int:
    """Return the number of retained tasks that have not completed."""
    return sum(not task.done() for task in _TASKS)


async def shutdown(timeout: float = 10) -> None:
    """Wait for outstanding tasks, then cancel and report any that exceed timeout."""
    pending = {task for task in _TASKS if not task.done()}
    if not pending:
        return

    _, pending = await asyncio.wait(pending, timeout=max(0.0, timeout))
    if not pending:
        return

    logger.warning("关闭时取消 %d 个超时后台任务", len(pending))
    for task in pending:
        task.cancel()
    # Give cancellation a scheduling turn so async context managers can begin cleanup.
    await asyncio.sleep(0)
