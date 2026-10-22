# taskwake

**A background-task ledger for AI agents, plus a webhook that wakes the agent that dispatched the task when it finishes.**

Chat-style agents (bots that run in turns) have a problem with long jobs: once they hand a
40-minute `codex exec` or a research script to the background, the turn ends and the agent goes
to sleep. Nobody tells it when the job is done, so it either polls (burning quota) or forgets.

taskwake fixes that with three small ideas:

1. **Ledger.** Every task gets an id like `0012-codex-fix`. State, command, stdout/stderr and
   events are written to disk. The disk is the source of truth.
2. **Wake-up.** When the task ends, taskwake POSTs a tiny JSON payload to *the dispatching
   agent's own* webhook (any HTTP endpoint that triggers your agent: a scheduler, a bot
   platform's webhook trigger, a CI `repository_dispatch` relay...). The agent wakes up, verifies
   the result, reports to the user, and marks the task as reported.
3. **Catch-up.** Notifications can get lost. Whenever the agent wakes up for any reason it runs
   `taskwake pending --owner <me>` and reports anything it has not reported yet.

It is a single file of standard-library Python (3.9+), no dependencies, POSIX only (uses `fcntl`
and process groups; tested on Linux, should work on macOS).

taskwake wraps **any** command: Codex CLI, Claude Code (`claude -p`), research scripts, batch
jobs, plain shell.

## Install

```bash
git clone https://github.com/sdpwh7-ux/taskwake.git
ln -s "$PWD/taskwake/taskwake" ~/.local/bin/taskwake    # or call taskwake.py directly
taskwake --help
```

## Quick start

```bash
# dispatch (backgrounds immediately and prints the task id)
taskwake start --owner research-bot --name demo --title "Smoke test" --sh 'sleep 5; echo hello'

taskwake list --owner research-bot         # running and recent tasks
taskwake status 0001-demo                  # details + recent events
taskwake log 0001-demo -n 50               # tail of output
taskwake cancel 0001-demo                  # SIGTERM the process group, SIGKILL after 10 s
taskwake resume 0001-demo                  # run again in the same task (same command)
taskwake resume 0001-demo --sh 'new cmd'   # continue with a new command
taskwake pending --owner research-bot      # finished but not yet reported
taskwake mark-reported 0001-demo --owner research-bot   # alias: ack
taskwake notify 0001-demo                  # resend the wake-up once
```

Other options: `--cwd DIR`, `--timeout SEC` (default: none), `--stdin FILE|-`, `--fg` (run in
the foreground and return the command's exit code).

Inside the command these variables are available: `$TASKWAKE_ID`, `$TASKWAKE_DIR` (the task
directory, e.g. write your conclusion to `$TASKWAKE_DIR/result.md`), `$TASKWAKE_RUN`,
`$TASKWAKE_OWNER`.

Exit codes: 0 done, 1 failed/refused, 2 usage error, 124 timeout, 130 cancelled.
States: `queued -> running -> done | failed | cancelled | interrupted` (`interrupted` = the runner
process disappeared, e.g. after a reboot; detected automatically on the next command).

### Examples

Codex CLI, with the final answer saved for verification. taskwake records the Codex
`session id` from the output as `codex_session` in `status.json`, so you can resume:

```bash
taskwake start --owner dev-bot --name codex-fix --cwd ~/src/myrepo --title "Fix login bug" \
  --sh 'codex exec --skip-git-repo-check -o "$TASKWAKE_DIR/result.md" "Fix the login bug, run the tests, list changed files"'

S=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["codex_session"])' ~/.local/state/taskwake/tasks/0012-codex-fix/status.json)
taskwake resume 0012-codex-fix --sh "codex exec resume -o \"\$TASKWAKE_DIR/result.md\" $S 'One test still fails, keep going'"
```

Claude Code with the prompt on stdin:

```bash
taskwake start --owner dev-bot --name claude-refactor --stdin prompt.txt -- claude -p
```

## Owners

Every task records who dispatched it (`--owner`, or `$TASKWAKE_OWNER`). Starting without an
owner is refused. The wake-up goes only to that owner's webhook. `pending` and `list` filter by
owner, and `mark-reported` requires `--owner` and refuses other owners' tasks. This keeps several
agents sharing one machine from reporting (or swallowing) each other's work.

Set `TASKWAKE_CHILD_OWNER_VAR=MY_AGENT_NAME` to also export the owner to the child under that
name (handy for per-agent usage accounting).

## Webhook setup

Store the URL and key once per owner. The key is never echoed or printed:

```bash
taskwake webhook-set --owner research-bot                                   # interactive, hidden input
taskwake webhook-set --owner research-bot --url https://... --key-file /path/to/keyfile
printf '%s' "$KEY" | taskwake webhook-set --owner research-bot --url https://... --key-stdin
taskwake webhook-check --owner research-bot   # "configured / not configured" + key length only
taskwake webhook-test  --owner research-bot   # sends {"source":"taskwake","event":"test"}
```

This writes `~/.config/taskwake/<owner>.env` with mode 600 (see `examples/owner.env.example`).
`TASKWAKE_WEBHOOK_URL` / `TASKWAKE_WEBHOOK_KEY` in the environment take precedence.

Without a webhook taskwake still works: tasks are recorded, the event log says
`notify skipped`, and the agent catches up with `pending` next time it wakes.

### What is sent

```
POST <your webhook URL>
Content-Type: application/json
Authorization: Bearer <key>

{"source":"taskwake","id":"0012-codex-fix","owner":"dev-bot","state":"done","exit_code":0,"title":"Fix login bug"}
```

Only the id and state are sent: never the command line or its output. 2xx = success. Failures
are retried after 0, 5, 20, 60, 180 s (`TASKWAKE_RETRY_DELAYS`); 400/401/403/404 are treated as
configuration errors and not retried. If your receiver wants the key in another header as well,
set `TASKWAKE_WEBHOOK_KEY_HEADER=X-Your-Header`. `TASKWAKE_NOTIFY_OFF=1` disables wake-ups.

## Agent instructions

`examples/agent-skill.md` is a ready-to-adapt prompt for the dispatching agent: how to write the
downstream prompt, how to dispatch and end the turn, how to verify and report when woken, and
when to ack.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `TASKWAKE_DIR` | `$XDG_STATE_HOME/taskwake/tasks` or `~/.local/state/taskwake/tasks` | ledger directory |
| `TASKWAKE_CONF_DIR` | `~/.config/taskwake` | per-owner webhook config |
| `TASKWAKE_OWNER` | - | default owner |
| `TASKWAKE_WEBHOOK_URL` / `TASKWAKE_WEBHOOK_KEY` | - | override the per-owner config |
| `TASKWAKE_WEBHOOK_KEY_HEADER` | - | extra header that also carries the key |
| `TASKWAKE_RETRY_DELAYS` | `0,5,20,60,180` | seconds before each notification attempt |
| `TASKWAKE_NOTIFY_OFF` | - | `1` disables notifications |
| `TASKWAKE_CHILD_OWNER_VAR` | - | also export the owner to the child under this name |

Ledger layout per task: `status.json`, `events.jsonl`, `output.log`, `cmd-N.json`,
`input-N.txt` (optional), `result.md` (optional, written by your command).

## Security notes (read before giving this to an agent)

- **Shell access.** taskwake runs whatever command it is given, with the full privileges of the
  calling user. It is a ledger and a process supervisor, **not a sandbox**. Put sandboxing in the
  command itself (container, `codex --sandbox`, a restricted user).
- **Credentials.** The webhook URL and key are read only by taskwake. `TASKWAKE_WEBHOOK_URL` and
  `TASKWAKE_WEBHOOK_KEY` are stripped from the child environment, the key is never printed by
  `webhook-set`/`webhook-check`, and config files are created with mode 600 in a 700 directory.
  Other secrets in your environment *are* inherited by the child; scrub them yourself if needed.
- **Network.** The only outbound request taskwake makes is the POST to the configured webhook,
  carrying task metadata only (id, owner, state, exit code, title). Do not put secrets in titles.
- **Files.** The ledger contains full command lines, stdin and output. Keep `TASKWAKE_DIR`
  private (it inherits your umask) and do not put secrets on the command line.
- **Prompt injection.** Treat the webhook body as an untrusted hint; the agent should re-read the
  ledger. Command output in `output.log` is equally untrusted when an agent reads it back.
- **Owner separation is discipline, not isolation.** Any process running as the same Unix user
  can read and edit every task. Use separate users for real isolation.

## Known limitations

- Rebooting kills running tasks (they show up as `interrupted`). If the runner itself is
  `kill -9`'d, its child may keep running.
- One run per task at a time; `resume` refuses while a run is active.

## Credits and license

Adapted from the task-ledger design of [robbin/grokbot-local-agent](https://github.com/robbin/grokbot-local-agent)
by Robbin Fan (MIT). The upstream license is kept in `LICENSE.upstream-MIT`.
This project is released under the MIT License (`LICENSE`).
