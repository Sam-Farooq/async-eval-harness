# Task

`src/task_queue.py` implements an async worker pool over `asyncio.Queue` with
retries and a bounded submission buffer. It works under light load and
misbehaves under concurrency and failure.

Fix it. Do not change the public API: `TaskQueue`, `Task`, `QueueFull`,
`submit`, `start`, `drain`, `close`, `result`, and the `pending`, `completed`
and `shed` properties all keep their current names and meanings.

## Reported symptoms

**A shutdown that never completes.** A service running this queue stopped
responding to SIGTERM. The handler cancels the worker pool and then awaits
`drain()` before exiting. With no task in flight it exits cleanly. If a worker
is partway through a task when the cancellation lands, `drain()` never returns
and the process has to be killed. Reproduced by cancelling the pool while a
long task is running.

**Memory that grows with throughput and never falls.** A long-lived instance
processing a steady stream climbed from 180MB to 2.1GB over four days with a
flat task rate. Memory tracked total tasks processed, not concurrent ones.
Restarting the process returned it to 180MB. The growth continued with the
retry path disabled, but got noticeably faster with it enabled, and faster
again when failures carried large arguments.

**A pool that quietly stops working.** One deployment processed traffic
normally for about an hour, then stopped. No exception reached the logs, no
alert fired, and the process stayed up with an apparently healthy queue. The
tasks being submitted were still being accepted. Nothing was running them.
That deployment had a task type that raised a `BaseException` subclass used
internally for control flow.

## What you are given

```
src/task_queue.py     the implementation
tests/test_verifier.py the suite your patch is scored against
manifest.json         which tests must flip, and which must not move
verify.py             the scorer
```

## How you are scored

Your patch has to make every test in `fail_to_pass` pass, and leave every test
in `pass_to_pass` passing. Any regression in `pass_to_pass` scores zero
regardless of how many `fail_to_pass` tests you fixed.

Changes to `tests/` are discarded before the suite runs. Only `src/` counts.

The suite has a 120 second budget. One of the defects presents as a hang, so
an overrun is scored as a failure, not as an infrastructure error.

## Output

A unified diff against the repository root:

```
python verify.py --patch your_patch.diff
```
