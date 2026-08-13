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

```sh
git clone https://github.com/khou/crew && cd crew
./bin/crew install      # links crew and crew-reap onto your PATH
crew doctor             # can every configured agent actually start
```

## Use

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

Hand a session [docs/DIRECTOR.md](docs/DIRECTOR.md) and it becomes the director.

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

## Roles

A role pairs an agent with a model, an effort level and a permission level, so
your best model plans and reviews while a cheaper one implements:

| role | agent | may change files |
|---|---|---|
| `planner` | your best model, maximum effort | no |
| `worker` | a cheaper model | yes, inside its own worktree |
| `reviewer` | your best model | no |

Nothing about those three is special. Define your own in
`~/.config/crew/crew.json`, or per project in `.crew.json` at the repo root so
they travel with the project:

```json
{
  "roles": {
    "art-director":    {"agent": "claude", "model": "opus", "effort": "high",
                        "permission": "plan"},
    "balance-analyst": {"agent": "codex", "effort": "high", "permission": "edit"}
  }
}
```

A project with its own vocabulary gets its own crew.

## What it notices without being asked

- A worker that **claims to be working but is not**: its screen unchanged for
  `stall_minutes` while it still reports running.
- A worker that has **run out of plan allowance**, after which crew refuses to
  spawn more onto that agent, since they all share one subscription.
- A worker that **died, or never started**. State comes from the terminal, not
  from the worker reporting in, so a crash still shows up truthfully.
