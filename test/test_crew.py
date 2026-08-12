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
import tempfile
import unittest

CREW = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin", "crew")


def crew_module():
    spec = importlib.util.spec_from_loader(
        "crew", importlib.machinery.SourceFileLoader("crew", CREW))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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

    def test_codex_is_told_to_trust_the_directory_crew_made(self):
        # crew creates codex's worktree, and codex refuses to run in a
        # directory it does not trust. The grant is per invocation so the
        # user's config is never edited.
        argv = self.argv("codex", model="m", effort="low", cwd="/tmp/wt")
        self.assertIn('projects."/tmp/wt".trust_level="trusted"', argv)

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
