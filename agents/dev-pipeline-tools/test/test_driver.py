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

import importlib.util
import json
import pathlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
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
    "tester":           [{"type": "bash", "command": "echo x > {output_file}", "normalizer": "claude-cli"}],
    "reviewer":         [{"type": "bash", "command": "echo x > {output_file}", "normalizer": "claude-cli"}],
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


def review_result(verdict="approve", findings=None, **extra):
    """Build a schema-valid review-result dict."""
    obj = {
        "verdict": verdict,
        "summary": "stub review result",
        "findings": findings if findings is not None else [],
        "next_steps": [],
        "source": "bash-runner",
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

    def __init__(self, config):
        self._tmp = tempfile.TemporaryDirectory()
        self.project = pathlib.Path(self._tmp.name)
        self._config = config
        self.run_dir = None
        self.contract_path = None

    def close(self):
        self._tmp.cleanup()

    # -- setup -------------------------------------------------------------

    def init(self, tdd=None, plan_text=None):
        """Run driver init (expecting success). `tdd` (if given) is baked into the
        config's driver.tdd_mode — the per-run --tdd/--no-tdd flags were removed in
        5.0.0. `plan_text` overrides the default valid plan body."""
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
        proc = run_driver(*args)
        assert proc.returncode == 0, f"init failed: {proc.stderr}"
        self.run_dir = pathlib.Path(proc.json["run_dir"])
        self.contract_path = pathlib.Path(proc.json["contract_path"])
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
            json.dumps({"verdict": "ok", "summary": "x",
                        "findings": [], "next_steps": [], "source": "x"}),
        )
        r = p.advance()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("schema violation", r.stderr)

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
        self.assertEqual(state["max"]["test"], 3)
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


class TestNormalizeReview(PipelineTestCase):
    def _normalize(self, raw):
        in_tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(raw, in_tmp)
        in_tmp.close()
        out_path = in_tmp.name + ".out"
        r = run_driver("normalize-review", "--source", "codex",
                       "--in", in_tmp.name, "--out", out_path)
        return r, out_path

    def test_happy_mapping(self):
        raw = {
            "codex": {"status": 0},
            "result": {
                "verdict": "needs-attention",
                "summary": "found issues",
                "findings": [
                    {"severity": "high", "title": "Bug", "body": "details",
                     "file": "a.py", "line_start": 3, "line_end": 4,
                     "confidence": 0.8, "recommendation": "fix"},
                ],
                "next_steps": ["do x"],
            },
        }
        r, out_path = self._normalize(raw)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.json["verdict"], "needs-attention")
        self.assertEqual(r.json["findings_count"], 1)

        out = json.loads(pathlib.Path(out_path).read_text())
        self.assertEqual(out["source"], "codex-adversarial-review")
        self.assertEqual(out["findings"][0]["severity"], "high")

    def test_confidence_clamped_and_defaulted(self):
        raw = {
            "codex": {"status": 0},
            "result": {
                "verdict": "needs-attention",
                "summary": "s",
                "findings": [
                    {"title": "no conf", "body": "b", "file": "f"},  # conf missing
                    {"title": "over", "body": "b", "file": "f",
                     "confidence": 5.0},  # > 1, clamp to 1
                ],
            },
        }
        r, out_path = self._normalize(raw)
        self.assertEqual(r.returncode, 0)
        out = json.loads(pathlib.Path(out_path).read_text())
        self.assertEqual(out["findings"][0]["confidence"], 0.5)
        self.assertEqual(out["findings"][1]["confidence"], 1.0)
        # missing severity defaults to low
        self.assertEqual(out["findings"][0]["severity"], "low")

    def test_parse_error_dies(self):
        r, _ = self._normalize({"parseError": "bad json from codex"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("parseError", r.stderr)

    def test_nonzero_codex_status_dies(self):
        r, _ = self._normalize({"codex": {"status": 1}, "result": {}})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("non-zero", r.stderr)

    def test_missing_result_dies(self):
        r, _ = self._normalize({"codex": {"status": 0}})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("result", r.stderr)

    def test_invalid_verdict_dies(self):
        r, _ = self._normalize({
            "codex": {"status": 0},
            "result": {"verdict": "lgtm", "summary": "s"},
        })
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("verdict", r.stderr)


class TestAppendAttempt(PipelineTestCase):
    def test_append_replaces_placeholder_and_accumulates(self):
        p = self.make_pipeline()
        p.init()
        attempts = p.run_dir / "attempts.md"
        self.assertIn("_No attempts recorded yet._",
                      attempts.read_text(encoding="utf-8"))

        r = run_driver("append-attempt", "--run", str(p.run_dir),
                       "--state", "test", "--outcome", "first failure")
        self.assertEqual(r.returncode, 0)
        body = attempts.read_text(encoding="utf-8")
        self.assertNotIn("_No attempts recorded yet._", body)
        self.assertIn("first failure", body)

        run_driver("append-attempt", "--run", str(p.run_dir),
                   "--state", "review", "--outcome", "second failure")
        body = attempts.read_text(encoding="utf-8")
        self.assertIn("first failure", body)
        self.assertIn("second failure", body)

    def test_empty_outcome_rejected(self):
        p = self.make_pipeline()
        p.init()
        r = run_driver("append-attempt", "--run", str(p.run_dir),
                       "--state", "test", "--outcome", "   ")
        self.assertNotEqual(r.returncode, 0)


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
            "reviewer": [{"type": "bash", "command": "true > {output_file}", "normalizer": "claude-cli"}],
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
        r2 = p.advance()  # test_implementation -> red_test
        self.assertEqual(r2.json["next_state"], "red_test")
        self.assertEqual(r2.json["directive"], "run_tester")
        self.assertEqual(r2.json["result_filename"], "red-test-result.json")
        return r2

    def _to_review(self, p):
        self._to_red_test(p)
        p.write_red_test_result(status="fail", failure_type="code")
        self.assertEqual(p.advance().json["next_state"], "implementation")  # RED confirmed
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
        # repair: test_implementation -> test (green re-run, NOT implementation)
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
        self.assertEqual(p.advance().json["next_state"], "test")  # repair: re-run GREEN
        # The tightened test now fails against the existing implementation.
        p.write_test_result(status="fail", failure_type="code")
        r = p.advance()
        self.assertEqual(r.json["next_state"], "implementation")
        self.assertEqual(r.json["directive"], "run_implementor")

    def test_default_max_test_implementation_iteration_is_two(self):
        # When the key is omitted from config, the driver default applies.
        cfg = valid_config(tdd_mode=True)
        cfg["driver"].pop("max_test_implementation_iteration", None)
        p = Pipeline(cfg)
        self._pipelines.append(p)
        p.init()
        state = json.loads((p.run_dir / "state.json").read_text())
        self.assertEqual(state["max"]["test_implementation"], 2)

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


class TestUpgradeSafety(PipelineTestCase):
    """A run created by an older driver (no tdd_mode/red_phase/test_implementation
    keys) must resume without crashing under the new driver."""

    def test_legacy_state_resume_does_not_crash(self):
        p = self.started()  # modern init
        p.advance()  # init -> implementation
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
        p.advance()  # implementation -> test  (tester; iter_dir echoed)
        si = json.loads((p.run_dir / "iterations" / "0" / "stage-input.json").read_text(encoding="utf-8"))
        self.assertEqual(si["role"], "tester")
        self.assertTrue(si["output_file"].endswith("test-result.json"))
        self.assertIn("build_instruction", si["inputs"])
        self.assertNotIn("directive", si["inputs"])  # control keys excluded
        self.assertNotIn("tester_runners", si["inputs"])  # M1: runner arrays never reach the prompt


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
        cfg["runners"]["implementor"] = [{"type": "bash", "command": "true"}]
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
        # 1st runner fails (exit 1), 2nd writes a file and succeeds.
        self._cfg("implementor", [
            {"type": "bash", "command": "exit 1"},
            {"type": "bash", "command": "echo x > {project_root}/made.py"},
        ])
        r = self._run("implementor", self._si("implementor"))
        self.assertEqual(r.returncode, 0)
        self.assertTrue(r.json["ok"])
        self.assertEqual(r.json["runner"], 1)
        self.assertTrue((self.proj / "made.py").exists())

    def test_prompt_prose_resolved_from_layout(self):
        # Guards role_prompt_path + the .agents/skills/dev-pipeline/agents/ layout:
        # if resolution broke, the role would silently run on the "You are the
        # <role>." stub and emit a stderr WARNING. Assert the real prose is found
        # and assembled instead.
        self._cfg("implementor", [{"type": "bash", "command": "true"}])
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
             "normalizer": "claude-cli"},
        ])
        out = self.run_dir / "tr.json"
        r = self._run("tester", self._si("tester", output_file=str(out)))
        self.assertEqual(r.returncode, 0)
        self.assertTrue(r.json["ok"])
        # The persisted file must be plain JSON — no fence — so json.loads succeeds.
        text = out.read_text(encoding="utf-8")
        self.assertNotIn("```", text)
        self.assertEqual(json.loads(text)["status"], "pass")

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


class TestConfigMigration(unittest.TestCase):
    """3.0.0: validate-config rejects removed runner types with a migration hint;
    migrate-config converts a pre-3.0.0 config to bash runners."""

    def _write(self, cfg):
        self._tmp = tempfile.TemporaryDirectory()
        p = pathlib.Path(self._tmp.name) / "config.json"
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
                      "tester": [{"type": "subagent", "model": "sonnet", "normalizer": "claude-cli"}]})
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

    def test_finalize_stage_normalizes_fenced_json(self):
        run_dir, proj, work, sp = self._setup("tester", [{"type": "subagent", "normalizer": "claude-cli"}])
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

    def test_finalize_stage_file_role_is_noop(self):
        run_dir, proj, work, sp = self._setup("implementor", [{"type": "main-session"}], json_role=False)
        r = run_driver("finalize-stage", "--run", str(run_dir), "--role", "implementor", "--stage-input", str(sp))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.json["ok"])
        self.assertIn("file role", r.json["note"])

    def test_run_stage_file_role_handoff(self):
        # A file-role (implementor) handoff: no output_file, no json output directive.
        run_dir, proj, work, sp = self._setup("implementor", [{"type": "subagent", "model": "x"}], json_role=False)
        r = run_driver("run-stage", "--run", str(run_dir), "--role", "implementor", "--stage-input", str(sp))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.json["mode"], "subagent")
        self.assertEqual(r.json["category"], "file")
        self.assertIsNone(r.json["output_file"])
        u = pathlib.Path(r.json["user_file"]).read_text(encoding="utf-8")
        self.assertIn("absolute project root", u)
        self.assertNotIn("valid JSON object", u)  # no json output directive for a file role

    def test_finalize_stage_stamps_true_review_source(self):
        # The driver overwrites a review's `source` with the true execution mode —
        # the role can't know its own runner (so a self-reported value is corrected).
        run_dir, proj, work, sp = self._setup("reviewer", [{"type": "subagent"}])
        outf = work / "reviewer-output.json"
        rr = review_result(verdict="approve")
        rr["source"] = "bash-runner"  # the role's guess (wrong under a subagent)
        outf.write_text(json.dumps(rr), encoding="utf-8")
        r = run_driver("finalize-stage", "--run", str(run_dir), "--role", "reviewer", "--stage-input", str(sp))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(outf.read_text(encoding="utf-8"))["source"], "host-subagent")


if __name__ == "__main__":
    unittest.main(verbosity=2)
