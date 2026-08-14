# Installing crew

Point any coding-agent session at this file and it becomes the installer. It
works the same whether that session is Claude Code, Codex or Cursor, because
everything below is shell commands and questions.

Installing crew is mostly decisions, not steps. Which agents you have, what a
worker may do unattended, how much of your terminal a fleet is allowed to
touch. A script would have to guess at those, and crew's own history says
guessing does not work: it printed advice about notification settings for a
whole release while the code never applied them, and nobody noticed. So the
installer asks.

## How to run this

You are talking to the person the whole way. Work through the sections in
order. For each one: say what it does, say what it costs, and ask. Then apply
what they chose, or move on.

**Never change a file without a yes.** Not a config, not a shell profile, not
their agent settings. Say what you would write and where, and wait.

**Back up before editing anything you did not create.** Copy it next to itself
with a timestamped `.bak`, and tell them the path. cmux's own docs require
this for `cmux.json`; do it for everything.

**Leave them somewhere working.** If they decline a step, say what that means
in practice and carry on. Every section below is optional except the first
two. A half-installed crew that they understand beats a complete one they did
not agree to.

**Do not decide for them.** If they ask what you would pick, say, and give the
reason. Then still let them answer.

## 1. Requirements

```sh
git --version && python3 --version && command -v lsof && command -v cmux
```

`git` 2.17+, `python3` 3.8+, `lsof`, and cmux. Nothing else: crew is standard
library only. If cmux is missing, stop here and point them at
https://cmux.com. crew drives cmux and has nothing to do without it.

## 2. Put crew on PATH

```sh
./bin/crew install
```

This is the only step that is not a question. It puts `crew` and `crew-reap`
on PATH and touches nothing else.

If they are already installed, say so and move on. Re-running is harmless.

## 3. Which agents actually work

```sh
crew doctor
```

Every agent should report a version. One that says `(not found)` is either not
installed or not on this PATH, and a worker can never be spawned onto it.

If none of them work, stop. crew has nothing to run.

If some work and some do not, that is fine and common. Tell them which they
have, and that roles can only name agents that work.

## 4. Roles

A role pairs an agent with a model, an effort level and a permission level.
crew ships `planner`, `worker` and `reviewer`. Nothing about those three is
special.

Ask what this codebase actually needs. A project with a design surface wants
different roles from a library. Put what they choose in `~/.config/crew/crew.json`
for every project, or in `<repo>/.crew.json` to travel with this one.

`permission` is `plan` (read only), `edit` (change files in its own worktree)
or `full`. Point them at [PERMISSIONS.md](PERMISSIONS.md) and let them decide
how much a worker may do while nobody is watching.

## 5. Keeping a fleet out of the way

Workers are tabs in the director's own workspace. Two things follow, and both
are the person's call.

**Give the director its own cmux workspace.** Then workers appearing and
disappearing happen somewhere you are not looking. This is a habit, not a
setting; just tell them.

**Stop the sidebar reshuffling.** In `~/.config/cmux/cmux.json`:

```json
{ "app": { "reorderOnNotification": false } }
```

then `cmux reload-config`. Without it, workspaces move toward the top as
notifications arrive, so a working fleet rearranges their sidebar under them.

**Optionally, quiet workers' notifications.** cmux alerts when an agent stops
for permission. For a worker, crew answers those itself, so the alert is about
something already handled, and it pulls them onto the worker's tab.

```json
{ "notifications": { "hooks": [
    { "id": "crew", "command": "crew notify-hook" } ] } }
```

Be honest about the trade here: this makes crew depend on a cmux hook. The
alternative, `notifications.agentPermissionPrompt: false`, is global and would
silence their own sessions too. If they would rather have neither, say that
workers will keep pulling their view, and leave it.

## 6. First run in a repo

Claude and Codex each ask once whether they trust a directory, and crew will
not answer that for them: it reports the worker blocked, types nothing, and
leaves the tab open.

So the first time crew is used in a repo, they run that agent there once
themselves and accept. Every worktree crew makes under it is covered
afterwards, because trust is inherited from a trusted parent. Cursor needs
nothing.

Offer to do this now for the repo they are standing in. You cannot accept the
prompt for them, and an agent that granted itself trust would defeat the
point.

## 7. Prove it works

Do not declare it installed. Show it:

```sh
crew doctor
crew status
```

`doctor` should list their agents with versions; `status` should say
`no workers`. If they want a real end-to-end check, spawn one throwaway worker
and stop it:

```sh
crew spawn worker "say hello and stop" --name smoke --no-task
crew status
crew stop smoke
crew reap --apply
```

Then tell them the one thing that matters next: point a session at
[DIRECTOR.md](DIRECTOR.md) and give it a goal. That is the whole interface.

## What to report at the end

A short list of what changed on their machine, each with its path, and what
they declined. Anyone should be able to undo the lot from that list.
