#!/usr/bin/env python3
"""Real-LLM end-to-end harness (manual, on-demand — NOT part of the unittest suite).

Runs a full pipeline through the SAME `e2e_lib.run_pipeline_to_done` engine as
`test_e2e.py`, but with the **real `claude` runners** from `config.example.json`
(default `claude -p --model sonnet ...`). It confirms that real LLM runner
outputs flow through the whole run (init -> ... -> done) and pass the driver's
validation — a smoke test, not a determinism check. It costs tokens, is
non-deterministic, and needs the `claude` CLI, so it is gated and never
collected by `unittest` (no `test_` prefix).

Note: this is a *headless mechanical orchestrator* + real runners. It does NOT
validate the SKILL's own prose orchestration — that is what a host LLM
(e.g. a subagent) following SKILL.md verifies.

Usage:
    DP_E2E_LLM=1 python3 agents/dev-pipeline-tools/test/e2e_llm.py [tdd|notdd|both]
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

import e2e_lib
from test_driver import DRIVER

EXAMPLE = pathlib.Path(DRIVER).parent / "config.example.json"

SRC_INIT = ""
TEST_SEED = '''import unittest
from src.mathutil import is_prime


class TestIsPrime(unittest.TestCase):
    def test_small_primes(self):
        self.assertTrue(is_prime(2)); self.assertTrue(is_prime(3))

    def test_non_primes(self):
        for n in (0, 1, -5, 4, 9):
            self.assertFalse(is_prime(n))

    def test_larger(self):
        self.assertTrue(is_prime(97)); self.assertFalse(is_prime(100))


if __name__ == "__main__":
    unittest.main()
'''

PLAN_BODY = """# Plan: implement is_prime

## Requirements
- R1. Implement `is_prime(n: int) -> bool` in `src/mathutil.py` (stdlib only).

## Acceptance Criteria
- [ ] AC1. `is_prime(2)` and `is_prime(3)` are True.
- [ ] AC2. `is_prime(0)`, `is_prime(1)`, negatives, and composites (4, 9, 100) are False.
- [ ] AC3. `is_prime(97)` is True.

## Interface
`is_prime(n: int) -> bool` in `src/mathutil.py` — True iff n is a prime number.

## File Layout
```
src/mathutil.py   # NEW — is_prime()
tests/            # unittest test_*.py
```
"""


def _git(proj, *args):
    subprocess.run(["git", "-C", str(proj), *args], check=True,
                   capture_output=True, text=True, env=e2e_lib.GIT_ENV)


def _setup(root, tdd):
    proj = root / ("proj-tdd" if tdd else "proj-notdd")
    (proj / "src").mkdir(parents=True)
    (proj / "tests").mkdir(parents=True)
    (proj / "src" / "__init__.py").write_text(SRC_INIT)
    (proj / "tests" / "__init__.py").write_text(SRC_INIT)
    (proj / "README.md").write_text("# e2e-llm target\n")
    (proj / ".gitignore").write_text(".dev-pipeline/\n__pycache__/\n*.pyc\n")
    if not tdd:  # legacy flow has no test author — seed the tests
        (proj / "tests" / "test_math.py").write_text(TEST_SEED)

    cfg = json.loads(EXAMPLE.read_text())  # sonnet claude runners
    cfg["driver"]["tdd_mode"] = tdd
    cfg["driver"]["run_self_evolution"] = False
    cfg["llm"]["implementor"]["design_instruction"] = (
        "Implement production code in src/mathutil.py, stdlib only. Production code only.")
    cfg["llm"]["test_implementor"]["focus"] = "One meaningful unittest per acceptance criterion."
    cfg["llm"]["test_implementor"]["framework_instruction"] = (
        "stdlib unittest under tests/, files test_*.py, import `from src.mathutil import is_prime`.")
    cfg["llm"]["test_implementor"]["test_paths"] = ["tests/**"]
    cfg["llm"]["tester"]["build_instruction"] = "no build step"
    cfg["llm"]["tester"]["install_instruction"] = "no install step"
    cfg["llm"]["tester"]["test_instruction"] = "python3 -m unittest discover -s tests -t . -p 'test_*.py' -q"
    cfg["llm"]["reviewer"]["focus"] = "Adversarially verify is_prime correctness and edge handling."

    (proj / ".dev-pipeline").mkdir()
    cfg_path = proj / ".dev-pipeline" / "dev-pipeline.config.json"
    cfg_path.write_text(json.dumps(cfg, indent=2))
    plan = proj / "plan.md"
    plan.write_text(PLAN_BODY)

    _git(proj, "init", "-q")
    _git(proj, "add", "-A")
    _git(proj, "-c", "user.email=e2e@x", "-c", "user.name=e2e", "commit", "-q", "-m", "baseline")
    return proj, plan, cfg_path


def _run_one(root, tdd):
    label = "TDD" if tdd else "no-TDD"
    print(f"\n===== real-LLM e2e: {label} =====")
    proj, plan, cfg = _setup(root, tdd)
    try:
        s = e2e_lib.run_pipeline_to_done(
            project_root=proj, driver_path=DRIVER, plan_path=plan,
            config_path=cfg, tdd_mode=tdd, header_approved=True)
    except Exception as e:  # PipelineError or a malformed echo — report, don't crash
        print(f"  FAILED ({type(e).__name__}): {e}")
        return False
    ok = s["final_state"] == "done"
    print(f"  final_state : {s['final_state']}")
    print(f"  trace       : {' -> '.join(s['trace'])}")
    print(f"  iterations  : {s['iterations']}")
    print(f"  review diff had new file: {s['review_diff_had_new_file']}")
    print(f"  commit      : {s['commit']}")
    if tdd:
        print(f"  boundary    : {[ (b['role'], b['ok']) for b in s['boundary'] ]}")
    print(f"  {'OK' if ok else 'NOT done'} (run_dir={s['run_dir']})")
    return ok


def main(argv):
    if not os.environ.get("DP_E2E_LLM"):
        print("Skipped: set DP_E2E_LLM=1 to run the real-LLM harness (costs tokens).")
        return 0
    if shutil.which("claude") is None:
        print("Skipped: `claude` CLI not on PATH.")
        return 0
    mode = (argv[1] if len(argv) > 1 else "tdd").lower()
    modes = {"tdd": [True], "notdd": [False], "both": [True, False]}.get(mode)
    if modes is None:
        print(f"usage: {argv[0]} [tdd|notdd|both]")
        return 2
    d = tempfile.mkdtemp(prefix="dp-e2e-llm-")
    results = [_run_one(pathlib.Path(d), tdd) for tdd in modes]
    ok = all(results)
    if ok:
        shutil.rmtree(d, ignore_errors=True)
    else:
        print(f"\n(kept scratch dir for inspection: {d})")
    print(f"\n===== {'ALL OK' if ok else 'SOME FAILED'} =====")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
