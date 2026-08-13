#!/usr/bin/env python3
"""End-to-end tests against a real cmux, using stand-in agents.

The parts most likely to break are the ones that talk to cmux: opening a tab,
knowing when a session is ready, and getting a message into it without typing
into the wrong thing. Those cannot be tested with mocks, because mocks would
just agree with whatever this code already believes.

So these use a real cmux and fake agents: shell scripts that reach a prompt and
echo what they are told. No tokens, no network, and the failure cases (an
expired login, a crash at startup) can be produced on demand.

Skipped unless run from inside a cmux terminal.

Run: python3 test/test_live.py
"""

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CREW = os.path.join(HERE, "..", "bin", "crew")
IN_CMUX = bool(os.environ.get("CMUX_SURFACE_ID")) and shutil.which("cmux")


def cmux(*args):
    env = dict(os.environ, CMUX_QUIET="1")
    return subprocess.run(["cmux", *args], capture_output=True, text=True, env=env)


def fake(name, worktree="crew", **extra):
    spec = {
        "bin": os.path.join(HERE, name),
        "worktree": worktree,
        "args": [],
        "permission": {"plan": [], "edit": [], "full": []},
        "ready": [r"for shortcuts"],
        "blocked": [r"Press any key to log in"],
    }
    spec.update(extra)
    return spec


@unittest.skipUnless(IN_CMUX, "needs to run inside a cmux terminal")
class LiveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="crew-live-")
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        for args in (["init", "-q", "-b", "main"],):
            subprocess.run(["git", *args], cwd=self.repo, capture_output=True)
        with open(os.path.join(self.repo, "README.md"), "w") as fh:
            fh.write("x")
        subprocess.run(["git", "add", "-A"], cwd=self.repo, capture_output=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-qm", "init"], cwd=self.repo, capture_output=True)

        self.hooks = os.path.join(self.tmp, "hooks")
        os.makedirs(self.hooks)
        self.state = os.path.join(self.tmp, "workers.json")
        self.cfg_path = os.path.join(self.tmp, "crew.json")
        self.write_config({
            "agents": {"fake": fake("fake-agent"),
                       "fakelogin": fake("fake-agent-login"),
                       "fakecrash": fake("fake-agent-crash"),
                       "fakestall": fake("fake-agent-stalled"),
                       "fakequota": fake("fake-agent-quota")},
            "roles": {"f": {"agent": "fake", "permission": "edit"},
                      "flogin": {"agent": "fakelogin", "permission": "edit"},
                      "fcrash": {"agent": "fakecrash", "permission": "edit"},
                      "fstall": {"agent": "fakestall", "permission": "edit"},
                      "fquota": {"agent": "fakequota", "permission": "edit"}},
            "ready_timeout": 25,
            "stall_minutes": 0.02,   # ~1.2s, so a test can trip it
        })

    def tearDown(self):
        for w in self.workers().values():
            cmux("close-surface", "--workspace", w["workspace"],
                 "--surface", w["surface"])
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_config(self, cfg):
        with open(self.cfg_path, "w") as fh:
            json.dump(cfg, fh)

    def crew(self, *args):
        env = dict(os.environ, CREW_CONFIG=self.cfg_path, CREW_STATE=self.state,
                   CREW_HOOKS_DIR=self.hooks)
        return subprocess.run([CREW, *args], capture_output=True, text=True,
                              env=env, cwd=self.repo)

    def workers(self):
        try:
            with open(self.state) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def screen(self, name):
        w = self.workers()[name]
        p = cmux("read-screen", "--workspace", w["workspace"], "--surface",
                 w["surface"], "--lines", "40")
        return p.stdout

    def fake_session(self, name, state, pid=None):
        """Write a hook store entry the way cmux would."""
        w = self.workers()[name]
        path = os.path.join(self.hooks, "fake-hook-sessions.json")
        with open(path, "w") as fh:
            json.dump({"sessions": {"s1": {
                "surfaceId": w["surface"], "agentLifecycle": state,
                "pid": pid or os.getpid(), "cwd": w["cwd"], "updatedAt": 1,
            }}}, fh)

    # tests ---------------------------------------------------------------
    def test_spawn_opens_a_tab_and_waits_for_the_prompt(self):
        p = self.crew("spawn", "f", "do a thing", "--name", "w1", "--no-task")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("ready", p.stdout)
        self.assertIn("for shortcuts", self.screen("w1"))

    def test_spawn_delivers_the_task(self):
        p = self.crew("spawn", "f", "carrots", "--name", "w2")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("task delivered", p.stdout)
        self.assertIn("GOT: carrots", self.screen("w2"))

    def test_a_session_asking_for_a_login_is_reported_and_never_typed_at(self):
        p = self.crew("spawn", "flogin", "anything", "--name", "w3")
        self.assertEqual(p.returncode, 1)
        self.assertIn("did not start", p.stdout)
        screen = self.screen("w3")
        self.assertIn("Press any key to log in", screen)
        # Nothing was sent: a keystroke here starts a browser sign-in flow, and
        # interrupting that can clear the stored credentials.
        self.assertNotIn("anything", screen)

    def test_an_agent_that_exits_is_reported_with_its_error(self):
        p = self.crew("spawn", "fcrash", "anything", "--name", "w4")
        self.assertEqual(p.returncode, 1)
        self.assertIn("did not start", p.stdout)
        self.assertIn("boom: could not start", self.screen("w4"))

    def test_the_tab_survives_a_failed_launch(self):
        self.crew("spawn", "fcrash", "anything", "--name", "w5")
        # cmux closes a pane whose command exits, which would take the error
        # with it. The evidence has to stay on screen.
        self.assertIn("agent exited with status 3", self.screen("w5"))

    def test_a_ready_agent_is_recognised_without_a_known_prompt(self):
        # Claude's prompt wording changes with its permission mode, and a
        # worker that was genuinely ready was reported as never starting
        # because of it. A registered session plus a screen that has stopped
        # changing says the same thing without depending on wording.
        self.write_config({
            "agents": {"quiet": fake("fake-agent-quiet",
                                     args=[self.hooks],
                                     ready=[r"this text never appears"])},
            "roles": {"q": {"agent": "quiet", "permission": "edit"}},
            "ready_timeout": 25,
        })
        p = self.crew("spawn", "q", "hello", "--no-task")
        self.assertIn("ready", p.stdout, p.stdout + p.stderr)

    def commit_in(self, path, filename, text):
        with open(os.path.join(path, filename), "w") as fh:
            fh.write(text)
        subprocess.run(["git", "add", "-A"], cwd=path, capture_output=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-qm", "worker output"], cwd=path,
                       capture_output=True)

    def test_stop_closes_the_tab_and_forgets_the_worker(self):
        self.crew("spawn", "f", "a task", "--name", "s1", "--no-task")
        w = self.workers()["s1"]
        p = self.crew("stop", "s1")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("s1", self.workers())
        screen = cmux("read-screen", "--workspace", w["workspace"],
                      "--surface", w["surface"], "--lines", "5")
        self.assertNotEqual(screen.returncode, 0, "the tab is still open")

    def test_stop_refuses_a_worker_mid_turn(self):
        self.crew("spawn", "f", "a task", "--name", "s2", "--no-task")
        self.fake_session("s2", "running")
        p = self.crew("stop", "s2")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("s2", self.workers(), "the worker was forgotten anyway")

    def test_stop_leaves_the_work_on_its_branch(self):
        self.crew("spawn", "f", "a task", "--name", "s3", "--no-task")
        wt = self.workers()["s3"]["cwd"]
        self.commit_in(wt, "kept.txt", "still here")
        self.crew("stop", "s3")
        p = subprocess.run(["git", "-C", self.repo, "show", "crew/s3:kept.txt"],
                           capture_output=True, text=True)
        self.assertEqual(p.stdout.strip(), "still here")

    def test_merge_brings_a_workers_branch_into_the_repo(self):
        self.crew("spawn", "f", "a task", "--name", "m1", "--no-task")
        wt = self.workers()["m1"]["cwd"]
        self.commit_in(wt, "delivered.txt", "from the worker")
        p = self.crew("merge", "m1")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        with open(os.path.join(self.repo, "delivered.txt")) as fh:
            self.assertEqual(fh.read(), "from the worker")

    def test_merge_works_after_the_worktree_has_been_reaped(self):
        # The order that always works is stop, reap, merge: workers leave
        # their changes uncommitted and reap is what commits them. So merge
        # has to work from the branch alone, with no worktree left.
        self.crew("spawn", "f", "a task", "--name", "m5", "--no-task")
        wt = self.workers()["m5"]["cwd"]
        self.commit_in(wt, "reaped.txt", "survived the reap")
        branch = subprocess.run(["git", "-C", wt, "rev-parse", "--abbrev-ref",
                                 "HEAD"], capture_output=True, text=True).stdout.strip()
        self.crew("stop", "m5")
        subprocess.run(["git", "-C", self.repo, "worktree", "remove", "--force",
                        wt], capture_output=True)

        p = self.crew("merge", branch)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        with open(os.path.join(self.repo, "reaped.txt")) as fh:
            self.assertEqual(fh.read(), "survived the reap")

    def test_merge_names_an_unfinished_merge_rather_than_blaming_you(self):
        # Mid-conflict the checkout is dirty, so the plain dirty message would
        # tell you to commit or stash, which is wrong and destructive advice
        # while a merge is half-applied.
        self.crew("spawn", "f", "a task", "--name", "m6", "--no-task")
        wt = self.workers()["m6"]["cwd"]
        self.commit_in(wt, "README.md", "worker's version")
        self.commit_in(self.repo, "README.md", "the repo's version")
        p = self.crew("merge", "m6")
        self.assertNotEqual(p.returncode, 0, "expected a conflict")

        p = self.crew("merge", "m6")
        self.assertIn("merge is already in progress", p.stdout)
        self.assertIn("merge --abort", p.stdout)
        subprocess.run(["git", "-C", self.repo, "merge", "--abort"],
                       capture_output=True)

    def test_merge_rejects_a_name_that_is_neither_worker_nor_branch(self):
        p = self.crew("merge", "not-a-thing")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("no worker or branch", p.stdout)

    def test_merge_refuses_while_the_worker_still_has_uncommitted_work(self):
        self.crew("spawn", "f", "a task", "--name", "m2", "--no-task")
        wt = self.workers()["m2"]["cwd"]
        with open(os.path.join(wt, "half-done.txt"), "w") as fh:
            fh.write("not committed")
        p = self.crew("merge", "m2")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("uncommitted", p.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "half-done.txt")))

    def test_merge_refuses_into_a_dirty_checkout(self):
        # Merging into a dirty tree mixes the human's work with the worker's,
        # and makes backing the merge out much harder than refusing.
        self.crew("spawn", "f", "a task", "--name", "m3", "--no-task")
        self.commit_in(self.workers()["m3"]["cwd"], "theirs.txt", "worker")
        with open(os.path.join(self.repo, "README.md"), "w") as fh:
            fh.write("edited by the human, not committed")
        p = self.crew("merge", "m3")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("uncommitted changes", p.stdout)

    def test_merge_is_not_blocked_by_crews_own_worktree_directory(self):
        # .crew/worktrees lives inside the repo and is untracked, so a check
        # that counted untracked files would refuse every merge forever.
        self.crew("spawn", "f", "a task", "--name", "m4", "--no-task")
        self.commit_in(self.workers()["m4"]["cwd"], "fine.txt", "ok")
        p = self.crew("merge", "m4")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_wait_answers_a_routine_permission_prompt_itself(self):
        # The director is the only thing that should interrupt the person, so
        # "may I run this" is answered rather than relayed.
        spec = fake("fake-agent-approval", approval={
            "prompt": [r"Do you want to proceed"], "always": "2", "once": "1"})
        self.write_config({
            "agents": {"appr": spec},
            "roles": {"a": {"agent": "appr", "permission": "edit"}},
            "ready_timeout": 25, "auto_approve": True,
        })
        self.crew("spawn", "a", "a task", "--name", "ap1", "--no-task")
        self.fake_session("ap1", "needsInput")
        p = self.crew("wait", "--timeout", "12")
        self.assertIn("approved its permission request", p.stdout,
                      p.stdout + p.stderr)
        self.assertIn("APPROVED-2", self.screen("ap1"))

    def test_a_login_screen_is_never_answered_automatically(self):
        # The same prompt-shaped screen, but it is a login. Answering one
        # starts a browser sign-in flow and can clear stored credentials.
        spec = fake("fake-agent-approval", approval={
            "prompt": [r"Do you want to proceed"], "always": "2", "once": "1"},
            blocked=[r"Press any key to log in", r"Do you want to proceed"])
        self.write_config({
            "agents": {"appr": spec},
            "roles": {"a": {"agent": "appr", "permission": "edit"}},
            "ready_timeout": 25, "auto_approve": True,
        })
        self.crew("spawn", "a", "a task", "--name", "ap2", "--no-task")
        self.fake_session("ap2", "needsInput")
        self.crew("wait", "--timeout", "8")
        self.assertNotIn("APPROVED", self.screen("ap2"),
                         "a blocked screen was answered automatically")

    def test_show_prints_what_a_worker_is_asking(self):
        # status says a worker is waiting. This is how the director finds out
        # what it is waiting for, which is the whole of "surface it to the
        # human" rather than guessing.
        self.crew("spawn", "f", "a task", "--no-task")
        name = next(iter(self.workers()))
        p = self.crew("show", name)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("for shortcuts", p.stdout,
                      "show did not print the worker's screen")

    def test_show_refuses_a_worker_that_is_not_there(self):
        p = self.crew("show", "no-such-worker")
        self.assertNotEqual(p.returncode, 0)

    def test_say_never_types_into_the_shell_a_dead_agent_left_behind(self):
        # The worst case is not a worker that is merely gone. It is a tab
        # sitting at a shell prompt: a shell echoes whatever is typed at it,
        # so "the text appeared on screen" cannot tell a composer from a
        # command line, and pressing Enter runs the message.
        #
        # Nothing recorded a hook session here, because the agent died before
        # one existed, which is exactly the case the pid check cannot cover.
        marker = os.path.join(self.tmp, "executed")
        self.crew("spawn", "fcrash", "task")
        self.assertIn("fcrash", " ".join(self.workers()), "worker was not registered")
        name = next(iter(self.workers()))

        p = self.crew("say", name, f"touch {marker}")
        time.sleep(1.5)

        self.assertFalse(os.path.exists(marker),
                         "the message was executed as a shell command")
        self.assertNotEqual(p.returncode, 0, "say reported success to a dead worker")

    def test_say_delivers_to_a_ready_worker(self):
        self.crew("spawn", "f", "first", "--name", "w6", "--no-task")
        p = self.crew("say", "w6", "second message")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("GOT: second message", self.screen("w6"))

    def test_say_refuses_to_interrupt_a_running_worker(self):
        self.crew("spawn", "f", "first", "--name", "w7", "--no-task")
        self.fake_session("w7", "running")
        p = self.crew("say", "w7", "do not interrupt")
        self.assertEqual(p.returncode, 1)
        self.assertIn("mid-turn", p.stdout)
        self.assertNotIn("GOT: do not interrupt", self.screen("w7"))

    def test_say_now_interrupts_a_running_worker(self):
        self.crew("spawn", "f", "first", "--name", "w8", "--no-task")
        self.fake_session("w8", "running")
        p = self.crew("say", "w8", "urgent", "--now")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("GOT: urgent", self.screen("w8"))

    def test_status_reports_the_state_cmux_sees(self):
        self.crew("spawn", "f", "first", "--name", "w9", "--no-task")
        self.fake_session("w9", "needsInput")
        p = self.crew("status")
        self.assertIn("needsInput", p.stdout)
        self.assertIn("waiting on you: w9", p.stdout)

    def test_status_says_gone_when_the_process_has_died(self):
        self.crew("spawn", "f", "first", "--name", "w10", "--no-task")
        self.fake_session("w10", "idle", pid=999999)
        p = self.crew("status")
        self.assertIn("gone", p.stdout)

    def test_wait_returns_as_soon_as_a_worker_goes_idle(self):
        self.crew("spawn", "f", "first", "--name", "w11", "--no-task")
        self.fake_session("w11", "idle")
        p = self.crew("wait", "--timeout", "10")
        self.assertEqual(p.returncode, 0)
        self.assertIn("w11", p.stdout)
        self.assertIn("idle", p.stdout)

    def test_wait_reports_each_change_once(self):
        # Otherwise a director looping on wait spins on the same news forever.
        self.crew("spawn", "f", "first", "--name", "w12", "--no-task")
        self.fake_session("w12", "idle")
        first = self.crew("wait", "--timeout", "5")
        self.assertIn("w12", first.stdout)
        again = self.crew("wait", "--timeout", "5", "--poll", "0.5")
        self.assertIn("nothing changed", again.stdout)

    def test_max_workers_is_enforced(self):
        cfg = json.load(open(self.cfg_path))
        cfg["max_workers"] = 1
        self.write_config(cfg)
        self.crew("spawn", "f", "one", "--name", "w13", "--no-task")
        self.fake_session("w13", "idle")
        p = self.crew("spawn", "f", "two", "--name", "w14", "--no-task")
        self.assertEqual(p.returncode, 1, "a refused spawn should exit non-zero")
        self.assertIn("limit is 1", p.stdout)
        self.assertNotIn("w14", self.workers(), "a refused worker was recorded")

    def test_a_worker_making_no_progress_is_reported_as_stalled(self):
        # Running is not the same as working. A wedged agent keeps saying it
        # is busy, so the screen has to be the evidence.
        self.crew("spawn", "fstall", "spin", "--name", "s1", "--no-task")
        self.fake_session("s1", "running")
        self.crew("status")           # first sample records the screen
        time.sleep(2)
        p = self.crew("status")       # unchanged since, and still "running"
        self.assertIn("stalled", p.stdout)
        self.assertIn("no visible progress", p.stdout)

    def test_a_busy_worker_is_not_called_stalled(self):
        self.crew("spawn", "f", "work", "--name", "s2", "--no-task")
        self.fake_session("s2", "running")
        self.crew("status")
        time.sleep(2)
        self.crew("say", "s2", "progress", "--now")   # screen changes
        p = self.crew("status")
        self.assertNotIn("stalled", p.stdout)

    def test_a_worker_reporting_a_plan_limit_is_flagged(self):
        self.crew("spawn", "fquota", "anything", "--name", "q1", "--no-task")
        p = self.crew("status")
        self.assertIn("quota", p.stdout)
        self.assertIn("plan allowance", p.stdout)

    def test_spawn_refuses_to_pile_onto_an_exhausted_agent(self):
        # The failure this prevents: a director reacting to a stuck worker by
        # starting three more on the same exhausted account.
        self.crew("spawn", "fquota", "one", "--name", "q2", "--no-task")
        p = self.crew("spawn", "fquota", "two", "--name", "q3", "--no-task")
        self.assertEqual(p.returncode, 1)
        self.assertIn("out of plan allowance", p.stdout)
        self.assertNotIn("q3", self.workers())

    def test_a_different_agent_is_still_allowed_when_one_is_exhausted(self):
        self.crew("spawn", "fquota", "one", "--name", "q4", "--no-task")
        p = self.crew("spawn", "f", "two", "--name", "q5", "--no-task")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_wait_wakes_for_a_stall(self):
        self.crew("spawn", "fstall", "spin", "--name", "s3", "--no-task")
        self.fake_session("s3", "running")
        self.crew("status")
        time.sleep(2)
        p = self.crew("wait", "--timeout", "10", "--poll", "0.5")
        self.assertIn("s3", p.stdout)
        self.assertIn("stalled", p.stdout)

    def test_worktree_is_created_for_agents_that_cannot_make_their_own(self):
        self.crew("spawn", "f", "thing", "--name", "w15", "--no-task")
        wt = os.path.join(self.repo, ".crew", "worktrees", "w15")
        self.assertTrue(os.path.isdir(wt), "no worktree was created")
        out = subprocess.run(["git", "worktree", "list"], cwd=self.repo,
                             capture_output=True, text=True).stdout
        self.assertIn("crew/w15", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
