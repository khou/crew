# crew

Coding agents create a git worktree per session and none of them clean up.
`crew-reap` finds the abandoned ones and removes them without losing work.

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

| Needs | Why |
|---|---|
| `python3` 3.8+ | Standard library only, nothing to install |
| `git` 2.17+ | `worktree list --porcelain`, `worktree remove`, `--no-optional-locks` |
| `lsof` | Tells a busy worktree from an abandoned one |

`lsof` is not optional. crew exits with status 2 rather than run without it,
because a liveness check that silently returns nothing would mark every
worktree idle and remove work that is still in progress. The same applies if
`lsof` is present but fails.

macOS ships all three. On Debian or Ubuntu, `apt install lsof`.
