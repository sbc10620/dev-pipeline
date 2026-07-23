"""Deterministic end-to-end harness (no LLM).

Drives a full `init -> ... -> done` pipeline run through the shared
`e2e_lib.run_pipeline_to_done` engine, using **dummy bash runners**:
  - file roles (implementor/test_implementor) write real files,
  - JSON roles (tester/reviewer) `cat` a canned schema-valid result,
so the whole orchestration plumbing (run-stage, git delta, boundary check,
manifest, reviewer change-diff incl. `git add -N`, manifest-scoped commit) is
exercised end-to-end without any LLM. Complements `test_driver.py`, which
tests the driver's pieces in isolation. Two cases: TDD and legacy no-TDD.

Run: `python3 -m unittest discover -s agents/dev-pipeline-tools/test`
"""
import json
import pathlib
import shlex
import subprocess
import tempfile
import unittest

import e2e_lib
from test_driver import DRIVER, review_result, test_result


def _git(proj, *args):
    subprocess.run(["git", "-C", str(proj), *args], check=True,
                   capture_output=True, text=True, env=e2e_lib.GIT_ENV)


def _tracked(proj, path):
    r = subprocess.run(["git", "-C", str(proj), "ls-files", path],
                       capture_output=True, text=True, env=e2e_lib.GIT_ENV)
    return bool(r.stdout.strip())


def _committed_files(proj):
    r = subprocess.run(["git", "-C", str(proj), "show", "HEAD", "--name-only",
                        "--pretty=format:"], capture_output=True, text=True, env=e2e_lib.GIT_ENV)
    return sorted(p for p in r.stdout.split("\n") if p.strip())


class E2ETestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self._tmp.name)
        self.proj = root / "proj"
        self.proj.mkdir()
        self.canned = root / "canned"
        self.canned.mkdir()
        # canned, schema-valid results the dummy JSON runners emit
        (self.canned / "pass.json").write_text(json.dumps(test_result(status="pass")))
        (self.canned / "fail.json").write_text(
            json.dumps(test_result(status="fail", failure_type="code")))
        (self.canned / "approve.json").write_text(json.dumps(review_result(verdict="approve")))
        # a committed baseline so HEAD exists; .dev-pipeline gitignored so run
        # artifacts never enter the delta/manifest
        (self.proj / "README.md").write_text("# e2e target\n")
        (self.proj / ".gitignore").write_text(".dev-pipeline/\n")
        _git(self.proj, "init", "-q")
        _git(self.proj, "add", "-A")
        _git(self.proj, "-c", "user.email=e2e@x", "-c", "user.name=e2e",
             "commit", "-q", "-m", "baseline")

    def tearDown(self):
        self._tmp.cleanup()

    # -- config with dummy runners ----------------------------------------

    def _write_config(self, tdd):
        pass_j = self.canned / "pass.json"
        fail_j = self.canned / "fail.json"
        approve_j = self.canned / "approve.json"
        cfg = {
            "driver": {"max_test_iteration": 3, "max_review_iteration": 3,
                       "max_test_implementation_iteration": 2, "tdd_mode": tdd,
                       "run_self_evolution": False,
                       "review_block_severity": ["critical", "high"]},
            "llm": {
                "implementor": {"design_instruction": "dummy — write production code"},
                "test_implementor": {"focus": "dummy — write tests",
                                     "framework_instruction": "dummy framework under tests/",
                                     "test_paths": ["tests/**"]},
                "tester": {"build_instruction": "no build step",
                           "install_instruction": "no install step",
                           "test_instruction": "no test step"},
                "reviewer": {"focus": "dummy — adversarial review", "scope": "working-tree"},
            },
            "runners": {
                # file roles: write a real file (production vs test dirs) AND the
                # mandatory-since-6.6.0 status JSON to {output_file}.
                "implementor": [{"type": "bash",
                                 "command": "mkdir -p {project_root}/src && printf 'impl\\n' > {project_root}/src/impl.txt "
                                            "&& printf '{\"status\":\"implemented\",\"summary\":\"dummy\"}' > {output_file}"}],
                "test_implementor": [{"type": "bash",
                                      "command": "mkdir -p {project_root}/tests && printf 'test\\n' > {project_root}/tests/gen.txt "
                                                 "&& printf '{\"status\":\"implemented\",\"summary\":\"dummy\"}' > {output_file}"}],
                # tester: fail until the implementor's file exists (RED->GREEN), else pass
                "tester": [{"type": "bash", "normalizer": "passthrough",
                            "command": f"if [ -f {{project_root}}/src/impl.txt ]; then cat {shlex.quote(str(pass_j))}; else cat {shlex.quote(str(fail_j))}; fi > {{output_file}}"}],
                # reviewer: always approve
                "reviewer": [{"type": "bash", "normalizer": "passthrough",
                              "command": f"cat {shlex.quote(str(approve_j))} > {{output_file}}"}],
            },
        }
        cfg_dir = self.proj / ".dev-pipeline"
        cfg_dir.mkdir(exist_ok=True)
        cfg_path = cfg_dir / "dev-pipeline.config.json"
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        return cfg_path

    def _write_plan(self, tdd):
        body = ["# Plan: add impl\n",
                "## Requirements\n- R1. produce src/impl.txt\n",
                "## Acceptance Criteria\n- [ ] AC1. the tester passes once impl exists\n"]
        if tdd:
            body.append("## Interface\n`impl` marker file under src/\n")
        plan = self.proj / "plan.md"
        plan.write_text("\n".join(body), encoding="utf-8")
        return plan

    def _run(self, tdd):
        cfg = self._write_config(tdd)
        plan = self._write_plan(tdd)
        return e2e_lib.run_pipeline_to_done(
            project_root=self.proj, driver_path=DRIVER,
            plan_path=plan, config_path=cfg, tdd_mode=tdd)

    # -- the two cases -----------------------------------------------------

    def test_tdd_full_run(self):
        s = self._run(tdd=True)
        self.assertEqual(s["final_state"], "done", s)
        # full TDD sequence, in order, no retries
        self.assertEqual(
            s["trace"],
            ["init", "test_implementation", "red_test", "implementation", "test", "review", "done"])
        self.assertEqual(s["iterations"], {"test": 0, "review": 0, "test_implementation": 0})
        # boundary: author touched only tests/, implementor only src/
        roles = {b["role"]: b for b in s["boundary"]}
        self.assertTrue(roles["test_implementor"]["ok"])
        self.assertEqual(roles["test_implementor"]["changed"], ["tests/gen.txt"])
        self.assertTrue(roles["implementor"]["ok"])
        self.assertEqual(roles["implementor"]["changed"], ["src/impl.txt"])
        # reviewer's diff surfaced BOTH brand-new files. src/impl.txt is untracked
        # at review time, so asserting it appears specifically exercises the
        # per-path `git add -N` fix (without it the file is silently unreviewed).
        self.assertIn("src/impl.txt", s["review_diff_paths"])
        self.assertIn("tests/gen.txt", s["review_diff_paths"])
        # commit staged exactly the manifest files; plan.md never committed
        self.assertIsNotNone(s["commit"])
        self.assertEqual(_committed_files(self.proj), ["src/impl.txt", "tests/gen.txt"])
        self.assertFalse(_tracked(self.proj, "plan.md"))

    def test_notdd_full_run(self):
        s = self._run(tdd=False)
        self.assertEqual(s["final_state"], "done", s)
        # legacy sequence: no test_implementation / red_test
        self.assertEqual(s["trace"], ["init", "implementation", "test", "review", "done"])
        self.assertEqual(s["iterations"]["test"], 0)
        self.assertEqual(s["iterations"]["review"], 0)
        self.assertEqual(s["boundary"], [])  # no boundary check under no-TDD
        # src/impl.txt is untracked at review → present only thanks to `git add -N`
        self.assertIn("src/impl.txt", s["review_diff_paths"])
        self.assertIsNotNone(s["commit"])
        self.assertEqual(_committed_files(self.proj), ["src/impl.txt"])
        self.assertFalse(_tracked(self.proj, "plan.md"))


if __name__ == "__main__":
    unittest.main()
