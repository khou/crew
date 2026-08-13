#!/usr/bin/env python3
"""Tests for crew's launch logic.

Spawning needs a live cmux, so these cover the decisions made before anything
is launched: how the command line is assembled, which permission flags a role
gets, and what environment a worker inherits. Those are where a mistake is
silent, and every one of these was a real bug at some point.

Run: python3 test/test_crew.py
"""

import importlib.machinery
import importlib.util
import json
import os
import shutil
import tempfile
import types
import unittest

CREW = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin", "crew")


def crew_module():
    spec = importlib.util.spec_from_loader(
        "crew", importlib.machinery.SourceFileLoader("crew", CREW))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class DeathTest(unittest.TestCase):
    """A worker that died must never look alive.

    The tab drops to a shell when the agent exits, and a shell echoes anything
    typed at it, so nothing on screen distinguishes a dead worker's prompt from
    a live agent's composer. Only a recorded exit does.
    """

    def setUp(self):
        self.crew = crew_module()
        self.tmp = tempfile.mkdtemp(prefix="crew-death-")
        self.crew.EXIT_DIR = self.tmp

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def mark(self, surface, status):
        with open(os.path.join(self.tmp, f"{surface}.exit"), "w") as fh:
            fh.write(str(status))

    def test_a_live_worker_has_no_exit_recorded(self):
        self.assertIsNone(self.crew.exited("s1"))

    def test_an_exit_is_read_back(self):
        self.mark("s1", 3)
        self.assertEqual(self.crew.exited("s1"), 3)

    def test_a_clean_exit_still_counts_as_gone(self):
        # Status 0 is falsy. Treating it as "no exit recorded" would make a
        # worker that quit normally look like it was still running.
        self.mark("s1", 0)
        self.assertEqual(self.crew.exited("s1"), 0)

    def test_a_recorded_exit_beats_a_hook_store_that_says_running(self):
        self.mark("s1", 137)
        sessions = {"s1": {"pid": os.getpid(), "state": "running"}}
        state, _ = self.crew.worker_state({"surface": "s1", "started": 0}, sessions)
        self.assertEqual(state, "gone")

    def test_the_wrapper_records_the_exit_before_dropping_to_a_shell(self):
        marker = self.crew.exit_marker("s1")
        script = self.crew_launch_script("s1")
        self.assertIn(marker, script)
        self.assertLess(script.index(marker), script.index("exec /bin/zsh -i"),
                        "the shell starts before the exit is recorded")

    def test_the_wrapper_avoids_zsh_read_only_names(self):
        # $status is read-only in zsh; assigning to it aborts the whole wrapper
        # and the tab dies with the evidence.
        self.assertNotIn("status=$?", self.crew_launch_script("s1"))

    def crew_launch_script(self, surface):
        captured = {}
        self.crew.cmux = lambda *a, **k: (captured.update(cmd=a), (0, ""))[1]
        self.crew.launch("ws", surface, "/tmp", ["/bin/true"], {})
        return " ".join(captured["cmd"])


class WaitTest(unittest.TestCase):
    def test_a_change_is_printed_before_it_is_marked_seen(self):
        # Interrupting between the two can then only repeat a change, never
        # swallow one. A change marked seen but never shown is gone for good,
        # because wait only ever reports what is new.
        crew = crew_module()
        marked = []
        worker = {"role": "r", "task": "t", "surface": "s"}
        crew.workers = lambda: {"w": worker}
        crew.hook_sessions = lambda: {}
        crew.refresh = lambda all_w, sessions, cfg: {"w": ("idle", worker)}
        crew.put_worker = lambda name, w: marked.append(name)

        printed = []

        def interrupted_log(msg=""):
            printed.append(msg)
            if len(printed) == 1:
                raise KeyboardInterrupt

        crew.log = interrupted_log
        rc = crew.cmd_wait(types.SimpleNamespace(timeout=1, poll=0.01), {})

        self.assertEqual(rc, 130)
        self.assertEqual(marked, [],
                         "the change was marked seen despite never being shown")


class LaunchTest(unittest.TestCase):
    def setUp(self):
        self.crew = crew_module()

    def argv(self, agent, **subs):
        spec = self.crew.AGENTS[agent]
        full = {"model": "", "effort": "", "worktree": "", "cwd": "", "name": ""}
        full.update(subs)
        return self.crew.build_argv(spec, spec["bin"], full)

    def test_empty_value_drops_its_flag(self):
        # A role with no effort must not produce a dangling --effort.
        argv = self.argv("claude", model="opus", worktree="w", name="w")
        self.assertNotIn("--effort", argv)
        self.assertIn("--model", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "opus")

    def test_no_flag_is_left_without_its_value(self):
        for agent in self.crew.AGENTS:
            argv = self.argv(agent)
            for i, tok in enumerate(argv):
                if tok.startswith("--") and i + 1 < len(argv):
                    continue
            pairs = [a for a in argv if a.startswith("-")]
            self.assertEqual(len(pairs), len(set(pairs)),
                             f"{agent}: a flag was emitted twice")

    def test_placeholders_are_all_substituted(self):
        for agent in self.crew.AGENTS:
            argv = self.argv(agent, model="m", effort="e", worktree="w",
                             cwd="/tmp/x", name="n")
            leftover = [a for a in argv if "{" in a or "}" in a]
            self.assertEqual(leftover, [], f"{agent}: unsubstituted placeholder")

    def test_no_agent_claims_to_grant_its_own_directory_trust(self):
        # Measured: codex shows its trust prompt in an unseen directory even
        # when passed -c projects."<dir>".trust_level="trusted". Carrying a
        # flag that does not work is worse than not carrying one, because it
        # reads as handled.
        for agent in self.crew.AGENTS:
            argv = self.argv(agent, model="m", effort="e", worktree="w",
                             cwd="/tmp/wt", name="n")
            self.assertNotIn("trust_level", " ".join(argv),
                             f"{agent} carries a trust grant that does not work")

    def test_every_agent_defines_all_three_permission_levels(self):
        for agent, spec in self.crew.AGENTS.items():
            self.assertEqual(set(spec["permission"]), {"plan", "edit", "full"},
                             f"{agent} is missing a permission level")

    def test_permission_levels_are_distinct_per_agent(self):
        for agent, spec in self.crew.AGENTS.items():
            flags = [tuple(v) for v in spec["permission"].values()]
            self.assertEqual(len(flags), len(set(flags)),
                             f"{agent} maps two levels to the same flags")

    def test_default_roles_reference_real_agents_and_levels(self):
        for role, r in self.crew.DEFAULT_ROLES.items():
            spec = self.crew.AGENTS.get(r["agent"])
            self.assertIsNotNone(spec, f"role {role} names an unknown agent")
            self.assertIn(r["permission"], spec["permission"],
                          f"role {role} wants a level {r['agent']} lacks")

    def test_reviewers_and_planners_cannot_write(self):
        for role in ("planner", "reviewer"):
            self.assertEqual(self.crew.DEFAULT_ROLES[role]["permission"], "plan",
                             f"{role} should not be able to change files")

    def test_inherited_path_drops_other_panes_wrappers(self):
        # A wrapper directory belongs to one pane. Passing the director's to a
        # worker costs that worker its hooks, which is how it goes missing
        # from crew status.
        os.environ["PATH"] = ("/tmp/cmux-cli-shims/AAAA-BBBB:/opt/homebrew/bin:"
                              "/usr/bin:/bin")
        got = self.crew.inherited_path()
        self.assertNotIn("cmux-cli-shims", got)
        self.assertIn("/opt/homebrew/bin", got)

    def test_cursor_worker_does_not_inherit_an_api_key(self):
        # An API key in the environment overrides the signed-in account and
        # moves the work onto usage billing instead of the subscription.
        self.assertIn("CURSOR_API_KEY",
                      self.crew.AGENTS["cursor"].get("env_unset", []))

    def test_config_overrides_a_role_without_losing_the_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "crew.json")
            with open(path, "w") as fh:
                json.dump({"roles": {"worker": {"model": "haiku"}}}, fh)
            os.environ["CREW_CONFIG"] = path
            cfg = crew_module().config()
            self.assertEqual(cfg["roles"]["worker"]["model"], "haiku")
            self.assertEqual(cfg["roles"]["worker"]["agent"], "claude",
                             "overriding one field dropped the rest of the role")
            self.assertIn("planner", cfg["roles"])
            del os.environ["CREW_CONFIG"]

    def test_shell_quoting_survives_a_task_with_quotes(self):
        q = self.crew.shell_quote("don't; rm -rf /")
        self.assertEqual(q, """'don'\\''t; rm -rf /'""")


if __name__ == "__main__":
    unittest.main(verbosity=2)
