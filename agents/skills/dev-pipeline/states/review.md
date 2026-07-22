# STATE: review

**Goal:** Prepare the change diff, run the reviewer runner, advance (the driver applies the gate).

The advance that landed here echoed `directive: run_reviewer`, `iter_dir`, `contract_path`, `changes_diff` (the path the reviewer will read), `reviewer_config`, and **`work_root`**. The driver persisted the reviewer context to `<iter_dir>/stage-input.json`, with `output_file` set to `<iter_dir>/review-result.json`.

- [Step 1] **Write the change diff** to the echoed `changes_diff` path so the reviewer can read it. Scope it to the pipeline's manifest when present (so unrelated/untracked files are not reviewed). **Run from `work_root`, not `project_root`** — identical under a normal run, but `work_root` is the isolated worktree checkout under `--worktree` (see `states/init.md`); the diff must reflect the worktree's own history, not the main checkout's. Check for an initial commit: `git -C <work_root> rev-parse --verify HEAD 2>/dev/null`.
  - **Manifest present** (`<run_dir>/changed-manifest.txt`): mark new files intent-to-add **one path at a time** so they show as `new file` hunks (a plain `diff HEAD` **omits untracked files** — the reviewer has no Bash and sees only this diff), write the diff, then undo the marking so the working tree is unchanged for later states. Mark per path (a single `add -N` with several paths aborts wholesale if any manifest entry is stale — created then deleted — silently omitting **all** new files); `diff`/`reset` tolerate stale pathspecs, so keep those scoped to the whole set:
    ```bash
    while IFS= read -r p; do [ -n "$p" ] || continue
      git -C <work_root> add -N -- "$p" 2>/dev/null || true
    done < <run_dir>/changed-manifest.txt
    git -C <work_root> diff HEAD -- <manifest paths> > <changes_diff>
    git -C <work_root> reset -q -- <manifest paths> 2>/dev/null || true
    ```
  - **No manifest / no HEAD (fallback):** surface untracked files first with `git -C <work_root> add -N . 2>/dev/null || true`, then diff **against the worktree, not the index** (intent-to-add files are invisible to `diff --cached`): `git -C <work_root> diff HEAD > <changes_diff>` (or, on a repo with no HEAD, `git -C <work_root> diff > <changes_diff>`), then `git -C <work_root> reset -q`.

- [Step 2] **Run the reviewer:**
  ```bash
  python3 <driver_path> run-stage --run <run_dir> --role reviewer --stage-input <iter_dir>/stage-input.json
  ```
  The runner reviews the diff + contract (read-only on the code) and writes a schema-valid `review-result.json` to `<iter_dir>`. Its runner list and tool envelope live in `config.runners.reviewer`. For a bash runner, prefer running this in the background and checking `<iter_dir>/reviewer-runner.log` per [SKILL §Role Execution](../SKILL.md#-role-execution) if your host supports it (a quiet log there doesn't mean it's stuck — see that section for the check/relay cadence; some reviewer commands write little to this log until they finish, see `RUNNERS.md`'s per-CLI log notes). Read the JSON:
  - **`mode` is `main-session`** — **before executing, always ask the user, even under `--auto-run`** (this is a runtime safety confirmation, not the approval gate `--auto-run` skips; this review would run in the same session as whatever came before it in this run — not guaranteed independent): "The reviewer is configured as `main-session` — it will review in this same session, not a fresh/independent one. Continue the review here, or open a **new session** first?" Give two options:
    - **Continue here** — proceed to execute the reviewer per [SKILL §Role Execution](../SKILL.md#-role-execution) (json role: the executor reads the diff + contract and writes `review-result.json` to `output_file`; then `driver finalize-stage` validates it), compacting first per the existing best-effort guidance, then **continue to [Step 3] below and call driver advance — do not stop here.** This holds even if the review **approves**: an approving verdict is not itself the end of the run (Global Rule 11) — only `driver advance` decides the next state, and it still routes to `states/done.md`, which has real finalization work left.
    - **Open a new session** — **stop here, do not execute the review, do not call `driver advance`.** Tell the user to open a new session and run:
      ```
      /dev-pipeline --resume <run_dir>
      ```
      The run stays parked at `review` — nothing here changes `state.json`, so `--resume` replays this exact landing echo, and that (fresh) session will be asked the same question again — answering "continue here" there is fine, since it genuinely is a new, independent session at that point.
  - **`mode` is `subagent`** → execute the reviewer per [SKILL §Role Execution](../SKILL.md#-role-execution) (json role: the executor reads the diff + contract and writes `review-result.json` to `output_file`; then `driver finalize-stage` validates it), then continue to [Step 3] below and call driver advance — do not stop here, even if it approves (Global Rule 11). **Note:** a subagent/main-session reviewer is not tool-sandboxed — if a strict read-only boundary matters here, use a `bash` reviewer instead (see the §Role Execution security note).
  - `ok: true` → a valid review result was written; continue to [Step 3] below and call driver advance — do not stop here, even if the review approves (Global Rule 11).
  - `ok: false` → every runner failed to produce a valid result; stop and report the `attempts`.

- [Step 3] Call driver advance. The driver applies the configured gate and, on failure, routes by where the blocking findings point (test files → `test_implementation`; production → `implementation`):
  ```bash
  python3 <driver_path> advance --run <run_dir>
  ```
  On a review-failure retry the driver **records the blocking findings to `attempts.md` automatically** (verdict + summary + findings) — you do not log them yourself.

- [Step 4] Follow `states/<next_state>.md` **as returned by advance** — `states/done.md` if `next_state` is `done` (a passing review is not itself completion; `done.md` still commits, merges/cleans up a worktree, and may run self-evolution), `implementation`/`test_implementation` on a retry, `failed` if exhausted.

**Checklist:**
- [ ] Change diff written to the echoed `changes_diff` path (manifest-scoped when present; new files surfaced via `add -N`, working tree left unchanged)
- [ ] If the reviewer was `main-session`, asked the user whether to continue here or open a new session — proceeded only per their answer
- [ ] `run-stage --role reviewer` returned `ok: true` (valid `review-result.json` written), **or** a `mode` handoff was executed and `finalize-stage` returned `ok: true`; else stopped/reported
- [ ] (bash runner, host permitting) ran in the background with the runner log checked periodically (a quiet log is expected for some runners, not a hang); relayed to the user only when there was something new to say
- [ ] `driver advance` called — **even after an approving review, never skipped**; followed the reported `next_state` (the driver auto-recorded any retry findings)
