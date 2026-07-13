# STATE: init

**Goal:** Validate the config and the plan body, snapshot the config, write the contract, and advance. There is **no spec-author stage** and **no config header** — the whole plan body *is* the contract (`contract.md`), produced by the driver.

- [Step 1] Run driver init, adding `--worktree` when that flag was passed to this invocation:
  ```bash
  python3 <driver_path> init --plan <plan_path> --config <project_root>/.dev-pipeline/dev-pipeline.config.json --project <project_root> [--worktree]
  ```
  - On non-zero exit: report the error and stop. **Do not edit `.dev-pipeline/dev-pipeline.config.json` yourself to satisfy validation** (Global Rule 10) — if the config is incomplete, that is the `--update-config` flow's job (the config gate should have run first). The failure names what is wrong — a placeholder/missing tester instruction, unconfigured runners (→ run `/dev-pipeline --update-config`), a plan body missing a required section (`## Requirements` / `## Acceptance Criteria` / `## Interface`), or — under `--worktree` — `project_root` not being a git repo, having no commits yet, or `git worktree add` itself failing (report the git error verbatim; a failed `--worktree` init leaves nothing on disk, so there is nothing to clean up). Tell the user which to fix.
  - On success: parse the JSON and save `run_dir`, `contract_path`, `plan_path`, **`tdd_mode`**, and **`work_root`** into the Run Context. **Use the echoed `work_root` for every subsequent state's git bookkeeping — never `project_root`** (they're identical unless `--worktree` was passed, but state files never special-case that; they just always read `work_root`). The driver snapshotted `config.json` into the run's `config.snapshot.json` (config.json on disk is untouched) and wrote the contract to `contract_path`. Under `--worktree`, it also created the worktree checkout at `work_root` and a `dev-pipeline/<run_id>` branch there, off `project_root`'s current HEAD.

- [Step 2] Call driver advance:
  ```bash
  python3 <driver_path> advance --run <run_dir>
  ```
  Then follow `states/<next_state>.md`. It will be `test_implementation` (TDD) or `implementation` (legacy).

**Checklist:**
- [ ] `driver init` succeeded (with `--worktree` if requested); `run_dir`, `contract_path`, `plan_path`, `tdd_mode`, `work_root` saved
- [ ] On failure, stopped and told the user whether to fix the plan body, run `--update-config`, or (worktree) fix the git repo/HEAD/worktree-add problem (did not edit the config myself)
- [ ] `driver advance` called; followed the reported `next_state`
