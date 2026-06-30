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

import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
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


def valid_config(**driver_overrides):
    """Return a schema-valid config dict with real (non-placeholder) instructions.

    Defaults to tdd_mode=False so the legacy-flow suites read unchanged; TDD
    tests opt in with valid_config(tdd_mode=True). The test_implementor block is
    filled with real values so it validates whenever TDD is enabled.
    `driver_overrides` patch the `driver` block per test.
    """
    cfg = json.loads(CONFIG_EXAMPLE.read_text(encoding="utf-8"))
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
        "source": "claude-subagent",
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


class Pipeline:
    """Drives a single pipeline run inside a temporary project directory.

    Encapsulates the init → write spec → advance → write result loop so each
    test reads as a short sequence of transitions and assertions.
    """

    def __init__(self, config):
        self._tmp = tempfile.TemporaryDirectory()
        self.project = pathlib.Path(self._tmp.name)
        self._config = config
        self.run_dir = None
        self.spec_path = None

    def close(self):
        self._tmp.cleanup()

    # -- setup -------------------------------------------------------------

    def init(self, tdd=None):
        plan = self.project / "plan.md"
        plan.write_text("# Plan\n\nDo the thing.\n", encoding="utf-8")
        cfg_dir = self.project / ".dev-pipeline"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        cfg_path = cfg_dir / "dev-pipeline.config.json"
        cfg_path.write_text(json.dumps(self._config), encoding="utf-8")

        extra = []
        if tdd is True:
            extra = ["--tdd"]
        elif tdd is False:
            extra = ["--no-tdd"]
        proc = run_driver(
            "init",
            "--plan", str(plan),
            "--config", str(cfg_path),
            "--project", str(self.project),
            *extra,
        )
        assert proc.returncode == 0, f"init failed: {proc.stderr}"
        self.run_dir = pathlib.Path(proc.json["run_dir"])
        self.spec_path = pathlib.Path(proc.json["spec_path"])
        return proc.json

    def write_spec(self):
        self.spec_path.write_text("# Spec\n\nThe spec.\n", encoding="utf-8")

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
        """Init + write spec, leaving the run in `init` state ready to advance."""
        p = self.make_pipeline(**driver_overrides)
        p.init()
        p.write_spec()
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

    def test_init_without_spec_dies(self):
        p = self.make_pipeline()
        p.init()  # note: no write_spec()
        r = p.advance()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("spec.md", r.stderr)

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

    def test_seeded_config_validates_after_filling_instructions(self):
        proj = self._tmp_project(git=False)
        run_driver("bootstrap-config", "--project", str(proj))
        config = proj / ".dev-pipeline" / "dev-pipeline.config.json"

        # The template ships with placeholder tester instructions, so it must
        # NOT validate until the user fills them in.
        r_before = run_driver("validate-config", "--config", str(config))
        self.assertNotEqual(r_before.returncode, 0)

        cfg = json.loads(config.read_text(encoding="utf-8"))
        cfg["llm"]["tester"]["build_instruction"] = "no build step"
        cfg["llm"]["tester"]["install_instruction"] = "no install step"
        cfg["llm"]["tester"]["test_instruction"] = "pytest"
        # TDD ships on by default, so the test_implementor block must be filled too.
        cfg["llm"]["test_implementor"]["framework_instruction"] = "pytest under tests/"
        cfg["llm"]["test_implementor"]["test_paths"] = ["tests/**"]
        config.write_text(json.dumps(cfg), encoding="utf-8")

        r_after = run_driver("validate-config", "--config", str(config))
        self.assertEqual(r_after.returncode, 0)
        self.assertTrue(r_after.json["valid"])


class TestTDD(PipelineTestCase):
    """TDD flow: init → test_implementation → red_test → implementation → test → review."""

    def started_tdd(self, **driver_overrides):
        driver_overrides.setdefault("tdd_mode", True)
        p = self.make_pipeline(**driver_overrides)
        p.init()
        p.write_spec()
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

    def test_no_tdd_flag_overrides_config_to_legacy(self):
        # Config says tdd_mode true, but --no-tdd forces the legacy flow.
        p = self.make_pipeline(tdd_mode=True)
        p.init(tdd=False)
        p.write_spec()
        r = p.advance()
        self.assertEqual(r.json["next_state"], "implementation")
        self.assertEqual(r.json["directive"], "run_implementor")

    def test_tdd_flag_overrides_config_to_tdd(self):
        # Config says tdd_mode false (valid_config default), but --tdd forces TDD.
        p = self.make_pipeline()
        p.init(tdd=True)
        p.write_spec()
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
        # ...but the same config validates under --no-tdd.
        r2 = run_driver("validate-config", "--config", tmp.name, "--no-tdd")
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
        p.write_spec()
        r1 = p.advance()  # init -> implementation
        j = r1.json
        self.assertEqual(j["next_state"], "implementation")
        self.assertFalse(j["tdd_mode"])
        self.assertTrue(j["design_instruction"])
        self.assertEqual(j["implementor_runners"][0]["agent"], "dp-implementor")
        # The implementor build-checks before handoff → it gets the tester's build cmd.
        self.assertEqual(j["build_instruction"], "make build")
        self.assertNotIn("test_paths", j)  # legacy: no test boundary

        r2 = p.advance()  # implementation -> test
        self.assertEqual(r2.json["next_state"], "test")
        self.assertFalse(r2.json["tdd_mode"])
        self.assertEqual(r2.json["tester_runners"][0]["agent"], "dp-tester")

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
        self.assertEqual(r1.json["test_implementor_runners"][0]["agent"],
                         "dp-test-implementor")

        r2 = p.advance()  # test_implementation -> red_test (red phase)
        self.assertEqual(r2.json["next_state"], "red_test")
        self.assertTrue(r2.json["tdd_mode"])
        self.assertEqual(r2.json["tester_runners"][0]["agent"], "dp-tester")

        p.write_red_test_result(status="fail", failure_type="code")
        r3 = p.advance()  # red confirmed -> implementation
        self.assertEqual(r3.json["next_state"], "implementation")
        self.assertTrue(r3.json["tdd_mode"])
        self.assertTrue(r3.json["design_instruction"])
        self.assertEqual(r3.json["implementor_runners"][0]["agent"], "dp-implementor")
        self.assertEqual(r3.json["build_instruction"], "no build step")
        self.assertEqual(r3.json["test_paths"], ["tests/**"])  # tdd echoes the boundary

    def test_no_tdd_override_echoes_frozen_false(self):
        # config says tdd_mode=True, but --no-tdd freezes state.tdd_mode=False.
        # The echo must reflect the frozen value, never config.driver.tdd_mode.
        p = self.make_pipeline(tdd_mode=True)
        p.init(tdd=False)
        p.write_spec()
        # Make the divergence explicit: the snapshot still says tdd_mode=true,
        # but the frozen state (and therefore the echo) must be false.
        snap = json.loads((p.run_dir / "config.snapshot.json").read_text(encoding="utf-8"))
        self.assertTrue(snap["driver"]["tdd_mode"])
        r1 = p.advance()
        self.assertEqual(r1.json["next_state"], "implementation")  # legacy path
        self.assertFalse(r1.json["tdd_mode"])


class TestStageInputWiring(PipelineTestCase):
    """init/advance persist a stage-input.json so `driver run-stage` can consume
    the same context the SKILL echo carries (additive; legacy flow unaffected)."""

    def test_init_writes_spec_stage_input(self):
        p = self.started(tdd_mode=False)
        si = json.loads((p.run_dir / "stage-input.json").read_text(encoding="utf-8"))
        self.assertEqual(si["role"], "spec_author")
        self.assertTrue(si["output_file"].endswith("spec.md"))
        self.assertIn("## Requirements", si["required_sections"])
        self.assertEqual(si["inputs"]["tdd_mode"], False)

    def test_advance_writes_tester_stage_input(self):
        p = self.started(tdd_mode=False)
        p.advance()  # init -> implementation (no iter_dir echoed yet)
        p.advance()  # implementation -> test  (tester; iter_dir echoed)
        si = json.loads((p.run_dir / "iterations" / "0" / "stage-input.json").read_text(encoding="utf-8"))
        self.assertEqual(si["role"], "tester")
        self.assertTrue(si["output_file"].endswith("test-result.json"))
        self.assertIn("build_instruction", si["inputs"])
        self.assertNotIn("directive", si["inputs"])  # control keys excluded


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

    def test_named_role_sections_present(self):
        self._cfg("spec_author", [
            {"type": "bash",
             "command": "printf '## Requirements\\n## Acceptance Criteria\\n' > {output_file}"},
        ])
        out = self.proj / "spec.md"
        r = self._run("spec_author", self._si(
            "spec_author", output_file=str(out),
            required_sections=["## Requirements", "## Acceptance Criteria"]))
        self.assertTrue(r.json["ok"])

    def test_named_role_missing_section_fails(self):
        self._cfg("spec_author", [
            {"type": "bash", "command": "printf '## Requirements\\n' > {output_file}"},
        ])
        out = self.proj / "spec.md"
        r = self._run("spec_author", self._si(
            "spec_author", output_file=str(out),
            required_sections=["## Requirements", "## Acceptance Criteria"]))
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(json.loads(r.stdout)["ok"])

    def test_named_role_insufficient_marker(self):
        self._cfg("spec_author", [
            {"type": "bash", "command": "printf 'INSUFFICIENT: too vague\\n' > {output_file}"},
        ])
        out = self.proj / "spec.md"
        r = self._run("spec_author", self._si(
            "spec_author", output_file=str(out), required_sections=["## Requirements"]))
        self.assertFalse(r.json["ok"])
        self.assertEqual(r.json["reason"], "insufficient")

    def test_empty_runners_errors(self):
        self._cfg("tester", [])
        r = self._run("tester", self._si("tester", output_file=str(self.run_dir / "o.json")))
        self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
