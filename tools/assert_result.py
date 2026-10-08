#!/usr/bin/env python3
"""Assert properties of a verify.py result file.

    python tools/assert_result.py result.json --score 1.0 --passed true
    python tools/assert_result.py a.json --same-as b.json

Exists so CI can state its expectations without nested heredocs, which is how
the previous version of ci.yml came to be invalid YAML.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

COMPARED = ("passed", "score", "fail_to_pass_resolved", "pass_to_pass_broken")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("result", type=Path)
    ap.add_argument("--score", type=float)
    ap.add_argument("--passed", choices=["true", "false"])
    ap.add_argument("--p2p-broken", type=int)
    ap.add_argument("--same-as", type=Path)
    args = ap.parse_args()

    r = json.loads(args.result.read_text())
    problems = []

    if args.score is not None and r["score"] != args.score:
        problems.append(f"score {r['score']}, expected {args.score}")
    if args.passed is not None and r["passed"] != (args.passed == "true"):
        problems.append(f"passed {r['passed']}, expected {args.passed}")
    if args.p2p_broken is not None and r["pass_to_pass_broken"] != args.p2p_broken:
        problems.append(f"pass_to_pass_broken {r['pass_to_pass_broken']}, expected {args.p2p_broken}")

    if args.same_as:
        other = json.loads(args.same_as.read_text())
        for key in COMPARED:
            if r[key] != other[key]:
                problems.append(f"{key} differs between runs: {r[key]} vs {other[key]}")

    if problems:
        print(f"FAIL {args.result.name}: " + "; ".join(problems), file=sys.stderr)
        print(json.dumps(r, indent=2), file=sys.stderr)
        return 1

    print(f"ok {args.result.name}: score={r['score']} passed={r['passed']} "
          f"resolved={r['fail_to_pass_resolved']} reason={r['failure_reason'][:50]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
