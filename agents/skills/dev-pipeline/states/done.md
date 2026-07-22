# STATE: done

**Goal:** Commit, merge and clean up a worktree run, retrospective feedback, optional self-evolution, next-step recommendations.

**Completing [Step 1] (the commit) is not the end of this state** — continue through [Step 6] (or the applicable skip conditions in between) before telling the user the run is finished; the checklist below lists everything that must be true first.

- [Step 1] **Commit** (if in a git repository). The advance that landed here echoed `tdd_mode`, `run_self_evolution`, `work_root`, `worktree_branch`, and `worktree_base_ref` — use them; do not read `config.snapshot.json`. **Run every command below against `work_root`, not `project_root`** — identical under a normal run, but `work_root` is the isolated worktree checkout under `--worktree` (see `states/init.md`); this commit lands on the worktree's own branch there, not on `project_root`'s checkout — [Step 2] is what brings it into `project_root`.
  - Check: `git rev-parse --git-dir 2>/dev/null`.
  - **Manifest-based staging** (commit only files the pipeline produced, so untracked junk — cscope/ctags/build caches — never gets committed). The manifest is `<run_dir>/changed-manifest.txt`, written by `record-changes` during the authoring states.
    - **If the manifest exists:**
      ```bash
      # 1. Clear the index (drops the baseline `git add -A` junk staged during the run).
      if git -C <work_root> rev-parse --verify -q HEAD >/dev/null; then
        git -C <work_root> reset -q
      else
        # Fresh repo, no HEAD. Scoped to work_root; if work_root is a strict
        # subdir of a larger repo, files the baseline staged OUTSIDE work_root
        # stay staged — a narrow no-HEAD+subdir edge. The clean-tree precondition
        # (Step 0) makes this a non-issue in practice.
        git -C <work_root> rm -r --cached -q -- . 2>/dev/null || true
      fi
      # 2. Stage ONLY manifest paths. `add -A` also stages deletions; a path that was
      #    created and later reverted (never tracked, now absent) is silently skipped.
      while IFS= read -r p; do [ -n "$p" ] || continue
        git -C <work_root> add -A -- "$p" 2>/dev/null || true
      done < <run_dir>/changed-manifest.txt
      # 3. Defensive excludes (plan.md / .dev-pipeline must never be committed;
      #    the contract lives under .dev-pipeline so it is covered by that path).
      git -C <work_root> reset -q HEAD -- <plan_path> 2>/dev/null || true
      git -C <work_root> check-ignore -q .dev-pipeline || \
        git -C <work_root> reset -q HEAD -- <work_root>/.dev-pipeline 2>/dev/null || true
      ```
    - **If the manifest does NOT exist** (e.g. a run started by an older driver before `record-changes`): fall back to the legacy `git add -A` flow and **warn the user**: "No change manifest found — committing with `git add -A`, so untracked files are NOT filtered. Review the staged set before this commits."
      ```bash
      git -C <work_root> add -A
      git -C <work_root> reset HEAD -- <plan_path>
      git -C <work_root> check-ignore -q .dev-pipeline || git -C <work_root> reset HEAD -- <work_root>/.dev-pipeline
      ```
  - **Commit only if something is staged**, with a one-line summary and a Co-Authored-By footer naming the model executing this skill. Substitute **only** `<orchestrator model>` with your own model name (the LLM running this skill); `<noreply@dev-pipeline.local>` is a literal git-trailer email — leave it as-is. If you cannot determine your model name, use `dev-pipeline orchestrator`.
    ```bash
    git -C <work_root> diff --cached --quiet || git -C <work_root> commit -m "<summary>

    Co-Authored-By: <orchestrator model> <noreply@dev-pipeline.local>"
    ```
    Do NOT push. (In TDD, the authored tests are part of the implementation and are committed normally.)
  - If not a git repo: inform the user that commit was skipped.

- [Step 2] **Rebase and merge the worktree branch, then clean up** — only if the echoed `worktree_branch` is set (this run used `--worktree`). Skip this entire step for a normal run. `project_root` is the user's **real checkout** here, so this never touches it without verifying first — a bad rebase/merge attempt would leave the one thing `--worktree` exists to protect in a conflicted state.
  - **Precondition check (mandatory, before attempting anything):**
    ```bash
    git -C <project_root> symbolic-ref --short -q HEAD   # empty/failure if detached
    git -C <project_root> status --porcelain --untracked-files=no
    ```
    - **Branch check** — the first command's output must equal the echoed `worktree_base_ref`. **If it fails or prints nothing** (`project_root` is on a different branch, or detached), the run may still be recoverable if `worktree_base_ref` is itself a commit SHA (i.e. the worktree was created from a detached HEAD, not a branch — `driver init --worktree` records a SHA in that case): fall back to comparing `git -C <project_root> rev-parse HEAD` against `worktree_base_ref` instead. If neither matches, the branch check fails.
    - **Clean-tree check** — the second command's output must be empty. **Deliberately `--untracked-files=no`**: an untracked `plan.md` may still sit in `project_root` under `--plan <path>` (a user-supplied plan can live anywhere in the tracked tree, and is never committed — Global Rule 4) — `--request`-generated plans no longer hit this at all, since they're gitignored under `.dev-pipeline/plans/` by default (`states/planning.md`) — and would otherwise fail this check on every such worktree run; only *tracked* changes (modified/staged) indicate the checkout isn't safe to merge into. (Git's own fast-forward merge still refuses on its own if an untracked file would be clobbered, so this doesn't weaken the actual safety net — see the fast-forward merge step below.)
    - **If either check fails, do NOT attempt the rebase.** Tell the user: "This run's worktree branch (`<worktree_branch>`) is ready to merge, but `<project_root>` is not on `<worktree_base_ref>` or has uncommitted tracked changes. Switch back to `<worktree_base_ref>`, make sure it's clean, then re-run this step (or finish manually: `git -C <work_root> rebase <worktree_base_ref>`, then `git -C <project_root> merge --ff-only <worktree_branch>`, then `python3 <driver_path> cleanup-worktree --run <run_dir>`)." The worktree and branch are left exactly as they are — nothing is lost, nothing is retried automatically. (A worktree created from a detached HEAD that the user can no longer reproduce exactly is expected to require this manual path — there is no branch identity to verify a return to.)
  - **Work_root readiness check** (only once the precondition above passed) — the rebase below runs IN `work_root`, so it also needs a clean tracked tree there; the check above says nothing about this (it only reads `project_root`). **Run these two checks FIRST, before touching anything** — do not run `clean -xdf` until both pass, since a check failure may be exactly the thing worth investigating (e.g. leftover untracked files from a still-unresolved manual step), and discarding that silently before the user sees it would repeat the same mistake this step's conflict-handling explicitly avoids elsewhere:
    ```bash
    git -C <work_root> symbolic-ref --short -q HEAD   # must equal <worktree_branch>
    git -C <work_root> status --porcelain --untracked-files=no
    ```
    - **If the HEAD check doesn't match `<worktree_branch>`, or the status check is non-empty** (tracked, uncommitted changes — e.g. a test-stage side effect outside the manifest that [Step 1] didn't commit), **STOP — this is a precondition failure, not a rebase conflict** (do not follow the "Rebase" conflict-recovery instructions below for this). Do **not** run `clean -xdf`. Tell the user exactly that distinction, show the offending path(s), and ask them to commit or discard it in `work_root` (or investigate why HEAD moved) before re-running this step.
    - **Only once both checks above pass**, drop untracked leftovers (e.g. test-stage caches) before rebasing:
      ```bash
      git -C <work_root> clean -xdf
      ```
  - **Rebase** (only once both precondition checks above passed) — replays the worktree branch's commits onto `worktree_base_ref`'s current tip, producing linear history with no merge commit. The worktree shares `project_root`'s underlying git object database, so this sees the branch's LIVE tip with no fetch needed, correctly picking up any commits `project_root` gained since the worktree was created (for a branch `worktree_base_ref`; a detached-HEAD SHA `worktree_base_ref` is frozen by definition, and the precondition above already requires `project_root` to still be at that exact SHA, so this is consistent either way):
    ```bash
    git -C <work_root> rebase <worktree_base_ref>
    ```
    - **Success** → proceed to the fast-forward merge below.
    - **Conflict/failure** → **STOP**. Do not run `git rebase --abort` yourself — the user may already be resolving it, and discarding that silently would be worse than leaving it. Show the conflicting files (`git -C <work_root> diff --name-only --diff-filter=U`) and give the user two distinct paths, each with its own next action (do not conflate them into one generic "then re-run"):
      - **Resolve and `git -C <work_root> rebase --continue`** — once the rebase itself reports done, re-run this step (it will re-check both preconditions and proceed to the fast-forward merge), or finish manually: `git -C <project_root> merge --ff-only <worktree_branch>`, then `python3 <driver_path> cleanup-worktree --run <run_dir>`.
      - **Abort with `git -C <work_root> rebase --abort`** — recoverable: this restores the pre-rebase branch, nothing is lost, though the worktree sits mid-rebase until then, not simply untouched. This alone does **not** resolve the conflict — merely re-running this step will hit the same conflict again. Only re-run once the user has actually done something to change the outcome (fixed the offending commit(s) on the branch, or `worktree_base_ref` has since moved).
      The worktree and branch are preserved either way.
  - **Fast-forward merge** (only immediately after a successful rebase above) — the rebase just replayed the branch directly onto `project_root`'s current tip, so this is normally a pure fast-forward, never a 3-way merge:
    ```bash
    git -C <project_root> merge --ff-only <worktree_branch>
    ```
    - **Success** → proceed to cleanup below.
    - **Failure** → **STOP** and report; do not force anything. Two known causes, both safe (neither moves `project_root`'s HEAD): (a) a race — `project_root`'s branch moved again in the narrow window between the two commands, or (b) an **ignore-not-covered untracked file in `project_root`** collides with a path the rebased commits add (`"untracked working tree files would be overwritten"` — git's own protection, not a bug). For (a), re-running this step (which re-checks both preconditions and re-rebases onto the new tip) is the correct recovery. For (b), re-running alone will loop — tell the user which path(s) collided (from the error output) and ask them to move/remove/commit it in `project_root` first.
  - **Cleanup** (only immediately after a successful fast-forward merge above):
    ```bash
    python3 <driver_path> cleanup-worktree --run <run_dir>
    ```
    Report `worktree_removed` / `branch_removed` from the JSON to the user. `branch_removed: false` should not happen right after a successful fast-forward merge (the branch is then fully merged into `project_root`'s HEAD, so the safe delete `-d` succeeds) — if it does, relay the `branch_error` and leave it for the user to inspect rather than forcing a delete yourself.

- [Step 3] **Update the project's agent memory doc** (`AGENTS.md`, the open standard read by Codex/Cline/Cursor/…; some hosts use `CLAUDE.md`, often a symlink to it — update whichever the project has) — only if there is genuinely new context worth adding. Be conservative.

- [Step 4] **Workflow Retrospective Feedback** — Review `state.json` history and report the **orchestrator (main session) model** by name, and for each state that actually ran report **which runner/method carried out the work**. Include the `test_implementation` and `red_test` sections **only when `tdd_mode` is true** (omit them when tdd_mode is false):

  ```markdown
  ## Workflow Retrospective Feedback

  _Orchestrator (main session) model: <model executing this skill>._

  ### planning / init state
  - Method: conversational planner (dp-planner.md) if run with --request, else the user's plan.md; config set via --update-config; init validated the config + contract
  - <issues, or "No issues">

  ### test_implementation state   (TDD only)
  - Runner/method: <e.g. bash runner (config.runners.test_implementor)>
  - <issues across all iterations, or "No issues">

  ### red_test state   (TDD only)
  - Runner/method: <e.g. bash runner (config.runners.tester)>
  - <issues, e.g. RED not confirmed and re-authoring, or "No issues">

  ### implementation state
  - Runner/method: <e.g. bash runner (config.runners.implementor)>
  - <issues, or "No issues">

  ### test state
  - Runner/method: <e.g. bash runner (config.runners.tester)>
  - <issues, or "No issues">

  ### review state
  - Runner/method: <e.g. bash runner(s) per config.runners.reviewer order>
  - <issues, or "No issues">
  ```

  Fill each `Runner/method` with the concrete agent/command actually used; note multi-iteration states. Be honest — if the workflow was not followed precisely (an advance out of order, a skipped validation, a boundary re-run), note it. (Worktree runs: note whether the rebase/merge in [Step 2] needed manual intervention.)

- [Step 5] **Self-evolution** — only if the echoed `run_self_evolution` is true.
  - Use the retrospective findings as input. Identify which agent `.md` files (or SKILL.md / its `states/*.md`) need updating.
  - If your host provides a dedicated advisory/code-review capability, consult it first; otherwise apply only clearly necessary changes.
  - Edit only the **canonical** `.agents/` tree, and **resolve every path against `project_root`, not your current directory** (your cwd may be a subdirectory) — this is independent of `work_root`/`--worktree`: the skill/agent prose always lives in and is committed from `project_root`, never the worktree. These are the sole files self-evolution may touch: `<project_root>/.agents/skills/dev-pipeline/agents/dp-planner.md`, `<project_root>/.agents/skills/dev-pipeline/agents/dp-implementor.md`, `<project_root>/.agents/skills/dev-pipeline/agents/dp-test-implementor.md`, `<project_root>/.agents/skills/dev-pipeline/agents/dp-tester.md`, `<project_root>/.agents/skills/dev-pipeline/agents/dp-reviewer.md`, `<project_root>/.agents/skills/dev-pipeline/SKILL.md`, and `<project_root>/.agents/skills/dev-pipeline/states/*.md`.
  - Notify the user that source-repo files are NOT updated.
  - **If any changed, commit them** (git repo). First re-sync the whole `.agents/` skill into the `.claude/` copy Claude Code loads from (a delete-then-recopy of the whole tree, so the two never partially diverge — do NOT mirror file-by-file, which risks the host silently loading stale prose), then stage both trees. Run these commands **verbatim, substituting `<project_root>` with the run's real project-root path** (keep the quotes):
    ```bash
    if [ -d "<project_root>/.claude/skills/dev-pipeline" ]; then
      rm -rf "<project_root>/.claude/skills/dev-pipeline" && \
        cp -R "<project_root>/.agents/skills/dev-pipeline" "<project_root>/.claude/skills/dev-pipeline"
    fi
    git -C "<project_root>" add .agents/skills/dev-pipeline
    [ -d "<project_root>/.claude/skills/dev-pipeline" ] && git -C "<project_root>" add .claude/skills/dev-pipeline
    git -C "<project_root>" diff --cached --quiet || \
      git -C "<project_root>" commit -m "dev-pipeline self-evolution: <one-line summary>"
    ```
    (Codex/Cursor/etc. read `.agents/` directly, so they need no mirror.) Do NOT push. Skip if nothing changed.

- [Step 6] **Next-step recommendations** — suggest 2–3 concrete next actions for the user.

**Checklist:**
- [ ] Commit done against `work_root` (or skipped with notification); plan.md/.dev-pipeline (incl. contract.md) NOT committed
- [ ] (worktree run) both preconditions (`project_root` AND `work_root`) checked before any rebase attempt; rebase + fast-forward merge succeeded and `cleanup-worktree` ran, **or** the run stopped with the worktree/branch preserved and clear manual instructions given (including which of the three failure classes — precondition / rebase conflict / fast-forward failure — applied)
- [ ] Retrospective output with the orchestrator model and a section per state that ran (TDD states included only when tdd_mode)
- [ ] Self-evolution skipped or done conservatively (committed separately, against `project_root`, if any change)
- [ ] Next-step recommendations provided
