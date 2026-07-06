#!/usr/bin/env python3
"""
dev-pipeline driver — deterministic state machine for the implement→test→review loop.

Usage:
  python3 driver.py bootstrap-config [--project <dir>]
  python3 driver.py init             --plan <path> [--config <path>] [--project <dir>] [--header-approved]
  python3 driver.py advance          --run <run_dir>
  python3 driver.py status           --run <run_dir>
  python3 driver.py validate-config  --config <path> [--plan <path>] [--header-approved]
  python3 driver.py validate-result  --type test|review --file <path>
  python3 driver.py normalize-review --source codex --in <file> --out <file>
  python3 driver.py append-attempt   --run <run_dir> --state <test_implementation|red_test|test|review> --outcome <text-or-file>
  python3 driver.py check-boundary   --run <run_dir> --role <test_implementation|implementation> --changed <file...>
  python3 driver.py record-changes   --run <run_dir> --changed <file...>
  python3 driver.py run-stage        --run <run_dir> --role <role> [--stage-input <file>]
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
import subprocess
import sys
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Single source of truth for the dev-pipeline version. driver.py is the only
# executable copied into installs, so install.sh and state.json read this value
# rather than maintaining their own copy.
__version__ = "5.1.0"

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


def effective_tdd_mode(cfg: dict) -> bool:
    """Resolve TDD mode. As of 5.0.0 the single source is config.driver.tdd_mode
    (the plan.md `dev-pipeline-config` header sets it via the init merge); the
    per-run --tdd/--no-tdd flags were removed. Default true when unset."""
    return bool(cfg.get("driver", {}).get("tdd_mode", True))


def _is_placeholder(val) -> bool:
    return isinstance(val, str) and val.strip().startswith("<") and val.strip().endswith(">")


def _legacy_runner_roles(cfg: dict) -> list:
    """Roles whose runners use a type removed in 3.0.0 (pre-schema detection)."""
    legacy = []
    for role, arr in (cfg.get("runners") or {}).items():
        if isinstance(arr, list) and any(
            isinstance(r, dict) and (r.get("type") in ("claude-subagent", "codex-adversarial-review")
                                     or "agent" in r)
            for r in arr):
            legacy.append(role)
    return sorted(set(legacy))


def validate_config_data(cfg: dict) -> list[str]:
    # 3.0.0 migration: surface removed runner types with an actionable message
    # BEFORE the generic schema enum error.
    legacy = _legacy_runner_roles(cfg)
    if legacy:
        return [f"runners.{', '.join(legacy)}: use a runner type removed in 3.0.0 "
                "(claude-subagent / codex-adversarial-review). Replace each with "
                '{"type":"bash","command":"..."}, or run `driver migrate-config '
                "--config <path>` to convert automatically."]

    # spec_author was removed in 5.0.0 (the plan.md body is the contract now). A
    # config still carrying its runner is rejected with an actionable message
    # rather than a cryptic additionalProperties schema error.
    if isinstance(cfg.get("runners"), dict) and "spec_author" in cfg["runners"]:
        return ["runners.spec_author: removed in 5.0.0 — the plan.md body is the contract "
                "and there is no spec-author stage. Delete runners.spec_author, or run "
                "`driver migrate-config --config <path>` to drop it automatically."]

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
# plan.md config header (5.0.0)
#
# plan.md carries a leading ```dev-pipeline-config fenced JSON block. `init`
# parses it, merges a trust-tiered whitelist into the run's config *snapshot*
# (never config.json), and validates the header-stripped body's required
# sections deterministically. plan.md is UNTRUSTED input, so: only whitelisted
# leaves ever merge (never `runners`, which become shell commands); executable /
# gate keys merge only with human approval; parsing is fail-closed.
# ---------------------------------------------------------------------------

HEADER_INFO_STRING = "dev-pipeline-config"

# Prose-guidance keys: merged on every run — they are only ever handed to an LLM
# as data, never executed and never a hard gate.
_HEADER_PROSE_KEYS = {
    ("llm", "implementor", "design_instruction"),
    ("llm", "reviewer", "focus"),
    ("llm", "reviewer", "scope"),
    ("llm", "test_implementor", "focus"),
    ("llm", "test_implementor", "framework_instruction"),
}
# Executable / gate keys: tester.* strings are RUN as shell; test_paths is the
# boundary + review-routing gate; review_block_severity / tdd_mode change control
# flow. Merged from the (untrusted) header ONLY when a human approved this header
# (init --header-approved) or the project opted in via
# driver.allow_unattended_header_merge. Otherwise they come from config.json and
# validation fails loudly if config.json left them as placeholders.
_HEADER_EXEC_KEYS = {
    ("llm", "tester", "build_instruction"),
    ("llm", "tester", "install_instruction"),
    ("llm", "tester", "test_instruction"),
    ("llm", "test_implementor", "test_paths"),
    ("driver", "review_block_severity"),
    ("driver", "tdd_mode"),
}


def parse_plan_header(text: str):
    """Fail-closed parse of a plan.md `dev-pipeline-config` header.

    Returns (header_dict | None, body_text).
      * Recognized ONLY when the file's first non-whitespace content is a fenced
        block whose info string is exactly `dev-pipeline-config` (leading BOM
        tolerated). This position anchor stops a dev-pipeline-config *example*
        inside a plan body from being mis-parsed/mis-stripped.
      * A recognized header that is not valid JSON is a HARD die — never a silent
        fall-through to "no header" (which would run on stale config values that
        differ per planner model). Body JSON examples are never extracted.
      * A genuinely absent header returns (None, text); the caller must surface it.
    """
    # Normalize line endings once so a CRLF/CR plan.md (Windows or some LLMs) is
    # parsed identically to LF — otherwise the trailing \r would break the fence
    # regexes and the header would be silently dropped (fail-open). The returned
    # body is therefore LF too, which is fine for contract.md.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    raw = text.lstrip("﻿")
    stripped = raw.lstrip()
    m = re.match(r"(`{3,}|~{3,})[ \t]*([^\s`~]*)[ \t]*\n", stripped)
    if not m or m.group(2) != HEADER_INFO_STRING:
        return None, text
    fence = m.group(1)
    rest = stripped[m.end():]
    # Closing fence: same char, at least as long as the opener, on its own line
    # (up to 3 leading spaces allowed, per CommonMark).
    close = re.search(r"^[ ]{0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}[ \t]*$",
                      rest, re.MULTILINE)
    if not close:
        die("plan.md config header: opening `dev-pipeline-config` fence has no closing fence.")
    json_text = rest[:close.start()]
    body = rest[close.end():]
    try:
        header = json.loads(json_text)
    except json.JSONDecodeError as e:
        die(f"plan.md config header is not valid JSON: {e}. Fix the "
            "```dev-pipeline-config block (no trailing commas, comments, or smart quotes).")
    if not isinstance(header, dict):
        die("plan.md config header must be a JSON object.")
    return header, body


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


def merge_plan_header(cfg: dict, header: dict, exec_allowed: bool):
    """Per-leaf deep-merge the whitelisted header leaves into cfg (in place).

    Only whitelisted leaves are copied — never `runners`, never any key outside
    the prose/exec whitelists. Prose keys always merge; exec/gate keys merge only
    when exec_allowed. Siblings the header omits are preserved (per-leaf, not
    subtree replace). Returns (applied, skipped_exec) dotted-path lists for the
    caller to report.
    """
    applied, skipped_exec = [], []
    if not isinstance(header, dict):
        return applied, skipped_exec
    for path in sorted(_HEADER_PROSE_KEYS | _HEADER_EXEC_KEYS):
        present, val = _get_nested(header, list(path))
        if not present:
            continue
        dotted = ".".join(path)
        if path in _HEADER_EXEC_KEYS and not exec_allowed:
            skipped_exec.append(dotted)
            continue
        _set_nested(cfg, list(path), val)
        applied.append(dotted)
    return applied, skipped_exec


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
    """Deterministic required-section + non-empty check on the header-stripped
    plan body (replaces the old LLM `INSUFFICIENT` refusal). Same bar as the
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

    # --- Parse + merge the plan.md config header into the in-memory cfg (which
    #     becomes this run's config.snapshot.json). config.json on disk is NEVER
    #     rewritten (per-run, idempotent). plan.md is untrusted: only whitelisted
    #     leaves merge; executable/gate keys need approval (--header-approved) or
    #     the durable driver.allow_unattended_header_merge opt-in. ---
    plan_text = plan_path.read_text(encoding="utf-8")
    header, body = parse_plan_header(plan_text)
    exec_allowed = bool(getattr(args, "header_approved", False)) or \
        bool(cfg.get("driver", {}).get("allow_unattended_header_merge", False))
    header_applied, header_skipped_exec = [], []
    if header is not None:
        header_applied, header_skipped_exec = merge_plan_header(cfg, header, exec_allowed)

    tdd_mode = effective_tdd_mode(cfg)

    # --- Validate the MERGED config AND the plan body BEFORE any disk change, so a
    #     rejected plan never leaves a half-created run that `advance` could pick
    #     up (the section gate must not be bypassable). ---
    errors = validate_config_data(cfg)
    if errors:
        sys.stderr.write("[dev-pipeline] Config validation failed:\n")
        for e in errors:
            sys.stderr.write(f"  - {e}\n")
        sys.stderr.write("\nFix the plan.md `dev-pipeline-config` header or "
                         ".dev-pipeline/dev-pipeline.config.json and retry.\n")
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

    # The contract = the header-stripped plan body. It is the single artifact the
    # downstream roles (test author, implementor, reviewer) read; there is no
    # separate spec.md and no spec-author stage as of 5.0.0.
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

    # save the MERGED config snapshot (config.json on disk is untouched)
    save_json(run_dir / "config.snapshot.json", cfg)

    # initialise attempts.md
    (run_dir / "attempts.md").write_text(
        "# Attempt History\n\n_No attempts recorded yet._\n", encoding="utf-8"
    )

    notes = []
    if header is None:
        notes.append("No `dev-pipeline-config` header found in the plan — using config.json as-is.")
    if header_skipped_exec:
        notes.append("Executable/gate header keys were NOT merged (no approval): "
                     + ", ".join(header_skipped_exec) + " — they come from config.json.")
    emit({
        "state": "init",
        "run_id": rid,
        "run_dir": str(run_dir),
        "contract_path": str(contract_path),
        "plan_path": str(plan_path),
        "tdd_mode": tdd_mode,
        "header_found": header is not None,
        "header_applied": header_applied,
        "header_skipped_exec": header_skipped_exec,
        "directive": "advance",
        "next_action": "advance",
        "message": ("Init successful. " + (" ".join(notes) + " " if notes else "")
                    + "Call `driver advance --run <run_dir>` to enter the first stage."),
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
        elif new_state == "done":
            e["run_self_evolution"] = cfg.get("driver", {}).get("run_self_evolution", False)
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
        save_state(run_dir, state)
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
        }
        result.update(dest_echoes(new_state))
        if extra:
            result.update(extra)
        # Persist a stage-input.json next to the iteration so `driver run-stage`
        # (bash-runner mode) can consume the same context the SKILL echo carries.
        si = build_stage_input(result, state.get("project_dir", ""))
        if si and si.get("work_dir") and si["work_dir"] != ".":
            save_json(pathlib.Path(si["work_dir"]) / "stage-input.json", si)
        emit(result)

    attempts_path = str(run_dir / "attempts.md")
    # The contract handed to every downstream role. `.get` fallback keeps a run
    # created just before 5.0.0's rename resumable.
    contract_path = state.get("contract_path") or state.get("spec_path")

    # --- init ---
    if current == "init":
        # The contract (header-stripped plan body) is written by `init` itself, so
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
                       "test_implementor framework_instruction + test_paths — or let a plan.md "
                       "`dev-pipeline-config` header supply them per run. Placeholder <...> values "
                       "are rejected. To skip TDD, set driver.tdd_mode=false. Then re-run "
                       "/dev-pipeline --request \"<goal>\" (or --plan <path>).",
    })


# ---------------------------------------------------------------------------
# Subcommand: validate-config
# ---------------------------------------------------------------------------

def cmd_validate_config(args) -> None:
    config_path = pathlib.Path(args.config).resolve()
    cfg = load_json(config_path)
    plan_path = getattr(args, "plan", None)
    body = None
    header_applied = []
    if plan_path:
        # Validate the config EXACTLY as `init` will see it, using the SAME
        # exec-key trust rule: executable/gate header keys merge only when the
        # header is approved (--header-approved, passed by the SKILL when the plan
        # will be approved) or the durable driver.allow_unattended_header_merge is
        # set. Mirroring init here keeps the planning pre-approval gate honest —
        # a plan that passes this check passes init under the same approval state.
        pp = pathlib.Path(plan_path).resolve()
        if not pp.exists():
            die(f"Plan file not found: {pp}")
        header, body = parse_plan_header(pp.read_text(encoding="utf-8"))
        exec_allowed = bool(getattr(args, "header_approved", False)) or \
            bool(cfg.get("driver", {}).get("allow_unattended_header_merge", False))
        if header is not None:
            header_applied, _ = merge_plan_header(cfg, header, exec_allowed)
    errors = validate_config_data(cfg)
    if body is not None:
        errors += [f"plan body: {p}" for p in validate_plan_body(body, effective_tdd_mode(cfg))]
    if errors:
        sys.stderr.write("[dev-pipeline] Config validation FAILED:\n")
        for e in errors:
            sys.stderr.write(f"  - {e}\n")
        sys.exit(1)
    emit({"valid": True, "config": str(config_path),
          "plan": str(pathlib.Path(plan_path).resolve()) if plan_path else None,
          "header_applied": header_applied})


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
    "test_implementor": {"category": "file",  "schema": None,            "prompt": "dp-test-implementor"},
    "implementor":      {"category": "file",  "schema": None,            "prompt": "dp-implementor"},
    "tester":           {"category": "json",  "schema": "test-result",   "prompt": "dp-tester"},
    "reviewer":         {"category": "json",  "schema": "review-result", "prompt": "dp-reviewer"},
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
    `passthrough` parses it directly; the *-cli variants tolerate a markdown
    fence or surrounding prose by extracting the outermost JSON object."""
    raw = raw.strip()
    if normalizer in ("claude-cli", "codex-cli"):
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


def _run_one(command: str, subst: dict, project_root: pathlib.Path, timeout: int) -> "subprocess.CompletedProcess":
    """Substitute placeholders (shell-quoted) into a command template and run it
    in a shell with cwd=project_root."""
    cmd = command
    for key, val in subst.items():
        cmd = cmd.replace("{" + key + "}", shlex.quote(str(val)))
    return subprocess.run(cmd, shell=True, cwd=str(project_root),
                          capture_output=True, text=True, timeout=timeout)


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
    project_root = pathlib.Path(stage_input.get("project_root") or state.get("project_dir") or ".").resolve()

    # Guard against passing the wrong stage-input (a mismatched iteration/role
    # path) — the SKILL always passes the matching path.
    if stage_input.get("role") and stage_input["role"] != role:
        die(f"stage-input role {stage_input['role']!r} != --role {role!r}; wrong --stage-input path.")

    runners = cfg.get("runners", {}).get(role, [])
    if not runners:
        die(f"config.runners.{role} is empty — nothing to run.")
    # A run created by a pre-3.0.0 driver has subagent runners (no `command`) frozen
    # in its snapshot; run-stage cannot drive those. Fail with an explicit reason.
    if any(isinstance(r, dict) and not r.get("command") for r in runners):
        die(f"config.runners.{role} has a runner with no `command` — this run was likely "
            "created by a pre-3.0.0 driver. Start a new run (its config snapshot is frozen).")

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
        """Per-runner output instruction: a runner that redirects stdout to
        `{output_file}` (e.g. `codex exec … > {output_file}`) must PRINT the result;
        one that writes via a tool must WRITE the file. Matches the actual mechanism."""
        if meta["category"] != "json":
            return ""
        what = "a single valid JSON object (no markdown fences, nothing else)"
        if re.search(r">\s*\{output_file\}", runner.get("command", "")):
            return f"\n\nOutput {what} to **stdout** only."
        return f"\n\nWrite {what} to this exact file path and nothing else there: {output_file}"

    def judge(runner: dict, command: str, timeout: int):
        """Run one command and judge it by category. Returns (problem|None, exit).
        `problem` is None on success, else a short reason string (also fed back on
        the retry)."""
        if meta["category"] == "json" and output_file.exists():
            output_file.unlink()  # clean slate so a stale file isn't mistaken for success
        try:
            proc = _run_one(command, subst, project_root, timeout)
        except subprocess.TimeoutExpired:
            return "timeout", None
        # On a non-zero exit, keep a stderr tail so a missing CLI / crash is visible
        # in the reason (file roles already include it; do it for json too).
        err = f" (exit {proc.returncode}: {proc.stderr.strip()[-300:]})" if proc.returncode else ""
        if meta["category"] == "file":
            return (None if proc.returncode == 0 else f"exit {proc.returncode}: {proc.stderr[-300:]}"), proc.returncode
        if not output_file.exists():
            return "output not produced" + err, proc.returncode
        # json role
        result = _normalize_output(output_file.read_text(encoding="utf-8"),
                                   runner.get("normalizer", "passthrough"))
        if result is None:
            return "no valid JSON in output" + err, proc.returncode
        if meta["schema"]:
            errs = validate_against_schema(result, f"{meta['schema']}.schema.json")
            if errs:
                return "schema: " + "; ".join(errs[:3]), proc.returncode
        # Persist the NORMALIZED JSON back to the file. The runner may have written
        # a markdown-fenced or prose-wrapped payload (common with many models);
        # _normalize_output tolerated it for validation, but every downstream
        # consumer (driver advance, the SKILL) reads this file with a plain
        # json.loads. Writing the canonical object back makes the result robust to
        # how the model formatted its output.
        output_file.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return None, proc.returncode

    attempts = []
    for idx, runner in enumerate(runners):
        command = runner.get("command")
        if not command:
            attempts.append({"runner": idx, "error": "no command"})
            continue
        timeout = int(runner.get("timeout", 600))
        runner_user = user_text + output_directive(runner)
        user_file.write_text(runner_user, encoding="utf-8")
        problem, exit_code = judge(runner, command, timeout)

        # One error-fed retry of the SAME runner before falling back: rewrite the
        # user prompt with the validation problem so the model can self-correct.
        if problem and meta["category"] == "json":
            user_file.write_text(
                runner_user + "\n\n## Your previous output was REJECTED\n" + problem +
                "\nProduce a corrected result.\n", encoding="utf-8")
            retry_problem, retry_exit = judge(runner, command, timeout)
            if retry_problem is None:
                problem, exit_code = None, retry_exit

        if problem is None:
            emit({"ok": True, "role": role, "category": meta["category"], "runner": idx,
                  "used": command, "output_file": str(output_file), "attempts": attempts})
            return
        attempts.append({"runner": idx, "exit": exit_code, "problem": problem})

    # Every runner failed: for json roles, remove any partial output so a
    # downstream `advance` cannot mistake it for a valid result (n3 hardening).
    if meta["category"] == "json" and output_file.exists():
        output_file.unlink()

    emit({"ok": False, "role": role, "category": meta["category"],
          "reason": "all_runners_failed", "attempts": attempts})
    sys.exit(2)


# ---------------------------------------------------------------------------
# Subcommand: migrate-config (pre-3.0.0 → bash runners)
# ---------------------------------------------------------------------------

def cmd_migrate_config(args) -> None:
    """Convert an old config to the current bash-runner shape: replace the whole
    runners section with the canonical bash defaults. This also drops a removed
    role like the pre-5.0.0 spec_author (no longer a runner). The driver and llm
    sections are preserved; only runners are rewritten."""
    config_path = pathlib.Path(args.config).resolve()
    cfg = load_json(config_path)
    legacy = _legacy_runner_roles(cfg)
    replaced = sorted((cfg.get("runners") or {}).keys())  # ALL roles are replaced wholesale
    example = load_json(EXAMPLE_PATH)
    out = pathlib.Path(args.out).resolve() if args.out else config_path
    # Back up the original (in place only) before overwriting runners wholesale.
    if not args.out:
        save_json(out.with_suffix(out.suffix + ".bak"), cfg)
    cfg["runners"] = example["runners"]
    save_json(out, cfg)
    emit({"migrated": True, "config": str(out),
          "legacy_roles": legacy, "replaced_roles": replaced,
          "backup": (str(out.with_suffix(out.suffix + ".bak")) if not args.out else None),
          "warning": "ALL runners were replaced with the current bash defaults; any removed role "
                     "(e.g. the pre-5.0.0 spec_author) is dropped and any custom runner commands "
                     "were lost (see the .bak). Review and customize."})


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
     /dev-pipeline --request "<what to build>"      (planner writes plan.md for you)
     /dev-pipeline --plan plan.md                   (run an existing plan.md)
   A plan.md carries a leading ```dev-pipeline-config header (tester instructions,
   tdd_mode, …) plus a spec body (Requirements, Acceptance Criteria, Interface).
   On first run the config is bootstrapped from the template; the plan header can
   supply the per-run instructions.

STATES
------
  init                → merge plan header + validate config/contract, write contract.md
  test_implementation → (TDD) test author writes tests from the contract
  red_test            → (TDD) tester proves the tests FAIL before any code exists
  implementation      → implementor agent writes code
  test                → tester agent runs build/install/test
  review              → reviewer runner(s) per config.runners.reviewer
  done                → commit, retrospective, (optional) self-evolution
  failed              → stopped due to exhausted iterations or environment error

  TDD flow (default; disable with driver.tdd_mode=false in the config/header):
    init → test_implementation → red_test → implementation → test → review → done
  Legacy flow (tdd off):
    init → implementation → test → review → done

DRIVER CLI
----------
  bootstrap-config Seed .dev-pipeline/dev-pipeline.config.json from the template
  init             Create a new run from a plan + config  [--header-approved]
  advance          Compute and apply the next state transition
  status           Print current run state
  validate-config  Check config completeness and schema   [--plan <path>]
  validate-result  Check a test-result or review-result file
  normalize-review Convert codex --json payload → canonical review-result JSON
  append-attempt   Log a failed attempt to attempts.md for implementor context
  check-boundary   (TDD) verify a role only touched files it is allowed to
  record-changes   Accumulate pipeline-produced files into changed-manifest.txt
  run-stage        Execute a role via its configured bash runner (assemble prompt, run, validate)
  migrate-config   Convert an old config's runners to the current bash defaults
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
  tdd_mode (config / plan header, default true) — author tests first and prove
  they fail (RED) before writing code, then make them pass (GREEN). The single
  source is driver.tdd_mode (a plan.md header can set it); it is frozen into the
  run at init. When enabled, llm.test_implementor (focus, framework_instruction,
  test_paths) and runners.test_implementor are required.

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
    # Human approved this plan's header (per --request approval / --plan confirm),
    # so its executable/gate keys (tester.* commands, test_paths, review_block_
    # severity, tdd_mode) may be merged from the untrusted plan.md. Without it,
    # those keys come from config.json (unless driver.allow_unattended_header_merge).
    p_init.add_argument("--header-approved", dest="header_approved", action="store_true")

    p_adv = sub.add_parser("advance")
    p_adv.add_argument("--run", required=True)

    p_sta = sub.add_parser("status")
    p_sta.add_argument("--run", required=True)

    p_vc = sub.add_parser("validate-config")
    p_vc.add_argument("--config", required=True)
    # Optional: merge a plan.md header and check the plan body, i.e. validate the
    # config exactly as `init` will see it for that plan. --header-approved mirrors
    # init's trust gate so the check matches init under the same approval state.
    p_vc.add_argument("--plan")
    p_vc.add_argument("--header-approved", dest="header_approved", action="store_true")

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

    p_rs = sub.add_parser("run-stage")
    p_rs.add_argument("--run", required=True)
    p_rs.add_argument("--role", required=True,
                      choices=list(ROLE_META.keys()))
    p_rs.add_argument("--stage-input", dest="stage_input", default="stage-input.json")

    p_mc = sub.add_parser("migrate-config")
    p_mc.add_argument("--config", required=True)
    p_mc.add_argument("--out", default="")

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
        "run-stage":        cmd_run_stage,
        "migrate-config":   cmd_migrate_config,
    }

    if args.cmd not in dispatch:
        print(HELP_TEXT)
        sys.exit(0)

    dispatch[args.cmd](args)


if __name__ == "__main__":
    main()
