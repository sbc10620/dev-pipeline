# STATE: init

**Goal:** Merge the plan's config header, validate the config and the plan body, write the contract, and advance. There is **no spec-author stage** — the header-stripped plan body *is* the contract (`contract.md`), produced by the driver.

- [Step 1] Run driver init. Forward `--header-approved` **only when** the Run Context `header_approved` is true (set by `states/planning.md` on approval, or by Step 0 when the user confirmed a `--plan` header). Omit it otherwise (executable/gate header keys then come from `config.json`, not the untrusted plan):
  ```bash
  python3 <driver_path> init --plan <plan_path> --config <project_root>/.dev-pipeline/dev-pipeline.config.json --project <project_root> [--header-approved]
  ```
  - On non-zero exit: report the error and stop. **Do not edit `.dev-pipeline/dev-pipeline.config.json` yourself to satisfy validation** (Global Rule 10). The failure names what is wrong — a placeholder/missing tester instruction, a plan body missing a required section (`## Requirements` / `## Acceptance Criteria` / `## Interface`), a malformed `dev-pipeline-config` header, or a pre-3.0.0 `claude-subagent` runner (→ suggest `driver migrate-config`). Tell the user which to fix (the plan header or the config).
  - On success: parse the JSON and save `run_dir`, `contract_path`, `plan_path`, and **`tdd_mode`** into the Run Context. The driver merged the whitelisted header into the run's `config.snapshot.json` (never `config.json`) and wrote the contract to `contract_path`.
    - If `header_found` is false, note to the user: "No config header in the plan — used `config.json` as-is." If `header_skipped_exec` is non-empty, note which executable/gate keys were **not** merged (no approval) — they came from `config.json`.

- [Step 2] Call driver advance:
  ```bash
  python3 <driver_path> advance --run <run_dir>
  ```
  Then follow `states/<next_state>.md`. It will be `test_implementation` (TDD) or `implementation` (legacy).

**Checklist:**
- [ ] `driver init` succeeded; `run_dir`, `contract_path`, `plan_path`, `tdd_mode` saved; `--header-approved` forwarded only when `header_approved`
- [ ] On failure, stopped and told the user whether to fix the plan header or the config (did not edit the config myself)
- [ ] `driver advance` called; followed the reported `next_state`
