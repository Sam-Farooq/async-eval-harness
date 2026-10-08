import asyncio
import gc
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from task_queue import QueueFull, Task, TaskQueue

pytestmark = pytest.mark.asyncio


async def _ok(value=1):
    return value


async def _boom():
    raise ValueError("boom")


def _task(tid, fn, **kw):
    return Task(id=tid, fn=fn, **kw)


# --------------------------------------------------------------- fail_to_pass

async def test_drain_returns_after_a_worker_is_cancelled():
    """A worker cancelled mid-task must still release its queue slot.

    Without it _unfinished_tasks never returns to zero and drain() blocks for
    the lifetime of the process.
    """
    q = TaskQueue(workers=1, max_buffer=50)
    q.start()
    running = asyncio.Event()

    async def slow():
        running.set()
        await asyncio.sleep(30)

    await q.submit(_task("slow", slow))
    await running.wait()
    await q.close()

    await asyncio.wait_for(q.drain(), timeout=2.0)


async def test_completed_tasks_do_not_accumulate_without_bound():
    q = TaskQueue(workers=4, max_buffer=2000, max_results=50)
    q.start()
    for i in range(400):
        await q.submit(_task(f"t{i}", _ok))
    await q.drain()
    await q.close()

    assert q.completed <= 50, f"retained {q.completed} results against a cap of 50"


async def test_a_failed_task_does_not_retain_its_traceback():
    """Keeping the exception keeps __traceback__, which keeps the frame and
    every local in it, including the task. That is the leak."""
    q = TaskQueue(workers=2, max_buffer=50, retry_backoff=0)
    q.start()
    await q.submit(_task("bad", _boom, max_retries=0))
    await q.drain()
    await q.close()

    err = q.result("bad").error
    assert not isinstance(err, BaseException), (
        f"error is a live {type(err).__name__} holding a traceback"
    )
    assert "boom" in str(err)


async def test_a_worker_survives_a_task_that_raises_outside_exception():
    """Anything not derived from Exception escapes _execute and silently kills
    the worker, shrinking the pool until nothing runs at all."""

    class Fatal(BaseException):
        pass

    async def fatal():
        raise Fatal()

    q = TaskQueue(workers=1, max_buffer=50, retry_backoff=0)
    q.start()
    await q.submit(_task("fatal", fatal, max_retries=0))
    await asyncio.sleep(0.05)

    alive = sum(1 for w in q._pool if not w.done())
    await q.submit(_task("after", _ok, max_retries=0))
    try:
        await asyncio.wait_for(q.drain(), timeout=2.0)
    finally:
        await q.close()

    assert alive == 1, "the worker pool lost a worker to an unhandled error"


# --------------------------------------------------------------- pass_to_pass

async def test_a_successful_task_records_its_result():
    q = TaskQueue(workers=2, max_buffer=50)
    q.start()
    await q.submit(_task("good", lambda: _ok(42)))
    await q.drain()
    await q.close()
    assert q.result("good").result == 42


async def test_submit_sheds_once_the_buffer_is_full():
    q = TaskQueue(workers=1, max_buffer=3)
    for i in range(3):
        await q.submit(_task(f"t{i}", _ok))
    with pytest.raises(QueueFull):
        await q.submit(_task("overflow", _ok))
    assert q.shed == 1


async def test_a_flaky_task_succeeds_on_retry():
    attempts = {"n": 0}

    async def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("not yet")
        return "finally"

    q = TaskQueue(workers=1, max_buffer=50, retry_backoff=0)
    q.start()
    await q.submit(_task("flaky", flaky, max_retries=5))
    await q.drain()
    await q.close()

    assert q.result("flaky").result == "finally"
    assert attempts["n"] == 3


async def test_retries_stop_at_the_limit():
    calls = {"n": 0}

    async def always_fail():
        calls["n"] += 1
        raise RuntimeError("no")

    q = TaskQueue(workers=1, max_buffer=50, retry_backoff=0)
    q.start()
    await q.submit(_task("doomed", always_fail, max_retries=2))
    await q.drain()
    await q.close()

    assert calls["n"] == 3, "one initial attempt plus two retries"
    assert q.result("doomed") is not None


async def test_a_closed_queue_refuses_new_work():
    q = TaskQueue(workers=1, max_buffer=10)
    q.start()
    await q.close()
    with pytest.raises(RuntimeError):
        await q.submit(_task("late", _ok))


async def test_tasks_do_not_leak_into_the_inflight_set():
    q = TaskQueue(workers=4, max_buffer=500, retry_backoff=0)
    q.start()
    for i in range(50):
        await q.submit(_task(f"m{i}", _ok if i % 2 else _boom, max_retries=0))
    await q.drain()
    await q.close()
    assert q._inflight == set()


@pytest.mark.slow
async def test_a_thousand_tasks_all_complete():
    q = TaskQueue(workers=16, max_buffer=2000, max_results=5000)
    q.start()
    for i in range(1000):
        await q.submit(_task(f"c{i}", _ok))
    await asyncio.wait_for(q.drain(), timeout=30.0)
    await q.close()

    assert q.completed == 1000
    assert q.pending == 0
    gc.collect()
