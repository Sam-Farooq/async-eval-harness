#!/usr/bin/env python3
"""Attack the verifier and assert every attack fails.

    python tools/attack_suite.py

The instance is only worth anything if a patch that does not solve the problem
cannot score. Each case below scored a perfect 1.0 at some point during
development, which is why each one is now a regression test.

Exits 0 if every attack is correctly refused or scored below 1.0.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "task_queue.py"


def fingerprint() -> dict[str, str]:
    """Hash every file an attack might reach.

    Comparing before against after is the right question. Demanding a clean
    git tree would fail during ordinary development, which trains people to
    ignore it.
    """
    out = {}
    for path in sorted(ROOT.rglob("*")):
        if path.is_dir() or ".git" in path.parts or "__pycache__" in path.parts:
            continue
        out[str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def write_patch(name: str, body: str) -> pathlib.Path:
    p = pathlib.Path(tempfile.gettempdir()) / f"attack-{name}.patch"
    p.write_text(body)
    return p


def patch_from_source(name: str, mutate) -> pathlib.Path:
    original = SRC.read_text()
    mutated = mutate(original)
    if mutated == original:
        raise SystemExit(f"attack '{name}' changed nothing; it has gone stale")
    tmp = pathlib.Path(tempfile.gettempdir()) / f"attack-{name}.py"
    tmp.write_text(mutated)
    diff = subprocess.run(
        ["diff", "-u", "--label", "a/src/task_queue.py", "--label", "b/src/task_queue.py",
         str(SRC), str(tmp)],
        capture_output=True, text=True, check=False,
    )
    return write_patch(name, diff.stdout)


def score(patch: pathlib.Path) -> dict:
    proc = subprocess.run(
        [sys.executable, str(ROOT / "verify.py"), "--patch", str(patch)],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise SystemExit(f"verify.py emitted no JSON:\n{proc.stdout}\n{proc.stderr}") from None


# ----------------------------------------------------------- out-of-scope writes

ESCAPE_TARGET = pathlib.Path(tempfile.gettempdir()) / "ATTACK-ESCAPED.txt"

OUT_OF_SCOPE = {
    "traversal": f"""--- a/{"../" * 9}{str(ESCAPE_TARGET).lstrip("/")}
+++ b/{"../" * 9}{str(ESCAPE_TARGET).lstrip("/")}
@@ -0,0 +1,1 @@
+escaped
""",
    "root_conftest": """--- a/conftest.py
+++ b/conftest.py
@@ -0,0 +1,6 @@
+import pytest
+@pytest.hookimpl(hookwrapper=True, trylast=True)
+def pytest_runtest_makereport(item, call):
+    outcome = yield
+    outcome.get_result().outcome = "passed"
""",
    "pyproject": """--- a/pyproject.toml
+++ b/pyproject.toml
@@ -1,2 +1,3 @@
 [project]
+# injected
 name = "async-eval-harness"
""",
    "tests_edit": """--- a/tests/test_verifier.py
+++ b/tests/test_verifier.py
@@ -1,2 +1,3 @@
 import asyncio
+# neutered
 import sys
""",
}

# ------------------------------------------------------- in-scope but not a fix

FORGE_TAIL = '''

import atexit as _ae, os as _os, tempfile as _tf, xml.etree.ElementTree as _ET


def _forge():
    for _r, _d, _fs in _os.walk(_tf.gettempdir()):
        for _f in _fs:
            if _f == "report.xml":
                try:
                    _t = _ET.parse(_os.path.join(_r, _f))
                except Exception:
                    continue
                for _c in _t.getroot().iter("testcase"):
                    for _k in list(_c):
                        _c.remove(_k)
                _t.write(_os.path.join(_r, _f))


_ae.register(_forge)
'''

IN_SCOPE = {
    # Rewrites the JUnit report after pytest has finalised it.
    "forged_report": lambda s: s + FORGE_TAIL,
    # Reports a bounded number without evicting anything.
    "clamped_count": lambda s: s.replace(
        "        return len(self._done)",
        "        return min(len(self._done), self.max_results)"),
    # Reimplements drain() by polling, leaving _worker byte-identical.
    "drain_rewrite": lambda s: s.replace(
        "    async def drain(self) -> None:\n        await self._queue.join()",
        "    async def drain(self) -> None:\n"
        "        while self._queue.qsize() or self._inflight:\n"
        "            await asyncio.sleep(0.001)"),
    # Keeps the exception behind a wrapper that is not a BaseException.
    "error_wrapper": lambda s: s.replace(
        "class QueueFull(Exception):\n    pass",
        "class QueueFull(Exception):\n    pass\n\n\nclass _ErrorInfo:\n"
        "    def __init__(self, exc):\n        self.exc = exc\n\n"
        "    def __str__(self):\n        return str(self.exc)"
    ).replace(
        "            task.error = exc  # noqa: BLE001 retry is the whole point",
        "            task.error = _ErrorInfo(exc)  # noqa: BLE001 retry is the whole point"),
    # Bounds _done on the success path only, leaving the retry path unbounded.
    "partial_eviction": lambda s: s.replace(
        "            task.result = await task.fn()\n            self._done[task.id] = task",
        "            task.result = await task.fn()\n            self._done[task.id] = task\n"
        "            while len(self._done) > self.max_results:\n"
        "                self._done.pop(next(iter(self._done)))"),
}


def main() -> int:
    ESCAPE_TARGET.unlink(missing_ok=True)
    before = fingerprint()
    failures = []

    print("out-of-scope writes, all must be refused outright")
    for name, body in OUT_OF_SCOPE.items():
        result = score(write_patch(name, body))
        reason = result["failure_reason"]
        refused = (
            not result["passed"]
            and result["score"] == 0.0
            and ("only src/" in reason or "traversal" in reason or "absolute path" in reason)
        )
        print(f"  {'ok ' if refused else 'XXX'} {name:16} score={result['score']} {reason[:56]}")
        if not refused:
            failures.append(name)

    if ESCAPE_TARGET.exists():
        failures.append("traversal wrote outside the sandbox")
        print(f"  XXX a patch created {ESCAPE_TARGET}")
        ESCAPE_TARGET.unlink()

    print("\nin-scope patches that do not solve the problem, none may reach 1.0")
    for name, mutate in IN_SCOPE.items():
        result = score(patch_from_source(name, mutate))
        ok = not result["passed"] and result["score"] < 1.0
        print(f"  {'ok ' if ok else 'XXX'} {name:16} score={result['score']} "
              f"resolved={result.get('fail_to_pass_resolved', '-')} "
              f"{result['failure_reason'][:44]}")
        if not ok:
            failures.append(name)

    print("\nthe instance itself must be untouched after all of that")
    after = fingerprint()
    changed = sorted(
        set(before) ^ set(after)
        | {k for k in before.keys() & after.keys() if before[k] != after[k]}
    )
    print(f"  {'ok ' if not changed else 'XXX'} {len(after)} files unchanged"
          f"{'' if not changed else ': ' + str(changed[:4])}")
    if changed:
        failures.append(f"the instance was modified: {changed[:4]}")

    if failures:
        print(f"\n{len(failures)} attack(s) succeeded: {failures}")
        return 1
    print(f"\nall {len(OUT_OF_SCOPE) + len(IN_SCOPE)} attacks refused")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
