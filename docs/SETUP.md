# Setting up

## Keeping a fleet out of your way

Workers open as tabs in a workspace of their own, one per director, never as
tabs beside the session you are working in. A tab created in the pane you are
looking at flashes to the front and back again, once per worker. One director
can be running work across several repositories, so the fleet belongs to the
director rather than to a repo.

Two cmux preferences are worth setting. crew prints them at `crew install
--quiet-cmux` and does not write them for you, because a tool that quietly
edits your terminal's configuration is the same class of behaviour this is
turning off:

```json
{ "app": { "reorderOnNotification": false } }
```

then `cmux reload-config`. Without it, workspaces are moved toward the top of
the sidebar as notifications arrive, so a working fleet reshuffles your
sidebar under you.

Workers are also launched with their own agent's notifications turned off, so
what reaches you comes from the director. That is the whole point: the
director is escalated to, and escalates onward, so notifications should come
from it and not from six workers at once.

One gap remains, and it is not fixed. cmux raises its own
`agentPermissionPrompt` notification when a worker stops for permission, and
that selects the worker's workspace, pulling you onto it. crew answers those
requests itself, so the alert is telling you about something already handled.
Turning it off is a single global setting, which would silence the same alert
for your own session, so crew does not suggest it.

## Keeping a director on its fleet

Workers stop mid-task and wait. Only the director notices, and a director that
ends its turn leaves them sitting there. Point your agent's stop hook at:

```sh
crew hook
```

It asks for the turn to continue while any worker is idle, needs input, has
stalled or is gone, naming which, and gets out of the way otherwise. It never
holds on for a worker that is merely working, and it lets a turn through after
a few tries so a worker nothing can fix cannot pin the director indefinitely.

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
