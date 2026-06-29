# STATE: done

**Goal:** Commit, retrospective feedback, optional self-evolution, next-step recommendations.

- [Step 1] **Commit** (if in a git repository):
  - Check: `git rev-parse --git-dir 2>/dev/null`.
  - If a git repo, stage all changes **excluding** the plan file, spec.md, and `.dev-pipeline/`:
    ```bash
    git add -A
    git reset HEAD -- <plan_path>
    git check-ignore -q .dev-pipeline || git reset HEAD -- <spec_path> <project_root>/.dev-pipeline
    ```
    Commit with a one-line summary of what was implemented and a Co-Authored-By footer naming the model executing this skill:
    ```
    <one-line summary of what was implemented>

    Co-Authored-By: Claude <noreply@anthropic.com>
    ```
    Do NOT push. (In TDD, the authored tests are part of the implementation and are committed normally.)
  - If not a git repo: inform the user that commit was skipped.

- [Step 2] **Update CLAUDE.md** — only if there is genuinely new context worth adding. Be conservative.

- [Step 3] **Workflow Retrospective Feedback** — Review `state.json` history and report the **orchestrator (main session) model** by name, and for each state that actually ran report **which runner/method carried out the work**. Include the `test_implementation` and `red_test` sections **only when `tdd_mode` is true** (omit them under `--no-tdd`):

  ```markdown
  ## Workflow Retrospective Feedback

  _Orchestrator (main session) model: <model executing this skill>._

  ### init state
  - Runner/method: main session (driver init + spec.md authored directly)
  - <issues, or "No issues">

  ### test_implementation state   (TDD only)
  - Runner/method: <e.g. claude-subagent (dp-test-implementor)>
  - <issues across all iterations, or "No issues">

  ### red_test state   (TDD only)
  - Runner/method: <e.g. claude-subagent (dp-tester)>
  - <issues, e.g. RED not confirmed and re-authoring, or "No issues">

  ### implementation state
  - Runner/method: <e.g. claude-subagent (dp-implementor) | bash (<command>)>
  - <issues, or "No issues">

  ### test state
  - Runner/method: <e.g. claude-subagent (dp-tester)>
  - <issues, or "No issues">

  ### review state
  - Runner/method: <e.g. codex-adversarial-review | claude-subagent (dp-reviewer) fallback>
  - <issues, or "No issues">
  ```

  Fill each `Runner/method` with the concrete agent/command/codex path actually used; note multi-iteration states. Be honest — if the workflow was not followed precisely (an advance out of order, a skipped validation, a boundary re-dispatch), note it.

- [Step 4] **Self-evolution** — only if `run_self_evolution: true` in the config snapshot.
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
