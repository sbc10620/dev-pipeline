# STATE: review

**Goal:** Prepare the change diff, run the reviewer runner, advance (the driver applies the gate).

The advance that landed here echoed `directive: run_reviewer`, `iter_dir`, `contract_path`, and `changes_diff` (the path the reviewer will read). The driver persisted the reviewer context to `<iter_dir>/stage-input.json`, with `output_file` set to `<iter_dir>/review-result.json`.

- [Step 1] **Write the change diff** to the echoed `changes_diff` path so the reviewer can read it. Scope it to the pipeline's manifest when present (so unrelated/untracked files are not reviewed). **Run from `project_root`.** Check for an initial commit: `git -C <project_root> rev-parse --verify HEAD 2>/dev/null`.
  - **Manifest present** (`<run_dir>/changed-manifest.txt`): `git -C <project_root> diff HEAD -- <manifest paths> > <changes_diff>`. New (untracked) manifest files are not in a diff-vs-HEAD; they remain on disk for the reviewer to Read.
  - **No manifest / no HEAD (fallback):** `git -C <project_root> diff HEAD > <changes_diff>` (or `git -C <project_root> diff --cached > <changes_diff>` on a repo with no HEAD).

- [Step 2] **Run the reviewer:**
  ```bash
  python3 <driver_path> run-stage --run <run_dir> --role reviewer --stage-input <iter_dir>/stage-input.json
  ```
  The runner reviews the diff + contract (read-only on the code) and writes a schema-valid `review-result.json` to `<iter_dir>`. Its runner list and tool envelope live in `config.runners.reviewer`. Read the JSON:
  - `ok: true` → a valid review result was written; proceed.
  - `ok: false` → every reviewer runner failed; stop and report the `attempts`.

- [Step 3] Call driver advance. The driver applies the configured gate and, on failure, routes by where the blocking findings point (test files → `test_implementation`; production → `implementation`):
  ```bash
  python3 <driver_path> advance --run <run_dir>
  ```

- [Step 4] If `next_state` is `implementation` or `test_implementation` (review failed, retry), append findings to attempt history **after** advance:
  ```bash
  # Write summary + top findings from <iter_dir>/review-result.json to <run_dir>/.attempt-tmp.md, then:
  python3 <driver_path> append-attempt --run <run_dir> --state review --outcome-file <run_dir>/.attempt-tmp.md
  ```

- [Step 5] Follow `states/<next_state>.md` (`done` on pass; `implementation`/`test_implementation` on a retry; `failed` if exhausted).

**Checklist:**
- [ ] Change diff written to the echoed `changes_diff` path (manifest-scoped when present)
- [ ] `run-stage --role reviewer` returned `ok: true` (valid `review-result.json` written); else stopped/reported
- [ ] `driver advance` called before any `append-attempt`; followed the reported `next_state`
