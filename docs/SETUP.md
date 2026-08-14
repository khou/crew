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

cmux raises its own alert too, separately from the agent's, when a worker
stops for permission. That one is aimed at whoever can unblock it, which for a
worker is the director, and the director answers routine requests itself. The
setting that turns it off is global and would silence your own sessions with
it, so crew filters instead:

```json
{ "notifications": { "hooks": [
    { "id": "crew", "command": "crew notify-hook" } ] } }
```

`crew notify-hook` reads the notification cmux is about to deliver and, only
when the surface is one crew opened, turns off the parts that interrupt you:
the desktop alert, the sound, the pane flash and the sidebar reorder. It
leaves the unread badge and the feed entry, so a director can still see a
worker wants it. Every notification crew did not create is passed back exactly
as it arrived, including anything it cannot parse.

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
