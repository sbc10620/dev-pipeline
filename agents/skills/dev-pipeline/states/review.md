# STATE: review

**Goal:** Prepare the change diff, run the reviewer runner, advance (the driver applies the gate).

The advance that landed here echoed `directive: run_reviewer`, `iter_dir`, `contract_path`, and `changes_diff` (the path the reviewer will read). The driver persisted the reviewer context to `<iter_dir>/stage-input.json`, with `output_file` set to `<iter_dir>/review-result.json`.

- [Step 1] **Write the change diff** to the echoed `changes_diff` path so the reviewer can read it. Scope it to the pipeline's manifest when present (so unrelated/untracked files are not reviewed). **Run from `project_root`.** Check for an initial commit: `git -C <project_root> rev-parse --verify HEAD 2>/dev/null`.
  - **Manifest present** (`<run_dir>/changed-manifest.txt`): mark new files intent-to-add **one path at a time** so they show as `new file` hunks (a plain `diff HEAD` **omits untracked files** — the reviewer has no Bash and sees only this diff), write the diff, then undo the marking so the working tree is unchanged for later states. Mark per path (a single `add -N` with several paths aborts wholesale if any manifest entry is stale — created then deleted — silently omitting **all** new files); `diff`/`reset` tolerate stale pathspecs, so keep those scoped to the whole set:
    ```bash
    while IFS= read -r p; do [ -n "$p" ] || continue
      git -C <project_root> add -N -- "$p" 2>/dev/null || true
    done < <run_dir>/changed-manifest.txt
    git -C <project_root> diff HEAD -- <manifest paths> > <changes_diff>
    git -C <project_root> reset -q -- <manifest paths> 2>/dev/null || true
    ```
  - **No manifest / no HEAD (fallback):** surface untracked files first with `git -C <project_root> add -N . 2>/dev/null || true`, then diff **against the worktree, not the index** (intent-to-add files are invisible to `diff --cached`): `git -C <project_root> diff HEAD > <changes_diff>` (or, on a repo with no HEAD, `git -C <project_root> diff > <changes_diff>`), then `git -C <project_root> reset -q`.

- [Step 2] **Run the reviewer:**
  ```bash
  python3 <driver_path> run-stage --run <run_dir> --role reviewer --stage-input <iter_dir>/stage-input.json
  ```
  The runner reviews the diff + contract (read-only on the code) and writes a schema-valid `review-result.json` to `<iter_dir>`. Its runner list and tool envelope live in `config.runners.reviewer`. For a bash runner, prefer running this in the background and polling `<iter_dir>/reviewer-runner.log` per [SKILL §Role Execution](../SKILL.md#-role-execution) if your host supports it — note some reviewer CLIs (e.g. a stdout-redirect claude runner) write little to this log until they finish; that's expected, not a hang. Read the JSON:
  - **`mode` is `main-session`/`subagent`** → execute the reviewer per [SKILL §Role Execution](../SKILL.md#-role-execution) (json role: the executor reads the diff + contract and writes `review-result.json` to `output_file`; then `driver finalize-stage` validates it), then proceed. **Note:** a subagent/main-session reviewer is not tool-sandboxed — if a strict read-only boundary matters here, use a `bash` reviewer instead (see the §Role Execution security note).
  - `ok: true` → a valid review result was written; proceed.
  - `ok: false` → every reviewer runner failed; stop and report the `attempts`.

- [Step 3] Call driver advance. The driver applies the configured gate and, on failure, routes by where the blocking findings point (test files → `test_implementation`; production → `implementation`):
  ```bash
  python3 <driver_path> advance --run <run_dir>
  ```
  On a review-failure retry the driver **records the blocking findings to `attempts.md` automatically** (verdict + summary + findings) — you do not log them yourself.

- [Step 4] Follow `states/<next_state>.md` (`done` on pass; `implementation`/`test_implementation` on a retry; `failed` if exhausted).

**Checklist:**
- [ ] Change diff written to the echoed `changes_diff` path (manifest-scoped when present; new files surfaced via `add -N`, working tree left unchanged)
- [ ] `run-stage --role reviewer` returned `ok: true` (valid `review-result.json` written), **or** a `mode` handoff was executed and `finalize-stage` returned `ok: true`; else stopped/reported
- [ ] (bash runner, host permitting) ran in the background with the runner log polled for progress
- [ ] `driver advance` called; followed the reported `next_state` (the driver auto-recorded any retry findings)
