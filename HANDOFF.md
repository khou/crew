# Handoff: crew, after an adversarial pass

You are picking up `crew` (~/github/crew, public at https://github.com/khou/crew).
crew is a multi-agent, multi-platform orchestrator for cmux: one coding-agent
session directs others as tabs.

You are the **director**. Read `docs/DIRECTOR.md` and work the loop.

All three suites are green: `test_crew.py` 84, `test_reap.py` 36,
`test_live.py` 43. Nothing is uncommitted and no branch is unmerged.

Fifteen defects were found and fixed in the last session, each with a test that
failed first and was then mutation-checked by breaking the fix and watching the
test go red again. Most were found by running crew on itself rather than by
reading it.

## The job, in order

### 1. Workers still interrupt the person. This is the top priority.

`docs/DIRECTOR.md` and `docs/SETUP.md` both promise workers' notifications are
off. There are three paths and only two are covered.

- Claude's own notifications: **fixed**. The `quiet_args` on the claude agent
  were declared in `8ab0848` and never passed to a launch, so the setting was
  dead config for its whole life. They are on every launch now.
- cmux's `notifications.agentPermissionPrompt`, default `true`: **not fixed**.
  This is what drags the person's workspace selection onto a worker every time
  one asks permission. crew auto-approves those, so the person is being pulled
  away to look at something crew is about to answer itself.

The fix is a cmux notification hook. `notifications.hooks` in
`~/.config/cmux/cmux.json` takes "a shell command run with notification policy
JSON on stdin" that returns updated policy JSON on stdout. crew should register
one at `crew install` that suppresses notifications only for surfaces listed in
its own state file, leaving every other session's alerts alone.

**The blocker:** the payload shape is undocumented. It is not in the settings
schema, not in `skills/cmux/SKILL.md`, and not in `cmux docs settings`. Capture
a real one before writing anything, using a hook that logs stdin and echoes it
back unchanged. Note that a notification only fires when its workspace is
**not** the visible one, so the person has to be looking elsewhere while you
capture. Back up `cmux.json` to a timestamped `.bak` before editing it, as the
cmux docs require, and put it back afterwards.

Do not simply set `agentPermissionPrompt` to false. That silences the alert for
the person's own main agent too, which they do want.

### 2. `crew stop` does not check that the tab actually closed.

Proven live, not theoretical. `crew stop` printed `stopped adv-reap, it was
idle`, dropped the worker record, and neither the tab nor the agent went. A
later `crew reap --apply` then removed that live agent's worktree, and the
agent silently re-rooted onto the primary checkout with edit permissions still
on. The record is the only thing holding the surface id, so dropping it leaves
nothing able to reach the tab.

The existing guard only fires when `close-surface` returns non-zero **and** the
pid is alive. Here it returned success while the surface survived. Success is
not evidence the tab went, so verify it. Beware the opposite failure: a worker
whose tab is already gone must still stop cleanly, and stop must not hang.

A worker was given this and produced nothing before it was stopped.

### 3. reap's liveness check.

`process_cwds` captures lsof's exit code and never reads it, while its own
docstring says a failed check must stop the run or live work gets reaped.

Measured on this machine, do not re-derive: `lsof -d cwd -F n` exits 0 on every
normal run, and a forced-error run exits 1 while still printing a **complete**
listing. So the exit code alone cannot tell complete-with-a-warning from
truncated-because-it-died, and a blanket `rc != 0` would reject good listings.
Candidate worth testing: lsof should always report the calling process's own
cwd, so a listing lacking it is untrustworthy whatever the exit code says.

A reviewer rated this below item 2. If no sound check exists, say so and change
nothing rather than shipping false comfort.

### 4. Workers stop mid-task, every time.

All six workers run in the last session ended their turn early, usually right
after a long stretch of tool calls, and each needed re-prompting to finish. The
work was good once they were nudged. Nothing but the director notices this.

The suggested mechanism is a Claude Code `Stop` hook: it fires when the agent
tries to end its turn, and returning `{"decision": "block", "reason": ...}`
sends it back to work. A hook that blocks while any worker is `idle`,
`needsInput`, `stalled` or `gone` makes losing the loop impossible rather than
something the director is trusted to remember. Two guards it needs: never block
on `running`, or it traps the session forever, and cap consecutive blocks so a
wedged worker cannot pin the director.

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
