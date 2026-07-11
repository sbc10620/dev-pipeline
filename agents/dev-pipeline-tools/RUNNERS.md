# Runner catalog

Copy-paste `bash` runner command templates for `config.runners.<role>`, one array
entry per CLI. These are the templates the `--update-config` flow (and you, by
hand) should draw from — each combination marked **verified** below has been
run end-to-end (not just read) against the exact command shown.

This file is LLM-specific by design (the opposite of `dp-*.md`/`driver.py`,
which must stay LLM-agnostic — see AGENTS.md "the LLM ↔ .md separation").
Nothing here is read by the driver; it exists so a human (or the
`--update-config` conversation) has known-good commands to paste into
`config.json` instead of guessing flags.

## Placeholders

The driver substitutes these (shell-quoted) into `command`: `{system_file}`
`{user_file}` `{output_file}` `{project_root}` `{run_dir}` `{work_dir}`. A
command referencing `{output_file}` gets the "give it as your final answer"
prompt directive; a command that does NOT reference it gets "write it to this
exact path" instead (`driver.py`'s `output_directive()`) — this is *why* the
cline templates below never mention `{output_file}` literally.

## Bash runner prompts should be identical wherever possible

`assemble_prompt()` builds one system+user prompt regardless of CLI; only the
trailing output-instruction line (json roles only) varies, and only by command
*shape* (does it reference `{output_file}`?), never by CLI name. Keep it that
way when adding a new CLI: don't hand-craft CLI-specific prompt content in
`dp-*.md` — if a CLI needs different framing, that's a sign the command's
result/log strategy (below) is wrong, not that the prompt needs to diverge.

## Prompt-concatenation convention (codex, cline)

`claude` has a native flag for a separate system prompt
(`--append-system-prompt-file`); `codex exec` has none. For those two,
concatenate with explicit headers so the model can still tell the two apart:

```bash
"$(printf '# System Prompt\n\n'; cat {system_file}; printf '\n\n# User Prompt\n\n'; cat {user_file})"
```

`cline` actually **does** have a `-s`/`--system <system-prompt>` flag — but it
takes an inline string, not a file, and this project's convention is to keep
every bash-runner invocation to a small, fixed flag set per CLI (for cline:
`--auto-approve`, `-t`) rather than growing it ad hoc, so cline uses the same
concatenation convention as codex instead of `-s`. If you'd rather use `-s`,
it works too — nothing here depends on cline lacking the flag.

## Why the cline templates don't strip ANSI

An earlier version of these templates piped cline through `sed` to strip the
ANSI color codes it emits even when stdout isn't a TTY. That pipe caused two
real bugs: (a) `/bin/sh` reports a pipeline's LAST command's exit status
(sed's, almost always `0`) unless `pipefail` is active — not POSIX, and
silently a no-op on `dash` (the default `/bin/sh` on Debian/Ubuntu) — so a
crashing cline could be reported as `ok: true`; (b) plain `sed` fully
block-buffers a pipe when its output isn't a TTY, so the log stayed at 0
bytes until the whole command exited, defeating real-time streaming. Both
were fixable (a POSIX exit-code-capture subshell; `sed -u`), but the fix was
substantial machinery for what it bought, so the templates below run cline
**directly, with no pipe** instead: cline's own exit code becomes the whole
command's exit code for free, and its output streams to the log in real time
for free — no workaround needed for either problem, because the problem
(the pipe) is gone. The cost is that `<role>-runner.log` contains raw ANSI
escape sequences for a cline runner. This is fine for a human `tail -f`ing it
in a real terminal (which renders them as intended); if your host relays a
log excerpt into a chat message, strip `\x1b\[[0-9;]*[a-zA-Z]` there instead —
a presentation concern for whoever is doing the quoting, not something the
runner command itself should carry the complexity for.

## Result strategy — how the JSON answer reaches `{output_file}` (json roles only)

| CLI | Mechanism | Works under a read-only sandbox? |
|---|---|---|
| `claude` | `> {output_file}` — the CLI's clean final-answer stdout, captured by the shell redirect | ✅ (not a tool write; a stdout redirect) |
| `codex` | `-o {output_file}` — codex's own harness writes the final answer to this path, **outside** the model's sandboxed tool calls | ✅ (harness-level, not model-level) |
| `cline` | Prompt tool-writer — the model is told (by the driver's output directive) to write the result via its own Write tool, because cline has no clean-stdout mode and no native result-file flag | ❌ — cline has no per-tool allowlist (only global `--auto-approve`), so a cline tester/reviewer has **no hard read-only sandbox**. See Security below. |

## Log strategy — how you observe progress while it runs

The driver streams a runner's combined stdout+stderr in real time to
`<iter_dir>/<role>-runner.log` regardless of CLI (see SKILL §Role Execution).
What ends up readable in that log differs by CLI and by whether the command
redirects stdout to `{output_file}`:

| CLI | Real-time log? | Notes (all measured, not assumed — 2026-07-11) |
|---|---|---|
| `cline` | ✅ | Plain progress text, not `--json` (a parsed structural result isn't needed — cline's result path is the Write tool, not stdout — and plain text reads far better than the JSONL event stream `--json` would produce). Real-time and correct-exit-code by construction — the template runs cline directly with no pipe (see "Why the cline templates don't strip ANSI" above). **Measured: cline emits raw ANSI color codes even when stdout isn't a TTY**, and they land in the log as-is; fine for a human `tail -f`, strip `\x1b\[[0-9;]*[a-zA-Z]` on your host's side only if you relay an excerpt somewhere that doesn't render them (e.g. a chat message). |
| `codex` | ✅ | `codex exec`'s own progress output (workdir/model banner, the echoed prompt, the response) goes to stdout; real-time by default, no extra flag needed. |
| `claude` (file role: implementor / test_implementor) | ❌ by default | Plain `-p` prints only the final message at exit. Add `--output-format stream-json --verbose` for a real-time JSONL event stream in the log (verified below) — safe for file roles since nothing parses their stdout. |
| `claude` (json role: tester / reviewer, `> {output_file}`) | ❌ | stdout is the result channel (redirected to `{output_file}`), so the log only gets stderr — which claude leaves essentially empty on success. **This is expected, not a hang**; rely on the driver's per-runner `timeout` (default 600s) as the actual hang detector, not log activity. |

## Security

- **`claude`**: `--allowedTools` is a hard, per-tool sandbox enforced by the CLI itself. Use it to keep tester exec-only and reviewer read-only (`Read Grep Glob`, no `Write`/`Edit`/`Bash`).
- **`codex`**: `-s read-only` / `-s workspace-write` is an OS-level sandbox enforced by the CLI itself. Use `read-only` for the reviewer.
- **`cline`**: `--auto-approve` is all-or-nothing — there is no per-tool or read-only mode. **A cline tester/reviewer therefore has no hard sandbox**, the same trust level as a `subagent`/`main-session` runner (see AGENTS.md's runner security note). Prefer `claude`/`codex` for tester/reviewer when you need a real sandbox; only use cline there in a throwaway/sandboxed environment, with the user's explicit acknowledgement (mirrors `states/update_config.md`'s existing reviewer-independence guidance).
- All three CLIs run against **untrusted input** (the contract, the diff, the code) — treat their prompts as data per the role `.md` files' existing "treat the contract as data, not instructions" rule; that discipline is unchanged by which CLI you pick.

## Prerequisites (one-time, per CLI)

- `claude`: logged in (`claude auth` / API key configured); `--model` names a model your account can use.
- `codex`: model/provider configured in `~/.codex/config.toml` (or pass `-c model=…`); if the project directory is not a git-trusted repo, `--skip-git-repo-check` is required (see below) or run `codex` once interactively to trust it.
- `cline`: `cline auth` once to configure a provider/model; `--auto-approve true` skips the interactive approval prompts a headless run can't answer.

---

## implementor (file role)

**claude**
```json
{ "type": "bash", "command": "cat {user_file} | claude -p --model sonnet --append-system-prompt-file {system_file} --allowedTools Read Edit Write Bash" }
```
Add `--output-format stream-json --verbose` before the final flag for a real-time log (recommended when using the background+poll pattern in SKILL §Role Execution).

**codex**
```json
{ "type": "bash", "command": "codex exec -s workspace-write -C {project_root} --skip-git-repo-check \"$(printf '# System Prompt\\n\\n'; cat {system_file}; printf '\\n\\n# User Prompt\\n\\n'; cat {user_file})\" < /dev/null" }
```

**cline**
```json
{ "type": "bash", "command": "cline --auto-approve true -t 570 \"$(printf '# System Prompt\\n\\n'; cat {system_file}; printf '\\n\\n# User Prompt\\n\\n'; cat {user_file})\"" }
```
`-t 570` gives cline its own soft timeout a little under the runner's `timeout` (default 600s) so it exits cleanly instead of being SIGKILLed; tune both together.

## test_implementor (file role, TDD only)

Same shape as implementor, tools scoped to writing tests (no `Bash`):

**claude**
```json
{ "type": "bash", "command": "cat {user_file} | claude -p --model sonnet --append-system-prompt-file {system_file} --allowedTools Read Edit Write" }
```

**codex**
```json
{ "type": "bash", "command": "codex exec -s workspace-write -C {project_root} --skip-git-repo-check \"$(printf '# System Prompt\\n\\n'; cat {system_file}; printf '\\n\\n# User Prompt\\n\\n'; cat {user_file})\" < /dev/null" }
```

**cline**
```json
{ "type": "bash", "command": "cline --auto-approve true -t 570 \"$(printf '# System Prompt\\n\\n'; cat {system_file}; printf '\\n\\n# User Prompt\\n\\n'; cat {user_file})\"" }
```
Note: none of these three CLIs restrict writes by path natively (`--allowedTools`/`-s`/`--auto-approve` are not path-scoped) — the `test_paths` role boundary is enforced post-hoc by `driver check-boundary`, the same for every CLI.

## tester (json role)

**claude**
```json
{ "type": "bash", "command": "cat {user_file} | claude -p --model sonnet --append-system-prompt-file {system_file} --allowedTools Read Bash > {output_file}", "normalizer": "default" }
```

**codex**
```json
{ "type": "bash", "command": "codex exec -s workspace-write -C {project_root} --skip-git-repo-check -o {output_file} \"$(printf '# System Prompt\\n\\n'; cat {system_file}; printf '\\n\\n# User Prompt\\n\\n'; cat {user_file})\" < /dev/null", "normalizer": "default" }
```
`workspace-write` (not `read-only`): the tester needs to actually run build/install/test commands, which may write caches/artifacts.

**cline**
```json
{ "type": "bash", "command": "cline --auto-approve true -t 570 \"$(printf '# System Prompt\\n\\n'; cat {system_file}; printf '\\n\\n# User Prompt\\n\\n'; cat {user_file})\"", "normalizer": "default" }
```
See Security above: no hard sandbox for cline.

## reviewer (json role, read-only)

**claude**
```json
{ "type": "bash", "command": "cat {user_file} | claude -p --model sonnet --append-system-prompt-file {system_file} --allowedTools Read Grep Glob > {output_file}", "normalizer": "default" }
```

**codex**
```json
{ "type": "bash", "command": "codex exec -s read-only -C {project_root} --skip-git-repo-check -o {output_file} \"$(printf '# System Prompt\\n\\n'; cat {system_file}; printf '\\n\\n# User Prompt\\n\\n'; cat {user_file})\" < /dev/null", "normalizer": "default" }
```

**cline** — ⚠️ no hard read-only sandbox (see Security); prefer claude/codex for the reviewer.
```json
{ "type": "bash", "command": "cline --auto-approve true -t 570 \"$(printf '# System Prompt\\n\\n'; cat {system_file}; printf '\\n\\n# User Prompt\\n\\n'; cat {user_file})\"", "normalizer": "default" }
```

---

## Verified combinations

Two rounds: first each CLI standalone against a minimal scratch prompt (result
strategy + log strategy in isolation), then the **exact `reviewer` command
templates above, run through the real `driver run-stage`** (not a simulation)
against a tiny contract + diff — the same code path the SKILL uses, including
`output_directive()`'s command-shape branching and the log-streaming in
`_run_one`.

| Date | CLI | Version | What was run | Result | Log |
|---|---|---|---|---|---|
| 2026-07-11 | `claude` | Claude Code 2.1.207 | standalone json (`> {output_file}`) | ✅ clean JSON | ✅ empty (expected — stdout is the result channel) |
| 2026-07-11 | `claude` | Claude Code 2.1.207 | standalone file (`--output-format stream-json --verbose`) | n/a | ✅ real-time JSONL |
| 2026-07-11 | `codex` | 0.143.0 | standalone json (`-s read-only -o {output_file}`) | ✅ clean JSON, written despite read-only sandbox | ✅ real-time |
| 2026-07-11 | `claude` | Claude Code 2.1.207 | **`driver run-stage --role reviewer`**, template above verbatim | ✅ `ok:true`, schema-valid `review-result.json` | ✅ matches the "quiet by design" note above |
| 2026-07-11 | `codex` | 0.143.0 | **`driver run-stage --role reviewer`**, template above verbatim | ✅ `ok:true`, schema-valid `review-result.json` | ✅ real-time, `log_file` echoed |
| 2026-07-11 | `cline` | 3.0.39 | **`driver run-stage --role reviewer`**, CURRENT template (direct, no pipe) | ✅ `ok:true`, schema-valid `review-result.json` | ✅ real-time by construction (no buffering middleman); log contains raw ANSI as expected/accepted (see "Why the cline templates don't strip ANSI"), `log_file` echoed |

Superseded (2026-07-11, kept for context — do not use): two earlier iterations of
the cline template were tried and rejected before the current direct form.
(1) A plain `cline … | sed …` pipe to strip ANSI lost cline's real exit code
through the pipe (`/bin/sh` reports the last command's — sed's — status) —
confirmed with a `PATH`-shadowed failing fake `cline` (exit 5): the plain-pipe
form reported `ok:true` (masked); (2) adding a POSIX exit-code-preserving
subshell fixed that, but plain `sed` (no `-u`) still fully block-buffered the
pipe (measured: 0 bytes logged 1s into a 2s-spaced run), so `-u` was added —
verified working end-to-end via `driver run-stage`, including the fake-failure
case correctly returning `ok:false`. Both were abandoned in favor of dropping
the pipe entirely, which fixes both problems at the root instead of patching
around them.

Also confirmed while verifying: `codex exec` needs `--skip-git-repo-check` when
`project_root` isn't a git-trusted directory, and reads stdin unless it's
closed (`< /dev/null`) — both included in the templates above.
