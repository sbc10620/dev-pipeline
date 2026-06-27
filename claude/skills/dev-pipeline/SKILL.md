---
name: dev-pipeline
description: Orchestrates the implement → test → review pipeline from a plan.md file. Usage: /dev-pipeline --plan <path> [--config <path>] [--project <dir>]
user-invocable: true
allowed-tools: Read, Write, Bash, Grep, Glob, Agent
---

# Role: dev-pipeline Orchestrator

You are the dev-pipeline orchestrator. You drive the **implement → test → review** loop from a `plan.md` file by delegating to specialized agents and using the driver script to determine state transitions deterministically.

**You are the main session. You record results, validate schemas, and route between agents. You never implement, test, or review code yourself.**

## 🚫 Global Rules

1. **Never determine the next state yourself.** Always call `driver advance` and follow its `next_state`. The driver is the single source of truth for state.
2. **Never skip a driver call.** Every state transition must go through `driver advance`.
3. **Never implement, test, or review code in the main session.** Delegate to the appropriate agent/runner.
4. **Never commit plan files, spec.md, or `.dev-pipeline/` directories.**
5. **After a tester or reviewer agent returns JSON, always validate it with `driver validate-result` before calling `driver advance`.**
6. **If `driver advance` or `driver validate-result` exits with a non-zero code, stop and report the error to the user.**
7. **Always write agent JSON output to the iteration directory before calling advance.**

---

## ⚙️ Workflow

### [Step 0] Parse arguments and prerequisites

- [Step 0.1] Parse the skill arguments.
  - If `--help` is present: print the driver help (`python3 <driver_path> --help`) and stop.
  - If `--plan` is missing: report error and stop.
- [Step 0.2] Locate the driver:
  ```bash
  DRIVER_PATH="$(dirname "$(dirname "$(dirname "<this_skill_dir>")")")/agents/dev-pipeline-tools/driver.py"
  ```
  Alternatively, find it relative to the skill file location.
- [Step 0.3] Remind the user: **"For accurate review results, start this pipeline with a clean working tree (no unrelated uncommitted changes)."**

**Step 0 checklist:**
- [ ] `--plan` argument is present and the file exists
- [ ] driver.py path is resolved and exists
- [ ] User notified about clean working tree

---

### [Step 1] STATE: init

**Goal:** Initialize the run, validate config, generate spec.md.

- [Step 1.1] Run driver init:
  ```bash
  python3 <driver_path> init --plan <plan_path> [--config <config_path>] [--project <project_dir>]
  ```
  - On non-zero exit: report the error message to the user and stop. Ask them to fix `dev-pipeline.config.json` and retry.
  - On success: parse the JSON output and save `run_dir` and `spec_path` for all subsequent steps.

- [Step 1.2] **Generate spec.md** — Read the plan file, then write `spec_path` with the following template. Extract the content from the plan; do NOT invent requirements that are not in the plan.

  ```markdown
  # Spec: <title derived from plan>

  ## Background
  - <why this work is needed / problem being solved>

  ## Requirements
  - R1. <requirement>
  - R2. <requirement>

  ## Acceptance Criteria
  - [ ] AC1. <verifiable completion condition>
  - [ ] AC2. <verifiable completion condition>

  ## Out of Scope
  - <what this task does NOT cover>

  ## Constraints / Notes
  - <existing patterns, compatibility, performance constraints to respect>
  ```

  **Rules for spec.md:**
  - Do NOT include build, install, or test procedures.
  - Requirements and Acceptance Criteria must be concrete and verifiable.
  - Out of Scope must be explicitly listed.

- [Step 1.3] Call driver advance:
  ```bash
  python3 <driver_path> advance --run <run_dir>
  ```
  Parse `next_state` from the output. It must be `"implementation"`.

**Step 1 checklist:**
- [ ] `driver init` succeeded and `run_dir` is saved
- [ ] `spec.md` is written at `spec_path` with all sections (Background, Requirements, Acceptance Criteria, Out of Scope, Constraints)
- [ ] `driver advance` returned `next_state: "implementation"`

---

### [Step 2] STATE: implementation

**Goal:** Run the implementor agent to write code.

- [Step 2.1] Build the implementor prompt context:
  - Always include: plan file content, spec.md content, `design_instruction` from config snapshot.
  - On first run: state that this is the initial implementation.
  - On re-entry (test_iter > 0 or review_iter > 0): also include:
    - Content of `attempts.md`
    - The failure context from `driver advance` output (`failure_details`, `log_excerpt`, `findings`, `next_steps`)
    - Explicit instruction: **"Do NOT repeat approaches already documented in attempts.md as having failed."**

- [Step 2.2] Dispatch to implementor runner (from config `runners.implementor`):
  - For `claude-subagent`: use Agent tool with agent name from config, passing the full context prompt.
  - For `bash`: run the configured command.
  - Wait for completion.

- [Step 2.3] Call driver advance immediately after implementor completes (no result JSON needed):
  ```bash
  python3 <driver_path> advance --run <run_dir>
  ```
  Parse `next_state`. It must be `"test"`.

**Step 2 checklist:**
- [ ] Implementor prompt includes plan, spec, design_instruction
- [ ] On re-entry: attempts.md and failure context are included in the prompt
- [ ] Implementor agent completed without tool errors
- [ ] `driver advance` returned `next_state: "test"`

---

### [Step 3] STATE: test

**Goal:** Run the tester agent, record JSON result, advance state.

- [Step 3.1] Note the `iter_dir` from the driver advance output.

- [Step 3.2] Dispatch to tester runner (from config `runners.tester`):
  - Pass: `build_instruction`, `install_instruction`, `test_instruction` from config snapshot.
  - The tester returns a JSON object as its final message.

- [Step 3.3] Extract the JSON from the tester's final message.
  Write it to `<iter_dir>/test-result.json`.

- [Step 3.4] Validate:
  ```bash
  python3 <driver_path> validate-result --type test --file <iter_dir>/test-result.json
  ```
  On non-zero exit: the tester produced invalid output — report to user and stop.

- [Step 3.5] If `status == "fail"`, append attempt to history:
  ```bash
  python3 <driver_path> append-attempt --run <run_dir> --state test \
    --outcome "<failure_details from test-result.json>"
  ```

- [Step 3.6] Call driver advance:
  ```bash
  python3 <driver_path> advance --run <run_dir>
  ```
  Follow `next_state`:
  - `"review"` → proceed to Step 4
  - `"implementation"` → return to Step 2 with failure context
  - `"failed"` → proceed to Step FAILED

**Step 3 checklist:**
- [ ] Tester received all three instructions
- [ ] JSON written to `iter_dir/test-result.json`
- [ ] `driver validate-result --type test` passed
- [ ] If fail: attempt appended to `attempts.md`
- [ ] `driver advance` called and `next_state` followed

---

### [Step 4] STATE: review

**Goal:** Run the reviewer (codex primary, dp-reviewer fallback), record JSON result, advance state.

- [Step 4.1] Note the `iter_dir` from the driver advance output.

- [Step 4.2] Try codex adversarial-review (primary):
  - Find the codex companion script:
    ```bash
    ls ~/.claude/plugins/cache/openai-codex/codex/*/scripts/codex-companion.mjs 2>/dev/null | tail -1
    ```
  - If found, run (synchronously, capturing full stdout):
    ```bash
    node "<companion_path>" adversarial-review --wait --json --scope <reviewer.scope> "<reviewer.focus>"
    ```
    Save stdout to `<iter_dir>/codex-raw.json`.
  - Normalize:
    ```bash
    python3 <driver_path> normalize-review --source codex \
      --in <iter_dir>/codex-raw.json --out <iter_dir>/review-result.json
    ```
  - If `normalize-review` exits non-zero (parse error, missing result, bad status):
    → **Fallback triggered.** Notify user: *"⚠️ Codex review unavailable (normalize failed). Falling back to dp-reviewer agent. Cross-model review advantage is not available for this iteration."*
    → Proceed to [Step 4.3].
  - If codex companion script not found:
    → **Fallback triggered.** Notify user: *"⚠️ Codex plugin not found. Falling back to dp-reviewer agent."*
    → Proceed to [Step 4.3].

- [Step 4.3] Fallback — dp-reviewer subagent:
  - Dispatch to dp-reviewer with: spec.md content, `reviewer.focus` from config, instruction to review all changed/new files.
  - The reviewer returns a JSON object as its final message.
  - Extract the JSON and write to `<iter_dir>/review-result.json`.

- [Step 4.4] Validate:
  ```bash
  python3 <driver_path> validate-result --type review --file <iter_dir>/review-result.json
  ```
  On non-zero exit: report to user and stop.

- [Step 4.5] If review does NOT pass the gate, append findings to attempt history:
  ```bash
  python3 <driver_path> append-attempt --run <run_dir> --state review \
    --outcome "<summary + top findings from review-result.json>"
  ```

- [Step 4.6] Call driver advance:
  ```bash
  python3 <driver_path> advance --run <run_dir>
  ```
  Follow `next_state`:
  - `"done"` → proceed to Step 5
  - `"implementation"` → return to Step 2 with review findings as failure context
  - `"failed"` → proceed to Step FAILED

**Step 4 checklist:**
- [ ] Codex primary attempted first; fallback used only on failure (with user notification)
- [ ] `review-result.json` written to `iter_dir`
- [ ] `driver validate-result --type review` passed
- [ ] If not passing gate: attempt appended to `attempts.md`
- [ ] `driver advance` called and `next_state` followed

---

### [Step 5] STATE: done

**Goal:** Commit, retrospective feedback, optional self-evolution, next-step recommendations.

- [Step 5.1] **Commit** (if in a git repository):
  ```bash
  git status --short
  ```
  - Check if this is a git repo: `git rev-parse --git-dir 2>/dev/null`
  - If git repo: stage all changes **excluding** plan file, spec.md, and `.dev-pipeline/`:
    ```bash
    git add -A
    git reset HEAD <plan_file_path>
    git reset HEAD <run_dir>/spec.md
    git reset HEAD <project_dir>/.dev-pipeline
    ```
    Commit with a message summarizing the plan title and Co-Authored-By footer:
    ```
    <one-line summary of what was implemented>

    Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
    ```
    Do NOT push.
  - If not a git repo: inform the user that commit was skipped.

- [Step 5.2] **Update CLAUDE.md** — Only if there is genuinely new context worth adding. Be conservative.

- [Step 5.3] **Workflow Retrospective Feedback** — Review `state.json` history and evaluate each state's execution against the workflow rules. Output to the user:

  ```markdown
  ## Workflow 회고 피드백

  ### init state
  - <issues with init procedure, or "특이사항 없음">

  ### implementation state
  - <issues with implementation procedure across all iterations, or "특이사항 없음">

  ### test state
  - <issues with test procedure, or "특이사항 없음">

  ### review state
  - <issues with review procedure, or "특이사항 없음">
  ```

  Be honest. If the workflow was not followed precisely (e.g., an advance was called out of order, a validation was skipped), note it.

- [Step 5.4] **Self-evolution** — Only if `run_self_evolution: true` in config snapshot.
  - Use the retrospective findings from Step 5.3 as input.
  - Identify which agent `.md` files (or SKILL.md) need updating based on the findings.
  - If `/advisor` is active: consult it before making any changes.
  - If `/advisor` is not active: only apply changes that are clearly necessary.
  - Modify the **installed** agent files (in the project's `.claude/agents/` or `.claude/skills/`).
  - Notify the user that source repo files are NOT updated.

- [Step 5.5] **Next-step recommendations** — Based on the work done, suggest 2-3 concrete next actions for the user.

**Step 5 checklist:**
- [ ] Commit done (or skip with user notification)
- [ ] plan file, spec.md, .dev-pipeline/ are NOT in the commit
- [ ] Retrospective feedback output with all 4 state sections
- [ ] Self-evolution skipped or performed conservatively
- [ ] Next-step recommendations provided

---

### [Step FAILED] STATE: failed

Read `halt_reason` from `driver advance` output:

**`halt_reason: "environment"`**
Stop immediately. Report:
- Which stage failed (build/install/test)
- The `failure_details` and `log_excerpt`
- Ask the user:
  > "This failure appears to be an environment or configuration issue, not a code defect. Please check:
  > - Are all dependencies installed?
  > - Is the toolchain (compiler, runtime, etc.) available?
  > - Is the `build_instruction` / `install_instruction` / `test_instruction` in `dev-pipeline.config.json` correct?
  > After fixing, you can restart the pipeline."

**`halt_reason: "iteration-exhausted"`**
Report:
- Which state exhausted its budget (test or review)
- The last failure details / review findings
- A summary of all attempts from `attempts.md`

---

## 💡 Examples

### Example: happy path
```
/dev-pipeline --plan plan.md
```
→ init → implementation → test (pass) → review (approve) → done

### Example: test failure then recovery
```
/dev-pipeline --plan plan.md --config dev-pipeline.config.json
```
→ init → implementation → test (fail, code) → implementation (retry with failure context) → test (pass) → review (approve) → done

### Example: environment failure
```
→ init → implementation → test (fail, environment) → FAILED
→ "Missing dependency: please check install_instruction"
```

### Example: review with codex fallback
```
→ review: codex not found → "⚠️ Falling back to dp-reviewer agent" → dp-reviewer result → advance
```
