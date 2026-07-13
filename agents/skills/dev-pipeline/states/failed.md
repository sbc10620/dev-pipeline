# STATE: failed

Read `halt_reason` (and any echoed context) from the `driver advance` output.

**Worktree run (echoed `worktree_branch` is set):** do **not** merge or clean up. Unlike `done.md`, a failed run's worktree + branch are **preserved as-is** for debugging — the failure is likely visible in the worktree's own state (partial edits, a failing test run, etc.), and auto-discarding it would destroy that evidence. Tell the user where it lives (the echoed `work_root`) and how to remove it once they're done: `python3 <driver_path> cleanup-worktree --run <run_dir>` (safe — it only force-removes the worktree checkout and safe-deletes the branch if it's already merged elsewhere; it never merges anything itself).

**`halt_reason: "environment"`**
Stop immediately. Report:
- Which stage failed (build/install/test) and, if the advance echoed `phase: "red_test"`, that it happened during **RED verification** (so it is a toolchain/framework setup problem, not a code defect).
- The `failure_details` and `log_excerpt`.
- Ask the user:
  > "This failure appears to be an environment or configuration issue, not a code defect. Please check:
  > - Are all dependencies (including the test framework) installed?
  > - Is the toolchain (compiler, runtime, etc.) available?
  > - Are `build_instruction` / `install_instruction` / `test_instruction` in `.dev-pipeline/dev-pipeline.config.json` correct?
  > After fixing, restart the pipeline."

  Do **not** edit `.dev-pipeline/dev-pipeline.config.json` yourself to "fix" the instructions and retry — surface the proposed change and let the user apply it (Global Rule 10).

**`halt_reason: "iteration-exhausted"`**
Report:
- Which budget was exhausted. The `outcome` in `state.json` history distinguishes them: `test_fail_exhausted` (green test), `review_fail_exhausted` (review), or `red_not_confirmed_exhausted` (the authored tests kept passing with no implementation — the tests are likely vacuous; point the user at the test author output).
- The last failure details / review findings, and a summary of all attempts from `attempts.md`.
- If the advance echoed a `hint` (TDD review exhaustion), surface it: the blocking findings may point at tests that contradict the contract, not at a production defect — inspect the test findings before assuming the code is wrong.

There is no further automatic action from a `failed` state. Stop after reporting.
