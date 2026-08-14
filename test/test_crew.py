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
import unittest.mock as mock

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


class PidAliveTest(unittest.TestCase):
    """A pid we cannot signal is not the same as a pid that is gone."""

    def setUp(self):
        self.crew = crew_module()

    def test_permission_denied_is_alive_but_no_such_process_is_dead(self):
        # os.kill(pid, 0) raises PermissionError for a live pid owned by
        # another user, not for a dead one.
        with mock.patch.object(os, "kill", side_effect=PermissionError):
            self.assertTrue(self.crew.pid_alive(123))
        with mock.patch.object(os, "kill", side_effect=ProcessLookupError):
            self.assertFalse(self.crew.pid_alive(123))


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
        self.crew.refresh = lambda all_w, sessions, cfg: {
            name: ("idle", ww) for name, ww in all_w.items()}

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
                self.crew.cmd_merge(args, {"agents": {}})
        finally:
            os.chdir(cwd)
        out = subprocess.run(["git", "-C", self.repo, "log", "--oneline"],
                             capture_output=True, text=True).stdout
        self.assertEqual(len(out.strip().splitlines()), 1,
                         "no merge commit should have been created")


class MergeStaleCwdTest(unittest.TestCase):
    """A worktree's slot in the session store can outlive the worktree.

    `crew reap` removes the worktree directory, but the hook session that
    reported it as the worker's cwd is not always cleaned up in lockstep,
    and some agents leave a leftover directory behind that is no longer a
    git repository at all. Merge must not read a failed `git status` in
    that directory as uncommitted files.
    """

    def setUp(self):
        self.crew = crew_module()
        self.tmp = tempfile.mkdtemp(prefix="crew-stalecwd-")
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.repo,
                       capture_output=True)
        with open(os.path.join(self.repo, "f.txt"), "w") as fh:
            fh.write("x")
        subprocess.run(["git", "add", "-A"], cwd=self.repo, capture_output=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-qm", "i"], cwd=self.repo, capture_output=True)
        subprocess.run(["git", "branch", "w1"], cwd=self.repo, capture_output=True)
        subprocess.run(["git", "-C", self.repo, "checkout", "-q", "w1"],
                       capture_output=True)
        with open(os.path.join(self.repo, "f.txt"), "w") as fh:
            fh.write("y")
        subprocess.run(["git", "add", "-A"], cwd=self.repo, capture_output=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-qm", "worker change"], cwd=self.repo,
                       capture_output=True)
        subprocess.run(["git", "-C", self.repo, "checkout", "-q", "main"],
                       capture_output=True)

        # No worktree was ever created for this worker, so worker_dir falls
        # through to the session's reported cwd, which exists on disk but
        # was never a git repo (or has since been reaped).
        self.stale = os.path.join(self.tmp, "stale-session-cwd")
        os.makedirs(self.stale)

        self.crew.hook_sessions = lambda: {"s": {"cwd": self.stale}}
        self.crew.refresh = lambda all_w, sessions, cfg: {
            name: ("idle", ww) for name, ww in all_w.items()}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_merge_succeeds_despite_a_stale_non_repo_session_cwd(self):
        w = {"repo": self.repo, "surface": "s", "branch": "w1",
             "task": "stale cwd merge"}
        self.crew.workers = lambda: {"w1": w}
        args = types.SimpleNamespace(worker="w1", force=False)
        cwd = os.getcwd()
        os.chdir(self.repo)
        try:
            rc = self.crew.cmd_merge(args, {"agents": {}})
        finally:
            os.chdir(cwd)
        self.assertEqual(rc, 0)
        with open(os.path.join(self.repo, "f.txt")) as fh:
            self.assertEqual(fh.read(), "y",
                             "the worker's branch change was not merged in")


class MergeAmbiguousNameTest(unittest.TestCase):
    """One name, two things to merge, so merge refuses.

    `crew merge` takes either a worker name or a branch name, and the worker
    wins. When a name is both a live worker and a branch of its own, that
    silently merges the worker's branch and never says so, which is a merge
    of something the person did not ask for.
    """

    def setUp(self):
        self.crew = crew_module()
        self.tmp = tempfile.mkdtemp(prefix="crew-ambiguous-")
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        self.git("init", "-q", "-b", "main")
        self.commit("f.txt", "x")
        self.crew.hook_sessions = lambda: {}
        self.crew.refresh = lambda all_w, sessions, cfg: {
            name: ("idle", ww) for name, ww in all_w.items()}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def git(self, *args):
        return subprocess.run(["git", "-C", self.repo, *args],
                              capture_output=True, text=True)

    def commit(self, name, text):
        with open(os.path.join(self.repo, name), "w") as fh:
            fh.write(text)
        self.git("add", "-A")
        self.git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "c")

    def branch_holding(self, branch, name, text):
        """A branch off main carrying one file, leaving main checked out."""
        self.git("checkout", "-q", "-b", branch)
        self.commit(name, text)
        self.git("checkout", "-q", "main")

    def merge(self, worker, force=False):
        args = types.SimpleNamespace(worker=worker, force=force)
        cwd, out = os.getcwd(), io.StringIO()
        os.chdir(self.repo)
        try:
            with contextlib.redirect_stdout(out):
                rc = self.crew.cmd_merge(args, {"agents": {}})
        finally:
            os.chdir(cwd)
        return rc, out.getvalue()

    def test_merge_refuses_a_name_that_is_both_a_worker_and_another_branch(self):
        self.branch_holding("crew/foo", "worker.txt", "the worker's work")
        self.branch_holding("foo", "branch.txt", "the branch's work")
        self.crew.workers = lambda: {"foo": {
            "repo": self.repo, "surface": "s", "branch": "crew/foo",
            "task": "ambiguous"}}

        cwd, out = os.getcwd(), io.StringIO()
        os.chdir(self.repo)
        try:
            with contextlib.redirect_stdout(out):
                with self.assertRaises(SystemExit):
                    self.crew.cmd_merge(
                        types.SimpleNamespace(worker="foo", force=True),
                        {"agents": {}})
        finally:
            os.chdir(cwd)

        said = out.getvalue()
        self.assertIn("crew/foo", said, "the worker's branch was not named")
        self.assertIn("foo", said, "the branch was not named")
        self.assertIn(self.repo, said, "the repo holding the branch was not named")
        # Nothing to act on unless it says how to ask for each one by itself.
        self.assertIn("crew merge crew/foo", said)
        self.assertIn(f"git -C {self.repo} merge --no-ff foo", said)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "worker.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.repo, "branch.txt")))

    def test_a_worker_whose_own_branch_shares_its_name_still_merges(self):
        # Every worker crew spawns is this case when its branch is named after
        # it: one name, one thing to merge, so there is nothing to refuse.
        self.branch_holding("solo", "solo.txt", "the worker's work")
        self.crew.workers = lambda: {"solo": {
            "repo": self.repo, "surface": "s", "branch": "solo",
            "task": "ordinary"}}
        rc, said = self.merge("solo")
        self.assertEqual(rc, 0, said)
        self.assertTrue(os.path.exists(os.path.join(self.repo, "solo.txt")), said)

    def test_a_name_matching_only_a_branch_still_merges_that_branch(self):
        self.branch_holding("just-a-branch", "loose.txt", "no worker here")
        self.crew.workers = lambda: {}
        rc, said = self.merge("just-a-branch")
        self.assertEqual(rc, 0, said)
        self.assertTrue(os.path.exists(os.path.join(self.repo, "loose.txt")), said)

    def test_a_name_matching_only_a_worker_still_merges_that_worker(self):
        self.branch_holding("crew/bar", "worker.txt", "the worker's work")
        self.crew.workers = lambda: {"bar": {
            "repo": self.repo, "surface": "s", "branch": "crew/bar",
            "task": "worker only"}}
        rc, said = self.merge("bar")
        self.assertEqual(rc, 0, said)
        self.assertTrue(os.path.exists(os.path.join(self.repo, "worker.txt")), said)


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

    def test_a_wrapped_login_route_mention_is_not_blocked(self):
        # Terminal wrapping can put a worker's own text about a web app's
        # /login route alone on a visual line, which a line-anchored bare
        # "/login" pattern used to match.
        def fake_cmux(*args, timeout=60):
            if args[0] == "read-screen":
                return 0, "hello there\nEditing src/routes/login.ts, the POST\n/login\nhandler"
            return 0, ""

        self.crew.cmux = fake_cmux

        ok, why = self.crew.deliver("ws", "s", "hello there", self.crew.AGENTS["claude"])

        self.assertTrue(ok, why)

    def test_claudes_own_login_instruction_is_still_blocked(self):
        def fake_cmux(*args, timeout=60):
            if args[0] == "read-screen":
                return 0, "Invalid API key · Please run /login"
            return 0, ""

        self.crew.cmux = fake_cmux

        ok, why = self.crew.deliver("ws", "s", "hello there", self.crew.AGENTS["claude"])

        self.assertFalse(ok)
        self.assertIn("only you can answer", why)


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


def pattern_source_lines():
    """The QUOTA_PATTERNS list exactly as it is written in bin/crew."""
    with open(CREW) as fh:
        src = fh.read()
    block = src.split("QUOTA_PATTERNS = [", 1)[1].split("]", 1)[0]
    return [l for l in block.splitlines() if l.strip().startswith('r"')]


# Screens taken from what these agents draw. The exhausted ones are the whole
# sentence an agent prints where it stopped; the healthy ones all contain a
# word from that sentence without meaning any of it.
CLAUDE_EXHAUSTED = """\
⏺ Read(bin/crew)
  ⎿  Read 1496 lines

⏺ I'll add the pattern list next.

Claude usage limit reached. Your limit will reset at 4pm (Europe/London)."""

CODEX_EXHAUSTED = """\
• Updated test/test_crew.py (+12 -3)

▌ You've hit your usage limit. Try again after 3:00 PM, or upgrade your plan."""

CURSOR_EXHAUSTED = """\
● Ran python3 test/test_crew.py
  40 passed

You are out of credits. Add credits or wait for the monthly reset."""

# The live one. crew named the worktree and the branch after the worker, and
# claude draws both in its own chrome, so a bare-word scan called a healthy
# welcome screen exhausted the instant it came up.
QUOTA_NAMED_WELCOME = """\
╭──────────────────────────────────────────────────────────────╮
│ ✻ Welcome to Claude Code                                     │
│                                                              │
│   /help for help, /status for your current setup             │
│                                                              │
│   cwd: /Users/k/github/crew/.crew/worktrees/fix-quota-detect │
╰──────────────────────────────────────────────────────────────╯

> Try "how do I log an error?"

  ⏵⏵ accept edits on (shift+tab to cycle) · crew/fix-quota-detect"""


class ExhaustionCorpusTest(unittest.TestCase):
    """Real screens, both ways. Only a session that has actually run out."""

    def setUp(self):
        self.crew = crew_module()
        self.crew.put_worker = lambda name, w: None

    def state(self, screen, name="w", agent=None, session="running", **extra):
        self.crew.read_screen = lambda ws, surface, lines=40: screen
        cfg = dict(self.crew.DEFAULTS)
        cfg["agents"] = {"a": {} if agent is None else agent}
        w = {"agent": "a", "surface": "s", "workspace": "ws", "role": "worker",
             "task": "t", "fp": "x", "fp_at": time.time()}
        w.update(extra)
        sessions = {"s": {"state": session, "pid": os.getpid()}}
        return self.crew.refresh({name: w}, sessions, cfg)[name][0]

    def test_a_claude_session_that_ran_out_is_exhausted(self):
        self.assertEqual(self.state(CLAUDE_EXHAUSTED), "quota")

    def test_a_codex_session_that_ran_out_is_exhausted(self):
        self.assertEqual(self.state(CODEX_EXHAUSTED), "quota")

    def test_a_cursor_session_that_ran_out_is_exhausted(self):
        self.assertEqual(self.state(CURSOR_EXHAUSTED), "quota")

    def test_an_allowance_status_line_is_not_exhaustion(self):
        # "api 0% left" is a healthy status line, not a session that stopped.
        screen = "⏺ Done. The suite is green.\n\n" \
                 "ga | Composer 2.5 | main | api 0% left (Aug 17) | toko 113 memories"
        self.assertEqual(self.state(screen), "running")

    def test_a_permission_prompt_quoting_a_limit_word_is_not_exhaustion(self):
        screen = ("Do you want to proceed?\n"
                  " 1. Yes\n"
                  " 2. Yes, and do not ask again for: python3 test/test_crew.py "
                  "QuotaOnlyInTheTailTest")
        state = self.state(screen, session="needsInput",
                           agent={"approval": {"prompt": [r"Do you want to proceed"]}})
        self.assertEqual(state, "needsInput")

    def test_a_worker_named_after_a_limit_is_not_exhausted(self):
        # The live failure: the worktree, the branch and the tab all carry the
        # worker's name, so the name must never be what crew reads.
        state = self.state(QUOTA_NAMED_WELCOME, name="fix-quota-detect",
                           branch="crew/fix-quota-detect",
                           cwd="/Users/k/github/crew/.crew/worktrees/fix-quota-detect",
                           task="fix quota detection")
        self.assertEqual(state, "running")

    def test_a_worker_showing_this_files_pattern_list_is_not_exhausted(self):
        for line in pattern_source_lines():
            screen = "⏺ Read(bin/crew)\n  ⎿  QUOTA_PATTERNS = [\n" + line
            self.assertNotEqual(self.state(screen), "quota", line)

    def test_the_task_crew_typed_coming_back_on_screen_is_not_exhaustion(self):
        # crew types the task into the composer and the agent draws it back.
        task = "reword the usage limit reached banner so it names the reset time"
        screen = ("⏵⏵ accept edits on\n"
                  "> " + task)
        self.assertEqual(self.state(screen, task=task), "running")


class AwaitReadyExhaustionTest(unittest.TestCase):
    """The same check at spawn time, where the screen is mostly crew's doing."""

    def setUp(self):
        self.crew = crew_module()

    def ready(self, screen, name="w", **rec):
        self.crew.read_screen = lambda ws, surface, lines=40: screen
        return self.crew.await_ready("ws", "s", self.crew.AGENTS["claude"], 5,
                                     self.crew.crew_text(name, rec))

    def test_a_welcome_screen_in_a_limit_named_worktree_is_ready(self):
        state, detail = self.ready(
            QUOTA_NAMED_WELCOME, name="fix-quota-detect",
            cwd="/Users/k/github/crew/.crew/worktrees/fix-quota-detect",
            task="fix quota detection")
        self.assertEqual(state, "ready", detail)

    def test_the_task_crew_just_typed_does_not_read_as_exhaustion(self):
        task = "reword the usage limit reached banner so it names the reset time"
        state, detail = self.ready(
            "  ⏵⏵ accept edits on (shift+tab to cycle)\n> " + task, task=task)
        self.assertEqual(state, "ready", detail)

    def test_a_session_that_really_ran_out_is_caught_before_anything_is_typed(self):
        state, detail = self.ready(CLAUDE_EXHAUSTED)
        self.assertEqual(state, "quota", detail)


class StallFingerprintTest(unittest.TestCase):
    """What counts as the screen changing.

    A spinner, a clock and a token counter all move on their own, so a screen
    that changes only there has not made progress.
    """

    def setUp(self):
        self.crew = crew_module()

    def same(self, before, after):
        return (self.crew.screen_fingerprint(before)
                == self.crew.screen_fingerprint(after))

    def test_a_spinner_frame_is_not_progress(self):
        self.assertTrue(self.same("⠋ Working… (12s)", "⠙ Working… (12s)"))

    def test_a_clock_tick_is_not_progress(self):
        self.assertTrue(self.same("✻ Herding… 14:03:21", "✻ Herding… 14:07:44"))

    def test_a_token_counter_is_not_progress(self):
        self.assertTrue(self.same(
            "✻ Herding… (esc to interrupt · 47s · ↑ 3.2k tokens)",
            "✽ Herding… (esc to interrupt · 4m 2s · ↑ 9.8k tokens)"))

    def test_a_slow_but_advancing_screen_is_progress(self):
        self.assertFalse(self.same(
            "⏺ Read(a.py)\n⠋ Working… (12s · 1.0k tokens)",
            "⏺ Read(a.py)\n⏺ Read(b.py)\n⠙ Working… (13s · 1.1k tokens)"))


class StallStateTest(unittest.TestCase):
    """And what refresh does with it."""

    def setUp(self):
        self.crew = crew_module()
        self.crew.put_worker = lambda name, w: None

    def state(self, before, after, quiet_minutes=16):
        self.crew.read_screen = lambda ws, surface, lines=40: after
        cfg = dict(self.crew.DEFAULTS)
        cfg["agents"] = {"a": {}}
        w = {"agent": "a", "surface": "s", "workspace": "ws", "role": "worker",
             "task": "t", "fp": self.crew.screen_fingerprint(before),
             "fp_at": time.time() - quiet_minutes * 60}
        sessions = {"s": {"state": "running", "pid": os.getpid()}}
        return self.crew.refresh({"w": w}, sessions, cfg)["w"][0]

    def test_a_worker_whose_only_change_is_its_counters_is_stalled(self):
        before = "⏺ Read(bin/crew)\n✻ Herding… (esc to interrupt · 47s · ↑ 3.2k tokens)"
        after = "⏺ Read(bin/crew)\n✽ Herding… (esc to interrupt · 21m 4s · ↑ 9.8k tokens)"
        self.assertEqual(self.state(before, after), "stalled")

    def test_a_worker_that_is_still_producing_output_is_not_stalled(self):
        before = "⏺ Read(bin/crew)\n✻ Herding… (47s · ↑ 3.2k tokens)"
        after = "⏺ Read(bin/crew)\n⏺ Edit(bin/crew)\n✽ Herding… (21m 4s · ↑ 9.1k tokens)"
        self.assertEqual(self.state(before, after), "running")


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
        self.crew.read_screen = lambda ws, surface, lines=40: ""
        self.crew.put_worker = lambda name, w: None
        delivered = {}
        self.crew.deliver = lambda ws, surface, text, spec: delivered.update(
            ws=ws, surface=surface, text=text, spec=spec) or (True, "")
        args = types.SimpleNamespace(worker="sparse", message="hi", now=False)
        cfg = dict(self.crew.DEFAULTS)
        cfg["agents"] = {"claude": {}}
        self.assertEqual(self.crew.cmd_say(args, cfg), 0)
        self.assertEqual(delivered["spec"], {})


class SayAcceptsAQuietScreenLifecycleWorkerTest(unittest.TestCase):
    """status and wait read a quiet screen-lifecycle worker as idle; say must
    agree, or a director that waited for idle still gets refused."""

    def setUp(self):
        self.crew = crew_module()
        self.crew.read_screen = lambda ws, surface, lines=40: "a still screen"
        self.crew.put_worker = lambda name, w: None
        w = {"agent": "a", "surface": "s", "workspace": "ws", "role": "r",
             "task": "t", "fp": self.crew.screen_fingerprint("a still screen"),
             "fp_at": time.time() - 30}
        self.crew.workers = lambda: {"w1": w}
        self.crew.hook_sessions = lambda: {"s": {"state": "running", "pid": os.getpid()}}
        self.delivered = {}
        self.crew.deliver = lambda ws, surface, text, spec: self.delivered.update(
            ws=ws, surface=surface, text=text, spec=spec) or (True, "")

    def test_say_delivers_to_a_worker_the_screen_shows_as_idle(self):
        cfg = dict(self.crew.DEFAULTS)
        cfg["agents"] = {"a": {"lifecycle": "screen"}}
        args = types.SimpleNamespace(worker="w1", message="hi", now=False)
        self.assertEqual(self.crew.cmd_say(args, cfg), 0)
        self.assertEqual(self.delivered["text"], "hi")

    def test_say_refuses_a_stalled_worker_without_now(self):
        # Stalled still means an active mid-turn process; only idle should
        # let a plain say through.
        cfg = dict(self.crew.DEFAULTS)
        cfg["agents"] = {"a": {}}
        cfg["stall_minutes"] = 0.4
        args = types.SimpleNamespace(worker="w1", message="hi", now=False)
        with self.assertRaises(SystemExit):
            self.crew.cmd_say(args, cfg)
        self.assertEqual(self.delivered, {})


class DoctorWithASparseAgentTest(unittest.TestCase):
    """A config-added agent missing required fields must be reported by
    name, not crash doctor with a KeyError."""

    def setUp(self):
        self.crew = crew_module()

    def test_doctor_names_the_agent_and_missing_fields_without_a_traceback(self):
        cfg = dict(self.crew.DEFAULTS)
        cfg["agents"] = {"sparse": {"bin": "true"}}
        cfg["roles"] = {}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = self.crew.cmd_doctor(types.SimpleNamespace(), cfg)
        printed = out.getvalue()
        self.assertNotEqual(rc, 0)
        self.assertIn("sparse", printed)
        for field in ("worktree", "args", "ready", "blocked"):
            self.assertIn(field, printed)
        self.assertNotIn("Traceback", printed)


class SpawnRefusesASparseAgentTest(unittest.TestCase):
    """A config-added agent missing required fields must be rejected before
    spawn touches cmux, so a tab is never orphaned waiting on a field crew
    never checked."""

    def setUp(self):
        self.crew = crew_module()
        self.crew.workers = lambda: {}
        self.crew.hook_sessions = lambda: {}
        self.calls = []
        self.crew.cmux = lambda *a, **k: (self.calls.append(a), (1, "unexpected"))[1]

    def test_spawn_dies_before_any_cmux_call(self):
        cfg = dict(self.crew.DEFAULTS)
        cfg["agents"] = {"sparse": {"bin": "true"}}
        cfg["roles"] = {"worker": {"agent": "sparse", "permission": "edit"}}
        args = types.SimpleNamespace(role="worker", task="do a thing", name=None,
                                     cwd=None, no_task=False)
        out = io.StringIO()
        with self.assertRaises(SystemExit) as ctx:
            with contextlib.redirect_stdout(out):
                self.crew.cmd_spawn(args, cfg)
        self.assertEqual(ctx.exception.code, 1)
        printed = out.getvalue()
        self.assertIn("sparse", printed)
        self.assertNotIn("Traceback", printed)
        self.assertEqual(self.calls, [], "cmux was called before validation")


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


class SayNowLeavesABlockedScreenAloneTest(unittest.TestCase):
    """--now interrupts a running turn. A login or trust dialog is not a turn.

    The session's lifecycle still says running while it sits on one, so
    --now used to send escape and ctrl+u before deliver ever looked at the
    screen. A keystroke at a login screen can start a browser sign-in flow
    and clear the stored credentials.
    """

    def setUp(self):
        self.crew = crew_module()
        self.crew.read_screen = lambda ws, s, lines=40: "Press any key to log in"
        self.crew.put_worker = lambda name, w: None
        self.calls = []
        self.crew.cmux = lambda *a, **k: (self.calls.append(a), (0, ""))[1]
        w = {"agent": "a", "surface": "s", "workspace": "ws", "role": "r",
             "task": "t",
             "fp": self.crew.screen_fingerprint("Press any key to log in"),
             "fp_at": time.time()}
        self.crew.workers = lambda: {"w1": w}
        self.crew.hook_sessions = lambda: {
            "s": {"state": "running", "pid": os.getpid()}}

    def test_no_keystrokes_reach_a_worker_showing_a_login_screen(self):
        cfg = dict(self.crew.DEFAULTS)
        cfg["agents"] = {"a": {"blocked": [r"Press any key to log in"]}}
        args = types.SimpleNamespace(worker="w1", message="hi", now=True)
        out = io.StringIO()
        with self.assertRaises(SystemExit) as ctx:
            with contextlib.redirect_stdout(out):
                self.crew.cmd_say(args, cfg)
        self.assertEqual(ctx.exception.code, 1)
        typed = [c for c in self.calls if c[0] in ("send", "send-key")]
        self.assertEqual(typed, [], f"blocked screen was typed at: {typed}")

    def test_now_still_interrupts_an_ordinary_running_turn(self):
        self.crew.read_screen = lambda ws, s, lines=40: "working on it"
        self.crew.deliver = lambda ws, s, text, spec: (True, "")
        cfg = dict(self.crew.DEFAULTS)
        cfg["agents"] = {"a": {"blocked": [r"Press any key to log in"]}}
        args = types.SimpleNamespace(worker="w1", message="hi", now=True)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.crew.cmd_say(args, cfg), 0)
        keys = [c[-1] for c in self.calls if c[0] == "send-key"]
        self.assertIn("escape", keys)


class ApproveDoesNotTypeAtAnExitedWorkerTest(unittest.TestCase):
    """A dead worker's tab is a shell, and its last frame still shows the
    question it was asking when it died. Answering that types "2" at a shell
    prompt, which runs it as a command."""

    def setUp(self):
        self.crew = crew_module()
        self.tmp = tempfile.mkdtemp(prefix="crew-approve-exit-")
        self.crew.EXIT_DIR = self.tmp
        with open(os.path.join(self.tmp, "s.exit"), "w") as fh:
            fh.write("1")
        self.crew.read_screen = lambda ws, s, lines=40: (
            "Do you want to proceed?\n 2. Yes, and do not ask again\n"
            "[crew] agent exited with status 1")
        self.calls = []
        self.crew.cmux = lambda *a, **k: (self.calls.append(a), (0, ""))[1]
        self.crew.workers = lambda: {
            "w1": {"agent": "a", "surface": "s", "workspace": "ws"}}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_approve_refuses_and_sends_nothing(self):
        cfg = dict(self.crew.DEFAULTS)
        cfg["agents"] = {"a": {"approval": {"prompt": [r"Do you want to proceed"],
                                            "always": "2", "once": "1"}}}
        args = types.SimpleNamespace(worker="w1", once=False)
        out = io.StringIO()
        with self.assertRaises(SystemExit) as ctx:
            with contextlib.redirect_stdout(out):
                self.crew.cmd_approve(args, cfg)
        self.assertEqual(ctx.exception.code, 1)
        typed = [c for c in self.calls if c[0] in ("send", "send-key")]
        self.assertEqual(typed, [], f"a dead worker's shell was typed at: {typed}")


class StopKeepsAWorkerWhoseTabWouldNotCloseTest(unittest.TestCase):
    """Forgetting a worker whose agent is still running loses it for good:
    nothing else names its surface, so it can never be stopped again."""

    def setUp(self):
        self.crew = crew_module()
        self.tmp = tempfile.mkdtemp(prefix="crew-stop-close-")
        self.crew.STATE_PATH = os.path.join(self.tmp, "workers.json")
        self.crew.EXIT_DIR = os.path.join(self.tmp, "exited")
        self.crew.read_screen = lambda ws, s, lines=40: ""
        self.crew.put_worker("w1", {"agent": "a", "surface": "s",
                                    "workspace": "ws", "role": "r", "task": "t",
                                    "repo": "", "cwd": "/nonexistent"},
                             create=True)
        self.calls = []

        def fake_cmux(*args, timeout=60):
            self.calls.append(args)
            return (1, "no surface with that id") if args[0] == "close-surface" \
                else (0, "")

        self.crew.cmux = fake_cmux
        self.cfg = dict(self.crew.DEFAULTS)
        self.cfg["agents"] = {"a": {}}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def stop(self):
        self.crew.hook_sessions = lambda: self.sessions
        args = types.SimpleNamespace(worker="w1", force=False)
        with contextlib.redirect_stdout(io.StringIO()):
            return self.crew.cmd_stop(args, self.cfg)

    def test_a_failed_close_with_the_agent_still_alive_keeps_the_record(self):
        self.sessions = {"s": {"state": "idle", "pid": os.getpid()}}
        with self.assertRaises(SystemExit):
            self.stop()
        self.assertIn("w1", self.crew.workers(),
                      "the worker was forgotten while its agent kept running")

    def test_a_close_that_reports_success_is_not_taken_as_proof_it_worked(self):
        # Measured live: close-surface returned 0, crew printed "stopped" and
        # dropped the record, and both the tab and the agent survived. A later
        # reap then removed that live agent's worktree and it re-rooted onto
        # the primary checkout. Only the agent going proves the stop happened.
        self.crew.cmux = lambda *a, timeout=60: (0, "")
        self.sessions = {"s": {"state": "idle", "pid": os.getpid()}}
        with mock.patch.object(self.crew.time, "sleep", lambda _s: None):
            with self.assertRaises(SystemExit):
                self.stop()
        self.assertIn("w1", self.crew.workers(),
                      "the worker was forgotten while its agent kept running")

    def test_a_worker_whose_tab_is_already_gone_is_still_forgotten(self):
        self.sessions = {}
        self.assertEqual(self.stop(), 0)
        self.assertNotIn("w1", self.crew.workers())


class UnreadableStateFileTest(unittest.TestCase):
    """A half-written or hand-edited workers.json must not read as empty.

    Treating it as empty makes the next write save that emptiness over it,
    and every worker still running is gone from crew's view for good.
    """

    def setUp(self):
        self.crew = crew_module()
        self.tmp = tempfile.mkdtemp(prefix="crew-badstate-")
        self.crew.STATE_PATH = os.path.join(self.tmp, "workers.json")
        self.truncated = '{"w1": {"task": "t", "surf'
        with open(self.crew.STATE_PATH, "w") as fh:
            fh.write(self.truncated)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def contents(self):
        with open(self.crew.STATE_PATH) as fh:
            return fh.read()

    def test_dropping_a_worker_does_not_wipe_the_file(self):
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.crew.drop_worker("w2")
        self.assertEqual(self.contents(), self.truncated)

    def test_creating_a_worker_does_not_wipe_the_file(self):
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.crew.put_worker("w2", {"task": "t"}, create=True)
        self.assertEqual(self.contents(), self.truncated)

    def test_the_failure_names_the_file(self):
        out = io.StringIO()
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stdout(out):
                self.crew.workers()
        self.assertIn(self.crew.STATE_PATH, out.getvalue())

    def test_a_missing_state_file_is_still_simply_empty(self):
        os.remove(self.crew.STATE_PATH)
        self.assertEqual(self.crew.workers(), {})


class StaleWriteKeepsFieldsWrittenSinceTest(unittest.TestCase):
    """Every command holds a worker record it read some time ago. Writing
    that whole copy back erases anything another command recorded in the
    meantime, and a lost branch is a worker whose work cannot be merged."""

    def setUp(self):
        self.crew = crew_module()
        self.tmp = tempfile.mkdtemp(prefix="crew-stale-write-")
        self.crew.STATE_PATH = os.path.join(self.tmp, "workers.json")
        self.crew.put_worker("w1", {"role": "r", "task": "t", "surface": "s"},
                             create=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_refresh_writing_its_old_copy_does_not_erase_a_branch(self):
        # A status refresh reads the worker, then spawn records the branch it
        # resolved, then the refresh writes its fingerprint from the copy it
        # read before that.
        stale = self.crew.workers()["w1"]

        recorded = dict(self.crew.workers()["w1"])
        recorded["branch"] = "crew/w1"
        self.crew.put_worker("w1", recorded)

        stale["fp"] = "abc"
        self.crew.put_worker("w1", stale)

        self.assertEqual(self.crew.workers()["w1"].get("branch"), "crew/w1",
                         "the recorded branch was erased by a stale write")
        self.assertEqual(self.crew.workers()["w1"].get("fp"), "abc")


class NotifyHookTest(unittest.TestCase):
    """A worker's notifications must not reach the person.

    cmux raises its own alert when an agent stops for permission, and it is
    aimed at whoever can unblock it. For a crew worker that is the director,
    which answers routine requests itself, so the alert pulls the person onto
    a tab about something already handled.
    """

    PAYLOAD = {
        "context": {"appFocused": False, "hookId": "crew-notify"},
        "effects": {"command": True, "desktop": True, "markUnread": True,
                    "paneFlash": True, "record": True,
                    "reorderWorkspace": True, "sound": True},
        "notification": {"body": "Claude is waiting for your input",
                         "subtitle": "Waiting", "surfaceId": "worker-surface",
                         "title": "Claude Code", "workspaceId": "ws"},
        "version": 1,
    }

    def setUp(self):
        self.crew = crew_module()
        self.tmp = tempfile.mkdtemp(prefix="crew-notify-")
        self.crew.STATE_PATH = os.path.join(self.tmp, "workers.json")
        self.crew.put_worker("w1", {"agent": "a", "surface": "worker-surface",
                                    "workspace": "ws", "role": "r", "task": "t",
                                    "repo": "", "cwd": "/nonexistent"},
                             create=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_hook(self, raw):
        out = io.StringIO()
        with mock.patch.object(self.crew.sys, "stdin", io.StringIO(raw)):
            with contextlib.redirect_stdout(out):
                self.crew.cmd_notify_hook(types.SimpleNamespace(), {})
        return out.getvalue()

    def test_a_workers_alert_stops_interrupting_the_person(self):
        got = json.loads(self.run_hook(json.dumps(self.PAYLOAD)))
        for effect in ("desktop", "sound", "paneFlash", "reorderWorkspace"):
            self.assertFalse(got["effects"][effect],
                             f"{effect} still fires for a crew worker")

    def test_a_workers_alert_is_still_recorded(self):
        # Silenced, not hidden. The badge and the feed are how the director
        # sees a worker wants it without being dragged to the tab.
        got = json.loads(self.run_hook(json.dumps(self.PAYLOAD)))
        self.assertTrue(got["effects"]["markUnread"])
        self.assertTrue(got["effects"]["record"])

    def test_someone_elses_session_is_left_completely_alone(self):
        # The hook sees every notification on the machine, so touching one
        # crew did not create would silence the person's own work.
        payload = json.loads(json.dumps(self.PAYLOAD))
        payload["notification"]["surfaceId"] = "not-a-worker"
        got = json.loads(self.run_hook(json.dumps(payload)))
        self.assertEqual(got, payload)

    def test_input_it_cannot_parse_is_passed_straight_back(self):
        # Sitting in front of every notification on the machine means a bug
        # here would break notifications generally. Change nothing instead.
        self.assertEqual(self.run_hook("not json at all").strip(),
                         "not json at all")


class StopHookTest(unittest.TestCase):
    """A director must not end its turn while a worker is waiting on it.

    Every worker run so far has needed re-prompting at least once, and only
    the director notices. A Stop hook makes that structural instead of
    something the director is trusted to remember.
    """

    def setUp(self):
        self.crew = crew_module()
        self.tmp = tempfile.mkdtemp(prefix="crew-hook-")
        self.crew.STATE_PATH = os.path.join(self.tmp, "workers.json")
        self.crew.EXIT_DIR = os.path.join(self.tmp, "exited")
        self.crew.read_screen = lambda *a, **k: ""
        self.sessions = {}
        self.crew.hook_sessions = lambda: self.sessions
        self.cfg = dict(self.crew.DEFAULTS)
        self.cfg["agents"] = {"a": {}}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add(self, name, state):
        self.crew.put_worker(name, {"agent": "a", "surface": name,
                                    "workspace": "ws", "role": "r", "task": "t",
                                    "repo": "", "cwd": "/nonexistent"},
                             create=True)
        self.sessions[name] = {"state": state, "pid": os.getpid()}

    def hook(self, **env):
        # Defaults to a session in the fleet's own workspace, which is where
        # the director sits: workers are tabs in its workspace.
        where = {"CMUX_WORKSPACE_ID": "ws", "CMUX_SURFACE_ID": "director"}
        where.update(env)
        out = io.StringIO()
        with mock.patch.dict(os.environ, where):
            with contextlib.redirect_stdout(out):
                self.crew.cmd_hook(types.SimpleNamespace(), self.cfg)
        return json.loads(out.getvalue())

    def test_nothing_waiting_lets_the_turn_end(self):
        self.assertEqual(self.hook(), {})

    def test_a_worker_that_is_only_working_lets_the_turn_end(self):
        # Blocking on running would trap the session for as long as the fleet
        # is busy, which is most of the time.
        self.add("w1", "running")
        self.assertEqual(self.hook(), {})

    def test_a_worker_waiting_on_the_director_blocks_the_turn(self):
        self.add("w1", "idle")
        decision = self.hook()
        self.assertEqual(decision.get("decision"), "block")
        self.assertIn("w1", decision.get("reason", ""),
                      "the reason has to say who, or it is not actionable")

    def test_a_worker_is_never_sent_to_manage_the_fleet(self):
        # Workers cannot see each other and have no business acting on one
        # another. If this hook ever reaches a worker's own session, through
        # inherited settings or a hand-installed one, it must do nothing.
        self.add("w1", "idle")
        self.assertEqual(self.hook(CMUX_SURFACE_ID="w1"), {},
                         "a worker was told to go and mind the fleet")

    def test_a_session_that_is_not_running_this_fleet_is_left_alone(self):
        # Installed for the person's account this sees every session they
        # run, and being dragged back to someone else's fleet is the same
        # interruption crew exists to prevent. Workers are tabs in their
        # director's workspace, so that workspace is what identifies it.
        self.add("w1", "idle")
        self.assertEqual(self.hook(CMUX_WORKSPACE_ID="a-different-workspace"),
                         {}, "an unrelated session was pulled into the fleet")

    def test_a_wedged_worker_cannot_pin_the_director_forever(self):
        # The escape hatch. Without it a worker nothing can fix would keep the
        # director in its own turn indefinitely.
        self.add("w1", "idle")
        for _ in range(self.crew.HOOK_BLOCK_LIMIT):
            self.assertEqual(self.hook().get("decision"), "block")
        self.assertEqual(self.hook(), {}, "a wedged worker pinned the director")


if __name__ == "__main__":
    unittest.main(verbosity=2)
