#!/usr/bin/env python3
"""
dev-pipeline driver — deterministic state machine for the implement→test→review loop.

Usage:
  python3 driver.py bootstrap-config [--project <dir>]
  python3 driver.py init             --plan <path> [--config <path>] [--project <dir>] [--tdd|--no-tdd]
  python3 driver.py advance          --run <run_dir>
  python3 driver.py status           --run <run_dir>
  python3 driver.py validate-config  --config <path> [--tdd|--no-tdd]
  python3 driver.py validate-result  --type test|review --file <path>
  python3 driver.py normalize-review --source codex --in <file> --out <file>
  python3 driver.py append-attempt   --run <run_dir> --state <test_implementation|red_test|test|review> --outcome <text-or-file>
  python3 driver.py check-boundary   --run <run_dir> --role <test_implementation|implementation> --changed <file...>
  python3 driver.py record-changes   --run <run_dir> --changed <file...>
  python3 driver.py --version
  python3 driver.py --help
"""

import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Single source of truth for the dev-pipeline version. driver.py is the only
# executable copied into installs, so install.sh and state.json read this value
# rather than maintaining their own copy.
__version__ = "2.2.0"

SCHEMA_DIR = pathlib.Path(__file__).parent / "schemas"
# Config template, co-located with driver.py (install.sh copies it next to this
# file). Resolved the same way as SCHEMA_DIR so an installed copy is standalone.
EXAMPLE_PATH = pathlib.Path(__file__).parent / "config.example.json"
SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}
VALID_SEVERITIES = set(SEVERITY_RANK)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_id_new() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def emit(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def die(message: str, code: int = 1) -> None:
    sys.stderr.write(f"[dev-pipeline] ERROR: {message}\n")
    sys.exit(code)


def load_json(path: pathlib.Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        die(f"Invalid JSON in {path}: {e}")
    except FileNotFoundError:
        die(f"File not found: {path}")


def save_json(path: pathlib.Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Lightweight schema validator (no external deps)
# ---------------------------------------------------------------------------

def _validate(data, schema: dict, path: str = "", root_schema: dict = None) -> list[str]:
    """Return list of violation messages."""
    errors = []

    # Resolve $ref before anything else
    if "$ref" in schema:
        ref = schema["$ref"]
        resolved = _resolve_ref(ref, root_schema or schema)
        if resolved is not None:
            return _validate(data, resolved, path, root_schema)
        # Unresolvable $ref — skip silently rather than crash

    root_schema = root_schema or schema
    t = schema.get("type")

    # type check (allow arrays of types for oneOf-style null support)
    if t:
        types = t if isinstance(t, list) else [t]
        type_map = {
            "object": dict, "array": list, "string": str,
            "integer": int, "number": (int, float), "boolean": bool, "null": type(None),
        }
        allowed = tuple(type_map[x] for x in types if x in type_map)
        if not isinstance(data, allowed):
            errors.append(f"{path or 'root'}: expected {t}, got {type(data).__name__}")
            return errors

    if t == "object" or isinstance(data, dict):
        for req in schema.get("required", []):
            if req not in data:
                errors.append(f"{path}.{req}: required field missing")
        props = schema.get("properties", {})
        for k, v in data.items():
            if k in props:
                errors.extend(_validate(v, props[k], f"{path}.{k}", root_schema))
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}.{k}: unexpected field")

    if t == "array" or isinstance(data, list):
        item_schema = schema.get("items")
        if item_schema:
            for i, item in enumerate(data):
                errors.extend(_validate(item, item_schema, f"{path}[{i}]", root_schema))
        mn = schema.get("minItems")
        if mn is not None and len(data) < mn:
            errors.append(f"{path}: minItems {mn}, got {len(data)}")

    if t == "string" or isinstance(data, str):
        mn = schema.get("minLength")
        if mn is not None and len(data) < mn:
            errors.append(f"{path}: minLength {mn}, got {len(data)}")
        enum = schema.get("enum")
        if enum is not None and data not in enum:
            errors.append(f"{path}: must be one of {enum}, got {data!r}")

    # numeric constraints — apply when data is int/float regardless of how type is declared
    if isinstance(data, (int, float)) and not isinstance(data, bool):
        types_list = t if isinstance(t, list) else ([t] if t else [])
        if any(x in types_list for x in ("integer", "number")) or not types_list:
            mn = schema.get("minimum")
            if mn is not None and data < mn:
                errors.append(f"{path}: minimum {mn}, got {data}")
            mx = schema.get("maximum")
            if mx is not None and data > mx:
                errors.append(f"{path}: maximum {mx}, got {data}")

    # oneOf: at least one subschema must produce zero errors
    one_of = schema.get("oneOf")
    if one_of:
        match_count = sum(1 for s in one_of if not _validate(data, s, path, root_schema))
        if match_count == 0:
            errors.append(f"{path}: matches none of the oneOf schemas")

    return errors


def _resolve_ref(ref: str, root_schema: dict):
    """Resolve a JSON Schema $ref of the form '#/$defs/Name'."""
    if not ref.startswith("#/"):
        return None
    parts = ref.lstrip("#/").split("/")
    node = root_schema
    for part in parts:
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def validate_against_schema(data: dict, schema_name: str) -> list[str]:
    schema_path = SCHEMA_DIR / schema_name
    if not schema_path.exists():
        return [f"Schema file not found: {schema_path}"]
    schema = load_json(schema_path)
    return _validate(data, schema, root_schema=schema)


# ---------------------------------------------------------------------------
# Config validation (extra business rules beyond schema)
# ---------------------------------------------------------------------------

INSTRUCTION_KEYS = ["build_instruction", "install_instruction", "test_instruction"]


def effective_tdd_mode(cfg: dict, tdd_override=None) -> bool:
    """Resolve TDD mode: per-run override beats config, config beats default(true)."""
    if tdd_override is not None:
        return bool(tdd_override)
    return bool(cfg.get("driver", {}).get("tdd_mode", True))


def _is_placeholder(val) -> bool:
    return isinstance(val, str) and val.strip().startswith("<") and val.strip().endswith(">")


def validate_config_data(cfg: dict, tdd_override=None) -> list[str]:
    errors = validate_against_schema(cfg, "config.schema.json")
    if errors:
        return errors

    tester = cfg.get("llm", {}).get("tester", {})
    for key in INSTRUCTION_KEYS:
        val = tester.get(key, "")
        if not isinstance(val, str) or not val.strip():
            errors.append(f"llm.tester.{key}: must be a non-empty string (use 'no build step' etc. if not needed)")
        elif _is_placeholder(val):
            errors.append(f"llm.tester.{key}: still contains a placeholder value — replace it with a real command")

    rbs = cfg.get("driver", {}).get("review_block_severity")
    if rbs is not None:
        for s in rbs:
            if s not in VALID_SEVERITIES:
                errors.append(f"driver.review_block_severity: unknown severity {s!r}")

    # TDD is opt-out-able. When effectively enabled, the test_implementor block
    # and its runner are mandatory and must not contain placeholders.
    if effective_tdd_mode(cfg, tdd_override):
        ti = cfg.get("llm", {}).get("test_implementor")
        if not ti:
            errors.append(
                "llm.test_implementor: required when tdd_mode is enabled — add it (focus, "
                "framework_instruction, test_paths) or run with --no-tdd / set driver.tdd_mode=false"
            )
        else:
            for key in ("focus", "framework_instruction"):
                val = ti.get(key, "")
                if not isinstance(val, str) or not val.strip():
                    errors.append(f"llm.test_implementor.{key}: must be a non-empty string")
                elif _is_placeholder(val):
                    errors.append(f"llm.test_implementor.{key}: still contains a placeholder value — replace it")
            tp = ti.get("test_paths", [])
            if not isinstance(tp, list) or not tp:
                errors.append("llm.test_implementor.test_paths: must be a non-empty array of globs")
            elif any(_is_placeholder(p) for p in tp):
                errors.append("llm.test_implementor.test_paths: still contains a placeholder value — replace it")
        if not cfg.get("runners", {}).get("test_implementor"):
            errors.append(
                "runners.test_implementor: required when tdd_mode is enabled — add it "
                "or run with --no-tdd / set driver.tdd_mode=false"
            )

    return errors


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def state_path(run_dir: pathlib.Path) -> pathlib.Path:
    return run_dir / "state.json"


def load_state(run_dir: pathlib.Path) -> dict:
    return load_json(state_path(run_dir))


def save_state(run_dir: pathlib.Path, state: dict) -> None:
    state["updated_at"] = now_iso()
    save_json(state_path(run_dir), state)


def get_iter_path(run_dir: pathlib.Path, state: dict) -> pathlib.Path:
    """Compute the current iteration directory path WITHOUT creating it (pure).

    The number is the sum of every retry counter. `.get(..., 0)` keeps this safe
    for runs created before the test_implementation counter existed (legacy
    state.json resumed under a newer driver — see the upgrade contract).
    """
    iters = state["iterations"]
    n = iters.get("test_implementation", 0) + iters.get("test", 0) + iters.get("review", 0)
    return run_dir / "iterations" / str(n)


def ensure_iter_dir(run_dir: pathlib.Path, state: dict) -> pathlib.Path:
    """Compute the current iteration directory path AND create it on disk."""
    d = get_iter_path(run_dir, state)
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Review pass/fail gate
# ---------------------------------------------------------------------------

def review_passes(review_result: dict, review_block_severity) -> bool:
    """Determine if a review result passes the configured gate."""
    if review_block_severity is None:
        return review_result.get("verdict") == "approve"
    block_set = set(review_block_severity)
    findings = review_result.get("findings", [])
    blocking = [f for f in findings if f.get("severity") in block_set]
    return len(blocking) == 0


def blocking_findings(review_result: dict, review_block_severity) -> list:
    """Return the findings that block the review under the configured gate.

    With verdict-based gating (review_block_severity is None) the whole finding
    set is considered "blocking" when the verdict is not approve, since there is
    no per-finding severity to filter on.
    """
    findings = review_result.get("findings", [])
    if review_block_severity is None:
        return [] if review_result.get("verdict") == "approve" else list(findings)
    block_set = set(review_block_severity)
    return [f for f in findings if f.get("severity") in block_set]


# ---------------------------------------------------------------------------
# Role-boundary glob matching (TDD: keep test author and implementor in lane)
# ---------------------------------------------------------------------------

def glob_to_regex(glob: str) -> str:
    """Translate a path glob into an anchored regex with explicit semantics:

      **  → zero or more path segments (matches '/')
      *   → any run of characters except '/'
      ?   → a single character except '/'

    This is deliberately NOT fnmatch (whose '*' also matches '/'), so that
    'tests/**' matches 'tests/a/b_test.py' and '**/*_test.go' matches both
    'foo_test.go' and 'pkg/foo_test.go' the way a developer expects.
    """
    out = []
    i, n = 0, len(glob)
    while i < n:
        c = glob[i]
        if c == "*":
            if i + 1 < n and glob[i + 1] == "*":
                i += 2
                if i < n and glob[i] == "/":
                    out.append("(?:.*/)?")  # '**/' = zero or more leading segments
                    i += 1
                else:
                    out.append(".*")        # trailing '**' = anything incl. '/'
            else:
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return "^" + "".join(out) + "$"


def matches_test_paths(path: str, test_paths: list) -> bool:
    """True if `path` matches any of the configured test_paths globs."""
    return any(re.match(glob_to_regex(g), path) for g in test_paths)


# ---------------------------------------------------------------------------
# Subcommand: init
# ---------------------------------------------------------------------------

def cmd_init(args) -> None:
    plan_path = pathlib.Path(args.plan).resolve()
    if not plan_path.exists():
        die(f"Plan file not found: {plan_path}")

    config_path = pathlib.Path(args.config).resolve() if args.config else (
        pathlib.Path(args.project or ".").resolve() / ".dev-pipeline" / "dev-pipeline.config.json"
    )
    if not config_path.exists():
        die(f"Config file not found: {config_path}\n  Run /dev-pipeline once — it bootstraps .dev-pipeline/dev-pipeline.config.json from the template on first run — then fill in the tester instructions and re-run.")

    cfg = load_json(config_path)
    tdd_mode = effective_tdd_mode(cfg, getattr(args, "tdd", None))
    errors = validate_config_data(cfg, tdd_override=getattr(args, "tdd", None))
    if errors:
        sys.stderr.write("[dev-pipeline] Config validation failed:\n")
        for e in errors:
            sys.stderr.write(f"  - {e}\n")
        sys.stderr.write("\nFix .dev-pipeline/dev-pipeline.config.json and retry.\n")
        sys.exit(1)

    project_dir = pathlib.Path(args.project).resolve() if args.project else pathlib.Path(".").resolve()
    rid = run_id_new()
    run_dir = project_dir / ".dev-pipeline" / "runs" / rid
    run_dir.mkdir(parents=True, exist_ok=True)

    # latest symlink
    latest_link = project_dir / ".dev-pipeline" / "latest"
    if latest_link.is_symlink():
        latest_link.unlink()
    elif latest_link.is_dir():
        die(f"Cannot create 'latest' symlink: {latest_link} is a directory. Remove it manually.")
    elif latest_link.exists():
        latest_link.unlink()
    # Relative target keeps the symlink valid if the project dir is moved/remounted.
    latest_link.symlink_to(pathlib.Path("runs") / rid)

    spec_path = run_dir / "spec.md"

    ts = now_iso()
    state_obj = {
        "run_id": rid,
        "dev_pipeline_version": __version__,
        "state": "init",
        "plan_path": str(plan_path),
        "config_path": str(config_path),
        "spec_path": str(spec_path),
        "project_dir": str(project_dir),
        "tdd_mode": tdd_mode,
        # red_phase is true only while the very first RED verification is pending.
        # It flips to false once red_test confirms RED, so later test fixes
        # (driven by review findings) do not re-impose the failing-test gate.
        "red_phase": tdd_mode,
        "iterations": {"test": 0, "review": 0, "test_implementation": 0},
        "max": {
            "test": cfg["driver"]["max_test_iteration"],
            "review": cfg["driver"]["max_review_iteration"],
            "test_implementation": cfg["driver"].get("max_test_implementation_iteration", 2),
        },
        "halt_reason": None,
        "history": [{"state": "init", "ts": ts, "outcome": "started", "failure_type": None}],
        "started_at": ts,
        "updated_at": ts,
    }
    save_state(run_dir, state_obj)

    # save config snapshot
    save_json(run_dir / "config.snapshot.json", cfg)

    # initialise attempts.md
    (run_dir / "attempts.md").write_text(
        "# Attempt History\n\n_No attempts recorded yet._\n", encoding="utf-8"
    )

    emit({
        "state": "init",
        "run_id": rid,
        "run_dir": str(run_dir),
        "spec_path": str(spec_path),
        "plan_path": str(plan_path),
        "tdd_mode": tdd_mode,
        "next_action": "write_spec",
        "message": "Init successful. Write spec.md at spec_path, then call `driver advance --run <run_dir>`.",
    })


# ---------------------------------------------------------------------------
# Subcommand: advance
# ---------------------------------------------------------------------------

def cmd_advance(args) -> None:
    run_dir = pathlib.Path(args.run).resolve()
    if not run_dir.exists():
        die(f"Run directory not found: {run_dir}")

    state = load_state(run_dir)
    cfg = load_json(run_dir / "config.snapshot.json")
    current = state["state"]
    ts = now_iso()

    tdd = state.get("tdd_mode", False)
    runners = cfg.get("runners", {})

    def dest_echoes(new_state: str) -> dict:
        """Config-derived values each destination state needs to drive itself.

        Echoing these on the advance that lands in a state makes the driver the
        single source: the SKILL never reads config.snapshot.json for control
        flow. All reads use .get(default) so a run created by an older driver
        (whose snapshot predates a key) resumes without a KeyError.
        """
        llm = cfg.get("llm", {})
        e = {}
        if new_state == "implementation":
            e["design_instruction"] = llm.get("implementor", {}).get("design_instruction", "")
            e["implementor_runners"] = runners.get("implementor", [])
            if tdd:
                # The implementor must know which paths are off-limits (test author's).
                e["test_paths"] = llm.get("test_implementor", {}).get("test_paths", [])
        elif new_state == "test_implementation":
            e["test_implementor_runners"] = runners.get("test_implementor", [])
        elif new_state in ("red_test", "test"):
            e["tester_runners"] = runners.get("tester", [])
        elif new_state == "done":
            e["run_self_evolution"] = cfg.get("driver", {}).get("run_self_evolution", False)
        # Note: reviewer has no echo — review.md hardcodes the codex→dp-reviewer
        # order and never consults config.runners.reviewer.
        return e

    def transition(new_state: str, outcome: str, failure_type=None, halt_reason=None, extra: dict = None):
        state["history"].append({
            "state": current,
            "ts": ts,
            "outcome": outcome,
            "failure_type": failure_type,
        })
        state["state"] = new_state
        if halt_reason is not None:
            state["halt_reason"] = halt_reason
        save_state(run_dir, state)
        result = {
            "previous_state": current,
            "next_state": new_state,
            "iterations": state["iterations"],
            "halt_reason": state.get("halt_reason"),
            # tdd_mode is the frozen, authoritative run flag (state.json). Echo it on
            # every advance so a resuming session never recovers it from
            # config.snapshot.json's driver.tdd_mode (wrong under --tdd/--no-tdd).
            "tdd_mode": tdd,
        }
        result.update(dest_echoes(new_state))
        if extra:
            result.update(extra)
        emit(result)

    attempts_path = str(run_dir / "attempts.md")

    # --- init ---
    if current == "init":
        spec_path = pathlib.Path(state["spec_path"])
        if not spec_path.exists():
            die("spec.md has not been written yet. Write it first, then call advance.")
        if state.get("tdd_mode", False):
            transition("test_implementation", "spec_ready",
                       extra={"directive": "run_test_implementor",
                              "spec_path": str(spec_path),
                              "plan_path": state["plan_path"],
                              "attempts_path": attempts_path,
                              "test_implementor_config": cfg["llm"].get("test_implementor", {})})
        else:
            transition("implementation", "spec_ready",
                       extra={"directive": "run_implementor",
                              "spec_path": str(spec_path),
                              "plan_path": state["plan_path"],
                              "attempts_path": attempts_path})

    # --- test_implementation (TDD: author tests, no result file needed) ---
    elif current == "test_implementation":
        iter_dir = ensure_iter_dir(run_dir, state)
        if state.get("red_phase", False):
            # First authoring pass: prove the tests FAIL (RED) before any code.
            transition("red_test", "tests_authored",
                       extra={"directive": "run_tester",
                              "iter_dir": str(iter_dir),
                              "red_test": True,
                              "result_filename": "red-test-result.json",
                              "build_instruction":   cfg["llm"]["tester"]["build_instruction"],
                              "install_instruction": cfg["llm"]["tester"]["install_instruction"],
                              "test_instruction":    cfg["llm"]["tester"]["test_instruction"]})
        else:
            # Repair pass (driven by a review finding about tests): code already
            # exists, so skip RED and re-run the GREEN tester against the fixed
            # tests. If the implementation no longer satisfies the tightened
            # tests, the normal test→implementation retry takes over from there.
            transition("test", "tests_repaired",
                       extra={"directive": "run_tester",
                              "iter_dir": str(iter_dir),
                              "build_instruction":   cfg["llm"]["tester"]["build_instruction"],
                              "install_instruction": cfg["llm"]["tester"]["install_instruction"],
                              "test_instruction":    cfg["llm"]["tester"]["test_instruction"]})

    # --- red_test (TDD: verify the freshly authored tests FAIL) ---
    elif current == "red_test":
        result_file = get_iter_path(run_dir, state) / "red-test-result.json"
        if not result_file.exists():
            die(f"red-test-result.json not found at {result_file}. Write the tester result first.")
        result = load_json(result_file)
        errors = validate_against_schema(result, "test-result.schema.json")
        if errors:
            die("red-test-result.json schema violation:\n" + "\n".join(f"  - {e}" for e in errors))

        status = result.get("status")
        failure_type = result.get("failure_type")

        if failure_type == "environment":
            transition("failed", "red_test_environment", failure_type="environment",
                       halt_reason="environment",
                       extra={"directive": "halt_and_ask",
                              "phase": "red_test",
                              "failure_details": "Environment failure during RED verification "
                                                 "(toolchain/framework). " + result.get("failure_details", ""),
                              "log_excerpt": result.get("log_excerpt", "")})
        elif status == "fail":
            # RED confirmed: tests fail because the feature is not implemented yet.
            # Leave the red phase so future test fixes don't re-impose RED.
            state["red_phase"] = False
            iter_dir = ensure_iter_dir(run_dir, state)
            transition("implementation", "red_confirmed",
                       extra={"directive": "run_implementor",
                              "iter_dir": str(iter_dir),
                              "spec_path": state["spec_path"],
                              "plan_path": state["plan_path"],
                              "attempts_path": attempts_path})
        else:  # status == "pass" → RED not confirmed (vacuous tests or feature exists)
            state["iterations"]["test_implementation"] += 1
            if state["iterations"]["test_implementation"] > state["max"]["test_implementation"]:
                transition("failed", "red_not_confirmed_exhausted",
                           halt_reason="iteration-exhausted",
                           extra={"directive": "report_failure",
                                  "phase": "red_test",
                                  "failure_details": "Authored tests passed without an implementation "
                                                     "(RED never confirmed). Tests are likely vacuous."})
            else:
                iter_dir = ensure_iter_dir(run_dir, state)
                transition("test_implementation", "red_not_confirmed",
                           extra={"directive": "run_test_implementor",
                                  "test_implementation_iter": state["iterations"]["test_implementation"],
                                  "iter_dir": str(iter_dir),
                                  "spec_path": state["spec_path"],
                                  "plan_path": state["plan_path"],
                                  "attempts_path": attempts_path,
                                  "test_implementor_config": cfg["llm"].get("test_implementor", {}),
                                  "note": "The authored tests PASSED with no implementation present. "
                                          "They are vacuous — strengthen them so they fail until the "
                                          "feature exists."})

    # --- implementation → test (automatic, no result file needed) ---
    elif current == "implementation":
        iter_dir = ensure_iter_dir(run_dir, state)
        transition("test", "implementation_done",
                   extra={"directive": "run_tester",
                          "iter_dir": str(iter_dir),
                          "build_instruction":   cfg["llm"]["tester"]["build_instruction"],
                          "install_instruction": cfg["llm"]["tester"]["install_instruction"],
                          "test_instruction":    cfg["llm"]["tester"]["test_instruction"]})

    # --- test ---
    elif current == "test":
        result_file = get_iter_path(run_dir, state) / "test-result.json"
        if not result_file.exists():
            die(f"test-result.json not found at {result_file}. Write the tester result first.")
        result = load_json(result_file)
        errors = validate_against_schema(result, "test-result.schema.json")
        if errors:
            die("test-result.json schema violation:\n" + "\n".join(f"  - {e}" for e in errors))

        status = result.get("status")
        failure_type = result.get("failure_type")

        if status == "pass":
            # No counter change on pass — review reuses the same iteration directory.
            iter_dir = ensure_iter_dir(run_dir, state)
            transition("review", "test_pass",
                       extra={"directive": "run_reviewer",
                              "iter_dir": str(iter_dir),
                              "spec_path": state["spec_path"],
                              "reviewer_config": cfg["llm"]["reviewer"]})
        elif failure_type == "environment":
            transition("failed", "test_fail_environment", failure_type="environment",
                       halt_reason="environment",
                       extra={"directive": "halt_and_ask",
                              "failure_details": result.get("failure_details", ""),
                              "log_excerpt": result.get("log_excerpt", "")})
        else:
            state["iterations"]["test"] += 1
            if state["iterations"]["test"] > state["max"]["test"]:
                transition("failed", "test_fail_exhausted", failure_type="code",
                           halt_reason="iteration-exhausted",
                           extra={"directive": "report_failure",
                                  "failure_details": result.get("failure_details", "")})
            else:
                iter_dir = ensure_iter_dir(run_dir, state)
                transition("implementation", "test_fail_retry", failure_type="code",
                           extra={"directive": "run_implementor",
                                  "test_iter": state["iterations"]["test"],
                                  "iter_dir": str(iter_dir),
                                  "spec_path": state["spec_path"],
                                  "plan_path": state["plan_path"],
                                  "attempts_path": str(run_dir / "attempts.md"),
                                  "failure_details": result.get("failure_details", ""),
                                  "log_excerpt": result.get("log_excerpt", "")})

    # --- review ---
    elif current == "review":
        result_file = get_iter_path(run_dir, state) / "review-result.json"
        if not result_file.exists():
            die(f"review-result.json not found at {result_file}. Write the reviewer result first.")
        result = load_json(result_file)
        errors = validate_against_schema(result, "review-result.schema.json")
        if errors:
            die("review-result.json schema violation:\n" + "\n".join(f"  - {e}" for e in errors))

        rbs = cfg["driver"].get("review_block_severity", ["critical", "high"])
        passes = review_passes(result, rbs)

        if passes:
            transition("done", "review_pass",
                       extra={"directive": "finalize",
                              "source": result.get("source")})
        else:
            state["iterations"]["review"] += 1
            if state["iterations"]["review"] > state["max"]["review"]:
                extra = {"directive": "report_failure",
                         "verdict": result.get("verdict"),
                         "summary": result.get("summary", ""),
                         "findings": result.get("findings", [])}
                # In TDD a tightened-but-unsatisfiable test can drain the budget;
                # surface that so the failure is not mistaken for a code defect.
                if state.get("tdd_mode", False):
                    extra["hint"] = ("If the implementation could not satisfy the reviewer, the "
                                     "blocking findings may point at tests that contradict the spec "
                                     "— inspect the test findings, not just the production code.")
                transition("failed", "review_fail_exhausted",
                           halt_reason="iteration-exhausted", extra=extra)
            else:
                iter_dir = ensure_iter_dir(run_dir, state)
                # Route by where the blocking findings point. The implementor is
                # walled off from test files (boundary guard), so a finding about
                # a test must go to the test author. Pure production findings skip
                # the test_implementation detour.
                blocking = blocking_findings(result, rbs)
                test_paths = cfg.get("llm", {}).get("test_implementor", {}).get("test_paths", [])
                touches_tests = state.get("tdd_mode", False) and any(
                    f.get("file") and matches_test_paths(f["file"], test_paths) for f in blocking
                )
                target = "test_implementation" if touches_tests else "implementation"
                directive = "run_test_implementor" if touches_tests else "run_implementor"
                extra = {"directive": directive,
                         "review_iter": state["iterations"]["review"],
                         "iter_dir": str(iter_dir),
                         "spec_path": state["spec_path"],
                         "plan_path": state["plan_path"],
                         "attempts_path": attempts_path,
                         "verdict": result.get("verdict"),
                         "summary": result.get("summary", ""),
                         "findings": result.get("findings", []),
                         "next_steps": result.get("next_steps", [])}
                if touches_tests:
                    extra["test_implementor_config"] = cfg["llm"].get("test_implementor", {})
                transition(target, "review_fail_retry", extra=extra)

    elif current in ("done", "failed"):
        emit({"next_state": current, "message": f"Pipeline already in terminal state: {current}"})

    else:
        die(f"Unknown state: {current}")


# ---------------------------------------------------------------------------
# Subcommand: status
# ---------------------------------------------------------------------------

def cmd_status(args) -> None:
    run_dir = pathlib.Path(args.run).resolve()
    state = load_state(run_dir)
    emit({
        "run_id": state["run_id"],
        "state": state["state"],
        "iterations": state["iterations"],
        "max": state["max"],
        "halt_reason": state.get("halt_reason"),
        "started_at": state["started_at"],
        "updated_at": state["updated_at"],
        "history_length": len(state.get("history", [])),
    })


# ---------------------------------------------------------------------------
# Subcommand: bootstrap-config
# ---------------------------------------------------------------------------

GITIGNORE_ENTRY = ".dev-pipeline/"


def _git_toplevel(start: pathlib.Path) -> "pathlib.Path | None":
    """Return the git repository root containing `start`, or None if not a repo."""
    try:
        out = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    top = out.stdout.strip()
    return pathlib.Path(top) if top else None


def _ensure_gitignore_entry(project_root: pathlib.Path) -> bool:
    """Idempotently ensure `.dev-pipeline/` is gitignored. Returns True if added."""
    gitignore = project_root / ".gitignore"
    if gitignore.exists():
        lines = gitignore.read_text(encoding="utf-8").splitlines()
        if GITIGNORE_ENTRY in lines:
            return False
        text = gitignore.read_text(encoding="utf-8")
        sep = "" if text.endswith("\n") or text == "" else "\n"
        gitignore.write_text(
            f"{text}{sep}\n# dev-pipeline runtime directory\n{GITIGNORE_ENTRY}\n",
            encoding="utf-8",
        )
    else:
        gitignore.write_text(
            f"# dev-pipeline runtime directory\n{GITIGNORE_ENTRY}\n",
            encoding="utf-8",
        )
    return True


def cmd_bootstrap_config(args) -> None:
    """Create .dev-pipeline/dev-pipeline.config.json from the template if absent.

    All filesystem decisions (project-root detection, directory creation, copy,
    .gitignore handling) live here so the SKILL never runs ad-hoc shell for them.
    """
    if args.project:
        project_root = pathlib.Path(args.project).resolve()
    else:
        git_root = _git_toplevel(pathlib.Path.cwd())
        project_root = git_root if git_root is not None else pathlib.Path.cwd()

    config_path = project_root / ".dev-pipeline" / "dev-pipeline.config.json"

    if config_path.exists():
        emit({
            "status": "exists",
            "project_root": str(project_root),
            "config_path": str(config_path),
        })
        return

    if not EXAMPLE_PATH.exists():
        die(f"Config template not found: {EXAMPLE_PATH}\n  Re-run install.sh to repair the installation.")

    config_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(EXAMPLE_PATH, config_path)

    # Only touch .gitignore when project_root is actually a git repository.
    is_git_repo = _git_toplevel(project_root) is not None
    gitignore_updated = _ensure_gitignore_entry(project_root) if is_git_repo else False

    emit({
        "status": "created",
        "project_root": str(project_root),
        "config_path": str(config_path),
        "gitignore_updated": gitignore_updated,
        "required_fields": [
            "llm.tester.build_instruction",
            "llm.tester.install_instruction",
            "llm.tester.test_instruction",
            "llm.test_implementor.framework_instruction",
            "llm.test_implementor.test_paths",
        ],
        "next_action": "Fill in the tester instructions and (TDD is on by default) the "
                       "test_implementor framework_instruction + test_paths. Placeholder <...> "
                       "values are rejected. To skip TDD, set driver.tdd_mode=false or run with "
                       "--no-tdd. Then re-run /dev-pipeline --plan <path>.",
    })


# ---------------------------------------------------------------------------
# Subcommand: validate-config
# ---------------------------------------------------------------------------

def cmd_validate_config(args) -> None:
    config_path = pathlib.Path(args.config).resolve()
    cfg = load_json(config_path)
    errors = validate_config_data(cfg, tdd_override=getattr(args, "tdd", None))
    if errors:
        sys.stderr.write("[dev-pipeline] Config validation FAILED:\n")
        for e in errors:
            sys.stderr.write(f"  - {e}\n")
        sys.exit(1)
    emit({"valid": True, "config": str(config_path)})


# ---------------------------------------------------------------------------
# Subcommand: validate-result
# ---------------------------------------------------------------------------

def cmd_validate_result(args) -> None:
    result_path = pathlib.Path(args.file).resolve()
    schema_name = "test-result.schema.json" if args.type == "test" else "review-result.schema.json"
    data = load_json(result_path)
    errors = validate_against_schema(data, schema_name)
    if errors:
        sys.stderr.write(f"[dev-pipeline] {args.type}-result validation FAILED:\n")
        for e in errors:
            sys.stderr.write(f"  - {e}\n")
        sys.exit(1)
    emit({"valid": True, "type": args.type, "file": str(result_path)})


# ---------------------------------------------------------------------------
# Subcommand: normalize-review
# ---------------------------------------------------------------------------

def cmd_normalize_review(args) -> None:
    """Convert codex --json payload → canonical review-result JSON."""
    in_path = pathlib.Path(args.input).resolve()
    out_path = pathlib.Path(args.output).resolve()

    raw = load_json(in_path)

    # Detect failure conditions
    parse_error = raw.get("parseError")
    codex_status = (raw.get("codex") or {}).get("status")
    result_data = raw.get("result")

    if parse_error:
        die(f"codex parseError: {parse_error}")
    if codex_status not in (None, 0, "0"):
        die(f"codex non-zero exit status: {codex_status}")
    if not result_data or not isinstance(result_data, dict):
        die("codex payload.result is missing or not an object")

    verdict = result_data.get("verdict", "").strip()
    if verdict not in ("approve", "needs-attention"):
        die(f"codex payload.result.verdict is invalid: {verdict!r}")

    def norm_finding(f: dict, idx: int) -> dict:
        sev = f.get("severity", "low")
        if sev not in VALID_SEVERITIES:
            sev = "low"
        ls = f.get("line_start")
        le = f.get("line_end")
        conf = f.get("confidence")
        if conf is None or not isinstance(conf, (int, float)):
            conf = 0.5
        conf = max(0.0, min(1.0, float(conf)))
        return {
            "severity":       sev,
            "title":          str(f.get("title") or f"Finding {idx + 1}").strip() or f"Finding {idx + 1}",
            "body":           str(f.get("body") or "No details provided.").strip() or "No details provided.",
            "file":           str(f.get("file") or "unknown").strip() or "unknown",
            "line_start":     int(ls) if isinstance(ls, int) and ls >= 1 else None,
            "line_end":       int(le) if isinstance(le, int) and le >= 1 else None,
            "confidence":     conf,
            "recommendation": str(f.get("recommendation") or "").strip(),
        }

    findings_raw = result_data.get("findings")
    if findings_raw is None:
        findings_raw = []
    elif not isinstance(findings_raw, list):
        die(f"codex payload.result.findings is not an array: {type(findings_raw).__name__}")
    findings = [norm_finding(f, i) for i, f in enumerate(findings_raw) if isinstance(f, dict)]

    next_steps_raw = result_data.get("next_steps") or []
    next_steps = [str(s).strip() for s in next_steps_raw if isinstance(s, str) and str(s).strip()]

    review_result = {
        "verdict":    verdict,
        "summary":    str(result_data.get("summary") or "").strip() or "No summary provided.",
        "findings":   findings,
        "next_steps": next_steps,
        "source":     "codex-adversarial-review",
    }

    errors = validate_against_schema(review_result, "review-result.schema.json")
    if errors:
        die("Normalized review-result failed schema validation:\n" + "\n".join(f"  - {e}" for e in errors))

    save_json(out_path, review_result)
    emit({"normalized": True, "verdict": verdict, "findings_count": len(findings), "out": str(out_path)})


# ---------------------------------------------------------------------------
# Subcommand: append-attempt
# ---------------------------------------------------------------------------

def cmd_append_attempt(args) -> None:
    run_dir = pathlib.Path(args.run).resolve()
    attempts_path = run_dir / "attempts.md"
    state = load_state(run_dir)
    iters = state["iterations"]
    test_n = iters["test"]
    review_n = iters["review"]
    ti_n = iters.get("test_implementation", 0)

    if args.outcome_file:
        outcome_path = pathlib.Path(args.outcome_file).resolve()
        if not outcome_path.exists():
            die(f"--outcome-file not found: {outcome_path}")
        outcome_text = outcome_path.read_text(encoding="utf-8")
    else:
        outcome_text = args.outcome or ""

    if not outcome_text.strip():
        die("append-attempt requires non-empty content via --outcome or --outcome-file")

    ts = now_iso()
    label = (f"### Attempt — state={args.state}, test_implementation_iter={ti_n}, "
             f"test_iter={test_n}, review_iter={review_n} ({ts})")
    entry = f"\n{label}\n\n{outcome_text.strip()}\n"

    current_content = attempts_path.read_text(encoding="utf-8") if attempts_path.exists() else ""
    if "_No attempts recorded yet._" in current_content:
        current_content = current_content.replace("_No attempts recorded yet._", "").rstrip()
    attempts_path.write_text(current_content + entry + "\n", encoding="utf-8")

    emit({"appended": True, "attempts_path": str(attempts_path)})


# ---------------------------------------------------------------------------
# Subcommand: check-boundary (TDD role isolation)
# ---------------------------------------------------------------------------

def cmd_check_boundary(args) -> None:
    """Deterministically check that a role only touched files it is allowed to.

    role=test_implementation : every changed file must be inside test_paths
                               (and at least one must match — a zero-match with
                               real changes signals a misconfigured test_paths).
    role=implementation      : no changed file may be inside test_paths.

    The driver owns the glob match so the decision is reproducible and unit
    tested, instead of leaving it to LLM prose in the SKILL. Nothing is deleted
    here — the SKILL decides how to react to a violation.
    """
    run_dir = pathlib.Path(args.run).resolve()
    cfg = load_json(run_dir / "config.snapshot.json")
    test_paths = cfg.get("llm", {}).get("test_implementor", {}).get("test_paths", [])
    if not test_paths:
        die("llm.test_implementor.test_paths is empty — cannot enforce the role boundary.")

    changed = [c for c in (args.changed or []) if c.strip()]
    in_tests = [c for c in changed if matches_test_paths(c, test_paths)]
    out_tests = [c for c in changed if not matches_test_paths(c, test_paths)]

    if args.role == "test_implementation":
        if changed and not in_tests:
            emit({"ok": False, "reason": "no_match", "role": args.role, "violating": out_tests,
                  "message": "No changed file matched test_paths. test_paths is likely misconfigured "
                             "for this project's layout — fix it before continuing (do not loop)."})
            return
        ok = len(out_tests) == 0
        emit({"ok": ok, "reason": None if ok else "out_of_bounds", "role": args.role,
              "violating": out_tests,
              "message": "" if ok else "The test author modified non-test (production) files; "
                                       "those changes must be reverted."})
    elif args.role == "implementation":
        ok = len(in_tests) == 0
        emit({"ok": ok, "reason": None if ok else "touched_tests", "role": args.role,
              "violating": in_tests,
              "message": "" if ok else "The implementor modified test files; tests are the test "
                                       "author's domain and those changes must be reverted."})
    else:
        die(f"Unknown role: {args.role}")


# ---------------------------------------------------------------------------
# Subcommand: record-changes (commit/review manifest)
# ---------------------------------------------------------------------------

# A path is a pipeline run artifact (spec.md, config snapshot, state, …) when it
# lives under a .dev-pipeline/ directory at any depth. Such paths are gitignored
# and must never enter the commit/review manifest.
_DEV_PIPELINE_RE = re.compile(r"(^|/)\.dev-pipeline/")


def cmd_record_changes(args) -> None:
    """Accumulate the set of files the pipeline's agents actually produced.

    The SKILL passes each authoring step's delta (the same project_root-relative
    paths it hands to check-boundary). They are merged, de-duplicated and stored
    in <run_dir>/changed-manifest.txt so the commit (done) and the dp-reviewer
    fallback (review) operate on an allowlist of pipeline-produced files instead
    of `git add -A` / `git ls-files --others`, which would sweep in untracked
    junk (cscope, ctags, build caches) the user never asked to commit.
    """
    run_dir = pathlib.Path(args.run).resolve()
    if not run_dir.exists():
        die(f"Run directory not found: {run_dir}")
    manifest_path = run_dir / "changed-manifest.txt"

    incoming = []
    skipped = []
    for raw in (args.changed or []):
        p = raw.strip()
        if not p:
            continue
        # Normalise a leading ./ so the exclusion match is stable.
        if p.startswith("./"):
            p = p[2:]
        if _DEV_PIPELINE_RE.search(p):
            skipped.append(p)
        else:
            incoming.append(p)

    existing = []
    if manifest_path.exists():
        existing = [ln for ln in manifest_path.read_text(encoding="utf-8").splitlines() if ln.strip()]

    merged = sorted(set(existing) | set(incoming))
    manifest_path.write_text("".join(f"{p}\n" for p in merged), encoding="utf-8")

    emit({"recorded": incoming, "skipped": skipped,
          "manifest_path": str(manifest_path), "total": len(merged)})


# ---------------------------------------------------------------------------
# Help text
# ---------------------------------------------------------------------------

HELP_TEXT = """
dev-pipeline — automated implement → test → review loop

WORKFLOW OVERVIEW
-----------------
1. Write a plan.md describing what to implement.
2. Install dev-pipeline into your project:
     bash /path/to/dev-pipeline/install.sh /path/to/project
3. Invoke the SKILL inside Claude Code:
     /dev-pipeline --plan plan.md
   On first run it bootstraps .dev-pipeline/dev-pipeline.config.json from the
   template and stops so you can fill in build/install/test instructions.
4. Edit .dev-pipeline/dev-pipeline.config.json, then re-run /dev-pipeline.

STATES
------
  init                → validate config, generate spec.md
  test_implementation → (TDD) test author writes tests from the spec
  red_test            → (TDD) tester proves the tests FAIL before any code exists
  implementation      → implementor agent writes code
  test                → tester agent runs build/install/test
  review              → codex adversarial-review (fallback: dp-reviewer agent)
  done                → commit, retrospective, (optional) self-evolution
  failed              → stopped due to exhausted iterations or environment error

  TDD flow (default; disable with --no-tdd or driver.tdd_mode=false):
    init → test_implementation → red_test → implementation → test → review → done
  Legacy flow (tdd off):
    init → implementation → test → review → done

DRIVER CLI
----------
  bootstrap-config Seed .dev-pipeline/dev-pipeline.config.json from the template
  init             Create a new run from a plan + config  [--tdd/--no-tdd]
  advance          Compute and apply the next state transition
  status           Print current run state
  validate-config  Check config completeness and schema   [--tdd/--no-tdd]
  validate-result  Check a test-result or review-result file
  normalize-review Convert codex --json payload → canonical review-result JSON
  append-attempt   Log a failed attempt to attempts.md for implementor context
  check-boundary   (TDD) verify a role only touched files it is allowed to
  record-changes   Accumulate pipeline-produced files into changed-manifest.txt
  --version        Print the dev-pipeline version and exit
  --help           Show this message

ITERATION LIMITS
----------------
  max_test_iteration                 — re-runs of implementation after a test failure
  max_review_iteration               — re-runs after a review failure
  max_test_implementation_iteration  — (TDD) re-authoring after RED is not confirmed
  Counters are independent and never reset within a run.

TDD MODE
--------
  tdd_mode (config, default true) — author tests first and prove they fail (RED)
  before writing code, then make them pass (GREEN). Override per run with
  --tdd / --no-tdd. When enabled, llm.test_implementor (focus,
  framework_instruction, test_paths) and runners.test_implementor are required.

REVIEW GATE
-----------
  review_block_severity (array)  — findings with listed severities block the review.
  If null, the review passes only when verdict == "approve".
  If omitted, defaults to ["critical", "high"] (severity-based gating).

CONFIG REQUIREMENTS
-------------------
  build_instruction, install_instruction, test_instruction must all be non-empty.
  Use "no build step" / "no install step" if a stage does not apply.
  The tester NEVER infers commands — it only runs exactly what is configured.
"""


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if len(sys.argv) >= 2 and sys.argv[1] in ("--version", "-V", "version"):
        print(f"dev-pipeline {__version__}")
        sys.exit(0)

    if len(sys.argv) < 2 or sys.argv[1] in ("--help", "-h", "help"):
        print(HELP_TEXT)
        sys.exit(0)

    parser = argparse.ArgumentParser(add_help=False)
    sub = parser.add_subparsers(dest="cmd")

    p_bc = sub.add_parser("bootstrap-config")
    p_bc.add_argument("--project")

    p_init = sub.add_parser("init")
    p_init.add_argument("--plan", required=True)
    p_init.add_argument("--config")
    p_init.add_argument("--project")
    # --tdd / --no-tdd: per-run override (None = fall back to config / default true)
    p_init.add_argument("--tdd", dest="tdd", default=None, action=argparse.BooleanOptionalAction)

    p_adv = sub.add_parser("advance")
    p_adv.add_argument("--run", required=True)

    p_sta = sub.add_parser("status")
    p_sta.add_argument("--run", required=True)

    p_vc = sub.add_parser("validate-config")
    p_vc.add_argument("--config", required=True)
    p_vc.add_argument("--tdd", dest="tdd", default=None, action=argparse.BooleanOptionalAction)

    p_vr = sub.add_parser("validate-result")
    p_vr.add_argument("--type", required=True, choices=["test", "review"])
    p_vr.add_argument("--file", required=True)

    p_nr = sub.add_parser("normalize-review")
    p_nr.add_argument("--source", required=True, choices=["codex"])
    p_nr.add_argument("--in", dest="input", required=True)
    p_nr.add_argument("--out", dest="output", required=True)

    p_aa = sub.add_parser("append-attempt")
    p_aa.add_argument("--run", required=True)
    p_aa.add_argument("--state", required=True,
                      choices=["test_implementation", "red_test", "test", "review"])
    p_aa.add_argument("--outcome", default="")
    p_aa.add_argument("--outcome-file", dest="outcome_file", default="")

    p_cb = sub.add_parser("check-boundary")
    p_cb.add_argument("--run", required=True)
    p_cb.add_argument("--role", required=True, choices=["test_implementation", "implementation"])
    p_cb.add_argument("--changed", nargs="*", default=[])

    p_rc = sub.add_parser("record-changes")
    p_rc.add_argument("--run", required=True)
    p_rc.add_argument("--changed", nargs="*", default=[])

    args = parser.parse_args()

    dispatch = {
        "bootstrap-config": cmd_bootstrap_config,
        "init":             cmd_init,
        "advance":          cmd_advance,
        "status":           cmd_status,
        "validate-config":  cmd_validate_config,
        "validate-result":  cmd_validate_result,
        "normalize-review": cmd_normalize_review,
        "append-attempt":   cmd_append_attempt,
        "check-boundary":   cmd_check_boundary,
        "record-changes":   cmd_record_changes,
    }

    if args.cmd not in dispatch:
        print(HELP_TEXT)
        sys.exit(0)

    dispatch[args.cmd](args)


if __name__ == "__main__":
    main()
