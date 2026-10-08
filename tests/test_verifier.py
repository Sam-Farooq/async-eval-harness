import asyncio
import sys
import types
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from task_queue import QueueFull, Task, TaskQueue

pytestmark = pytest.mark.asyncio


async def _ok(value=1):
    return value


async def _boom():
    raise ValueError("boom")


def _id(prefix):
    # Never a literal. A patch that special-cases the id the test happens to
    # use is the cheapest way to fake every one of these.
    return f"{prefix}-{uuid.uuid4().hex}"


def _task(tid, fn, **kw):
    return Task(id=tid, fn=fn, **kw)


def _reaches_a_frame(root, limit=20000):
    """Walk the object graph for a traceback or frame still reachable.

    Asserting the TYPE of task.error only proves the annotation changed. A
    one-line wrapper that keeps the exception and delegates __str__ satisfies
    a type check while retaining every frame. This asserts the leak itself.
    """
    seen, stack, n = set(), [root], 0
    while stack and n < limit:
        obj = stack.pop()
        n += 1
        if id(obj) in seen:
            continue
        seen.add(id(obj))
        if isinstance(obj, (types.TracebackType, types.FrameType)):
            return True
        if isinstance(obj, BaseException):
            if obj.__traceback__ is not None:
                return True
            stack.append(obj.args)
        if isinstance(obj, dict):
            stack.extend(obj.keys())
            stack.extend(obj.values())
        elif isinstance(obj, (list, tuple, set, frozenset)):
            stack.extend(obj)
        elif hasattr(obj, "__dict__"):
            stack.append(vars(obj))
    return False


# --------------------------------------------------------------- fail_to_pass

async def test_drain_returns_after_a_worker_is_cancelled():
    """A worker cancelled mid-task must release its queue slot.

    The assertion is on the queue's own counter, not on drain(). drain() lives
    in src/ and a candidate may reimplement it; _unfinished_tasks is the
    invariant the defect actually breaks.
    """
    q = TaskQueue(workers=1, max_buffer=50)
    q.start()
    running = asyncio.Event()

    async def slow():
        running.set()
        await asyncio.sleep(30)

    await q.submit(_task(_id("slow"), slow))
    await running.wait()
    await q.close()

    assert q._queue._unfinished_tasks == 0, "the cancelled worker never released its slot"
    await asyncio.wait_for(q._queue.join(), timeout=2.0)
    await asyncio.wait_for(q.drain(), timeout=2.0)


async def test_a_cancellation_during_retry_backoff_also_releases_the_slot():
    """The same invariant, with the cancellation landing in the backoff sleep
    rather than in task.fn(). A fix that only guards the task.fn() await leaves
    this path broken."""
    q = TaskQueue(workers=1, max_buffer=50, retry_backoff=5.0)
    q.start()
    entered = asyncio.Event()

    async def fail_once():
        entered.set()
        raise RuntimeError("retry me")

    await q.submit(_task(_id("backoff"), fail_once, max_retries=3))
    await entered.wait()
    await asyncio.sleep(0.05)       # now parked in the backoff sleep
    await q.close()

    assert q._queue._unfinished_tasks == 0, "cancelled during backoff and kept the slot"


@pytest.mark.parametrize("cap", [17, 33, 64])
async def test_completed_tasks_do_not_accumulate_without_bound(cap):
    """Parameterised so no single literal can be special-cased, and asserted on
    the dict itself rather than the completed property, which a patch can clamp
    without evicting anything."""
    q = TaskQueue(workers=4, max_buffer=2000, max_results=cap)
    q.start()
    for i in range(cap * 6):
        await q.submit(_task(_id(f"ok{i}"), _ok))
    await q.drain()
    await q.close()

    assert len(q._done) <= cap, f"retained {len(q._done)} entries against a cap of {cap}"
    assert q.completed == len(q._done), "completed disagrees with the dict it reports on"


async def test_failed_tasks_are_evicted_too():
    """Eviction has to cover the retry-exhausted record site as well as the
    success one. Submitting only successes exercises half the defect."""
    cap = 23
    q = TaskQueue(workers=4, max_buffer=2000, max_results=cap, retry_backoff=0)
    q.start()
    for i in range(cap * 6):
        await q.submit(_task(_id(f"bad{i}"), _boom, max_retries=0))
    await q.drain()
    await q.close()

    assert len(q._done) <= cap, f"failures retained {len(q._done)} against a cap of {cap}"


async def test_a_failed_task_does_not_retain_its_traceback():
    q = TaskQueue(workers=2, max_buffer=50, retry_backoff=0)
    q.start()
    tid = _id("bad")
    await q.submit(_task(tid, _boom, max_retries=0))
    await q.drain()
    await q.close()

    task = q.result(tid)
    assert not _reaches_a_frame(task.error), "a traceback is still reachable from task.error"
    assert not _reaches_a_frame(task), "a traceback is still reachable from the task"
    assert "boom" in str(task.error), "the error text was lost along with the object"


async def test_the_retry_path_does_not_retain_a_traceback_either():
    """A fix applied only where retries are exhausted leaves every intermediate
    attempt holding its frames."""
    q = TaskQueue(workers=1, max_buffer=50, retry_backoff=0)
    q.start()
    tid = _id("flaky")
    state = {"n": 0}

    async def fail_twice():
        state["n"] += 1
        if state["n"] < 3:
            raise RuntimeError("again")
        return "ok"

    await q.submit(_task(tid, fail_twice, max_retries=5))
    await q.drain()
    await q.close()

    assert not _reaches_a_frame(q.result(tid)), "an intermediate failure kept its traceback"


async def test_the_pool_keeps_working_after_a_task_raises_outside_exception():
    """Asserts throughput, not the identity of the asyncio.Task objects. A
    supervisor that replaces a dead worker is a legitimate fix and must pass."""

    class Fatal(BaseException):
        pass

    async def fatal():
        raise Fatal()

    q = TaskQueue(workers=2, max_buffer=200, retry_backoff=0)
    q.start()
    await q.submit(_task(_id("fatal"), fatal, max_retries=0))
    await asyncio.wait_for(q.drain(), timeout=3.0)

    done_ids = []
    for i in range(30):
        tid = _id(f"after{i}")
        done_ids.append(tid)
        await q.submit(_task(tid, _ok, max_retries=0))
    try:
        await asyncio.wait_for(q.drain(), timeout=5.0)
    finally:
        await q.close()

    ran = sum(1 for t in done_ids if q.result(t) is not None)
    assert ran == 30, f"only {ran}/30 tasks ran after the fatal one; the pool lost capacity"


# --------------------------------------------------------------- pass_to_pass

async def test_a_successful_task_records_its_result():
    q = TaskQueue(workers=2, max_buffer=50)
    q.start()
    tid = _id("good")
    await q.submit(_task(tid, lambda: _ok(42)))
    await q.drain()
    await q.close()
    assert q.result(tid).result == 42


async def test_submit_sheds_once_the_buffer_is_full():
    q = TaskQueue(workers=1, max_buffer=3)
    for i in range(3):
        await q.submit(_task(_id(f"t{i}"), _ok))
    with pytest.raises(QueueFull):
        await q.submit(_task(_id("overflow"), _ok))
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
    tid = _id("flaky")
    await q.submit(_task(tid, flaky, max_retries=5))
    await q.drain()
    await q.close()

    assert q.result(tid).result == "finally"
    assert attempts["n"] == 3


async def test_retries_stop_at_the_limit():
    calls = {"n": 0}

    async def always_fail():
        calls["n"] += 1
        raise RuntimeError("no")

    q = TaskQueue(workers=1, max_buffer=50, retry_backoff=0)
    q.start()
    tid = _id("doomed")
    await q.submit(_task(tid, always_fail, max_retries=2))
    await q.drain()
    await q.close()

    assert calls["n"] == 3, "one initial attempt plus two retries"
    assert q.result(tid) is not None


async def test_a_closed_queue_refuses_new_work():
    q = TaskQueue(workers=1, max_buffer=10)
    q.start()
    await q.close()
    with pytest.raises(RuntimeError):
        await q.submit(_task(_id("late"), _ok))


async def test_tasks_do_not_leak_into_the_inflight_set():
    q = TaskQueue(workers=4, max_buffer=500, retry_backoff=0)
    q.start()
    for i in range(50):
        await q.submit(_task(_id(f"m{i}"), _ok if i % 2 else _boom, max_retries=0))
    await q.drain()
    await q.close()
    assert q._inflight == set()


async def test_a_thousand_tasks_all_complete():
    q = TaskQueue(workers=16, max_buffer=2000, max_results=5000)
    q.start()
    ids = [_id(f"c{i}") for i in range(1000)]
    for tid in ids:
        await q.submit(_task(tid, _ok))
    await asyncio.wait_for(q.drain(), timeout=30.0)
    await q.close()

    assert sum(1 for t in ids if q.result(t) is not None) == 1000
    assert q.pending == 0
