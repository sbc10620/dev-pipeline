#!/usr/bin/env python3
"""
dev-pipeline driver — deterministic state machine for the implement→test→review loop.

Usage:
  python3 driver.py bootstrap-config [--project <dir>]
  python3 driver.py apply-config     --config <path> --values-file <path>
  python3 driver.py init             --plan <path> [--config <path>] [--project <dir>] [--worktree]
  python3 driver.py advance          --run <run_dir>
  python3 driver.py resume           --run <run_dir>
  python3 driver.py cleanup-worktree --run <run_dir>
  python3 driver.py status           --run <run_dir>
  python3 driver.py validate-config  --config <path> [--plan <path>]
  python3 driver.py validate-result  --type test|review|implementor|test_implementor --file <path>
  python3 driver.py check-boundary   --run <run_dir> --role <test_implementation|implementation> --changed <file...>
  python3 driver.py record-changes   --run <run_dir> --changed <file...>
  python3 driver.py run-stage        --run <run_dir> --role <role> [--stage-input <file>]
  python3 driver.py finalize-stage   --run <run_dir> --role <role> [--stage-input <file>]
  python3 driver.py migrate-config   --config <path> [--out <path>]
  python3 driver.py --version
  python3 driver.py --help
"""

import argparse
import json
import os
import pathlib
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Single source of truth for the dev-pipeline version. driver.py is the only
# executable copied into installs, so install.sh and state.json read this value
# rather than maintaining their own copy.
__version__ = "6.8.0"

SCHEMA_DIR = pathlib.Path(__file__).parent / "schemas"
# Config template, co-located with driver.py (install.sh copies it next to this
# file). Resolved the same way as SCHEMA_DIR so an installed copy is standalone.
EXAMPLE_PATH = pathlib.Path(__file__).parent / "config.example.json"
SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}
VALID_SEVERITIES = set(SEVERITY_RANK)
# Bash runners have no default timeout (unset = unbounded). This constant is used
# ONLY by the resume-live-window heuristic below, which needs *some* finite number
# to sum per unset runner when guessing whether a run might still be live — it does
# not bound actual execution. Deliberately generous (24h, not the old 600s): the
# heuristic only decides whether to warn the resuming SKILL "this might still be
# live" (best-effort, not a lock), so under-guessing risks silently double-dispatching
# a still-running unbounded runner (working-tree corruption), while over-guessing only
# costs an extra confirmation prompt on a genuinely dead run. See _resume_live_window.
_RESUME_ASSUMED_UNBOUNDED_RUNNER_SECS = 24 * 60 * 60


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_id_new() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def reserve_run_id(project_dir: pathlib.Path) -> str:
    """Pick a run_id not already used under project_dir's runs/ or worktrees/.
    run_id_new() has 1-second resolution, so two `init` calls landing in the same
    second collide on the bare timestamp — realistic for concurrent runs (the
    worktree feature's whole point) and a latent bug even without worktrees. On
    collision, append -2, -3, … until free. Best-effort (a probe-then-create race
    remains between this call and cmd_init's actual mkdir), not a hard lock —
    consistent with this file's other best-effort concurrency guards (see
    _resume_live_window)."""
    base_rid = run_id_new()
    n = 1
    while True:
        rid = base_rid if n == 1 else f"{base_rid}-{n}"
        if not (project_dir / ".dev-pipeline" / "runs" / rid).exists() and \
           not (project_dir / ".dev-pipeline" / "worktrees" / rid).exists():
            return rid
        n += 1


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
    # Atomic write: a crash/SIGKILL/disk-full mid-write must never truncate the
    # existing file (which for config.json / state.json holds the only copy of the
    # user's settings or the run's progress). Write a sibling temp file, fsync, then
    # os.replace (atomic on the same filesystem).
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


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
        # bool is a subclass of int in Python, so a naive isinstance would let
        # `true`/`false` satisfy an integer/number schema (e.g. timeout: true → a
        # 1-second timeout downstream). Reject bool unless "boolean" is allowed.
        if isinstance(data, bool) and "boolean" not in types:
            errors.append(f"{path or 'root'}: expected {t}, got boolean")
            return errors
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


def effective_tdd_mode(cfg: dict) -> bool:
    """Resolve TDD mode. The single source is config.driver.tdd_mode (set via
    --update-config / apply-config); the per-run --tdd/--no-tdd flags were removed
    in 5.0.0. Default true when unset."""
    return bool(cfg.get("driver", {}).get("tdd_mode", True))


def _is_placeholder(val) -> bool:
    return isinstance(val, str) and val.strip().startswith("<") and val.strip().endswith(">")


RUNNER_TYPES = ("bash", "main-session", "subagent")
# json-role output normalizers. `default` (LLM-agnostic, tolerant) replaced the
# pre-6.0.0 `claude-cli`/`codex-cli`; `passthrough` is the strict opt-in.
VALID_NORMALIZERS = ("passthrough", "default")

# The four roles every config's `runners` section must configure. Defined once,
# ahead of both cmd_bootstrap_config (seeds them as "unconfigured") and ROLE_META
# (which run-stage/finalize-stage key off of) so the two stay in lockstep.
UNCONFIGURABLE_ROLES = ("implementor", "test_implementor", "tester", "reviewer")


def _legacy_runner_roles(cfg: dict) -> list:
    """Roles whose runners use a type removed in 3.0.0 (pre-schema detection). The
    bare `"agent" in r` heuristic only flags a runner whose type is NOT one of the
    current ones, so the new main-session/subagent runners are never mis-flagged."""
    legacy = []
    for role, arr in (cfg.get("runners") or {}).items():
        if isinstance(arr, list) and any(
            isinstance(r, dict) and (r.get("type") in ("claude-subagent", "codex-adversarial-review")
                                     or ("agent" in r and r.get("type") not in RUNNER_TYPES))
            for r in arr):
            legacy.append(role)
    return sorted(set(legacy))


def _unconfigured_runner_roles(cfg: dict) -> list:
    """Roles whose runners are still the bootstrap sentinel `{"type":"unconfigured"}`
    (pre-schema detection, same tier as `_legacy_runner_roles`) — --update-config
    (apply-config) has not configured them yet for this config."""
    unconfigured = []
    for role, arr in (cfg.get("runners") or {}).items():
        if isinstance(arr, list) and any(
            isinstance(r, dict) and r.get("type") == "unconfigured" for r in arr):
            unconfigured.append(role)
    return sorted(set(unconfigured))


def _runner_shape_errors(cfg: dict) -> list:
    """Precise per-runner business rules (better messages than the generic schema
    oneOf 'matches none'): a role's runners must be one homogeneous type (cross-type
    fallback is a future feature); bash needs a command; main-session/subagent must
    not carry one."""
    errors = []
    for role, arr in (cfg.get("runners") or {}).items():
        if not isinstance(arr, list):
            continue
        types = {r.get("type") for r in arr if isinstance(r, dict)}
        if len(types) > 1:
            shown = sorted((t if t else "(missing type)") for t in types)
            errors.append(f"runners.{role}: mixed runner types {shown} — "
                          "keep one execution type per role (cross-type fallback is not supported yet)")
        for i, r in enumerate(arr):
            if not isinstance(r, dict):
                continue
            t = r.get("type")
            if t not in RUNNER_TYPES:
                # Name the bad type explicitly (the generic schema oneOf would only say
                # "matches none of the oneOf schemas", which the repair loop can't act on).
                if t == "unconfigured":
                    errors.append(f"runners.{role}[{i}]: still the 'unconfigured' sentinel — "
                                  "provide a real runner (bash / subagent / main-session)")
                elif t in ("claude-subagent", "codex-adversarial-review"):
                    errors.append(f"runners.{role}[{i}]: runner type {t!r} was removed in 3.0.0 — "
                                  "use bash / subagent / main-session")
                else:
                    errors.append(f"runners.{role}[{i}]: unknown runner type {t!r} — "
                                  f"must be one of {list(RUNNER_TYPES)}")
            elif t == "bash" and not (isinstance(r.get("command"), str) and r["command"].strip()):
                errors.append(f"runners.{role}[{i}]: a bash runner requires a non-empty `command`")
            elif t in ("main-session", "subagent") and "command" in r:
                errors.append(f"runners.{role}[{i}]: a {t} runner must not have a `command` "
                              "(the host session/subagent runs it, not a shell)")
            # A normalizer only applies to a JSON role's output. A file role
            # (implementor/test_implementor) always uses the `default` normalizer
            # for its (now-mandatory) status JSON — a normalizer key there is
            # meaningless and rejected as a config mistake.
            if "normalizer" in r:
                if ROLE_META.get(role, {}).get("category") == "file":
                    errors.append(f"runners.{role}[{i}]: a `normalizer` is meaningless for the {role} "
                                  "(a file role's status JSON always uses the `default` normalizer); remove it")
                elif r["normalizer"] not in VALID_NORMALIZERS:
                    # Name a removed/unknown normalizer (esp. the pre-6.0.0
                    # claude-cli/codex-cli) instead of the generic oneOf error.
                    errors.append(f"runners.{role}[{i}]: unknown normalizer {r['normalizer']!r} — "
                                  f"use one of {list(VALID_NORMALIZERS)} (`default` replaced the "
                                  "pre-6.0.0 claude-cli/codex-cli).")
    return errors


_REMOVED_DRIVER_KEYS = ("allow_unattended_header_merge",)


def validate_config_data(cfg: dict) -> list[str]:
    # Fail closed on a malformed `runners` section (a non-dict would crash the
    # per-role helpers below with a raw traceback) before anything else.
    runners = cfg.get("runners")
    if runners is not None and not isinstance(runners, dict):
        return [f"runners: must be an object mapping each role to a runner array, got "
                f"{type(runners).__name__}."]

    # 6.0.0 removed `driver.allow_unattended_header_merge` (the plan header is gone).
    # A 5.x config still carrying it would fail the generic additionalProperties
    # check AND be unrepairable via apply-config (deep-merge cannot delete a key) —
    # so name it with a migrate hint that DOES drop it.
    drv = cfg.get("driver")
    if isinstance(drv, dict):
        stale = [k for k in _REMOVED_DRIVER_KEYS if k in drv]
        if stale:
            return [f"driver.{', '.join(stale)}: removed in 6.0.0. Run "
                    "`driver migrate-config --config <path>` to drop it (then reconfigure "
                    "with `/dev-pipeline --update-config`)."]

    # 3.0.0 migration: surface removed runner types with an actionable message
    # BEFORE the generic schema enum error.
    legacy = _legacy_runner_roles(cfg)
    if legacy:
        return [f"runners.{', '.join(legacy)}: use a runner type removed in 3.0.0 "
                "(claude-subagent / codex-adversarial-review). Replace each with "
                '{"type":"bash","command":"..."}, or run `driver migrate-config '
                "--config <path>` to convert automatically."]

    # A freshly bootstrapped config's runners are the "unconfigured" sentinel until
    # the --update-config flow seeds them — surface that plainly before the generic
    # schema oneOf error (which would just say "matches none of the oneOf schemas").
    unconfigured = _unconfigured_runner_roles(cfg)
    if unconfigured:
        return [f"runners.{', '.join(unconfigured)}: not configured yet. Run "
                "`/dev-pipeline --update-config` (it recommends runners + instructions and "
                "writes them via `driver apply-config`)."]

    # spec_author was removed in 5.0.0 (the plan.md body is the contract now). A
    # config still carrying its runner is rejected with an actionable message
    # rather than a cryptic additionalProperties schema error.
    if isinstance(cfg.get("runners"), dict) and "spec_author" in cfg["runners"]:
        return ["runners.spec_author: removed in 5.0.0 — the plan.md body is the contract "
                "and there is no spec-author stage. Delete runners.spec_author, or run "
                "`driver migrate-config --config <path>` to drop it automatically."]

    # Precise per-runner shape messages before the generic schema oneOf error.
    shape = _runner_shape_errors(cfg)
    if shape:
        return shape

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

    # TDD is opt-out-able. When enabled, the test_implementor block
    # and its runner are mandatory and must not contain placeholders.
    if effective_tdd_mode(cfg):
        ti = cfg.get("llm", {}).get("test_implementor")
        if not ti:
            errors.append(
                "llm.test_implementor: required when tdd_mode is enabled — add it (focus, "
                "framework_instruction, test_paths) or set driver.tdd_mode=false"
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
                "or set driver.tdd_mode=false"
            )

    # Runner commands may only reference placeholders the driver substitutes, so a
    # typo fails fast at validate time instead of breaking at the shell.
    known = {"system_file", "user_file", "output_file", "project_root", "run_dir", "work_dir"}
    for role, arr in (cfg.get("runners") or {}).items():
        for i, r in enumerate(arr if isinstance(arr, list) else []):
            cmd = r.get("command", "") if isinstance(r, dict) else ""
            unknown = sorted(set(re.findall(r"(?<!\$)\{(\w+)\}", cmd)) - known)
            if unknown:
                errors.append(f"runners.{role}[{i}].command references unknown placeholder(s) "
                              f"{{{', '.join(unknown)}}}; allowed: {sorted(known)}")

    return errors


# ---------------------------------------------------------------------------
# Config deep-merge helpers
#
# Used by `apply-config` (the --update-config write path) to merge a partial
# values file into config.json leaf-by-leaf, preserving sibling keys the caller
# omits. (There is no plan.md config header as of 6.0.0 — config lives only in
# config.json; the plan.md body is purely the contract.)
# ---------------------------------------------------------------------------

def _get_nested(d: dict, path):
    cur = d
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return (False, None)
        cur = cur[k]
    return (True, cur)


def _set_nested(d: dict, path, value) -> None:
    cur = d
    for k in path[:-1]:
        nxt = cur.get(k)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[k] = nxt
        cur = nxt
    cur[path[-1]] = value


def _deep_merge(dst: dict, src: dict) -> None:
    """Recursively merge src into dst (in place). Dicts merge key-by-key so a
    partial values file only overrides the leaves it names; every non-dict value
    (including a list such as a role's whole `runners` array) replaces wholesale."""
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v


def _strip_code_fences(text: str) -> str:
    """Blank out fenced code blocks (``` or ~~~, 3+ chars) so a heading shown
    inside an example is not counted as a real section heading. Tracks the
    opener's (char, length): a closing fence must be the same char and at least
    as long — so a 3-backtick line inside a 4-backtick example does NOT close it
    (the exact wrapping dp-planner.md's template teaches)."""
    out, fence = [], None  # fence = (char, length) while inside a block
    for line in text.splitlines():
        s = line.lstrip()
        if fence is None:
            m = re.match(r"(`{3,}|~{3,})", s)
            if m:
                fence = (m.group(1)[0], len(m.group(1)))
                out.append("")
            else:
                out.append(line)
            continue
        # inside a fence — close only on same char, length >= opener
        if re.match(re.escape(fence[0]) + "{" + str(fence[1]) + r",}[ \t]*$", s):
            fence = None
        out.append("")
    return "\n".join(out)


def _has_h2(text: str, name: str) -> bool:
    return re.search(r"(?m)^##[ \t]+" + re.escape(name) + r"[ \t]*$", text) is not None


def _section_body(text: str, name: str) -> str:
    """Lines under an exact H2 `name` up to the next H2 (or EOF)."""
    m = re.search(r"(?m)^##[ \t]+" + re.escape(name) + r"[ \t]*$", text)
    if not m:
        return ""
    rest = text[m.end():]
    nxt = re.search(r"(?m)^##[ \t]+\S", rest)
    return rest[:nxt.start()] if nxt else rest


def validate_plan_body(body: str, tdd_mode: bool) -> list:
    """Deterministic required-section + non-empty check on the plan body (the whole
    plan.md; replaces the old LLM `INSUFFICIENT` refusal). Same bar as the
    spec-author contract: `## Requirements` + `## Acceptance Criteria` always,
    `## Interface` additionally under TDD. Exact H2 headings, code fences ignored.
    Structure only — testability of the ACs is the planner's / user's job.
    """
    scan = _strip_code_fences(body)
    problems = []
    required = ["Requirements", "Acceptance Criteria"] + (["Interface"] if tdd_mode else [])
    for name in required:
        if not _has_h2(scan, name):
            problems.append(f"missing required section: ## {name}")
    if _has_h2(scan, "Acceptance Criteria") and not re.search(
            r"(?m)^[ \t]*(?:[-*+][ \t]+|\d+[.)][ \t]+)\S",
            _section_body(scan, "Acceptance Criteria")):
        problems.append("## Acceptance Criteria has no list items "
                        "(e.g. `- [ ] AC1. …` or `1. AC1. …`)")
    if tdd_mode and _has_h2(scan, "Interface") and not _section_body(scan, "Interface").strip():
        problems.append("## Interface is empty")
    return problems


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
# Orchestrator "what to do next" cue
# ---------------------------------------------------------------------------

def _next_action_for(state: str) -> str:
    """Imperative, English "what to do next" cue for a landed state, echoed on
    every advance / resume so a context-thin orchestrator (esp. all-main-session
    + --resume, where per-stage compaction strips the SKILL loop rules) always has
    an explicit "keep going / execute this state" instruction in its freshest tool
    result. Computed in ONE place so transition(), the already-terminal advance
    emit, and cmd_resume's legacy fallback all speak identically — a fixed per-site
    string would read as "loop until done" even when the state already IS done.
    This is a cue for the orchestrator only; it is excluded from stage-input
    (`_STAGE_INPUT_CONTROL`) so it never reaches a role's own prompt."""
    if state == "done":
        return ("Open and EXECUTE states/done.md now — the commit / merge / "
                "retrospective happen there. 'done' is NOT a stop signal; stop only "
                "after done.md completes.")
    if state == "failed":
        return "Open states/failed.md, report per its steps, then stop."
    return (f"This run is NOT finished. Open and follow states/{state}.md, run its "
            "steps, then continue the advance loop (call `driver advance` again). "
            "Do not stop after this state.")


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
        die(f"Config file not found: {config_path}\n  Run /dev-pipeline once — it bootstraps .dev-pipeline/dev-pipeline.config.json from the template on first run, then walks you through `--update-config` to fill it in — and re-run.")

    cfg = load_json(config_path)

    # --- The plan.md body IS the contract; there is no config header as of 6.0.0.
    #     config lives only in config.json (set via --update-config / apply-config),
    #     which is snapshotted per-run into config.snapshot.json. ---
    body = plan_path.read_text(encoding="utf-8")

    tdd_mode = effective_tdd_mode(cfg)

    # --- Validate the config AND the plan body BEFORE any disk change, so a
    #     rejected plan never leaves a half-created run that `advance` could pick
    #     up (the section gate must not be bypassable). Config completeness is
    #     enforced here as a safety net — the SKILL runs --update-config first. ---
    errors = validate_config_data(cfg)
    if errors:
        sys.stderr.write("[dev-pipeline] Config validation failed:\n")
        for e in errors:
            sys.stderr.write(f"  - {e}\n")
        sys.stderr.write("\nFix .dev-pipeline/dev-pipeline.config.json "
                         "(run /dev-pipeline --update-config) and retry.\n")
        sys.exit(1)

    body_problems = validate_plan_body(body, tdd_mode)
    if body_problems:
        sys.stderr.write("[dev-pipeline] Plan body is not a usable contract:\n")
        for p in body_problems:
            sys.stderr.write(f"  - {p}\n")
        req = "Requirements, Acceptance Criteria" + (", Interface" if tdd_mode else "")
        sys.stderr.write(f"\nThe plan body needs the required sections with real content ({req}).\n")
        sys.exit(1)

    # --- All checks passed; now create the run on disk. ---
    project_dir = pathlib.Path(args.project).resolve() if args.project else pathlib.Path(".").resolve()

    # Cheap, deterministic preconditions — checked BEFORE claiming a run_dir or
    # creating a worktree, so none of these failure modes leaves either behind.
    latest_link = project_dir / ".dev-pipeline" / "latest"
    if latest_link.exists() and not latest_link.is_symlink() and latest_link.is_dir():
        die(f"Cannot create 'latest' symlink: {latest_link} is a directory. Remove it manually.")

    # --worktree preconditions that don't depend on rid (so they run before ANY
    # claim): project_dir must be inside a git repo with an existing commit.
    git_root = None
    if getattr(args, "worktree", False):
        git_root = _git_toplevel(project_dir)
        if git_root is None:
            die(f"--worktree requires project_dir to be inside a git repository: {project_dir}")
        head_check = _git(project_dir, "rev-parse", "--verify", "-q", "HEAD")
        if head_check.returncode != 0:
            die(f"--worktree requires an existing commit in {project_dir} — the "
                "repository has no HEAD yet. Make an initial commit and retry.")

    # Claim a run_dir atomically: reserve_run_id only PROBES for a free rid, so a
    # concurrent init could still win the race between the probe and this mkdir
    # (realistic — concurrent runs are the point of --worktree). exist_ok=False
    # turns that race into a clean, retried collision instead of two runs silently
    # sharing (and one overwriting) the same run_dir/state.json.
    while True:
        rid = reserve_run_id(project_dir)
        run_dir = project_dir / ".dev-pipeline" / "runs" / rid
        try:
            run_dir.mkdir(parents=True, exist_ok=False)
            break
        except FileExistsError:
            continue

    # From here on, run_dir (and, under --worktree, the worktree + branch) must be
    # rolled back on ANY failure — including our own die() calls below, which
    # raise SystemExit, not a normal exception — so this success flag + `finally`
    # is used instead of try/except (a `finally` runs on SystemExit too, unlike
    # `except Exception`). This closes the leak a die() between worktree-add and
    # the final emit() would otherwise cause (cleanup-worktree can only find a
    # worktree/branch that made it into a saved state.json). The only failure
    # that can still occur inside this block is `git worktree add` itself (rid- and
    # therefore run_dir-dependent, so it can't be checked any earlier) or a genuine
    # I/O error — every rid-independent precondition was already checked above.
    worktree_branch = None
    worktree_base_ref = None
    work_root = project_dir
    wt_path = None
    success = False
    try:
        # --worktree: isolate this run's code edits + git bookkeeping into a fresh
        # git worktree + branch instead of project_dir's own working tree, so the
        # pipeline never touches the user's real checkout.
        if getattr(args, "worktree", False):
            ref_out = _git(project_dir, "symbolic-ref", "--short", "-q", "HEAD")
            worktree_base_ref = (ref_out.stdout.strip() if ref_out.returncode == 0
                                  else _git(project_dir, "rev-parse", "HEAD").stdout.strip())
            worktree_branch = f"dev-pipeline/{rid}"
            wt_path = project_dir / ".dev-pipeline" / "worktrees" / rid
            wt_path.parent.mkdir(parents=True, exist_ok=True)
            added = _git(project_dir, "worktree", "add", str(wt_path), "-b", worktree_branch)
            if added.returncode != 0:
                die(f"git worktree add failed for {wt_path} (branch {worktree_branch}):\n"
                    f"{added.stderr.strip()}")
            # `git worktree add` checks out the WHOLE repo at wt_path (from
            # git_root), not just project_dir's subtree. If project_dir is a
            # strict subdirectory of the repo (a layout the non-worktree flow
            # already supports — see done.md's no-HEAD+subdir note), work_root
            # must point at the matching subdirectory inside the new checkout,
            # not at the checkout's root, or every git bookkeeping/role-boundary
            # path downstream resolves against the wrong directory.
            try:
                rel = project_dir.relative_to(git_root)
            except ValueError:
                rel = pathlib.Path(".")
            work_root = wt_path if str(rel) == "." else wt_path / rel

        # latest symlink
        if latest_link.is_symlink() or latest_link.exists():
            latest_link.unlink()
        # Relative target keeps the symlink valid if the project dir is moved/remounted.
        latest_link.symlink_to(pathlib.Path("runs") / rid)

        # The contract = the plan body (the whole plan.md, no header to strip as of
        # 6.0.0). It is the single artifact the downstream roles (test author,
        # implementor, reviewer) read; there is no separate spec.md and no
        # spec-author stage.
        contract_path = run_dir / "contract.md"
        contract_path.write_text(body if body.endswith("\n") else body + "\n", encoding="utf-8")

        ts = now_iso()
        state_obj = {
            "run_id": rid,
            "dev_pipeline_version": __version__,
            "state": "init",
            "plan_path": str(plan_path),        # source plan (provenance; not fed to roles)
            "config_path": str(config_path),
            "contract_path": str(contract_path),
            "project_dir": str(project_dir),
            # work_root is where code is edited and the working-tree git bookkeeping
            # (baseline/delta/review-diff/commit) happens: the worktree path when
            # --worktree was used, else identical to project_dir. worktree_branch /
            # worktree_base_ref are None for a non-worktree run.
            "work_root": str(work_root),
            "worktree_branch": worktree_branch,
            "worktree_base_ref": worktree_base_ref,
            "tdd_mode": tdd_mode,
            # red_phase is true only while the very first RED verification is pending.
            # It flips to false once red_test confirms RED, so later test fixes
            # (driven by review findings) do not re-impose the failing-test gate.
            "red_phase": tdd_mode,
            "iterations": {"test": 0, "review": 0, "test_implementation": 0},
            "max": {
                "test": cfg["driver"]["max_test_iteration"],
                "review": cfg["driver"]["max_review_iteration"],
                "test_implementation": cfg["driver"].get("max_test_implementation_iteration", 3),
            },
            "halt_reason": None,
            "history": [{"state": "init", "ts": ts, "outcome": "started", "failure_type": None}],
            "started_at": ts,
            "updated_at": ts,
        }
        save_state(run_dir, state_obj)

        # snapshot config.json into the run (config.json on disk is untouched — it
        # is only ever written by apply-config / the --update-config flow)
        save_json(run_dir / "config.snapshot.json", cfg)

        # initialise attempts.md
        (run_dir / "attempts.md").write_text(
            "# Attempt History\n\n_No attempts recorded yet._\n", encoding="utf-8"
        )

        success = True
    finally:
        if not success:
            # Best-effort: undo whatever we managed to create so a failed
            # --worktree init leaves nothing on disk — the same "no partial run"
            # contract the config/plan-body validation above already has. Never
            # let a rollback failure mask the original error.
            if wt_path is not None:
                try:
                    _git(wt_path, "clean", "-xdf")
                    _git(project_dir, "worktree", "remove", "--force", str(wt_path))
                    _git(project_dir, "worktree", "prune")
                    if worktree_branch:
                        _git(project_dir, "branch", "-D", worktree_branch)
                except Exception:
                    pass
            try:
                if latest_link.is_symlink() and latest_link.resolve() == run_dir.resolve():
                    latest_link.unlink()
            except Exception:
                pass
            shutil.rmtree(run_dir, ignore_errors=True)

    emit({
        "state": "init",
        "run_id": rid,
        "run_dir": str(run_dir),
        "contract_path": str(contract_path),
        "plan_path": str(plan_path),
        "tdd_mode": tdd_mode,
        "work_root": str(work_root),
        "directive": "advance",
        "next_action": "advance",
        "message": "Init successful. Call `driver advance --run <run_dir>` to enter the first stage.",
    })


# ---------------------------------------------------------------------------
# Subcommand: advance
# ---------------------------------------------------------------------------

def _append_attempt_entry(attempts_path: pathlib.Path, state_label: str, iters: dict, text: str) -> None:
    """Append a formatted failure entry to attempts.md (replacing the initial
    placeholder). Called by `cmd_advance` when it routes a failure back to a retry,
    so the retry context (test log / review findings / vacuous-test note) is
    recorded deterministically — no separate SKILL step to forget. The label uses
    the POST-increment counters, matching the attempt about to be retried."""
    text = (text or "").strip()
    if not text:
        return
    ti_n = iters.get("test_implementation", 0)
    test_n = iters.get("test", 0)
    review_n = iters.get("review", 0)
    label = (f"### Attempt — state={state_label}, test_implementation_iter={ti_n}, "
             f"test_iter={test_n}, review_iter={review_n} ({now_iso()})")
    entry = f"\n{label}\n\n{text}\n"
    current = attempts_path.read_text(encoding="utf-8") if attempts_path.exists() else ""
    if "_No attempts recorded yet._" in current:
        current = current.replace("_No attempts recorded yet._", "").rstrip()
    attempts_path.write_text(current + entry + "\n", encoding="utf-8")


def _test_failure_text(result: dict) -> str:
    """Human-readable retry context from a failing test-result.json."""
    parts = [result.get("failure_details", "").strip()]
    excerpt = result.get("log_excerpt", "").strip()
    if excerpt:
        parts.append("Log excerpt:\n" + excerpt)
    return "\n\n".join(p for p in parts if p)


def _review_failure_text(result: dict) -> str:
    """Human-readable retry context from a blocking review-result.json."""
    parts = []
    if result.get("verdict"):
        parts.append(f"Verdict: {result['verdict']}")
    if result.get("summary"):
        parts.append(f"Summary: {result['summary'].strip()}")
    findings = result.get("findings") or []
    if findings:
        lines = ["Findings:"]
        for f in findings[:8]:
            sev = f.get("severity", "?")
            loc = f" ({f['file']})" if f.get("file") else ""
            title = (f.get("title") or "").strip()
            lines.append(f"- [{sev}]{loc} {title}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _implementor_blocked_text(result: dict) -> str:
    """Human-readable retry context from a `status:"blocked"` implementor-result.json."""
    parts = [f"Summary: {result.get('summary', '').strip()}"]
    concern = result.get("concern")
    if concern:
        parts.append(f"Concern: {concern.strip()}")
    return "\n\n".join(p for p in parts if p)


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
            # The implementor build-checks its code before handoff (catches compile
            # errors early); it reuses the tester's build command.
            e["build_instruction"] = llm.get("tester", {}).get("build_instruction", "no build step")
            if tdd:
                # The implementor must know which paths are off-limits (test author's).
                e["test_paths"] = llm.get("test_implementor", {}).get("test_paths", [])
        elif new_state == "test_implementation":
            e["test_implementor_runners"] = runners.get("test_implementor", [])
        elif new_state in ("red_test", "test"):
            e["tester_runners"] = runners.get("tester", [])
        elif new_state == "review" and state.get("red_confirmation_skipped"):
            # 6.7.0: surfaces a red_expected:false skip to the reviewer — the
            # ONLY other place in the pipeline that could catch a test that was
            # wrongly declared "targets pre-existing behavior" when it was
            # actually just vacuous (the test↔implementation retry loop only
            # catches the OTHER failure mode: a genuinely unimplemented feature).
            e["red_confirmation_skipped_note"] = (
                "RED confirmation was skipped for tests authored earlier in this run "
                "(test_implementor declared red_expected: false — "
                f"{state.get('red_confirmation_skip_summary', '')}). These specific "
                "tests never went through the pipeline's automatic vacuous-test "
                "detector (red_test). Give them extra scrutiny: confirm they contain "
                "real, meaningful assertions of already-existing behavior, not just "
                "code that happens to run without asserting anything.")
        elif new_state == "done":
            e["run_self_evolution"] = cfg.get("driver", {}).get("run_self_evolution", False)
            # Needed to merge the worktree branch back and then tear it down;
            # None/absent for a non-worktree run (done.md's git stays no-op).
            e["worktree_branch"] = state.get("worktree_branch")
            e["worktree_base_ref"] = state.get("worktree_base_ref")
        elif new_state == "failed":
            # A worktree run that fails must NOT be auto-merged/cleaned up — the
            # SKILL preserves it for debugging (states/failed.md) and needs these
            # to tell the user where it is / how to clean it up manually.
            e["worktree_branch"] = state.get("worktree_branch")
            e["worktree_base_ref"] = state.get("worktree_base_ref")
        # Note: no reviewer-runner echo here — the reviewer (like every role in
        # 3.0.0) is run by `driver run-stage`, which reads config.runners.reviewer
        # itself; the SKILL never needs the runner array.
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
        result = {
            "previous_state": current,
            "next_state": new_state,
            "iterations": state["iterations"],
            "halt_reason": state.get("halt_reason"),
            # tdd_mode is the frozen, authoritative run flag (state.json). Echo it on
            # every advance so a resuming session recovers it from the echo (or
            # state.json), not by re-deriving from config — the frozen state value
            # is the single source once a run has started.
            "tdd_mode": tdd,
            # work_root is where code is edited and working-tree git bookkeeping
            # happens (the worktree path under --worktree, else == project_dir).
            # Echoed unconditionally like tdd_mode, never re-derived from disk
            # (Global Rule 9) — state files must git -C <work_root>, not
            # <project_root>, for baseline/delta/review-diff/commit.
            "work_root": state.get("work_root") or state.get("project_dir", ""),
            # Explicit "what to do next" cue for the orchestrator on EVERY transition
            # (see _next_action_for) — non-terminal → keep looping; done → execute
            # done.md; failed → report + stop. Excluded from stage-input so it never
            # reaches a role's prompt.
            "next_action": _next_action_for(new_state),
        }
        result.update(dest_echoes(new_state))
        if extra:
            result.update(extra)
        # Persist the full landing echo for `driver resume` to replay, and do it
        # BEFORE save_state so a crash between the two writes is disambiguable: the
        # only possible mismatch is last-advance.next_state == new_state while
        # state.json still holds `current` (== the echo's previous_state), i.e. the
        # advance died mid-flight and persisted no state change (counters/history
        # mutate in memory and are persisted only by save_state below). cmd_resume
        # detects that window and re-runs advance instead of dead-ending.
        save_json(run_dir / "last-advance.json", result)
        save_state(run_dir, state)
        # Persist a stage-input.json next to the iteration so `driver run-stage`
        # (bash-runner mode) can consume the same context the SKILL echo carries.
        # work_root (not project_dir) is what a runner's cwd/{project_root} must
        # resolve to — see build_stage_input's docstring.
        si = build_stage_input(result, state.get("work_root") or state.get("project_dir", ""))
        if si and si.get("work_dir") and si["work_dir"] != ".":
            save_json(pathlib.Path(si["work_dir"]) / "stage-input.json", si)
        emit(result)

    attempts_path = str(run_dir / "attempts.md")
    # The contract handed to every downstream role. `.get` fallback keeps a run
    # created just before 5.0.0's rename resumable.
    contract_path = state.get("contract_path") or state.get("spec_path")

    # --- init ---
    if current == "init":
        # The contract (the plan body) is written by `init` itself, so
        # it always exists here; a fallback covers runs created before 5.0.0.
        contract_raw = state.get("contract_path") or state.get("spec_path")
        if not contract_raw or not pathlib.Path(contract_raw).exists():
            die("Contract not found. This run was created by a pre-5.0.0 driver — "
                "finish it with the driver version that started it, or start a new run.")
        contract_path = str(contract_raw)
        iter_dir = ensure_iter_dir(run_dir, state)
        if state.get("tdd_mode", False):
            transition("test_implementation", "contract_ready",
                       extra={"directive": "run_test_implementor",
                              "iter_dir": str(iter_dir),
                              "contract_path": str(contract_path),
                              "attempts_path": attempts_path,
                              "test_implementor_config": cfg["llm"].get("test_implementor", {})})
        else:
            transition("implementation", "contract_ready",
                       extra={"directive": "run_implementor",
                              "iter_dir": str(iter_dir),
                              "contract_path": str(contract_path),
                              "attempts_path": attempts_path})

    # --- test_implementation (TDD: author tests; its status file is mandatory
    # at the run-stage/finalize-stage layer since 6.6.0. While red_phase is
    # pending, advance now reads it (6.7.0) — not to route a "blocked" status
    # anywhere (there is still no other role to route a test-author "blocked"
    # to, so that case falls through to red_test unconditionally as before),
    # but to honor a status:"implemented" + red_expected:false declaration:
    # every test in this pass targets pre-existing behavior, so RED
    # confirmation is skipped and the run lands directly on "test" instead.) ---
    elif current == "test_implementation":
        iter_dir = ensure_iter_dir(run_dir, state)
        if state.get("red_phase", False):
            result_file = iter_dir / "test_implementor-result.json"
            if not result_file.exists():
                die(f"test_implementor-result.json not found at {result_file}. "
                    "run-stage/finalize-stage should have produced it (mandatory since "
                    "6.6.0) — this indicates a driver or runner bug. If this run was "
                    "created by a pre-6.6.0 driver, write a valid status file to this "
                    "exact path by hand — minimally: "
                    '{"status": "implemented", "summary": "<one-line outcome>"} — '
                    "or start a new run.")
            result = load_json(result_file)
            errors = validate_against_schema(result, "implementor-result.schema.json")
            if errors:
                die("test_implementor-result.json schema violation:\n" +
                    "\n".join(f"  - {e}" for e in errors))

            if result.get("status") == "implemented" and result.get("red_expected", True) is False:
                # The test author declared every test in this pass targets pre-existing
                # behavior — skip RED confirmation, land exactly where a repair pass does.
                state["red_phase"] = False
                # Persisted (not just echoed) so it survives however many
                # test<->implementation retries happen before review is reached —
                # the reviewer needs to know these specific tests bypassed the
                # pipeline's only automatic vacuous-test detector (see dest_echoes
                # for "review" below and dp-reviewer.md's severity rule for it).
                state["red_confirmation_skipped"] = True
                state["red_confirmation_skip_summary"] = result.get("summary", "")
                transition("test", "tests_added_no_red_expected",
                           extra={"directive": "run_tester",
                                  "iter_dir": str(iter_dir),
                                  "note": ("The test author declared these tests target "
                                           "pre-existing behavior (red_expected: false), "
                                           "skipping RED confirmation: "
                                           f"{result.get('summary', '')}"),
                                  "build_instruction":   cfg["llm"]["tester"]["build_instruction"],
                                  "install_instruction": cfg["llm"]["tester"]["install_instruction"],
                                  "test_instruction":    cfg["llm"]["tester"]["test_instruction"]})
            else:
                # First authoring pass (or a re-entry without red_expected:false):
                # prove the tests FAIL (RED) before any code.
                transition("red_test", "tests_authored",
                           extra={"directive": "run_tester",
                                  "iter_dir": str(iter_dir),
                                  "red_test": True,
                                  "result_filename": "red-test-result.json",
                                  "red_phase_context": (
                                      "RED phase: production code for the feature under test is "
                                      "intentionally not implemented yet. A failure caused by the feature "
                                      "being absent (missing module/function/symbol, import error, or compile "
                                      "error referencing the contract's intended interface) MUST be classified "
                                      "failure_type=code (the expected RED). Reserve environment for failures "
                                      "unrelated to the missing feature (toolchain/framework/network/permissions)."),
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
                              "contract_path": contract_path,
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
                _append_attempt_entry(
                    pathlib.Path(attempts_path), "red_test", state["iterations"],
                    "The authored tests PASSED with no implementation present. They are "
                    "vacuous — strengthen them so they fail until the feature exists.")
                transition("test_implementation", "red_not_confirmed",
                           extra={"directive": "run_test_implementor",
                                  "test_implementation_iter": state["iterations"]["test_implementation"],
                                  "iter_dir": str(iter_dir),
                                  "contract_path": contract_path,
                                  "attempts_path": attempts_path,
                                  "test_implementor_config": cfg["llm"].get("test_implementor", {}),
                                  "note": "The authored tests PASSED with no implementation present. "
                                          "They are vacuous — strengthen them so they fail until the "
                                          "feature exists."})

    # --- implementation ---
    elif current == "implementation":
        # Read with the UNMUTATED counters (matches test/review's own idiom) —
        # the result file was written into the iter_dir the last advance echoed,
        # which get_iter_path recomputes identically as long as no counter here
        # has changed yet. Since 6.6.0 this file is mandatory: run-stage /
        # finalize-stage already validated it before reporting ok:true, so its
        # absence here means a driver/runner bug, not a normal advisory gap.
        result_file = get_iter_path(run_dir, state) / "implementor-result.json"
        if not result_file.exists():
            die(f"implementor-result.json not found at {result_file}. "
                "run-stage/finalize-stage should have produced it — this indicates "
                "a driver or runner bug, not a normal advisory-absence case. If this "
                "run was created by a pre-6.6.0 driver (implementor-result.json was "
                "optional then), write a valid status file to this exact path by hand "
                'to unblock it — minimally: {"status": "implemented", "summary": '
                '"<one-line outcome>"} — or start a new run.')
        result = load_json(result_file)
        errors = validate_against_schema(result, "implementor-result.schema.json")
        if errors:
            die("implementor-result.json schema violation:\n" + "\n".join(f"  - {e}" for e in errors))

        if (result.get("status") == "blocked" and result.get("blocked_on") == "tests"
                and state.get("tdd_mode", False)):
            state["iterations"]["test_implementation"] += 1
            if state["iterations"]["test_implementation"] > state["max"]["test_implementation"]:
                transition("failed", "implementor_blocked_on_tests_exhausted",
                           halt_reason="iteration-exhausted",
                           extra={"directive": "report_failure",
                                  "failure_details": result.get("concern") or result.get("summary", "")})
            else:
                # ensure_iter_dir AFTER the bump above — same idiom as test/review's
                # own retry branches — so this lands in a NEW iteration directory.
                iter_dir = ensure_iter_dir(run_dir, state)
                _append_attempt_entry(pathlib.Path(attempts_path), "implementation",
                                      state["iterations"], _implementor_blocked_text(result))
                transition("test_implementation", "implementor_blocked_on_tests",
                           extra={"directive": "run_test_implementor",
                                  "iter_dir": str(iter_dir),
                                  "contract_path": contract_path,
                                  "attempts_path": attempts_path,
                                  "test_implementor_config": cfg["llm"].get("test_implementor", {}),
                                  "test_implementation_iter": state["iterations"]["test_implementation"],
                                  "note": ("The implementor reported the authored tests may "
                                           f"contradict the contract: {result.get('concern') or result.get('summary', '')}. "
                                           "Verify against the contract and revise only what actually "
                                           "contradicts it — the implementor's read may be wrong.")})
        else:
            # No counter changed — ensure_iter_dir resolves to the SAME dir as
            # the read above (matches the old code's single iter_dir here).
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
                              "contract_path": contract_path,
                              "changes_diff": str(iter_dir / "changes.diff"),
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
                _append_attempt_entry(pathlib.Path(attempts_path), "test",
                                      state["iterations"], _test_failure_text(result))
                transition("implementation", "test_fail_retry", failure_type="code",
                           extra={"directive": "run_implementor",
                                  "test_iter": state["iterations"]["test"],
                                  "iter_dir": str(iter_dir),
                                  "contract_path": contract_path,
                                  "attempts_path": str(run_dir / "attempts.md"),
                                  "failure_details": result.get("failure_details", ""),
                                  "log_excerpt": result.get("log_excerpt", "")})

    # --- review ---
    elif current == "review":
        result_file = get_iter_path(run_dir, state) / "review-result.json"
        if not result_file.exists():
            die(f"review-result.json not found at {result_file}. Write the reviewer result first.")
        result = load_json(result_file)
        # A run interrupted under a pre-6.1.1 driver may have persisted a result
        # with the removed `source` field; tolerate it here (advance re-validates
        # legacy files) while finalize-stage stays strict for new emissions.
        result.pop("source", None)
        errors = validate_against_schema(result, "review-result.schema.json")
        if errors:
            die("review-result.json schema violation:\n" + "\n".join(f"  - {e}" for e in errors))

        rbs = cfg["driver"].get("review_block_severity", ["critical", "high"])
        passes = review_passes(result, rbs)

        if passes:
            transition("done", "review_pass",
                       extra={"directive": "finalize"})
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
                                     "blocking findings may point at tests that contradict the contract "
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
                         "contract_path": contract_path,
                         "attempts_path": attempts_path,
                         "verdict": result.get("verdict"),
                         "summary": result.get("summary", ""),
                         "findings": result.get("findings", []),
                         "next_steps": result.get("next_steps", [])}
                if touches_tests:
                    extra["test_implementor_config"] = cfg["llm"].get("test_implementor", {})
                _append_attempt_entry(pathlib.Path(attempts_path), "review",
                                      state["iterations"], _review_failure_text(result))
                transition(target, "review_fail_retry", extra=extra)

    elif current in ("done", "failed"):
        # A resumed session may call advance while already parked at a terminal
        # state — echo the state's next_action so "already in terminal state" is not
        # misread as "nothing to do" (a run parked at done still owes done.md's commit).
        emit({"next_state": current,
              "message": f"Pipeline already in terminal state: {current}",
              "next_action": _next_action_for(current)})

    else:
        die(f"Unknown state: {current}")


# ---------------------------------------------------------------------------
# Subcommand: resume
# ---------------------------------------------------------------------------

# State → the run-stage role that runs in that state. Used only for the pre-resume
# manual-recipe hint (a landing echo already carries the directive otherwise).
_STATE_ROLE = {
    "test_implementation": "test_implementor",
    "red_test":            "tester",
    "implementation":      "implementor",
    "test":                "tester",
    "review":              "reviewer",
}


def _resume_live_window(run_dir: pathlib.Path) -> int:
    """Seconds after the last state write within which a run might still belong to a
    LIVE session (a runner subprocess could still be executing). A stage runs ONE
    role's runners sequentially (fallback tries them front-to-back), so the bound is
    the largest per-role SUM of runner timeouts, plus a margin. A runner with no
    `timeout` set now runs UNBOUNDED (no 10-minute default) — for this heuristic
    only, an unset runner is assumed to take `_RESUME_ASSUMED_UNBOUNDED_RUNNER_SECS`
    (deliberately generous, not the runner's real bound) since a live-window
    estimate needs a finite number; this does not cap actual execution.
    This is a best-effort heuristic, not a lock — updated_at is stamped at the
    landing advance, so a wedged run can still read as old. Read the snapshot
    defensively (NOT via load_json, which would die): a run whose snapshot is
    missing/corrupt — e.g. init crashed before writing it — must still reach
    cmd_resume's coherent branches, not die inside this heuristic."""
    longest = _RESUME_ASSUMED_UNBOUNDED_RUNNER_SECS
    snap = run_dir / "config.snapshot.json"
    try:
        if snap.exists():
            cfg = json.loads(snap.read_text(encoding="utf-8"))
            per_role = [sum(r.get("timeout") if r.get("timeout") is not None
                            else _RESUME_ASSUMED_UNBOUNDED_RUNNER_SECS
                            for r in runners if isinstance(r, dict))
                        for runners in cfg.get("runners", {}).values()]
            longest = max(per_role) if per_role else _RESUME_ASSUMED_UNBOUNDED_RUNNER_SECS
    except Exception:
        longest = _RESUME_ASSUMED_UNBOUNDED_RUNNER_SECS
    return int(longest) + 120


def cmd_resume(args) -> None:
    """Re-emit the landing echo for a run's CURRENT state so an interrupted run
    resumes from where it stopped — without re-running init (which starts a NEW
    run) and without redoing completed stages. The driver makes NO transition here;
    it only replays what the advance that landed in the current state emitted
    (persisted to last-advance.json). The SKILL-side choreography — including the
    delta reconstruction an authoring state needs before re-entry so a pre-crash
    edit is not silently dropped — lives in states/resume.md."""
    run_dir = pathlib.Path(args.run).resolve()
    sp = state_path(run_dir)
    if not sp.exists():
        die(f"Not a resumable run: {sp} does not exist. The run may have been "
            f"deleted, or this is not a run directory. Pass --run <run_dir> "
            f"(e.g. .dev-pipeline/latest) explicitly, or start a new run.")
    state = load_state(run_dir)
    current = state.get("state")
    if not current:
        die(f"Corrupt state.json (no 'state' field) at {sp}. Cannot resume; "
            f"inspect the run or start a new one.")

    # Context a resuming SKILL session needs but that a landing echo does not carry:
    # project root, the source plan (provenance), the contract, the run dir.
    ctx = {
        "run_dir": str(run_dir),
        "project_dir": state.get("project_dir", ""),
        "plan_path": state.get("plan_path", ""),
        "contract_path": state.get("contract_path") or state.get("spec_path", ""),
        # tdd_mode is in every replayed landing echo, but the init/H1 branches emit
        # only ctx — carry it here so resume.md can always restore it.
        "tdd_mode": state.get("tdd_mode", False),
        # Same story for work_root and the worktree identity: a replayed echo from
        # an OLD advance already carries work_root (it's now always echoed), but
        # the init/H1 branches below emit only ctx, and a run created before this
        # feature has no work_root in state.json at all — fall back to project_dir
        # so resume.md always has a value to restore.
        "work_root": state.get("work_root") or state.get("project_dir", ""),
        "worktree_branch": state.get("worktree_branch"),
        "worktree_base_ref": state.get("worktree_base_ref"),
    }

    # Concurrency guard (best-effort): a state.json touched more recently than a
    # runner could still be executing may belong to a live session — flag it so the
    # SKILL asks. now_iso() writes a trailing "Z"; fromisoformat only accepts it on
    # Python 3.11+, so normalize to "+00:00" to keep this working on 3.9/3.10.
    possibly_live = False
    updated = state.get("updated_at")
    if updated:
        try:
            parsed = datetime.fromisoformat(updated.replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - parsed).total_seconds()
            possibly_live = age < _resume_live_window(run_dir)
        except ValueError:
            pass

    def out(payload: dict) -> None:
        payload.update(ctx)
        payload["resumed"] = True
        # A run interrupted under a NEW driver already carries next_action in its
        # replayed landing echo; a run from an OLDER driver does not — synthesize it,
        # keyed on the state being resumed into so a run parked at `done` gets the
        # execute-done.md cue, not a "loop until done" one. Skip the directive:"advance"
        # branches (init-parked / crash-window): there the instruction is "call advance"
        # (which then emits the real next_action), NOT "open states/<state>.md" — for
        # init-parked that would even tell the orchestrator to open init.md, which
        # resume.md forbids.
        if not payload.get("next_action") and payload.get("directive") != "advance":
            payload["next_action"] = _next_action_for(payload.get("next_state", current))
        if possibly_live:
            payload["possibly_live"] = True
        emit(payload)

    # Parked at init: no stage has run yet — just advance into the first working
    # state (advance carries every echo from there).
    if current == "init":
        out({"next_state": "init", "directive": "advance",
             "message": "run is at init — call `driver advance` to proceed"})
        return

    la_path = run_dir / "last-advance.json"
    echo = None
    if la_path.exists():
        # Read defensively: save_json is atomic so the driver's own writes can't
        # corrupt this, but external damage/truncation should fall through to the
        # manual-recipe fallback (same situation as "the record was lost"), not die
        # on a raw JSONDecodeError.
        try:
            loaded = json.loads(la_path.read_text(encoding="utf-8"))
            echo = loaded if isinstance(loaded, dict) else None
        except (json.JSONDecodeError, OSError):
            echo = None
    if echo is not None:
        if echo.get("next_state") == current:
            # Normal replay — hand back the exact landing echo (terminal states too:
            # done/failed carry run_self_evolution/halt_reason that finalization
            # needs; never gut the echo). Re-persist stage-input from the PRISTINE
            # echo (before injecting resume metadata) so it is byte-identical to the
            # original the interrupted advance wrote.
            si = build_stage_input(echo, state.get("work_root") or state.get("project_dir", ""))
            if si and si.get("work_dir") and si["work_dir"] != ".":
                save_json(pathlib.Path(si["work_dir"]) / "stage-input.json", si)
            out(dict(echo))
            return
        if echo.get("previous_state") == current:
            # Crash window: advance died between writing last-advance.json and
            # save_state, so state.json never moved past `current`. Re-run advance —
            # idempotent, since the result files it reads are still on disk.
            out({"next_state": current, "directive": "advance",
                 "resume_note": "the last transition was interrupted before it "
                                "persisted — re-run `driver advance`"})
            return
        die(f"last-advance.json is inconsistent (next_state="
            f"{echo.get('next_state')!r} / previous_state="
            f"{echo.get('previous_state')!r} vs state.json {current!r}). Cannot "
            f"safely resume automatically; inspect the run.")

    # No landing record (a run created before resume support, or the record was
    # lost). Give the EXACT manual recipe — including the delta-recording step,
    # without which re-running an authoring stage silently drops pre-crash edits.
    role = _STATE_ROLE.get(current)
    iter_dir = get_iter_path(run_dir, state)
    # work_root (not project_dir) is where the run's git bookkeeping actually
    # happens — identical to project_dir unless this was a --worktree run.
    proj = ctx["work_root"] or ctx["project_dir"] or "<work_root>"
    lines = [f"python3 driver.py status --run {run_dir}"]
    if role:
        lines.append(f"python3 driver.py run-stage --run {run_dir} --role {role} "
                     f"--stage-input {iter_dir}/stage-input.json")
    lines.append("# record everything the run has produced (else `done` commits an "
                 "incomplete change set):")
    lines.append(f"#   git -C {proj} diff HEAD --name-only --relative   (plus untracked)")
    lines.append(f"python3 driver.py record-changes --run {run_dir} --changed <those paths>")
    lines.append(f"python3 driver.py advance --run {run_dir}")
    die("This run predates resume support (no last-advance.json). Finish it "
        "manually, then advance:\n  " + "\n  ".join(lines) +
        "\n(Or start a new run.) WARNING: re-running an authoring stage without "
        "first recording the outstanding `git diff HEAD` delta can silently drop "
        "pre-crash edits from the commit and review.")


# ---------------------------------------------------------------------------
# Subcommand: cleanup-worktree
# ---------------------------------------------------------------------------

def cmd_cleanup_worktree(args) -> None:
    """Tear down a `--worktree` run's checkout + branch (idempotent; a no-op for
    a non-worktree run). This is the deterministic HALF of the worktree
    lifecycle — driver-owned, like init's creation. Merging the branch back is
    NOT deterministic (conflicts require a judgment call) and is NOT done here;
    it is the SKILL's job (states/done.md), which calls this only after a
    successful merge (or, from states/failed.md, never — a failed run's worktree
    is preserved for debugging and cleaned up manually)."""
    run_dir = pathlib.Path(args.run).resolve()
    state = load_state(run_dir)
    project_dir = pathlib.Path(state.get("project_dir") or ".")
    work_root = state.get("work_root")
    branch = state.get("worktree_branch")

    if not work_root or not branch or pathlib.Path(work_root) == project_dir:
        emit({"ok": True, "cleaned": False, "reason": "not a worktree run"})
        return

    wt_path = pathlib.Path(work_root)
    worktree_removed = False
    if wt_path.exists():
        # The `test` stage runs with cwd=work_root, so build/test caches routinely
        # leave untracked files behind — `git worktree remove` refuses those
        # without --force. clean first so --force is a formality, not a data-loss
        # risk (only ever removes untracked files INSIDE the worktree checkout).
        _git(wt_path, "clean", "-xdf")
        removed = _git(project_dir, "worktree", "remove", "--force", str(wt_path))
        if removed.returncode != 0 and wt_path.exists():
            die(f"git worktree remove failed for {wt_path}:\n{removed.stderr.strip()}")
        worktree_removed = True
    # Prune stale metadata even if the checkout dir is already gone (e.g. removed
    # by hand), so `git worktree list` stops reporting it.
    _git(project_dir, "worktree", "prune")

    branch_removed = False
    branch_error = None
    exists = _git(project_dir, "rev-parse", "--verify", "-q", f"refs/heads/{branch}")
    if exists.returncode == 0:
        # -d (safe delete) only, never -D (force): if the branch isn't fully
        # merged into project_dir's current HEAD — e.g. done.md's merge failed
        # and the user is resolving it outside the pipeline — this is SUPPOSED
        # to fail rather than silently discard unmerged work. Report, don't force.
        deleted = _git(project_dir, "branch", "-d", branch)
        if deleted.returncode == 0:
            branch_removed = True
        else:
            branch_error = deleted.stderr.strip()

    emit({"ok": True, "cleaned": True, "worktree_removed": worktree_removed,
          "branch_removed": branch_removed, "branch_error": branch_error})


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


def _git(cwd: pathlib.Path, *args: str) -> "subprocess.CompletedProcess[str]":
    """Run `git -C <cwd> <args>`, capturing output. Does NOT raise or die on a
    non-zero exit — git failures here are routine (e.g. `worktree remove` on an
    already-gone worktree, `branch -d` on an unmerged branch) and callers decide
    what a given failure means. Used only by the worktree lifecycle (init
    --worktree / cleanup-worktree); every other git call in this file is the
    single read-only `_git_toplevel` above."""
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True)


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


def _seed_config_from_template(config_path: pathlib.Path, project_root: pathlib.Path) -> bool:
    """Seed config_path from config.example.json with `runners` left as the
    'unconfigured' sentinel, and ensure the .gitignore entry. Returns whether
    .gitignore was updated. Shared by bootstrap-config and apply-config."""
    if not EXAMPLE_PATH.exists():
        die(f"Config template not found: {EXAMPLE_PATH}\n  Re-run install.sh to repair the installation.")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    # The template already ships runners as the sentinel; re-assert it defensively
    # so a customized template can never seed concrete (executable) runners.
    cfg = load_json(EXAMPLE_PATH)
    cfg["runners"] = {role: [{"type": "unconfigured"}] for role in UNCONFIGURABLE_ROLES}
    save_json(config_path, cfg)
    is_git_repo = _git_toplevel(project_root) is not None
    return _ensure_gitignore_entry(project_root) if is_git_repo else False


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
        # Report whether the config is ready to run so the SKILL can decide whether
        # to run `--update-config` first — a first run that bootstrapped then died
        # before setup finished leaves it existing BUT incomplete (unconfigured
        # runners / placeholder instructions), and must still be finishable.
        try:
            existing = load_json(config_path)
            runners_configured = not _unconfigured_runner_roles(existing)
            # config_complete = runners set AND no placeholders AND schema-valid, i.e.
            # `init` would accept it as-is (no --update-config needed).
            config_complete = not validate_config_data(existing)
        except SystemExit:
            runners_configured = None  # unreadable/invalid — let the SKILL surface it
            config_complete = None
        emit({
            "status": "exists",
            "project_root": str(project_root),
            "config_path": str(config_path),
            "runners_configured": runners_configured,
            "config_complete": config_complete,
        })
        return

    gitignore_updated = _seed_config_from_template(config_path, project_root)

    emit({
        "status": "created",
        "project_root": str(project_root),
        "config_path": str(config_path),
        "gitignore_updated": gitignore_updated,
        "runners_configured": False,
        "config_complete": False,
        "required_fields": [
            "llm.tester.build_instruction",
            "llm.tester.install_instruction",
            "llm.tester.test_instruction",
            "llm.test_implementor.framework_instruction",
            "llm.test_implementor.test_paths",
        ],
        "next_action": "Run `/dev-pipeline --update-config`: recommend a runner (execution "
                       "mode + LLM/model) per role AND the llm.* instructions + driver gate keys, "
                       "confirm with the user, then call `driver apply-config` to write them into "
                       "config.json. Placeholder <...> values are rejected. To skip TDD, set "
                       "driver.tdd_mode=false.",
    })


def cmd_apply_config(args) -> None:
    """Merge a partial values file into config.json — the sanctioned config-write
    path behind the SKILL's `--update-config` flow, and the ONE exception to
    'the SKILL never edits the user's config itself' (SKILL.md Global Rule 10).
    Unlike a hand-edit it deep-merges only the leaves the values file names,
    validates the merged result, and writes atomically; unlike the removed
    one-time set-runners it is re-runnable (config only ever changes here). Seeds
    the config from the template first if it does not exist yet."""
    config_path = pathlib.Path(args.config).resolve()

    # Build the merge BASE in memory (do NOT write yet): an existing config, or a
    # fresh template with unconfigured runners. This way a validation failure on an
    # absent config leaves NOTHING on disk (the message "nothing was written" stays
    # honest), and the .gitignore/dir side effects only happen on a successful write.
    seeded = not config_path.exists()
    if seeded:
        if not EXAMPLE_PATH.exists():
            die(f"Config template not found: {EXAMPLE_PATH}\n  Re-run install.sh to repair the installation.")
        base = load_json(EXAMPLE_PATH)
        base["runners"] = {role: [{"type": "unconfigured"}] for role in UNCONFIGURABLE_ROLES}
    else:
        base = load_json(config_path)

    values_path = pathlib.Path(args.values_file).resolve()
    values = load_json(values_path)
    if not isinstance(values, dict):
        die(f"{values_path}: must be a JSON object with any of the keys "
            '{"driver": {...}, "llm": {...}, "runners": {...}}.')
    allowed_top = {"driver", "llm", "runners"}
    extra = sorted(set(values) - allowed_top)
    if extra:
        die(f"{values_path}: unexpected top-level key(s) {extra}; only {sorted(allowed_top)} may be set.")

    merged = json.loads(json.dumps(base))  # deep copy so a validation failure writes nothing
    _deep_merge(merged, values)

    errors = validate_config_data(merged)
    if errors:
        die("Invalid merged config — nothing was written:\n  " + "\n  ".join(errors))

    # All checks passed — now do the on-disk work. For a freshly seeded config also
    # create .dev-pipeline/ and the .gitignore entry (the bootstrap side effects).
    if seeded:
        project_root = (pathlib.Path(args.project).resolve() if getattr(args, "project", None)
                        else config_path.parent.parent)  # default <root>/.dev-pipeline/<file>
        config_path.parent.mkdir(parents=True, exist_ok=True)
        if _git_toplevel(project_root) is not None:
            _ensure_gitignore_entry(project_root)
    save_json(config_path, merged)
    # Clean up the scratch values file on success — but NEVER unlink the config
    # itself (a full config.json is a valid values file, so `--values-file` could
    # legitimately point at the config).
    if values_path != config_path:
        values_path.unlink(missing_ok=True)
    emit({"ok": True, "config": str(config_path), "seeded": seeded,
          "applied": sorted(values), "config_complete": True})


# ---------------------------------------------------------------------------
# Subcommand: validate-config
# ---------------------------------------------------------------------------

def cmd_validate_config(args) -> None:
    config_path = pathlib.Path(args.config).resolve()
    cfg = load_json(config_path)
    plan_path = getattr(args, "plan", None)
    body = None
    if plan_path:
        # Validate the plan body EXACTLY as `init` will see it: the whole plan.md
        # is the contract (no header to strip as of 6.0.0). The config is checked
        # as-is on disk — a plan that passes here passes init.
        pp = pathlib.Path(plan_path).resolve()
        if not pp.exists():
            die(f"Plan file not found: {pp}")
        body = pp.read_text(encoding="utf-8")
    errors = validate_config_data(cfg)
    if body is not None:
        errors += [f"plan body: {p}" for p in validate_plan_body(body, effective_tdd_mode(cfg))]
    if errors:
        sys.stderr.write("[dev-pipeline] Config validation FAILED:\n")
        for e in errors:
            sys.stderr.write(f"  - {e}\n")
        sys.exit(1)
    emit({"valid": True, "config": str(config_path),
          "plan": str(pathlib.Path(plan_path).resolve()) if plan_path else None})


# ---------------------------------------------------------------------------
# Subcommand: validate-result
# ---------------------------------------------------------------------------

# Maps validate-result's --type to its schema file. A dict, not a ternary — a
# ternary silently misroutes a third type into the `else` branch (this was a
# real bug caught in review when `implementor`/`test_implementor` were added:
# adding a choice without rewriting a 2-way ternary would have validated
# implementor results against review-result.schema.json). implementor and
# test_implementor deliberately share one schema (same shape, same precedent as
# test-result.schema.json already being shared by the `test` and `red_test`
# states).
SCHEMA_BY_TYPE = {
    "test":            "test-result.schema.json",
    "review":          "review-result.schema.json",
    "implementor":     "implementor-result.schema.json",
    "test_implementor": "implementor-result.schema.json",
}


def cmd_validate_result(args) -> None:
    result_path = pathlib.Path(args.file).resolve()
    schema_name = SCHEMA_BY_TYPE[args.type]
    try:
        raw = result_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        die(f"File not found: {result_path}")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Tolerate a stray markdown fence / surrounding prose around an otherwise
        # valid JSON object — the same leniency a json role's `default` normalizer
        # gets. This command is a standalone/manual debugging tool since 6.6.0
        # (the normal SKILL workflow now gets this validation for free from
        # run-stage/finalize-stage), but stays lenient for the same reason: a
        # "do not fence this JSON" prompt directive is advisory, not enforced.
        data = _normalize_output(raw, "default")
        if data is None:
            die(f"Invalid JSON in {result_path}")
    errors = validate_against_schema(data, schema_name)
    if errors:
        sys.stderr.write(f"[dev-pipeline] {args.type}-result validation FAILED:\n")
        for e in errors:
            sys.stderr.write(f"  - {e}\n")
        sys.exit(1)
    emit({"valid": True, "type": args.type, "file": str(result_path)})


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

# A path is a pipeline run artifact (contract.md, config snapshot, state, …) when it
# lives under a .dev-pipeline/ directory at any depth. Such paths are gitignored
# and must never enter the commit/review manifest.
_DEV_PIPELINE_RE = re.compile(r"(^|/)\.dev-pipeline/")


def cmd_record_changes(args) -> None:
    """Accumulate the set of files the pipeline's agents actually produced.

    The SKILL passes each authoring step's delta (the same project_root-relative
    paths it hands to check-boundary). They are merged, de-duplicated and stored
    in <run_dir>/changed-manifest.txt so the commit (done) and the review diff
    (review) operate on an allowlist of pipeline-produced files instead
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
# Subcommand: run-stage (execute a role via a configured bash runner)
# ---------------------------------------------------------------------------

# Role → execution metadata. `category` decides how the driver judges success:
#   "file"  — the runner edits the repo; success = exit 0 (delta read elsewhere).
#   "json"  — the runner writes a result JSON to {output_file}; validated against
#             `schema`, with one error-fed retry then fallback to the next runner.
# (The 3.0.0 "named" category and its spec_author role were removed in 5.0.0 —
#  the plan.md body is the contract now; init validates it deterministically.)
ROLE_META = {
    # category drives boundary/manifest handling (the git delta is the checked
    # artifact); schema drives JSON-result validation (judge()/finalize-stage) —
    # the two axes are independent since 6.6.0. A file role's status JSON is
    # validated exactly like a json role's result once it has a schema.
    "test_implementor": {"category": "file",  "schema": "implementor-result", "prompt": "dp-test-implementor"},
    "implementor":      {"category": "file",  "schema": "implementor-result", "prompt": "dp-implementor"},
    "tester":           {"category": "json",  "schema": "test-result",       "prompt": "dp-tester"},
    "reviewer":         {"category": "json",  "schema": "review-result",     "prompt": "dp-reviewer"},
}


# Maps an advance `directive` to the run-stage role it drives.
_DIRECTIVE_ROLE = {
    "run_test_implementor": "test_implementor",
    "run_tester":           "tester",
    "run_implementor":      "implementor",
    "run_reviewer":         "reviewer",
}
# Keys in an advance result that are control/echo metadata, not stage inputs.
# The *_runners arrays are config the driver consumes via config.runners — they
# must never reach a role's own prompt (first principle: the role does not know
# which LLM runs it).
_STAGE_INPUT_CONTROL = {
    "directive", "iter_dir", "previous_state", "next_state", "iterations",
    "halt_reason", "tdd_mode", "result_filename", "red_test",
    "implementor_runners", "test_implementor_runners", "tester_runners",
    # resume-injected metadata — must never reach a role's prompt. cmd_resume also
    # builds stage-input from the pristine echo (before injecting these), so this
    # is defense-in-depth for a byte-identical re-persist.
    "resumed", "resume_note", "message", "possibly_live",
    "run_dir", "project_dir", "plan_path",
    # orchestrator-only "what to do next" cue (echoed on every advance) — an
    # instruction to the ORCHESTRATOR ("open states/*.md, continue the advance
    # loop"), never a stage input; leaking it would inject orchestrator directives
    # into a runner role's own prompt.
    "next_action",
}


def build_stage_input(result: dict, project_dir: str) -> "dict | None":
    """Translate an advance/init result into a run-stage stage-input.json so the
    bash-runner path consumes exactly the context the echo carries (M-1: retry
    context lives only in the echo, never in state.json)."""
    role = _DIRECTIVE_ROLE.get(result.get("directive"))
    if role is None:
        return None
    iter_dir = result.get("iter_dir") or result.get("work_dir")
    si = {"role": role, "project_root": project_dir, "work_dir": iter_dir or ".",
          "inputs": {k: v for k, v in result.items() if k not in _STAGE_INPUT_CONTROL}}
    if role == "tester" and iter_dir:
        si["output_file"] = str(pathlib.Path(iter_dir) / result.get("result_filename", "test-result.json"))
    elif role == "reviewer" and iter_dir:
        si["output_file"] = str(pathlib.Path(iter_dir) / "review-result.json")
    elif role == "implementor" and iter_dir:
        # A file role's PRIMARY *content* result is still the git delta — but its
        # status JSON is a REQUIRED, schema-validated channel (see judge()/
        # finalize-stage) so a role that concludes the contract can't be
        # satisfied has a structured way to say so, instead of grinding
        # indefinitely (esp. in main-session, which has no external supervision
        # at all — see AGENTS.md "Worktree isolation" security note on
        # main-session's lack of a hard envelope, same root cause here).
        si["output_file"] = str(pathlib.Path(iter_dir) / "implementor-result.json")
    elif role == "test_implementor" and iter_dir:
        si["output_file"] = str(pathlib.Path(iter_dir) / "test_implementor-result.json")
    return si


def role_prompt_path(prompt_name: str) -> "pathlib.Path | None":
    """Locate a role's prose file (the system prompt), source or installed layout."""
    here = pathlib.Path(__file__).parent
    for c in (
        here / "agents" / f"{prompt_name}.md",                                     # installed: co-located in skills/dev-pipeline/agents/ (driver flattened beside SKILL.md)
        here.parent / "skills" / "dev-pipeline" / "agents" / f"{prompt_name}.md",  # source repo layout: agents/skills/dev-pipeline/agents/ (driver in agents/dev-pipeline-tools/)
    ):
        if c.exists():
            return c
    return None


def strip_frontmatter(text: str) -> str:
    """Drop a leading YAML frontmatter block so dp-*.md reads as a portable system
    prompt (no host-specific `model:`/`tools:` keys leak into the LLM prompt)."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            nl = text.find("\n", end + 1)
            return text[nl + 1:].lstrip("\n") if nl != -1 else ""
    return text


def assemble_prompt(role: str, stage_input: dict) -> "tuple[str, str]":
    """Build (system, user) prompt text deterministically from the role prose +
    the advance-echoed inputs. The role prose is LLM-agnostic; transport-specific
    directives (where to write output) are appended here, never baked into .md."""
    meta = ROLE_META[role]
    sp = role_prompt_path(meta["prompt"])
    if sp:
        system = strip_frontmatter(sp.read_text(encoding="utf-8"))
    else:
        # No prose file found: the role runs with a stub system prompt. This is a
        # degraded run (a partial/corrupt install), so make it visible instead of
        # silently shipping a gutted prompt to the LLM.
        sys.stderr.write(
            f"[dev-pipeline] WARNING: prose file '{meta['prompt']}.md' not found; "
            f"running {role} with a stub system prompt. Re-run install.sh to repair.\n"
        )
        system = f"You are the {role}."

    inputs = stage_input.get("inputs", {})
    lines = ["## Inputs", ""]
    for k, v in inputs.items():
        if isinstance(v, (dict, list)):
            v = json.dumps(v, ensure_ascii=False)
        lines.append(f"- **{k}**: {v}")
    # The output directive (write-to-file vs print-to-stdout) is appended per
    # runner by cmd_run_stage, because it depends on the runner's command.
    return system, "\n".join(lines) + "\n"


def _normalize_output(raw: str, normalizer: str) -> "dict | None":
    """Map a runner's output-file content to a canonical JSON object.
    `passthrough` parses it strictly; every other value (`default`, the LLM-agnostic
    default — plus any legacy name like a pre-6.0.0 snapshot's `claude-cli`, for
    which we stay lenient rather than silently reverting to strict mid-run) tolerates
    a markdown fence or surrounding prose by extracting the outermost JSON object."""
    raw = raw.strip()
    if normalizer != "passthrough":
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-zA-Z]*\n", "", raw)
            raw = re.sub(r"\n```\s*$", "", raw).strip()
        if not raw.startswith("{"):
            i, j = raw.find("{"), raw.rfind("}")
            if i != -1 and j != -1 and j > i:
                raw = raw[i:j + 1]
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def _run_one(command: str, subst: dict, project_root: pathlib.Path, timeout: "int | None",
             log_path: pathlib.Path) -> "tuple[int | None, str]":
    """Substitute placeholders (shell-quoted) into a command template and run it
    in a shell with cwd=project_root, with the runner's combined stdout+stderr
    connected DIRECTLY to `log_path` (append) — the child writes straight to that
    fd, so the log grows in real time (observable via `tail -f log_path`) while a
    long-running LLM CLI is still executing, not only after it exits. Returns
    (exit_code, this_attempt's log tail): exit_code is None on timeout (the old
    stderr-based error tail is replaced by a slice of the log written since this
    attempt started, tracked by byte offset so concurrent attempts in the same
    run-stage call don't bleed into each other's tail).

    `timeout=None` means UNBOUNDED — the runner's `timeout` field is optional and,
    unlike earlier versions, has no default cap: an unset timeout runs until the
    command exits on its own. Pass an int to opt back into a hard cap.

    start_new_session=True puts the runner in its OWN process group so that a
    timeout (or the driver being interrupted) can SIGKILL the whole group — the
    shell AND the LLM CLI it spawned. Plain subprocess.run kills only the direct
    child shell, orphaning the CLI grandchild (reparented to PID 1) to keep
    running.

    A direct-file redirect (vs. the old PIPE+communicate()) has no EOF to wait
    on, so the direct child exiting is no longer proof the WHOLE group is done —
    a runner command that backgrounds part of its own work (`… & true`) would
    otherwise be reported "finished" while a group-mate is still writing to the
    log. So on a clean exit we still poll the process group for emptiness,
    bounded by whatever's left of `timeout`; if group-mates outlive that budget
    we kill the group and report a timeout, same as the direct-timeout path — a
    runner can never make this wait longer than its configured timeout. If
    `timeout` is None (unset — the default), there is no budget: this drain
    waits for the process group to empty with no cap, same as the main wait. A
    lingering group-mate a command backgrounds and never reaps (`… & true` where
    the child never exits) will therefore hang run-stage indefinitely despite the
    main command having already finished — an accepted consequence of "no
    default timeout," not a bug; set an explicit `timeout` on that runner if you
    need a hang backstop. A genuine interrupt (KeyboardInterrupt) still
    re-raises after cleanup; only a timeout (direct or via a lingering
    group-mate) is reported back to the caller."""
    cmd = command
    for key, val in subst.items():
        cmd = cmd.replace("{" + key + "}", shlex.quote(str(val)))
    with open(log_path, "ab") as logf:
        header = f"\n----- run @ {datetime.now().isoformat(timespec='seconds')}: {cmd[:200]} -----\n"
        logf.write(header.encode("utf-8"))
        logf.flush()
        start_offset = logf.tell()
        timed_out = False
        returncode = None
        deadline = None if timeout is None else time.monotonic() + timeout
        with subprocess.Popen(cmd, shell=True, cwd=str(project_root),
                              stdout=logf, stderr=subprocess.STDOUT,
                              start_new_session=True) as proc:
            pgid = os.getpgid(proc.pid)  # capture while proc is still alive/queryable
            try:
                proc.wait(timeout=timeout)  # timeout=None blocks indefinitely (stdlib behavior)
                returncode = proc.returncode
            except subprocess.TimeoutExpired:
                timed_out = True
            except BaseException:  # a genuine interrupt (KeyboardInterrupt/SIGINT)
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    proc.kill()
                proc.wait()
                raise
            else:
                # Direct child exited on its own — wait out any group-mate it
                # left running, within what remains of the timeout budget (or
                # indefinitely if this runner has no timeout).
                while deadline is None or time.monotonic() < deadline:
                    try:
                        os.killpg(pgid, 0)  # signal 0: existence check, no-op
                    except ProcessLookupError:
                        break
                    time.sleep(0.1)
                else:
                    timed_out = True
            if timed_out:
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    proc.kill()
                proc.wait()
    tail = log_path.read_bytes()[start_offset:].decode("utf-8", errors="replace")[-300:]
    return (None if timed_out else returncode), tail


def _finalize_json(output_file: pathlib.Path, normalizer: str, schema_name) -> "str | None":
    """Normalize a json-role's output file (strip markdown fences / prose wrappers),
    schema-validate it, and persist the canonical JSON back. Returns None on success
    or a short problem string. Shared by run-stage's bash `judge()` and
    `cmd_finalize_stage`, so a json result validates identically no matter whether a
    bash CLI, a subagent, or the main session produced it."""
    if not output_file.exists():
        return "output not produced"
    result = _normalize_output(output_file.read_text(encoding="utf-8"), normalizer)
    if result is None:
        return "no valid JSON in output"
    if schema_name:
        errs = validate_against_schema(result, f"{schema_name}.schema.json")
        if errs:
            return "schema: " + "; ".join(errs[:3])
    # Every downstream consumer (driver advance, the SKILL) reads this with a plain
    # json.loads, so persist the canonical object regardless of how it was formatted.
    output_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return None


def cmd_run_stage(args) -> None:
    """Execute a role's configured runner(s): assemble the prompt, run the LLM CLI
    (subprocess), and validate by category. The LLM choice/flags live entirely in
    config.runners.<role>; this driver only assembles, runs, and checks."""
    run_dir = pathlib.Path(args.run).resolve()
    role = args.role
    if role not in ROLE_META:
        die(f"Unknown role: {role}")
    meta = ROLE_META[role]

    # stage-input.json is written by cmd_advance into each iteration dir; it
    # carries the dynamic, advance-computed context. The static runner array and
    # project root come from the run's own files (driver-owned, not the SKILL).
    si_path = run_dir / args.stage_input if pathlib.Path(args.stage_input).name == args.stage_input else pathlib.Path(args.stage_input)
    stage_input = load_json(si_path)
    cfg = load_json(run_dir / "config.snapshot.json")
    state = load_state(run_dir)
    # stage_input["project_root"] already carries work_root (build_stage_input sets
    # it from that); the state.get(...) chain is a fallback for a stage-input.json
    # missing/predating this field, preferring work_root over project_dir so a
    # worktree run still resolves to its worktree, not the main checkout.
    project_root = pathlib.Path(stage_input.get("project_root") or state.get("work_root")
                                or state.get("project_dir") or ".").resolve()

    # Guard against passing the wrong stage-input (a mismatched iteration/role
    # path) — the SKILL always passes the matching path.
    if stage_input.get("role") and stage_input["role"] != role:
        die(f"stage-input role {stage_input['role']!r} != --role {role!r}; wrong --stage-input path.")

    runners = cfg.get("runners", {}).get(role, [])
    if not runners:
        die(f"config.runners.{role} is empty — nothing to run.")
    # main-session/subagent runners legitimately have no `command` (the host runs
    # them). Reject only an unsupported type (a pre-3.0.0 snapshot) or a bash runner
    # missing its command.
    for r in runners:
        if not isinstance(r, dict):
            continue
        t = r.get("type")
        if t not in RUNNER_TYPES:
            if t == "unconfigured":
                die(f"config.runners.{role} is still the 'unconfigured' sentinel — this snapshot "
                    "was created before runner setup completed. Configure runners "
                    "(/dev-pipeline --update-config) and start a new run.")
            die(f"config.runners.{role} has an unsupported runner type {t!r} — this run was "
                "likely created by a pre-3.0.0 driver. Start a new run (its snapshot is frozen).")
        if t == "bash" and not r.get("command"):
            die(f"config.runners.{role} has a bash runner with no `command`.")
    # validate-config rejects heterogeneous arrays, but a hand-edited frozen snapshot
    # could still reach here — fail loud rather than silently honor only runners[0].
    if len({r.get("type") for r in runners if isinstance(r, dict)}) > 1:
        die(f"config.runners.{role} mixes execution types in the frozen snapshot — "
            "one type per role (cross-type fallback is unsupported).")

    work = pathlib.Path(stage_input.get("work_dir") or run_dir)
    work.mkdir(parents=True, exist_ok=True)
    system_text, user_text = assemble_prompt(role, stage_input)
    system_file = work / f"{role}-system.txt"
    user_file = work / f"{role}-user.txt"
    system_file.write_text(system_text, encoding="utf-8")
    user_file.write_text(user_text, encoding="utf-8")
    output_file = pathlib.Path(stage_input["output_file"]) if stage_input.get("output_file") else (work / f"{role}-output.json")

    subst = {
        "system_file": str(system_file), "user_file": str(user_file),
        "output_file": str(output_file), "project_root": str(project_root),
        "run_dir": str(run_dir), "work_dir": str(work),
    }

    def output_directive(runner: dict) -> str:
        """Per-runner output instruction, two branches by command shape (kept
        deliberately close so bash-runner prompts stay as similar as possible
        across CLIs — see AGENTS.md "bash runner prompts should be identical
        wherever possible"):

        - The command already references `{output_file}` (a stdout redirect like
          claude's `… > {output_file}`, or a CLI-native result flag like codex's
          `-o {output_file}` that captures the final answer outside the model's
          own tool calls) — the harness/shell captures it deterministically, so
          the model is told to answer normally and NOT write the file itself.
        - The command has no `{output_file}` reference at all (e.g. cline, which
          has no clean-stdout or native result-file flag) — the only way to get
          the result out is the model's own Write tool, so it's told the exact
          path to write to.

        implementor/test_implementor (file roles) get a THIRD, distinct branch:
        their status JSON is now REQUIRED and schema-validated the same way a
        json role's result is (see judge()/finalize-stage) — the git delta
        remains the role's primary *content* result (what boundary/manifest
        checks), but the status JSON is no longer optional. It is still always
        "write it yourself," never a stdout-capture directive (the model's stdout
        during a file role is tool-call chatter, not a clean JSON answer) —
        switching to stdout-capture (as a json role can) is future work, not
        done here; see RUNNERS.md's codex+`--worktree` known-limitation note."""
        if meta["category"] == "json":
            what = "a single valid JSON object (no markdown fences, nothing else)"
            if "{output_file}" in runner.get("command", ""):
                return f"\n\nGive {what} as your final answer only — it is captured automatically. Do NOT write it to a file yourself."
            return f"\n\nWrite {what} to this exact file path and nothing else there: {output_file}"
        if role in ("implementor", "test_implementor"):
            return (f"\n\nAdditionally, after you finish, write a brief status JSON "
                    f"(see your role instructions for the exact shape) to this exact "
                    f"file path: {output_file}")
        return ""

    # --- main-session / subagent handoff ---------------------------------------
    # The driver (a subprocess) cannot invoke the host's subagent tool or the main
    # session, so for these modes it assembles the prompt and hands off to the SKILL
    # (see SKILL.md §Role Execution). validate-config forbids heterogeneous arrays,
    # so runners[0] is authoritative — no cross-type fallback here.
    mode = runners[0].get("type")
    if mode in ("main-session", "subagent"):
        # Persona injection: a main-session/subagent executor runs inside the host
        # session (main-session shares its live context; a subagent has only the
        # prompt we assemble — no host agent-definition file). Neither has a bash
        # runner's fresh subprocess + hard tool sandbox, so prepend a firm
        # role-switch preamble to the assembled system prompt so prior-role/context
        # bleed cannot weaken this role (esp. the reviewer's independence).
        persona = (
            f"You are now acting SOLELY as the dev-pipeline {role.replace('_', ' ')}. "
            "This is a fresh role assignment: disregard any prior role, plan, or "
            "conversation context in this session. The instructions below and the "
            "inputs you are given are your ONLY source of truth — follow them and "
            "nothing else.\n\n"
            "Do ONLY the work THIS role's instructions below define, then STOP and "
            "hand back to the orchestrator (which then continues the pipeline loop) — "
            "do NOT take on the OTHER pipeline stages yourself. (e.g. as the "
            "implementor you write and build-check your code exactly as your "
            "instructions say, then stop; you do NOT run the project's test suite or "
            "review the diff — separate tester and reviewer roles do that.) Where this "
            "note and the role instructions below seem to differ, the role "
            "instructions win for that role's own work.\n\n---\n\n"
        )
        system_file.write_text(persona + system_text, encoding="utf-8")
        directive = ""
        # json roles get output_file as their PRIMARY (only) result channel;
        # implementor/test_implementor get it as a required status-JSON channel
        # alongside their git delta (see output_directive) — both need the
        # directive text + a clean slate.
        wants_output_file = meta["category"] == "json" or role in ("implementor", "test_implementor")
        if wants_output_file:
            directive += output_directive(runners[0])
            if output_file.exists():
                output_file.unlink()  # stale-output guard (parity with judge())
        # The host executor inherits the session cwd (possibly a subdir); pin the root.
        directive += ("\n\nOperate from this absolute project root; resolve every relative "
                      f"path (files you create/edit, test commands, globs) against it: {project_root}")
        user_file.write_text(user_text + directive, encoding="utf-8")
        payload = {"ok": True, "mode": mode, "role": role, "category": meta["category"],
                   "system_file": str(system_file), "user_file": str(user_file),
                   "output_file": str(output_file) if wants_output_file else None}
        if mode == "main-session":
            payload["compact_first"] = True
        elif runners[0].get("model"):
            payload["model"] = runners[0]["model"]
        emit(payload)
        return

    # Fresh log per run-stage invocation (not per retry/fallback runner — every
    # attempt below appends to the same file so the whole stage's activity reads
    # as one timeline); only reached on the bash path (the handoff branch above
    # already returned, and has no subprocess of its own to log).
    log_path = work / f"{role}-runner.log"
    log_path.write_text("", encoding="utf-8")

    def judge(runner: dict, command: str, timeout: "int | None"):
        """Run one command and judge it by category. Returns (problem|None, exit).
        `problem` is None on success, else a short reason string (also fed back on
        the retry)."""
        if (meta["category"] == "json" or role in ("implementor", "test_implementor")) and output_file.exists():
            output_file.unlink()  # clean slate so a stale file isn't mistaken for success
        returncode, log_tail = _run_one(command, subst, project_root, timeout, log_path)
        if returncode is None:
            return ("timeout" + (f" (log tail: {log_tail})" if log_tail.strip() else "")), None
        # On a non-zero exit, keep a log tail so a missing CLI / crash is visible
        # in the reason (file roles already include it; do it for json too).
        err = f" (exit {returncode}: {log_tail})" if returncode else ""
        if meta["category"] == "file":
            if returncode != 0:
                return f"exit {returncode}: {log_tail}", returncode
            # The code delta is the role's primary result, but its status JSON
            # (implementor/test_implementor) is now mandatory and validated the
            # same way a json role's result is — a bash runner that exits 0 but
            # fails to produce a valid status file is NOT ok:true.
            if meta.get("schema"):
                problem = _finalize_json(output_file, "default", meta["schema"])
                if problem:
                    return (problem if problem.startswith("schema:") else problem + err), returncode
            return None, returncode
        # json role — shared normalize → schema → persist-canonical (same path a
        # subagent/main-session result takes via cmd_finalize_stage).
        problem = _finalize_json(output_file, runner.get("normalizer", "default"), meta["schema"])
        if problem:
            # keep the log tail visible for produce/parse failures, as before
            return (problem if problem.startswith("schema:") else problem + err), returncode
        return None, returncode

    attempts = []
    for idx, runner in enumerate(runners):
        command = runner.get("command")
        if not command:
            attempts.append({"runner": idx, "error": "no command"})
            continue
        # No default cap: an unset `timeout` runs unbounded. Set it explicitly on
        # a runner to opt back into a hard timeout (e.g. for hang detection on a
        # json role whose log stays quiet on success — see RUNNERS.md).
        raw_timeout = runner.get("timeout")
        timeout = int(raw_timeout) if raw_timeout is not None else None
        runner_user = user_text + output_directive(runner)
        user_file.write_text(runner_user, encoding="utf-8")
        problem, exit_code = judge(runner, command, timeout)

        # One error-fed retry of the SAME runner before falling back: rewrite the
        # user prompt with the validation problem so the model can self-correct.
        # A json role always retries on any problem. A file role only retries
        # when it ran successfully but failed to produce a valid status JSON
        # (exit_code == 0 there — judge() returns the nonzero returncode itself
        # for a crash, which must NOT retry the whole implementation attempt).
        if problem and (meta["category"] == "json" or (meta.get("schema") and exit_code == 0)):
            user_file.write_text(
                runner_user + "\n\n## Your previous output was REJECTED\n" + problem +
                "\nProduce a corrected result.\n", encoding="utf-8")
            retry_problem, retry_exit = judge(runner, command, timeout)
            if retry_problem is None:
                problem, exit_code = None, retry_exit

        if problem is None:
            emit({"ok": True, "role": role, "category": meta["category"], "runner": idx,
                  "used": command, "output_file": str(output_file), "log_file": str(log_path),
                  "attempts": attempts})
            return
        attempts.append({"runner": idx, "exit": exit_code, "problem": problem})

    # Every runner failed: for any role with a schema (json roles, and
    # implementor/test_implementor's now-mandatory status file), remove any
    # partial output so a downstream `advance` cannot mistake it for a valid
    # result (n3 hardening).
    if (meta["category"] == "json" or role in ("implementor", "test_implementor")) and output_file.exists():
        output_file.unlink()

    emit({"ok": False, "role": role, "category": meta["category"],
          "reason": "all_runners_failed", "log_file": str(log_path), "attempts": attempts})
    sys.exit(2)


# ---------------------------------------------------------------------------
# Subcommand: finalize-stage (validate a main-session / subagent json result)
# ---------------------------------------------------------------------------

def cmd_finalize_stage(args) -> None:
    """Validate a role's result the SKILL obtained from a main-session/subagent
    runner: normalize (strip fences), schema-check, and persist the canonical JSON —
    the exact post-processing a bash json role gets inside run-stage. Since 6.6.0
    this also covers implementor/test_implementor's now-mandatory status JSON
    (same pipeline, keyed off schema presence rather than category) — a file
    role's git delta is still its primary content result, but its status JSON is
    validated identically to a json role's. Only a schema-less role (none
    currently exist) is a no-op here."""
    run_dir = pathlib.Path(args.run).resolve()
    role = args.role
    if role not in ROLE_META:
        die(f"Unknown role: {role}")
    meta = ROLE_META[role]
    if not meta.get("schema"):
        emit({"ok": True, "role": role, "category": meta["category"],
              "note": "no schema for this role — nothing to finalize"})
        return
    si_path = run_dir / args.stage_input if pathlib.Path(args.stage_input).name == args.stage_input else pathlib.Path(args.stage_input)
    stage_input = load_json(si_path)
    if stage_input.get("role") and stage_input["role"] != role:
        die(f"stage-input role {stage_input['role']!r} != --role {role!r}; wrong --stage-input path.")
    cfg = load_json(run_dir / "config.snapshot.json")
    work = pathlib.Path(stage_input.get("work_dir") or run_dir)
    output_file = pathlib.Path(stage_input["output_file"]) if stage_input.get("output_file") else (work / f"{role}-output.json")
    runners = cfg.get("runners", {}).get(role, [])
    first = runners[0] if runners and isinstance(runners[0], dict) else {}
    # Default the handoff normalizer to `default` (tolerates a fence AND bare JSON):
    # a host model may fence its output despite the "no fences" directive, and a
    # too-strict passthrough would dead-end the run.
    normalizer = first.get("normalizer", "default")
    problem = _finalize_json(output_file, normalizer, meta["schema"])
    if problem:
        emit({"ok": False, "role": role, "problem": problem, "output_file": str(output_file)})
        sys.exit(2)
    emit({"ok": True, "role": role, "category": meta["category"], "output_file": str(output_file)})


# ---------------------------------------------------------------------------
# Subcommand: migrate-config (legacy runners → 'unconfigured' sentinel)
# ---------------------------------------------------------------------------

def cmd_migrate_config(args) -> None:
    """Convert an old config to the current shape: reset the whole runners section
    to the 'unconfigured' sentinel (the user then reconfigures via --update-config).
    This also drops a removed role like the pre-5.0.0 spec_author and any removed
    `driver` key (e.g. the pre-6.0.0 allow_unattended_header_merge). The llm section
    is preserved; runners are reset and removed driver keys are stripped."""
    config_path = pathlib.Path(args.config).resolve()
    cfg = load_json(config_path)
    stale_driver = [k for k in _REMOVED_DRIVER_KEYS
                    if isinstance(cfg.get("driver"), dict) and k in cfg["driver"]]
    # migrate-config converts a LEGACY config. Refuse a freshly bootstrapped,
    # already-unconfigured config UNLESS it still carries a removed driver key to
    # strip (there would be nothing else to migrate).
    if _unconfigured_runner_roles(cfg) and not stale_driver:
        die(f"{config_path}: runners are already the 'unconfigured' sentinel (a freshly "
            "bootstrapped config). migrate-config converts a legacy config's runners — it is not "
            "the setup path. Run `/dev-pipeline --update-config` to configure runners.")
    legacy = _legacy_runner_roles(cfg)
    replaced = sorted((cfg.get("runners") or {}).keys())  # ALL roles are reset wholesale
    out = pathlib.Path(args.out).resolve() if args.out else config_path
    # Back up the original (in place only) before overwriting.
    if not args.out:
        save_json(out.with_suffix(out.suffix + ".bak"), cfg)
    cfg["runners"] = {role: [{"type": "unconfigured"}] for role in UNCONFIGURABLE_ROLES}
    for k in stale_driver:
        cfg["driver"].pop(k, None)
    save_json(out, cfg)
    emit({"migrated": True, "config": str(out),
          "legacy_roles": legacy, "replaced_roles": replaced,
          "dropped_driver_keys": stale_driver,
          "backup": (str(out.with_suffix(out.suffix + ".bak")) if not args.out else None),
          "warning": "ALL runners were reset to the 'unconfigured' sentinel; any removed role "
                     "(e.g. the pre-5.0.0 spec_author) or removed driver key (e.g. "
                     "allow_unattended_header_merge) is dropped and any custom runner commands "
                     "were lost (see the .bak). Run `/dev-pipeline --update-config` to reconfigure."})


# ---------------------------------------------------------------------------
# Help text
# ---------------------------------------------------------------------------

HELP_TEXT = """
dev-pipeline — automated implement → test → review loop

WORKFLOW OVERVIEW
-----------------
1. Install dev-pipeline into your project:
     bash /path/to/dev-pipeline/install.sh /path/to/project
2. Invoke the SKILL inside your agent host:
     /dev-pipeline --update-config "<plan>"         (recommend + write config.json)
     /dev-pipeline --request "<what to build>"      (planner writes plan.md for you)
     /dev-pipeline --plan plan.md                   (run an existing plan.md)
   A plan.md is a pure spec body (Requirements, Acceptance Criteria, Interface) —
   no config header. Config (runners, tester instructions, tdd_mode, …) lives only
   in config.json; --update-config recommends the values and writes them. --plan /
   --request auto-run --update-config first when the config is incomplete.

STATES
------
  init                → validate config + contract, snapshot config, write contract.md
  test_implementation → (TDD) test author writes tests from the contract
  red_test            → (TDD) tester proves the tests FAIL before any code exists
  implementation      → implementor agent writes code
  test                → tester agent runs build/install/test
  review              → reviewer runner(s) per config.runners.reviewer
  done                → commit, retrospective, (optional) self-evolution
  failed              → stopped due to exhausted iterations or environment error

  TDD flow (default; disable with driver.tdd_mode=false in the config):
    init → test_implementation → red_test → implementation → test → review → done
  Legacy flow (tdd off):
    init → implementation → test → review → done

DRIVER CLI
----------
  bootstrap-config Seed .dev-pipeline/dev-pipeline.config.json (runners left unconfigured)
  apply-config     Merge a values file into config.json (validated, atomic; --update-config)
  init             Create a new run from a plan + config   [--worktree]
  advance          Compute and apply the next state transition
  resume           Re-emit the current state's landing echo to continue an interrupted run
  cleanup-worktree Remove a --worktree run's checkout + branch (no-op if not a worktree run)
  status           Print current run state
  validate-config  Check config completeness and schema   [--plan <path>]
  validate-result  Check a test/review/implementor/test_implementor result file
  check-boundary   (TDD) verify a role only touched files it is allowed to
  record-changes   Accumulate pipeline-produced files into changed-manifest.txt
  run-stage        Execute a role via its configured runner (bash), or hand off (main-session/subagent)
  finalize-stage   Normalize + schema-validate a json result the SKILL got from a main-session/subagent runner
  migrate-config   Reset an old config's runners to the 'unconfigured' sentinel
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
  before writing code, then make them pass (GREEN). The single source is
  driver.tdd_mode; it is frozen into the run at init. When enabled,
  llm.test_implementor (focus, framework_instruction, test_paths) and
  runners.test_implementor are required.

WORKTREES
---------
  --worktree (per-run init flag, not a config key) — code edits and working-tree
  git bookkeeping (baseline/delta/review-diff/commit) happen in a fresh git
  worktree + branch (dev-pipeline/<run_id>) instead of the project's own working
  tree, isolating the run from it and allowing concurrent runs. Requires
  project_dir to be a git repo with an existing commit. On `done`, the branch is
  merged back (only after verifying project_dir is on the original branch and
  clean) and the worktree + branch are removed via `cleanup-worktree`. On
  `failed`, the worktree is preserved for debugging — clean it up manually with
  `cleanup-worktree --run <run_dir>` once you're done.

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

    p_ac = sub.add_parser("apply-config")
    p_ac.add_argument("--config", required=True)
    p_ac.add_argument("--values-file", dest="values_file", required=True)
    p_ac.add_argument("--project")

    p_init = sub.add_parser("init")
    p_init.add_argument("--plan", required=True)
    p_init.add_argument("--config")
    p_init.add_argument("--project")
    p_init.add_argument("--worktree", action="store_true")

    p_adv = sub.add_parser("advance")
    p_adv.add_argument("--run", required=True)

    p_res = sub.add_parser("resume")
    p_res.add_argument("--run", required=True)

    p_cw = sub.add_parser("cleanup-worktree")
    p_cw.add_argument("--run", required=True)

    p_sta = sub.add_parser("status")
    p_sta.add_argument("--run", required=True)

    p_vc = sub.add_parser("validate-config")
    p_vc.add_argument("--config", required=True)
    # Optional: also check a plan body (the whole plan.md is the contract), i.e.
    # validate the config exactly as `init` will see it for that plan.
    p_vc.add_argument("--plan")

    p_vr = sub.add_parser("validate-result")
    p_vr.add_argument("--type", required=True,
                      choices=["test", "review", "implementor", "test_implementor"])
    p_vr.add_argument("--file", required=True)

    p_cb = sub.add_parser("check-boundary")
    p_cb.add_argument("--run", required=True)
    p_cb.add_argument("--role", required=True, choices=["test_implementation", "implementation"])
    p_cb.add_argument("--changed", nargs="*", default=[])

    p_rc = sub.add_parser("record-changes")
    p_rc.add_argument("--run", required=True)
    p_rc.add_argument("--changed", nargs="*", default=[])

    p_rs = sub.add_parser("run-stage")
    p_rs.add_argument("--run", required=True)
    p_rs.add_argument("--role", required=True,
                      choices=list(ROLE_META.keys()))
    p_rs.add_argument("--stage-input", dest="stage_input", default="stage-input.json")

    p_fs = sub.add_parser("finalize-stage")
    p_fs.add_argument("--run", required=True)
    p_fs.add_argument("--role", required=True, choices=list(ROLE_META.keys()))
    p_fs.add_argument("--stage-input", dest="stage_input", default="stage-input.json")

    p_mc = sub.add_parser("migrate-config")
    p_mc.add_argument("--config", required=True)
    p_mc.add_argument("--out", default="")

    args = parser.parse_args()

    dispatch = {
        "bootstrap-config": cmd_bootstrap_config,
        "apply-config":     cmd_apply_config,
        "init":             cmd_init,
        "advance":          cmd_advance,
        "resume":           cmd_resume,
        "cleanup-worktree": cmd_cleanup_worktree,
        "status":           cmd_status,
        "validate-config":  cmd_validate_config,
        "validate-result":  cmd_validate_result,
        "check-boundary":   cmd_check_boundary,
        "record-changes":   cmd_record_changes,
        "run-stage":        cmd_run_stage,
        "finalize-stage":   cmd_finalize_stage,
        "migrate-config":   cmd_migrate_config,
    }

    if args.cmd not in dispatch:
        print(HELP_TEXT)
        sys.exit(0)

    dispatch[args.cmd](args)


if __name__ == "__main__":
    main()
