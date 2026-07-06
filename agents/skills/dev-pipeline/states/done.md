# STATE: done

**Goal:** Commit, retrospective feedback, optional self-evolution, next-step recommendations.

- [Step 1] **Commit** (if in a git repository). The advance that landed here echoed `tdd_mode` and `run_self_evolution` — use them; do not read `config.snapshot.json`.
  - Check: `git rev-parse --git-dir 2>/dev/null`.
  - **Manifest-based staging** (commit only files the pipeline produced, so untracked junk — cscope/ctags/build caches — never gets committed). The manifest is `<run_dir>/changed-manifest.txt`, written by `record-changes` during the authoring states.
    - **If the manifest exists:**
      ```bash
      # 1. Clear the index (drops the baseline `git add -A` junk staged during the run).
      if git -C <project_root> rev-parse --verify -q HEAD >/dev/null; then
        git -C <project_root> reset -q
      else
        # Fresh repo, no HEAD. Scoped to project_root; if project_root is a strict
        # subdir of a larger repo, files the baseline staged OUTSIDE project_root
        # stay staged — a narrow no-HEAD+subdir edge. The clean-tree precondition
        # (Step 0) makes this a non-issue in practice.
        git -C <project_root> rm -r --cached -q -- . 2>/dev/null || true
      fi
      # 2. Stage ONLY manifest paths. `add -A` also stages deletions; a path that was
      #    created and later reverted (never tracked, now absent) is silently skipped.
      while IFS= read -r p; do [ -n "$p" ] || continue
        git -C <project_root> add -A -- "$p" 2>/dev/null || true
      done < <run_dir>/changed-manifest.txt
      # 3. Defensive excludes (plan.md / .dev-pipeline must never be committed;
      #    the contract lives under .dev-pipeline so it is covered by that path).
      git -C <project_root> reset -q HEAD -- <plan_path> 2>/dev/null || true
      git -C <project_root> check-ignore -q .dev-pipeline || \
        git -C <project_root> reset -q HEAD -- <project_root>/.dev-pipeline 2>/dev/null || true
      ```
    - **If the manifest does NOT exist** (e.g. a run started by an older driver before `record-changes`): fall back to the legacy `git add -A` flow and **warn the user**: "No change manifest found — committing with `git add -A`, so untracked files are NOT filtered. Review the staged set before this commits."
      ```bash
      git -C <project_root> add -A
      git -C <project_root> reset HEAD -- <plan_path>
      git -C <project_root> check-ignore -q .dev-pipeline || git -C <project_root> reset HEAD -- <project_root>/.dev-pipeline
      ```
  - **Commit only if something is staged**, with a one-line summary and a Co-Authored-By footer naming the model executing this skill. Substitute **only** `<orchestrator model>` with your own model name (the LLM running this skill); `<noreply@dev-pipeline.local>` is a literal git-trailer email — leave it as-is. If you cannot determine your model name, use `dev-pipeline orchestrator`.
    ```bash
    git -C <project_root> diff --cached --quiet || git -C <project_root> commit -m "<summary>

    Co-Authored-By: <orchestrator model> <noreply@dev-pipeline.local>"
    ```
    Do NOT push. (In TDD, the authored tests are part of the implementation and are committed normally.)
  - If not a git repo: inform the user that commit was skipped.

- [Step 2] **Update CLAUDE.md** — only if there is genuinely new context worth adding. Be conservative.

- [Step 3] **Workflow Retrospective Feedback** — Review `state.json` history and report the **orchestrator (main session) model** by name, and for each state that actually ran report **which runner/method carried out the work**. Include the `test_implementation` and `red_test` sections **only when `tdd_mode` is true** (omit them when tdd_mode is false):

  ```markdown
  ## Workflow Retrospective Feedback

  _Orchestrator (main session) model: <model executing this skill>._

  ### planning / init state
  - Method: conversational planner (dp-planner.md) if run with --request, else the user's plan.md; init merged the header + validated the contract
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

  Fill each `Runner/method` with the concrete agent/command actually used; note multi-iteration states. Be honest — if the workflow was not followed precisely (an advance out of order, a skipped validation, a boundary re-run), note it.

- [Step 4] **Self-evolution** — only if the echoed `run_self_evolution` is true.
  - Use the retrospective findings as input. Identify which agent `.md` files (or SKILL.md / its `states/*.md`) need updating.
  - If `/advisor` is active, consult it first; otherwise apply only clearly necessary changes.
  - Edit only the **canonical** `.agents/` tree, and **resolve every path against `project_root`, not your current directory** (your cwd may be a subdirectory). These are the sole files self-evolution may touch: `<project_root>/.agents/skills/dev-pipeline/agents/dp-planner.md`, `<project_root>/.agents/skills/dev-pipeline/agents/dp-implementor.md`, `<project_root>/.agents/skills/dev-pipeline/agents/dp-test-implementor.md`, `<project_root>/.agents/skills/dev-pipeline/agents/dp-tester.md`, `<project_root>/.agents/skills/dev-pipeline/agents/dp-reviewer.md`, `<project_root>/.agents/skills/dev-pipeline/SKILL.md`, and `<project_root>/.agents/skills/dev-pipeline/states/*.md`.
  - Notify the user that source-repo files are NOT updated.
  - **If any changed, commit them** (git repo). First re-sync the whole `.agents/` skill into the `.claude/` copy Claude Code loads from (a delete-then-recopy of the whole tree, so the two never partially diverge — do NOT mirror file-by-file, which risks Claude silently loading stale prose), then stage both trees. Run these commands **verbatim, substituting `<project_root>` with the run's real project-root path** (keep the quotes):
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

- [Step 5] **Next-step recommendations** — suggest 2–3 concrete next actions for the user.

**Checklist:**
- [ ] Commit done (or skipped with notification); plan.md/.dev-pipeline (incl. contract.md) NOT committed
- [ ] Retrospective output with the orchestrator model and a section per state that ran (TDD states included only when tdd_mode)
- [ ] Self-evolution skipped or done conservatively (committed separately if any change)
- [ ] Next-step recommendations provided
