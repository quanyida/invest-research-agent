import asyncio

import pytest

from app.agent.agentscope_runtime import _await_write
from app.agent.persist import run_terminal_status


@pytest.mark.asyncio
async def test_write_finishes_even_when_caller_is_cancelled():
    finished = asyncio.Event()

    async def write():
        await asyncio.sleep(0.05)
        finished.set()
        return "persisted"

    write_task = asyncio.create_task(write())
    caller = asyncio.create_task(_await_write(write_task))
    await asyncio.sleep(0.01)
    caller.cancel()

    with pytest.raises(asyncio.CancelledError):
        await caller

    assert finished.is_set()
    assert write_task.done()
    assert write_task.result() == "persisted"


@pytest.mark.asyncio
async def test_write_result_is_returned_when_not_cancelled():
    async def write():
        await asyncio.sleep(0)
        return (True, False)

    task = asyncio.create_task(write())
    assert await _await_write(task) == (True, False)


def test_terminal_run_status_mapping():
    assert run_terminal_status("done") == "done"
    assert run_terminal_status("degraded") == "done"
    assert run_terminal_status("error") == "failed"
