# STATE: resume  (only when invoked with `--resume`)

**Goal:** Continue an **interrupted** run from the state it stopped in — without re-running `init` (which would start a NEW run) and without redoing completed stages. The driver re-emits the current state's **landing echo** (persisted to `<run_dir>/last-advance.json`); this file adds the git bookkeeping a mid-state re-entry needs so a pre-crash edit is never silently lost.

- [Step 1] **Locate the run.** If the user passed `--resume <run_dir>`, use that path. Otherwise resolve `project_root` (Step 0) and use `<project_root>/.dev-pipeline/latest`. Verify the path exists; if not, stop: "No run to resume — `<path>` not found. Start a run with `--plan`/`--request`, or pass `--resume <run_dir>`."

- [Step 2] **Ask the driver where to resume** (optionally handing it the prior session's task summary):
  ```bash
  python3 <driver_path> resume --run <run_dir> [--summary "<text>" | --summary-file <path>]
  ```
  Pass `--summary`/`--summary-file` only if the user carried over a task summary from the interrupted session (what was decided / done / planned next); it is optional and never required.
  - Non-zero exit → the driver prints the reason and (for a run with no `last-advance.json`) an exact manual recipe. Relay it and stop.
  - On success, parse the JSON and **restore the Run Context** from it: `project_root` (from `project_dir`), `plan_path`, `contract_path`, `tdd_mode`, `run_dir`, and **`work_root`, `worktree_branch`, `worktree_base_ref`**. These replace the values `init` normally seeds — in particular, **use the restored `work_root` for every git command below and in the resumed state file, never `project_root`**, exactly as a fresh run would (a run predating this feature has no `work_root` in `state.json`; `driver resume` falls back to `project_dir` for it, so this restore always yields a usable value).
  - **`task_summary`** (present only when you passed `--summary`/`--summary-file`) is the prior session's **handoff context** — read it to re-orient yourself as the orchestrator (what was in progress, decisions taken, next steps), distinct from `contract.md` (the spec) and `attempts.md` (the failure log). It is orchestrator-only context: the driver does **not** forward it to any role's prompt, and a bare `resume` (no flag) never carries it.
  - `possibly_live` is a **best-effort** flag (the run's `state.json` was written recently). It can miss a genuinely live session and can fire on a benign quick retry, so treat it as a nudge, not a guarantee: **always** make sure no other session is still driving this run before continuing — a second driver on the same run corrupts its state. If `possibly_live` is set and you can't confirm the run is idle, stop.

- [Step 3] **Dispatch on the driver's output.** It reports a `next_state` (the state to resume in) and sometimes a `directive`:
  - **`directive: "advance"`** (the run is parked at `init`, or the last transition was interrupted before it persisted — see `resume_note`) → call `python3 <driver_path> advance --run <run_dir>` **once**, then follow `states/<the next_state that advance returns>.md`. Do **not** open `states/init.md` (it would start a new run) — the `next_state: "init"` here only means "call advance to enter the first working state".
  - **`next_state` is `done` or `failed`** (terminal) → the *transition* finished but finalization may not have (a crash between the landing advance and the commit/report). **Always follow `states/<next_state>.md`** with the echoed context (`done` → `done.md`; its commit is idempotent — it commits only if something is staged, so a run that already finalized just no-ops, though its retrospective/self-evolution may repeat, note that to the user; `failed` → `failed.md`, using the echoed `halt_reason`/`failure_details`). Do not try to pre-decide "already finalized" from a diff — `done.md`'s own idempotent staging is the authority.
  - **`next_state` is an authoring state (`implementation` or `test_implementation`)** → **first recover the interrupted delta (Step 4)**, then follow `states/<next_state>.md` from the top.
  - **`next_state` is a JSON-role state (`test`, `red_test`, `review`)** → no delta recovery needed (re-running just overwrites the result file and advance re-validates it) → follow `states/<next_state>.md` from the top.

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

**Resuming does not shrink the loop.** Whichever `states/<next_state>.md` Step 3 sends you into, the normal advance loop continues from there exactly as a non-resumed run would (SKILL.md's main loop: keep calling `driver advance` and opening the next state file until `next_state` is `done` or `failed`, per Global Rule 11). Reaching that first state file is not itself the task — finish the run.

**Checklist:**
- [ ] Run located (`--resume <run_dir>` or `latest`); stopped cleanly if absent
- [ ] `driver resume` ran (with `--summary`/`--summary-file` if a prior-session summary was carried over); Run Context (`project_root`, `plan_path`, `contract_path`, `tdd_mode`, `work_root`, `worktree_branch`, `worktree_base_ref`) restored, and any echoed `task_summary` read as handoff context; confirmed the run is idle (esp. if `possibly_live`)
- [ ] Dispatched correctly: `advance` directive re-advanced (never opened `init.md`); terminal always followed its state file; authoring vs JSON-role routed right
- [ ] (authoring re-entry) recovered the working-tree-vs-index delta (against `work_root`) minus the manifest, boundary-checked the remainder (TDD, no auto-revert), and `record-changes`d it **before** entering the state file
- [ ] Continued the normal loop from the recovered state — kept calling `driver advance` through every subsequent state, whichever state that turned out to be, until `next_state` is `done` or `failed` (reaching the first resumed-into state file is not itself the task)
