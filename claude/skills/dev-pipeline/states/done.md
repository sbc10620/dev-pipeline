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
      # 3. Defensive excludes (plan / spec.md / .dev-pipeline must never be committed).
      git -C <project_root> reset -q HEAD -- <plan_path> 2>/dev/null || true
      git -C <project_root> check-ignore -q .dev-pipeline || \
        git -C <project_root> reset -q HEAD -- <spec_path> <project_root>/.dev-pipeline 2>/dev/null || true
      ```
    - **If the manifest does NOT exist** (e.g. a run started by an older driver before `record-changes`): fall back to the legacy `git add -A` flow and **warn the user**: "No change manifest found — committing with `git add -A`, so untracked files are NOT filtered. Review the staged set before this commits."
      ```bash
      git -C <project_root> add -A
      git -C <project_root> reset HEAD -- <plan_path>
      git -C <project_root> check-ignore -q .dev-pipeline || git -C <project_root> reset HEAD -- <spec_path> <project_root>/.dev-pipeline
      ```
  - **Commit only if something is staged**, with a one-line summary and a Co-Authored-By footer naming the model executing this skill:
    ```bash
    git -C <project_root> diff --cached --quiet || git -C <project_root> commit -m "<summary>

    Co-Authored-By: Claude <noreply@anthropic.com>"
    ```
    Do NOT push. (In TDD, the authored tests are part of the implementation and are committed normally.)
  - If not a git repo: inform the user that commit was skipped.

- [Step 2] **Update CLAUDE.md** — only if there is genuinely new context worth adding. Be conservative.

- [Step 3] **Workflow Retrospective Feedback** — Review `state.json` history and report the **orchestrator (main session) model** by name, and for each state that actually ran report **which runner/method carried out the work**. Include the `test_implementation` and `red_test` sections **only when `tdd_mode` is true** (omit them under `--no-tdd`):

  ```markdown
  ## Workflow Retrospective Feedback

  _Orchestrator (main session) model: <model executing this skill>._

  ### init state
  - Runner/method: spec_author runner (driver run-stage)
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
  - Runner/method: <e.g. bash runner (config.runners.reviewer): codex then claude fallback>
  - <issues, or "No issues">
  ```

  Fill each `Runner/method` with the concrete agent/command/codex path actually used; note multi-iteration states. Be honest — if the workflow was not followed precisely (an advance out of order, a skipped validation, a boundary re-dispatch), note it.

- [Step 4] **Self-evolution** — only if the echoed `run_self_evolution` is true.
  - Use the retrospective findings as input. Identify which agent `.md` files (or SKILL.md / its `states/*.md`) need updating.
  - If `/advisor` is active, consult it first; otherwise apply only clearly necessary changes.
  - Editable files (the only ones self-evolution may touch): `.claude/agents/dp-implementor.md`, `.claude/agents/dp-test-implementor.md`, `.claude/agents/dp-tester.md`, `.claude/agents/dp-reviewer.md`, `.claude/skills/dev-pipeline/SKILL.md`, and `.claude/skills/dev-pipeline/states/*.md`.
  - Notify the user that source-repo files are NOT updated.
  - **If any changed, commit them separately** (git repo):
    ```bash
    git add .claude/agents/dp-*.md .claude/skills/dev-pipeline/SKILL.md .claude/skills/dev-pipeline/states/*.md
    git commit -m "dev-pipeline self-evolution: <one-line summary>"
    ```
    Do NOT push. Skip if nothing changed.

- [Step 5] **Next-step recommendations** — suggest 2–3 concrete next actions for the user.

**Checklist:**
- [ ] Commit done (or skipped with notification); plan/spec.md/.dev-pipeline NOT committed
- [ ] Retrospective output with the orchestrator model and a section per state that ran (TDD states included only when tdd_mode)
- [ ] Self-evolution skipped or done conservatively (committed separately if any change)
- [ ] Next-step recommendations provided
