# crew

Run a fleet of coding-agent sessions as tabs in one terminal window, driven by
another coding-agent session.

One session is the director. It spawns workers, gives them work, watches their
state, and cleans up after them. Workers are ordinary interactive sessions
signed in as you, so they spend your subscription rather than an API key. Any
of Claude Code, Codex, or Cursor can be the director, and any can be a worker,
in any combination.

```sh
crew doctor                            # can every agent actually start
crew spawn worker "add the retry path" # open a worker tab, wait until ready
crew status                            # who is working, idle, or stuck
crew wait                              # block until one needs you
crew say worker-add-the "also cover timeouts"
crew show worker-add-the               # read what it is asking
crew merge worker-add-the              # bring its branch into the repo
crew stop worker-add-the               # close its tab, keeping its work
crew reap --apply                      # remove finished worktrees
```

Requires [cmux](https://cmux.com). See [docs/DIRECTOR.md](docs/DIRECTOR.md) for
the instructions to hand a session so it acts as the director.

## Why a director helps

Roles pair an agent with a model, an effort level, and a permission level, so
the expensive model plans and reviews while a cheaper one implements:

| role | agent | permission |
|---|---|---|
| `planner` | your best model, maximum effort | read only |
| `worker` | a cheaper model | edits inside its own worktree |
| `reviewer` | your best model | read only |

Change any of it in `~/.config/crew/crew.json`.

## What it notices without being asked

- A worker that **claims to be working but is not**. Its screen going unchanged
  for `stall_minutes` (default 15) while it still reports `running` marks it
  `stalled`. Spinners, clocks and elapsed-time counters are stripped before
  comparing, so thinking does not read as stuck.
- A worker that has **run out of plan allowance**. crew then refuses to spawn
  more workers on that agent, because they all share one subscription and
  starting three more spends nothing while hiding the problem. Honestly: these
  patterns have never been matched against a real exhaustion event, so a match
  is a strong hint and a non-match tells you nothing. What has been checked is
  the opposite direction, that they do not fire on healthy sessions, including
  one whose status line read `api 0% left`, which does not mean exhausted.
- A worker that **died, or never started**. State comes from the terminal, not
  from the worker reporting in, so a crash still shows up truthfully.

## First run in a repo

Both Claude Code and Codex refuse to work in a directory they have not seen
before, and both ask interactively. crew will not answer that for you: it
reports the worker as blocked, types nothing, and leaves the tab open.

So the first time you use crew in a repo, run that agent there once yourself
and accept its prompt. After that every worktree crew creates under it is
covered, because trust is inherited from a trusted parent. Cursor needs
nothing, `--trust` handles it.

This is a one-off per repo, per agent. It is not something crew can do for
you, and an agent that grants itself trust would defeat the point of the
prompt.

## Permissions, and why a worker stops

A worker runs as autonomously as its agent allows without a human in the loop.
crew asks each agent for the nearest thing it has to that:

| role level | claude | codex | cursor |
|---|---|---|---|
| `plan` | `--permission-mode plan` | `--sandbox read-only --ask-for-approval never` | `--mode plan` |
| `edit` | `--permission-mode acceptEdits` | `--approve-for-me` | `--auto-review` |
| `full` | `--permission-mode bypassPermissions` | `--dangerously-bypass-approvals-and-sandbox` | `--force` |

At `edit`, a worker changes files freely. It still stops before doing something
its agent will not do unattended, usually running a shell command. That stop is
not a failure. It shows up as `needsInput`, `crew show` prints the question, and
the director puts it to you. crew never answers a permission prompt for you.

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

## crew-reap

Coding agents create a git worktree per session and none of them clean up.
`crew-reap` finds the abandoned ones and removes them without losing work. It
is useful on its own, whether or not you ever run a fleet.

It works with any agent that uses `git worktree`, because it asks git rather
than looking in agent-specific directories. Verified against Claude Code
(`.claude/worktrees/`), Codex (`.git/codex-worktrees/`, `~/.codex/worktrees/`)
and Cursor (`~/.cursor/worktrees/`).

## Use

```sh
crew-reap                 # dry run across your repos, changes nothing
crew-reap --apply         # do it
crew-reap --repo ~/src/myproject --apply
```

Dry run is the default. Nothing is removed until you pass `--apply`.

## What it does to each worktree

1. Skips it if anything is using it: a live process with its working directory
   inside, a live session in a tool's session registry, a git lock naming a
   live pid, or git metadata touched in the last hour.
2. Rescues configured gitignored files that the primary checkout does not have.
3. Commits anything uncommitted to that worktree's branch, so nothing is lost.
4. Removes the directory. Never with `--force`.

The branch always survives. Removing a worktree only reclaims the working copy;
your commits stay in the repository.

It refuses rather than guesses:

- A file over 10 MB, or over 50 MB in total, is left alone. Committing large
  files to a branch nobody deletes pins them past any future `git gc`.
- Leftovers are never committed onto `main`, `master`, `develop`, `trunk`, or
  `release`. They go to `crew-rescued/<worktree>` instead.
- Work on a detached HEAD is committed and saved to `crew-rescued/<worktree>`,
  which would otherwise be unreachable once the worktree is gone.

## Rescuing files git does not track

Removing a worktree deletes gitignored files with it. Usually that is the
point, since it is how you reclaim a 5 GB build directory. But some ignored
output is expensive: generated art, model weights, anything you paid for.

Name those paths in `.crew.json` at the root of the repo:

```json
{ "rescue": ["art/generated", "models/checkpoints"] }
```

Before removing a worktree, files under those paths that the primary checkout
does not already have are copied to it. Existing files are never overwritten,
and the comparison is per file, so a worktree that added two images to a
directory the primary already has keeps those two images.

## Configuration

Everything is optional. `~/.config/crew/reap.json`:

```json
{
  "roots": ["~/src"],
  "idle_minutes": 60,
  "max_file_mb": 10,
  "max_total_mb": 50,
  "protected_branches": ["main", "master", "develop", "trunk", "release"]
}
```

Without `roots`, crew scans the parent directory of the repo you are in, or
falls back to the usual places (`~/src`, `~/code`, `~/dev`, `~/projects`,
`~/repos`, `~/git`, `~/github`, `~/work`).

### Session registries

A live process is usually enough to tell that a worktree is busy. The exception
is a session that has been paused and can be resumed later: the process is
gone, so nothing else would notice, and removing the worktree breaks the
resume.

Tools that support this write a JSON file recording each session. Point crew at
yours:

```json
{ "session_registries": ["~/.some-tool/sessions/*.json"] }
```

crew looks for any object carrying both a working directory (`cwd`,
`workingDirectory`, `working_directory`, `directory` or `path`) and a process
id (`pid`, `processId` or `process_id`), so it works with tools it was never
written against. Entries whose process is gone are ignored, so a stale registry
never blocks cleanup forever.

## Running it on a schedule

It is safe to run repeatedly. Every step is idempotent and it picks up from
whatever state it finds, including a worktree left half-removed by an earlier
crash.

```sh
0 * * * * /path/to/crew/bin/crew-reap --apply --quiet
```

## Requirements

| Needs | For | Why |
|---|---|---|
| `python3` 3.8+ | both | Standard library only, nothing to install |
| `git` 2.17+ | both | `worktree list --porcelain`, `worktree remove`, `--no-optional-locks` |
| `lsof` | `crew-reap` | Tells a busy worktree from an abandoned one |
| `cmux` 0.64.22+ | `crew` | Tabs, titles, reading a session's screen, agent hooks |

Plus at least one agent, each of which must already be signed in. crew never
handles credentials; a worker that lands on a login screen fails loudly and is
never typed at.

| Agent | Verified | Notes |
|---|---|---|
| Claude Code | 2.1.228 | Makes its own worktree |
| Codex | 0.147.0 | Older versions cannot read a config written by the current desktop app |
| Cursor | 2026.08.11 | Makes its own worktree |

Run `cmux hooks setup` once so cmux can report each session's state. Without it
`crew status` cannot see anything.

`lsof` is not optional. crew exits with status 2 rather than run without it,
because a liveness check that silently returns nothing would mark every
worktree idle and remove work that is still in progress. The same applies if
`lsof` is present but fails.

macOS ships all three. On Debian or Ubuntu, `apt install lsof`.
