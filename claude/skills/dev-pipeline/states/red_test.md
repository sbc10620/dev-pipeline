# STATE: red_test  (TDD only)

**Goal:** Prove the freshly authored tests FAIL before any code exists (the RED phase). **A failing test run is the success condition here.**

The advance that landed here echoed `directive: run_tester`, `iter_dir`, `result_filename: "red-test-result.json"`, and the three `*_instruction` values. This is the same tester as the `test` state — only the result filename and the driver's interpretation differ.

- [1] Use the echoed `iter_dir` for this step.

- [2] Dispatch the tester runner (from config `runners.tester`, default `dp-tester`), passing the echoed `build_instruction`, `install_instruction`, `test_instruction`. The tester returns a JSON object as its final message. Do **not** run the commands yourself.

- [3] Extract the tester JSON and write it to `<iter_dir>/red-test-result.json` (note the `red-` prefix — this must NOT overwrite `test-result.json`).

- [4] Validate (it uses the same schema as a normal test result):
  ```bash
  python3 <driver_path> validate-result --type test --file <iter_dir>/red-test-result.json
  ```
  On non-zero exit, re-dispatch the tester **once** with the exact error text and re-validate. If still invalid, report and stop.

- [5] Call driver advance:
  ```bash
  python3 <driver_path> advance --run <run_dir>
  ```
  The driver interprets the result:
  - **tests failed (RED confirmed)** → `next_state: implementation`.
  - **tests passed (RED not confirmed)** → `next_state: test_implementation` (re-author stronger tests), or `failed` if the re-authoring budget is exhausted.
  - **environment failure** → `failed` (`halt_reason: environment`).

- [6] If `next_state == "test_implementation"` (RED not confirmed), append the outcome to attempt history **after** advance:
  ```bash
  # Write a short "tests passed with no implementation — vacuous" note to <run_dir>/.attempt-tmp.md, then:
  python3 <driver_path> append-attempt --run <run_dir> --state red_test --outcome-file <run_dir>/.attempt-tmp.md
  ```

- [7] Follow `states/<next_state>.md`.

**Checklist:**
- [ ] Tester dispatched; commands run ONLY by the tester
- [ ] JSON written to `<iter_dir>/red-test-result.json` (not `test-result.json`)
- [ ] `driver validate-result --type test` passed
- [ ] `driver advance` called (before any `append-attempt`); followed the reported `next_state`
