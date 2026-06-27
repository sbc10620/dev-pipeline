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
    """Return a schema-valid config dict with real (non-placeholder) tester
    instructions. `driver_overrides` patch the `driver` block per test."""
    cfg = json.loads(CONFIG_EXAMPLE.read_text(encoding="utf-8"))
    cfg["llm"]["tester"] = {
        "build_instruction": "no build step",
        "install_instruction": "no install step",
        "test_instruction": "no test step",
    }
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


def finding(severity="critical", title="Stub finding"):
    """Build a schema-valid review finding."""
    return {
        "severity": severity,
        "title": title,
        "body": "stub body",
        "file": "src/foo.py",
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

    def init(self):
        plan = self.project / "plan.md"
        plan.write_text("# Plan\n\nDo the thing.\n", encoding="utf-8")
        cfg_dir = self.project / ".dev-pipeline"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        cfg_path = cfg_dir / "dev-pipeline.config.json"
        cfg_path.write_text(json.dumps(self._config), encoding="utf-8")

        proc = run_driver(
            "init",
            "--plan", str(plan),
            "--config", str(cfg_path),
            "--project", str(self.project),
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
        # Mirrors driver.get_iter_path: n = test counter + review counter.
        st = self.status()
        n = st["iterations"]["test"] + st["iterations"]["review"]
        return self.run_dir / "iterations" / str(n)

    def write_test_result(self, **kwargs):
        d = self._current_iter_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / "test-result.json").write_text(
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
        self.assertEqual(r.json["iterations"], {"test": 0, "review": 0})
        self.assertEqual(
            r.json["iter_dir"], test_iter_dir,
            "review must reuse the same iteration dir as a passing test",
        )

        # review(approve) -> done
        p.write_review_result(verdict="approve")
        r = p.advance()
        self.assertEqual(r.json["next_state"], "done")
        self.assertEqual(r.json["directive"], "finalize")
        self.assertEqual(r.json["iterations"], {"test": 0, "review": 0})


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
        self.assertEqual(state["iterations"], {"test": 0, "review": 0})
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
