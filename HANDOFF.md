# Handoff: crew, after an adversarial pass

You are picking up `crew` (~/github/crew, public at https://github.com/khou/crew).
crew is a multi-agent, multi-platform orchestrator for cmux: one coding-agent
session directs others as tabs.

You are the **director**. Read `docs/DIRECTOR.md` and work the loop.

All three suites are green: `test_crew.py` 93, `test_reap.py` 37,
`test_live.py` 43. Nothing is uncommitted and no branch is unmerged.

Eighteen defects were found and fixed in the last session, each with a test
that failed first and was then mutation-checked by breaking the fix and
watching the test go red again. Most were found by running crew on itself
rather than by reading it: the worst of them, a live agent's worktree being
deleted underneath it, happened during the session and was traced from there.

## The job

### 1. Make `crew install` an agent session, not a shell command

The owner's idea, and it is a good one. Today `crew install` puts two files on
PATH and *prints* advice about settings, on the principle that crew never edits
a config it does not own. The result is that printed advice does not get
applied: `docs/SETUP.md` promised workers' notifications were off while the
code never passed the flags, for a whole feature's lifetime, and nobody caught
it.

Instead, make installing crew something an agent does. crew already has the
pattern: `docs/DIRECTOR.md` opens with "Point any coding-agent session at this
file and it becomes the director". A `docs/INSTALL.md` written the same way
turns a session into an installer that walks the person through each choice,
explains it, backs the file up, and applies it with consent. The real branches
are: which agents they have, whether the notification hook goes in, trust per
repo, and what roles their codebase needs.

Keep `crew install` working headless. Losing the one-liner costs the second
machine and the scripted setup; the guided path should be what the README
leads with, not the only way in.

### 2. Notifications: understood, deliberately not solved

Workers still pull the person's workspace selection when one asks permission.
The full picture, so nobody re-derives it:

- The agent's own notifications are off. `quiet_args` was declared in
  `8ab0848` and never passed to a launch until this session.
- cmux raises a **separate** alert, `notifications.agentPermissionPrompt`,
  default on. That is the one still firing.
- cmux has no per-surface or per-workspace notification control. Every setting
  under `notifications` is global, `cmux new-surface` has no notification flag,
  and `cmux surface` only stores resume metadata.
- So the only thing that can tell crew's surfaces from the person's is
  `notifications.hooks`. `crew notify-hook` implements exactly that and is
  verified against real captured payloads, but it is **not wired up**, because
  the owner does not want crew depending on hooks.

The shape that would actually fit is a per-surface notification setting in
cmux, passed at `new-surface` time, so crew marks the tabs it opens and it
works for Claude, Codex and Cursor alike. That is a change to cmux, not crew.
Until then this stays open, knowingly.

Do not "fix" it by turning `agentPermissionPrompt` off globally. That silences
the person's own sessions, which they use.

### 3. Known limits, none of them defects

- Plan-limit detection has **still never met a real exhaustion event**. The
  phrases are a reconstruction. A match is a strong hint; a non-match tells you
  nothing.
- `crew stop` moves the person's workspace selection when it closes a tab, via
  `cmux close-surface`. cmux appears to offer no flag for it. Confirm that
  before attempting anything, and do not paper over it by restoring the
  selection: that was tried, was worse, and was removed.
- Rescue paths in a repo's `.crew.json` containing `..` are contained but do
  not work end to end; `rescue_missing` mishandles them independently of the
  containment check. Nobody has asked for them to work.

## How to work

```sh
python3 test/test_crew.py     # 84 tests, no cmux needed
python3 test/test_reap.py     # 36 tests, real git repos in a temp dir
python3 test/test_live.py     # 43 tests, drives a real cmux
```

`test_live.py` now opens a workspace of its own and puts every tab in it, so it
is safe to run at any time and cleans up after itself. It needs no wrapper.

Every fix needs a test that fails without it. Check that by breaking the fix on
purpose and watching the test fail, then restore it. This has repeatedly caught
tests that could not fail.

## Things already learned, do not rediscover

- **Never move the person's focus.** Not to a worker, and not back either: they
  may have gone to look at one deliberately. An earlier "restore the director's
  view" attempt was actively worse and was removed. `crew stop` still moves the
  selection via `close-surface`, and cmux appears to offer no flag for it.
  Confirm that before attempting anything, and do not paper over it.
- **crew reap is scoped** to the repo you are in plus every repo crew has a
  worker in. Never widen it. One director ran it unscoped and reaped 18
  worktrees across four unrelated projects.
- **A worker is not ready because its screen says so.** Readiness is a settled
  screen, because wording changes with permission mode and between releases.
- **Plan-limit detection matches whole phrases only**, and masks crew's own
  text (worker name, worktree path, branch, role, task, tab title) before
  scanning. It used to be a bare word scan, which read a worker named after one
  of those words as an exhausted account on a healthy welcome screen, and crew
  then refused to spawn anything else onto that agent.
- **It has still never met a real exhaustion event.** The phrases are a
  reconstruction. A match is a strong hint; a non-match tells you nothing.
- **A dead worker's tab is a shell**, and a shell echoes what you type, so the
  exit is recorded to a file and checked before anything is delivered.
- **Routine permission prompts are answered automatically**; logins and trust
  dialogs never are, by crew or by you.
- Finish order that works: `crew stop`, `crew reap --apply`, `crew merge
  <branch>`. Workers leave work uncommitted and reap is what commits it. After
  a stop, helper processes hold the worktree for up to two minutes, so reap may
  skip it once.
- **Check a worker's branch base before merging.** Workers branch from the HEAD
  they were spawned at, so a long session leaves them behind trunk. Use
  `git diff main...<branch>` (three dots) to see what a branch actually
  contributes; the two-dot diff makes a stale branch look like a huge deletion.
- **Give a worker one task and expect to nudge it.** The ones that produced
  good work got a precise brief, a reproduction, and at least one follow-up
  telling them to finish. The ones that produced nothing were spawned into a
  session already fighting permission prompts.

## The rule that matters most

The person talks to you and to nobody else. Workers stay quiet. When one needs
a decision, read its screen with `crew show` and put the question to the person
in your own words. Never send them to a worker's tab.
