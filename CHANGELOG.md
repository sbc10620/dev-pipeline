# Changelog

All notable changes to dev-pipeline are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The version is defined in one place — `__version__` in
`agents/dev-pipeline-tools/driver.py`. Check an installed copy with
`python3 .agents/skills/dev-pipeline/driver.py --version`.

## [7.1.2] - 2026-07-22

**Fixed a reliability gap: under the default (no `--auto-run`) `--request` flow, the orchestrator could
proceed straight from writing `plan.md` into running the pipeline — the `--plan` downstream path — without
the user ever actually approving it in the conversation.** `--request` and `--plan` are architecturally
designed to converge on one downstream path (config gate → `states/init.md`) once `plan_path` is set, so
this isn't a literal mode-switch bug — it's the plan-approval step lacking a genuine stop. `states/planning.md`
Step 3 only said "show the user the finished plan... for sign-off," with no hard-stop verb and no instruction
to wait for a reply; its checklist item audited that the plan was *shown*, not that a *reply arrived*. Since
7.1.1 added Global Rule 11 ("always advance, don't stop early") to fix a different bug, that pressure could
now make an orchestrator *more* likely to blow through this pause, absent an explicit carve-out.

As with the 7.1.1 fix, there is no code-level barrier available here: `driver init` has no
approval/consent concept, and a human-approved plan is a byte-identical input to one nobody looked at — any
orchestrator-supplied "approved" flag would be self-certified by the same actor skipping the gate. The fix is
prose-only, mirroring how the reviewer's main-session gate was hardened previously.

### Changed
- **`states/planning.md` Step 3** now reads as a genuine blocking stop: show the plan, then **STOP and wait
  for the user's reply** — do not continue to Step 4/the config gate/`init`/any downstream state until they
  have actually responded. Displaying the plan, or the orchestrator's own judgement that it looks ready, is
  explicitly **not** approval. States that **Global Rule 11 does not override this pause**. `--auto-run`'s
  documented skip of this prompt is unchanged.
- **`states/planning.md`'s checklist** now audits that the flow **blocked for an explicit reply**, not merely
  that the plan was shown.
- **`SKILL.md` Global Rule 11** gains a reconciling sentence: it governs not mistaking a role's finished
  output for a finished state, and is not license to skip a genuine human-approval gate — naming both such
  gates (`states/planning.md`'s sign-off, `states/review.md`'s reviewer question) so the carve-out holds
  regardless of which file is being read.

No schema/CLI/driver-logic changed and no test-visible behavior changed — prose-only reliability fix, PATCH
bump.

## [7.1.1] - 2026-07-21

**Fixed a reliability gap: a main-session/subagent role's own output could be mistaken for run
completion, most visibly an approving `main-session` reviewer causing the orchestrator to declare
"done" without ever calling `driver advance` or running `states/done.md`'s finalization (commit,
worktree merge/cleanup, self-evolution).** The state machine and its echoes were always correct —
the review-pass branch of `cmd_advance` returns `next_state:"done"` plus everything `done.md`
needs, and `done.md` is self-sufficient from that echo. The gap was entirely in the **prose** that
drives the LLM orchestrator, and it generalized beyond review→done to any main-session/subagent
role finishing in any state: the driver-injected persona-switch preamble said "STOP and hand back"
without saying what handing back concretely requires; several state files' mode-dispatch bullets
said the vague "then proceed" with no step named; and nothing locally countered the "an approving
review feels like completion" framing at the one point most likely to trigger it.

### Changed
- **`driver.py`'s persona-switch preamble** (prepended to every main-session/subagent role's system
  prompt, for every role) now explicitly says finishing the role's own work is not a stopping
  point — even when the output looks like the task is finished (an approving review) — and
  restates concretely what "hand back" requires: complete the dispatching state file's remaining
  steps through `driver advance`, then open whatever `states/<next_state>.md` it returns. The
  "acting SOLELY as …" persona text is unchanged. The implementor sub-example that used to say
  "...then stop" was reworded to "...without also running the project's test suite or reviewing
  the diff" — reusing the trigger word right next to the new "not a stopping point" paragraph
  undermined it.
- **`SKILL.md` gains Global Rule 11**: finishing a role's own output is not the end of the state,
  for any role in any state; cross-referenced from the existing §Role Execution "you are the
  orchestrator again" paragraph.
- **`states/test.md`, `states/red_test.md`, `states/review.md`**: **every** mode-dispatch bullet —
  main-session, subagent, and the plain bash-runner `ok: true` path alike — now names the exact
  next Step to continue to and says "do not stop here" (matching the pattern
  `states/implementation.md`/`states/test_implementation.md` already used), instead of the vague
  "then proceed."; `states/review.md`'s three bullets also each state that an approving verdict
  does not end the run (Global Rule 11), so none of the three review paths is under-reinforced
  relative to the others.
- **`states/review.md`**: Step 4 and its checklist item now say a passing review is not itself
  completion — only `driver advance` decides the next state, and it still routes to
  `states/done.md`, which has real work left.
- **`states/test.md`**: Step 3 notes a passing test run is not the end — `review` is still ahead.
- **`states/done.md`**: a new line up front states that completing Step 1 (the commit) is not the
  end of this state — the literal form of the reported bug is stopping after the commit and
  skipping the worktree merge, retrospective, self-evolution, and next-step recommendations.
- **`states/resume.md`**: restates that resuming re-enters the same loop a non-resumed run follows
  — the normal advance loop continues through whichever state was resumed into until `next_state`
  is `done` or `failed`; reaching the first resumed-into state file is not itself the task.
- **`dp-implementor.md`/`dp-test-implementor.md`**: the closing "...this file is written, **stop**."
  line — the last words the executor reads, since the persona preamble is prepended to the role
  body — reworded to "...you are done authoring..." for the same reason as the persona example
  above. (`dp-reviewer.md`/`dp-tester.md` already ended cleanly, on schema-formatting instructions.)

### Added
- A regression-guard test (`test_driver.py`, `test_run_stage_main_session_handoff`) pins the new
  preamble text ("you are NOT done for this turn") in the assembled system prompt, mirroring the
  existing assertion on the original wording, so this fix can't silently regress.

No schema/CLI/driver-logic changed and no test-visible behavior changed beyond the preamble string
(the existing substring assertion on it still passes) — prose-only reliability fix, PATCH bump.

## [7.1.0] - 2026-07-21

**`driver resume` optionally carries a prior-session task summary to the resuming
orchestrator.** When the host session fills its context and hands off to a fresh
session, that new orchestrator starts cold — it has the precise state echo but no
narrative of what the prior session was doing/decided/planned. `resume` now takes
an optional summary so the fresh session can re-orient.

### Added
- **`resume --summary <text>` / `--summary-file <path>`** (mutually exclusive). The
  text is surfaced in the resume output as `task_summary`, which `states/resume.md`
  reads as prior-session handoff context (distinct from `contract.md` and
  `attempts.md`). It rides in the resume `ctx` — merged *after* `build_stage_input`
  runs on the pristine echo — so it reaches the orchestrator but **never** a role's
  `stage-input.json` (a role stays fed only by the contract/attempts). Explicit-only
  and **not persisted**: a bare `resume` carries no summary and is byte-for-byte
  unchanged, so a stale summary is never auto-reused (pass it again to reuse).

## [7.0.0] - 2026-07-21

**Removed `red_expected`** (the 6.7.0 in-flow "these tests target pre-existing
behavior, skip RED confirmation" escape hatch) and moved the TDD-vs-non-TDD
decision to **plan time**. `red_expected` encoded a question — *"is this
genuinely-new-feature work whose test truly fails first, or existing-behavior
work?"* — that belongs to planning, not a mid-flow test-author declaration. It
also carried real weight (a schema field, a driver branch, `red_confirmation_skipped`
state + a reviewer echo, and the long Rule 13) and entangled with the 6.8.0
routing work. With the decision made up front, it is unnecessary.

New principle: **TDD** is only for genuinely new behavior where a freshly-written
test *actually fails first* (real RED); **non-TDD** covers regression tests,
maintenance, bug fixes, refactors, and coverage for already-existing behavior.

### Removed (BREAKING)
- **`red_expected`** from `implementor-result.schema.json` (the schema is
  `additionalProperties:false`, so a result JSON still carrying the field is now
  rejected — affects only an in-flight run created by ≤6.8.0 resuming under 7.0.0).
- The driver's red-phase skip branch: a `test_implementation` red-phase pass now
  **always** → `red_test` (the mandatory test-author status file is still read for
  the die-on-missing guard, but routing no longer depends on its content). The
  `tests_added_no_red_expected` transition, the `red_confirmation_skipped` /
  `red_confirmation_skip_summary` state, and the reviewer's
  `red_confirmation_skipped_note` echo (`dest_echoes`) are gone.
- `dp-test-implementor.md` Rule 13 and its cross-references (Rule 3 exception,
  Rule 8, Step 3.5, Step 5 JSON/prose, checklist); `dp-reviewer.md`'s
  `red_confirmation_skipped_note` severity rule; the `red_expected`/`red_confirmation`
  clauses in `states/test_implementation.md`, `states/test.md`, `states/review.md`,
  and the `AGENTS.md` transition rules. `red_test`'s `red_not_confirmed` now
  unambiguously means "vacuous tests" (existing-behavior work never reaches it).

### Changed
- **The planner classifies TDD vs non-TDD** (`dp-planner.md`) and recommends
  `driver.tdd_mode` through the existing planning → `--update-config` flow —
  non-TDD for regression/maintenance/existing-behavior work, TDD only when a
  written test genuinely fails until new code exists. The `plan.md` body stays
  config-free.
- **The non-TDD implementor owns tests too** (`dp-implementor.md`): Rule 9's
  test-file boundary is explicitly TDD-only (it already keys on `test_paths` being
  provided), and the implementor now carries concise test-writing guidance for
  non-TDD runs (meaningful asserting tests, edge/error cases, existing
  conventions) — since a non-TDD run has no test-author role, this is how
  regression/coverage tests for existing behavior get written.

## [6.8.0] - 2026-07-20

On a **repair pass** (a `test_implementation` re-entry driven by a reviewer
finding about a test file, or the implementor's `blocked_on:"tests"` reroute),
`cmd_advance` transitioned to `test` **unconditionally** — it never read the
(mandatory since 6.6.0) `test_implementor-result.json`. So when the test author
verified its tests correct and reported `status:"blocked"`, the run re-ran the
tester against unchanged tests (which passed), returned to `review`, re-raised
the same finding, and spun `test → review → test_implementation` until the
`review` budget was exhausted — **the implementor was never reached**. This is
the missing mirror of the implementor's `blocked_on:"tests"` reroute
(`implementation → test_implementation`): there was no reverse route, and no
prose telling the test author how to signal "the production code is the gap".

### Added
- **`blocked_on` gains an `"implementation"` value** (`implementor-result.schema.json`,
  shared enum → `["contract", "tests", "implementation"]`; not schema-enforced
  against `status`/role, matching the existing convention). **test_implementor
  only, and only on a repair pass** — it is inert during `red_phase` (a `blocked`
  authoring pass still falls through to `red_test` regardless of `blocked_on`).
  The test author sets it when it verified the authored tests correct and the
  production code is what must change.
- **`cmd_advance`'s repair-pass `test_implementation` branch now reads
  `test_implementor-result.json`** (die()-on-missing/invalid, keeping the
  pre-6.6.0 hand-fix hint) and routes `status:"blocked"` + `blocked_on:"implementation"`
  → `implementation` (symmetric to the implementor's `blocked_on:"tests"`
  reroute; takes **no** counter bump, mirroring `red_confirmed → implementation`,
  so the standoff stays bounded by the `implementation → test_implementation`
  edge). Every other result (`implemented`, or `blocked` with
  `contract`/omitted/`tests`) → `test`, exactly as before.

### Changed
- **`dp-test-implementor.md` Rule 12** widened to cover BOTH repair-pass
  re-entry causes (implementor concern **and** reviewer finding), with distinct
  guidance: fix a genuinely-wrong or vacuous test (`implemented`); report
  `blocked_on:"implementation"` (quoting the disputed finding) only when the
  tests are verified correct and the production code is the gap; report a Rule 11
  contract block (`blocked_on` `contract`/omitted) when the AC is untestable as
  specified. Without this the reported bug reproduced unchanged — the driver
  route existed but the author never emitted the value that keys it.
- **`dp-implementor.md`**: corrected the `blocked_on` doc (the shared schema now
  also defines `"implementation"`, which is test-author-only — the implementor
  must never set it), and added a rule: on a re-entry note saying the tests were
  verified correct, re-verify against the contract rather than re-asserting
  `blocked_on:"tests"` with no new evidence (avoids a zero-information standoff).
- **Standoff exhaust messaging** (`implementor_blocked_on_tests_exhausted` and
  the TDD `review_fail_exhausted` hint) generalized to name an
  author↔implementor standoff — the ping-pong can now be initiated from either
  side, so the failure no longer reads as solely the implementor's fault.
- **State prose** (`states/test_implementation.md`, `states/implementation.md`,
  `states/failed.md`) and the **Key transition rules** (`AGENTS.md`) updated to
  describe the new route and the generalized standoff exhaustion; the driver's
  `advance` still decides the destination from `blocked_on` (the SKILL never
  routes it). `states/failed.md` now surfaces the echoed `hint` verbatim rather
  than paraphrasing the review-exhaust one, and names both standoff initiators.

## [6.7.0] - 2026-07-17

A legitimate TDD authoring pass — adding regression/coverage tests for
behavior that **already exists**, with no new implementation-requiring AC in
the same pass — used to be indistinguishable from a vacuous test suite: the
tests pass immediately with no implementation present, and `red_test` always
routed that to a re-author loop (`red_not_confirmed`), burning
`iterations.test_implementation` budget on a pass that was correct as
written and could eventually exhaust the run. (`cmd_advance`'s own comment
already flagged this ambiguity: `"RED not confirmed (vacuous tests **or
feature exists**)"`.)

### Added
- **`test_implementor-result.json` gains an optional `red_expected` boolean**
  (`implementor-result.schema.json`, shared with `implementor`; not schema-
  enforced against `status`/`red_phase`, matching the existing
  `concern`/`blocked_on` convention). Meaningful only when `status:
  "implemented"` and the driver's one-time RED gate (`state.red_phase`) is
  still pending. Defaults to `true` (normal RED-confirmation behavior,
  unchanged). Set to `false` only when EVERY test authored in this pass
  targets already-existing behavior — the driver then skips the
  RED-confirmation `red_test` run entirely and lands directly on `test`
  (the GREEN run), flipping `red_phase` to `false` in the same step `red_test`
  itself would have.
- **`cmd_advance`'s `test_implementation` branch now reads this file while
  `red_phase` is pending** (die()-on-missing/invalid, mirroring the 6.6.0
  `implementation`-branch pattern — the file has been mandatory since 6.6.0,
  so this is not a new compatibility gap). `status: "blocked"` still always
  falls through to `red_test` unconditionally, same as before — there is
  still no other role to route a test-author "blocked" to; `red_expected`
  only affects the `status: "implemented"` path.
- **Safety**: skipping RED confirmation never skips GREEN verification. If
  `red_expected: false` turns out to be wrong because the feature genuinely
  isn't implemented yet, the unmodified `test` branch's existing
  test↔implementation retry loop (`iterations.test`, not
  `iterations.test_implementation`) catches it exactly like any other test
  failure. The one case this doesn't protect against — a genuinely vacuous
  test wrongly declared `red_expected: false` — bypasses the only automatic
  vacuous-test detector (`red_test`) and reaches GREEN too; `reviewer` is the
  backstop for that case. `dp-test-implementor.md`'s new Rule 13 documents
  this asymmetry. **The `reviewer` backstop is now actually informed of it**
  (adversarial-review fix, before release): `state.red_confirmation_skipped`
  persists across however many test↔implementation retries happen before
  review is reached, and `dest_echoes("review")` surfaces it to the reviewer
  as `red_confirmation_skipped_note` — without this, the reviewer had no way
  to know which tests bypassed RED confirmation, and `dp-reviewer.md`'s own
  default severity ceiling (test-file nitpicks capped at `medium`, below the
  default blocking threshold) meant a wrongly-skipped vacuous test could
  reach `done` without ever being flagged at a blocking severity.
  `dp-reviewer.md` gained a rule to escalate a vacuous/non-asserting finding
  in that case to at least `high`.
- `dp-test-implementor.md`: Rule 3 gains an exception for tests expected to
  pass immediately under Rule 13; Rule 8 and Step 3.5 gain a carve-out so a
  `red_not_confirmed` re-entry — the field's primary use case — can resolve
  via `red_expected: false` instead of being forced to "always strengthen";
  new Rule 13 spells out the all-or-nothing scope, that it's valid on any
  pass while `red_phase` is pending (not just the first), verification
  requirements (read the actual code, don't trust `attempts.md`'s "vacuous"
  framing at face value), and the safety asymmetry above.

### Changed
- `states/test_implementation.md`, `states/test.md`, `AGENTS.md` (`red_phase`
  description + the `test_implementation`/`red_test` transition rules):
  updated to describe the new third arrival path at `test`.
- `test_driver.py`: `test_test_implementor_result_not_read_by_advance`
  renamed to `test_blocked_test_implementor_result_does_not_change_routing`
  and its comment corrected — the `test_implementation` branch now reads
  this file when `red_phase` is pending; it just doesn't change the routing
  for a `status: "blocked"` result. All other call sites that advance from
  `test_implementation` while `red_phase` is true were retrofitted to write
  a valid `test_implementor-result.json` first (mandatory since 6.6.0, now
  actually enforced by this branch).

## [6.6.2] - 2026-07-15

`--worktree` runs now merge back into `project_root` at `done` via **rebase +
fast-forward** (`git rebase <base>` then `git merge --ff-only`) instead of
`git merge --no-ff`, so the resulting history is linear — no merge commit —
replacing the old strategy outright (not a config option).

### Changed
- **`states/done.md` Step 2** ("Merge and clean up the worktree" → "Rebase and merge the worktree branch, then clean up"): the `project_root` precondition check (branch + clean-tree) keeps its command block and both sub-checks unchanged; only its failure-message's manual-recovery command was updated to the new rebase+ff two-step form. A **new `work_root` readiness check** precedes the rebase — the rebase runs *in* `work_root`, which the old merge-based flow never touched, so it needs its own guard: `work_root`'s `HEAD` must equal the echoed `worktree_branch`, and its tracked tree must be clean (a test-stage side effect outside the manifest could otherwise leave it dirty and unnoticed) — **checked before anything destructive runs**; only once both pass does `git clean -xdf` drop untracked leftovers (e.g. test-stage caches) ahead of the rebase, so a precondition failure never discards evidence worth investigating. The old single **Merge** step is now two: **Rebase** (`git -C <work_root> rebase <worktree_base_ref>` — sees the base branch's live tip with no fetch needed, since a worktree shares the main repo's object database) and **Fast-forward merge** (`git -C <project_root> merge --ff-only <worktree_branch>` — always a pure fast-forward after a clean rebase, never a 3-way merge). Conflict/failure handling is split by failure class instead of one generic "conflict" bucket: a `work_root` precondition failure (STOP, not a rebase conflict — the old `--continue`/`--abort` recovery doesn't apply), a mid-rebase conflict (STOP, `rebase --continue`/`rebase --abort` — abort safely restores the pre-rebase branch), or a fast-forward failure (STOP — either a race, recoverable by re-running the step, or an ignore-not-covered untracked file in `project_root` colliding with a path the rebase adds, which needs the user to move/remove/commit it before re-running). `driver cleanup-worktree` (unchanged) still runs only after a successful fast-forward.
- **`AGENTS.md` / `README.md`**: the worktree-isolation description updated to describe rebase + fast-forward instead of `merge --no-ff`; "leaving the worktree/branch intact" softened to "recoverable" (a rebase conflict leaves the worktree mid-rebase, not literally untouched).

### Fixed (adversarial review, before release)
- **The `work_root` readiness check's `git clean -xdf` ran unconditionally**,
  before the HEAD/tracked-clean checks it sat next to were evaluated — a
  precondition-check failure (HEAD mismatch, or a dirty tracked tree) would
  have already discarded any untracked files worth investigating (e.g. a
  scratch patch the user left mid-resolution) before the state file even told
  them there was a problem, contradicting this same step's own
  "discarding that silently would be worse than leaving it" principle for
  rebase conflicts a few lines below. Reordered so `clean -xdf` only runs
  after both checks pass.
- **The rebase-conflict recovery text conflated two different recovery paths
  into one "then re-run this step."** Read literally, aborting
  (`rebase --abort`) and then re-running would just reproduce the same
  conflict — re-running is only valid after the branch or the base has
  actually changed. Split into two explicit paths with their own next action.
- **`README.md` still said a stopped run "leaves the worktree + branch in
  place"** — inconsistent with `AGENTS.md`'s "recoverable" wording, and
  inaccurate for a mid-conflict rebase (the worktree sits mid-rebase, not
  literally untouched). Aligned to the same "recoverable" language.

### Design note
This is a full replacement, not an option — no new config key or CLI flag was
added (deliberately; adding a `driver.worktree_merge_strategy` toggle was
considered and rejected to keep the config surface unchanged for a single-user
default swap).

### Known regression surface (documented, not fixed — none of it loses work)
- **Rebase can conflict where the old 3-way merge would have auto-resolved.**
  A 3-way merge reconciles final trees in one shot; `git rebase` replays commits
  one at a time, so a history where an early commit touches a line and a later
  commit in the same branch fixes it back can conflict mid-replay even though
  the *net* change is identical to what a 3-way merge would have taken cleanly.
- **`work_root` must now be tracked-clean before `done` can proceed** — a new
  precondition the old merge-based flow didn't have (it only ever read the
  branch ref, never `work_root`'s working tree). A test-stage side effect
  outside the change manifest will now stop the run at `done` instead of being
  silently absorbed into a merge commit.
Both failure modes are safe (stop-and-preserve, nothing lost, no history
corruption) and fall well short of this project's own MAJOR-bump criterion
("an existing install would break"), consistent with the PATCH classification
below.

### Versioning note
PATCH: this project's PATCH/MINOR line is drawn by **change scope** (prose-only
== PATCH — see 6.5.1–6.5.3), not by behavioral semantics. No driver.py logic,
schema, or test change — `states/done.md` (a Markdown file whose git commands
the host LLM orchestrator executes, not driver.py) is the only behavioral
change; `driver.py`'s only edit is this version string. `cmd_cleanup_worktree`
and `cmd_init`'s worktree creation are both merge-strategy-agnostic and
required no change (confirmed: `cmd_cleanup_worktree`'s own docstring already
states merging is the SKILL's job, not the driver's).

## [6.6.1] - 2026-07-14

Prompt-prose consistency refactor of the skill/state/agent Markdown — **no
behavior or state-machine change** (driver.py logic untouched; the only driver.py
edit is this version string). Improves how reliably a host LLM follows the
runner→result-JSON→state flow by making the instructions uniform across roles.

### Changed
- **The 5 runner state files** (`states/test.md`, `red_test.md`, `review.md`, `implementation.md`, `test_implementation.md`) now share one "run the runner → read the result JSON → branch" skeleton (`mode` bullet(s) / `ok: true` / `ok: false`). The two file-role states (`implementation`/`test_implementation`) previously described this inline as prose; they now use the same bullet structure the json-role states already used, with the `finalize-stage` handoff call restated inline and both the `mode` and `ok: true` bullets naming their next destination (`[Step 3]` → `[Step 4]`) so the status-read and empty-delta steps are never skipped. `ok: false` lead phrasing unified to "every runner failed to produce a valid result" across all five. `red_test.md` gains the same "do not run the tester yourself" reassurance note `test.md` already had. State-specific content (result filenames, `red_test`'s "pass/fail is the driver's to interpret", `review.md`'s two-mode-bullet ask/subagent split and security note) is preserved.
- **`dp-tester.md` / `dp-reviewer.md`** step order: the "Checklist before outputting" step now comes **before** the "Output the result" step (previously after — contradicting its own name), matching `dp-implementor.md`/`dp-test-implementor.md`. Content is unchanged, only step positions/numbers. This also makes Global Rule 6/7's "Match the JSON shown in the **final step**" accurate (the JSON block is now the final step). **`dp-reviewer.md` Step 1.3's empty-diff "Skip to Step 4" is corrected to "Skip to [Step 5]"** — after the reorder Step 4 is the checklist, so the old target would have routed the empty-diff path into a review checklist that cannot apply.
- **`SKILL.md` §Role Execution** file-role bullet split from one dense paragraph into three ordered sub-steps (validate status JSON → read status → empty-delta guard) for readability; wording/meaning unchanged. The `## 🎭 Role Execution` heading (an anchor target) was left untouched.

### Fixed (intended correction, not pure prose)
- **`implementation.md` / `test_implementation.md` checklists** now read "…**or** a `mode` handoff was executed **and** `finalize-stage` returned `ok: true`" (previously stopped at "handoff was executed"), matching the json-role states and the actual 6.6.0 behavior where file-role handoffs are validated via `finalize-stage` too.
- **`test_implementation.md` Step 1** referenced "skip the boundary guard in `[Step 3]`" — a stale reference left over from 6.6.0 (which inserted the status-read as Step 3, pushing the boundary guard to Step 4). Corrected to `[Step 4]`.

### Versioning note
PATCH: prose consistency across the skill/state/agent Markdown plus a one-line
`__version__` bump; no driver.py logic, schema, or test change. The full 183-test
suite passes unchanged (it exercises driver.py, which was not touched).

## [6.6.0] - 2026-07-13

`implementor`/`test_implementor`'s status JSON (added optional in 6.5.0) is now
**required and schema-validated exactly like `tester`/`reviewer`'s result** —
a bash runner that edits code but fails to produce a valid status file is no
longer `ok: true`. On top of that mandatory foundation, `cmd_advance` can now
route a `blocked` implementor result back to the test author instead of always
retrying implementation, closing a gap where a genuinely wrong test could
exhaust `test`'s retry budget with no way to recover inside the run.

### Added
- **`blocked_on` field** (`implementor-result.schema.json`, optional, `"contract"|"tests"`): when `status:"blocked"`, distinguishes "the contract itself is unsatisfiable" (`"contract"`, the default if omitted) from "I believe the authored tests — not the contract — are wrong" (`"tests"`, implementor-only, TDD-only).
- **`cmd_advance`'s `implementation` branch now reads `implementor-result.json`** (previously it moved to `test` unconditionally, no result file). `status:"blocked"` + `blocked_on:"tests"` + TDD routes to `test_implementation` instead — incrementing `iterations.test_implementation` (shared with the existing `red_not_confirmed` re-author budget), recording an `attempts.md` entry, and echoing a `note` telling the test author to verify (not blindly obey) the implementor's claim. Every other case (`implemented`; `blocked` with `blocked_on` omitted/`"contract"`; non-TDD) still routes to `test`, unchanged.
- **`dp-implementor.md` Rule 11 / Step 6, `dp-test-implementor.md` Rule 12**: the implementor may now report `blocked_on:"tests"` when it's confident the tests are wrong (it may read, just never edit, test files — including a retry pass's `failure_details`/`log_excerpt`, which show it the failing assertion directly); the test author, on re-entry via that `note`, is told to verify the claim against the contract before touching anything, and to leave a test unchanged if its own check finds the test was already correct.

### Changed
- **`ROLE_META`**: `implementor`/`test_implementor` gain `"schema": "implementor-result"` (was `None`). `category` (boundary/manifest handling) and `schema` (JSON-result validation) are now independent axes — a file role's git delta still drives boundary checks, but its status JSON is validated the same way a json role's result is.
- **`judge()`** (run-stage's bash-runner path): a file role now additionally runs `_finalize_json` on its status JSON after a `returncode == 0` exit — a produce/schema failure there is a failed attempt, not silently ignored. A nonzero exit still fails immediately with no retry (unchanged); only a "ran fine but bad/missing status JSON" failure gets the same one-shot error-fed retry a json role's bad output already gets — a plain crash must not force redoing the whole implementation attempt.
- **`cmd_finalize_stage`** (main-session/subagent handoff validation): its no-op guard now keys off `schema` presence rather than `category == "json"`, so a file role's status JSON gets the identical normalize→schema→persist-canonical treatment a json role's result already gets. Its success response now reports the role's true `category` (previously hardcoded to `"json"` — a file role finalizing would have been mislabeled).
- **`states/implementation.md` / `states/test_implementation.md` Step 3**: dropped the "absent — proceed exactly as before" branch and the separate `validate-result` call — the file is now guaranteed present and valid by the time the state file reads it (`run-stage`/`finalize-stage` already checked it), so both state files read it directly. `states/implementation.md` Step 5's hardcoded "(it will be `test`)" is corrected to name the new `test_implementation` possibility instead of asserting a transition the driver decides.
- **`SKILL.md` Global Rule 5 / §Role Execution**: the file-role exception added in 6.5.3 for "you still run `validate-result` yourself" is reverted — `driver finalize-stage` now covers file roles too, so the rule is unified back to one sentence for every role.
- **`RUNNERS.md`**: the 4 cline bash-runner templates (implementor/test_implementor/tester/reviewer) drop a hardcoded `-t 570`. This was a **deliberate** 6.3.0-era choice (giving cline its own ceiling after the driver's own default 10-minute runner cap was removed that same release), not an oversight — removed now because it gave cline alone a hidden ~9.5-minute cap no other CLI's template carried, at odds with the driver's unbounded-by-default philosophy; a user who wants a cap sets the runner's own `timeout` (and `-t` alongside it) explicitly, same as for any other CLI.

### Fixed (adversarial review, before release)
- **`dp-implementor.md`'s Step 6 example JSON included `"blocked_on": null`**, which the schema rejects (unlike `concern`, `blocked_on` has no `oneOf`-with-null — it's a string enum or entirely absent). A model that copied the example literally would have every `status:"implemented"` result rejected by `_finalize_json`, retried once, and potentially fail the whole role. Removed from the example; the prose now explicitly calls out that `blocked_on` must be omitted rather than nulled.
- **`states/implementation.md`'s "continue anyway" path after a `blocked` result dropped the boundary check and manifest recording**, calling `driver advance` directly instead of running the rest of Step 4 first. Rule 11 explicitly allows the implementor to leave partial changes when blocked — skipping Step 4 meant that partial delta never reached `changed-manifest.txt` (dropped from the `done` commit/review diff, and under `--worktree`, lost entirely once the worktree is cleaned up) and a TDD implementor's out-of-bounds test edit would pass unchecked. Restored: skip only the *empty-delta guard* sub-step, not boundary check / manifest recording.
- **`dp-test-implementor.md`'s Rule 12 ("if the tests were already correct, leave them unchanged") deadlocked the run** — an unchanged test suite is indistinguishable from "the role didn't run" to the empty-delta guard, so this exact scenario (the one the whole `blocked_on` feature exists to allow: the implementor being wrong) triggered a re-execute, then `stop and report`. Rule 12 now directs the test author to report `status:"blocked"` with a `concern` stating the tests were verified correct, instead of silently leaving them unchanged with no signal; `states/test_implementation.md`'s relay wording was generalized from "untestable as written" to cover both this case and Rule 11's original one.
- **No `cmd_advance` routing tests existed** for the `blocked_on` feature at all — every `write_implementor_result` call in the test suite used `status="implemented"`, and the newly-added `write_test_implementor_result` helper was never called. Added: `blocked_on:"tests"`+TDD routing (including the `attempts.md`/echo content), its exhaustion path, the non-TDD negative case (must fall through to `test`, `blocked_on` is meaningless there), the `blocked`+`"contract"` case (must still route to `test`), the `implementation`-branch `die()` paths (missing/schema-invalid file), and a test confirming `test_implementor`'s status file is validated but never read by `cmd_advance` (by design — there is no other role to route a test-author "blocked" to).
- **`states/failed.md`'s iteration-exhausted outcome list was missing `implementor_blocked_on_tests_exhausted`** (this diff's own new outcome) and didn't note it shares the `test_implementation` budget with `red_not_confirmed_exhausted`.
- **The `implementor-result.json not found` `die()` message told the user to "write a valid status file by hand" without showing the required shape.** Added a minimal inline example.
- **`states/implementation.md`'s blocked-relay message always said "unimplementable as written... revise plan.md"**, even for `blocked_on:"tests"` (where the implementor is blaming the tests, not the contract). Now worded per `blocked_on`.

### Known limitation
**codex + `--worktree`, for the implementor/test_implementor roles specifically.** Under `--worktree`, codex's `-C {project_root}` substitutes to `work_root` (the isolated worktree checkout), but this role's status JSON always lives under the true `project_root`'s `.dev-pipeline/` — outside codex's `workspace-write` sandbox. Since the status file is now mandatory, this combination now fails loudly (retry, then `all_runners_failed`) where it previously failed silently (the file was just never produced, unnoticed). Documented in `RUNNERS.md` with a workaround (prefer `claude`/`cline` for these two roles under `--worktree`); a proper fix (widen codex's writable roots, or switch to a stdout-capture result channel like tester/reviewer already use) is out of scope here — it would change a `RUNNERS.md` command template this project only ships after live CLI re-verification, which this change did not do.

### Versioning note
MINOR, not MAJOR, though this narrows a previously-graceful case into a hard
failure for one specific combination:
1. **Standard configurations are unaffected.** `claude`/`cline` runners, and a
   `codex` runner used without `--worktree`, have always had unrestricted
   write access to the status-file path — and the role prompts have
   unconditionally instructed writing this file since 6.5.0, so nothing about
   what a well-behaved runner needs to do has changed.
2. **The only combination that newly fails is `codex` + `--worktree`** for
   implementor/test_implementor — and that combination was already silently
   failing to produce the status file before this release; this makes that
   failure visible instead of introducing a new one.
3. **A run already parked in `implementation` when upgrading to 6.6.0** will
   `die()` with an explicit migration note if its `implementor-result.json` is
   absent (a pre-6.6.0 driver could reach this state without one) — the error
   message tells the user to either write the file by hand or start a new run;
   there is no silent data loss, but there is no automatic recovery either.

## [6.5.3] - 2026-07-13

`states/review.md`: a `main-session` reviewer now **always asks the user** before
running — continue in this session, or open a new session for an independent
review — instead of silently proceeding with a best-effort warning.

### Changed
- **`states/review.md` Step 2**: split the `main-session`/`subagent` bullet in two. `mode: "subagent"` is unchanged (executes directly). `mode: "main-session"` now asks the user first: "continue here" proceeds exactly as before (compact first, then execute per §Role Execution); "open a new session" stops without executing the reviewer or calling `driver advance` — the run stays parked at `review`, and the user is told to run `/dev-pipeline --resume <run_dir>` in a fresh session (which will ask the same question again, where "continue here" is now genuinely independent). New checklist item.
- **`SKILL.md`** "Reviewer independence" paragraph rewritten to describe the ask-every-time behavior instead of the prior silent best-effort-and-warn behavior.

### Fixed (adversarial review, before release)
- **`--auto-run` interaction was undefined.** `SKILL.md` describes `--auto-run` as "run end-to-end," which read as suppressing this question too. `--auto-run`'s description and `states/review.md` Step 2 now both state explicitly that this is a runtime safety confirmation, not the approval gate `--auto-run` skips, so it is always asked regardless.
- **`states/update_config.md`'s existing self-review acknowledgement** (the one-time approval when recommending a `main-session` reviewer) read as if it superseded the new per-review question, since neither file cross-referenced the other. Added a clarifying line: the two checks are complementary (one-time runner-choice consent vs. every-review runtime confirmation), not duplicative.

### Design note
The trigger condition is deliberately just "is the reviewer itself `main-session`" —
it does **not** track whether the implementor/test_implementor earlier in the same
run was also `main-session` (an earlier design explored this via run-directory marker
files, which turned out to need extra machinery to avoid re-triggering forever after
a legitimate `--resume` into a fresh session). Asking every time a `main-session`
reviewer is about to run sidesteps that: the question itself, not a persisted flag,
is what changes across a session boundary, so there is nothing to get stale.

### Versioning note
PATCH: prose-only (one state file + one SKILL paragraph); no driver.py logic,
schema, or state-machine change — same precedent as 6.5.1/6.5.2.

## [6.5.2] - 2026-07-13

`--request`-generated plans are now saved to `.dev-pipeline/plans/<YYYYMMDD>-<slug>.md`
instead of `<project_root>/plan.md` — no longer overwritten by the next `--request`,
and no longer left in the user's tracked working tree.

### Changed
- **`states/planning.md` Step 1**: computes `<project_root>/.dev-pipeline/plans/<YYYYMMDD>-<slug>.md` (UTC date, matching the driver's own run-id convention; a filesystem-safe slug of the goal; `-2`/`-3`… on a same-day collision) as the default save path, creating `.dev-pipeline/plans/` if needed. A user-named path still overrides this. Only affects `--request`; a `--plan <path>`-supplied plan is untouched, wherever it lives.
- **`states/done.md` / `AGENTS.md`**: the `done`-state merge precondition's `--untracked-files=no` rationale updated — it previously justified itself entirely by "the planner's plan.md sits untracked in project_root," which is no longer true for `--request` (now gitignored under `.dev-pipeline/plans/`, so it never appears in `git status` at all); the rationale now correctly scopes to `--plan <path>`-supplied plans, which can still live untracked anywhere in the tree.
- **`SKILL.md`**: `plan_path`'s Run Context description documents the new default location.
- **`AGENTS.md` / `README.md`**: runtime-layout diagram and usage docs updated with the new `.dev-pipeline/plans/` entry.

### Versioning note
PATCH: prose-only (SKILL orchestration + docs); no driver.py logic, schema, or state-machine change. `driver init --plan <path>` already accepted any path with no location assumption, so nothing there needed to change.

## [6.5.1] - 2026-07-13

`dp-test-implementor.md`: makes edge/error-case coverage a first-class, explicit
requirement instead of a parenthetical aside — in practice `test_implementor` was
observed writing only happy-path tests per Acceptance Criterion and skipping the
edge cases the criterion or Interface implies.

### Changed
- **Rule 4** rewritten: previously "Cover each Acceptance Criterion (and its edge/error cases)..." buried the requirement in a parenthetical, and its "nothing speculative" framing was ambiguous about whether an *implied* edge case (e.g. empty-list handling, given the Interface takes a list) counted as "speculative" and should be skipped. Now explicit: an AC covered by only a happy-path test is incomplete, and the speculative/non-speculative line is drawn precisely — behavior a criterion or the Interface implies is in scope, behavior nothing in the contract points to is not.
- **New Step 3.2** ("Author the tests"): requires identifying and testing each AC's edge/error cases (empty/null/zero/missing input, boundary values, invalid input, implied error conditions) immediately after its happy-path test, before moving to the next criterion — turns the rule into a concrete per-AC procedure rather than a general aside. Subsequent steps renumbered 3.3–3.6.
- **Step 4 checklist** item replaced with an edge-case-specific check (was the same parenthetical as Rule 4's old wording).

### Versioning note
PATCH: a single role-prompt's prose, no schema/driver.py/state-machine change — same precedent as 6.1.1's prose-only changes.

## [6.5.0] - 2026-07-13

Gives `implementor` and `test_implementor` a structured, optional way to report
"I concluded this can't be done as specified" instead of grinding indefinitely
(the only external supervision `main-session` runners have is their own
prose — no subprocess, no timeout, nothing driver.py can enforce) or writing a
vacuous test to satisfy a rule against empty/skip/always-true tests while
violating its intent.

### Added
- **`implementor-result.schema.json`** (`agents/dev-pipeline-tools/schemas/`): a small schema (`status: "implemented"|"blocked"`, `summary`, `concern`, `assumptions`) shared by both roles — same precedent as `test-result.schema.json` already being shared by the `test` and `red_test` states. `concern`-required-when-`status:"blocked"` is documented (not schema-enforced, matching `test-result.schema.json`'s `failure_type` convention — the hand-rolled validator doesn't support `if`/`then`).
- **`driver.py`**: `build_stage_input` now wires an `output_file` for `implementor`/`test_implementor` (`<iter_dir>/implementor-result.json` / `<iter_dir>/test_implementor-result.json`), reusing the same mechanism `tester`/`reviewer` already have — a role that never saw its own `iter_dir` (the driver's `_STAGE_INPUT_CONTROL` deliberately strips it from every role's prompt) now gets an absolute path via `output_directive()` instead. `output_directive()` grows a third branch for these two roles: always "write it yourself" (never a stdout-capture directive — a file role's stdout is tool-call chatter, not a clean JSON answer), since this is an OPTIONAL signal alongside the git delta (the role's real result), not a replacement for it. The three `category == "json"` gates in the main-session/subagent handoff path (the output directive, the stale-output cleanup, and the `output_file` field in the handoff payload) are relaxed to also cover these two roles, so the mechanism works identically across bash, main-session, and subagent execution. `judge()`'s stale-output cleanup is extended the same way, so a retry can't mistake a previous attempt's leftover status file for the current one.
- **`driver validate-result --type implementor|test_implementor`**: validates a role's result-status JSON against the shared schema. The `--type`→schema mapping changed from a 2-way ternary to a dict (`SCHEMA_BY_TYPE`) — the ternary would have silently routed a third `--type` value into its `else` branch (i.e. validated an implementor result against `review-result.schema.json`), a real bug caught in review before it shipped.
- **`dp-implementor.md`**: Rule 5 (ambiguity) now explicitly distinguishes "unsure how the existing codebase works" (keep reading) from "unsure what the contract intends" (no amount of extra reading answers that — state the smallest reasonable assumption instead), with a matching stop-condition at the end of the codebase-exploration step. New Rule 11: when implementation is genuinely impossible as specified (not just difficult), stop and report `status: "blocked"` with a specific `concern` instead of forcing a broken implementation. A new final workflow step writes the result-status JSON to the path given in the prompt's output directive.
- **`dp-test-implementor.md`**: the same two rules, adapted — Rule 10 for ambiguous expected behavior, Rule 11 for "no meaningful test can be written for this Acceptance Criterion as specified," which is a real dead end this role could hit today: Rule 3 already forbids empty/skipped/always-true tests, so without an escape hatch a genuinely untestable criterion had no valid path forward. Same final "write your result status" step.
- **`SKILL.md` §Role Execution / `states/implementation.md` / `states/test_implementation.md`**: the existing empty-delta guard ("no file changes means the role didn't run — re-execute") now runs *after* checking for a result-status file. A `status: "blocked"` result is treated as the role's deliberate outcome (an empty or partial delta is expected and correct) and is never mistaken for a no-op re-execute trigger — this reordering was a real gap caught in review: without it, the guard would have fired first in exactly the `main-session` case this feature targets, silently discarding the "blocked" signal before it was ever surfaced. A `blocked` result is relayed to the user with a suggestion to revise `plan.md`; the user decides whether to stop or continue — nothing here auto-halts a run.

### Fixed (adversarial review, before release)
- **`install.sh`** never copied the new `implementor-result.schema.json` — every real install would have had `validate-result --type implementor|test_implementor` fail with "Schema file not found" (advisory-only, so silently swallowed), permanently discarding a `blocked` signal in exactly the `main-session` case this feature targets. Added to the copy list; file count corrected 4 → 5.
- **`driver validate-result`** used a strict JSON parser with no fence tolerance, unlike a json role's `default` normalizer — a model fencing its status JSON despite the "do not fence" prompt directive would have had its result silently treated as absent. Now falls back to the same fence/prose-stripping normalizer json roles get, with a regression test.
- **`dp-implementor.md` Rule 10 / `dp-test-implementor.md` Rule 2** ("never touch `.dev-pipeline/`" / "stay inside `test_paths`") had no exception for the new result-status write, directly contradicting the new final step that requires it. Both rules now explicitly exempt the output-directive path.
- **`SKILL.md` Global Rule 5** claimed "a bash runner's result file is already schema-validated by run-stage" — true for json roles, but `judge()` never schema-validates a file role's optional status JSON, so `states/implementation.md`/`test_implementation.md`'s own instruction to call `validate-result` after a bash runner contradicted this global rule. Rule 5 now carves out the file-role exception.
- **`states/implementation.md`** referenced "[Step 4]'s result-status check" where it meant Step 3 (the check that actually runs before the empty-delta guard) — a mismatch with the correctly-numbered mirror text in `test_implementation.md`.
- **`test_implementation.md`'s checklist** was missing the "empty-delta guard applied when no blocked status was found" item `implementation.md`'s checklist already had.
- **The `blocked`-relay wording** in both state files quoted `<concern>` unconditionally, but the schema does not force `concern` non-null even when `status: "blocked"` (documented as doc-only, not schema-enforced) — both now fall back to `summary` when `concern` is absent.

### Versioning note
MINOR: new schema, two new `validate-result` types, and new (fully optional, backward-compatible) role behavior. A result-status file's absence is handled identically to before this release; no existing config, run, or prompt breaks.

## [6.4.0] - 2026-07-13

Adds `--worktree`: a per-run flag (not a config key) that isolates a pipeline
run's code edits and working-tree git bookkeeping in a fresh git worktree +
branch instead of the project's own working tree, so a run never touches the
user's real checkout while it's in progress and independent runs can proceed
concurrently. On `done`, the branch is merged back (after verifying the
checkout is safe to merge into) and the worktree is removed; a `failed` run's
worktree is preserved for manual debugging/cleanup instead.

### Added
- **`driver.py init --worktree`.** Validates `project_dir` is a git repo with an existing HEAD, then `git worktree add`s a checkout at `<project_dir>/.dev-pipeline/worktrees/<run_id>` on a new `dev-pipeline/<run_id>` branch (off `project_dir`'s current HEAD/branch, recorded as `worktree_base_ref` — a SHA if HEAD was detached). If `project_dir` is a strict subdirectory of a larger repo, `work_root` is adjusted to the matching subdirectory inside the new checkout (`git worktree add` checks out the whole repo, not just the subtree). `run_dir` is now claimed atomically (`mkdir(..., exist_ok=False)` in a retry loop, re-picking `rid` via `reserve_run_id` on collision — `reserve_run_id` alone only narrows a same-second collision window from `run_id_new()`'s 1-second resolution, it doesn't close it), and any failure after the worktree is created — including a `latest`-symlink precondition or an unexpected disk error — rolls the worktree, branch, and `run_dir` back via a `finally` block, so a failed `--worktree` init genuinely leaves nothing on disk (not just "before `run_dir` exists," which left a gap a die() between worktree-add and the final write could fall into). `state.json` gains `work_root` (the worktree checkout, or `project_dir` for a non-worktree run — identical either way to every prior run), `worktree_branch`, and `worktree_base_ref` (all optional in `state.schema.json`, unvalidated at runtime like the rest of `state.json`, so old runs are unaffected).
- **`driver.py cleanup-worktree --run <run_dir>`.** Idempotent teardown of a `--worktree` run's checkout + branch (a no-op for a non-worktree run): `git clean -xdf` the checkout (the `test` stage routinely leaves build/test caches there), `git worktree remove --force` it, then `git branch -d` (safe delete only — never `-D`; an unmerged branch is reported, not force-discarded). This is the sole place a worktree/branch is removed; merging is not its job.
- **`work_root` joins `tdd_mode` as an always-echoed value** (`cmd_advance`'s `transition()`), and `worktree_branch`/`worktree_base_ref` are echoed at `done`/`failed`. Every git-touching state file (`implementation.md`, `test_implementation.md`, `review.md`, `done.md`, `resume.md`) now reads `work_root` from the echo for its `git -C .../cd ...` commands instead of `project_root` — identical under a normal run, the isolated worktree checkout under `--worktree`. `cmd_run_stage`'s runner `cwd`/`{project_root}` placeholder substitution (the placeholder *name* is unchanged for existing `RUNNERS.md` templates) and both `build_stage_input` call sites (`transition()`, `cmd_resume`) now resolve to `work_root`, falling back to `project_dir` for old stage-input/state data. `cmd_resume`'s manual-recovery recipe (printed when `last-advance.json` is missing) also targets `work_root`.
- **`states/done.md` §Step 2 (new): merge + cleanup, worktree runs only.** Before attempting `git merge --no-ff <worktree_branch>` into `project_root`, verifies `project_root` is still on `worktree_base_ref` (falling back to a commit-SHA comparison for a worktree created from a detached HEAD, which has no branch to return to) *and* clean of **tracked** changes (`status --porcelain --untracked-files=no` — deliberately ignoring untracked files, since the planner's `plan.md` sits untracked in `project_root` by design in the default flow and would otherwise fail this check on every single run; git's own merge still refuses if an untracked file would actually be clobbered). The merge is skipped entirely (not attempted-then-aborted) if either check fails, and the worktree/branch are left untouched either way. A merge conflict likewise stops without an automatic `--abort`, showing the conflicting files and leaving resolution to the user. `cleanup-worktree` runs only after a successful merge. `states/failed.md` preserves a worktree run's checkout for debugging and points the user at `cleanup-worktree` to remove it manually — the pipeline never auto-discards a failed run's work.
- Docs: `AGENTS.md` (new "Worktree isolation" subsection; the `project_dir`/`work_root` split threaded through the runner-abstraction, change-manifest, and echo-channel sections; the security/trust-model sandbox note reconciled — `--worktree` isolates from `project_dir`'s working tree, it does not add a tool sandbox), `README.md` (usage + Runtime directory + Driver CLI), `agents/skills/dev-pipeline/SKILL.md` (Step 0, Run Context, Role Execution), all touched `states/*.md`.

### Versioning note
MINOR: `--worktree` is fully opt-in and backward-compatible — a run started without it behaves byte-for-byte as before (`work_root` simply equals `project_dir`, and every echoed/state field this release adds is new, not repurposed). No existing config, state.json, or install breaks.

## [6.3.0] - 2026-07-12

Removes the hardcoded 10-minute (600s) default timeout on bash runners: a
runner with no `timeout` set now runs unbounded instead of being SIGKILLed
at 600s. `timeout` stays available as an opt-in per-runner cap, unchanged.

### Changed
- **`driver.py`: bash-runner `timeout` is now optional with no default cap.** `cmd_run_stage` no longer falls back to `600` when a runner omits `timeout` — it passes `None` through to `_run_one`, which now blocks on `proc.wait(timeout=None)` (stdlib's documented indefinite-wait behavior) instead of capping at 10 minutes. The post-exit process-group drain (waiting out a backgrounded group-mate after the direct child exits) is likewise uncapped when `timeout` is `None` — an accepted trade-off documented in `_run_one`'s docstring: a runner that backgrounds a job it never reaps (`… & true` with a non-exiting child) will hang run-stage indefinitely; set an explicit `timeout` on that runner if you need a hang backstop. Runners with `timeout` set behave exactly as before (hard cap, whole process group SIGKILLed on expiry).
- **`_resume_live_window`'s heuristic accounts for unbounded runners.** This best-effort heuristic (decides whether to flag a resumed run as `possibly_live`, prompting the SKILL to confirm before re-dispatching) previously assumed every runner without an explicit `timeout` took 600s. With unbounded now the default, that assumption would under-estimate how long a runner might legitimately still be running, letting `cmd_resume` silently treat a still-live unbounded runner as dead and risk a double-dispatch. The per-runner fallback used by this heuristic (`_RESUME_ASSUMED_UNBOUNDED_RUNNER_SECS`, only used here — it does not bound actual execution) is now 24h instead of 600s: erring long only costs an extra confirmation prompt on a genuinely dead run, whereas erring short risked working-tree corruption from a concurrent double-dispatch.
- **Docs** (`AGENTS.md`, `RUNNERS.md`, `agents/skills/dev-pipeline/SKILL.md`): every "timeout defaults to 600s" statement updated to describe the new unbounded-unless-set behavior, including the guidance that a quiet bash-runner log is not proof of a hang (there is no default backstop killing it) and that hang detection now requires an explicit `timeout`.

### Added
- `test_no_timeout_runs_unbounded` (`test/test_driver.py`): a bash runner with no `timeout` key runs to completion (`sleep 2`) without being killed — a regression guard for `_run_one`'s `timeout=None` path (deadline computation, `proc.wait`, and the group-drain loop all handle `None` without raising or misreporting a timeout).

### Versioning note
MINOR: this changes `driver.py`'s runtime behavior for any existing config that omits a runner's `timeout` (that runner now runs unbounded instead of being capped at 600s) — backward-compatible in the sense that no existing config becomes invalid and no install breaks, but the default *behavior* for unset `timeout` genuinely changes, which is more than a docs/patch-level change.

## [6.2.1] - 2026-07-12

Refines 6.2.0's bash-runner observability with real CLI runs: adopts a codex
flag that makes its log genuinely useful, deliberately declines the
equivalent for claude, and reduces the token cost of the SKILL's background
polling. Docs/config-template only — no `driver.py` logic changes (see
Versioning note below).

### Added
- **`codex` templates gain `-c model_reasoning_summary=auto`** (`RUNNERS.md`, all four roles). Verified: this makes codex stream genuine natural-language reasoning text mid-run (e.g. `"I'm reading the contract and diff first, then I'll inspect every changed file..."`) plus the shell commands it runs and their output — previously the log showed only a banner, the echoed prompt, and the final answer, with a long silent gap between. `-o {output_file}` result capture is untouched and still validates cleanly (confirmed via `driver._normalize_output` + schema validation) — the flag only adds stdout content, so `output_directive()`'s command-shape branching (`driver.py`) is unaffected. Verified end-to-end through the real `driver run-stage` for three of the four roles (implementor, tester, reviewer); the fourth, test_implementor, is inconclusive — that run hit this account's codex usage quota mid-task rather than completing, though it showed rich reasoning text streaming right up to that point (see `RUNNERS.md`'s Verified combinations table for the per-role detail).

### Changed
- **SKILL.md §Role Execution: separated log *checking* from user-facing *relaying*.** Checking a runner's log (a single file read) is cheap; composing a "still running" note every cycle is not. The guidance now checks every ~30–60s as before, but only relays a note to the user when the log has new content since the last relay — a log that never changes (e.g. a claude json-role reviewer) now gets a sparse fallback relay (~90–180s) instead of one every check. This is CLI-agnostic by construction (it reacts to "did the log change," never to which CLI produced it) and does not weaken the underlying always-background-if-possible default — the point of that default (a multi-minute call shouldn't leave the session looking hung) is preserved by the fallback cadence, not removed.
- **`states/{implementation,test_implementation,red_test,test,review}.md`**: the "prefer running in the background" sentence in each now notes that a quiet log doesn't mean the runner is stuck, pointing at `SKILL.md §Role Execution` for the reasoning instead of repeating it five times. Their checklist items changed from treating "the log was polled" as a success condition (which read as a failure for a runner whose log is quiet by design, or on a foreground-only host) to "checked periodically ... relayed only when there was something new to say," matching the new SKILL.md guidance.
- **`states/review.md` no longer names an LLM.** It previously said "e.g. a stdout-redirect claude runner" when explaining why some reviewer logs stay quiet — a Global Rule 8 violation (state files must reference roles abstractly; which LLM runs a role belongs only in `config.runners.<role>`). Reworded to name the command *shape* (stdout-redirect), not the CLI.
- **`RUNNERS.md`**: log-strategy table's codex row updated to reflect the new flag (still ✅ real-time, now with genuinely useful content); a new "Why the claude json role doesn't stream reasoning" section records that a full `stream-json` + `--json-schema` + `tee` + exit-code-preserving-subshell + extraction pipeline for claude's tester/reviewer was prototyped and verified working end-to-end (real-time log growth, correct exit code, clean schema-valid result) but rejected — the complexity cost wasn't worth it once this account's claude CLI (2.1.207, observed 2026-07-11) turned out to return empty `thinking` fields (an opaque `signature` blob instead of readable text), leaving only tool-call names visible. The prototype's `--json-schema` payload was supplied by hand-editing `_run_one`'s `subst` dict in the verification harness, not through a real `driver.py` placeholder — `{schema_file}` isn't one of the six placeholders `driver.py` actually substitutes, so reviving this design would need a matching `driver.py` change first, not just the command shape recorded in `RUNNERS.md`. Re-evaluate if a future claude/account streams genuinely readable reasoning. The file-role `stream-json` recommendation stays (it's cheap there — no extraction needed) but now says plainly it shows tool names, not reasoning. Also records (Prerequisites) that this account's local codex config (`model_reasoning_effort=low`, `gpt-5.4-mini`) answered without actually reading the contract/diff in roughly 3 of 5 test runs, while still returning a schema-valid result — a local reliability characteristic, not a `RUNNERS.md`/`driver.py` bug, tracked for future investigation.

### Existing installs
If you already ran `--update-config` under 6.2.0, your `config.json` has the *old* codex commands baked in verbatim — this release doesn't change them for you. `--update-config` recommends from your project's **installed** `RUNNERS.md` copy (`.agents/skills/dev-pipeline/RUNNERS.md`, written by `install.sh`), not this source repo's copy, so re-running `--update-config` alone won't pick up 6.2.1's changes — **re-run `install.sh` first** (installs 6.2.1's `RUNNERS.md`/`SKILL.md`/`states/`), then `/dev-pipeline --update-config` to get `-c model_reasoning_summary=auto` (and any other `RUNNERS.md` refinements) into your recommendations.

### Versioning note
This is a PATCH bump, not MINOR: `driver.py`'s logic and state machine are byte-for-byte unchanged (only `__version__` itself differs), no existing install breaks, and no config auto-changes (`RUNNERS.md` is human/`--update-config`-facing only — "Nothing here is read by the driver"). 6.1.1 is a related but not identical precedent — it was a patch that included both a `driver.py`/schema change (the `source` field removal) and a `dp-planner.md` prose behavior change, so it doesn't establish "prose-only is always patch" on its own; it's cited here only for "shipped prose behavior changes have been treated as patch-eligible before," not as an exact match. A reasonable counter-argument is that shipping a new observability capability in the installed templates is "backward-compatible new functionality" and thus MINOR by strict SemVer; noted here for the record rather than treated as settled.

## [6.2.0] - 2026-07-11

Adds `cline` as a third supported bash-runner CLI, real-time observability for
every bash runner, and a verified runner-command catalog.

### Added
- **Live bash-runner log streaming.** `_run_one` now connects a bash runner's combined stdout+stderr directly to `<iter_dir>/<role>-runner.log` (truncated fresh each `run-stage` call), so a long-running LLM CLI's progress is observable via `tail -f` while it is still executing, not only after it exits. `run-stage` echoes the path as `log_file` on both success and `all_runners_failed`. Error/timeout reasons now quote a tail of this log instead of the old in-memory `stderr[-300:]` (the old pipe-based capture is gone — stdout/stderr go straight to a file, removing the prior `communicate()`-deadlock class of risk entirely). `SKILL.md` §Role Execution documents the companion pattern: run a bash-runner `run-stage` call in the background where the host supports it, and poll the log every ~30–60s to keep the user informed instead of going silent for the runner's whole timeout window.
- **`cline` as a third bash-runner CLI**, alongside claude/codex. `agents/dev-pipeline-tools/RUNNERS.md` is a new verified command catalog — one template per role × CLI, each combination actually run end-to-end (including through the real `driver run-stage` path, not just standalone) before being marked verified. Because cline has no clean-stdout mode or native result-file flag, its json-role result strategy is prompt tool-writer (the model Writes `{output_file}` itself) rather than a stdout/`-o` capture — and because `--auto-approve` is all-or-nothing (no per-tool allowlist like claude's `--allowedTools` or codex's `-s read-only`), a cline tester/reviewer carries the same no-hard-sandbox caveat as a `subagent`/`main-session` runner; `RUNNERS.md` and `states/update_config.md` both surface this so claude/codex stay the default recommendation for read-only roles.

### Changed
- **`output_directive()` collapses to two branches by command *shape*, not CLI identity**, keeping bash-runner prompts as close to identical as the underlying CLIs allow: a command whose template already references `{output_file}` (claude's `> {output_file}` stdout redirect, or codex's CLI-native `-o {output_file}` result flag) is told to give the JSON as its final answer only; a command with no such reference (cline) is told to write it to that exact path itself. This also **fixes a latent bug**: previously only a literal `> {output_file}` redirect was recognized, so a codex `-o {output_file}` runner was wrongly told to write the file itself (impossible under `-s read-only`, since that's a model tool-write, not codex's own harness-level result capture) — codex reviewers/testers now get the correct instruction.
- **`dp-tester.md` / `dp-reviewer.md`** — unified their output-instruction wording to the same transport-neutral sentence ("...either as your final answer text, or written to the exact file path given"), matching the `output_directive()` change above; previously `dp-reviewer.md` was written as if the file-write path were the only option.

### Fixed (found by an adversarial review of this release before it shipped)
- **`_run_one` no longer reports a runner "done" while a group-mate it backgrounded is still running.** Switching stdout/stderr from a `PIPE` to a direct file redirect (above) removed the pipe-EOF wait that `communicate()` used to provide for free — the direct child's own exit is not proof the whole process group is finished. Reproduced concretely: a command like `real-work & true` returned in ~10ms with `ok: true`, while the backgrounded work (and its log output) was still 2s from completing. Fixed by polling the process group for emptiness, bounded by whatever remains of the runner's `timeout`; a group-mate that outlives that budget is killed and reported as a timeout, so this can never hang longer than the configured limit, and adds no measurable overhead to the (overwhelmingly common) non-backgrounding case.
- **The `cline` runner templates in `RUNNERS.md` originally piped through `sed` to strip the ANSI color codes cline emits even off a TTY, and that pipe caused two real bugs**, both traced to the same root cause: `/bin/sh` reports a pipeline's LAST command's exit status (sed's, almost always `0`) unless `pipefail` is active — not POSIX, silently a no-op on `dash` (`/bin/sh` on Debian/Ubuntu) — so a crashing `cline` could be reported as a false `ok: true`; and plain `sed` fully block-buffers a pipe when its output isn't a TTY (measured on both BSD and GNU sed), so the log stayed at 0 bytes until the whole command exited, silently defeating real-time streaming for cline specifically. A POSIX-portable exit-code-capture pattern plus `sed -u` fixed both (verified end-to-end, including a `PATH`-shadowed failing fake `cline` correctly returning `ok: false`) but added real complexity for what it bought — so the templates now run `cline` **directly, with no pipe at all**: its exit code and real-time output both come through unmodified because there's no pipe left to lose them through. The cost is that `<role>-runner.log` now contains raw ANSI escapes for a cline runner (fine for a human `tail -f`; strip them host-side only if relaying an excerpt somewhere that won't render them, e.g. a chat message) — see `RUNNERS.md`'s "Why the cline templates don't strip ANSI" for the full trade-off and the abandoned intermediate fix.
- **`RUNNERS.md`** incorrectly claimed cline has no flag for a separate system prompt; it does (`-s`/`--system`), the project just doesn't use it (a deliberate convention — see the file for why) — the false technical claim is corrected without changing the command templates.

## [6.1.1] - 2026-07-11

### Removed
- **The review-result `source` field.** It was write-only provenance: no state file read it, `done.md` never surfaced it, and it duplicated `config.snapshot.json`'s `runners.reviewer[0].type`. It was also the root cause of a correctness bug — `dp-reviewer.md` told the role to always emit `"source": "bash-runner"`, and the driver only corrected it to the true execution mode inside `finalize-stage`/`run-stage`'s `judge()`; if that stamp step was ever skipped (e.g. a `subagent`/`main-session` reviewer whose `finalize-stage` call didn't run), the false `bash-runner` value passed `advance`'s schema re-validation undetected, because it was a valid enum member. Removing the field removes the possibility of a false value entirely. `review-result.schema.json` drops `source` from both `required` and `properties`; `dp-reviewer.md`'s example output and checklist no longer mention it; `_finalize_json` no longer takes a `source` argument. For old-run compatibility, `advance` strips a legacy `source` key from a result persisted by a pre-6.1.1 driver before re-validating (new emissions are still rejected at `finalize-stage`/`run-stage`).

### Changed
- **`dp-planner.md`** — when the interface has real alternatives (e.g. exceptions vs. a result value, a data shape, how a function is decomposed), the planner now surfaces the 1–2 leading options with a one-line trade-off each instead of silently picking one, since the interface is the one HOW decision it owns and it binds every downstream role. `## Background` is reframed from a bare "why" to "why + what prompted it + the intended outcome", giving the reviewer and implementor clearer intent to judge the diff against.

## [6.1.0] - 2026-07-10

Re-introduces `--resume`, removes an orphaned subcommand, and folds Karpathy-style discipline into the authoring role prompts.

### Added
- **`--resume [<run_dir>]` / `driver resume`** — continue an **interrupted** run from the state it stopped in, without re-running `init` (which starts a NEW run) or redoing completed stages. Every `advance` now persists its full landing echo to `<run_dir>/last-advance.json` (written **before** `state.json`, so a crash between the two is disambiguable); `driver resume` replays it. Handles: parked-at-init (→ advance), normal replay (re-emits the exact echo incl. runner arrays + re-persists a byte-identical `stage-input.json`), the crash window (advance died before persisting → re-run advance, idempotent), terminal states (replays the full `done`/`failed` echo for idempotent finalization), a best-effort `possibly_live` concurrency flag, and a precise manual recipe for a run with no `last-advance.json`. `states/resume.md` adds the SKILL-side delta recovery (worktree-vs-index minus the manifest, boundary-checked, `record-changes`d) so a pre-crash authoring edit is never silently dropped. Ported from the parked 5.3.0 branch and adapted to the 6.0.0 flow (no header; attempts are auto-recorded by advance).
- **Karpathy-style discipline in the authoring prompts** — `dp-implementor.md` sharpens its scope rule to **minimum, surgical changes** (nothing speculative; touch only what the contract requires; do not refactor unrelated code or delete pre-existing dead code) and to **surface assumptions instead of guessing** when the contract is ambiguous. `dp-test-implementor.md` frames each test as the **executable success criterion** for its Acceptance Criterion and adds a **test-only-what-is-specified** rule (no speculative/redundant tests).

### Removed
- **The orphaned `normalize-review --source codex` subcommand** — its only caller, the pre-3.0.0 `codex-adversarial-review` runner type, was removed in 3.0.0; no state file, runner, or doc referenced it. (A codex reviewer is still fully supported as a bash runner emitting plain review-result JSON through the `default`/`passthrough` normalizer.)

### Changed
- Conservative MD pass: trimmed a couple of clearly-redundant restatements (the files are deliberately dense LLM-executed prose, so load-bearing rules/checklists/structure were preserved).
- Provider-neutrality: replaced the Claude-Code-specific `/advisor` reference in `done.md`/`dp-tester.md` with host-agnostic wording ("a dedicated advisory/code-review capability").

## [6.0.0] - 2026-07-10

Config/plan flow redesign: the `plan.md` config header is **removed**, config lives solely in `config.json` and is written by a new conversational **`--update-config`** flow, and `main-session`/`subagent` handoffs get a firm single-role persona preamble. Also: `advance` records retry context to `attempts.md` itself (the `append-attempt` subcommand is gone), and the LLM-named `claude-cli`/`codex-cli` normalizers are replaced by a single `default`. Folds in the reviewer-independence guidance drafted for 5.4.1. **Breaking** — existing installs must reconfigure.

### Removed (breaking)
- **The `plan.md` `dev-pipeline-config` header is gone.** `plan.md` is now a pure spec body (Requirements, Acceptance Criteria, Interface). All header machinery is deleted: `parse_plan_header`, `merge_plan_header`, the `_HEADER_PROSE_KEYS`/`_HEADER_EXEC_KEYS` whitelists, the `--header-approved` flag (on `init` and `validate-config`), and the `driver.allow_unattended_header_merge` opt-in. `init` now reads the whole plan file as the contract and snapshots `config.json` verbatim; there is nothing untrusted to merge, which **simplifies the trust model** (the plan carries no config).
- **`driver set-runners` is replaced by `driver apply-config`** (see Added). The one-time, runners-only, refuse-when-configured write is gone; config setup is now the re-runnable `apply-config`.
- **The `append-attempt` subcommand is removed** — `advance` now records the retry context to `attempts.md` itself (see Changed), so there is no separate step to call (or forget).
- **The `claude-cli`/`codex-cli` normalizer names are removed** — replaced by a single LLM-agnostic **`default`** (see Changed). A config using an old name is now rejected by the schema enum.

### Added
- **`driver apply-config --config <path> --values-file <path>`** — the sanctioned config-write path behind `--update-config`. Deep-merges a partial `{driver?, llm?, runners?}` values file into `config.json` (only the named leaves change; a role's whole `runners` array replaces wholesale), validates the merged result (placeholders/invalid runners → nothing written), writes atomically, and seeds the config from the template first if absent. Unlike the removed `set-runners`, it is **re-runnable** — config only ever changes here, kept conservative. Safety: it never deletes the config even when `--values-file` points at the config itself, and a failed apply on an absent config leaves nothing on disk (seed happens only on a valid merge).
- **`--update-config [<plan>]` entry mode + `states/update_config.md`** — a conversational, host-session step (like the planner) that recommends the per-role **runners** (execution mode + model), the **`llm.*`** instructions, and the **`driver`** gate keys, gets the user's approval (required even under `--auto-run`), and calls `apply-config`. A plan path is **optional** (it sharpens the recommendations); omit it to reconfigure from the repo + current config. `--plan`/`--request` **auto-run** it (with their plan) when the config is incomplete; run it standalone to reconfigure.
- **`bootstrap-config` reports `config_complete`** — true when `config.json` is ready to run (runners configured **and** no placeholders), so the SKILL knows whether the config gate needs to run. Reported on both the `created` and `exists` paths.
- **`main-session`/`subagent` persona injection** — `run-stage` now prepends a firm role-switch preamble to the assembled system prompt for handoff modes ("act SOLELY as the dev-pipeline `<role>` … disregard any prior role/context; **do ONLY the work THIS role's instructions define, then STOP — do not take on the other pipeline stages**"; the role instructions win for that role's own work, so e.g. the implementor's build-check is untouched), so prior-role/context bleed cannot weaken the role and a `main-session` implementor does not run the project's test suite itself. `SKILL.md §Role Execution` adds the matching discipline: freshly Read the `system_file` at each role start, and re-anchor as the orchestrator after the role. Bash runners are unaffected (fresh subprocess + hard sandbox).
- **Reviewer-independence rule in `dp-reviewer.md`** (drafted for 5.4.1, shipped here) — "You did not write this code — review it as an independent auditor; judge only the diff+contract from disk, do not rely on prior context or memory of how it was produced." Assembled into the reviewer prompt for **every** mode. **Honest limit:** a same-session reviewer retains latent memory after compaction, so this is best-effort independence, not equivalent to a fresh subagent — ordering stays bash/subagent > main-session.

### Changed
- **`config.example.json` ships `runners` as the `unconfigured` sentinel** (was concrete claude bash defaults). The test suite and the real-LLM e2e harness now define their concrete runners inline instead of reading them from the template.
- **`migrate-config` resets runners to `unconfigured`** (was: replace with the template's bash defaults) — a legacy config is converted to the setup state, then reconfigured via `--update-config`. It also **drops removed `driver` keys** (e.g. the pre-6.0.0 `allow_unattended_header_merge`), which `apply-config`'s deep-merge cannot delete — so a 5.x config carrying one stays repairable.
- **`advance` records the retry context to `attempts.md` itself** — when it routes a failure back to a retry (test→implementation, review→implementation/test_implementation, red_test not-confirmed→re-author) it writes the failure log / blocking findings / vacuous-tests note directly, using the result it already loaded. The old two-step "advance then `append-attempt`" (which a `main-session` orchestrator could forget) is gone; `states/test.md`/`review.md`/`red_test.md` drop the manual step.
- **Normalizer `default` replaces `claude-cli`/`codex-cli`** — the two were identical (both strip a markdown fence + extract the outermost JSON), so they collapse into one LLM-agnostic `default`, which is now the default for both bash and handoff json roles (`passthrough` remains the strict opt-in). A `normalizer` on a **file** role (implementor/test_implementor, which produce a git delta, not JSON) is now rejected, and a removed/unknown normalizer in a config is **named with a hint** (not the generic oneOf error). A pre-6.0.0 name frozen in a run's `config.snapshot.json` still normalizes **leniently** at runtime (any non-`passthrough` value ⇒ tolerant), so an in-flight run resumed after upgrading does not silently start rejecting fenced output.
- **The 5.4.0 absolute ban on a `main-session` reviewer alongside a `main-session` implementor is relaxed to a preference** (drafted for 5.4.1). It over-restricted the exact host it targeted: one with *neither* an LLM CLI *nor* a subagent tool forces every role to `main-session`. Now: prefer `subagent`/`bash` for the reviewer; a `main-session` reviewer is acceptable **only as a last resort**, as a best-effort gate (compact first, rely on the persona preamble + the reviewer's independence rule, warn the user).
- **Global Rule 10's config-write exception** now names the `--update-config`/`apply-config` flow (was `set-runners`). `SKILL.md` gains the `--update-config` entry mode and config gate, drops the header trust gate (old Step 7) and the runner-setup dialog (old Step 5, subsumed by `--update-config`); `states/planning.md` writes a spec-only plan; `dp-planner.md` no longer authors a config header. `AGENTS.md`/`README.md`/`install.sh` updated to match.
- **Default iteration budgets raised**: `max_test_iteration` 3→5 (retries after a test failure) and `max_test_implementation_iteration` 2→3 (test re-authoring when RED is not confirmed) — 3 total attempts was tight for a retry loop meant to catch fixable implementor/test-author mistakes, not just structural ones. `max_review_iteration` stays 3.

## [5.4.0] - 2026-07-09

`config.runners` — previously the one setting nobody ever inferred or confirmed — is now configured through a one-time interactive dialog right after the config is first bootstrapped, instead of silently copying the template's concrete claude commands.

### Added
- **`driver bootstrap-config` seeds `runners` as `"unconfigured"`** — each role gets `[{"type": "unconfigured"}]` instead of the template's concrete bash commands. `config.example.json` itself is untouched (still the known-good bash defaults `migrate-config`, the real-LLM e2e harness, and the test suite rely on); only the newly written `.dev-pipeline/dev-pipeline.config.json` differs.
- **`driver set-runners --config <path> --runners-file <path>`** — the one-time write of the user-confirmed `runners` into a still-unconfigured config: validates the given runners (schema shape + actionable business-rule messages — bash-needs-command, no-command-on-subagent/main-session, homogeneous array, and named errors for unknown/legacy/`unconfigured` types so the SKILL's repair loop can act on them), then atomically replaces `runners` and deletes the scratch file on success (left in place on failure, for the retry). Refuses to run once runners are already configured — edit the config file directly after that.
- **`save_json` is now atomic** (temp file + `fsync` + `os.replace`) — a crash mid-write can no longer truncate `config.json` or `state.json`.
- **`validate-config` detects "not configured yet"** — a config with any `unconfigured` role fails with an actionable message (before the generic schema error), pointing at the interactive setup or `set-runners` directly.
- **`SKILL.md` Step 5: interactive runner-setup dialog** — runs whenever `bootstrap-config` reports `runners_configured: false` (a fresh bootstrap **or** a first run that was interrupted before setup finished — the setup is resumable, not deadlocked), for **both** `--request` and `--plan`, even under `--auto-run`. Detects available CLIs (`command -v claude`/`codex`), proposes a runner per role with reasoning in a single batched message (mirroring the planner's Step 2 confirmation pattern) — bash with a scoped tool envelope when a CLI is available (reviewer defaults to read-only, the only mode with a **hard** sandbox); `subagent`/`main-session` otherwise, with the no-hard-sandbox trade-off stated plainly for the reviewer, and never a `main-session` reviewer when the implementor is also `main-session` (self-review). A bounded (3-attempt) repair loop handles `set-runners` validation failures. This is the SKILL's one sanctioned exception to Global Rule 10 ("never modify the user's config yourself"), scoped to a config whose runners are still unconfigured. `bootstrap-config` reports `runners_configured` on both the `created` and `exists` paths; `migrate-config` refuses a still-unconfigured config (it converts a legacy config, not the setup path).

### Changed
- `bootstrap-config`'s `required_fields`/`next_action` output no longer implies runners are ready; it points at the new setup step first.

## [5.3.0] - 2026-07-09

Role runners gain two **host execution modes** alongside `bash`, so a role can run in the host session instead of shelling out to a CLI — chosen per role, kept LLM-free (no host agent-definition files).

### Added
- **`config.runners.<role>` execution modes** — besides `{type:"bash", command, …}`, a runner may now be `{type:"main-session", normalizer?}` (the host LLM performs the role itself, after compacting the conversation — works on any host) or `{type:"subagent", model?, normalizer?}` (the host spawns a subagent with the driver-assembled prompt injected — no `.claude/agents` file needed, so it stays provider-neutral; `model` is selectable). A role's runner array must be homogeneous (cross-mode fallback is a future feature).
- **`driver run-stage` handoff** — for a `main-session`/`subagent` runner the driver assembles + persists the prompt (as always) but cannot execute it (a subprocess can't call the host's Task tool), so it emits `{mode, system_file, user_file, output_file, model?, compact_first?}` and the SKILL executes it per the new **`SKILL.md §Role Execution`** section (dispatch a subagent, or compact-then-run in the main session; STOP if a subagent runner lands on a host with no Task tool). The `Task` tool is re-added to the SKILL's `allowed-tools` (ignored by hosts that lack it).
- **`driver finalize-stage --run --role [--stage-input]`** — normalizes (strips fences), schema-validates, and persists the canonical JSON for a result the SKILL got from a main-session/subagent runner — the exact post-processing a bash JSON role gets inside `run-stage` (shared `_finalize_json`), so results validate identically regardless of who produced them. File roles are a no-op (their result is the git delta).
- **`validate-config` rules** — accepts the new types; rejects a bash runner without `command`, a main-session/subagent runner *with* one, and a heterogeneous runner array — each with a precise message. The pre-3.0.0 `claude-subagent` type is still rejected (not confused with the new `subagent`, which carries `model`, not `agent`).

### Changed
- `run-stage`'s no-command guard now allows main-session/subagent runners (they have no command by design) while still rejecting an unsupported/legacy type. The bash JSON validation path was factored into `_finalize_json` and shared with `finalize-stage`.

### Security
- `subagent`/`main-session` runners have **no hard tool envelope** (LLM-free means no host agent-definition files); the executor runs with the host's tools, contained only by the role prose (role prompts were reworded to be tool-envelope-agnostic — e.g. "do not run anything even if a Bash tool is available"). A `subagent`/`main-session` **reviewer/tester** reviews untrusted code with write access — for a read-only role prefer a `bash` runner with a scoped `--allowedTools`. Also: don't run `main-session` for the reviewer when the implementor is also `main-session` (the gate becomes self-review). The driver now **stamps the review-result `source`** with the true execution mode (`bash-runner` / `host-subagent` / `main-session`) instead of the role self-reporting a fixed `bash-runner`, so an audit reflects how a review was actually run (schema enum extended). `bash` remains the default and the portable/sandboxable option; the new modes are opt-in and host-coupled. Documented in `AGENTS.md`, `README.md`, and `SKILL.md §Role Execution`.

## [5.2.0] - 2026-07-07

Stronger conversational planner: plans are contract-detailed but implementation-agnostic, and the required config-header values are decided **with the user**.

### Added
- **`dp-planner.md` guidance** — new Global Rules ("right-size to one increment", "specify WHAT, delegate HOW / every added detail must be a testable AC or a real constraint"); Step 1 now captures **concrete reuse targets** (`file:symbol`) and the new-file directory, and derives build/install/test commands **by reading the project's build files** (choosing a test command that runs the new tests *with* the existing suite, so regressions aren't missed); Interface calls for data shapes/error modes; a new optional **`## File Layout`** section (kept consistent with `test_paths`); `## Constraints / Notes` carries explicit `Reuse:` pointers; Acceptance-Criteria guidance now requires one-behavior, concrete, deterministic criteria including edge/error cases.

### Changed
- **Required config-header values are confirmed with the user during planning.** The planner presents the derived `tester.*` commands and (TDD) `test_implementor.framework_instruction` + `test_paths` with their evidence and has the user confirm/correct them instead of silently guessing. In the `--request` flow this confirmation **is** the human consent the executable/gate keys require, so `states/planning.md` now sets `header_approved = true` from it — the confirmed values merge into the run snapshot **even under `--auto-run`** (a hand-written `--plan` still runs the planner-less path and stays gated by SKILL Step 0). `config.json` is never overwritten — the header always merges into the per-run `config.snapshot.json`. The batched confirmation covers **all** executable/gate keys (`tester.*`, `test_paths`, `review_block_severity`, `tdd_mode`), and `header_approved` is set only when that confirmation actually happened. Removed the old `--auto-run` + placeholder-config planning stop — the planner now confirms and fills those values instead.

## [5.1.0] - 2026-07-06

Default runners are now **all `claude`, pinned to the `sonnet` model**; the shipped default reviewer is claude (codex is no longer the default). Role/orchestrator prose is kept LLM-neutral.

### Changed
- **`config.example.json` (bootstrap template)** — every default `claude` runner (`implementor`, `test_implementor`, `tester`, `reviewer`) now passes `--model sonnet`, pinning the model instead of relying on the CLI default. The **codex reviewer runner was removed** from the default `reviewer` array, which now ships a single claude reviewer. Codex remains fully supported as an opt-in runner (the `codex-cli` normalizer, `codex exec -s read-only`, and `llm.reviewer.scope` are unchanged) — add it back to `config.runners.reviewer` to use it.
- **LLM-neutral prose** — the `done` commit `Co-Authored-By` trailer is no longer hardcoded to `Claude`; it names the orchestrator model actually running the skill. Removed the vestigial `model:`/`tools:` frontmatter from the `dp-*.md` role prompts (stripped at assembly; per the "no LLM name in prose" rule). Generalized "codex then claude"/"codex fallback" wording in `states/*.md`, `SKILL.md`, `dp-reviewer.md`, `README.md`, `install.sh`, and `driver.py --help` to reference `config.runners.reviewer` order instead of naming the default LLMs. `AGENTS.md` security/architecture notes updated to reflect the claude default reviewer. The `done` state's "Update CLAUDE.md" step now targets the project's agent memory doc generically (`AGENTS.md`, the open standard; `CLAUDE.md` is just one host's variant/symlink).

### Fixed
- **Review diff omitted brand-new files** — `states/review.md` built the reviewer's `changes_diff` with `git diff HEAD -- <manifest paths>`, which silently drops **untracked** files. For a change consisting of newly-created files (e.g. the first file on a branch), the reviewer received an empty/partial diff and the `dp-reviewer.md` empty-diff guard could raise a spurious blocking finding — observed causing an extra review→implementation round-trip in an end-to-end run. The diff step now marks manifest files intent-to-add (`git add -N`) before diffing and resets afterward, so new files appear as `new file` hunks while the working tree is left unchanged. The reviewer's empty-diff guard is unchanged (a genuinely empty diff still means "nothing to review").
- **Runner timeout orphaned the LLM CLI** — `run-stage` ran each runner via `subprocess.run(shell=True, timeout=…)`, which on timeout SIGKILLs only the direct child shell, leaving the LLM CLI it spawned orphaned (reparented to PID 1) and still running (wasting compute and able to write its output file late). `_run_one` now starts the runner in its own session (`start_new_session=True`) and SIGKILLs the whole **process group** on timeout — or when the driver itself is interrupted. (Limits: a grandchild that calls `setsid()` itself escapes the group, and a `SIGKILL` of the driver leaves nothing to clean up.)

### Migration
- No action needed for existing installs — their `.dev-pipeline/dev-pipeline.config.json` is unchanged. New runs bootstrapped after upgrading get the sonnet-pinned, claude-only reviewer default. To keep a codex reviewer, add its runner to `config.runners.reviewer`. To use a different claude model, change `--model` in the runner commands.

## [5.0.0] - 2026-07-05

Conversational **planner** front-end; `plan.md` becomes the single contract (config header + spec body); `spec.md` / the `spec_author` role are removed.

### Added
- **`dp-planner.md` + `states/planning.md`** — a conversational planner that runs in the **host session** (not a headless runner). `/dev-pipeline --request "<goal>"` refines the goal, explores the repo read-only, asks the user when ambiguous, decides TDD/no-TDD, and writes one `plan.md`, then runs the pipeline. `--auto-run` skips the post-plan approval gate (planning-phase questions still happen).
- **`plan.md` config header** — a leading fenced `dev-pipeline-config` JSON block. `init` parses it and merges a **trust-tiered whitelist** into the run's `config.snapshot.json` (never `config.json`): prose keys (`design_instruction`, `focus`, `framework_instruction`, `reviewer.scope`) always; executable/gate keys (`tester.*` commands, `test_paths`, `review_block_severity`, `tdd_mode`) only with human approval (`init --header-approved`, set by the SKILL on approval) or the durable `driver.allow_unattended_header_merge`. `runners` are **never** merged. Parsing is **fail-closed**: a malformed header is a hard error, never a silent fallback.
- **`driver validate-config --plan <path>`** — validate the config exactly as `init` will (header merged, plan body sections checked).
- **`driver.allow_unattended_header_merge`** (optional config bool) — opt into unattended executable/gate header merges.

### Changed (breaking)
- **Removed `spec.md` and the `spec_author` role.** The header-stripped plan **body is the contract**, written by `init` to `<run_dir>/contract.md` and read by the test author, implementor, and reviewer. `init` validates the required body sections deterministically (`## Requirements`, `## Acceptance Criteria`, and `## Interface` under TDD) with non-empty checks — replacing the LLM `INSUFFICIENT` refusal. Section validation runs **before** any run directory is created.
- **Removed the `--tdd` / `--no-tdd` flags.** `tdd_mode` is sourced solely from `driver.tdd_mode` (which a plan header may set) and frozen into `state.tdd_mode` at init.
- **State key `spec_path` → `contract_path`** (with a `spec_path` fallback so an in-flight pre-5.0.0 run stays inspectable). The `plan_path` echo to downstream roles was dropped — roles read only `contract.md`.
- **`runners.spec_author` removed** from the schema/template; a config still carrying it is rejected with a `migrate-config` hint. The run-stage `"named"` category and `INSUFFICIENT` machinery were removed.
- `install.sh` now installs `dp-planner.md` and `states/planning.md` (9 state files) and no longer `dp-spec-author.md`.

### Migration
- Re-run `bash install.sh <project>`. Old configs carrying `runners.spec_author` fail validation → run `driver migrate-config --config <path>` (drops it). A pre-5.0.0 run in flight should be finished with the driver version that started it. Hand-written plans now need the section headings above; add a `dev-pipeline-config` header (or keep the instructions in `config.json`).

## [4.0.0] - 2026-07-03

Make the install layout **provider-neutral and multi-host**. The role prompts
are no longer Claude Code subagents (since 3.0.0 they are just LLM-agnostic prose
the driver assembles), so they move inside the skill, and the skill installs into
the **open Agent Skills standard** location `.agents/skills/dev-pipeline/` — read
natively by Codex, Gemini CLI, Cursor, Kiro, OpenCode and others. Hosts that
don't read that standard yet get their own entry point. **Breaking** — existing
installs must be reinstalled (`bash install.sh <project-dir>`).

### Changed
- **Source layout**: the `claude/` directory is gone. The skill lives at
  `agents/skills/dev-pipeline/`, and the role prompts move from `claude/agents/`
  into the skill's own `agents/` subdir (`agents/skills/dev-pipeline/agents/dp-*.md`).
- **Install layout**: `install.sh` installs the canonical skill into
  `<project>/.agents/skills/dev-pipeline/` (prompts under `agents/`).
  `driver.role_prompt_path` resolves prompts from `<skill_dir>/agents/`, so every
  installed copy is self-contained.
- **Per-host entry points**:
  - **Claude Code** — a **real copy** at `.claude/skills/dev-pipeline/`. Claude Code
    does not read `.agents/skills/` yet (anthropics/claude-code#31005) and won't
    follow a symlinked skill directory (#14836), so a copy is required, not a symlink.
  - **Cline** — a thin pointer at `.clinerules/workflows/dev-pipeline.md` (slash
    `/dev-pipeline.md`) that reads and follows `.agents/skills/dev-pipeline/SKILL.md`.
  - **Codex / Gemini / Cursor / …** — no wiring needed; they discover
    `.agents/skills/` directly.
- **Committing the install**: stage `.agents/skills/dev-pipeline/`,
  `.claude/skills/dev-pipeline/`, and `.clinerules/workflows/dev-pipeline.md`.
  `.agents/` is the single source of truth; the self-evolution commit in `done.md`
  edits `.agents/` and mirrors the change into the `.claude/` copy.

### Fixed
- Upgrading over a pre-4.0.0 install: `install.sh` replaces the old real
  `.claude/skills/dev-pipeline` directory (or a 4.0.0-dev symlink) with the real
  copy and removes the stale `.claude/agents/dp-*.md` prompts.
- The driver now warns on stderr when a role's prose file is missing instead of
  silently running with a stub system prompt.

## [3.0.0] - 2026-06-30

Run every LLM role through a single **bash runner** so the pipeline is
host-agnostic: any LLM can drive the loop, and each stage can use a different
LLM. The Claude-Code subagent path is removed. **Breaking** — existing configs
must be migrated.

### Added
- **`driver run-stage --role <role>`**: the driver deterministically assembles a
  role's prompt from its LLM-agnostic `dp-*.md` (frontmatter stripped → system) +
  the persisted `stage-input.json` (inputs), runs the configured bash runner(s)
  front-to-back, and validates by category — file roles (exit 0), JSON roles
  (result written to `{output_file}` → normalizer → schema), named roles
  (spec_author: required sections / `INSUFFICIENT:` marker). One error-fed retry
  before fallback. Normalizers: `passthrough` / `claude-cli` / `codex-cli`.
- **`dp-spec-author`** role + runner: the spec is now authored by a runner, not
  the orchestrator, so the whole creative path is LLM-agnostic.
- **`driver migrate-config`**: converts a pre-3.0.0 config (claude-subagent /
  codex-adversarial-review runners) to the bash defaults (incl. `spec_author`).
- First principle, documented and enforced: **role `.md` files are LLM-agnostic**
  (no model/tool/CLI references); the LLM, flags, and per-role tool envelope live
  only in `config.runners.<role>`. Swapping/adding an LLM is a config-only change.

### Changed / Breaking
- `config.runners.<role>` items are now `{type:"bash", command, normalizer?,
  timeout?}` only; `claude-subagent` and `codex-adversarial-review` are removed
  from the schema. `runners.spec_author` is now required. `validate-config`
  detects the removed types and points at `migrate-config`.
- `config.example.json` ships per-role bash runners with minimal tool envelopes
  (claude `--allowedTools` by role; codex `exec -s read-only` reviewer + claude
  fallback), validated by a real-CLI spike.
- The SKILL and every state file call `driver run-stage` instead of dispatching a
  subagent / assembling prompts inline; `SKILL.md` drops the `Agent` tool. The git
  baseline/boundary/manifest bookkeeping and the `done` commit are unchanged.
- `review-result.source` gains `bash-runner`; the reviewer no longer claims a
  specific backend (it does not know which LLM runs it).

### Migration
- Run `python3 .claude/skills/dev-pipeline/driver.py migrate-config --config
  <project>/.dev-pipeline/dev-pipeline.config.json`, then review the generated
  runner commands. Update the driver and the installed skill **in lockstep**.
- Requires the `claude` and/or `codex` CLI on PATH for the default runners.
- **Security:** default `claude` runners run headless with pre-approved tools and
  no OS sandbox; `plan.md`/`spec.md`/code are untrusted. Run dev-pipeline in a
  sandboxed/throwaway environment and keep each role's `--allowedTools` minimal
  (read-only roles use a stdout-redirect command with no `Write`). See AGENTS.md
  "Security / trust model".

## [2.3.1] - 2026-07-01

### Fixed
- A **skipped** test stage now validates when the tester emits `command: null`.
  The `test-result` schema required `command` to be a string, so a skipped stage
  (which runs no command) failed schema validation — forcing a needless retry.
  `command` is now nullable (like `exit_code`), and `dp-tester` is told to emit
  `command: null` for skipped stages.

## [2.3.0] - 2026-06-30

The implementor build-checks its code before handing off, so compile errors are
caught early instead of bouncing through the tester.

### Changed
- `dp-implementor` now runs the project's `build_instruction` after implementing
  (skipped for "no build step"), fixes compile errors within its turn (soft cap of
  2–3 rebuilds), and only then hands off. It still must not run the separate
  install/test stages — the tester remains the authoritative build/install/test
  gate. `build_instruction` is now echoed on every transition into `implementation`.

### Notes / limitations
- The implementor's build is a best-effort early check, not a replacement for the
  tester's build (which may run in a cleaner/different environment) — it reduces
  bounces from obvious compile errors but adds a second build per iteration.
- **Keep build output gitignored and outside `test_paths`.** Build artifacts the
  implementor produces are part of its git delta: gitignored untracked files are
  excluded automatically (`--exclude-standard`), but a non-gitignored artifact
  under `test_paths` can trip the role-boundary check, and a build that rewrites a
  *tracked* file (e.g. a lockfile) can land that change in the commit. Gitignore
  build output and do not let `test_paths` overlap the build directory.

## [2.2.0] - 2026-06-30

Harden RED-phase failure classification so an unimplemented feature is not
mistaken for an environment failure, and forbid the orchestrator from silently
editing the user's config.

### Added
- **Config guardrail (Global Rule 10).** The orchestrator must never modify
  `.dev-pipeline/dev-pipeline.config.json` itself, nor let an agent do so. If at
  any point it judges the config needs changing (validation failure, a wrong
  tester instruction, an environment halt, a runner change), it must STOP, propose
  the exact change to the user, and let the user apply/confirm it before
  continuing — never edit-and-proceed. Reinforced in `init`/`failed` states and in
  every write-capable agent (`dp-implementor`, `dp-test-implementor`, `dp-tester`).

### Changed
- `dp-tester` now distinguishes a **missing third-party dependency/toolchain**
  (`environment`) from a **missing first-party symbol that is part of the feature
  under test** (`code`). The latter — e.g. `ModuleNotFoundError` for a module the
  spec defines, or a compile error referencing a not-yet-implemented function — is
  the expected RED signal, not an environment problem.
- The `red_test` state now passes the tester an explicit RED-phase context:
  production is intentionally absent, so import/compile/symbol failures pointing at
  the spec's interface must be classified `code`. This prevents a misclassification
  from halting the run (`red_test` treats `environment` as a hard halt). The driver
  state machine is unchanged — `fail`+`code` still confirms RED → implementation,
  and genuine `environment` failures still halt.

## [2.1.0] - 2026-06-30

Commit only what the pipeline produced, and make every state decision flow
through the driver's echoes instead of the config snapshot.

### Added
- **Change manifest** — a new `driver record-changes` subcommand accumulates the
  files each authoring agent actually wrote (the same per-step delta already used
  for the role-boundary check) into `<run_dir>/changed-manifest.txt`, de-duplicated
  and excluding `.dev-pipeline/` artifacts. The `done` commit now stages **only**
  manifest paths (with `git add -A -- <path>` so deletions are committed too)
  instead of `git add -A`, so untracked junk **not produced by the authoring
  agents themselves** (cscope, ctags, and build/test caches — the latter are
  generated in the separate `test` state, after the delta snapshot) no longer
  lands in the commit — no per-run `.gitignore` upkeep required. The `review`
  state's dp-reviewer fallback scopes to the manifest as well.
- `tdd_mode` and every config-derived value a destination state needs
  (`design_instruction`, `test_paths`, the per-role runner arrays,
  `run_self_evolution`) are now echoed by **every** `driver advance`. State files
  read these echoes; reading `config.snapshot.json` for control flow is now
  forbidden (new Global Rule 9). This removes a class of resume/compaction bugs —
  notably recovering `tdd_mode` from `config.snapshot.driver.tdd_mode`, which is
  wrong under a `--tdd`/`--no-tdd` override (the authoritative value is the frozen
  `state.tdd_mode`).

### Changed
- The per-agent delta is now computed `project_root`-relative
  (`git -C <project_root> diff --name-only --relative` + `ls-files --others`) so
  the manifest, boundary check, and commit agree on one path base even when the
  config lives in a repo subdirectory.
- In legacy (`--no-tdd`) runs the `implementation` state now also stages a baseline
  and records the manifest, so non-TDD commits get the same junk filtering.

### Compatibility
- MINOR bump: no driver API breaks. A run started by an older driver (no manifest)
  resumes fine — `done` falls back to the legacy `git add -A` flow and warns. All
  newly echoed values are read with `.get(default)`.
- **Update the driver and the skill in lockstep.** A partial install (new
  `states/*.md` against an old `driver.py`) would call `record-changes` on a driver
  that lacks it; the call fails, no manifest is written, and `done` silently falls
  back to `git add -A` — re-admitting junk. `install.sh` copies both together, so
  this only affects manual partial updates.

## [2.0.0] - 2026-06-29

Test-Driven Development support. The pipeline can now author tests from the spec
and prove they fail (RED) before writing code, then make them pass (GREEN).

### Added
- **TDD flow** (default): `init → test_implementation → red_test → implementation
  → test → review → done`. New states `test_implementation` (a new
  `dp-test-implementor` agent writes tests from the spec) and `red_test` (the
  existing `dp-tester` proves those tests fail; a failing run is the success
  condition). Disable per run with `--no-tdd` or `driver.tdd_mode: false`.
- `driver.tdd_mode` (config, default true) and `--tdd` / `--no-tdd` flags on
  `init` and `validate-config` (precedence: flag > config > default). The
  resolved value is frozen into `state.tdd_mode`; `state.red_phase` tracks the
  one-time RED gate.
- `llm.test_implementor` config (`focus`, `framework_instruction`, `test_paths`)
  and `runners.test_implementor`; `driver.max_test_implementation_iteration`
  (default 2) bounds re-authoring when RED is not confirmed.
- New `check-boundary` subcommand + role guard: the test author may only touch
  `test_paths`, the implementor may never touch them. The driver owns a
  deterministic glob matcher (`**` = any depth, `*` = within a segment).
- Review-failure routing is finding-aware under TDD: a blocking finding in a
  test file routes back to `test_implementation`; production findings route to
  `implementation`.
- `spec.md` gains a `Test Targets / Interface` section under TDD, and the init
  state requires Acceptance Criteria concrete enough to test (it stops and asks
  rather than fabricating tests for a too-vague plan).
- New `dp-test-implementor.md` agent; `TestTDD`, `TestUpgradeSafety`, and
  `TestCheckBoundary` suites in `test_driver.py`.

### Changed
- **BREAKING:** TDD is on by default. Upgrading a 1.x install: the new config
  keys are optional in the schema (code-level defaults), but because `tdd_mode`
  defaults to true a config without `llm.test_implementor` is rejected at
  `validate-config`/`init` — add the `test_implementor` block, or set
  `tdd_mode: false` / run `--no-tdd` to keep the legacy flow. Refresh the
  installed schema by re-running `install.sh`.
- SKILL.md is now a thin orchestrator (Global Rules, Step 0, Run Context,
  state→file index); each state's procedure lives in
  `claude/skills/dev-pipeline/states/<state>.md` and is read on demand. The
  driver echoes the per-step `iter_dir` so each state file is self-contained.
- `dp-reviewer.md` clarifies that read-only means "never *run* tests" — test
  source code is in review scope, and a test that contradicts the spec is a
  legitimate high-severity finding (test style/coverage nitpicks stay ≤ medium).
  This guidance is also in the default `reviewer.focus` so the codex path sees it.
- `dp-implementor.md` must not create/modify `test_paths` files under TDD.
- `install.sh` installs `dp-test-implementor.md` and the `states/` directory.
- All new state/iteration keys are read with `.get(default)`, so a run created
  by an older driver resumes on the legacy path without crashing.

## [1.3.0] - 2026-06-28

### Added
- Done-state retrospective (SKILL Step 5.3) now reports the model running the
  orchestrator (main session) and, for each state, the runner/method that
  actually carried out the work (claude-subagent + agent name, bash command, or
  codex vs dp-reviewer fallback), making a finished run's execution path
  traceable from the retrospective alone.
- The codex reviewer now receives the spec through the focus text (codex has no
  dedicated spec flag), so it can review changes against the spec's Acceptance
  Criteria instead of focus text alone.

### Changed
- `dp-tester.md` / `dp-reviewer.md`: the JSON example in each agent is now the
  single source of truth for output shape; rules no longer re-list keys or tell
  the tester to read `test-result.schema.json`. Field-level constraints and
  enum meanings (per-stage `status`, `severity`) are defined once, reducing the
  risk of emitting placeholder strings like `"pass or fail"`.
- SKILL Step 3 no longer passes a schema path to the tester — the driver still
  enforces the shape via `validate-result`.
- SKILL Step 4.4 reviewer instruction references only the spec (the reviewer
  reads spec.md, never the plan).
- SKILL Step 5.4 spells out exactly which installed files self-evolution may
  edit and commit (`.claude/agents/dp-*.md`, `.claude/skills/dev-pipeline/SKILL.md`).
- `README.md` and `CLAUDE.md` updated to note that the spec is passed to the
  codex reviewer and that self-evolution may also update `SKILL.md`.

### Removed
- `dp-reviewer.md`: dropped the redundant "fallback reviewer" sentence (no
  behavioral effect; already stated in the frontmatter description).

## [1.2.0] - 2026-06-28

### Added
- `driver.py bootstrap-config [--project <dir>]` — seeds
  `.dev-pipeline/dev-pipeline.config.json` from the template when it is absent.
  The driver detects the project root (git top-level, else cwd), creates the
  directory, copies the template, and idempotently adds `.dev-pipeline/` to
  `.gitignore` (in a git repo). Emits a JSON object with
  `status` (`created`/`exists`), `config_path`, and `required_fields`. Existing
  configs are never overwritten.

### Changed
- Config seeding moved from `install.sh` into the skill. On the first
  `/dev-pipeline` run, the SKILL calls `driver bootstrap-config` when no config
  is found, then stops so the user can fill in the tester instructions and
  re-run. This makes the pipeline self-bootstrapping for anyone who obtains the
  repo without knowing about `install.sh`.
- `install.sh` no longer creates `.dev-pipeline/dev-pipeline.config.json`.
  Instead it copies `config.example.json` next to the installed `driver.py`
  (`.claude/skills/dev-pipeline/config.example.json`) so `bootstrap-config` can
  find the template standalone. It still adds `.dev-pipeline/` to `.gitignore`.

## [1.1.1] - 2026-06-27

### Added
- Deterministic test suite for `driver.py` in `agents/dev-pipeline-tools/test/`.
  Drives the driver as a CLI subprocess (as the SKILL does) and verifies state
  transitions, the review gate (severity and verdict modes), schema validation,
  and the auxiliary subcommands — without invoking any LLM agent or codex.
  Standard library only. Run with
  `python3 agents/dev-pipeline-tools/test/test_driver.py`.

## [1.1.0] - 2026-06-27

### Added
- `driver.py --version` (also `-V` / `version`) prints the dev-pipeline version.
- `dev_pipeline_version` is recorded in each run's `state.json` for audit.
- `install.sh` prints the version it installs and how to check it afterward.

### Changed
- The implementor and reviewer agents now receive **file paths** (plan, spec,
  attempts, diff) instead of inlined file contents; agents read large files
  themselves via the Read tool. Small config strings remain inline.
- `dev-pipeline.config.json` moved from the project root into the gitignored
  `.dev-pipeline/` directory, so it no longer clutters the project root or gets
  confused with the project's own source files.
  **Note:** this is a relocation — copies installed from 1.0.0 keep their config
  at the project root. Re-run `install.sh` (or move the config) to adopt 1.1.0.
- The installed `.claude/` machinery is intentionally NOT gitignored (its history
  is tracked, e.g. for self-evolution). `install.sh` instead instructs the user
  to commit the installed agents + skill before running, so they stay out of the
  working-tree review scope.

### Fixed
- Test state: the tester now receives the authoritative schema path and the exact
  allowed keys, and on schema-validation failure the orchestrator re-dispatches to
  the tester instead of running build/install/test itself (which violated the
  delegation rule). `dp-tester.md` forbids inventing fields like `failure_stage`.
- Review scope: documented gitignoring build outputs, and stopped the installed
  dev-pipeline files from being swept into the working-tree review scope.

## [1.0.0] - 2026-06-27

### Added
- Initial release: automated **implement → test → review** loop for Claude Code.
- Deterministic state machine in `driver.py` (Python 3 stdlib only).
- Specialized agents: `dp-implementor`, `dp-tester`, `dp-reviewer`.
- Orchestrator skill `dev-pipeline` (`/dev-pipeline --plan <path>`).
- Codex adversarial-review as primary reviewer with `dp-reviewer` fallback.
- Pluggable runner abstraction (ordered backends with fallback).
- `attempts.md` oscillation prevention; environment-vs-code failure routing.
- `install.sh` copies driver + schemas for standalone operation.
