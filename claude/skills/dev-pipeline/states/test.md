# STATE: test  (GREEN)

**Goal:** Run the tester, record JSON, advance. (Reached after implementation, or after a test-repair pass in TDD.)

The advance that landed here echoed `directive: run_tester`, `iter_dir`, the three `*_instruction` values, and `tester_runners`. **Use the echoed values — do not read `config.snapshot.json`.**

- [Step 1] Use the echoed `iter_dir` for this step.

- [Step 2] Dispatch the tester runner — try the echoed `tester_runners` array front-to-back (default `dp-tester`), passing the echoed `build_instruction`, `install_instruction`, `test_instruction`. The tester returns a JSON object as its final message. Do **not** run the commands yourself. Pass **only** the three instructions: do **NOT** specify or invent an output schema in the prompt — `dp-tester` already defines its result schema, and overriding it causes `validate-result` failures.

- [Step 3] Extract the JSON and write it to `<iter_dir>/test-result.json`.

- [Step 4] Validate:
  ```bash
  python3 <driver_path> validate-result --type test --file <iter_dir>/test-result.json
  ```
  On non-zero exit, the tester produced invalid output. **Do NOT run the commands yourself** (Global Rule 3). Re-dispatch the tester **once** with the exact error text, overwrite the file, and re-validate. If still invalid, report and stop.

- [Step 5] Call driver advance:
  ```bash
  python3 <driver_path> advance --run <run_dir>
  ```

- [Step 6] If `next_state == "implementation"` (test failed, retry), append the failure to attempt history **after** advance (so the counter label is accurate). Write `failure_details` (+ `log_excerpt`) from `test-result.json` to a temp file first:
  ```bash
  python3 <driver_path> append-attempt --run <run_dir> --state test --outcome-file <run_dir>/.attempt-tmp.md
  ```

- [Step 7] Follow `states/<next_state>.md` (`review` on pass, `implementation` on a code failure, `failed` if exhausted/environment).

**Checklist:**
- [ ] Build/install/test commands run ONLY by the tester, never the main session
- [ ] JSON written to `<iter_dir>/test-result.json`
- [ ] `driver validate-result --type test` passed (after at most one re-dispatch)
- [ ] `driver advance` called before any `append-attempt`; followed the reported `next_state`
