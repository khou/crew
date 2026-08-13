#!/usr/bin/env python3
"""Tests for crew's launch logic.

Spawning needs a live cmux, so these cover the decisions made before anything
is launched: how the command line is assembled, which permission flags a role
gets, and what environment a worker inherits. Those are where a mistake is
silent, and every one of these was a real bug at some point.

Run: python3 test/test_crew.py
"""

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import threading
import time
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

    def test_no_hook_session_alone_does_not_make_an_old_worker_gone(self):
        # Some agents only register a hook session lazily, on their first
        # hook fire. A worker older than 60s with no session yet and no
        # recorded exit is still starting, not dead.
        sessions = {}
        state, _ = self.crew.worker_state(
            {"surface": "s1", "started": time.time() - 3600}, sessions)
        self.assertEqual(state, "starting")

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


class DeliverRobustnessTest(unittest.TestCase):
    def test_an_agent_without_blocked_patterns_does_not_crash_delivery(self):
        # Agents are user-configurable, and the built-in ones all happen to
        # define "blocked". Indexing it directly made delivery raise KeyError
        # for any agent added in config without one.
        crew = crew_module()
        crew.read_screen = lambda ws, surface, lines=40: "an ordinary screen"
        crew.exited = lambda surface: None
        crew.cmux = lambda *a, **k: (0, "")
        ok, why = crew.deliver("ws", "s", "hello", {})
        self.assertFalse(ok)
        self.assertNotIn("KeyError", why)


class WorkerDirTest(unittest.TestCase):
    """Where a worker is working, when the worker is wrong about it."""

    def setUp(self):
        self.crew = crew_module()
        self.tmp = tempfile.mkdtemp(prefix="crew-dir-")
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        for cmd in (["init", "-q", "-b", "main"], ["add", "-A"]):
            subprocess.run(["git", *cmd], cwd=self.repo, capture_output=True)
        with open(os.path.join(self.repo, "f.txt"), "w") as fh:
            fh.write("x")
        subprocess.run(["git", "add", "-A"], cwd=self.repo, capture_output=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-qm", "i"], cwd=self.repo, capture_output=True)
        # A worktree well outside the repo, the way some agents place theirs.
        self.wt = os.path.join(self.tmp, "elsewhere", "w1")
        subprocess.run(["git", "worktree", "add", "-q", self.wt, "-b", "w1"],
                       cwd=self.repo, capture_output=True)

    def tearDown(self):
        subprocess.run(["git", "worktree", "remove", "--force", self.wt],
                       cwd=self.repo, capture_output=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_git_is_believed_over_a_session_reporting_the_wrong_directory(self):
        # One agent records its config directory as the session cwd. Trusting
        # that would send merge and stop at the wrong tree entirely.
        w = {"repo": self.repo, "cwd": "/also/wrong"}
        sessions = {"s": {"cwd": "/completely/wrong"}}
        self.assertEqual(os.path.realpath(self.crew.worker_dir("w1", w, sessions)),
                         os.path.realpath(self.wt))

    def test_the_session_is_used_when_git_knows_no_such_worktree(self):
        w = {"repo": self.repo, "cwd": "/fallback"}
        sessions = {"s": {"cwd": "/from/session"}}
        got = self.crew.worker_dir("nosuchworker", w, sessions)
        self.assertEqual(got, "/fallback")


class WorkerStateRaceTest(unittest.TestCase):
    """crew stop dropping a worker must stick, even against a concurrent
    refresh that is still writing from a snapshot taken before the stop."""

    def setUp(self):
        self.crew = crew_module()
        self.tmp = tempfile.mkdtemp(prefix="crew-state-race-")
        self.crew.STATE_PATH = os.path.join(self.tmp, "workers.json")
        self.crew.put_worker("w1", {"task": "t"}, create=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_stopped_worker_does_not_reappear_from_a_stale_refresh(self):
        stale_snapshot = threading.Event()
        stopped = threading.Event()

        # A status refresh reads the worker before crew stop runs, then is
        # slow enough to write its (now stale) copy back after the stop has
        # already removed it.
        def refresh_process():
            stale = self.crew.workers()["w1"]
            stale_snapshot.set()
            stopped.wait(timeout=5)
            self.crew.put_worker("w1", stale)

        def stop_process():
            stale_snapshot.wait(timeout=5)
            self.crew.drop_worker("w1")
            stopped.set()

        t1 = threading.Thread(target=refresh_process)
        t2 = threading.Thread(target=stop_process)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        self.assertNotIn("w1", self.crew.workers(),
                         "a stale refresh resurrected a worker crew stop had "
                         "just dropped")


class MergeSelfTest(unittest.TestCase):
    """A worker that resolves to the primary checkout has nothing to merge.

    A worker registered without its own worktree resolves to the checkout
    you are merging into. Merging its branch into that same checkout is a
    merge of a branch into itself: git reports "Already up to date" and
    crew must not read that as a successful merge of the worker's work.
    """

    def setUp(self):
        self.crew = crew_module()
        self.tmp = tempfile.mkdtemp(prefix="crew-selfmerge-")
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        for cmd in (["init", "-q", "-b", "main"],):
            subprocess.run(["git", *cmd], cwd=self.repo, capture_output=True)
        with open(os.path.join(self.repo, "f.txt"), "w") as fh:
            fh.write("x")
        subprocess.run(["git", "add", "-A"], cwd=self.repo, capture_output=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-qm", "i"], cwd=self.repo, capture_output=True)
        self.crew.hook_sessions = lambda: {}
        self.crew.worker_state = lambda w, sessions: ("idle", {})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_merge_refuses_when_the_worker_resolves_to_the_primary_checkout(self):
        w = {"repo": self.repo, "cwd": self.repo, "surface": "s",
             "branch": "main", "task": "self merge"}
        self.crew.workers = lambda: {"w1": w}
        args = types.SimpleNamespace(worker="w1", force=False)
        cwd = os.getcwd()
        os.chdir(self.repo)
        try:
            with self.assertRaises(SystemExit):
                self.crew.cmd_merge(args, {})
        finally:
            os.chdir(cwd)
        out = subprocess.run(["git", "-C", self.repo, "log", "--oneline"],
                             capture_output=True, text=True).stdout
        self.assertEqual(len(out.strip().splitlines()), 1,
                         "no merge commit should have been created")


class ScreenLifecycleTest(unittest.TestCase):
    """Agents whose reported lifecycle never changes."""

    def setUp(self):
        self.crew = crew_module()
        self.crew.read_screen = lambda ws, surface, lines=40: "a still screen"
        self.crew.put_worker = lambda name, w: None

    def refresh(self, spec, fp_at_offset):
        cfg = dict(self.crew.DEFAULTS)
        cfg["agents"] = {"a": spec}
        cfg["screen_idle_seconds"] = 20
        cfg["stall_minutes"] = 15
        w = {"agent": "a", "surface": "s", "workspace": "ws", "role": "r", "task": "t",
             "fp": self.crew.screen_fingerprint("a still screen"),
             "fp_at": time.time() - fp_at_offset}
        sessions = {"s": {"state": "running", "pid": os.getpid()}}
        return self.crew.refresh({"w": w}, sessions, cfg)["w"][0]

    def test_a_still_screen_means_idle_when_the_lifecycle_cannot_be_trusted(self):
        # Without this the agent reads as running forever and wait never wakes.
        self.assertEqual(self.refresh({"lifecycle": "screen"}, 30), "idle")

    def test_it_is_still_working_until_the_screen_has_been_quiet_long_enough(self):
        self.assertEqual(self.refresh({"lifecycle": "screen"}, 5), "running")

    def test_an_agent_with_a_trustworthy_lifecycle_is_left_alone(self):
        # A quiet screen is not idleness for these; that is what stalled is for.
        self.assertEqual(self.refresh({}, 30), "running")


class StaleNeedsInputTest(unittest.TestCase):
    """A session that says it needs input, then quietly finishes the turn."""

    def setUp(self):
        self.crew = crew_module()
        self.crew.put_worker = lambda name, w: None

    def state(self, screen):
        self.crew.read_screen = lambda ws, surface, lines=40: screen
        cfg = dict(self.crew.DEFAULTS)
        cfg["agents"] = {"a": {"approval": {"prompt": [r"Do you want to proceed"]},
                               "blocked": [r"Press any key to log in"]}}
        w = {"agent": "a", "surface": "s", "workspace": "ws", "role": "r",
             "task": "t", "fp": "x", "fp_at": time.time()}
        sessions = {"s": {"state": "needsInput", "pid": os.getpid()}}
        return self.crew.refresh({"w": w}, sessions, cfg)["w"][0]

    def test_nothing_asking_on_screen_means_it_is_not_waiting(self):
        # Measured on a real session: it reported needsInput, finished the
        # turn, and never updated. A director would relay a question nobody
        # is asking.
        self.assertEqual(self.state("the work is done, at the prompt"), "idle")

    def test_a_real_question_still_counts_as_needing_input(self):
        self.assertEqual(self.state("Do you want to proceed?"), "needsInput")

    def test_a_login_screen_still_counts_as_needing_input(self):
        self.assertEqual(self.state("Press any key to log in"), "needsInput")

    def test_stale_state_clears_for_an_agent_with_no_approval_block(self):
        # codex and cursor define "blocked" but no "approval" prompt. The
        # stale-clear must not require an approval block to exist.
        self.crew.read_screen = lambda ws, surface, lines=40: "the work is done, at the prompt"
        cfg = dict(self.crew.DEFAULTS)
        cfg["agents"] = {"a": {"blocked": [r"Press any key to log in"]}}
        w = {"agent": "a", "surface": "s", "workspace": "ws", "role": "r",
             "task": "t", "fp": "x", "fp_at": time.time()}
        sessions = {"s": {"state": "needsInput", "pid": os.getpid()}}
        self.assertEqual(self.crew.refresh({"w": w}, sessions, cfg)["w"][0], "idle")


class DeliverTest(unittest.TestCase):
    """A blocked screen must never be typed at, not even a probe."""

    def setUp(self):
        self.crew = crew_module()
        self.crew.exited = lambda surface: None

    def test_a_blocked_screen_receives_no_keystrokes(self):
        calls = []

        def fake_cmux(*args, timeout=60):
            calls.append(args)
            if args[0] == "read-screen":
                return 0, "hello there\nPress any key to log in"
            return 0, ""

        self.crew.cmux = fake_cmux
        spec = {"blocked": [r"Press any key to log in"]}

        ok, why = self.crew.deliver("ws", "s", "hello there", spec)

        self.assertFalse(ok)
        self.assertIn("only you can answer", why)
        sent = [c for c in calls if c[0] in ("send", "send-key")]
        self.assertEqual(sent, [], f"blocked screen was typed at: {sent}")


class QuotaOnlyInTheTailTest(unittest.TestCase):
    """A worker writing about limits is not a worker that has hit one."""

    def setUp(self):
        self.crew = crew_module()
        self.crew.put_worker = lambda name, w: None

    def state(self, screen):
        self.crew.read_screen = lambda ws, surface, lines=40: screen
        cfg = dict(self.crew.DEFAULTS)
        cfg["agents"] = {"a": {}}
        w = {"agent": "a", "surface": "s", "workspace": "ws", "role": "r",
             "task": "t", "fp": "x", "fp_at": time.time()}
        sessions = {"s": {"state": "running", "pid": os.getpid()}}
        return self.crew.refresh({"w": w}, sessions, cfg)["w"][0]

    def test_a_review_discussing_rate_limits_is_not_an_exhausted_plan(self):
        # A reviewer reading this very file wrote the words into its output and
        # was reported as out of allowance, so the director stopped spawning.
        body = "\n".join(["QUOTA_PATTERNS covers rate limit and quota wording"]
                         + [f"line {i}" for i in range(12)])
        self.assertEqual(self.state(body), "running")

    def test_a_permission_prompt_mentioning_quota_is_not_exhaustion(self):
        # Reproduced live: a fixer asked to run QuotaOnlyInTheTailTest and was
        # marked exhausted, which also stopped it being approved, so it stuck.
        self.crew.read_screen = lambda ws, surface, lines=40: (
            "Do you want to proceed?\n 2. Yes, and do not ask again for: "
            "python3 test/test_crew.py QuotaOnlyInTheTailTest")
        cfg = dict(self.crew.DEFAULTS)
        cfg["agents"] = {"a": {"approval": {"prompt": [r"Do you want to proceed"]}}}
        w = {"agent": "a", "surface": "s", "workspace": "ws", "role": "r",
             "task": "t", "fp": "x", "fp_at": time.time()}
        sessions = {"s": {"state": "needsInput", "pid": os.getpid()}}
        self.assertNotEqual(
            self.crew.refresh({"w": w}, sessions, cfg)["w"][0], "quota")

    def test_a_limit_where_the_agent_actually_stopped_still_counts(self):
        screen = "\n".join([f"line {i}" for i in range(12)]
                           + ["you have hit your usage limit"])
        self.assertEqual(self.state(screen), "quota")

    def test_a_mention_mid_tail_with_more_output_after_it_is_not_a_stop(self):
        # The worker warns about rate limits while still working, then keeps
        # producing unrelated output afterward. It is still running: only
        # the true end of the screen says whether it actually stopped there.
        screen = "\n".join([f"line {i}" for i in range(4)]
                           + ["note: watch for rate limit errors on retries"]
                           + [f"line {i}" for i in range(4, 8)])
        self.assertEqual(self.state(screen), "running")


class QuotaPatternTest(unittest.TestCase):
    def test_an_allowance_indicator_is_not_an_exhausted_plan(self):
        # Real status lines from healthy sessions. A pattern matching "0% left"
        # or a bare percentage would stop the fleet for no reason.
        crew = crew_module()
        healthy = [
            "stall1 | Haiku 4.5 | ctx 21% | 5h 36% (23:20) | wk 51% | $0.10",
            "ga | Composer 2.5 | main | api 0% left (Aug 17) | toko 113 memories",
            "ga | gpt-5.6-sol max | No changes | Context 0% used | weekly 100% left | Ready",
        ]
        for line in healthy:
            hits = [p for p in crew.QUOTA_PATTERNS if re.search(p, line, re.I)]
            self.assertEqual(hits, [], f"false positive on a healthy screen: {line}")


class ProjectRolesTest(unittest.TestCase):
    def test_a_repo_can_define_roles_of_its_own(self):
        # A codebase with its own vocabulary gets a crew that speaks it, and
        # the definitions travel with the repo rather than living in one
        # person's home directory.
        tmp = tempfile.mkdtemp(prefix="crew-proj-")
        try:
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp,
                           capture_output=True)
            with open(os.path.join(tmp, ".crew.json"), "w") as fh:
                json.dump({"roles": {"art-director": {
                    "agent": "claude", "model": "opus", "permission": "plan"}}}, fh)
            here = os.getcwd()
            os.chdir(tmp)
            try:
                cfg = crew_module().config()
            finally:
                os.chdir(here)
            self.assertIn("art-director", cfg["roles"])
            self.assertIn("worker", cfg["roles"], "the built-in roles were lost")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


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

    def test_idle_is_reported_again_after_a_run_in_between(self):
        # idle -> running -> idle must be reported twice. A worker that goes
        # back to work and finishes again is news each time, not just once.
        crew = crew_module()
        worker = {"role": "r", "task": "t", "surface": "s"}
        crew.workers = lambda: {"w": worker}
        crew.hook_sessions = lambda: {}
        crew.put_worker = lambda name, w: None

        current = ["idle"]
        crew.refresh = lambda all_w, sessions, cfg: {"w": (current[0], worker)}

        printed = []
        crew.log = lambda msg="": printed.append(msg)

        rc = crew.cmd_wait(types.SimpleNamespace(timeout=1, poll=0.01), {})
        self.assertEqual(rc, 0)
        self.assertEqual(len(printed), 1, "first idle was not reported")
        self.assertIn("idle", printed[0])

        current[0] = "running"
        rc = crew.cmd_wait(types.SimpleNamespace(timeout=0.05, poll=0.01), {})
        self.assertEqual(rc, 0)

        current[0] = "idle"
        printed.clear()
        rc = crew.cmd_wait(types.SimpleNamespace(timeout=1, poll=0.01), {})
        self.assertEqual(rc, 0)
        self.assertEqual(len(printed), 1,
                         "idle after a run in between was not reported")
        self.assertIn("idle", printed[0],
                       "expected a worker report, got: " + printed[0])


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

    def test_invalid_config_warns_and_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "crew.json")
            with open(path, "w") as fh:
                fh.write("{not valid json")
            os.environ["CREW_CONFIG"] = path
            mod = crew_module()
            printed = []
            mod.log = lambda msg="": printed.append(msg)
            cfg = mod.config()
            self.assertEqual(cfg["auto_approve"], mod.DEFAULTS["auto_approve"])
            self.assertTrue(any(path in msg and "warning" in msg
                                for msg in printed),
                            f"expected a warning naming {path}, got {printed}")
            del os.environ["CREW_CONFIG"]

    def test_shell_quoting_survives_a_task_with_quotes(self):
        q = self.crew.shell_quote("don't; rm -rf /")
        self.assertEqual(q, """'don'\\''t; rm -rf /'""")


class SayToAWorkerWithARemovedAgentTest(unittest.TestCase):
    """workers.json can outlive crew.json: a worker recorded against an agent
    that was since deleted from the config must not crash `crew say`."""

    def setUp(self):
        self.crew = crew_module()
        self.crew.workers = lambda: {
            "stale": {"agent": "ghost", "surface": "s1", "workspace": "ws"},
        }

    def test_say_reports_the_missing_agent_instead_of_crashing(self):
        args = types.SimpleNamespace(worker="stale", message="hi", now=False)
        cfg = {"agents": {"claude": {}}}
        out = io.StringIO()
        with self.assertRaises(SystemExit) as ctx:
            with contextlib.redirect_stdout(out):
                self.crew.cmd_say(args, cfg)
        self.assertEqual(ctx.exception.code, 1)
        printed = out.getvalue()
        self.assertIn("stale", printed)
        self.assertIn("ghost", printed)
        self.assertNotIn("Traceback", printed)

    def test_say_still_works_for_an_agent_configured_with_an_empty_spec(self):
        # A configured agent can legitimately have no per-agent overrides, so
        # its spec is `{}` -- that must not be mistaken for "not configured".
        self.crew.workers = lambda: {
            "sparse": {"agent": "claude", "surface": "s1", "workspace": "ws"},
        }
        self.crew.hook_sessions = lambda: {}
        delivered = {}
        self.crew.deliver = lambda ws, surface, text, spec: delivered.update(
            ws=ws, surface=surface, text=text, spec=spec) or (True, "")
        args = types.SimpleNamespace(worker="sparse", message="hi", now=False)
        cfg = {"agents": {"claude": {}}}
        self.assertEqual(self.crew.cmd_say(args, cfg), 0)
        self.assertEqual(delivered["spec"], {})


class InstallUninstallTest(unittest.TestCase):
    """--uninstall must remove both links when run through the installed symlink.

    cmd_install decides ownership from the path of the running script. Invoked
    directly that is the checkout; invoked through the installed symlink (as it
    always is in real use) it must still resolve back to the checkout, or the
    ownership check fails and every link is left behind.
    """

    def setUp(self):
        self.prefix = tempfile.mkdtemp(prefix="crew-install-")

    def tearDown(self):
        shutil.rmtree(self.prefix, ignore_errors=True)

    def test_uninstall_through_the_installed_symlink_removes_both_links(self):
        subprocess.run(
            [CREW, "install", "--prefix", self.prefix],
            capture_output=True, text=True)
        crew_link = os.path.join(self.prefix, "crew")
        reap_link = os.path.join(self.prefix, "crew-reap")
        self.assertTrue(os.path.islink(crew_link))
        self.assertTrue(os.path.islink(reap_link))

        subprocess.run(
            [crew_link, "install", "--uninstall", "--prefix", self.prefix],
            check=True, capture_output=True, text=True)

        self.assertFalse(os.path.exists(crew_link),
                          "crew link should have been removed")
        self.assertFalse(os.path.exists(reap_link),
                          "crew-reap link should have been removed")

    def test_uninstall_leaves_a_symlink_to_a_sibling_checkout_alone(self):
        # A sibling checkout whose bin dir string-extends this one's ("bin" vs
        # "bin-sibling") used to fool a startswith ownership check even though
        # it is a different checkout entirely.
        checkout = tempfile.mkdtemp(prefix="crew-checkout-")
        here = os.path.join(checkout, "bin")
        os.makedirs(here)
        shutil.copy(CREW, os.path.join(here, "crew"))
        with open(os.path.join(here, "crew-reap"), "w") as fh:
            fh.write("#!/bin/sh\n")

        sibling = os.path.join(checkout, "bin-sibling")
        os.makedirs(sibling)
        with open(os.path.join(sibling, "crew-reap"), "w") as fh:
            fh.write("#!/bin/sh\n")

        subprocess.run(
            [os.path.join(here, "crew"), "install", "--prefix", self.prefix],
            capture_output=True, text=True)
        crew_link = os.path.join(self.prefix, "crew")
        reap_link = os.path.join(self.prefix, "crew-reap")
        self.assertTrue(os.path.islink(crew_link))

        os.remove(reap_link)
        os.symlink(os.path.join(sibling, "crew-reap"), reap_link)

        subprocess.run(
            [crew_link, "install", "--uninstall", "--prefix", self.prefix],
            check=True, capture_output=True, text=True)

        self.assertFalse(os.path.exists(crew_link),
                          "crew link should have been removed")
        self.assertTrue(os.path.islink(reap_link),
                         "sibling checkout's link should have been left alone")


if __name__ == "__main__":
    unittest.main(verbosity=2)
