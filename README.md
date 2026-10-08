# async-eval-harness

A single SWE benchmark instance: a deliberately broken async task queue, a
test suite that pins down exactly what is broken, a deterministic verifier,
and a golden patch. Built to score model-generated patches the way SWE-bench
does, with the gaming routes closed.

```
python verify.py --no-patch                 # baseline: score 0.0, exit 1
python verify.py --patch solution.patch     # golden:   score 1.0, exit 0
```

## The instance

`src/task_queue.py` is a worker pool over `asyncio.Queue` with retries and a
bounded submission buffer. It carries four defects, chosen because each one is
invisible under the kind of testing that usually gets written:

| Defect | Why normal testing misses it |
|---|---|
| `task_done()` is skipped when a worker is cancelled mid-task | Only reachable if you cancel the pool *while* a task is in flight. `drain()` then blocks forever |
| The failed task keeps the exception object | `__traceback__` holds the frame, the frame holds every local, including the task. Memory tracks total throughput rather than concurrency |
| `_done` is never evicted | Correct at every scale a unit test runs at. Wrong after four days |
| Anything outside `Exception` escapes the worker loop | Kills the worker silently. The pool shrinks to nothing with no log line and no alert |

The third and fourth are the interesting pair. Both present as "the service
stopped working" with no traceback anywhere.

## Scoring

Not a pass rate. The manifest splits the suite:

- **`fail_to_pass`** (4 tests) are red on the unmodified tree and must be green
  after the patch. This is what the patch is for.
- **`pass_to_pass`** (7 tests) are green before and must stay green. This is
  what the patch must not break.

Any regression in `pass_to_pass` scores **0.0** outright, not a deduction. A
patch that fixes the leak by breaking the retry path has not fixed the leak.

Partial credit exists only within `fail_to_pass`: resolving three of four
scores 0.75 and still reports `passed: false`.

## Closing the obvious routes

**Editing the tests.** The patch lands on the whole tree, so a candidate can
delete the assertion it cannot satisfy. `verify.py` restores `tests/` and
`manifest.json` from the pristine copy *after* applying the patch, so the only
thing a patch can influence is `src/`. CI asserts this with a patch that
neuters two assertions and checks it still scores 0.0.

**Hanging instead of failing.** One defect presents as a deadlock, so a naive
runner stalls rather than scoring. The suite has a hard 180s kill and a 120s
budget, and an overrun is recorded as a failure with a reason, never as an
infrastructure error.

**Run-to-run drift.** `PYTHONHASHSEED=0`, `TZ=UTC`, `LC_ALL=C`,
`SOURCE_DATE_EPOCH` pinned, bytecode off. The hash seed is the one that
actually bites: set iteration order decides which task a worker picks first,
which changes the traceback text in a failure message. CI scores the same
patch twice and diffs the result.

## The golden patch is generated, not written

`solution.patch` is produced by `tools/make_solution.py`, which applies a list
of transformations to `src/task_queue.py` and diffs the result.

A hand-maintained patch drifts the moment anyone touches the source, including
for something cosmetic like an import reorder. It then fails to apply, the
instance stops validating, and nothing says why. Regenerate instead:

```
python tools/make_solution.py
```

## Output

```json
{
  "fail_to_pass_resolved": 4,
  "failure_reason": "",
  "instance_id": "asyncq-001-cancel-and-leak",
  "pass_to_pass_broken": 0,
  "passed": true,
  "runtime_seconds": 0.21,
  "score": 1.0
}
```

Keys are sorted and floats are rounded so two runs of the same patch produce
byte-identical JSON. Exit code is 0 on pass, 1 otherwise.

## Docker

```
docker build -t async-eval-harness .
docker run --rm async-eval-harness --no-patch                  # exits 1
docker run --rm async-eval-harness --patch solution.patch      # exits 0
docker run --rm -v "$PWD/cand.diff:/bench/cand.diff:ro" \
    async-eval-harness --patch /bench/cand.diff
```

Runs as uid 10001, non-root. `patch(1)` is installed explicitly because
`python:slim` does not ship it and `verify.py` shells out to it.

The default `CMD` is the baseline, which exits non-zero on purpose. An unsolved
instance failing is the correct state, so do not read that as a broken image.

## CI validates the benchmark, not just the code

The thing most likely to be silently wrong here is the benchmark itself. An
instance whose golden patch does not pass, or whose baseline accidentally
does, measures nothing, and no ordinary test suite would notice.

So `instance-validity` asserts all three properties on every push: baseline
fails with no `pass_to_pass` instability, golden patch scores exactly 1.0, and
a test-tampering patch scores 0.0.

## Layout

```
src/task_queue.py       the implementation under test
tests/test_verifier.py  11 tests, 4 fail_to_pass and 7 pass_to_pass
manifest.json           the split, timeouts, and the test command
verify.py               stage, patch, restore tests, run, score
solution.patch          generated by tools/make_solution.py
task_prompt.md          what an evaluated model is shown
```

`task_prompt.md` describes the three reported symptoms the way a bug report
would, without naming the mechanism. It is the input to the model; everything
else here is the grader.

## Adding another instance

The shape generalises: a `src/` tree, a suite split into `fail_to_pass` and
`pass_to_pass`, a manifest, and a generator for the golden patch. `verify.py`
reads everything it needs from `manifest.json` and has nothing specific to the
queue in it.

What does not generalise for free is the defect selection. A bug that a
first-pass test catches is not worth an instance, and a bug that needs a
four-day soak to show up cannot be scored in 120 seconds. The four here sit in
between, which took more iterations than writing the harness did.
