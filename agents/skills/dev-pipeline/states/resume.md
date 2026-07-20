# STATE: resume  (only when invoked with `--resume`)

**Goal:** Continue an **interrupted** run from the state it stopped in — without re-running `init` (which would start a NEW run) and without redoing completed stages. The driver re-emits the current state's **landing echo** (persisted to `<run_dir>/last-advance.json`); this file adds the git bookkeeping a mid-state re-entry needs so a pre-crash edit is never silently lost.

- [Step 1] **Locate the run.** If the user passed `--resume <run_dir>`, use that path. Otherwise resolve `project_root` (Step 0) and use `<project_root>/.dev-pipeline/latest`. Verify the path exists; if not, stop: "No run to resume — `<path>` not found. Start a run with `--plan`/`--request`, or pass `--resume <run_dir>`."

- [Step 2] **Ask the driver where to resume:**
  ```bash
  python3 <driver_path> resume --run <run_dir>
  ```
  - Non-zero exit → the driver prints the reason and (for a run with no `last-advance.json`) an exact manual recipe. Relay it and stop.
  - On success, parse the JSON and **restore the Run Context** from it: `project_root` (from `project_dir`), `plan_path`, `contract_path`, `tdd_mode`, `run_dir`, and **`work_root`, `worktree_branch`, `worktree_base_ref`**. These replace the values `init` normally seeds — in particular, **use the restored `work_root` for every git command below and in the resumed state file, never `project_root`**, exactly as a fresh run would (a run predating this feature has no `work_root` in `state.json`; `driver resume` falls back to `project_dir` for it, so this restore always yields a usable value).
  - `possibly_live` is a **best-effort** flag (the run's `state.json` was written recently). It can miss a genuinely live session and can fire on a benign quick retry, so treat it as a nudge, not a guarantee: **always** make sure no other session is still driving this run before continuing — a second driver on the same run corrupts its state. If `possibly_live` is set and you can't confirm the run is idle, stop.

- [Step 3] **Dispatch on the driver's output.** It reports a `next_state` (the state to resume in) and sometimes a `directive`:
  - **`directive: "advance"`** (the run is parked at `init`, or the last transition was interrupted before it persisted — see `resume_note`) → call `python3 <driver_path> advance --run <run_dir>` **once**, then follow `states/<the next_state that advance returns>.md`. Do **not** open `states/init.md` (it would start a new run) — the `next_state: "init"` here only means "call advance to enter the first working state".
  - **`next_state` is `done` or `failed`** (terminal) → the *transition* finished but finalization may not have (a crash between the landing advance and the commit/report). **Always follow `states/<next_state>.md`** with the echoed context (`done` → `done.md`; its commit is idempotent — it commits only if something is staged, so a run that already finalized just no-ops, though its retrospective/self-evolution may repeat, note that to the user; `failed` → `failed.md`, using the echoed `halt_reason`/`failure_details`). Do not try to pre-decide "already finalized" from a diff — `done.md`'s own idempotent staging is the authority.
  - **`next_state` is an authoring state (`implementation` or `test_implementation`)** → **first recover the interrupted delta (Step 4)**, then follow `states/<next_state>.md` from the top, **then continue the advance loop through every subsequent state until `next_state` is `done`/`failed` — resume re-enters the full pipeline, it is not a one-state task.**
  - **`next_state` is a JSON-role state (`test`, `red_test`, `review`)** → no delta recovery needed (re-running just overwrites the result file and advance re-validates it) → follow `states/<next_state>.md` from the top, **then continue the advance loop through every subsequent state until `next_state` is `done`/`failed` — resume re-enters the full pipeline, it is not a one-state task.**

- [Step 4] **Authoring re-entry — recover the interrupted stage's delta before re-baselining (mandatory).** `states/implementation.md`/`test_implementation.md` Step 1 runs `git add -A`, which would fold the interrupted author's not-yet-recorded edits into the new baseline and hide them from the manifest, the review diff, and the commit. Recover them the **same way the state files compute their delta** — working tree vs the git **index** (whose `git add -A` baseline from the crashed stage survives on disk, and already absorbed earlier stages' files and any pre-run dirty edits, so those are correctly excluded). **Run against the restored `work_root`, not `project_root`** (see Step 2):
  ```bash
  { git -C <work_root> -c core.quotePath=false diff --name-only --relative; \
    git -C <work_root> -c core.quotePath=false ls-files --others --exclude-standard; } | sort -u
  ```
  - **Subtract what's already recorded.** Drop any path already in `<run_dir>/changed-manifest.txt` — those were recorded and boundary-checked when their stage produced them. The **remainder** is the interrupted stage's unrecorded output.
  - **Boundary check the remainder — only when `tdd_mode` is true** — with the current state's role (`implementation` re-entry → `--role implementation`; `test_implementation` → `--role test_implementation`):
    ```bash
    python3 <driver_path> check-boundary --run <run_dir> --role <implementation|test_implementation> --changed <remainder paths>
    ```
    On a violation, **do not auto-revert** — after a crash the offending file's authorship is ambiguous (it may predate the run). Show the paths and **ask the user** how to proceed.
  - **Record the remainder** so it survives into the manifest regardless of what the re-run author does:
    ```bash
    python3 <driver_path> record-changes --run <run_dir> --changed <remainder paths>
    ```
  - Tell the user which paths were recovered, then proceed into `states/<next_state>.md` from the top.

**Note (accepted limits):** attempts are recorded by `advance` itself (before it persists the transition), so an interrupted advance never loses the retry context — at worst a crash mid-advance duplicates one attempt log line when the advance re-runs (harmless; the context is re-derived from the on-disk result file). A file role interrupted mid-edit may leave a partial file in the working tree; Step 4 surfaces and records it but does **not** auto-roll-back — inspect `git status` if a stage looks half-done.

**Checklist:**
- [ ] Run located (`--resume <run_dir>` or `latest`); stopped cleanly if absent
- [ ] `driver resume` ran; Run Context (`project_root`, `plan_path`, `contract_path`, `tdd_mode`, `work_root`, `worktree_branch`, `worktree_base_ref`) restored; confirmed the run is idle (esp. if `possibly_live`)
- [ ] Dispatched correctly: `advance` directive re-advanced (never opened `init.md`); terminal always followed its state file; authoring vs JSON-role routed right
- [ ] (authoring re-entry) recovered the working-tree-vs-index delta (against `work_root`) minus the manifest, boundary-checked the remainder (TDD, no auto-revert), and `record-changes`d it **before** entering the state file
- [ ] Continued the normal loop from the recovered state **to a terminal state (`done`/`failed`)** — not just the single recovered state
