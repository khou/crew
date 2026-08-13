# crew

A multi-agent, multi-platform orchestrator for [cmux](https://cmux.com).

One coding-agent session is the director. It opens other agent sessions as tabs
in your terminal, gives them work, keeps them moving, and clears up after them.
Workers are ordinary interactive sessions signed in as you, so they spend your
subscription rather than an API key. Any of Claude Code, Codex or Cursor can
direct, and any can be a worker, in any combination.

The director is the only thing you talk to. Workers stay quiet.

## Requirements

| | |
|---|---|
| [cmux](https://cmux.com) | the terminal crew drives |
| `git` 2.17+, `python3` 3.8+, `lsof` | standard library only, nothing to install |
| at least one agent CLI, signed in | `claude`, `codex`, `cursor-agent` |

## Quick start

**1. Install it.**

```sh
git clone https://github.com/khou/crew && cd crew
./bin/crew install
```

**2. Check your agents can actually start.**

```sh
crew doctor
```

Every agent should show a version. If one says `(not found)`, install it or
sign in. Do this once per machine.

**3. Open a session in your project and make it the director.**

Start `claude`, `codex` or `cursor-agent` in the repo you want to work on, and
tell it:

> You are the crew director. Read `~/crew/docs/DIRECTOR.md`. Then <your goal>.

That is the whole interface. Give it a goal in your own words, the way you
would a colleague, and answer it when it comes back to you. It spawns the
workers, watches them, and cleans up.

The first time you use a repo, Claude and Codex each ask once whether they
trust the directory. Run the agent there yourself and accept, then the fleet
runs unattended. See [docs/SETUP.md](docs/SETUP.md).

## Configuration

All optional. `~/.config/crew/crew.json` for your machine, or `.crew.json` at a
repo's root for settings that travel with the project.

| option | default | what it does |
|---|---|---|
| `roles` | planner, worker, reviewer | pairs an agent with a model, effort and permission level |
| `agents` | claude, codex, cursor | how each is launched and how crew knows it started |
| `max_workers` | `4` | how many run at once, `0` for no limit |
| `ready_timeout` | `90` | seconds to wait for a worker to reach its prompt |
| `auto_approve` | `true` | answer workers' routine permission requests instead of relaying them |
| `stall_minutes` | `15` | unchanged screen for this long, while claiming to work, is stalled |
| `screen_idle_seconds` | `20` | for agents whose reported state cannot be trusted |
| `rescue` | none | gitignored paths worth keeping when a worktree is removed |

Define roles your project needs, and they travel with it:

```json
{
  "roles": {
    "art-director":    {"agent": "claude", "model": "opus", "effort": "high",
                        "permission": "plan"},
    "balance-analyst": {"agent": "codex", "effort": "high", "permission": "edit"}
  }
}
```

`permission` is `plan` (read only), `edit` (change files in its own worktree)
or `full`. See [docs/PERMISSIONS.md](docs/PERMISSIONS.md).

## Driving it yourself

You do not have to go through a director:

```sh
crew spawn worker "add the retry path"   # open a worker, wait until it is ready
crew status                              # who is working, idle or stuck
crew wait                                # block until one needs you
crew say <worker> "also cover timeouts"
crew show <worker>                       # read what it is showing
crew stop <worker>                       # close its tab, keeping its work
crew reap --apply                        # remove finished worktrees
crew merge <branch>                      # bring the work in
```

## Where things are

```
bin/crew             the fleet: spawn, status, wait, say, show, approve,
                     stop, merge, reap
bin/crew-reap        worktree cleanup, useful on its own
docs/DIRECTOR.md     give this to a session to make it the director
docs/SETUP.md        first run in a repo, keeping a fleet out of your way
docs/PERMISSIONS.md  what a worker may do unattended, and why it stops
docs/REAP.md         what reap removes, what it never removes, configuration
test/                three suites, one of which drives a real cmux
```

## What it notices without being asked

- A worker that **claims to be working but is not**: its screen unchanged for
  `stall_minutes` while it still reports running.
- A worker that has **run out of plan allowance**, after which crew refuses to
  spawn more onto that agent, since they all share one subscription.
- A worker that **died, or never started**. State comes from the terminal, not
  from the worker reporting in, so a crash still shows up truthfully.
