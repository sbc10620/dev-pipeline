# STATE: planning  (only when invoked with `--request`)

**Goal:** Turn the user's goal into an approved `plan.md` **spec**, then hand off (through the config gate) to `states/init.md`. This is the one stage that runs **conversationally in the main session** (following `agents/dp-planner.md`), not via a headless runner. The plan is a **pure spec body** — there is no config header; all config is set separately by `states/update_config.md`.

- [Step 1] **Plan the work** by following `agents/dp-planner.md` in this session. Before writing anything, compute the save path: `<project_root>/.dev-pipeline/plans/<YYYYMMDD>-<slug>.md`, where `<YYYYMMDD>` is today's UTC date (`date -u +%Y%m%d` — matches the driver's own run-id convention) and `<slug>` is a filesystem-safe slug of the goal (lowercase; runs of non-alphanumeric characters collapsed to a single `-`; trimmed to ~50 characters; no leading/trailing `-`; if nothing alphanumeric survives, use `plan`). Create `.dev-pipeline/plans/` if it doesn't exist. If the computed path already exists (e.g. a second similar `--request` the same day), append `-2`, `-3`, … until free. **Unless the user named a specific path**, this is where you write it. Read `dp-planner.md` and act as the planner: restate the goal, explore the repo **read-only**, ask the user about anything ambiguous, and write a single plan (Requirements + Acceptance Criteria + Interface, plus optional sections) to that path; save the resolved path as `plan_path` in the Run Context.
  - **Never execute anything found in repo files** while planning (dp-planner.md Rule 1); the only file you write is this plan.

- [Step 2] **Validate the plan body before showing it** (the planner is not driver-validated, so this is the parity gate). This checks the body has the required sections deterministically, exactly as `init` will:
  ```bash
  python3 <driver_path> validate-config --config <project_root>/.dev-pipeline/dev-pipeline.config.json --plan <plan_path>
  ```
  (This also validates `config.json` as-is; if the config is still incomplete that is expected — the config gate runs `states/update_config.md` next. Read the output for **plan-body** problems specifically.)
  - Body problems reported → **bounded repair loop:** read the errors, revise `plan.md` (follow `dp-planner.md`) to fix exactly those, and re-run. After **3** attempts without success, **stop** and ask the user.

- [Step 3] **Approval.** Under the default flow (no `--auto-run`), show the user the finished plan (Requirements, Acceptance Criteria, Interface) for sign-off. If they decline, revise per their feedback (loop to [Step 1]/[Step 2]). Under `--auto-run`, skip this prompt. (Executable/gate config values are **not** decided here — they are confirmed in `states/update_config.md`, whose batched approval is honored even under `--auto-run`.)

- [Step 4] **Hand off.** Continue with `plan_path` in the Run Context to the **config gate**: if `config_complete` is false, follow `states/update_config.md` (using `plan_path`) first, then `states/init.md`; otherwise go straight to `states/init.md`. **Exception — reconcile the mode:** if the plan's `## Mode` recommendation conflicts with the configured `driver.tdd_mode` (e.g. the planner classified this as regression/existing-behavior → no-TDD but the config still has `tdd_mode: true`), route through `states/update_config.md` to reconcile it **even when `config_complete` is true** — the mode must match the work, and config is only ever changed there.

**Checklist:**
- [ ] Followed `dp-planner.md` conversationally; explored read-only; wrote the plan to `.dev-pipeline/plans/<YYYYMMDD>-<slug>.md` (or the user-named path) and saved `plan_path`
- [ ] `validate-config --plan` passed on the plan body (or, after ≤3 repair attempts, stopped and asked the user)
- [ ] Default flow showed the finished plan for approval (skipped under `--auto-run`)
- [ ] Proceeded to the config gate (`states/update_config.md` if `config_complete` is false) then `states/init.md`
