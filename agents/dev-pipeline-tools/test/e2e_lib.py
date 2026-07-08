"""Shared end-to-end orchestration engine for the dev-pipeline harnesses.

This module encodes, in code, what the SKILL + `states/*.md` do when a host
LLM drives a run: it loops `driver advance`, runs the role for each landed
state via `driver run-stage`, computes the git delta, enforces the boundary,
records the manifest, builds the reviewer's change diff, and makes the
manifest-scoped `done` commit. It is **runner-agnostic** — the caller supplies
a project whose `config.runners.*` are either dummy bash commands
(`test_e2e.py`, deterministic, no LLM) or real `claude` commands
(`e2e_llm.py`). Only the runners differ; this flow is shared (DRY).

It intentionally MIRRORS `states/*.md`; if those step commands change, update
this file to match (see: implementation.md / test_implementation.md / red_test.md
/ test.md / review.md / done.md). It does NOT validate the SKILL prose itself
(a host LLM following SKILL.md does that) — only the driver + this copy of the
git choreography.
"""
import json
import os
import pathlib
import subprocess

# Run git with the developer's global/system config neutralized, so a machine
# with commit.gpgsign / core.hooksPath doesn't break the harness commits.
GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}


class PipelineError(RuntimeError):
    """Raised when a stage fails to produce a valid result or the run halts."""


ROLE_BY_DIRECTIVE = {
    "run_test_implementor": "test_implementor",
    "run_implementor": "implementor",
    "run_tester": "tester",
    "run_reviewer": "reviewer",
}
# The check-boundary role name differs from the runner role name.
BOUNDARY_ROLE = {"test_implementor": "test_implementation", "implementor": "implementation"}


def _run(cmd, cwd=None, env=None):
    return subprocess.run(cmd, capture_output=True, text=True,
                          cwd=str(cwd) if cwd else None, env=env)


def _driver(driver_path, *args):
    """Run a driver subcommand; return (proc, parsed_json_or_None)."""
    proc = _run(["python3", str(driver_path), *args])
    parsed = None
    if proc.returncode == 0 and proc.stdout.strip():
        try:
            parsed = json.loads(proc.stdout)
        except json.JSONDecodeError:
            parsed = None
    return proc, parsed


def _git(proj, *args):
    return _run(["git", "-C", str(proj), *args], env=GIT_ENV)


def _delta(proj):
    """This turn's delta: modified/deleted tracked + new untracked, sorted-unique,
    project_root-relative — exactly the two commands in implementation.md Step 3."""
    a = _git(proj, "-c", "core.quotePath=false", "diff", "--name-only", "--relative").stdout.split("\n")
    b = _git(proj, "-c", "core.quotePath=false", "ls-files", "--others", "--exclude-standard").stdout.split("\n")
    return sorted({p for p in a + b if p.strip()})


def _manifest_paths(run_dir):
    m = pathlib.Path(run_dir) / "changed-manifest.txt"
    if not m.exists():
        return []
    return [ln.strip() for ln in m.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _write_review_diff(proj, run_dir, changes_diff):
    """review.md Step 1: surface new files (intent-to-add, ONE PATH AT A TIME so a
    stale path can't abort the whole `add -N`), diff, then undo. Returns the diff
    text. Mirrors the manifest-present and the no-manifest / no-HEAD fallbacks."""
    paths = _manifest_paths(run_dir)
    has_head = _git(proj, "rev-parse", "--verify", "-q", "HEAD").returncode == 0
    if paths:
        for p in paths:
            _git(proj, "add", "-N", "--", p)   # tolerated failures ok (stale paths)
    else:
        _git(proj, "add", "-N", ".")           # no-manifest fallback
    if has_head:
        d = _git(proj, "diff", "HEAD", "--", *paths) if paths else _git(proj, "diff", "HEAD")
    else:
        # no HEAD: worktree diff (intent-to-add is invisible to `diff --cached`)
        d = _git(proj, "diff", "--", *paths) if paths else _git(proj, "diff")
    pathlib.Path(changes_diff).write_text(d.stdout, encoding="utf-8")
    if paths:
        _git(proj, "reset", "-q", "--", *paths)
    else:
        _git(proj, "reset", "-q")
    return d.stdout


def _done_commit(proj, run_dir, plan_path):
    """done.md: manifest-scoped commit (stage only manifest paths; exclude plan.md
    and .dev-pipeline). Returns the commit sha, or None if nothing was staged."""
    paths = _manifest_paths(run_dir)
    if _git(proj, "rev-parse", "--verify", "-q", "HEAD").returncode == 0:
        _git(proj, "reset", "-q")
    else:
        _git(proj, "rm", "-r", "--cached", "-q", "--", ".")   # fresh repo, no HEAD
    for p in paths:
        _git(proj, "add", "-A", "--", p)
    _git(proj, "reset", "-q", "HEAD", "--", str(plan_path))
    _git(proj, "reset", "-q", "HEAD", "--", ".dev-pipeline")
    if not _git(proj, "diff", "--cached", "--name-only").stdout.strip():
        return None
    _git(proj, "-c", "user.email=e2e@dev-pipeline.local", "-c", "user.name=dp-e2e",
         "commit", "-q", "-m", "dev-pipeline e2e run")
    return _git(proj, "rev-parse", "HEAD").stdout.strip()


def _run_stage(driver_path, run_dir, role, iter_dir):
    si = str(pathlib.Path(iter_dir) / "stage-input.json")
    proc, j = _driver(driver_path, "run-stage", "--run", run_dir, "--role", role, "--stage-input", si)
    if not (j and j.get("ok")):
        raise PipelineError(f"run-stage {role} failed: {proc.stdout}\n{proc.stderr}")
    return j


def _diff_paths(diff_text):
    """The set of file paths present in a unified diff (from its `+++ b/<path>`
    lines) — used to prove new files actually reached the reviewer's diff."""
    out = set()
    for ln in diff_text.splitlines():
        if ln.startswith("+++ b/"):
            out.add(ln[len("+++ b/"):])
    return out


def run_pipeline_to_done(project_root, driver_path, plan_path, config_path,
                         tdd_mode, header_approved=True, max_steps=40):
    """Drive a run from init to done/failed, mirroring the SKILL. Returns a
    summary dict. Raises PipelineError on a stage failure."""
    proj = pathlib.Path(project_root)
    driver_path = pathlib.Path(driver_path)

    args = ["init", "--plan", str(plan_path), "--config", str(config_path), "--project", str(proj)]
    if header_approved:
        args.append("--header-approved")
    iproc, init = _driver(driver_path, *args)
    if not init:
        raise PipelineError(f"init failed: {iproc.stdout}\n{iproc.stderr}")
    run_dir = init["run_dir"]

    trace = ["init"]
    boundary = []
    review_diff_paths = []
    review_diff_had_new_file = None
    state = "init"

    for _ in range(max_steps):
        aproc, adv = _driver(driver_path, "advance", "--run", run_dir)
        if not adv:
            raise PipelineError(f"advance failed: {aproc.stdout}\n{aproc.stderr}")
        state = adv["next_state"]
        trace.append(state)
        if state in ("done", "failed"):
            break

        # Use the frozen tdd_mode the driver echoes, never the caller's flag
        # (the repo's single-channel rule — see AGENTS.md determinism section).
        state_tdd = adv.get("tdd_mode", tdd_mode)
        role = ROLE_BY_DIRECTIVE.get(adv.get("directive"))
        iter_dir = adv.get("iter_dir")

        if role in ("test_implementor", "implementor"):
            _git(proj, "add", "-A")                      # boundary/manifest baseline
            _run_stage(driver_path, run_dir, role, iter_dir)
            delta = _delta(proj)
            if state_tdd:                                # boundary check is TDD-only
                bproc, bj = _driver(driver_path, "check-boundary", "--run", run_dir,
                                    "--role", BOUNDARY_ROLE[role], "--changed", *delta)
                ok = bool(bj and bj.get("ok"))
                boundary.append({"role": role, "ok": ok, "changed": delta})
                if not ok:
                    raise PipelineError(f"boundary {role} failed: {bproc.stdout}")
            rproc, _ = _driver(driver_path, "record-changes", "--run", run_dir, "--changed", *delta)
            if rproc.returncode != 0:
                raise PipelineError(f"record-changes failed: {rproc.stdout}\n{rproc.stderr}")

        elif role == "tester":
            _run_stage(driver_path, run_dir, "tester", iter_dir)

        elif role == "reviewer":
            diff_text = _write_review_diff(proj, run_dir, adv["changes_diff"])
            review_diff_paths = sorted(_diff_paths(diff_text))
            review_diff_had_new_file = "new file" in diff_text
            _run_stage(driver_path, run_dir, "reviewer", iter_dir)
    else:
        raise PipelineError(f"exceeded {max_steps} steps without terminating (trace={trace})")

    commit = _done_commit(proj, run_dir, plan_path) if state == "done" else None
    _, st = _driver(driver_path, "status", "--run", run_dir)
    return {
        "final_state": state,
        "trace": trace,
        "run_dir": run_dir,
        "iterations": (st or {}).get("iterations"),
        "boundary": boundary,
        "review_diff_paths": review_diff_paths,
        "review_diff_had_new_file": review_diff_had_new_file,
        "commit": commit,
    }
