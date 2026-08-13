# Permissions, and why a worker stops

A worker runs as autonomously as its agent allows without a human in the loop.
crew asks each agent for the nearest thing it has to that:

| role level | claude | codex | cursor |
|---|---|---|---|
| `plan` | `--permission-mode plan` | `--sandbox read-only --ask-for-approval never` | `--mode plan` |
| `edit` | `--permission-mode acceptEdits` | `--approve-for-me` | `--auto-review` |
| `full` | `--permission-mode bypassPermissions` | `--dangerously-bypass-approvals-and-sandbox` | `--force` |

At `edit`, a worker changes files freely. It still stops before doing something
its agent will not do unattended, usually running a shell command.

Those routine stops are answered for you. A fleet that relays every "may I run
ls" is worse than no fleet: one real worker on one task stopped seven times.
`crew wait` and `crew status` approve them and tell the worker not to ask
again. Set `"auto_approve": false` if you would rather see each one.

What is never answered automatically is anything in an agent's blocked list: a
login screen or a trust dialog. Those are decisions only you can make, and a
keystroke at a login can start a browser sign-in and clear stored credentials.
They surface as `needsInput`, `crew show` prints the question, and the director
puts it to you.

Recognising a routine prompt needs a pattern for that agent, and only Claude
Code has one today. Codex and Cursor are configured to approve their own safe
actions instead (`--approve-for-me`, `--auto-review`), so they rarely ask.

**Setting your agent's own default does not replace this.** Worth knowing
before you rely on it: with a user-level default of `auto` configured, a worker
launched with no permission flag still asked before every edit, so that default
did not reach the worker. `--permission-mode auto` is accepted on the command
line and also leaves the session in manual mode. Both measured on Claude Code
2.1.229.

To be interrupted less, allow the specific commands you trust in your agent's
own settings, rather than moving a role to `full`. `full` exists, but on Claude
it puts a warning dialog in front of every worker that a human has to answer
once per session, which defeats the point.
