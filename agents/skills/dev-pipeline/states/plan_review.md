# STATE: plan_review  (`--plan-review` only)

**Goal:** Run a standalone, read-only adversarial review of an already-written `plan.md` and report the findings. This is **not** a pipeline stage — it has no `run_dir`, is never invoked automatically during `states/planning.md`, and never chains into the config gate or `states/init.md`. `plan_reviewer` is **opt-in**: unlike `implementor`/`test_implementor`/`tester`/`reviewer`, it does not exist in a freshly bootstrapped config and never affects `config_complete` — you configure it once, the first time it's needed, scoped to just its own keys.

- [Step 1] **Ensure `plan_reviewer` is configured.** Load `.dev-pipeline/dev-pipeline.config.json` and check for a real (non-`unconfigured`) `runners.plan_reviewer` array and a non-placeholder `llm.plan_reviewer.focus`. If either is missing:
  - Tell the user this is a **one-time setup** for the plan reviewer specifically (their other roles, if any, are untouched). Recommend a runner conversationally using the same reasoning as `states/update_config.md` §Step 3's runner guidance: prefer `bash`/`subagent` for independence (a `main-session` plan reviewer run in the same session that wrote the plan is not guaranteed independent — same caveat as a `main-session` code reviewer); scope a bash runner's tools to **read-only** (`Read`/`Grep`/`Glob`, no `Write`/`Edit`/`Bash`) exactly like the reviewer (see `RUNNERS.md`'s `plan_reviewer` templates). Recommend `llm.plan_reviewer.focus` (what to emphasize — ambiguity, coverage gaps, testability, scope, mode classification; a sensible default mirrors the reviewer's adversarial framing, adapted to a spec instead of a diff).
  - Get the user's explicit approval, then write **only** the `plan_reviewer` keys — never touch any other role — via:
    ```bash
    python3 <driver_path> apply-config --config <project_root>/.dev-pipeline/dev-pipeline.config.json --values-file <scratch-values.json>
    ```
    where the scratch file is `{"llm":{"plan_reviewer":{"focus":"..."}},"runners":{"plan_reviewer":[...]}}`. This is the same sanctioned config-write path `states/update_config.md` uses (SKILL Global Rule 10) — never hand-edit `config.json`. Non-zero exit → show the user the exact error and retry with a fix, same bounded-repair spirit as `states/update_config.md` §Step 4 (3 attempts, then stop and ask).
  - If both are already configured, skip straight to [Step 2].

- [Step 2] **Run the review.**
  ```bash
  python3 <driver_path> review-plan --plan <plan_path> --config <project_root>/.dev-pipeline/dev-pipeline.config.json
  ```
  Read the JSON output — it follows the **exact same contract as `run-stage`** (this command assembles the prompt and executes `config.runners.plan_reviewer` internally), so handle it identically to any other role:
  - **No `mode` field** (a bash result) — `ok: true` → continue to [Step 3]. `ok: false` with `reason: "all_runners_failed"` → stop and report the `attempts` to the user; do not retry yourself.
  - **`mode: "subagent"` / `"main-session"`** — follow SKILL [§Role Execution] exactly as for any other role (dispatch a subagent with the assembled prompt verbatim, or perform it yourself after compacting), then validate with:
    ```bash
    python3 <driver_path> finalize-stage --run <review_dir> --role plan_reviewer --stage-input stage-input.json
    ```
    `<review_dir>` is the directory containing the echoed `system_file`/`user_file`/`output_file` (their common parent) — read it from those paths, never guessed or reused from an earlier run. `ok: true` → continue. `ok: false` → re-execute once with the `problem` appended (same one-retry rule every json role gets); still failing → stop and report.

- [Step 3] **Present the result.** Read the validated `plan-review-result.json` (the `output_file` from Step 2) and show the user the `verdict`, `summary`, and every finding (`severity`, `title`, `body`, `section`, `recommendation`) in full — do not summarize away a finding. Remind the user this is advisory: the plan reviewer never edited `plan.md`, so any revision is theirs (or the planner's, run again) to make.

- [Step 4] **Stop.** `--plan-review` ends here — do not continue into `states/planning.md`, the config gate, or `states/init.md`, and do not re-invoke the reviewer yourself even on a `needs-revision` verdict. Tell the user they can revise `plan.md` and re-run `/dev-pipeline --plan-review <plan_path>` to check again, or run `/dev-pipeline --plan <plan_path>` themselves once satisfied.

**Checklist:**
- [ ] Checked `runners.plan_reviewer` + `llm.plan_reviewer.focus`; if missing, recommended + approved + applied via `apply-config` scoped to only those keys (bounded repair loop on failure)
- [ ] Ran `driver review-plan`; handled a bash `ok`/`all_runners_failed` result, or followed [§Role Execution] + `finalize-stage` for a subagent/main-session handoff
- [ ] Showed the user `verdict`, `summary`, and every finding in full
- [ ] Stopped without touching `plan.md` and without entering planning/config-gate/init
