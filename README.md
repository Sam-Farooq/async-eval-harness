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

- **`fail_to_pass`** (9 node ids) are red on the unmodified tree and must be
  green after the patch. This is what the patch is for.
- **`pass_to_pass`** (7 node ids) are green before and must stay green. This is
  what the patch must not break.

Any regression in `pass_to_pass` scores **0.0** outright, not a deduction. A
patch that fixes the leak by breaking the retry path has not fixed the leak.

Partial credit exists only within `fail_to_pass`: resolving three of four
scores 0.75 and still reports `passed: false`.

## What it took to make the scoring hard to fake

Four independent attackers were pointed at this harness with instructions to
score 1.0 without fixing anything. All four succeeded, in nine distinct ways.
Every one of them is now a regression test in `tools/attack_suite.py`.

**Writes outside `src/`.** The worst of them: `patch(1)` follows `../` in a
target path, so a diff could climb out of the sandbox, write into the pristine
instance on disk, and have `restore_tests()` faithfully copy the attacker's own
conftest back in. That one was persistent, so every later submission scored 1.0
too, including the baseline, with nothing in the JSON to show it.

The fix is a scope check that runs before `patch(1)` is invoked at all: every
target path must be relative, free of `..`, and under `src/`. That single rule
also closed a conftest dropped at the sandbox root, a plugin registered through
`pyproject.toml`, and a straightforward edit to the test file. It additionally
fixed a *false negative*, where a correct fix was rejected for the crime of also
touching a README in the same diff.

**A forged report.** The score was read from a JUnit file the candidate's own
pytest process wrote, and `src/task_queue.py` is imported during collection, so
an `atexit` hook could rewrite the report after pytest had finished with it. The
report now lands outside the sandbox, and pytest's exit code is cross-checked
against it: a report claiming all-pass alongside a non-zero exit is refused.

**Hard-coded and partial fixes.** Every `fail_to_pass` test exposed exactly one
observable, and each was a literal the test itself supplied. Special-casing the
task id `"slow"`, or `max_results == 50`, scored 1.0 with all four defects
intact. Task ids are now `uuid4`-based, the eviction cap is parameterised over
three values, and the assertions moved from derived surfaces to mechanisms:

| Was asserted | Now asserted | Attack it killed |
|---|---|---|
| `drain()` returns | `q._queue._unfinished_tasks == 0` | reimplementing `drain()` and leaving `_worker` byte-identical |
| `q.completed <= cap` | `len(q._done) <= cap` | clamping the reported number without evicting |
| `not isinstance(err, BaseException)` | nothing reachable from the task is a frame | a one-line wrapper that keeps the exception and delegates `__str__` |
| one worker object still alive | 30 further tasks actually run | a supervisor that replaces dead workers is a valid fix and must pass |

Two tests were added outright: eviction driven entirely by *failures*, because
the original only submitted successes and so exercised one of the two record
sites, and a cancellation landing in the retry backoff rather than in
`task.fn()`.

**A self-contradicting verdict.** The wall-clock budget set `passed: false`
while leaving `score` at 1.0, so two consumers reading the same JSON disagreed
about the same run. A slow grading host is a fact about the host, so it is now
reported as `over_budget` and kept out of the score entirely.

Run the lot:

```
python tools/attack_suite.py
```

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

So CI asserts it on every push: the baseline fails with no `pass_to_pass`
instability, the golden patch scores exactly 1.0, `solution.patch` still matches
what its generator produces, the same patch scores identically twice, and all
nine attacks are refused.

## Layout

```
src/task_queue.py       the implementation under test, the only writable path
tests/test_verifier.py  14 tests, 16 node ids: 9 fail_to_pass and 7 pass_to_pass
manifest.json           the split, timeouts, writable paths, the test command
verify.py               stage, scope-check, patch, restore, run, cross-check, score
solution.patch          generated by tools/make_solution.py
tools/attack_suite.py   nine patches that must not score
tools/assert_result.py  what CI asserts about a result file
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
