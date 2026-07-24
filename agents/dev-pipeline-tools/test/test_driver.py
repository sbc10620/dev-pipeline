#!/usr/bin/env python3
"""
Black-box tests for driver.py — the dev-pipeline state machine.

These tests guarantee that state transitions, the review gate, schema
validation, and the auxiliary subcommands behave as specified, WITHOUT
running any LLM agent or codex. They invoke driver.py exactly the way the
SKILL does — as a CLI subprocess — and assert on the emitted JSON and exit
codes. Standard library only; no external dependencies.

Run:
    python3 agents/dev-pipeline-tools/test/test_driver.py
    python3 -m unittest discover -s agents/dev-pipeline-tools/test -v
"""

import contextlib
import importlib.util
import io
import json
import pathlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest

# driver.py lives one directory up from this test file.
TOOLS_DIR = pathlib.Path(__file__).resolve().parent.parent
DRIVER = TOOLS_DIR / "driver.py"
CONFIG_EXAMPLE = TOOLS_DIR / "config.example.json"


def run_driver(*args, cwd=None):
    """Invoke driver.py as a subprocess and return the CompletedProcess.

    Parses stdout into `.json` (attribute set on the returned object) when the
    command exited 0 and produced JSON; otherwise `.json` is None.
    """
    proc = subprocess.run(
        [sys.executable, str(DRIVER), *args],
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
    )
    parsed = None
    if proc.returncode == 0 and proc.stdout.strip():
        try:
            parsed = json.loads(proc.stdout)
        except json.JSONDecodeError:
            parsed = None
    proc.json = parsed  # type: ignore[attr-defined]
    return proc


# Concrete bash runners for tests. config.example.json ships runners as the
# 'unconfigured' sentinel (the user configures them via --update-config), so tests
# that need a runnable config define their own here instead of inheriting them.
VALID_RUNNERS = {
    "implementor":      [{"type": "bash", "command": "echo {system_file} {user_file}"}],
    "test_implementor": [{"type": "bash", "command": "echo {system_file} {user_file}"}],
    "tester":           [{"type": "bash", "command": "echo x > {output_file}", "normalizer": "default"}],
    "reviewer":         [{"type": "bash", "command": "echo x > {output_file}", "normalizer": "default"}],
}


def valid_config(**driver_overrides):
    """Return a schema-valid config dict with real (non-placeholder) instructions
    and concrete runners.

    Defaults to tdd_mode=False so the legacy-flow suites read unchanged; TDD
    tests opt in with valid_config(tdd_mode=True). The test_implementor block is
    filled with real values so it validates whenever TDD is enabled.
    `driver_overrides` patch the `driver` block per test.
    """
    cfg = json.loads(CONFIG_EXAMPLE.read_text(encoding="utf-8"))
    cfg["runners"] = json.loads(json.dumps(VALID_RUNNERS))  # deep copy
    cfg["llm"]["tester"] = {
        "build_instruction": "no build step",
        "install_instruction": "no install step",
        "test_instruction": "no test step",
    }
    cfg["llm"]["test_implementor"] = {
        "focus": "one meaningful test per acceptance criterion",
        "framework_instruction": "pytest under tests/, one test per AC",
        "test_paths": ["tests/**"],
    }
    cfg["driver"]["tdd_mode"] = False
    cfg["driver"].update(driver_overrides)
    return cfg


def test_result(status="pass", failure_type=None, **extra):
    """Build a schema-valid test-result dict."""
    obj = {
        "status": status,
        "stages": [
            {
                "name": "test",
                "command": "no test step",
                "exit_code": 0 if status == "pass" else 1,
                "status": status,
                "summary": "stub stage",
            }
        ],
        "summary": "stub test result",
    }
    if failure_type is not None:
        obj["failure_type"] = failure_type
    obj.update(extra)
    return obj


def implementor_result(status="implemented", **extra):
    """Build a schema-valid implementor-result dict (shared by test_implementor).
    Since 7.2.0 `blocked_on` is required (no default) whenever status is
    "blocked" — default it to "contract" here so existing blocked-result callers
    stay schema-valid without each specifying it; callers that care about routing
    pass blocked_on= explicitly via **extra."""
    obj = {"status": status, "summary": "stub implementor result"}
    if status == "blocked":
        obj["concern"] = "stub concern"
        obj["blocked_on"] = "contract"
    obj.update(extra)
    return obj


def review_result(verdict="approve", findings=None, **extra):
    """Build a schema-valid review-result dict."""
    obj = {
        "verdict": verdict,
        "summary": "stub review result",
        "findings": findings if findings is not None else [],
    }
    obj.update(extra)
    return obj


def plan_review_result(verdict="approve", findings=None, **extra):
    """Build a schema-valid plan-review-result dict."""
    obj = {
        "verdict": verdict,
        "summary": "stub plan review result",
        "findings": findings if findings is not None else [],
    }
    obj.update(extra)
    return obj


def finding(severity="critical", title="Stub finding", file="src/foo.py"):
    """Build a schema-valid review finding."""
    return {
        "severity": severity,
        "title": title,
        "body": "stub body",
        "file": file,
        "line_start": 1,
        "line_end": 2,
        "confidence": 0.9,
        "recommendation": "fix it",
    }


def plan_body(tdd=False):
    """A minimal but VALID plan body (the whole plan.md is the contract). Contains the
    sections `validate_plan_body` requires: Requirements + Acceptance Criteria,
    plus Interface under TDD."""
    parts = [
        "# Plan: Test\n",
        "## Requirements\n- R1. do the thing\n",
        "## Acceptance Criteria\n- [ ] AC1. given x, return y\n",
    ]
    if tdd:
        parts.append("## Interface\n`do(x) -> y`\n")
    return "\n".join(parts)


class Pipeline:
    """Drives a single pipeline run inside a temporary project directory.

    Encapsulates the init → advance → write result loop so each test reads as a
    short sequence of transitions and assertions. As of 5.0.0 there is no spec
    stage: `init` writes the contract (contract.md) from the plan body itself.
    """

    def __init__(self, config, git=False, git_commit=True):
        """`git`/`git_commit`: initialize self.project as a real git repo (with an
        initial commit unless git_commit=False) — needed only by --worktree tests;
        every other test leaves the default (plain temp dir, no git) unchanged."""
        self._tmp = tempfile.TemporaryDirectory()
        self.project = pathlib.Path(self._tmp.name)
        self._config = config
        self.run_dir = None
        self.contract_path = None
        self.work_root = None
        if git:
            subprocess.run(["git", "init", "-q", str(self.project)], check=True)
            subprocess.run(["git", "-C", str(self.project), "config", "user.email", "t@example.com"], check=True)
            subprocess.run(["git", "-C", str(self.project), "config", "user.name", "Test"], check=True)
            if git_commit:
                (self.project / ".gitkeep").write_text("", encoding="utf-8")
                subprocess.run(["git", "-C", str(self.project), "add", ".gitkeep"], check=True)
                subprocess.run(["git", "-C", str(self.project), "commit", "-q", "-m", "initial"], check=True)

    def close(self):
        self._tmp.cleanup()

    # -- setup -------------------------------------------------------------

    def init(self, tdd=None, plan_text=None, worktree=False, expect_success=True):
        """Run driver init. `tdd` (if given) is baked into the config's
        driver.tdd_mode — the per-run --tdd/--no-tdd flags were removed in 5.0.0.
        `plan_text` overrides the default valid plan body. `worktree` passes
        --worktree through. `expect_success=False` returns the raw CompletedProcess
        instead of asserting success and returning its parsed JSON — for tests
        exercising an expected init failure (e.g. --worktree without a git repo)."""
        if tdd is not None:
            self._config.setdefault("driver", {})["tdd_mode"] = tdd
        tdd_eff = self._config.get("driver", {}).get("tdd_mode", True)
        plan = self.project / "plan.md"
        plan.write_text(plan_text if plan_text is not None else plan_body(tdd_eff),
                        encoding="utf-8")
        cfg_dir = self.project / ".dev-pipeline"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        cfg_path = cfg_dir / "dev-pipeline.config.json"
        cfg_path.write_text(json.dumps(self._config), encoding="utf-8")

        args = ["init", "--plan", str(plan), "--config", str(cfg_path),
                "--project", str(self.project)]
        if worktree:
            args.append("--worktree")
        proc = run_driver(*args)
        if not expect_success:
            return proc
        assert proc.returncode == 0, f"init failed: {proc.stderr}"
        self.run_dir = pathlib.Path(proc.json["run_dir"])
        self.contract_path = pathlib.Path(proc.json["contract_path"])
        self.work_root = pathlib.Path(proc.json["work_root"])
        return proc.json

    # -- driving -----------------------------------------------------------

    def advance(self):
        proc = run_driver("advance", "--run", str(self.run_dir))
        return proc

    def status(self):
        proc = run_driver("status", "--run", str(self.run_dir))
        assert proc.returncode == 0, f"status failed: {proc.stderr}"
        return proc.json

    def _current_iter_dir(self):
        # Mirrors driver.get_iter_path: n = test_implementation + test + review.
        st = self.status()
        it = st["iterations"]
        n = it.get("test_implementation", 0) + it["test"] + it["review"]
        return self.run_dir / "iterations" / str(n)

    def write_test_result(self, **kwargs):
        d = self._current_iter_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / "test-result.json").write_text(
            json.dumps(test_result(**kwargs)), encoding="utf-8"
        )

    def write_implementor_result(self, **kwargs):
        """Write the (since 6.6.0, mandatory) implementor-result.json the
        implementation state's advance reads to decide its next transition."""
        d = self._current_iter_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / "implementor-result.json").write_text(
            json.dumps(implementor_result(**kwargs)), encoding="utf-8"
        )

    def write_test_implementor_result(self, **kwargs):
        """Write the (since 6.6.0, mandatory) test_implementor-result.json —
        shares the implementor-result schema/factory."""
        d = self._current_iter_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / "test_implementor-result.json").write_text(
            json.dumps(implementor_result(**kwargs)), encoding="utf-8"
        )

    def write_red_test_result(self, **kwargs):
        """Write the red-test-result.json the red_test state consumes."""
        d = self._current_iter_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / "red-test-result.json").write_text(
            json.dumps(test_result(**kwargs)), encoding="utf-8"
        )

    def write_review_result(self, **kwargs):
        d = self._current_iter_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / "review-result.json").write_text(
            json.dumps(review_result(**kwargs)), encoding="utf-8"
        )

    def write_raw_result_file(self, name, content):
        """Write an arbitrary (possibly invalid) result file for guard tests."""
        d = self._current_iter_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(content, encoding="utf-8")


class PipelineTestCase(unittest.TestCase):
    """Base class that tracks Pipelines and cleans them up."""

    def setUp(self):
        self._pipelines = []

    def tearDown(self):
        for p in self._pipelines:
            p.close()

    def make_pipeline(self, **driver_overrides):
        p = Pipeline(valid_config(**driver_overrides))
        self._pipelines.append(p)
        return p

    def started(self, **driver_overrides):
        """Init, leaving the run in `init` state ready to advance (contract.md is
        written by init)."""
        p = self.make_pipeline(**driver_overrides)
        p.init()
        return p

    def to_test_state(self, p):
        """Advance init → implementation → test. Returns the advance JSON that
        landed in `test` (i.e. the implementation→test transition output)."""
        r1 = p.advance()  # init -> implementation
        self.assertEqual(r1.json["next_state"], "implementation")
        p.write_implementor_result(status="implemented")
        r2 = p.advance()  # implementation -> test
        self.assertEqual(r2.json["next_state"], "test")
        return r2.json


class TestHappyPath(PipelineTestCase):
    def test_full_run_to_done(self):
        p = self.started()

        # init -> implementation
        r = p.advance()
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.json["previous_state"], "init")
        self.assertEqual(r.json["next_state"], "implementation")
        self.assertEqual(r.json["directive"], "run_implementor")

        # implementation -> test
        p.write_implementor_result(status="implemented")
        r = p.advance()
        self.assertEqual(r.json["next_state"], "test")
        self.assertEqual(r.json["directive"], "run_tester")
        test_iter_dir = r.json["iter_dir"]

        # test(pass) -> review (same iteration dir, no counter change)
        p.write_test_result(status="pass")
        r = p.advance()
        self.assertEqual(r.json["next_state"], "review")
        self.assertEqual(r.json["directive"], "run_reviewer")
        self.assertEqual(r.json["iterations"], {"test": 0, "review": 0, "test_implementation": 0})
        self.assertEqual(
            r.json["iter_dir"], test_iter_dir,
            "review must reuse the same iteration dir as a passing test",
        )

        # review(approve) -> done
        p.write_review_result(verdict="approve")
        r = p.advance()
        self.assertEqual(r.json["next_state"], "done")
        self.assertEqual(r.json["directive"], "finalize")
        self.assertEqual(r.json["iterations"], {"test": 0, "review": 0, "test_implementation": 0})


class TestTestFailures(PipelineTestCase):
    def test_code_failure_retries_implementation(self):
        p = self.started()
        self.to_test_state(p)

        p.write_test_result(status="fail", failure_type="code",
                            failure_details="boom")
        r = p.advance()
        self.assertEqual(r.json["next_state"], "implementation")
        self.assertEqual(r.json["directive"], "run_implementor")
        self.assertEqual(r.json["iterations"]["test"], 1)
        self.assertIsNone(r.json["halt_reason"])

    def test_environment_failure_halts_immediately(self):
        p = self.started()
        self.to_test_state(p)

        p.write_test_result(status="fail", failure_type="environment",
                            failure_details="no toolchain")
        r = p.advance()
        self.assertEqual(r.json["next_state"], "failed")
        self.assertEqual(r.json["halt_reason"], "environment")
        self.assertEqual(r.json["directive"], "halt_and_ask")
        # No retry: the test counter must not have been incremented.
        self.assertEqual(r.json["iterations"]["test"], 0)

    def test_code_failures_exhaust_to_failed(self):
        p = self.started(max_test_iteration=1)
        self.to_test_state(p)

        # First code failure: counter -> 1 (1 > 1 is False), retry.
        p.write_test_result(status="fail", failure_type="code")
        r = p.advance()
        self.assertEqual(r.json["next_state"], "implementation")
        self.assertEqual(r.json["iterations"]["test"], 1)

        # Re-run implementation -> test, then fail again to exhaust.
        p.write_implementor_result(status="implemented")
        r = p.advance()
        self.assertEqual(r.json["next_state"], "test")
        p.write_test_result(status="fail", failure_type="code")
        r = p.advance()
        self.assertEqual(r.json["next_state"], "failed")
        self.assertEqual(r.json["halt_reason"], "iteration-exhausted")
        self.assertEqual(r.json["directive"], "report_failure")
        self.assertEqual(r.json["iterations"]["test"], 2)


class TestReviewFailures(PipelineTestCase):
    def _to_review_state(self, p):
        self.to_test_state(p)
        p.write_test_result(status="pass")
        r = p.advance()
        self.assertEqual(r.json["next_state"], "review")
        return r

    def test_blocking_finding_retries_implementation(self):
        p = self.started()
        self._to_review_state(p)

        p.write_review_result(verdict="needs-attention",
                             findings=[finding(severity="critical")])
        r = p.advance()
        self.assertEqual(r.json["next_state"], "implementation")
        self.assertEqual(r.json["directive"], "run_implementor")
        self.assertEqual(r.json["iterations"]["review"], 1)

    def test_review_failures_exhaust_to_failed(self):
        p = self.started(max_review_iteration=1)
        self._to_review_state(p)

        # First blocking review: counter -> 1, retry implementation.
        p.write_review_result(verdict="needs-attention",
                             findings=[finding(severity="critical")])
        r = p.advance()
        self.assertEqual(r.json["next_state"], "implementation")
        self.assertEqual(r.json["iterations"]["review"], 1)

        # implementation -> test -> review again, then fail to exhaust.
        p.write_implementor_result(status="implemented")
        self.assertEqual(p.advance().json["next_state"], "test")
        p.write_test_result(status="pass")
        self.assertEqual(p.advance().json["next_state"], "review")
        p.write_review_result(verdict="needs-attention",
                             findings=[finding(severity="critical")])
        r = p.advance()
        self.assertEqual(r.json["next_state"], "failed")
        self.assertEqual(r.json["halt_reason"], "iteration-exhausted")
        self.assertEqual(r.json["iterations"]["review"], 2)

    def test_severity_gate_low_finding_passes(self):
        # Default gate blocks critical+high; a lone low finding must pass.
        p = self.started()
        self._to_review_state(p)
        p.write_review_result(verdict="needs-attention",
                             findings=[finding(severity="low")])
        r = p.advance()
        self.assertEqual(r.json["next_state"], "done")

    def test_verdict_gate_approve_passes_despite_findings(self):
        # review_block_severity=null switches to verdict-based gating.
        p = self.started(review_block_severity=None)
        self._to_review_state(p)
        p.write_review_result(verdict="approve",
                             findings=[finding(severity="critical")])
        r = p.advance()
        self.assertEqual(r.json["next_state"], "done")

    def test_verdict_gate_needs_attention_fails(self):
        p = self.started(review_block_severity=None)
        self._to_review_state(p)
        # No findings at all, but verdict says needs-attention -> retry.
        p.write_review_result(verdict="needs-attention", findings=[])
        r = p.advance()
        self.assertEqual(r.json["next_state"], "implementation")
        self.assertEqual(r.json["iterations"]["review"], 1)


class TestTerminalAndGuards(PipelineTestCase):
    def test_advance_from_done_is_noop(self):
        p = self.started()
        self.to_test_state(p)
        p.write_test_result(status="pass")
        p.advance()  # -> review
        p.write_review_result(verdict="approve")
        p.advance()  # -> done

        r = p.advance()
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.json["next_state"], "done")
        self.assertIn("terminal state", r.json["message"])

    def test_advance_from_failed_is_noop(self):
        p = self.started()
        self.to_test_state(p)
        p.write_test_result(status="fail", failure_type="environment")
        p.advance()  # -> failed

        r = p.advance()
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.json["next_state"], "failed")
        self.assertIn("terminal state", r.json["message"])

    def test_init_rejects_plan_missing_sections_and_creates_no_run(self):
        # A plan body missing `## Acceptance Criteria` is not a usable contract.
        # init must die BEFORE creating any run on disk (the section gate must not
        # be bypassable via a later `advance`).
        p = self.make_pipeline()
        plan = p.project / "plan.md"
        plan.write_text("# Plan\n\n## Requirements\n- R1. do it\n", encoding="utf-8")
        cfg_dir = p.project / ".dev-pipeline"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        cfg_path = cfg_dir / "dev-pipeline.config.json"
        cfg_path.write_text(json.dumps(p._config), encoding="utf-8")
        r = run_driver("init", "--plan", str(plan), "--config", str(cfg_path),
                       "--project", str(p.project))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Acceptance Criteria", r.stderr)
        self.assertFalse((p.project / ".dev-pipeline" / "runs").exists())
        self.assertFalse((p.project / ".dev-pipeline" / "latest").exists())

    def test_test_state_without_result_file_dies(self):
        p = self.started()
        self.to_test_state(p)
        # No test-result.json written.
        r = p.advance()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("test-result.json not found", r.stderr)

    def test_review_state_without_result_file_dies(self):
        p = self.started()
        self.to_test_state(p)
        p.write_test_result(status="pass")
        p.advance()  # -> review
        r = p.advance()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("review-result.json not found", r.stderr)

    def test_schema_invalid_test_result_dies(self):
        p = self.started()
        self.to_test_state(p)
        # 'status' must be one of pass|fail.
        p.write_raw_result_file(
            "test-result.json",
            json.dumps({"status": "maybe", "stages": [], "summary": "x"}),
        )
        r = p.advance()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("schema violation", r.stderr)

    def test_schema_invalid_review_result_dies(self):
        p = self.started()
        self.to_test_state(p)
        p.write_test_result(status="pass")
        p.advance()  # -> review
        p.write_raw_result_file(
            "review-result.json",
            json.dumps({"verdict": "ok", "summary": "x", "findings": []}),
        )
        r = p.advance()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("schema violation", r.stderr)

    def test_review_result_rejects_next_steps_field(self):
        # `next_steps` was removed (7.2.0, dead field — written but never read
        # by the driver or surfaced by any state file); additionalProperties:false
        # must reject a result that still carries it.
        p = self.started()
        self.to_test_state(p)
        p.write_test_result(status="pass")
        p.advance()  # -> review
        p.write_raw_result_file(
            "review-result.json",
            json.dumps(review_result(verdict="approve", next_steps=["do the thing"])),
        )
        r = p.advance()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("schema violation", r.stderr)

    def test_advance_tolerates_legacy_review_source_key(self):
        # A run interrupted under a pre-6.1.1 driver may leave a persisted
        # review-result.json still carrying the removed `source` field; advance
        # must strip it rather than die on re-validation (old-run compat).
        p = self.started()
        self.to_test_state(p)
        p.write_test_result(status="pass")
        p.advance()  # -> review
        rr = review_result(verdict="approve")
        rr["source"] = "bash-runner"  # legacy stamped value
        p.write_raw_result_file("review-result.json", json.dumps(rr))
        r = p.advance()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.json["next_state"], "done")

    def test_iteration_dir_numbering_matches_counter_sum(self):
        # After a code-failure retry, the active iteration dir must be
        # iterations/<test+review>, matching get_iter_path.
        p = self.started()
        self.to_test_state(p)
        self.assertTrue((p.run_dir / "iterations" / "0").is_dir())

        p.write_test_result(status="fail", failure_type="code")
        p.advance()  # test fail -> implementation, test counter -> 1
        st = p.status()
        n = st["iterations"]["test"] + st["iterations"]["review"]
        self.assertEqual(n, 1)
        self.assertTrue((p.run_dir / "iterations" / "1").is_dir())


class TestInit(PipelineTestCase):
    def test_init_creates_run_structure(self):
        p = self.make_pipeline()
        out = p.init()

        run_dir = pathlib.Path(out["run_dir"])
        self.assertTrue((run_dir / "state.json").is_file())
        self.assertTrue((run_dir / "config.snapshot.json").is_file())
        self.assertTrue((run_dir / "attempts.md").is_file())

        latest = p.project / ".dev-pipeline" / "latest"
        self.assertTrue(latest.is_symlink())
        self.assertEqual(latest.resolve(), run_dir.resolve())

        state = json.loads((run_dir / "state.json").read_text())
        self.assertEqual(state["state"], "init")
        self.assertEqual(state["iterations"], {"test": 0, "review": 0, "test_implementation": 0})
        self.assertIn("dev_pipeline_version", state)
        # max is populated from config.
        self.assertEqual(state["max"]["test"], 5)
        self.assertEqual(state["max"]["review"], 3)

    def test_init_missing_plan_dies(self):
        p = self.make_pipeline()
        cfg_dir = p.project / ".dev-pipeline"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        cfg_path = cfg_dir / "dev-pipeline.config.json"
        cfg_path.write_text(json.dumps(valid_config()), encoding="utf-8")

        r = run_driver("init", "--plan", str(p.project / "nope.md"),
                       "--config", str(cfg_path), "--project", str(p.project))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Plan file not found", r.stderr)

    def test_init_missing_config_dies(self):
        p = self.make_pipeline()
        plan = p.project / "plan.md"
        plan.write_text("# Plan\n", encoding="utf-8")
        r = run_driver("init", "--plan", str(plan),
                       "--config", str(p.project / "nope.json"),
                       "--project", str(p.project))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Config file not found", r.stderr)


class TestWorktree(PipelineTestCase):
    """--worktree: isolate a run's code edits + git bookkeeping in a fresh git
    worktree + branch instead of project_dir's own working tree."""

    def _git_pipeline(self, git_commit=True, **driver_overrides):
        p = Pipeline(valid_config(**driver_overrides), git=True, git_commit=git_commit)
        self._pipelines.append(p)
        return p

    def test_init_worktree_creates_checkout_and_branch(self):
        p = self._git_pipeline()
        j = p.init(worktree=True)

        work_root = pathlib.Path(j["work_root"])
        self.assertNotEqual(work_root, p.project)
        self.assertTrue(work_root.is_dir())

        run_id = j["run_id"]
        branch = f"dev-pipeline/{run_id}"
        branches = subprocess.run(
            ["git", "-C", str(p.project), "branch", "--list", branch],
            capture_output=True, text=True, check=True).stdout
        self.assertIn(branch, branches)

        state = json.loads((p.run_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["work_root"], str(work_root))
        self.assertEqual(state["worktree_branch"], branch)
        self.assertTrue(state["worktree_base_ref"])

    def test_init_worktree_requires_git_repo(self):
        p = self.make_pipeline()  # plain temp dir, not a git repo
        proc = p.init(worktree=True, expect_success=False)
        self.assertNotEqual(proc.returncode, 0)
        # No partial run left on disk — the same "no partial run" contract as a
        # rejected plan/config.
        self.assertFalse((p.project / ".dev-pipeline" / "runs").exists())

    def test_init_worktree_requires_head(self):
        p = self._git_pipeline(git_commit=False)  # git repo, but no commits yet
        proc = p.init(worktree=True, expect_success=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse((p.project / ".dev-pipeline" / "runs").exists())

    def test_run_stage_uses_work_root_as_cwd(self):
        p = self._git_pipeline()
        j = p.init(worktree=True)
        work_root = pathlib.Path(j["work_root"])

        # Override the implementor runner (post-init, in the frozen snapshot run-stage
        # actually reads) to prove where it executes: `pwd` written via the
        # {project_root} placeholder, which run-stage must substitute to work_root.
        snap_path = p.run_dir / "config.snapshot.json"
        snap = json.loads(snap_path.read_text(encoding="utf-8"))
        snap["runners"]["implementor"] = [
            {"type": "bash", "command": "pwd > {project_root}/cwd_marker.txt && "
             "echo '{\"status\":\"implemented\",\"summary\":\"stub\"}' > {output_file}"},
        ]
        snap_path.write_text(json.dumps(snap), encoding="utf-8")

        adv = p.advance()  # init -> implementation
        self.assertEqual(adv.json["next_state"], "implementation")
        self.assertEqual(adv.json["work_root"], str(work_root))
        iter_dir = pathlib.Path(adv.json["iter_dir"])

        rs = run_driver("run-stage", "--run", str(p.run_dir), "--role", "implementor",
                        "--stage-input", str(iter_dir / "stage-input.json"))
        self.assertEqual(rs.returncode, 0, rs.stderr)
        marker = work_root / "cwd_marker.txt"
        self.assertTrue(marker.exists(),
                        "runner did not execute with cwd/{project_root} == work_root")
        self.assertEqual(marker.read_text(encoding="utf-8").strip(), str(work_root))
        self.assertFalse((p.project / "cwd_marker.txt").exists(),
                         "runner incorrectly ran against project_dir instead of work_root")

    def test_cleanup_worktree_removes_checkout_and_branch(self):
        p = self._git_pipeline()
        j = p.init(worktree=True)
        work_root = pathlib.Path(j["work_root"])
        state = json.loads((p.run_dir / "state.json").read_text(encoding="utf-8"))
        branch = state["worktree_branch"]

        r = run_driver("cleanup-worktree", "--run", str(p.run_dir))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.json["cleaned"])
        self.assertTrue(r.json["worktree_removed"])
        self.assertTrue(r.json["branch_removed"])
        self.assertFalse(work_root.exists())
        branches = subprocess.run(
            ["git", "-C", str(p.project), "branch", "--list", branch],
            capture_output=True, text=True, check=True).stdout
        self.assertNotIn(branch, branches)

        # Idempotent: a second call is a clean no-op, not an error.
        r2 = run_driver("cleanup-worktree", "--run", str(p.run_dir))
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertTrue(r2.json["cleaned"])

    def test_cleanup_worktree_force_removes_dirty_worktree(self):
        # The `test` stage runs with cwd=work_root, so build/test caches routinely
        # leave untracked files behind — cleanup must not be blocked by them.
        p = self._git_pipeline()
        j = p.init(worktree=True)
        work_root = pathlib.Path(j["work_root"])
        (work_root / "build_cache").mkdir()
        (work_root / "build_cache" / "artifact.o").write_text("junk", encoding="utf-8")

        r = run_driver("cleanup-worktree", "--run", str(p.run_dir))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.json["worktree_removed"])
        self.assertFalse(work_root.exists())

    def test_cleanup_worktree_reports_unmerged_branch_without_forcing(self):
        p = self._git_pipeline()
        j = p.init(worktree=True)
        work_root = pathlib.Path(j["work_root"])
        state = json.loads((p.run_dir / "state.json").read_text(encoding="utf-8"))
        branch = state["worktree_branch"]

        # Commit something ON the worktree branch that the base branch never gets —
        # cleanup-worktree must not discard this unmerged work.
        (work_root / "new_file.txt").write_text("unmerged work\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(work_root), "add", "new_file.txt"], check=True)
        subprocess.run(["git", "-C", str(work_root), "commit", "-q", "-m", "unmerged change"], check=True)

        r = run_driver("cleanup-worktree", "--run", str(p.run_dir))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.json["worktree_removed"])
        self.assertFalse(r.json["branch_removed"])
        self.assertTrue(r.json["branch_error"])
        branches = subprocess.run(
            ["git", "-C", str(p.project), "branch", "--list", branch],
            capture_output=True, text=True, check=True).stdout
        self.assertIn(branch, branches)

    def test_cleanup_worktree_noop_for_non_worktree_run(self):
        p = self.started()  # plain (non-git) init, no --worktree
        r = run_driver("cleanup-worktree", "--run", str(p.run_dir))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(r.json["cleaned"])
        self.assertEqual(r.json["reason"], "not a worktree run")

    def test_reserve_run_id_dedupes_on_collision(self):
        # Deterministic unit test of the collision-avoidance helper itself (run
        # via subprocess, `run_id_new()`'s real 1-second resolution can't be forced
        # to collide reliably) — import driver.py as a module and monkeypatch
        # run_id_new, following the same pattern used elsewhere in this file.
        spec = importlib.util.spec_from_file_location("dp_driver_worktree", DRIVER)
        drv = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(drv)

        proj = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, proj, ignore_errors=True)
        runs_dir = proj / ".dev-pipeline" / "runs"
        runs_dir.mkdir(parents=True)
        fixed_rid = "20260101-000000"
        (runs_dir / fixed_rid).mkdir()
        (runs_dir / f"{fixed_rid}-2").mkdir()

        orig = drv.run_id_new
        drv.run_id_new = lambda: fixed_rid
        try:
            rid = drv.reserve_run_id(proj)
        finally:
            drv.run_id_new = orig
        self.assertEqual(rid, f"{fixed_rid}-3")

    def test_cmd_init_retries_run_dir_on_real_collision(self):
        # reserve_run_id only PROBES for a free rid; cmd_init's own run_dir.mkdir(
        # ..., exist_ok=False) retry loop is what actually closes the race (a
        # concurrent run could win between the probe and the claim). Prove the
        # retry loop itself recovers by pre-creating the run_dir the forced rid
        # would claim and calling cmd_init directly (bypassing the subprocess
        # boundary so run_id_new can be monkeypatched deterministically).
        spec = importlib.util.spec_from_file_location("dp_driver_init_collision", DRIVER)
        drv = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(drv)

        proj = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, proj, ignore_errors=True)
        plan = proj / "plan.md"
        plan.write_text(plan_body(False), encoding="utf-8")
        cfg_dir = proj / ".dev-pipeline"
        cfg_dir.mkdir(parents=True)
        cfg_path = cfg_dir / "dev-pipeline.config.json"
        cfg_path.write_text(json.dumps(valid_config()), encoding="utf-8")

        fixed_rid = "20260101-000000"
        # Simulate a run that already claimed the rid run_id_new() is about to
        # return (the exact race window reserve_run_id's probe cannot close).
        (proj / ".dev-pipeline" / "runs" / fixed_rid).mkdir(parents=True)

        orig = drv.run_id_new
        drv.run_id_new = lambda: fixed_rid
        try:
            args = types.SimpleNamespace(plan=str(plan), config=str(cfg_path),
                                         project=str(proj), worktree=False)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                drv.cmd_init(args)
            result = json.loads(buf.getvalue())
        finally:
            drv.run_id_new = orig

        self.assertEqual(result["run_id"], f"{fixed_rid}-2")
        self.assertTrue(pathlib.Path(result["run_dir"]).is_dir())

    def test_init_worktree_latest_dir_conflict_leaves_no_orphaned_worktree(self):
        # A worktree is created BEFORE run_dir in cmd_init; if a later,
        # deterministic precondition (the `latest`-is-a-directory check) then
        # fails, the worktree/branch it already created must not be left orphaned
        # with no state.json ever pointing at them (cleanup-worktree could never
        # find them again).
        p = self._git_pipeline()
        latest_dir = p.project / ".dev-pipeline" / "latest"
        latest_dir.mkdir(parents=True)
        (latest_dir / "keep.txt").write_text("do not delete me", encoding="utf-8")

        proc = p.init(worktree=True, expect_success=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse((p.project / ".dev-pipeline" / "worktrees").exists(),
                         "a worktree was left behind after init failed")
        self.assertFalse((p.project / ".dev-pipeline" / "runs").exists())
        # The pre-existing (unrelated) directory must survive untouched.
        self.assertTrue((latest_dir / "keep.txt").exists())
        wt_list = subprocess.run(["git", "-C", str(p.project), "worktree", "list"],
                                 capture_output=True, text=True, check=True).stdout
        self.assertNotIn("worktrees", wt_list)

    def test_init_worktree_in_repo_subdirectory_resolves_work_root_to_subdir(self):
        # `git worktree add` checks out the WHOLE repo, not just project_dir's
        # subtree — when project_dir is a strict subdirectory of a larger repo,
        # work_root must be adjusted to the matching subdirectory inside the new
        # checkout, or every downstream path (runner cwd, test_paths globs, delta
        # computation) resolves against the wrong directory.
        repo = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
        sub = repo / "sub" / "project"
        sub.mkdir(parents=True)
        (sub / "marker.txt").write_text("hi\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "initial"], check=True)

        cfg_dir = sub / ".dev-pipeline"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        cfg_path = cfg_dir / "dev-pipeline.config.json"
        cfg_path.write_text(json.dumps(valid_config()), encoding="utf-8")
        plan = sub / "plan.md"
        plan.write_text(plan_body(False), encoding="utf-8")

        r = run_driver("init", "--plan", str(plan), "--config", str(cfg_path),
                       "--project", str(sub), "--worktree")
        self.assertEqual(r.returncode, 0, r.stderr)
        work_root = pathlib.Path(r.json["work_root"])
        self.assertTrue(
            (work_root / "marker.txt").exists(),
            f"work_root {work_root} does not contain the subproject's committed "
            "files — it likely points at the worktree checkout's repo root "
            "instead of the matching subdirectory")


class TestValidateConfig(PipelineTestCase):
    def _write_config(self, cfg):
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(cfg, tmp)
        tmp.close()
        return tmp.name

    def test_valid_config_passes(self):
        path = self._write_config(valid_config())
        r = run_driver("validate-config", "--config", path)
        self.assertEqual(r.returncode, 0)
        self.assertTrue(r.json["valid"])

    def test_placeholder_tester_instruction_rejected(self):
        cfg = valid_config()
        cfg["llm"]["tester"]["build_instruction"] = "<REQUIRED: build command>"
        path = self._write_config(cfg)
        r = run_driver("validate-config", "--config", path)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("placeholder", r.stderr)

    def test_empty_tester_instruction_rejected(self):
        cfg = valid_config()
        cfg["llm"]["tester"]["test_instruction"] = "   "
        path = self._write_config(cfg)
        r = run_driver("validate-config", "--config", path)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("non-empty string", r.stderr)

    def test_unknown_review_block_severity_rejected(self):
        cfg = valid_config(review_block_severity=["showstopper"])
        path = self._write_config(cfg)
        r = run_driver("validate-config", "--config", path)
        self.assertNotEqual(r.returncode, 0)
        # Caught either by schema enum or the extra business rule.
        self.assertTrue(
            "review_block_severity" in r.stderr or "showstopper" in r.stderr)


class TestValidateResult(PipelineTestCase):
    def _write(self, obj):
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(obj, tmp)
        tmp.close()
        return tmp.name

    def test_valid_test_result_passes(self):
        path = self._write(test_result(status="pass"))
        r = run_driver("validate-result", "--type", "test", "--file", path)
        self.assertEqual(r.returncode, 0)
        self.assertTrue(r.json["valid"])

    def test_invalid_test_result_fails(self):
        path = self._write({"status": "pass"})  # missing required fields
        r = run_driver("validate-result", "--type", "test", "--file", path)
        self.assertNotEqual(r.returncode, 0)

    def test_skipped_stage_null_command_passes(self):
        # A skipped stage genuinely ran no command; the tester emits command:null
        # (like exit_code:null), which must validate.
        tr = test_result(status="pass")
        tr["stages"].append({"name": "build", "command": None, "exit_code": None,
                             "status": "skipped", "summary": "no build step"})
        r = run_driver("validate-result", "--type", "test", "--file", self._write(tr))
        self.assertEqual(r.returncode, 0)
        self.assertTrue(r.json["valid"])

    def test_valid_review_result_passes(self):
        path = self._write(review_result(verdict="approve"))
        r = run_driver("validate-result", "--type", "review", "--file", path)
        self.assertEqual(r.returncode, 0)
        self.assertTrue(r.json["valid"])

    def test_invalid_review_result_fails(self):
        path = self._write({"verdict": "approve"})  # missing required fields
        r = run_driver("validate-result", "--type", "review", "--file", path)
        self.assertNotEqual(r.returncode, 0)

    # implementor/test_implementor: added when the SCHEMA_BY_TYPE ternary->dict
    # fix landed (a 2-way ternary would have silently routed a third --type
    # value into review-result.schema.json — verified this does NOT happen).
    def test_valid_implementor_result_passes(self):
        path = self._write({"status": "implemented", "summary": "done", "concern": None})
        r = run_driver("validate-result", "--type", "implementor", "--file", path)
        self.assertEqual(r.returncode, 0)
        self.assertTrue(r.json["valid"])

    def test_implementor_result_rejects_assumptions_field(self):
        # `assumptions` was removed (7.2.0, dead field — never consumed anywhere);
        # additionalProperties:false must reject a result that still carries it.
        path = self._write({"status": "implemented", "summary": "done", "assumptions": []})
        r = run_driver("validate-result", "--type", "implementor", "--file", path)
        self.assertNotEqual(r.returncode, 0)

    def test_valid_implementor_blocked_result_passes(self):
        path = self._write({"status": "blocked", "summary": "cannot proceed",
                            "concern": "contract contradicts itself on X",
                            "blocked_on": "contract"})
        r = run_driver("validate-result", "--type", "implementor", "--file", path)
        self.assertEqual(r.returncode, 0)
        self.assertTrue(r.json["valid"])

    def test_blocked_result_missing_blocked_on_fails(self):
        # 7.2.0: blocked_on is now required (no implicit "contract" default)
        # whenever status is "blocked".
        path = self._write({"status": "blocked", "summary": "cannot proceed",
                            "concern": "contract contradicts itself on X"})
        r = run_driver("validate-result", "--type", "implementor", "--file", path)
        self.assertNotEqual(r.returncode, 0)

    def test_blocked_result_null_concern_fails(self):
        # 7.2.0: concern must be non-null/non-empty whenever status is "blocked"
        # (previously documented but not schema-enforced).
        path = self._write({"status": "blocked", "summary": "cannot proceed",
                            "concern": None, "blocked_on": "contract"})
        r = run_driver("validate-result", "--type", "implementor", "--file", path)
        self.assertNotEqual(r.returncode, 0)

    def test_blocked_result_missing_concern_fails(self):
        path = self._write({"status": "blocked", "summary": "cannot proceed",
                            "blocked_on": "contract"})
        r = run_driver("validate-result", "--type", "implementor", "--file", path)
        self.assertNotEqual(r.returncode, 0)

    def test_implemented_result_blocked_on_not_required(self):
        # The if/then only fires when status is "blocked" — an "implemented"
        # result needs no blocked_on at all.
        path = self._write({"status": "implemented", "summary": "done"})
        r = run_driver("validate-result", "--type", "implementor", "--file", path)
        self.assertEqual(r.returncode, 0)
        self.assertTrue(r.json["valid"])

    def test_invalid_implementor_result_fails(self):
        path = self._write({"status": "confused"})  # bad enum + missing summary
        r = run_driver("validate-result", "--type", "implementor", "--file", path)
        self.assertNotEqual(r.returncode, 0)

    def test_valid_test_implementor_result_passes(self):
        # test_implementor shares implementor-result.schema.json — confirm the
        # shared mapping actually resolves to a working schema for this type too.
        path = self._write({"status": "implemented", "summary": "tests written"})
        r = run_driver("validate-result", "--type", "test_implementor", "--file", path)
        self.assertEqual(r.returncode, 0)
        self.assertTrue(r.json["valid"])

    def test_invalid_test_implementor_result_fails(self):
        path = self._write({"status": "blocked"})  # missing summary
        r = run_driver("validate-result", "--type", "test_implementor", "--file", path)
        self.assertNotEqual(r.returncode, 0)

    def test_implementor_result_tolerates_markdown_fence(self):
        # A model may fence its status JSON despite the "do not fence" prompt
        # directive — validate-result must not silently treat a deliberate
        # `blocked` signal as absent just because of a stray ```json fence.
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8")
        tmp.write('```json\n{"status": "blocked", "summary": "cannot proceed", '
                  '"concern": "x", "blocked_on": "contract"}\n```\n')
        tmp.close()
        r = run_driver("validate-result", "--type", "implementor", "--file", tmp.name)
        self.assertEqual(r.returncode, 0)
        self.assertTrue(r.json["valid"])


class TestAttemptAutoRecord(PipelineTestCase):
    """6.0.0: `advance` records the retry context to attempts.md itself when it
    routes a failure back to a retry — no separate append-attempt step to forget."""

    def _to_review(self, p):
        self.to_test_state(p)          # init -> implementation -> test
        p.write_test_result(status="pass")
        r = p.advance()                # test -> review
        self.assertEqual(r.json["next_state"], "review")

    def test_test_failure_records_attempt(self):
        p = self.started()             # legacy flow (tdd off)
        self.to_test_state(p)
        attempts = p.run_dir / "attempts.md"
        self.assertIn("_No attempts recorded yet._", attempts.read_text(encoding="utf-8"))
        p.write_test_result(status="fail", failure_type="code",
                            failure_details="assertion X failed",
                            log_excerpt="E   AssertionError: nope")
        r = p.advance()                # test -> implementation (retry)
        self.assertEqual(r.json["next_state"], "implementation")
        body = attempts.read_text(encoding="utf-8")
        self.assertNotIn("_No attempts recorded yet._", body)
        self.assertIn("assertion X failed", body)
        self.assertIn("E   AssertionError", body)
        self.assertIn("state=test", body)

    def test_test_pass_records_nothing(self):
        p = self.started()
        self.to_test_state(p)
        p.write_test_result(status="pass")
        r = p.advance()                # test -> review
        self.assertEqual(r.json["next_state"], "review")
        self.assertIn("_No attempts recorded yet._",
                      (p.run_dir / "attempts.md").read_text(encoding="utf-8"))

    def test_review_failure_records_findings(self):
        p = self.started()
        self._to_review(p)
        p.write_review_result(verdict="needs-attention",
                             findings=[finding(severity="critical", title="bug in foo",
                                               file="src/a.py")])
        r = p.advance()                # review -> implementation (retry)
        self.assertEqual(r.json["next_state"], "implementation")
        body = (p.run_dir / "attempts.md").read_text(encoding="utf-8")
        self.assertIn("bug in foo", body)
        self.assertIn("src/a.py", body)
        self.assertIn("state=review", body)

    def test_red_not_confirmed_records_vacuous_note(self):
        p = self.make_pipeline(tdd_mode=True)
        p.init()
        self.assertEqual(p.advance().json["next_state"], "test_implementation")
        p.write_test_implementor_result(status="implemented")
        self.assertEqual(p.advance().json["next_state"], "red_test")
        p.write_red_test_result(status="pass")   # RED not confirmed (vacuous tests)
        r = p.advance()                            # red_test -> test_implementation (re-author)
        self.assertEqual(r.json["next_state"], "test_implementation")
        body = (p.run_dir / "attempts.md").read_text(encoding="utf-8")
        self.assertIn("vacuous", body)
        self.assertIn("state=red_test", body)


class TestResume(PipelineTestCase):
    """`driver resume` re-emits the current state's landing echo so an interrupted
    run continues without a new init or redoing completed stages (6.1.0)."""

    def _resume(self, p):
        return run_driver("resume", "--run", str(p.run_dir))

    def test_advance_persists_last_advance(self):
        # Every transition writes last-advance.json, matching the current state.
        p = self.started(tdd_mode=False)
        r = p.advance()  # init -> implementation
        la = p.run_dir / "last-advance.json"
        self.assertTrue(la.exists())
        echo = json.loads(la.read_text(encoding="utf-8"))
        self.assertEqual(echo["next_state"], r.json["next_state"])
        self.assertEqual(echo["next_state"], "implementation")

    def test_resume_replays_landing_echo(self):
        p = self.started(tdd_mode=False)
        adv = p.advance()  # -> implementation
        before = json.loads((p.run_dir / "state.json").read_text(encoding="utf-8"))
        res = self._resume(p)
        self.assertEqual(res.returncode, 0, res.stderr)
        # same landing echo, incl. the runner array the SKILL replays
        self.assertEqual(res.json["next_state"], "implementation")
        self.assertEqual(res.json["directive"], adv.json["directive"])
        self.assertEqual(res.json["implementor_runners"], adv.json["implementor_runners"])
        self.assertTrue(res.json["resumed"])
        # context restored from state.json
        self.assertTrue(res.json["project_dir"])
        self.assertTrue(res.json["contract_path"])
        # the driver made NO transition
        after = json.loads((p.run_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(after["state"], before["state"])
        self.assertEqual(after["iterations"], before["iterations"])
        self.assertEqual(len(after["history"]), len(before["history"]))

    def test_resume_at_init(self):
        # Parked at init (no stage run yet) → tell the SKILL to just advance.
        p = self.started(tdd_mode=False)
        res = self._resume(p)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.json["next_state"], "init")
        self.assertEqual(res.json["directive"], "advance")

    def test_resume_summary_surfaces_in_output(self):
        # 7.1.0: --summary is surfaced to the resuming orchestrator as task_summary.
        p = self.started(tdd_mode=False)
        p.advance()  # -> implementation
        res = run_driver("resume", "--run", str(p.run_dir),
                         "--summary", "wip: added parse(), next: empty-input error path")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.json["task_summary"],
                         "wip: added parse(), next: empty-input error path")

    def test_resume_summary_file(self):
        # --summary-file reads the summary from a file (for long text).
        p = self.started(tdd_mode=False)
        p.advance()  # -> implementation
        sf = p.run_dir / "handoff.md"
        sf.write_text("multi\nline\nsummary", encoding="utf-8")
        res = run_driver("resume", "--run", str(p.run_dir), "--summary-file", str(sf))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.json["task_summary"], "multi\nline\nsummary")

    def test_resume_bare_has_no_task_summary(self):
        # A bare resume (no flag) is byte-for-byte unchanged — no task_summary key.
        p = self.started(tdd_mode=False)
        p.advance()  # -> implementation
        res = self._resume(p)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("task_summary", res.json)

    def test_resume_summary_flags_mutually_exclusive(self):
        # --summary and --summary-file cannot be combined (argparse error).
        p = self.started(tdd_mode=False)
        p.advance()
        res = run_driver("resume", "--run", str(p.run_dir),
                         "--summary", "a", "--summary-file", "b")
        self.assertNotEqual(res.returncode, 0)

    def test_resume_summary_file_missing_dies_cleanly(self):
        # A nonexistent --summary-file path dies with a clear message (OSError catch),
        # not a raw traceback.
        p = self.started(tdd_mode=False)
        p.advance()
        res = run_driver("resume", "--run", str(p.run_dir),
                         "--summary-file", str(p.run_dir / "nope.md"))
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("could not be read", res.stderr)

    def test_resume_empty_summary_is_surfaced(self):
        # An empty inline summary is still a supplied value (not None), so it is
        # surfaced as an empty task_summary rather than omitted — harmless handoff.
        p = self.started(tdd_mode=False)
        p.advance()
        res = run_driver("resume", "--run", str(p.run_dir), "--summary", "")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.json["task_summary"], "")

    def test_resume_summary_does_not_leak_into_stage_input(self):
        # task_summary is orchestrator-only: it rides in ctx (merged after
        # build_stage_input runs on the pristine echo), so a role's stage-input
        # written by resume must NOT contain it.
        p = self.started(tdd_mode=False)
        p.advance()  # -> implementation (a role-bearing state)
        res = run_driver("resume", "--run", str(p.run_dir), "--summary", "SECRET-HANDOFF")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.json["task_summary"], "SECRET-HANDOFF")
        si = json.loads((p._current_iter_dir() / "stage-input.json").read_text(encoding="utf-8"))
        self.assertNotIn("task_summary", si.get("inputs", {}))
        self.assertNotIn("SECRET-HANDOFF", json.dumps(si))

    def test_resume_stale_window_reruns_advance(self):
        # A REAL crash window — advance wrote last-advance.json, then died before
        # save_state. Reconstruct it by snapshotting state.json before an advance and
        # restoring it after; resume must re-run advance (not die), and re-running it
        # must converge exactly (no doubled history).
        p = self.started(tdd_mode=False)
        p.advance()  # init -> implementation
        window = (p.run_dir / "state.json").read_bytes()  # state == implementation
        p.write_implementor_result(status="implemented")
        p.advance()  # implementation -> test; writes last-advance {prev:impl, next:test}
        (p.run_dir / "state.json").write_bytes(window)     # roll state.json back to the window
        res = self._resume(p)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.json["next_state"], "implementation")
        self.assertEqual(res.json["directive"], "advance")   # re-run advance, not a die
        self.assertIn("resume_note", res.json)
        # convergence: re-running advance reproduces the clean run exactly.
        adv = p.advance()
        self.assertEqual(adv.json["next_state"], "test")
        st = json.loads((p.run_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(st["state"], "test")
        self.assertEqual([h["state"] for h in st["history"]],
                         ["init", "init", "implementation"])

    def test_resume_replays_tdd_state(self):
        # A TDD run replays a JSON-role echo (red_test) with its tester runners.
        p = self.started(tdd_mode=True)
        p.advance()        # init -> test_implementation
        p.write_test_implementor_result(status="implemented")
        adv = p.advance()  # test_implementation -> red_test
        self.assertEqual(adv.json["next_state"], "red_test")
        res = self._resume(p)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.json["next_state"], "red_test")
        self.assertEqual(res.json["directive"], adv.json["directive"])
        self.assertTrue(res.json["tdd_mode"])
        self.assertEqual(res.json.get("result_filename"), adv.json.get("result_filename"))
        self.assertEqual(res.json.get("tester_runners"), adv.json.get("tester_runners"))

    def test_resume_possibly_live_flag(self):
        # A freshly-written run is flagged possibly_live; an ancient one is not.
        # Also guards the fromisoformat "Z" parse (broken on 3.9/3.10 without the
        # normalize) — if the parse silently failed, the first assert would fail.
        p = self.started(tdd_mode=False)
        p.advance()  # implementation; updated_at just written -> within the live window
        self.assertTrue(self._resume(p).json.get("possibly_live"))
        st = json.loads((p.run_dir / "state.json").read_text(encoding="utf-8"))
        st["updated_at"] = "2000-01-01T00:00:00Z"  # ancient
        (p.run_dir / "state.json").write_text(json.dumps(st), encoding="utf-8")
        self.assertNotIn("possibly_live", self._resume(p).json)

    def test_resume_terminal_replays_full_echo(self):
        # A run that reached done replays its FULL landing echo (with
        # run_self_evolution), not a gutted "nothing to resume".
        p = self.started(tdd_mode=False, run_self_evolution=True)
        p.advance()  # implementation
        p.write_implementor_result(status="implemented")
        p.advance()  # test
        p.write_test_result(status="pass")
        p.advance()  # review
        p.write_review_result(verdict="approve")
        done = p.advance()  # done
        self.assertEqual(done.json["next_state"], "done")
        res = self._resume(p)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.json["next_state"], "done")
        self.assertIn("run_self_evolution", res.json)
        self.assertTrue(res.json["resumed"])

    def test_resume_stage_input_byte_equal(self):
        # The re-persisted stage-input.json is byte-identical to the original
        # (resume metadata never leaks into it).
        p = self.started(tdd_mode=False)
        p.advance()  # implementation writes iterations/0/stage-input.json
        si = p.run_dir / "iterations" / "0" / "stage-input.json"
        self.assertTrue(si.exists())
        before = si.read_bytes()
        res = self._resume(p)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(si.read_bytes(), before)
        self.assertNotIn("resumed", json.loads(si.read_text(encoding="utf-8")).get("inputs", {}))

    def test_resume_missing_record_errors(self):
        # A run with no last-advance.json gets a precise manual recipe (incl.
        # record-changes), not a crash.
        p = self.started(tdd_mode=False)
        p.advance()  # implementation
        (p.run_dir / "last-advance.json").unlink()
        res = self._resume(p)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("record-changes", res.stderr)
        self.assertIn("predates resume", res.stderr)

    def test_resume_missing_run_errors(self):
        # A not-a-run path fails cleanly with guidance.
        res = run_driver("resume", "--run", "/nonexistent/run-dir")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Not a resumable run", res.stderr)

    def test_save_json_atomic_leaves_no_tmp(self):
        # Atomic writes must not leave .tmp files behind in the run dir.
        p = self.started(tdd_mode=False)
        p.advance()
        tmps = list(p.run_dir.rglob("*.tmp"))
        self.assertEqual(tmps, [], f"stray temp files: {tmps}")

    def test_resume_stale_window_failure_transition_converges(self):
        # The risky crash window is a FAILURE transition (test-fail -> implementation):
        # the counter is incremented and attempts.md appended before transition()
        # persists anything. A crash-window re-run must converge the counter EXACTLY
        # (the crashed increment was never saved to state.json), with at most a
        # duplicated attempts line — pins the idempotency the "accepted limits" claims.
        p = self.started(tdd_mode=False)
        p.advance()  # init -> implementation
        p.write_implementor_result(status="implemented")
        p.advance()  # implementation -> test
        p.write_test_result(status="fail", failure_type="code",
                            failure_details="boom", log_excerpt="E boom")
        window = (p.run_dir / "state.json").read_bytes()  # state==test, iterations.test==0
        p.advance()  # test -> implementation; iterations.test->1, appends attempt
        (p.run_dir / "state.json").write_bytes(window)     # roll back to the crash window
        res = self._resume(p)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.json["next_state"], "test")
        self.assertEqual(res.json["directive"], "advance")  # crash window -> re-run advance
        adv = p.advance()                                    # re-run the failure transition
        self.assertEqual(adv.json["next_state"], "implementation")
        st = json.loads((p.run_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(st["iterations"]["test"], 1)        # converged, NOT 2
        self.assertEqual([h["state"] for h in st["history"]],
                         ["init", "init", "implementation", "test"])
        self.assertIn("boom", (p.run_dir / "attempts.md").read_text(encoding="utf-8"))

    def test_resume_corrupt_last_advance_falls_back_to_recipe(self):
        # An externally corrupted last-advance.json falls back to the manual recipe,
        # not a raw JSONDecodeError.
        p = self.started(tdd_mode=False)
        p.advance()  # implementation
        (p.run_dir / "last-advance.json").write_text("{ not json", encoding="utf-8")
        res = self._resume(p)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("record-changes", res.stderr)   # the recipe
        self.assertNotIn("Invalid JSON", res.stderr)  # not the raw parse error

    def test_resume_missing_snapshot_still_resumes(self):
        # A missing config.snapshot.json must not make the live-window heuristic die —
        # resume still reaches its coherent branches.
        p = self.started(tdd_mode=False)
        p.advance()  # implementation
        (p.run_dir / "config.snapshot.json").unlink()
        res = self._resume(p)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.json["next_state"], "implementation")
        self.assertNotIn("config.snapshot", res.stderr)


class TestBootstrapConfig(unittest.TestCase):
    """bootstrap-config seeds the config from the template deterministically."""

    def _tmp_project(self, git=False):
        d = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        if git:
            subprocess.run(["git", "init", "-q", str(d)], check=True)
        return d

    def test_creates_config_in_non_git_dir(self):
        proj = self._tmp_project(git=False)
        r = run_driver("bootstrap-config", "--project", str(proj))
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.json["status"], "created")
        self.assertFalse(r.json["runners_configured"])
        config = proj / ".dev-pipeline" / "dev-pipeline.config.json"
        self.assertTrue(config.exists())
        # No git repo → .gitignore is not touched.
        self.assertFalse(r.json["gitignore_updated"])
        self.assertFalse((proj / ".gitignore").exists())
        self.assertEqual(
            r.json["required_fields"],
            ["llm.tester.build_instruction",
             "llm.tester.install_instruction",
             "llm.tester.test_instruction",
             "llm.test_implementor.framework_instruction",
             "llm.test_implementor.test_paths"],
        )
        # runners are seeded UNCONFIGURED — the --update-config flow (driver
        # apply-config) fills them in.
        self.assertFalse(r.json["config_complete"])
        cfg = json.loads(config.read_text(encoding="utf-8"))
        self.assertEqual(
            cfg["runners"],
            {role: [{"type": "unconfigured"}] for role in
             ("implementor", "test_implementor", "tester", "reviewer")},
        )

    def test_existing_config_not_overwritten(self):
        proj = self._tmp_project(git=False)
        run_driver("bootstrap-config", "--project", str(proj))
        config = proj / ".dev-pipeline" / "dev-pipeline.config.json"
        config.write_text('{"sentinel": true}', encoding="utf-8")

        r = run_driver("bootstrap-config", "--project", str(proj))
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.json["status"], "exists")
        # Content is preserved, not re-seeded from the template.
        self.assertEqual(json.loads(config.read_text(encoding="utf-8")),
                         {"sentinel": True})

    def test_gitignore_updated_in_git_repo(self):
        proj = self._tmp_project(git=True)
        r = run_driver("bootstrap-config", "--project", str(proj))
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.json["status"], "created")
        self.assertTrue(r.json["gitignore_updated"])
        gitignore = proj / ".gitignore"
        self.assertIn(".dev-pipeline/",
                      gitignore.read_text(encoding="utf-8").splitlines())

    def test_gitignore_entry_is_idempotent(self):
        proj = self._tmp_project(git=True)
        (proj / ".gitignore").write_text(".dev-pipeline/\n", encoding="utf-8")
        r = run_driver("bootstrap-config", "--project", str(proj))
        self.assertEqual(r.returncode, 0)
        # Already present → not added again.
        self.assertFalse(r.json["gitignore_updated"])
        occurrences = (proj / ".gitignore").read_text(
            encoding="utf-8").splitlines().count(".dev-pipeline/")
        self.assertEqual(occurrences, 1)

    def test_seeded_config_validates_after_apply_config(self):
        proj = self._tmp_project(git=False)
        run_driver("bootstrap-config", "--project", str(proj))
        config = proj / ".dev-pipeline" / "dev-pipeline.config.json"

        # The template ships with placeholder tester instructions AND unconfigured
        # runners, so it must NOT validate until --update-config fills them in.
        r_before = run_driver("validate-config", "--config", str(config))
        self.assertNotEqual(r_before.returncode, 0)
        self.assertIn("not configured yet", r_before.stderr)  # runners flagged first

        # A partial apply (instructions only) is still incomplete — runners remain
        # unconfigured, so the merged config is rejected and nothing is written.
        vf = proj / ".dev-pipeline" / "v.json"
        vf.write_text(json.dumps({"llm": {"tester": {
            "build_instruction": "no build step", "install_instruction": "no install step",
            "test_instruction": "pytest"}}}), encoding="utf-8")
        r_mid = run_driver("apply-config", "--config", str(config), "--values-file", str(vf))
        self.assertNotEqual(r_mid.returncode, 0)
        self.assertIn("not configured yet", r_mid.stderr)
        self.assertTrue(vf.exists())  # scratch kept on failure

        # A complete apply (instructions + test_implementor + runners) validates.
        vf.write_text(json.dumps({
            "driver": {"tdd_mode": False},
            "llm": {"tester": {"build_instruction": "no build step",
                               "install_instruction": "no install step",
                               "test_instruction": "pytest"}},
            "runners": {
                "implementor": [{"type": "bash", "command": "true"}],
                "test_implementor": [{"type": "bash", "command": "true"}],
                "tester": [{"type": "bash", "command": "true > {output_file}"}],
                "reviewer": [{"type": "bash", "command": "true > {output_file}"}],
            },
        }), encoding="utf-8")
        r_apply = run_driver("apply-config", "--config", str(config), "--values-file", str(vf))
        self.assertEqual(r_apply.returncode, 0, r_apply.stderr)
        self.assertTrue(r_apply.json["config_complete"])
        self.assertFalse(vf.exists())  # scratch file cleaned up on success

        r_after = run_driver("validate-config", "--config", str(config))
        self.assertEqual(r_after.returncode, 0, r_after.stderr)
        self.assertTrue(r_after.json["valid"])

    def test_init_on_unconfigured_config_fails_cleanly(self):
        proj = self._tmp_project(git=False)
        run_driver("bootstrap-config", "--project", str(proj))
        config = proj / ".dev-pipeline" / "dev-pipeline.config.json"
        plan = proj / "plan.md"
        plan.write_text("# P\n\n## Requirements\n- r\n\n## Acceptance Criteria\n- [ ] a\n",
                        encoding="utf-8")
        r = run_driver("init", "--plan", str(plan), "--config", str(config), "--project", str(proj))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not configured yet", r.stderr)
        # A rejected init must not create a run directory.
        self.assertFalse((proj / ".dev-pipeline" / "runs").exists())

    def test_exists_reports_incomplete_when_unconfigured(self):
        proj = self._tmp_project(git=False)
        run_driver("bootstrap-config", "--project", str(proj))
        r = run_driver("bootstrap-config", "--project", str(proj))  # second call → exists
        self.assertEqual(r.json["status"], "exists")
        self.assertFalse(r.json["runners_configured"])  # still unconfigured — setup resumable
        self.assertFalse(r.json["config_complete"])     # SKILL runs --update-config first

    def test_migrate_config_refuses_unconfigured(self):
        proj = self._tmp_project(git=False)
        run_driver("bootstrap-config", "--project", str(proj))
        config = proj / ".dev-pipeline" / "dev-pipeline.config.json"
        r = run_driver("migrate-config", "--config", str(config))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not the setup path", r.stderr)


class TestApplyConfig(unittest.TestCase):
    """`driver apply-config` — the --update-config write path: deep-merge a partial
    values file into config.json, validate the merged result, write atomically, and
    stay re-runnable (config only ever changes here)."""

    # A complete set of values that makes a freshly bootstrapped config valid
    # (tdd off keeps test_implementor optional; runners exercise all three modes).
    FULL_VALUES = {
        "driver": {"tdd_mode": False},
        "llm": {"tester": {"build_instruction": "no build step",
                           "install_instruction": "no install step",
                           "test_instruction": "pytest"}},
        "runners": {
            "implementor": [{"type": "bash", "command": "true"}],
            "test_implementor": [{"type": "subagent", "model": "sonnet"}],
            "tester": [{"type": "main-session"}],
            "reviewer": [{"type": "bash", "command": "true > {output_file}", "normalizer": "default"}],
        },
    }

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.proj = pathlib.Path(self._tmp.name)
        run_driver("bootstrap-config", "--project", str(self.proj))
        self.config = self.proj / ".dev-pipeline" / "dev-pipeline.config.json"
        self.vf = self.proj / ".dev-pipeline" / "v.json"

    def tearDown(self):
        self._tmp.cleanup()

    def _apply(self, values, config=None):
        self.vf.write_text(json.dumps(values), encoding="utf-8")
        return run_driver("apply-config", "--config", str(config or self.config),
                          "--values-file", str(self.vf))

    def test_success_writes_and_cleans_up(self):
        r = self._apply(self.FULL_VALUES)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.json["ok"])
        self.assertTrue(r.json["config_complete"])
        self.assertFalse(self.vf.exists())  # scratch file removed on success
        cfg = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(cfg["runners"], self.FULL_VALUES["runners"])
        self.assertEqual(cfg["llm"]["tester"]["test_instruction"], "pytest")

    def test_re_runnable(self):
        # Unlike the removed one-time set-runners, apply-config may run again — it is
        # the conservative single point where config changes.
        r1 = self._apply(self.FULL_VALUES)
        self.assertEqual(r1.returncode, 0, r1.stderr)
        r2 = self._apply({"driver": {"max_test_iteration": 5}})
        self.assertEqual(r2.returncode, 0, r2.stderr)
        cfg = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(cfg["driver"]["max_test_iteration"], 5)

    def test_partial_merge_preserves_untouched_siblings(self):
        self._apply(self.FULL_VALUES)
        # A prose-only follow-up must not disturb runners or tester instructions.
        r = self._apply({"llm": {"implementor": {"design_instruction": "small units"}}})
        self.assertEqual(r.returncode, 0, r.stderr)
        cfg = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(cfg["llm"]["implementor"]["design_instruction"], "small units")
        self.assertEqual(cfg["llm"]["tester"]["test_instruction"], "pytest")  # preserved
        self.assertEqual(cfg["runners"]["tester"], [{"type": "main-session"}])  # preserved

    def test_seeds_when_config_absent(self):
        fresh = pathlib.Path(self._tmp.name) / "sub" / ".dev-pipeline" / "dev-pipeline.config.json"
        r = self._apply(self.FULL_VALUES, config=fresh)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.json["seeded"])
        self.assertTrue(fresh.exists())

    def test_rejects_incomplete_merge_nothing_written(self):
        # runners only, onto the placeholder tester instructions → merged config is
        # incomplete → rejected, config untouched, scratch kept.
        before = self.config.read_text(encoding="utf-8")
        r = self._apply({"runners": self.FULL_VALUES["runners"]})
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.config.read_text(encoding="utf-8"), before)
        self.assertTrue(self.vf.exists())  # kept on failure

    def test_rejects_placeholder_value(self):
        before = self.config.read_text(encoding="utf-8")
        r = self._apply({**self.FULL_VALUES,
                         "llm": {"tester": {"build_instruction": "no build step",
                                            "install_instruction": "no install step",
                                            "test_instruction": "<REQUIRED: cmd>"}}})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("placeholder", r.stderr)
        self.assertEqual(self.config.read_text(encoding="utf-8"), before)

    def test_rejects_unexpected_top_level_key(self):
        before = self.config.read_text(encoding="utf-8")
        r = self._apply({**self.FULL_VALUES, "bogus": 1})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("unexpected top-level key", r.stderr)
        self.assertEqual(self.config.read_text(encoding="utf-8"), before)

    def test_rejects_bad_runner_shape(self):
        before = self.config.read_text(encoding="utf-8")
        vals = json.loads(json.dumps(self.FULL_VALUES))
        vals["runners"]["implementor"] = [{"type": "bash"}]  # missing command
        r = self._apply(vals)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("bash runner requires", r.stderr)
        self.assertEqual(self.config.read_text(encoding="utf-8"), before)

    def test_rejects_subagent_with_command(self):
        vals = json.loads(json.dumps(self.FULL_VALUES))
        vals["runners"]["implementor"] = [{"type": "subagent", "command": "x"}]
        r = self._apply(vals)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("must not have a `command`", r.stderr)

    def test_rejects_mixed_array(self):
        vals = json.loads(json.dumps(self.FULL_VALUES))
        vals["runners"]["implementor"] = [{"type": "bash", "command": "x"}, {"type": "subagent"}]
        r = self._apply(vals)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("mixed runner types", r.stderr)

    def test_rejects_legacy_type_with_actionable_message(self):
        vals = json.loads(json.dumps(self.FULL_VALUES))
        vals["runners"]["implementor"] = [{"type": "claude-subagent", "agent": "x"}]
        r = self._apply(vals)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("3.0.0", r.stderr)

    def test_rejects_bool_timeout(self):
        # bool is a subclass of int in Python; a naive schema check would accept
        # timeout:true (→ a 1-second timeout at run time). It must be rejected.
        vals = json.loads(json.dumps(self.FULL_VALUES))
        vals["runners"]["implementor"] = [{"type": "bash", "command": "x", "timeout": True}]
        r = self._apply(vals)
        self.assertNotEqual(r.returncode, 0)

    def test_values_file_equal_config_is_not_deleted(self):
        # A full config.json is itself a valid values file, so `--values-file` may
        # point at the config. apply-config must NOT unlink the config it just wrote.
        self._apply(self.FULL_VALUES)              # write a complete config
        before = self.config.read_text(encoding="utf-8")
        r = run_driver("apply-config", "--config", str(self.config),
                       "--values-file", str(self.config))  # values == config
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.config.exists())      # not deleted
        self.assertEqual(self.config.read_text(encoding="utf-8"), before)

    def test_seed_failure_writes_no_config(self):
        # An absent config + an invalid (incomplete) values file must leave NOTHING
        # on disk — no half-seeded config — so "nothing was written" stays honest.
        fresh = pathlib.Path(self._tmp.name) / "sub2" / ".dev-pipeline" / "dev-pipeline.config.json"
        r = self._apply({"driver": {"tdd_mode": False}}, config=fresh)  # no runners/instructions
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(fresh.exists())


class TestTDD(PipelineTestCase):
    """TDD flow: init → test_implementation → red_test → implementation → test → review."""

    def started_tdd(self, **driver_overrides):
        driver_overrides.setdefault("tdd_mode", True)
        p = self.make_pipeline(**driver_overrides)
        p.init()  # config tdd_mode=True → init writes a TDD contract (incl. Interface)
        return p

    def _to_red_test(self, p):
        r1 = p.advance()  # init -> test_implementation
        self.assertEqual(r1.json["next_state"], "test_implementation")
        self.assertEqual(r1.json["directive"], "run_test_implementor")
        p.write_test_implementor_result(status="implemented")
        r2 = p.advance()  # test_implementation -> red_test
        self.assertEqual(r2.json["next_state"], "red_test")
        self.assertEqual(r2.json["directive"], "run_tester")
        self.assertEqual(r2.json["result_filename"], "red-test-result.json")
        return r2

    def _to_review(self, p):
        self._to_red_test(p)
        p.write_red_test_result(status="fail", failure_type="code")
        self.assertEqual(p.advance().json["next_state"], "implementation")  # RED confirmed
        p.write_implementor_result(status="implemented")
        self.assertEqual(p.advance().json["next_state"], "test")
        p.write_test_result(status="pass")
        r = p.advance()
        self.assertEqual(r.json["next_state"], "review")
        return r

    def test_full_tdd_run_to_done(self):
        p = self.started_tdd()
        self._to_review(p)
        p.write_review_result(verdict="approve")
        r = p.advance()
        self.assertEqual(r.json["next_state"], "done")
        self.assertEqual(r.json["directive"], "finalize")

    def test_red_confirmed_clears_red_phase(self):
        p = self.started_tdd()
        self._to_red_test(p)
        p.write_red_test_result(status="fail", failure_type="code")
        p.advance()  # red_test -> implementation, red_phase -> false
        state = json.loads((p.run_dir / "state.json").read_text())
        self.assertFalse(state["red_phase"])

    def test_red_not_confirmed_retries_then_exhausts(self):
        p = self.started_tdd(max_test_implementation_iteration=1)
        self._to_red_test(p)

        # Tests passed with no implementation -> RED not confirmed -> re-author.
        p.write_red_test_result(status="pass")
        r = p.advance()
        self.assertEqual(r.json["next_state"], "test_implementation")
        self.assertEqual(r.json["iterations"]["test_implementation"], 1)

        # test_implementation -> red_test again, fail to confirm RED -> exhaust.
        p.write_test_implementor_result(status="implemented")
        self.assertEqual(p.advance().json["next_state"], "red_test")
        p.write_red_test_result(status="pass")
        r = p.advance()
        self.assertEqual(r.json["next_state"], "failed")
        self.assertEqual(r.json["halt_reason"], "iteration-exhausted")
        self.assertEqual(r.json["iterations"]["test_implementation"], 2)

    def test_red_test_environment_halts(self):
        p = self.started_tdd()
        self._to_red_test(p)
        p.write_red_test_result(status="fail", failure_type="environment")
        r = p.advance()
        self.assertEqual(r.json["next_state"], "failed")
        self.assertEqual(r.json["halt_reason"], "environment")
        self.assertEqual(r.json["directive"], "halt_and_ask")

    def test_iteration_dir_includes_test_implementation_counter(self):
        p = self.started_tdd()
        self._to_red_test(p)
        p.write_red_test_result(status="pass")  # RED not confirmed
        p.advance()  # -> test_implementation, test_implementation -> 1
        st = p.status()
        it = st["iterations"]
        n = it["test_implementation"] + it["test"] + it["review"]
        self.assertEqual(n, 1)
        self.assertTrue((p.run_dir / "iterations" / "1").is_dir())

    def test_review_test_finding_routes_to_test_implementation(self):
        p = self.started_tdd()
        self._to_review(p)
        p.write_review_result(verdict="needs-attention",
                              findings=[finding(severity="critical",
                                                file="tests/test_foo.py")])
        r = p.advance()
        self.assertEqual(r.json["next_state"], "test_implementation")
        self.assertEqual(r.json["directive"], "run_test_implementor")
        self.assertEqual(r.json["iterations"]["review"], 1)
        # repair: test author fixes the flagged test (status:"implemented") ->
        # test (green re-run, NOT implementation)
        p.write_test_implementor_result(status="implemented")
        r2 = p.advance()
        self.assertEqual(r2.json["next_state"], "test")
        self.assertEqual(r2.json["directive"], "run_tester")

    def test_review_test_finding_repair_converges_to_implementation(self):
        # review test-finding → test_implementation → test; if the green test then
        # fails (code), the normal test→implementation retry takes over.
        p = self.started_tdd()
        self._to_review(p)
        p.write_review_result(verdict="needs-attention",
                              findings=[finding(severity="critical",
                                                file="tests/test_foo.py")])
        self.assertEqual(p.advance().json["next_state"], "test_implementation")
        p.write_test_implementor_result(status="implemented")  # test author fixed the flagged test
        self.assertEqual(p.advance().json["next_state"], "test")  # repair: re-run GREEN
        # The tightened test now fails against the existing implementation.
        p.write_test_result(status="fail", failure_type="code")
        r = p.advance()
        self.assertEqual(r.json["next_state"], "implementation")
        self.assertEqual(r.json["directive"], "run_implementor")

    def test_review_test_finding_test_author_blocked_on_impl_routes_to_implementation(self):
        # 6.8.0: a review test-finding routes to test_implementation; the test
        # author verifies its tests correct and reports blocked_on:"implementation"
        # -> the driver reroutes to the implementor (NOT back to the tester).
        p = self.started_tdd()
        self._to_review(p)
        p.write_review_result(verdict="needs-attention",
                              findings=[finding(severity="critical",
                                                file="tests/test_foo.py")])
        self.assertEqual(p.advance().json["next_state"], "test_implementation")
        p.write_test_implementor_result(status="blocked", blocked_on="implementation",
                                        concern="tests verified correct; the production code is the gap")
        r = p.advance()
        self.assertEqual(r.json["next_state"], "implementation")
        self.assertEqual(r.json["directive"], "run_implementor")
        # The disputed concern reaches the implementor via the echoed note.
        self.assertIn("production code is the gap", r.json["note"])
        # No counter bumped on this reroute (mirrors red_confirmed -> implementation).
        self.assertEqual(r.json["iterations"]["test_implementation"], 0)

    def test_repair_blocked_contract_stays_on_test(self):
        # A repair-pass blocked WITHOUT blocked_on:"implementation" (blocked_on:
        # "contract") is NOT the implementor's problem -> stays on `test`, exactly
        # as before 6.8.0. Documents the split.
        p = self.started_tdd()
        self._to_review(p)
        p.write_review_result(verdict="needs-attention",
                              findings=[finding(severity="critical",
                                                file="tests/test_foo.py")])
        self.assertEqual(p.advance().json["next_state"], "test_implementation")
        p.write_test_implementor_result(status="blocked", blocked_on="contract",
                                        concern="AC3 is untestable as specified")
        r = p.advance()
        self.assertEqual(r.json["next_state"], "test")
        self.assertEqual(r.json["directive"], "run_tester")

    def test_repair_pass_missing_test_implementor_result_dies(self):
        # 6.8.0: the repair branch now reads the (mandatory) result file too, so a
        # missing one dies with the pre-6.6.0 hand-fix hint.
        p = self.started_tdd()
        self._to_review(p)
        p.write_review_result(verdict="needs-attention",
                              findings=[finding(severity="critical",
                                                file="tests/test_foo.py")])
        self.assertEqual(p.advance().json["next_state"], "test_implementation")
        # No test_implementor-result.json written for the repair pass.
        r = p.advance()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("test_implementor-result.json not found", r.stderr)
        self.assertIn("by hand", r.stderr)

    def test_repair_pass_bogus_blocked_on_rejected(self):
        # 6.8.0: the shared schema's blocked_on enum now includes "implementation";
        # a value outside the enum is still rejected on the repair-pass read.
        p = self.started_tdd()
        self._to_review(p)
        p.write_review_result(verdict="needs-attention",
                              findings=[finding(severity="critical",
                                                file="tests/test_foo.py")])
        self.assertEqual(p.advance().json["next_state"], "test_implementation")
        p.write_raw_result_file(
            "test_implementor-result.json",
            json.dumps({"status": "blocked", "summary": "x",
                        "concern": "x", "blocked_on": "nonsense"}))
        r = p.advance()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("schema violation", r.stderr)

    def test_default_max_test_implementation_iteration_is_three(self):
        # When the key is omitted from config, the driver default applies.
        cfg = valid_config(tdd_mode=True)
        cfg["driver"].pop("max_test_implementation_iteration", None)
        p = Pipeline(cfg)
        self._pipelines.append(p)
        p.init()
        state = json.loads((p.run_dir / "state.json").read_text())
        self.assertEqual(state["max"]["test_implementation"], 3)

    def test_review_prod_finding_routes_to_implementation(self):
        p = self.started_tdd()
        self._to_review(p)
        p.write_review_result(verdict="needs-attention",
                              findings=[finding(severity="critical",
                                                file="src/foo.py")])
        r = p.advance()
        self.assertEqual(r.json["next_state"], "implementation")
        self.assertEqual(r.json["directive"], "run_implementor")

    def test_config_tdd_false_runs_legacy_flow(self):
        # tdd_mode is now sourced solely from config (the --tdd/--no-tdd flags were
        # removed in 5.0.0).
        p = self.make_pipeline(tdd_mode=False)
        p.init()
        r = p.advance()
        self.assertEqual(r.json["next_state"], "implementation")
        self.assertEqual(r.json["directive"], "run_implementor")

    def test_config_tdd_true_runs_tdd_flow(self):
        p = self.make_pipeline(tdd_mode=True)
        p.init()
        r = p.advance()
        self.assertEqual(r.json["next_state"], "test_implementation")


class TestImplementorBlockedRouting(PipelineTestCase):
    """6.6.0: `implementation`'s advance reads implementor-result.json (now
    mandatory) and routes status:"blocked"+blocked_on:"tests" (TDD only) to
    test_implementation instead of test."""

    def _to_implementation(self, p):
        """TDD: init -> test_implementation -> red_test -> implementation
        (RED confirmed)."""
        r1 = p.advance()  # init -> test_implementation
        self.assertEqual(r1.json["next_state"], "test_implementation")
        p.write_test_implementor_result(status="implemented")
        r2 = p.advance()  # test_implementation -> red_test
        self.assertEqual(r2.json["next_state"], "red_test")
        p.write_red_test_result(status="fail", failure_type="code")
        r3 = p.advance()  # red confirmed -> implementation
        self.assertEqual(r3.json["next_state"], "implementation")
        return r3

    def test_blocked_on_tests_routes_to_test_implementation(self):
        p = self.make_pipeline(tdd_mode=True)
        p.init()
        self._to_implementation(p)
        p.write_implementor_result(status="blocked", blocked_on="tests",
                                   concern="AC1's test asserts the wrong return value")
        r = p.advance()
        self.assertEqual(r.json["next_state"], "test_implementation")
        self.assertEqual(r.json["directive"], "run_test_implementor")
        self.assertEqual(r.json["iterations"]["test_implementation"], 1)
        self.assertEqual(r.json["test_implementation_iter"], 1)
        self.assertIn("test_implementor_config", r.json)
        self.assertIn("AC1's test asserts the wrong return value", r.json["note"])
        body = (p.run_dir / "attempts.md").read_text(encoding="utf-8")
        self.assertIn("state=implementation", body)
        self.assertIn("AC1's test asserts the wrong return value", body)

    def test_blocked_on_tests_exhausts_to_failed(self):
        p = self.make_pipeline(tdd_mode=True, max_test_implementation_iteration=1)
        p.init()
        self._to_implementation(p)
        p.write_implementor_result(status="blocked", blocked_on="tests", concern="first")
        r1 = p.advance()
        self.assertEqual(r1.json["next_state"], "test_implementation")
        self.assertEqual(r1.json["iterations"]["test_implementation"], 1)
        # Repair pass lands back on `test` (red_phase already false); the test
        # author writes valid tests (status:"implemented"), then fail the test so
        # the implementor runs again and can re-report blocked_on:"tests".
        p.write_test_implementor_result(status="implemented")
        r2 = p.advance()  # test_implementation -> test (repair)
        self.assertEqual(r2.json["next_state"], "test")
        p.write_test_result(status="fail", failure_type="code")
        r3 = p.advance()  # test -> implementation (retry)
        self.assertEqual(r3.json["next_state"], "implementation")
        p.write_implementor_result(status="blocked", blocked_on="tests", concern="second")
        r4 = p.advance()
        self.assertEqual(r4.json["next_state"], "failed")
        self.assertEqual(r4.json["halt_reason"], "iteration-exhausted")
        state = json.loads((p.run_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["history"][-1]["outcome"], "implementor_blocked_on_tests_exhausted")

    def test_repair_blocked_on_impl_standoff_exhausts(self):
        # 6.8.0: the implementor(blocked_on:tests) <-> test_author(blocked_on:
        # implementation) standoff terminates. The test-author reroute takes no
        # counter bump, so the loop is bounded by the implementation ->
        # test_implementation edge; it exhausts to `failed` with the generalized
        # standoff hint.
        p = self.make_pipeline(tdd_mode=True, max_test_implementation_iteration=1)
        p.init()
        self._to_implementation(p)
        p.write_implementor_result(status="blocked", blocked_on="tests", concern="tests wrong")
        r1 = p.advance()  # implementation -> test_implementation (test_impl=1)
        self.assertEqual(r1.json["next_state"], "test_implementation")
        self.assertEqual(r1.json["iterations"]["test_implementation"], 1)
        # Test author disagrees: the tests are correct, the code is the gap.
        p.write_test_implementor_result(status="blocked", blocked_on="implementation",
                                        concern="tests correct; code is the gap")
        r2 = p.advance()  # test_implementation -> implementation (NO bump)
        self.assertEqual(r2.json["next_state"], "implementation")
        self.assertEqual(r2.json["iterations"]["test_implementation"], 1)
        # Implementor holds its ground; the next reroute bumps past the budget.
        p.write_implementor_result(status="blocked", blocked_on="tests", concern="still tests")
        r3 = p.advance()
        self.assertEqual(r3.json["next_state"], "failed")
        self.assertEqual(r3.json["halt_reason"], "iteration-exhausted")
        self.assertIn("standoff", r3.json.get("hint", "").lower())
        state = json.loads((p.run_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["history"][-1]["outcome"], "implementor_blocked_on_tests_exhausted")

    def test_blocked_on_tests_non_tdd_falls_through_to_test(self):
        # blocked_on:"tests" only means something in TDD (there is no test
        # author to hand off to otherwise) — a non-TDD run must ignore it.
        p = self.started(tdd_mode=False)
        p.advance()  # init -> implementation
        p.write_implementor_result(status="blocked", blocked_on="tests", concern="irrelevant")
        r = p.advance()
        self.assertEqual(r.json["next_state"], "test")

    def test_blocked_contract_routes_to_test(self):
        # blocked_on:"contract" is the implementor-can't-satisfy-the-contract
        # case — routes to test as before, not test_implementation.
        p = self.make_pipeline(tdd_mode=True)
        p.init()
        self._to_implementation(p)
        p.write_implementor_result(status="blocked", concern="the contract contradicts itself")
        r = p.advance()
        self.assertEqual(r.json["next_state"], "test")
        self.assertEqual(r.json["iterations"]["test_implementation"], 0)

    def test_implementation_missing_result_file_dies(self):
        p = self.started(tdd_mode=False)
        p.advance()  # init -> implementation
        # No implementor-result.json written.
        r = p.advance()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("implementor-result.json not found", r.stderr)

    def test_implementation_schema_invalid_result_dies(self):
        p = self.started(tdd_mode=False)
        p.advance()  # init -> implementation
        p.write_raw_result_file("implementor-result.json",
                                json.dumps({"status": "maybe"}))
        r = p.advance()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("schema violation", r.stderr)

    def test_blocked_test_implementor_result_does_not_change_routing(self):
        # cmd_advance's test_implementation branch reads test_implementor-result.json
        # while red_phase is pending only for the mandatory-status guard; routing is
        # unconditional (-> red_test). A status:"blocked" result therefore falls
        # through to red_test like any other: there's no other role to route a
        # red-phase test-author "blocked" to.
        p = self.make_pipeline(tdd_mode=True)
        p.init()
        p.advance()  # init -> test_implementation
        p.write_test_implementor_result(status="blocked", concern="no meaningful test for AC2")
        r = p.advance()  # test_implementation -> red_test, routing unaffected by "blocked"
        self.assertEqual(r.json["next_state"], "red_test")

    def test_blocked_on_implementation_is_inert_during_red_phase(self):
        # 6.8.0: blocked_on:"implementation" reroutes to the implementor ONLY on a
        # repair pass. During red_phase it is inert — a blocked result still falls
        # through to red_test regardless of blocked_on (schema documents this).
        p = self.make_pipeline(tdd_mode=True)
        p.init()
        p.advance()  # init -> test_implementation (red_phase)
        p.write_test_implementor_result(status="blocked", blocked_on="implementation",
                                        concern="code is the gap")
        r = p.advance()
        self.assertEqual(r.json["next_state"], "red_test")


class TestTestImplementorResultGuard(PipelineTestCase):
    """The test_implementor status file is mandatory (6.6.0): the red-phase
    advance reads and validates it (die-on-missing/invalid). As of 7.0.0 routing
    from a red-phase pass is unconditional (always -> red_test); the read exists
    only to enforce the mandatory-status guard."""

    def test_missing_test_implementor_result_dies_during_red_phase(self):
        # Symmetry with the `implementation` branch's own die()-on-missing —
        # the file is mandatory since 6.6.0, so its absence here is a driver/
        # runner bug, not a normal advisory gap.
        p = self.make_pipeline(tdd_mode=True)
        p.init()
        p.advance()  # init -> test_implementation
        # No test_implementor-result.json written.
        r = p.advance()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("test_implementor-result.json not found", r.stderr)

    def test_schema_invalid_test_implementor_result_dies_during_red_phase(self):
        p = self.make_pipeline(tdd_mode=True)
        p.init()
        p.advance()  # init -> test_implementation
        p.write_raw_result_file("test_implementor-result.json",
                                json.dumps({"status": "maybe"}))
        r = p.advance()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("schema violation", r.stderr)

    def test_implemented_status_in_red_phase_routes_to_red_test(self):
        # A red-phase authoring pass always proves RED first (7.0.0): there is no
        # in-flow skip path any more, so `implemented` routes straight to red_test.
        p = self.make_pipeline(tdd_mode=True)
        p.init()
        p.advance()  # init -> test_implementation
        p.write_test_implementor_result(status="implemented")
        r = p.advance()
        self.assertEqual(r.json["next_state"], "red_test")
        state = json.loads((p.run_dir / "state.json").read_text(encoding="utf-8"))
        self.assertTrue(state["red_phase"])

    def test_red_expected_field_is_rejected_by_schema(self):
        # Regression guard for the 7.0.0 removal: the shared schema is
        # additionalProperties:false, so a result still carrying the removed
        # `red_expected` field must now be rejected outright.
        p = self.make_pipeline(tdd_mode=True)
        p.init()
        p.advance()  # init -> test_implementation
        p.write_raw_result_file(
            "test_implementor-result.json",
            json.dumps({"status": "implemented", "summary": "x",
                        "red_expected": False}))
        r = p.advance()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("schema violation", r.stderr)


class TestUpgradeSafety(PipelineTestCase):
    """A run created by an older driver (no tdd_mode/red_phase/test_implementation
    keys) must resume without crashing under the new driver."""

    def test_legacy_state_resume_does_not_crash(self):
        p = self.started()  # modern init
        p.advance()  # init -> implementation
        p.write_implementor_result(status="implemented")
        p.advance()  # implementation -> test

        # Rewrite state.json into the 1.3.0 shape: strip the new keys.
        sp = p.run_dir / "state.json"
        st = json.loads(sp.read_text(encoding="utf-8"))
        st.pop("tdd_mode", None)
        st.pop("red_phase", None)
        st["iterations"] = {"test": 0, "review": 0}
        st["max"] = {"test": st["max"]["test"], "review": st["max"]["review"]}
        sp.write_text(json.dumps(st), encoding="utf-8")

        # get_iter_path / advance must tolerate the missing test_implementation key.
        p.write_test_result(status="pass")
        r = p.advance()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.json["next_state"], "review")

    def test_config_missing_test_implementor_rejected_when_tdd_default_on(self):
        # A bare 1.x config (no tdd_mode, no test_implementor) is NOT silently
        # accepted: tdd defaults true, so test_implementor is required.
        cfg = valid_config()  # has test_implementor
        del cfg["llm"]["test_implementor"]
        del cfg["runners"]["test_implementor"]
        cfg["driver"].pop("tdd_mode", None)  # absent -> default true
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json",
                                          delete=False, encoding="utf-8")
        json.dump(cfg, tmp); tmp.close()
        r = run_driver("validate-config", "--config", tmp.name)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("test_implementor", r.stderr)
        # ...but the same config validates once tdd_mode is turned off in the config
        # (the --no-tdd flag was removed in 5.0.0; tdd_mode is config-sourced).
        cfg["driver"]["tdd_mode"] = False
        tmp2 = tempfile.NamedTemporaryFile(mode="w", suffix=".json",
                                           delete=False, encoding="utf-8")
        json.dump(cfg, tmp2); tmp2.close()
        r2 = run_driver("validate-config", "--config", tmp2.name)
        self.assertEqual(r2.returncode, 0, r2.stderr)


class TestCheckBoundary(PipelineTestCase):
    """Deterministic role-boundary subcommand (TDD file isolation)."""

    def _run(self):
        # A tdd-on init writes config.snapshot.json with test_paths ["tests/**"].
        p = self.make_pipeline(tdd_mode=True)
        p.init()
        return p

    def _check(self, p, role, changed):
        return run_driver("check-boundary", "--run", str(p.run_dir),
                          "--role", role, "--changed", *changed)

    def test_test_author_inside_paths_ok(self):
        p = self._run()
        r = self._check(p, "test_implementation", ["tests/test_a.py", "tests/sub/test_b.py"])
        self.assertEqual(r.returncode, 0)
        self.assertTrue(r.json["ok"])

    def test_test_author_touching_production_flagged(self):
        p = self._run()
        r = self._check(p, "test_implementation", ["tests/test_a.py", "src/app.py"])
        self.assertEqual(r.returncode, 0)
        self.assertFalse(r.json["ok"])
        self.assertEqual(r.json["reason"], "out_of_bounds")
        self.assertEqual(r.json["violating"], ["src/app.py"])

    def test_test_author_zero_match_is_misconfig(self):
        p = self._run()
        r = self._check(p, "test_implementation", ["src/app.py"])
        self.assertEqual(r.returncode, 0)
        self.assertFalse(r.json["ok"])
        self.assertEqual(r.json["reason"], "no_match")

    def test_implementor_outside_paths_ok(self):
        p = self._run()
        r = self._check(p, "implementation", ["src/app.py", "src/util.py"])
        self.assertEqual(r.returncode, 0)
        self.assertTrue(r.json["ok"])

    def test_implementor_touching_tests_flagged(self):
        p = self._run()
        r = self._check(p, "implementation", ["src/app.py", "tests/test_a.py"])
        self.assertEqual(r.returncode, 0)
        self.assertFalse(r.json["ok"])
        self.assertEqual(r.json["reason"], "touched_tests")
        self.assertEqual(r.json["violating"], ["tests/test_a.py"])


class TestRecordChanges(PipelineTestCase):
    """The commit/review manifest accumulator (record-changes)."""

    def _init(self, tdd_mode=False):
        p = self.make_pipeline(tdd_mode=tdd_mode)
        p.init()
        return p

    def _record(self, p, changed):
        return run_driver("record-changes", "--run", str(p.run_dir),
                          "--changed", *changed)

    def _manifest(self, p):
        m = p.run_dir / "changed-manifest.txt"
        return m.read_text(encoding="utf-8").splitlines() if m.exists() else []

    def test_records_and_sorts(self):
        p = self._init()
        r = self._record(p, ["src/b.py", "src/a.py"])
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self._manifest(p), ["src/a.py", "src/b.py"])

    def test_accumulates_and_dedupes(self):
        p = self._init()
        self._record(p, ["src/a.py", "src/b.py"])
        self._record(p, ["src/b.py", "src/c.py"])  # b.py is a duplicate
        self.assertEqual(self._manifest(p), ["src/a.py", "src/b.py", "src/c.py"])

    def test_excludes_dev_pipeline_paths(self):
        p = self._init()
        r = self._record(p, ["src/a.py", ".dev-pipeline/runs/x/spec.md",
                             "sub/.dev-pipeline/state.json"])
        self.assertEqual(r.json["skipped"],
                         [".dev-pipeline/runs/x/spec.md", "sub/.dev-pipeline/state.json"])
        self.assertEqual(self._manifest(p), ["src/a.py"])

    def test_normalizes_leading_dot_slash(self):
        p = self._init()
        self._record(p, ["./src/a.py"])
        self.assertEqual(self._manifest(p), ["src/a.py"])

    def test_empty_and_blank_input(self):
        p = self._init()
        r = self._record(p, ["", "   "])
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.json["recorded"], [])
        self.assertEqual(self._manifest(p), [])

    def test_missing_run_dir_errors(self):
        r = run_driver("record-changes", "--run", "/nonexistent/run-dir",
                       "--changed", "a.py")
        self.assertNotEqual(r.returncode, 0)


class TestAdvanceEchoes(PipelineTestCase):
    """advance must echo every config-derived value a destination state needs,
    so the SKILL never reads config.snapshot.json for control flow."""

    def test_legacy_flow_echoes(self):
        p = self.make_pipeline(tdd_mode=False, run_self_evolution=True)
        # A distinctive build command proves the echo reads config verbatim,
        # not just the "no build step" fallback default.
        p._config["llm"]["tester"]["build_instruction"] = "make build"
        p.init()
        r1 = p.advance()  # init -> implementation
        j = r1.json
        self.assertEqual(j["next_state"], "implementation")
        self.assertFalse(j["tdd_mode"])
        self.assertTrue(j["design_instruction"])
        # echoed verbatim from config (LLM-agnostic — not "is it claude?")
        self.assertEqual(j["implementor_runners"], p._config["runners"]["implementor"])
        # The implementor build-checks before handoff → it gets the tester's build cmd.
        self.assertEqual(j["build_instruction"], "make build")
        self.assertNotIn("test_paths", j)  # legacy: no test boundary

        p.write_implementor_result(status="implemented")
        r2 = p.advance()  # implementation -> test
        self.assertEqual(r2.json["next_state"], "test")
        self.assertFalse(r2.json["tdd_mode"])
        self.assertEqual(r2.json["tester_runners"], p._config["runners"]["tester"])

        p.write_test_result(status="pass")
        r3 = p.advance()  # test -> review
        self.assertEqual(r3.json["next_state"], "review")
        self.assertFalse(r3.json["tdd_mode"])

        p.write_review_result(verdict="approve")
        r4 = p.advance()  # review -> done
        self.assertEqual(r4.json["next_state"], "done")
        self.assertFalse(r4.json["tdd_mode"])
        self.assertTrue(r4.json["run_self_evolution"])

    def test_tdd_flow_echoes(self):
        p = self.started(tdd_mode=True)
        r1 = p.advance()  # init -> test_implementation
        self.assertEqual(r1.json["next_state"], "test_implementation")
        self.assertTrue(r1.json["tdd_mode"])
        self.assertEqual(r1.json["test_implementor_runners"], p._config["runners"]["test_implementor"])

        p.write_test_implementor_result(status="implemented")
        r2 = p.advance()  # test_implementation -> red_test (red phase)
        self.assertEqual(r2.json["next_state"], "red_test")
        self.assertTrue(r2.json["tdd_mode"])
        self.assertEqual(r2.json["tester_runners"], p._config["runners"]["tester"])

        p.write_red_test_result(status="fail", failure_type="code")
        r3 = p.advance()  # red confirmed -> implementation
        self.assertEqual(r3.json["next_state"], "implementation")
        self.assertTrue(r3.json["tdd_mode"])
        self.assertTrue(r3.json["design_instruction"])
        self.assertEqual(r3.json["implementor_runners"], p._config["runners"]["implementor"])
        self.assertEqual(r3.json["build_instruction"], "no build step")
        self.assertEqual(r3.json["test_paths"], ["tests/**"])  # tdd echoes the boundary

    def test_tdd_mode_frozen_into_state_and_echoed(self):
        # tdd_mode is frozen into state.json at init (from the merged config) and
        # re-echoed on every advance; the SKILL never re-derives it.
        p = self.make_pipeline(tdd_mode=True)
        p.init()
        state = json.loads((p.run_dir / "state.json").read_text(encoding="utf-8"))
        self.assertTrue(state["tdd_mode"])
        r1 = p.advance()
        self.assertEqual(r1.json["next_state"], "test_implementation")
        self.assertTrue(r1.json["tdd_mode"])


class TestStageInputWiring(PipelineTestCase):
    """init/advance persist a stage-input.json so `driver run-stage` can consume
    the same context the SKILL echo carries (additive; legacy flow unaffected)."""

    def test_init_writes_contract_not_spec_stage_input(self):
        # As of 5.0.0 init writes the contract (contract.md) from the plan body and
        # does NOT emit a spec-author stage-input.
        p = self.started(tdd_mode=False)
        contract = pathlib.Path(json.loads(
            (p.run_dir / "state.json").read_text(encoding="utf-8"))["contract_path"])
        self.assertTrue(contract.exists())
        self.assertEqual(contract.name, "contract.md")
        self.assertIn("## Acceptance Criteria", contract.read_text(encoding="utf-8"))
        self.assertFalse((p.run_dir / "stage-input.json").exists())

    def test_advance_writes_tester_stage_input(self):
        p = self.started(tdd_mode=False)
        p.advance()  # init -> implementation (no iter_dir echoed yet)
        p.write_implementor_result(status="implemented")
        p.advance()  # implementation -> test  (tester; iter_dir echoed)
        si = json.loads((p.run_dir / "iterations" / "0" / "stage-input.json").read_text(encoding="utf-8"))
        self.assertEqual(si["role"], "tester")
        self.assertTrue(si["output_file"].endswith("test-result.json"))
        self.assertIn("build_instruction", si["inputs"])
        self.assertNotIn("directive", si["inputs"])  # control keys excluded
        self.assertNotIn("tester_runners", si["inputs"])  # M1: runner arrays never reach the prompt

    def test_advance_writes_implementor_output_file(self):
        # 6.5.0: implementor (a file role) gets an output_file too, so its prompt
        # can be told an exact path for its status JSON — iter_dir itself is
        # deliberately never exposed to a role's prompt (_STAGE_INPUT_CONTROL),
        # so this wiring is the only way it learns the path. Since 6.6.0 this
        # status JSON is mandatory (validated by judge()/finalize-stage), not
        # merely optional.
        p = self.started(tdd_mode=False)
        p.advance()  # init -> implementation (iter_dir echoed here)
        si = json.loads((p.run_dir / "iterations" / "0" / "stage-input.json").read_text(encoding="utf-8"))
        self.assertEqual(si["role"], "implementor")
        self.assertTrue(si["output_file"].endswith("implementor-result.json"))
        self.assertNotIn("iter_dir", si["inputs"])  # never leaks into the prompt itself

    def test_advance_writes_test_implementor_output_file(self):
        p = self.started(tdd_mode=True)
        p.advance()  # init -> test_implementation (iter_dir echoed here)
        si = json.loads((p.run_dir / "iterations" / "0" / "stage-input.json").read_text(encoding="utf-8"))
        self.assertEqual(si["role"], "test_implementor")
        self.assertTrue(si["output_file"].endswith("test_implementor-result.json"))


class TestRunStageIntegration(PipelineTestCase):
    """The advance/init → run-stage contract, with dummy runners (no LLM):
    the stage-input.json the DRIVER writes must be consumable by run-stage."""

    def _to_impl_stage_input(self, cfg):
        """init → advance to implementation; return (pipeline, driver-written
        implementor stage-input path)."""
        p = Pipeline(cfg)
        self._pipelines.append(p)
        p.init()
        adv = p.advance()  # init -> implementation; driver writes the iter stage-input
        self.assertEqual(adv.json["directive"], "run_implementor")
        return p, pathlib.Path(adv.json["iter_dir"]) / "stage-input.json"

    def test_file_role_from_driver_written_stage_input(self):
        cfg = valid_config(tdd_mode=False)
        cfg["runners"]["implementor"] = [
            {"type": "bash", "command": "echo '{\"status\":\"implemented\",\"summary\":\"stub\"}' > {output_file}"},
        ]
        p, si = self._to_impl_stage_input(cfg)
        r = run_driver("run-stage", "--run", str(p.run_dir), "--role", "implementor",
                       "--stage-input", str(si))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.json["ok"])

    def test_role_mismatch_guard(self):
        cfg = valid_config(tdd_mode=False)
        p, si = self._to_impl_stage_input(cfg)
        # stage-input role is implementor; calling --role tester must fail loudly.
        r = run_driver("run-stage", "--run", str(p.run_dir), "--role", "tester",
                       "--stage-input", str(si))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("role", r.stderr)

    def test_legacy_snapshot_runner_guard(self):
        cfg = valid_config(tdd_mode=False)
        p, si = self._to_impl_stage_input(cfg)
        # Simulate a pre-3.0.0 snapshot: a runner with no command.
        snap = json.loads((p.run_dir / "config.snapshot.json").read_text(encoding="utf-8"))
        snap["runners"]["implementor"] = [{"type": "claude-subagent", "agent": "dp-implementor"}]
        (p.run_dir / "config.snapshot.json").write_text(json.dumps(snap), encoding="utf-8")
        r = run_driver("run-stage", "--run", str(p.run_dir), "--role", "implementor",
                       "--stage-input", str(si))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("pre-3.0.0", r.stderr)


class TestRunStage(unittest.TestCase):
    """run-stage: prompt assembly + bash-runner execution + per-category checks.
    Uses dummy shell runners (no LLM) so the behavior is deterministic."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.run_dir = pathlib.Path(self._tmp.name)
        self.proj = self.run_dir / "proj"
        self.proj.mkdir()
        (self.run_dir / "state.json").write_text(json.dumps({
            "state": "test", "project_dir": str(self.proj), "tdd_mode": False,
            "iterations": {"test": 0, "review": 0, "test_implementation": 0},
        }), encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def _cfg(self, role, runners):
        (self.run_dir / "config.snapshot.json").write_text(
            json.dumps({"runners": {role: runners}}), encoding="utf-8")

    def _si(self, role, **extra):
        si = {"role": role, "work_dir": str(self.run_dir / "w"),
              "project_root": str(self.proj), "inputs": {"design_instruction": "x"}}
        si.update(extra)
        p = self.run_dir / "si.json"
        p.write_text(json.dumps(si), encoding="utf-8")
        return p

    def _run(self, role, si_path):
        return run_driver("run-stage", "--run", str(self.run_dir),
                          "--role", role, "--stage-input", str(si_path))

    def test_file_role_fallback(self):
        # 1st runner fails (exit 1), 2nd writes a file + the (now-mandatory)
        # status JSON and succeeds.
        self._cfg("implementor", [
            {"type": "bash", "command": "exit 1"},
            {"type": "bash", "command": "echo x > {project_root}/made.py && "
             "echo '{\"status\":\"implemented\",\"summary\":\"stub\"}' > {output_file}"},
        ])
        r = self._run("implementor", self._si("implementor"))
        self.assertEqual(r.returncode, 0)
        self.assertTrue(r.json["ok"])
        self.assertEqual(r.json["runner"], 1)
        self.assertTrue((self.proj / "made.py").exists())

    def test_file_role_status_json_missing_fails_ok(self):
        # 6.6.0: exit 0 but no status JSON produced is no longer ok:true — the
        # role's status file is mandatory, validated the same way a json role's
        # result is. A single runner with no fallback ends in all_runners_failed.
        self._cfg("implementor", [{"type": "bash", "command": "true"}])
        r = self._run("implementor", self._si("implementor"))
        self.assertNotEqual(r.returncode, 0)
        j = json.loads(r.stdout)
        self.assertFalse(j["ok"])
        self.assertEqual(j["reason"], "all_runners_failed")

    def test_file_role_status_json_crash_does_not_retry(self):
        # A non-zero exit must NOT trigger the error-fed retry (that would redo
        # the whole implementation attempt for a plain crash) — it fails
        # immediately, same as before 6.6.0.
        self._cfg("implementor", [{"type": "bash", "command": "echo boom 1>&2; exit 3"}])
        r = self._run("implementor", self._si("implementor"))
        self.assertNotEqual(r.returncode, 0)
        j = json.loads(r.stdout)
        self.assertFalse(j["ok"])
        self.assertEqual(len(j["attempts"]), 1)  # no retry attempt appended
        self.assertIn("exit 3", j["attempts"][0]["problem"])

    def test_implementor_bash_prompt_gets_status_directive(self):
        # The assembled prompt for a bash-runner implementor must name the
        # exact path for its (mandatory, since 6.6.0) result-status JSON — never
        # guessed by the model (iter_dir/work_dir is never in the prompt's own
        # inputs).
        self._cfg("implementor", [{"type": "bash", "command": "true"}])
        self._run("implementor", self._si("implementor"))
        user_text = (self.run_dir / "w" / "implementor-user.txt").read_text(encoding="utf-8")
        self.assertIn("write a brief status JSON", user_text)
        self.assertIn("implementor-output.json", user_text)  # default output_file naming (no output_file in this stage-input)
        self.assertNotIn("valid JSON object", user_text)  # not the json-role directive

    def test_test_implementor_bash_prompt_gets_status_directive(self):
        self._cfg("test_implementor", [{"type": "bash", "command": "true"}])
        self._run("test_implementor", self._si("test_implementor"))
        user_text = (self.run_dir / "w" / "test_implementor-user.txt").read_text(encoding="utf-8")
        self.assertIn("write a brief status JSON", user_text)

    def test_prompt_prose_resolved_from_layout(self):
        # Guards role_prompt_path + the .agents/skills/dev-pipeline/agents/ layout:
        # if resolution broke, the role would silently run on the "You are the
        # <role>." stub and emit a stderr WARNING. Assert the real prose is found
        # and assembled instead.
        self._cfg("implementor", [
            {"type": "bash", "command": "echo '{\"status\":\"implemented\",\"summary\":\"stub\"}' > {output_file}"},
        ])
        r = self._run("implementor", self._si("implementor"))
        self.assertEqual(r.returncode, 0)
        self.assertNotIn("WARNING: prose file", r.stderr)
        sysf = self.run_dir / "w" / "implementor-system.txt"
        self.assertTrue(sysf.exists(), "run-stage did not persist the system prompt")
        system_text = sysf.read_text(encoding="utf-8")
        self.assertNotEqual(system_text.strip(), "You are the implementor.")
        # The assembled system prompt IS the real prose file (frontmatter stripped),
        # so a distinctive body line from the source must survive into it. Reuse the
        # driver's own strip_frontmatter so the "body" matches what run-stage built
        # (no divergent local heuristic).
        src = (TOOLS_DIR.parent / "skills" / "dev-pipeline" / "agents"
               / "dp-implementor.md").read_text(encoding="utf-8")
        spec = importlib.util.spec_from_file_location("dp_driver", DRIVER)
        drv = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(drv)
        body = drv.strip_frontmatter(src)
        marker = next((l.strip() for l in body.splitlines() if len(l.strip()) > 40), None)
        self.assertIsNotNone(marker, "no distinctive prose line found to assert on")
        self.assertIn(marker, system_text)

    def test_json_role_fenced_output_persisted_clean(self):
        # A model may wrap its JSON in a markdown fence. run-stage tolerates it for
        # validation, but must PERSIST clean JSON so the driver's advance (which
        # reads the file with a plain json.loads) does not choke. Regression guard.
        payload = json.dumps(test_result(status="pass"))
        self._cfg("tester", [
            {"type": "bash",
             "command": "printf '```json\\n%s\\n```\\n' " + shlex.quote(payload) + " > {output_file}",
             "normalizer": "default"},
        ])
        out = self.run_dir / "tr.json"
        r = self._run("tester", self._si("tester", output_file=str(out)))
        self.assertEqual(r.returncode, 0)
        self.assertTrue(r.json["ok"])
        # The persisted file must be plain JSON — no fence — so json.loads succeeds.
        text = out.read_text(encoding="utf-8")
        self.assertNotIn("```", text)
        self.assertEqual(json.loads(text)["status"], "pass")

    def test_json_role_default_normalizer_is_tolerant(self):
        # With NO explicit normalizer the default is `default` (tolerant), so fenced
        # output still validates. Guards against reverting the bash default to the
        # strict `passthrough` (which would dead-end on a fenced result).
        payload = json.dumps(test_result(status="pass"))
        self._cfg("tester", [
            {"type": "bash",
             "command": "printf '```json\\n%s\\n```\\n' " + shlex.quote(payload) + " > {output_file}"},
        ])
        out = self.run_dir / "tr.json"
        r = self._run("tester", self._si("tester", output_file=str(out)))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.json["ok"])

    def test_legacy_normalizer_in_snapshot_stays_lenient(self):
        # A pre-6.0.0 frozen snapshot may carry normalizer "claude-cli". run-stage
        # reads the snapshot UNVALIDATED, so _normalize_output must stay lenient for
        # any non-passthrough value (not silently revert to strict mid-run).
        payload = json.dumps(test_result(status="pass"))
        self._cfg("tester", [
            {"type": "bash",
             "command": "printf '```json\\n%s\\n```\\n' " + shlex.quote(payload) + " > {output_file}",
             "normalizer": "claude-cli"}])
        out = self.run_dir / "tr.json"
        r = self._run("tester", self._si("tester", output_file=str(out)))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.json["ok"])

    def test_json_role_invalid_then_valid(self):
        good = self.run_dir / "good.json"
        good.write_text(json.dumps(test_result(status="pass")), encoding="utf-8")
        self._cfg("tester", [
            {"type": "bash", "command": "echo NOT_JSON > {output_file}"},
            {"type": "bash", "command": f"cp {good} " + "{output_file}"},
        ])
        out = self.run_dir / "tr.json"
        r = self._run("tester", self._si("tester", output_file=str(out)))
        self.assertEqual(r.returncode, 0)
        self.assertTrue(r.json["ok"])
        self.assertEqual(r.json["runner"], 1)

    def test_json_role_error_fed_retry(self):
        # Same runner: writes invalid JSON first, then valid once the prompt shows
        # the REJECTED feedback (the driver's one error-fed retry).
        good = self.run_dir / "good.json"
        good.write_text(json.dumps(test_result(status="pass")), encoding="utf-8")
        self._cfg("tester", [
            {"type": "bash",
             "command": f"if grep -q REJECTED {{user_file}}; then cp {good} {{output_file}}; "
                        "else echo BAD > {output_file}; fi"},
        ])
        out = self.run_dir / "tr.json"
        r = self._run("tester", self._si("tester", output_file=str(out)))
        self.assertEqual(r.returncode, 0)
        self.assertTrue(r.json["ok"])
        self.assertEqual(r.json["runner"], 0)   # same runner, recovered on retry
        self.assertEqual(r.json["attempts"], [])  # no fallback needed

    def test_json_role_schema_invalid_fails(self):
        self._cfg("tester", [
            {"type": "bash", "command": "echo '{\"status\":\"bogus\"}' > {output_file}"},
        ])
        out = self.run_dir / "tr.json"
        r = self._run("tester", self._si("tester", output_file=str(out)))
        # run-stage emits JSON then exits non-zero on failure (run_driver only
        # parses .json on a 0 exit), so parse stdout here.
        self.assertNotEqual(r.returncode, 0)
        j = json.loads(r.stdout)
        self.assertFalse(j["ok"])
        self.assertEqual(j["reason"], "all_runners_failed")

    def test_empty_runners_errors(self):
        self._cfg("tester", [])
        r = self._run("tester", self._si("tester", output_file=str(self.run_dir / "o.json")))
        self.assertNotEqual(r.returncode, 0)

    def test_timeout_kills_process_group(self):
        # A runner timeout must SIGKILL the whole process group, not just the
        # direct child shell — otherwise a grandchild (the real LLM CLI) is
        # orphaned and keeps running. The runner backgrounds a subshell that
        # would touch a marker after 5s; with the group-kill fix the marker is
        # never created. (implementor is a file role, so a timeout is a single
        # fast attempt — no error-fed retry.)
        marker = self.proj / "orphan_marker"
        self._cfg("implementor", [
            {"type": "bash",
             "command": "(sleep 5; touch {project_root}/orphan_marker) & sleep 30",
             "timeout": 1},
        ])
        r = self._run("implementor", self._si("implementor"))
        # all_runners_failed → non-zero exit; run_driver only sets .json on exit 0.
        self.assertNotEqual(r.returncode, 0)
        j = json.loads(r.stdout)
        self.assertFalse(j["ok"])
        self.assertEqual(j["attempts"][0]["problem"], "timeout")
        # Wait past the grandchild's 5s sleep; the marker must NOT appear.
        time.sleep(6)
        self.assertFalse(marker.exists(),
                         "grandchild survived the timeout — process group was not killed")

    def test_success_waits_for_backgrounded_group_mate(self):
        # 6.2.0 switched _run_one from PIPE+communicate() (which, as a side
        # effect of waiting for pipe EOF, blocked until every process holding
        # the write end — including a backgrounded grandchild — closed it) to
        # a direct file redirect. A file has no EOF to wait on, so without an
        # explicit fix the direct child's own exit alone would be reported as
        # "done" even while a same-process-group background job it spawned is
        # still running and still writing to the log. Confirm the fix: the
        # runner must not be reported successful until the marker the
        # backgrounded job creates 2s later actually exists, and the delayed
        # output must be captured.
        marker = self.proj / "bg_marker"
        self._cfg("implementor", [
            {"type": "bash",
             "command": (f"echo shell-done; (sleep 2; echo grandchild-done; touch {{project_root}}/bg_marker) & "
                        "echo '{\"status\":\"implemented\",\"summary\":\"stub\"}' > {output_file}"),
             "timeout": 10},
        ])
        r = self._run("implementor", self._si("implementor"))
        self.assertEqual(r.returncode, 0)
        self.assertTrue(r.json["ok"])
        self.assertTrue(marker.exists(),
                         "run-stage reported success before the backgrounded group-mate finished")
        log_text = pathlib.Path(r.json["log_file"]).read_text(encoding="utf-8")
        self.assertIn("grandchild-done", log_text)

    def test_no_timeout_runs_unbounded(self):
        # A bash runner with no `timeout` key must run unbounded (no implicit
        # 10-minute default) rather than being SIGKILLed. This is a regression
        # guard for _run_one's `timeout=None` path: `deadline = None`, then
        # `proc.wait(timeout=None)` (stdlib blocks indefinitely) and the
        # post-exit group-drain loop (`while deadline is None or ...`) must both
        # complete without raising (e.g. a stray `time.monotonic() + None`
        # TypeError) and without ever reporting "timeout".
        self._cfg("implementor", [
            {"type": "bash", "command": "sleep 2; echo done; "
             "echo '{\"status\":\"implemented\",\"summary\":\"stub\"}' > {output_file}"},
        ])
        r = self._run("implementor", self._si("implementor"))
        self.assertEqual(r.returncode, 0)
        self.assertTrue(r.json["ok"])
        self.assertEqual(r.json["attempts"], [])

    def test_runner_log_content_and_path_are_echoed(self):
        # 6.2.0: a bash runner's stdout+stderr must land in <role>-runner.log
        # (not be buffered/discarded), and run-stage must echo its path as
        # log_file. This only checks the FINAL content — see
        # test_runner_log_streams_in_real_time_while_running for the actual
        # real-time claim, which this test alone would pass even under a
        # batch-write-at-exit implementation.
        self._cfg("implementor", [
            {"type": "bash", "command": "echo hello-stdout; echo hello-stderr 1>&2; "
             "echo '{\"status\":\"implemented\",\"summary\":\"stub\"}' > {output_file}"},
        ])
        r = self._run("implementor", self._si("implementor"))
        self.assertEqual(r.returncode, 0)
        self.assertTrue(r.json["ok"])
        log_path = pathlib.Path(r.json["log_file"])
        self.assertTrue(log_path.exists())
        log_text = log_path.read_text(encoding="utf-8")
        self.assertIn("hello-stdout", log_text)
        self.assertIn("hello-stderr", log_text)

    def test_runner_log_streams_in_real_time_while_running(self):
        # The whole point of 6.2.0's logging is observing a runner WHILE it
        # executes, not only after. Launch run-stage ourselves (bypassing the
        # blocking self._run helper) so we can poll the log mid-run: the
        # pre-sleep line must appear before the runner exits, and the
        # post-sleep line must NOT appear until after it does — a batched,
        # write-at-exit implementation would fail the second assertion.
        self._cfg("implementor", [
            {"type": "bash", "command": "printf 'PRE_MARKER\\n'; sleep 2; printf 'POST_MARKER\\n'; "
             "echo '{\"status\":\"implemented\",\"summary\":\"stub\"}' > {output_file}"},
        ])
        si_path = self._si("implementor")
        log_path = self.run_dir / "w" / "implementor-runner.log"

        def body():
            # The header line echoes the command's own source text (for audit),
            # so both markers are present in it from the moment the header is
            # written — BEFORE either has actually run. Only content after the
            # header's closing "-----\n" is real runtime output.
            if not log_path.exists():
                return ""
            return log_path.read_text(encoding="utf-8").split("-----\n", 1)[-1]

        proc = subprocess.Popen(
            [sys.executable, str(DRIVER), "run-stage", "--run", str(self.run_dir),
             "--role", "implementor", "--stage-input", str(si_path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.time() + 5
            seen_pre = False
            while time.time() < deadline:
                if "PRE_MARKER" in body():
                    seen_pre = True
                    break
                time.sleep(0.05)
            mid_body = body()
            self.assertTrue(seen_pre, "log never showed the pre-sleep marker while the runner was still running")
            self.assertNotIn("POST_MARKER", mid_body,
                              "post-sleep marker appeared before the sleep finished — log is not streaming live")
            out, err = proc.communicate(timeout=10)
        finally:
            if proc.poll() is None:
                proc.kill()
        self.assertEqual(proc.returncode, 0, err)
        self.assertIn("POST_MARKER", body())

    def test_runner_log_echoed_and_quoted_on_all_runners_failed(self):
        # log_file must be present even when every runner failed, and the
        # failure reason must quote the log (the old proc.stderr[-300:] tail is
        # gone now that stdout/stderr go straight to a file, not a pipe).
        self._cfg("implementor", [{"type": "bash", "command": "echo boom 1>&2; exit 1"}])
        r = self._run("implementor", self._si("implementor"))
        self.assertNotEqual(r.returncode, 0)
        j = json.loads(r.stdout)
        self.assertFalse(j["ok"])
        self.assertIn("log_file", j)
        log_text = pathlib.Path(j["log_file"]).read_text(encoding="utf-8")
        self.assertIn("boom", log_text)
        self.assertIn("boom", j["attempts"][0]["problem"])

    def test_runner_log_truncated_fresh_each_run_stage_call(self):
        # A fresh run-stage call for the same role/work_dir must not accumulate
        # unrelated content from a PRIOR call into the new log.
        status_write = " && echo '{\"status\":\"implemented\",\"summary\":\"stub\"}' > {output_file}"
        self._cfg("implementor", [{"type": "bash", "command": "echo first-call" + status_write}])
        r1 = self._run("implementor", self._si("implementor"))
        self.assertIn("first-call", pathlib.Path(r1.json["log_file"]).read_text(encoding="utf-8"))

        self._cfg("implementor", [{"type": "bash", "command": "echo second-call" + status_write}])
        r2 = self._run("implementor", self._si("implementor"))
        text2 = pathlib.Path(r2.json["log_file"]).read_text(encoding="utf-8")
        self.assertIn("second-call", text2)
        self.assertNotIn("first-call", text2)

    def test_output_directive_final_answer_branch_for_output_file_reference(self):
        # A command whose template references {output_file} (a stdout redirect
        # like claude's `>`, or a CLI-native flag like codex's `-o`) must be told
        # to give the JSON as its final answer, NOT to write the file itself —
        # this is also what keeps claude/codex bash-runner prompts identical.
        self._cfg("tester", [{"type": "bash", "command": "echo x > {output_file}"}])
        out = self.run_dir / "tr.json"
        self._run("tester", self._si("tester", output_file=str(out)))
        user_text = (self.run_dir / "w" / "tester-user.txt").read_text(encoding="utf-8")
        self.assertIn("as your final answer only", user_text)
        self.assertNotIn("Write a single valid JSON object", user_text)

    def test_output_directive_final_answer_branch_for_native_flag_without_redirect(self):
        # The actual regression this fix targets: a command referencing
        # {output_file} via a CLI-native flag with NO `>` redirect anywhere
        # (e.g. codex's `-o {output_file}`). The OLD regex `>\s*\{output_file\}`
        # would miss this and wrongly fall through to the "write it yourself"
        # branch — impossible/wrong for a harness-level capture like `-o`. Use
        # a command shaped like that (no `>` at all) and confirm it still gets
        # the "final answer" branch, not "write it yourself".
        self._cfg("tester", [{"type": "bash", "command": "true -o {output_file} --other-flag"}])
        out = self.run_dir / "tr.json"
        self._run("tester", self._si("tester", output_file=str(out)))
        user_text = (self.run_dir / "w" / "tester-user.txt").read_text(encoding="utf-8")
        self.assertIn("as your final answer only", user_text)
        self.assertNotIn("Write a single valid JSON object", user_text)

    def test_output_directive_write_branch_for_no_output_file_reference(self):
        # A command with no {output_file} reference at all (e.g. cline, which has
        # no clean-stdout mode or native result-file flag) must be told the exact
        # path to Write the result to.
        self._cfg("tester", [{"type": "bash", "command": "echo not-json"}])
        out = self.run_dir / "tr.json"
        self._run("tester", self._si("tester", output_file=str(out)))
        user_text = (self.run_dir / "w" / "tester-user.txt").read_text(encoding="utf-8")
        self.assertIn("Write a single valid JSON object", user_text)
        self.assertIn(str(out), user_text)


class TestConfigMigration(unittest.TestCase):
    """3.0.0: validate-config rejects removed runner types with a migration hint;
    migrate-config converts a pre-3.0.0 config to bash runners."""

    def _write(self, cfg):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = pathlib.Path(tmp.name) / "config.json"
        p.write_text(json.dumps(cfg), encoding="utf-8")
        return p

    def test_validate_rejects_legacy_runner(self):
        cfg = valid_config()
        cfg["runners"]["implementor"] = [{"type": "claude-subagent", "agent": "dp-implementor"}]
        r = run_driver("validate-config", "--config", str(self._write(cfg)))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("3.0.0", r.stderr)
        self.assertIn("migrate-config", r.stderr)

    def test_validate_rejects_unknown_placeholder(self):
        cfg = valid_config()
        cfg["runners"]["tester"] = [{"type": "bash", "command": "run-model {spec_path}"}]
        r = run_driver("validate-config", "--config", str(self._write(cfg)))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("spec_path", r.stderr)

    def test_migrate_resets_to_unconfigured(self):
        cfg = valid_config()
        cfg["runners"] = {
            "implementor": [{"type": "claude-subagent", "agent": "dp-implementor"}],
            "test_implementor": [{"type": "claude-subagent", "agent": "dp-test-implementor"}],
            "tester": [{"type": "claude-subagent", "agent": "dp-tester"}],
            "reviewer": [{"type": "codex-adversarial-review"}],
        }
        p = self._write(cfg)
        r = run_driver("migrate-config", "--config", str(p))
        self.assertEqual(r.returncode, 0)
        self.assertTrue(r.json["migrated"])
        migrated = json.loads(p.read_text(encoding="utf-8"))
        # spec_author was removed in 5.0.0; migration drops it.
        self.assertNotIn("spec_author", migrated["runners"])
        # 6.0.0: migration resets ALL runners to the 'unconfigured' sentinel (the
        # user reconfigures via --update-config), not to concrete bash defaults.
        self.assertEqual(
            migrated["runners"],
            {role: [{"type": "unconfigured"}] for role in
             ("implementor", "test_implementor", "tester", "reviewer")},
        )

    def test_migrate_drops_removed_driver_key(self):
        # A 5.x config with the removed driver.allow_unattended_header_merge must be
        # repairable: migrate-config strips it (apply-config's deep-merge can't).
        cfg = valid_config()
        cfg["driver"]["allow_unattended_header_merge"] = True
        cfg["runners"]["implementor"] = [{"type": "claude-subagent", "agent": "x"}]
        p = self._write(cfg)
        r = run_driver("migrate-config", "--config", str(p))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("allow_unattended_header_merge", r.json["dropped_driver_keys"])
        migrated = json.loads(p.read_text(encoding="utf-8"))
        self.assertNotIn("allow_unattended_header_merge", migrated["driver"])


class TestPlanBody(PipelineTestCase):
    """6.0.0: the plan.md body IS the contract (there is no config header) — the
    snapshot mirrors config.json, and the deterministic required-section gate."""

    def _snap(self, p):
        return json.loads((p.run_dir / "config.snapshot.json").read_text(encoding="utf-8"))

    def _cfg_on_disk(self, p):
        return json.loads((p.project / ".dev-pipeline" / "dev-pipeline.config.json")
                          .read_text(encoding="utf-8"))

    def _init_direct(self, p, plan_text):
        plan = p.project / "plan.md"
        plan.write_text(plan_text, encoding="utf-8")
        cfg_dir = p.project / ".dev-pipeline"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        cfg_path = cfg_dir / "dev-pipeline.config.json"
        cfg_path.write_text(json.dumps(p._config), encoding="utf-8")
        return run_driver("init", "--plan", str(plan), "--config", str(cfg_path),
                          "--project", str(p.project))

    def test_snapshot_equals_config_json(self):
        # init freezes config.json into the run snapshot verbatim — no header merge,
        # and config.json on disk is never rewritten by init.
        p = self.make_pipeline(tdd_mode=False)
        p.init()
        self.assertEqual(self._snap(p), self._cfg_on_disk(p))

    def test_contract_is_whole_plan_body(self):
        # The entire plan.md is the contract (nothing is stripped).
        p = self.make_pipeline(tdd_mode=False)
        p.init(plan_text=plan_body(False))
        contract = p.contract_path.read_text(encoding="utf-8")
        self.assertIn("## Acceptance Criteria", contract)
        self.assertIn("AC1", contract)
        self.assertIn("# Plan: Test", contract)

    def test_validate_config_plan_checks_config_and_body(self):
        p = self.make_pipeline(tdd_mode=False)
        cfg_path = p.project / ".dev-pipeline" / "dev-pipeline.config.json"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps(p._config), encoding="utf-8")
        good = p.project / "good.md"
        good.write_text(plan_body(False), encoding="utf-8")
        r = run_driver("validate-config", "--config", str(cfg_path), "--plan", str(good))
        self.assertEqual(r.returncode, 0, r.stderr)
        # a plan whose body lacks Acceptance Criteria is rejected by the same command.
        bad = p.project / "bad.md"
        bad.write_text("# Plan\n\n## Requirements\n- r\n", encoding="utf-8")
        r2 = run_driver("validate-config", "--config", str(cfg_path), "--plan", str(bad))
        self.assertNotEqual(r2.returncode, 0)
        self.assertIn("Acceptance Criteria", r2.stderr)

    def test_old_config_with_spec_author_runner_rejected(self):
        cfg = valid_config()
        cfg["runners"]["spec_author"] = [{"type": "bash", "command": "true"}]
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(cfg, tmp); tmp.close()
        r = run_driver("validate-config", "--config", tmp.name)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("spec_author", r.stderr)
        self.assertIn("migrate-config", r.stderr)

    def test_status_on_pre_5_state_with_spec_path_does_not_crash(self):
        # A run created just before the spec_path→contract_path rename must still
        # be inspectable and advanceable (contract_path falls back to spec_path).
        p = self.started(tdd_mode=False)
        state = json.loads((p.run_dir / "state.json").read_text(encoding="utf-8"))
        state["spec_path"] = state.pop("contract_path")  # simulate old key
        (p.run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
        st = p.status()
        self.assertEqual(st["state"], "init")
        r = p.advance()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.json["next_state"], "implementation")

    def test_heading_inside_nested_example_fence_is_not_a_section(self):
        # A required heading that only appears inside a 4-backtick example (which
        # itself shows a 3-backtick block) must NOT satisfy the section gate.
        p = self.make_pipeline(tdd_mode=True)
        body = ("# Plan\n\n## Requirements\n- r\n\n## Acceptance Criteria\n- [ ] a\n\n"
                "````\nexample:\n```\n## Interface\nfoo()\n```\n````\n")
        r = self._init_direct(p, body)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Interface", r.stderr)

    def test_numbered_acceptance_criteria_accepted(self):
        p = self.make_pipeline(tdd_mode=False)
        body = "# Plan\n\n## Requirements\n- r\n\n## Acceptance Criteria\n1. AC1 does x\n"
        j = p.init(plan_text=body)
        self.assertTrue((p.run_dir / "state.json").exists())
        self.assertIn("AC1", p.contract_path.read_text(encoding="utf-8"))
        _ = j

    def test_advance_dies_when_state_has_neither_contract_nor_spec_path(self):
        # A corrupted state.json missing both keys must die, not echo "." as the
        # contract (the pre-5.0.0 guard must actually fire).
        p = self.started(tdd_mode=False)
        state = json.loads((p.run_dir / "state.json").read_text(encoding="utf-8"))
        state.pop("contract_path", None)
        state.pop("spec_path", None)
        (p.run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
        r = p.advance()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("pre-5.0.0", r.stderr)


class TestRunnerModes(unittest.TestCase):
    """5.3.0: main-session / subagent runner types — validate-config rules, the
    run-stage handoff (assemble, don't execute), and finalize-stage validation."""

    # -- validate-config -------------------------------------------------------

    def _vc(self, runners_patch):
        cfg = valid_config()  # tdd off, real bash defaults
        cfg["runners"].update(runners_patch)
        p = pathlib.Path(tempfile.mktemp(suffix=".json"))
        p.write_text(json.dumps(cfg), encoding="utf-8")
        return run_driver("validate-config", "--config", str(p))

    def test_validate_accepts_main_session_and_subagent(self):
        r = self._vc({"reviewer": [{"type": "main-session"}],
                      "tester": [{"type": "subagent", "model": "sonnet", "normalizer": "default"}]})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.json["valid"])

    def test_validate_rejects_mixed_array(self):
        r = self._vc({"reviewer": [{"type": "subagent"}, {"type": "bash", "command": "x {output_file}"}]})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("mixed runner types", r.stderr)

    def test_validate_rejects_bash_without_command(self):
        r = self._vc({"reviewer": [{"type": "bash"}]})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("bash runner requires", r.stderr)

    def test_validate_rejects_subagent_with_command(self):
        r = self._vc({"reviewer": [{"type": "subagent", "command": "x"}]})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("must not have a `command`", r.stderr)

    def test_validate_still_rejects_legacy_subagent(self):
        # The pre-3.0.0 type (with `agent`) stays rejected — not confused with the
        # new `subagent` type (which carries `model`, no `agent`).
        r = self._vc({"reviewer": [{"type": "claude-subagent", "agent": "dp-reviewer"}]})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("3.0.0", r.stderr)

    def test_validate_rejects_removed_normalizer_name(self):
        # 6.0.0: the LLM-specific `claude-cli`/`codex-cli` normalizers were replaced
        # by `default`; an old name is named (not the generic oneOf) with a hint.
        r = self._vc({"reviewer": [{"type": "bash", "command": "x > {output_file}",
                                    "normalizer": "claude-cli"}]})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("unknown normalizer", r.stderr)
        self.assertIn("claude-cli", r.stderr)

    def test_validate_rejects_normalizer_on_file_role(self):
        # A file role (implementor/test_implementor) has no JSON output, so a
        # normalizer on it is a config mistake — rejected with an actionable reason.
        r = self._vc({"implementor": [{"type": "bash", "command": "true",
                                       "normalizer": "default"}]})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("normalizer", r.stderr)
        self.assertIn("file role", r.stderr)

    def test_validate_rejects_removed_driver_key_with_hint(self):
        # 6.0.0 dropped driver.allow_unattended_header_merge (the plan header is
        # gone). A 5.x config carrying it is named with a migrate-config hint (it
        # cannot be repaired by apply-config, whose deep-merge cannot delete a key).
        cfg = valid_config()
        cfg["driver"]["allow_unattended_header_merge"] = True
        p = pathlib.Path(tempfile.mktemp(suffix=".json"))
        p.write_text(json.dumps(cfg), encoding="utf-8")
        r = run_driver("validate-config", "--config", str(p))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("allow_unattended_header_merge", r.stderr)
        self.assertIn("migrate-config", r.stderr)

    def test_validate_rejects_non_dict_runners_cleanly(self):
        # A malformed runners section must give an actionable message, not a raw
        # AttributeError traceback from the per-role helpers.
        cfg = valid_config()
        cfg["runners"] = "oops"
        p = pathlib.Path(tempfile.mktemp(suffix=".json"))
        p.write_text(json.dumps(cfg), encoding="utf-8")
        r = run_driver("validate-config", "--config", str(p))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("runners: must be an object", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    # -- run-stage handoff + finalize-stage ------------------------------------

    def _setup(self, role, runners, json_role=True):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        run_dir = pathlib.Path(tmp.name)
        proj = run_dir / "proj"
        proj.mkdir()
        (run_dir / "state.json").write_text(json.dumps({
            "state": "test", "project_dir": str(proj), "tdd_mode": False,
            "iterations": {"test": 0, "review": 0, "test_implementation": 0}}), encoding="utf-8")
        (run_dir / "config.snapshot.json").write_text(
            json.dumps({"runners": {role: runners}}), encoding="utf-8")
        work = run_dir / "w"
        work.mkdir()
        si = {"role": role, "work_dir": str(work), "project_root": str(proj),
              "inputs": {"design_instruction": "x"}}
        if json_role:
            si["output_file"] = str(work / f"{role}-output.json")
        sp = run_dir / "si.json"
        sp.write_text(json.dumps(si), encoding="utf-8")
        return run_dir, proj, work, sp

    def test_run_stage_subagent_handoff(self):
        run_dir, proj, work, sp = self._setup("tester", [{"type": "subagent", "model": "sonnet"}])
        stale = work / "tester-output.json"
        stale.write_text("STALE")  # must be cleared before handoff
        r = run_driver("run-stage", "--run", str(run_dir), "--role", "tester", "--stage-input", str(sp))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.json["mode"], "subagent")
        self.assertEqual(r.json["model"], "sonnet")
        self.assertTrue(pathlib.Path(r.json["system_file"]).exists())
        self.assertIn("absolute project root", pathlib.Path(r.json["user_file"]).read_text(encoding="utf-8"))
        self.assertFalse(stale.exists())  # stale output unlinked; no execution wrote a new one

    def test_run_stage_main_session_handoff(self):
        run_dir, proj, work, sp = self._setup("tester", [{"type": "main-session"}])
        r = run_driver("run-stage", "--run", str(run_dir), "--role", "tester", "--stage-input", str(sp))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.json["mode"], "main-session")
        self.assertTrue(r.json["compact_first"])
        self.assertNotIn("model", r.json)
        # the driver prepends the persona preamble (role-switch + single-role scope)
        sys_text = pathlib.Path(r.json["system_file"]).read_text(encoding="utf-8")
        self.assertIn("acting SOLELY as the dev-pipeline tester", sys_text)
        self.assertIn("do NOT take on the OTHER pipeline stages", sys_text)
        # 7.1.1: the role's own output must not be mistaken for the run being
        # finished — regression guard for the "declares done without advancing" bug.
        self.assertIn("you are NOT done for this turn", sys_text)

    def test_finalize_stage_normalizes_fenced_json(self):
        run_dir, proj, work, sp = self._setup("tester", [{"type": "subagent", "normalizer": "default"}])
        outf = work / "tester-output.json"
        outf.write_text("```json\n" + json.dumps(test_result(status="pass")) + "\n```\n", encoding="utf-8")
        r = run_driver("finalize-stage", "--run", str(run_dir), "--role", "tester", "--stage-input", str(sp))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.json["ok"])
        self.assertNotIn("```", outf.read_text(encoding="utf-8"))  # canonical, no fence
        self.assertEqual(json.loads(outf.read_text(encoding="utf-8"))["status"], "pass")

    def test_finalize_stage_rejects_invalid(self):
        run_dir, proj, work, sp = self._setup("tester", [{"type": "subagent"}])
        (work / "tester-output.json").write_text("not json at all", encoding="utf-8")
        r = run_driver("finalize-stage", "--run", str(run_dir), "--role", "tester", "--stage-input", str(sp))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("problem", r.stdout)

    def test_finalize_stage_file_role_validates_status_json(self):
        # 6.6.0: a file role's status JSON is no longer a finalize-stage no-op —
        # it goes through the identical normalize/schema/persist path a json
        # role's result gets, keyed off schema presence rather than category.
        run_dir, proj, work, sp = self._setup("implementor", [{"type": "main-session"}], json_role=False)
        outf = work / "implementor-output.json"
        outf.write_text("```json\n" + json.dumps(implementor_result(status="implemented")) + "\n```\n",
                        encoding="utf-8")
        r = run_driver("finalize-stage", "--run", str(run_dir), "--role", "implementor", "--stage-input", str(sp))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.json["ok"])
        # the hardcoded "category": "json" bug (pre-6.6.0) must not resurface —
        # a file role's emit must report its own true category.
        self.assertEqual(r.json["category"], "file")
        self.assertNotIn("```", outf.read_text(encoding="utf-8"))  # fence stripped, canonical persisted

    def test_finalize_stage_file_role_rejects_invalid_status_json(self):
        run_dir, proj, work, sp = self._setup("implementor", [{"type": "main-session"}], json_role=False)
        (work / "implementor-output.json").write_text("not json at all", encoding="utf-8")
        r = run_driver("finalize-stage", "--run", str(run_dir), "--role", "implementor", "--stage-input", str(sp))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("problem", r.stdout)

    def test_run_stage_file_role_handoff(self):
        # A file-role (implementor) handoff: no JSON-role output directive ("valid
        # JSON object" phrasing), but as of 6.5.0 it DOES get an output_file + a
        # distinct status-JSON directive (optional signal alongside its code
        # delta) — see test_run_stage_implementor_handoff_status_directive below
        # for that behavior specifically.
        run_dir, proj, work, sp = self._setup("implementor", [{"type": "subagent", "model": "x"}], json_role=False)
        r = run_driver("run-stage", "--run", str(run_dir), "--role", "implementor", "--stage-input", str(sp))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.json["mode"], "subagent")
        self.assertEqual(r.json["category"], "file")
        self.assertIsNotNone(r.json["output_file"])
        u = pathlib.Path(r.json["user_file"]).read_text(encoding="utf-8")
        self.assertIn("absolute project root", u)
        self.assertNotIn("valid JSON object", u)  # no json-role output directive for a file role

    def test_run_stage_implementor_handoff_status_directive(self):
        # 6.5.0: implementor/test_implementor get a status-JSON directive distinct
        # from a json role's ("write it yourself," never a stdout-capture
        # instruction), plus the same stale-output cleanup a json role's handoff
        # gets — a leftover file from a prior attempt must not survive to be
        # mistaken for this attempt's result.
        run_dir, proj, work, sp = self._setup("implementor", [{"type": "subagent", "model": "x"}], json_role=False)
        stale = work / "implementor-output.json"
        stale.write_text("STALE")
        r = run_driver("run-stage", "--run", str(run_dir), "--role", "implementor", "--stage-input", str(sp))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.json["output_file"], str(stale))
        self.assertFalse(stale.exists())  # stale status file cleared before handoff
        u = pathlib.Path(r.json["user_file"]).read_text(encoding="utf-8")
        self.assertIn("write a brief status JSON", u)
        self.assertIn(str(stale), u)

    def test_finalize_stage_review_result_needs_no_source(self):
        # review-result no longer carries a `source` field at all (the role can't
        # know its own runner, and nothing downstream consumed it) — a subagent
        # reviewer's source-less result validates and persists as-is.
        run_dir, proj, work, sp = self._setup("reviewer", [{"type": "subagent"}])
        outf = work / "reviewer-output.json"
        outf.write_text(json.dumps(review_result(verdict="approve")), encoding="utf-8")
        r = run_driver("finalize-stage", "--run", str(run_dir), "--role", "reviewer", "--stage-input", str(sp))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("source", json.loads(outf.read_text(encoding="utf-8")))

    def test_finalize_stage_rejects_review_result_with_source(self):
        # Regression guard: if a `source` key reappears (e.g. an old prompt still
        # emits it), the schema's additionalProperties:false must reject it.
        run_dir, proj, work, sp = self._setup("reviewer", [{"type": "subagent"}])
        outf = work / "reviewer-output.json"
        rr = review_result(verdict="approve")
        rr["source"] = "bash-runner"
        outf.write_text(json.dumps(rr), encoding="utf-8")
        r = run_driver("finalize-stage", "--run", str(run_dir), "--role", "reviewer", "--stage-input", str(sp))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("problem", r.stdout)
        self.assertIn("source", r.stdout)  # rejected specifically for the stray key


class TestPlanReview(unittest.TestCase):
    """`driver review-plan` / plan_reviewer: a standalone, opt-in role — no
    run_dir, never part of config_complete, reuses run-stage's runner
    abstraction via a throwaway .dev-pipeline/plan-reviews/<id>/ scaffold."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.proj = pathlib.Path(self._tmp.name) / "proj"
        self.proj.mkdir()
        self.plan = self.proj / "plan.md"
        self.plan.write_text(
            "# Plan: Widget\n\n## Requirements\n- R1. Add a widget.\n\n"
            "## Acceptance Criteria\n- [ ] AC1. Widget exists.\n\n"
            "## Interface\nSome interface.\n",
            encoding="utf-8",
        )
        self.config_path = self.proj / ".dev-pipeline" / "dev-pipeline.config.json"
        self.config_path.parent.mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def _write_config(self, cfg):
        self.config_path.write_text(json.dumps(cfg), encoding="utf-8")

    def test_fresh_bootstrap_has_no_plan_reviewer(self):
        # A freshly bootstrapped config must NOT carry plan_reviewer at all (it is
        # opt-in) — only the four required roles are seeded as 'unconfigured', and
        # config_complete is false purely because of THOSE (unrelated to plan_reviewer).
        r = run_driver("bootstrap-config", "--project", str(self.proj))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(r.json["config_complete"])
        cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertNotIn("plan_reviewer", cfg.get("runners", {}))
        self.assertNotIn("plan_reviewer", cfg.get("llm", {}))

    def test_review_plan_dies_when_plan_reviewer_unconfigured(self):
        self._write_config(valid_config())  # no plan_reviewer key at all
        r = run_driver("review-plan", "--plan", str(self.plan), "--config", str(self.config_path))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("runners.plan_reviewer", r.stderr)
        self.assertIn("not configured", r.stderr)
        self.assertFalse((self.proj / ".dev-pipeline" / "plan-reviews").exists())

    def test_review_plan_dies_when_focus_is_placeholder(self):
        cfg = valid_config()
        cfg["runners"]["plan_reviewer"] = [{"type": "bash", "command": "echo x > {output_file}"}]
        cfg["llm"]["plan_reviewer"] = {"focus": "<REQUIRED: fill this in>"}
        self._write_config(cfg)
        r = run_driver("review-plan", "--plan", str(self.plan), "--config", str(self.config_path))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("llm.plan_reviewer.focus", r.stderr)
        self.assertIn("placeholder", r.stderr)

    def test_review_plan_dies_on_unknown_placeholder_in_command(self):
        # Regression: review-plan's readiness gate must catch the same
        # unknown-{placeholder} typo class validate-config/apply-config already
        # do for every other role, instead of failing opaquely at runtime.
        cfg = valid_config()
        cfg["runners"]["plan_reviewer"] = [{"type": "bash", "command": "echo {sytem_file} > {output_file}"}]
        cfg["llm"]["plan_reviewer"] = {"focus": "Be adversarial."}
        self._write_config(cfg)
        r = run_driver("review-plan", "--plan", str(self.plan), "--config", str(self.config_path))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("unknown placeholder", r.stderr)
        self.assertIn("sytem_file", r.stderr)

    def test_apply_config_plan_reviewer_only_write_succeeds_on_unconfigured_project(self):
        # Regression: a plan_reviewer-only values file must apply successfully
        # even while the four required roles are still the bootstrap
        # 'unconfigured' sentinel (states/plan_review.md's documented first-use
        # flow: "their other roles, if any, are untouched"). Before the fix,
        # apply-config validated the WHOLE merged config and died on the
        # unrelated unconfigured roles.
        r = run_driver("bootstrap-config", "--project", str(self.proj))
        self.assertFalse(r.json["config_complete"])

        payload = shlex.quote(json.dumps(plan_review_result()))
        values = self.proj / "values.json"
        values.write_text(json.dumps({
            "llm": {"plan_reviewer": {"focus": "Adversarially review plan.md."}},
            "runners": {"plan_reviewer": [{"type": "bash",
                                            "command": f"echo {payload} > " + "{output_file}",
                                            "normalizer": "default"}]},
        }), encoding="utf-8")
        ac = run_driver("apply-config", "--config", str(self.config_path), "--values-file", str(values))
        self.assertEqual(ac.returncode, 0, ac.stderr)
        # The rest of the config is still incomplete, so config_complete must
        # honestly report False here — a plan_reviewer-only write does not
        # magically finish the pipeline config.
        self.assertFalse(ac.json["config_complete"])

        cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(cfg["llm"]["plan_reviewer"]["focus"], "Adversarially review plan.md.")
        for role in ("implementor", "test_implementor", "tester", "reviewer"):
            self.assertEqual(cfg["runners"][role], [{"type": "unconfigured"}])

        # review-plan itself must now be usable despite the rest being unconfigured.
        rp = run_driver("review-plan", "--plan", str(self.plan), "--config", str(self.config_path))
        self.assertEqual(rp.returncode, 0, rp.stderr)

    def test_apply_config_rejects_plan_reviewer_only_write_with_bad_shape(self):
        # The scoped validation path must still catch a genuinely broken
        # plan_reviewer value — it is narrower, not absent.
        run_driver("bootstrap-config", "--project", str(self.proj))
        values = self.proj / "values.json"
        values.write_text(json.dumps({
            "llm": {"plan_reviewer": {"focus": "<REQUIRED: fill this in>"}},
            "runners": {"plan_reviewer": [{"type": "bash", "command": "echo x > {output_file}"}]},
        }), encoding="utf-8")
        ac = run_driver("apply-config", "--config", str(self.config_path), "--values-file", str(values))
        self.assertNotEqual(ac.returncode, 0)
        self.assertIn("llm.plan_reviewer.focus", ac.stderr)
        self.assertFalse(self.config_path.exists() and
                          "plan_reviewer" in json.loads(self.config_path.read_text(encoding="utf-8")).get("llm", {}),
                          "a rejected merge must not have been written")

    def test_apply_config_mixed_write_still_fully_validates(self):
        # A values file touching plan_reviewer ALONGSIDE another role must NOT
        # take the scoped path — it should still be held to the full
        # validate_config_data check (only a plan_reviewer-ONLY write is scoped).
        run_driver("bootstrap-config", "--project", str(self.proj))
        values = self.proj / "values.json"
        values.write_text(json.dumps({
            "llm": {"plan_reviewer": {"focus": "Be adversarial."}},
            "runners": {
                "plan_reviewer": [{"type": "bash", "command": "echo x > {output_file}", "normalizer": "default"}],
                "implementor": [{"type": "bash", "command": "echo x"}],
            },
        }), encoding="utf-8")
        ac = run_driver("apply-config", "--config", str(self.config_path), "--values-file", str(values))
        # Still fails: test_implementor/tester/reviewer remain unconfigured, and
        # this write is NOT plan_reviewer-only, so the full check must apply.
        self.assertNotEqual(ac.returncode, 0)
        self.assertIn("not configured yet", ac.stderr)

    def _configured(self, command, normalizer="default"):
        cfg = valid_config()
        cfg["runners"]["plan_reviewer"] = [{"type": "bash", "command": command, "normalizer": normalizer}]
        cfg["llm"]["plan_reviewer"] = {"focus": "Be adversarial about ambiguity and coverage gaps."}
        self._write_config(cfg)
        return cfg

    def test_review_plan_bash_runner_end_to_end(self):
        payload = json.dumps(plan_review_result(
            verdict="needs-revision",
            findings=[{"severity": "high", "title": "Vague AC", "body": "AC1 lacks a concrete effect.",
                       "section": "Acceptance Criteria", "confidence": 0.9,
                       "recommendation": "Rewrite AC1 with a concrete input/output."}],
        ))
        self._configured("echo " + shlex.quote(payload) + " > {output_file}")
        r = run_driver("review-plan", "--plan", str(self.plan), "--config", str(self.config_path))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.json["ok"])
        self.assertEqual(r.json["role"], "plan_reviewer")

        reviews = list((self.proj / ".dev-pipeline" / "plan-reviews").iterdir())
        self.assertEqual(len(reviews), 1)
        review_dir = reviews[0]
        result = json.loads((review_dir / "plan-review-result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["verdict"], "needs-revision")
        self.assertEqual(result["findings"][0]["section"], "Acceptance Criteria")

        # plan_reviewer is read-only: the plan file itself must be untouched, and
        # nothing was created under runs/ (this is not a pipeline run).
        self.assertEqual(self.plan.read_text(encoding="utf-8").count("AC1"), 1)
        self.assertFalse((self.proj / ".dev-pipeline" / "runs").exists())

        # The assembled prompt names plan_path explicitly (dp-plan-reviewer.md
        # Step 1 relies on this exact input key) and carries the configured focus.
        user_text = (review_dir / "plan_reviewer-user.txt").read_text(encoding="utf-8")
        self.assertIn(str(self.plan), user_text)
        self.assertIn("adversarial about ambiguity", user_text)

    def test_review_plan_missing_plan_file_dies(self):
        self._configured("echo x > {output_file}")
        r = run_driver("review-plan", "--plan", str(self.proj / "nope.md"), "--config", str(self.config_path))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Plan file not found", r.stderr)

    def test_review_plan_subagent_handoff(self):
        cfg = valid_config()
        cfg["runners"]["plan_reviewer"] = [{"type": "subagent"}]
        cfg["llm"]["plan_reviewer"] = {"focus": "Be adversarial."}
        self._write_config(cfg)
        r = run_driver("review-plan", "--plan", str(self.plan), "--config", str(self.config_path))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.json["mode"], "subagent")
        self.assertEqual(r.json["role"], "plan_reviewer")
        self.assertTrue(pathlib.Path(r.json["system_file"]).exists())

        # finalize-stage validates the handoff result the same way as any other
        # json role, against the review dir run-stage created.
        review_dir = pathlib.Path(r.json["system_file"]).parent
        out = pathlib.Path(r.json["output_file"])
        out.write_text(json.dumps(plan_review_result(verdict="approve")), encoding="utf-8")
        fr = run_driver("finalize-stage", "--run", str(review_dir), "--role", "plan_reviewer",
                         "--stage-input", "stage-input.json")
        self.assertEqual(fr.returncode, 0, fr.stderr)
        self.assertTrue(fr.json["ok"])

    def test_adding_plan_reviewer_does_not_affect_config_complete(self):
        # A fully-configured project (the 4 required roles) must stay
        # config_complete both before and after plan_reviewer is added.
        self._write_config(valid_config())
        before = run_driver("bootstrap-config", "--project", str(self.proj))
        self.assertTrue(before.json["config_complete"])

        values = self.proj / "values.json"
        values.write_text(json.dumps({
            "llm": {"plan_reviewer": {"focus": "Be adversarial about ambiguity."}},
            "runners": {"plan_reviewer": [{"type": "bash", "command": "echo x > {output_file}",
                                            "normalizer": "default"}]},
        }), encoding="utf-8")
        ac = run_driver("apply-config", "--config", str(self.config_path), "--values-file", str(values))
        self.assertEqual(ac.returncode, 0, ac.stderr)
        self.assertTrue(ac.json["config_complete"])

        after = run_driver("bootstrap-config", "--project", str(self.proj))
        self.assertTrue(after.json["config_complete"])

    def test_validate_result_plan_review_round_trip(self):
        good = self.proj / "good.json"
        good.write_text(json.dumps(plan_review_result()), encoding="utf-8")
        r = run_driver("validate-result", "--type", "plan_review", "--file", str(good))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.json["valid"])

        bad = self.proj / "bad.json"
        bad.write_text(json.dumps({"verdict": "bogus", "summary": "x", "findings": []}), encoding="utf-8")
        r2 = run_driver("validate-result", "--type", "plan_review", "--file", str(bad))
        self.assertNotEqual(r2.returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
