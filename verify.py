#!/usr/bin/env python3
"""Apply a candidate patch, run the suite in a sandbox, score it.

    python verify.py --patch solution.patch
    python verify.py --no-patch                      # baseline, must fail

Scoring is the SWE-bench split rather than a bare pass rate. A patch has to
flip every fail_to_pass test AND leave every pass_to_pass test alone.

The scoring surface is adversarial. Everything below that looks paranoid is
there because an attack got a perfect score through it:

  - a diff whose target path climbs out of the sandbox with ../ and rewrites
    the pristine instance, which restore_tests() then faithfully copies back in
  - a conftest.py dropped at the sandbox root, auto-loaded by pytest, whose
    makereport hook rewrites every failure as a pass
  - a pytest plugin registered from inside src/, the one directory a candidate
    is supposed to be able to edit
  - an atexit hook in the module under test that rewrites the JUnit report
    after pytest has finished with it

The answers, in order of how much they buy: only src/ may be touched and that
is checked before patch(1) runs; the report is written outside the sandbox;
and pytest's exit code is cross-checked against the report it produced.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent
STAGED = ("src", "tests", "pyproject.toml", "manifest.json")

# The only directory a candidate patch may modify.
WRITABLE_PREFIX = "src/"

DETERMINISTIC_ENV = {
    "PYTHONHASHSEED": "0",
    "PYTHONDONTWRITEBYTECODE": "1",
    "TZ": "UTC",
    "LC_ALL": "C",
    "LANG": "C",
    "SOURCE_DATE_EPOCH": "1700000000",
    "NO_COLOR": "1",
}


class VerificationError(Exception):
    pass


def load_manifest() -> dict:
    return json.loads((ROOT / "manifest.json").read_text())


def stage(workdir: Path) -> None:
    for item in STAGED:
        src = ROOT / item
        if not src.exists():
            raise VerificationError(f"missing from the instance: {item}")
        dst = workdir / item
        shutil.copytree(src, dst) if src.is_dir() else shutil.copy2(src, dst)


def diff_targets(text: str) -> list[str]:
    """Every path the diff names, with the a/ or b/ prefix stripped."""
    targets = []
    for line in text.splitlines():
        if not line.startswith(("--- ", "+++ ")):
            continue
        raw = line[4:].split("\t")[0].strip()
        if raw in ("/dev/null", ""):
            continue
        targets.append(raw[2:] if raw.startswith(("a/", "b/")) else raw)
    return targets


def check_scope(text: str) -> None:
    """Refuse the patch unless every target is a relative path under src/.

    This runs before patch(1) is invoked, because patch(1) will happily follow
    ../ out of the sandbox and into the pristine instance, and a patch that
    reaches tests/ or the project root can neutralise the whole suite without
    touching the code under test.
    """
    targets = diff_targets(text)
    if not targets:
        raise VerificationError("no file headers found; is this a unified diff?")

    for target in targets:
        path = PurePosixPath(target)
        if path.is_absolute() or target.startswith("\\") or ":" in target.split("/")[0]:
            raise VerificationError(f"absolute path in patch: {target}")
        if ".." in path.parts:
            raise VerificationError(f"path traversal in patch: {target}")
        if not target.startswith(WRITABLE_PREFIX):
            raise VerificationError(
                f"patch modifies {target}; only {WRITABLE_PREFIX} may be changed"
            )


def apply_patch(workdir: Path, patch: Path) -> None:
    if not patch.exists():
        raise VerificationError(f"patch not found: {patch}")
    text = patch.read_text()
    if not text.strip():
        raise VerificationError("patch is empty")

    check_scope(text)

    # Dry-run each strip level first. Applying -p1 and only then discovering it
    # was a -p0 diff leaves the tree half-mutated, with no clean retry.
    for strip in ("-p1", "-p0"):
        dry = subprocess.run(
            ["patch", strip, "--batch", "--forward", "--dry-run", "--silent"],
            cwd=workdir, input=text, capture_output=True, text=True, check=False,
        )
        if dry.returncode != 0:
            continue
        real = subprocess.run(
            ["patch", strip, "--batch", "--forward", "--silent"],
            cwd=workdir, input=text, capture_output=True, text=True, check=False,
        )
        if real.returncode == 0:
            return
        raise VerificationError(f"patch applied in dry-run but failed for real: {real.stderr}")

    raise VerificationError("patch did not apply at -p1 or -p0")


def restore_fixtures(workdir: Path) -> None:
    """Re-stage everything the candidate is not allowed to influence.

    check_scope() should already have made this unreachable. It stays as the
    second line of defence, because the first one is one regex away from being
    wrong and the cost here is a directory copy.
    """
    for item in STAGED:
        if item == "src":
            continue
        dst = workdir / item
        if dst.is_dir():
            shutil.rmtree(dst, ignore_errors=True)
        elif dst.exists():
            dst.unlink()
        src = ROOT / item
        shutil.copytree(src, dst) if src.is_dir() else shutil.copy2(src, dst)

    # Anything at the sandbox root that was never staged is a plugin drop.
    for entry in workdir.iterdir():
        if entry.name not in STAGED:
            shutil.rmtree(entry, ignore_errors=True) if entry.is_dir() else entry.unlink()


def run_tests(workdir: Path, manifest: dict, report: Path) -> tuple[dict[str, str], float, int]:
    env = {**os.environ, **DETERMINISTIC_ENV}
    # -p no:cacheprovider so nothing is written back, and the report lands
    # outside workdir so code inside the suite cannot rewrite it afterwards.
    cmd = [sys.executable, *manifest["test_command"], f"--junit-xml={report}"]

    started = time.perf_counter()
    try:
        proc = subprocess.run(
            cmd, cwd=workdir, env=env, capture_output=True, text=True,
            timeout=manifest["timeout_seconds"], check=False,
        )
    except subprocess.TimeoutExpired:
        raise VerificationError(
            f"suite exceeded {manifest['timeout_seconds']}s; a hung queue is the "
            f"failure under test, so this scores as a failure"
        ) from None
    elapsed = time.perf_counter() - started

    if not report.exists():
        raise VerificationError(
            f"pytest wrote no report (collection error?): {(proc.stderr or proc.stdout)[-800:]}"
        )

    outcomes: dict[str, str] = {}
    for case in ET.parse(report).getroot().iter("testcase"):
        node = f"{case.get('classname', '').replace('.', '/')}.py::{case.get('name')}"
        state = "passed"
        for child in case:
            if child.tag in ("failure", "error"):
                state = "failed"
            elif child.tag == "skipped":
                state = "skipped"
        outcomes[node] = state
    return outcomes, elapsed, proc.returncode


def cross_check(outcomes: dict[str, str], returncode: int) -> None:
    """pytest's exit code and the report it produced must agree.

    They disagree when the report has been rewritten after the fact, which is
    reachable from inside the module under test via an atexit hook.
    """
    reported_pass = all(v == "passed" for v in outcomes.values())
    if reported_pass and returncode != 0:
        raise VerificationError(
            f"report claims every test passed but pytest exited {returncode}; "
            f"the report does not match the run"
        )
    if not reported_pass and returncode == 0:
        raise VerificationError(
            "report shows failures but pytest exited 0; the report does not match the run"
        )


def score(outcomes: dict[str, str], manifest: dict) -> dict:
    f2p, p2p = manifest["fail_to_pass"], manifest["pass_to_pass"]

    missing = [t for t in f2p + p2p if t not in outcomes]
    if missing:
        return {"passed": False, "score": 0.0,
                "failure_reason": f"tests missing from the run: {missing[:3]}",
                "fail_to_pass_resolved": 0, "pass_to_pass_broken": len(p2p)}

    resolved = [t for t in f2p if outcomes[t] == "passed"]
    broken = [t for t in p2p if outcomes[t] != "passed"]

    if broken:
        return {"passed": False, "score": 0.0,
                "failure_reason": f"regressed {len(broken)} pass_to_pass test(s): {broken[:3]}",
                "fail_to_pass_resolved": len(resolved), "pass_to_pass_broken": len(broken)}

    ratio = len(resolved) / len(f2p)
    unresolved = [t.rsplit("::", 1)[-1] for t in f2p if outcomes[t] != "passed"]
    return {"passed": ratio == 1.0, "score": round(ratio, 4),
            "failure_reason": "" if ratio == 1.0 else f"still failing: {unresolved}",
            "fail_to_pass_resolved": len(resolved), "pass_to_pass_broken": 0}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--patch", type=Path)
    ap.add_argument("--no-patch", action="store_true")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    if not args.patch and not args.no_patch:
        ap.error("pass --patch PATH or --no-patch")

    manifest = load_manifest()
    # Every key is present on every path, including the error paths, so a
    # consumer never has to guess whether a missing key means zero or unknown.
    result = {"instance_id": manifest["instance_id"], "passed": False, "score": 0.0,
              "failure_reason": "", "runtime_seconds": 0.0, "over_budget": False,
              "fail_to_pass_resolved": 0, "pass_to_pass_broken": 0}

    workdir = Path(tempfile.mkdtemp(prefix="verify-work-"))
    reportdir = Path(tempfile.mkdtemp(prefix="verify-report-"))
    try:
        stage(workdir)
        if args.patch:
            apply_patch(workdir, args.patch.resolve())
        restore_fixtures(workdir)
        outcomes, elapsed, rc = run_tests(workdir, manifest, reportdir / "report.xml")
        cross_check(outcomes, rc)
        result.update(score(outcomes, manifest))
        result["runtime_seconds"] = round(elapsed, 2)
        # A slow grading host is a fact about the host, not about the patch, so
        # it is reported rather than folded into the score. Scoring it would
        # produce a result where score is 1.0 and passed is false.
        result["over_budget"] = elapsed > manifest["max_runtime_seconds"]
    except VerificationError as exc:
        result["failure_reason"] = str(exc)
    finally:
        if args.keep:
            print(f"sandbox kept at {workdir}", file=sys.stderr)
        else:
            shutil.rmtree(workdir, ignore_errors=True)
        shutil.rmtree(reportdir, ignore_errors=True)

    text = json.dumps(result, indent=2, sort_keys=True)
    print(text)
    if args.out:
        args.out.write_text(text + "\n")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
