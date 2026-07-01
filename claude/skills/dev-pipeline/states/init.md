# STATE: init

**Goal:** Initialize the run, validate config, author spec.md (via the spec-author runner), advance.

- [Step 1] Run driver init, forwarding the TDD flag from Step 0 if the user passed one:
  ```bash
  python3 <driver_path> init --plan <plan_path> --config <project_root>/.dev-pipeline/dev-pipeline.config.json --project <project_root> [--tdd|--no-tdd]
  ```
  (Omit the flag entirely if the user passed neither; the config's `tdd_mode` then decides.)
  - On non-zero exit: report the error to the user and stop. **Do not edit `.dev-pipeline/dev-pipeline.config.json` yourself to satisfy validation** (Global Rule 10) — tell the user what is wrong (e.g. a missing `llm.test_implementor`, a placeholder instruction, or a pre-3.0.0 `claude-subagent` runner → suggest `driver migrate-config`) and let them fix it.
  - On success: parse the JSON and save `run_dir`, `spec_path`, `plan_path`, and **`tdd_mode`** into the Run Context. The driver wrote `<run_dir>/stage-input.json` for the spec author and echoed `directive: run_spec_author`.

- [Step 2] **Author the spec** — run the spec-author runner (the driver assembles the prompt from `dp-spec-author.md` + the plan and runs the configured LLM):
  ```bash
  python3 <driver_path> run-stage --run <run_dir> --role spec_author --stage-input <run_dir>/stage-input.json
  ```
  Read the emitted JSON:
  - `ok: true` → spec.md was written to `spec_path`. Continue to [Step 3].
  - `ok: false`, `reason: "insufficient"` → the plan is too vague to specify (in TDD, untestable). **Stop.** Show the user the `message` and ask them to make the plan more concrete, or to re-run with `--no-tdd`. Do not advance.
  - `ok: false`, `reason: "all_runners_failed"` → report the `attempts` (the runner could not produce a valid spec) and stop.

- [Step 3] Call driver advance:
  ```bash
  python3 <driver_path> advance --run <run_dir>
  ```
  Then follow `states/<next_state>.md`. It will be `test_implementation` (TDD) or `implementation` (legacy).

**Checklist:**
- [ ] `driver init` succeeded; `run_dir`, `spec_path`, `plan_path`, `tdd_mode` saved
- [ ] `run-stage --role spec_author` returned `ok: true` (spec.md written); on `insufficient`/`all_runners_failed`, stopped and reported instead of advancing
- [ ] `driver advance` called; followed the reported `next_state`
