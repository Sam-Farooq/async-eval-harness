#!/usr/bin/env python3
"""Regenerate solution.patch from src/task_queue.py.

The golden patch is derived, not maintained. Edit the transformations here and
run this; never edit solution.patch by hand, or it drifts from the source and
the instance silently stops validating.

    python tools/make_solution.py
"""
from __future__ import annotations

import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]

FIXES = [
    # The worker drops its queue slot when cancelled, and anything outside
    # Exception escapes the loop and kills the worker for good.
    (
        """            task = await self._queue.get()
            await self._execute(task)
            self._queue.task_done()
""",
        """            task = await self._queue.get()
            try:
                await self._execute(task)
            except asyncio.CancelledError:
                raise
            except BaseException:
                log.exception("worker %s: task %s died", name, task.id)
            finally:
                self._queue.task_done()
""",
    ),
    # Holding the exception holds its __traceback__, and that holds the frame
    # and every local in it, including the task.
    ("    error: BaseException | None = None\n", "    error: str | None = None\n"),
    ("            task.error = exc\n", "            task.error = repr(exc)\n"),
    # _done was never evicted.
    (
        """    def result(self, task_id: str) -> Task | None:
        return self._done.get(task_id)
""",
        """    def result(self, task_id: str) -> Task | None:
        return self._done.get(task_id)

    def _record(self, task: Task) -> None:
        self._done[task.id] = task
        while len(self._done) > self.max_results:
            self._done.pop(next(iter(self._done)))
""",
    ),
    ("            self._done[task.id] = task\n", "            self._record(task)\n"),
]


def main() -> int:
    source = ROOT / "src" / "task_queue.py"
    text = source.read_text()

    for old, new in FIXES:
        if old not in text:
            print(f"transformation no longer matches:\n{old[:70]}", file=sys.stderr)
            return 1
        text = text.replace(old, new)

    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
        fh.write(text)
        fixed = fh.name

    diff = subprocess.run(
        ["diff", "-u", "--label", "a/src/task_queue.py", "--label", "b/src/task_queue.py",
         str(source), fixed],
        capture_output=True, text=True, check=False,
    )
    # diff exits 1 when the files differ, which is the expected case here.
    if diff.returncode not in (0, 1):
        print(diff.stderr, file=sys.stderr)
        return 1

    (ROOT / "solution.patch").write_text(diff.stdout)
    print(f"solution.patch: {len(diff.stdout.splitlines())} lines")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
