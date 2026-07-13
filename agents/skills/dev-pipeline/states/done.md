# STATE: done

**Goal:** Commit, merge and clean up a worktree run, retrospective feedback, optional self-evolution, next-step recommendations.

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

- [Step 2] **Merge and clean up the worktree** — only if the echoed `worktree_branch` is set (this run used `--worktree`). Skip this entire step for a normal run. `project_root` is the user's **real checkout** here, so this never merges without verifying it first — a bad merge attempt would leave the one thing `--worktree` exists to protect in a conflicted state.
  - **Precondition check (mandatory, before attempting anything):**
    ```bash
    git -C <project_root> symbolic-ref --short -q HEAD   # empty/failure if detached
    git -C <project_root> status --porcelain --untracked-files=no
    ```
    - **Branch check** — the first command's output must equal the echoed `worktree_base_ref`. **If it fails or prints nothing** (`project_root` is on a different branch, or detached), the run may still be recoverable if `worktree_base_ref` is itself a commit SHA (i.e. the worktree was created from a detached HEAD, not a branch — `driver init --worktree` records a SHA in that case): fall back to comparing `git -C <project_root> rev-parse HEAD` against `worktree_base_ref` instead. If neither matches, the branch check fails.
    - **Clean-tree check** — the second command's output must be empty. **Deliberately `--untracked-files=no`**: an untracked `plan.md` sits in `project_root` in the default flow (the planner writes it there, and it is never committed — Global Rule 4) and would otherwise fail this check on every single worktree run; only *tracked* changes (modified/staged) indicate the checkout isn't safe to merge into. (Git's own merge still refuses on its own if an untracked file would be clobbered, so this doesn't weaken the actual safety net.)
    - **If either check fails, do NOT attempt the merge.** Tell the user: "This run's worktree branch (`<worktree_branch>`) is ready to merge, but `<project_root>` is not on `<worktree_base_ref>` or has uncommitted tracked changes. Switch back to `<worktree_base_ref>`, make sure it's clean, then re-run this step (or merge manually: `git -C <project_root> merge --no-ff <worktree_branch>`, then `python3 <driver_path> cleanup-worktree --run <run_dir>`)." The worktree and branch are left exactly as they are — nothing is lost, nothing is retried automatically. (A worktree created from a detached HEAD that the user can no longer reproduce exactly is expected to require this manual path — there is no branch identity to verify a return to.)
  - **Merge** (only once the precondition above passed):
    ```bash
    git -C <project_root> merge --no-ff <worktree_branch>
    ```
    - **Success** → proceed to cleanup below.
    - **Conflict/failure** → **STOP**. Do not run `git merge --abort` yourself — the user may already be resolving it, and discarding that silently would be worse than leaving it. Show the conflicting files (`git -C <project_root> diff --name-only --diff-filter=U`) and tell the user to either resolve and commit the merge, or abort it (`git -C <project_root> merge --abort`), then run `python3 <driver_path> cleanup-worktree --run <run_dir>` themselves once the branch is fully merged (or they've decided to discard it). The worktree and branch are preserved either way.
  - **Cleanup** (only immediately after a successful merge above):
    ```bash
    python3 <driver_path> cleanup-worktree --run <run_dir>
    ```
    Report `worktree_removed` / `branch_removed` from the JSON to the user. `branch_removed: false` should not happen right after a successful merge (the branch is then fully merged into `project_root`'s HEAD, so the safe delete `-d` succeeds) — if it does, relay the `branch_error` and leave it for the user to inspect rather than forcing a delete yourself.

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

  Fill each `Runner/method` with the concrete agent/command actually used; note multi-iteration states. Be honest — if the workflow was not followed precisely (an advance out of order, a skipped validation, a boundary re-run), note it. (Worktree runs: note whether the merge in [Step 2] needed manual intervention.)

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
- [ ] (worktree run) precondition checked before any merge attempt; merge succeeded and `cleanup-worktree` ran, **or** the run stopped with the worktree/branch preserved and clear manual instructions given
- [ ] Retrospective output with the orchestrator model and a section per state that ran (TDD states included only when tdd_mode)
- [ ] Self-evolution skipped or done conservatively (committed separately, against `project_root`, if any change)
- [ ] Next-step recommendations provided
