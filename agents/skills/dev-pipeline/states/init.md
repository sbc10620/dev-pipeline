# STATE: init

**Goal:** Validate the config and the plan body, snapshot the config, write the contract, and advance. There is **no spec-author stage** and **no config header** — the whole plan body *is* the contract (`contract.md`), produced by the driver.

- [Step 1] Run driver init:
  ```bash
  python3 <driver_path> init --plan <plan_path> --config <project_root>/.dev-pipeline/dev-pipeline.config.json --project <project_root>
  ```
  - On non-zero exit: report the error and stop. **Do not edit `.dev-pipeline/dev-pipeline.config.json` yourself to satisfy validation** (Global Rule 10) — if the config is incomplete, that is the `--update-config` flow's job (the config gate should have run first). The failure names what is wrong — a placeholder/missing tester instruction, unconfigured runners (→ run `/dev-pipeline --update-config <plan>`), or a plan body missing a required section (`## Requirements` / `## Acceptance Criteria` / `## Interface`). Tell the user which to fix.
  - On success: parse the JSON and save `run_dir`, `contract_path`, `plan_path`, and **`tdd_mode`** into the Run Context. The driver snapshotted `config.json` into the run's `config.snapshot.json` (config.json on disk is untouched) and wrote the contract to `contract_path`.

- [Step 2] Call driver advance:
  ```bash
  python3 <driver_path> advance --run <run_dir>
  ```
  Then follow `states/<next_state>.md`. It will be `test_implementation` (TDD) or `implementation` (legacy).

**Checklist:**
- [ ] `driver init` succeeded; `run_dir`, `contract_path`, `plan_path`, `tdd_mode` saved
- [ ] On failure, stopped and told the user whether to fix the plan body or run `--update-config` (did not edit the config myself)
- [ ] `driver advance` called; followed the reported `next_state`
