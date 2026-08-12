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
                       "fakecrash": fake("fake-agent-crash")},
            "roles": {"f": {"agent": "fake", "permission": "edit"},
                      "flogin": {"agent": "fakelogin", "permission": "edit"},
                      "fcrash": {"agent": "fakecrash", "permission": "edit"}},
            "ready_timeout": 25,
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

    def test_worktree_is_created_for_agents_that_cannot_make_their_own(self):
        self.crew("spawn", "f", "thing", "--name", "w15", "--no-task")
        wt = os.path.join(self.repo, ".crew", "worktrees", "w15")
        self.assertTrue(os.path.isdir(wt), "no worktree was created")
        out = subprocess.run(["git", "worktree", "list"], cwd=self.repo,
                             capture_output=True, text=True).stdout
        self.assertIn("crew/w15", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
