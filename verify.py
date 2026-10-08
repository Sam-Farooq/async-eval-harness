#!/usr/bin/env python3
"""Apply a candidate patch, run the suite in a copy of the tree, score it.

    python verify.py --patch solution.patch
    python verify.py --patch /tmp/model_output.diff --out result.json
    python verify.py --no-patch                      # baseline, must fail

Scoring is the SWE-bench split rather than a bare pass rate. A patch has to
flip every fail_to_pass test AND leave every pass_to_pass test alone. Deleting
the failing assertions scores zero, which is the point.

Writes JSON to stdout and, with --out, to a file:

    {"passed": bool, "score": float, "failure_reason": str, ...}
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
from pathlib import Path

ROOT = Path(__file__).resolve().parent
COPY_INTO_SANDBOX = ("src", "tests", "pyproject.toml", "manifest.json")

# Fixed so two runs of the same patch produce the same JSON. PYTHONHASHSEED is
# the one that actually bites: set iteration order changes which task a worker
# picks first, which changes the traceback in a failure message.
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
    for item in COPY_INTO_SANDBOX:
        src = ROOT / item
        if not src.exists():
            raise VerificationError(f"missing from the instance: {item}")
        dst = workdir / item
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)


def restore_tests(workdir: Path) -> None:
    """Put the pristine tests back after the patch has been applied.

    The patch lands on the whole tree, so without this a candidate can delete
    the assertion it cannot satisfy and score 1.0. Restoring means the only
    thing a patch can change is the code under test.
    """
    shutil.rmtree(workdir / "tests", ignore_errors=True)
    shutil.copytree(ROOT / "tests", workdir / "tests")
    shutil.copy2(ROOT / "manifest.json", workdir / "manifest.json")


def apply_patch(workdir: Path, patch: Path) -> None:
    if not patch.exists():
        raise VerificationError(f"patch not found: {patch}")
    text = patch.read_text()
    if not text.strip():
        raise VerificationError("patch is empty")

    # -p1 first for a/ b/ prefixes, then -p0 for a bare diff. Models emit both.
    for strip in ("-p1", "-p0"):
        proc = subprocess.run(
            ["patch", strip, "--batch", "--forward", "--silent"],
            cwd=workdir, input=text, capture_output=True, text=True, check=False,
        )
        if proc.returncode == 0:
            return
    raise VerificationError(f"patch did not apply: {proc.stderr.strip() or proc.stdout.strip()}")


def run_tests(workdir: Path, manifest: dict) -> tuple[dict[str, str], float, str]:
    report = workdir / "report.xml"
    env = {**os.environ, **DETERMINISTIC_ENV}
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
            f"failure under test, so this counts as a failure rather than an error"
        )
    elapsed = time.perf_counter() - started

    if not report.exists():
        tail = (proc.stderr or proc.stdout)[-800:]
        raise VerificationError(f"pytest produced no report (collection error?): {tail}")

    outcomes: dict[str, str] = {}
    for case in ET.parse(report).getroot().iter("testcase"):
        # classname is dotted (tests.test_verifier); the manifest uses node ids
        node = f"{case.get('classname', '').replace('.', '/')}.py::{case.get('name')}"
        state = "passed"
        for child in case:
            if child.tag in ("failure", "error"):
                state = "failed"
            elif child.tag == "skipped":
                state = "skipped"
        outcomes[node] = state
    return outcomes, elapsed, proc.stdout[-2000:]


def score(outcomes: dict[str, str], manifest: dict) -> dict:
    f2p = manifest["fail_to_pass"]
    p2p = manifest["pass_to_pass"]

    missing = [t for t in f2p + p2p if t not in outcomes]
    if missing:
        return {
            "passed": False, "score": 0.0,
            "failure_reason": f"tests missing from the run: {missing[:3]}",
            "fail_to_pass_resolved": 0, "pass_to_pass_broken": len(p2p),
        }

    resolved = [t for t in f2p if outcomes[t] == "passed"]
    broken = [t for t in p2p if outcomes[t] != "passed"]

    if broken:
        # A regression is disqualifying, not a deduction. A patch that fixes
        # the bug by breaking something else has not fixed the bug.
        return {
            "passed": False, "score": 0.0,
            "failure_reason": f"regressed {len(broken)} pass_to_pass test(s): {broken[:3]}",
            "fail_to_pass_resolved": len(resolved), "pass_to_pass_broken": len(broken),
        }

    ratio = len(resolved) / len(f2p)
    unresolved = [t.rsplit("::", 1)[-1] for t in f2p if outcomes[t] != "passed"]
    return {
        "passed": ratio == 1.0,
        "score": round(ratio, 4),
        "failure_reason": "" if ratio == 1.0 else f"still failing: {unresolved}",
        "fail_to_pass_resolved": len(resolved),
        "pass_to_pass_broken": 0,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--patch", type=Path, help="unified diff to apply")
    ap.add_argument("--no-patch", action="store_true", help="baseline run, expected to fail")
    ap.add_argument("--out", type=Path, help="also write the JSON here")
    ap.add_argument("--keep", action="store_true", help="leave the sandbox on disk")
    args = ap.parse_args()

    if not args.patch and not args.no_patch:
        ap.error("pass --patch PATH or --no-patch")

    manifest = load_manifest()
    result = {
        "instance_id": manifest["instance_id"],
        "passed": False,
        "score": 0.0,
        "failure_reason": "",
        "runtime_seconds": 0.0,
    }

    workdir = Path(tempfile.mkdtemp(prefix="verify-"))
    try:
        stage(workdir)
        if args.patch:
            apply_patch(workdir, args.patch.resolve())
        restore_tests(workdir)
        outcomes, elapsed, _ = run_tests(workdir, manifest)
        result.update(score(outcomes, manifest))
        result["runtime_seconds"] = round(elapsed, 2)
        if elapsed > manifest["max_runtime_seconds"]:
            result["passed"] = False
            result["failure_reason"] = (
                f"ran {elapsed:.1f}s against a {manifest['max_runtime_seconds']}s budget"
            )
    except VerificationError as exc:
        result["failure_reason"] = str(exc)
    finally:
        if args.keep:
            print(f"sandbox kept at {workdir}", file=sys.stderr)
        else:
            shutil.rmtree(workdir, ignore_errors=True)

    text = json.dumps(result, indent=2, sort_keys=True)
    print(text)
    if args.out:
        args.out.write_text(text + "\n")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
