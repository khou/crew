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

### 1. Wire up the two hooks. Nothing else is outstanding.

Both were built and tested but neither is switched on, because switching them
on means editing files crew does not own.

**`crew notify-hook`** stops a worker's alerts reaching the person. cmux raises
its own alert when an agent stops for permission, separate from the agent's own
(which crew already turns off, since `8ab0848` declared `quiet_args` and
nothing ever passed them). That alert is aimed at whoever can unblock the
agent, which for a worker is the director, and the director answers routine
requests itself. The global setting that disables it would silence the person's
own sessions too, so this filters instead. In `~/.config/cmux/cmux.json`:

```json
{ "notifications": { "hooks": [
    { "id": "crew", "command": "crew notify-hook" } ] } }
```

then `cmux reload-config`. Back up `cmux.json` to a timestamped `.bak` first,
as the cmux docs require. Verified against real captured payloads: a surface
crew opened comes back with `desktop`, `sound`, `paneFlash` and
`reorderWorkspace` off and `markUnread`/`record` intact; a surface crew does
not own comes back byte-identical.

**`crew hook`** stops a director walking away from its fleet. Point the agent's
Stop hook at it. It asks for the turn to continue while any worker is idle,
needs input, has stalled or is gone.

Open design question for the person, not for you to decide alone: `crew
install` currently only *prints* these, on the stated principle that crew does
not edit a config it does not own. They asked for the quiet behaviour to be
crew's default, which pulls the other way.

### 2. Known limits, none of them defects

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
