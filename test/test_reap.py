#!/usr/bin/env python3
"""Behavioural tests for crew-reap.

Each test builds a real git repo with real worktrees in a temp directory and
runs the real script, because the failures worth catching are git's, not
Python's. Nothing here touches the user's repos.

Run: python3 test/test_reap.py
"""

import importlib.machinery
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
import unittest.mock as mock

REAP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin", "crew-reap")


def reap_module():
    """Import crew-reap for the few things worth testing as functions."""
    import importlib.util
    spec = importlib.util.spec_from_loader(
        "crew_reap", importlib.machinery.SourceFileLoader("crew_reap", REAP))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def git(repo, *args, check=True):
    p = subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=repo, capture_output=True, text=True,
    )
    if check and p.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {p.stdout}{p.stderr}")
    return p.stdout.strip()


class PidAliveTest(unittest.TestCase):
    """A pid we cannot signal is not the same as a pid that is gone."""

    def setUp(self):
        self.reap = reap_module()

    def test_permission_denied_is_alive_but_no_such_process_is_dead(self):
        with mock.patch.object(os, "kill", side_effect=PermissionError):
            self.assertTrue(self.reap.pid_alive(123))
        with mock.patch.object(os, "kill", side_effect=ProcessLookupError):
            self.assertFalse(self.reap.pid_alive(123))


class ReapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="crew-test-")
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        git(self.repo, "init", "-q", "-b", "main")
        self.write("README.md", "hello")
        self.write(".gitignore", "build/\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "init")
        self.cfg_path = os.path.join(self.tmp, "cfg.json")
        self.config({})

    def tearDown(self):
        for wt in self._worktree_paths():
            if wt != self.repo:
                subprocess.run(["git", "worktree", "remove", "--force", wt],
                               cwd=self.repo, capture_output=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # helpers ------------------------------------------------------------
    def write(self, rel, content, root=None):
        path = os.path.join(root or self.repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(content)
        return path

    def config(self, extra):
        cfg = {"roots": [], "idle_minutes": 0}
        cfg.update(extra)
        with open(self.cfg_path, "w") as fh:
            json.dump(cfg, fh)

    def worktree(self, name, branch=None, detach=False):
        path = os.path.join(self.tmp, name)
        args = ["worktree", "add", "-q"]
        if detach:
            args += ["--detach", path]
        elif branch:
            args += [path, branch]
        else:
            args += [path, "-b", name]
        git(self.repo, *args)
        return path

    def _worktree_paths(self):
        out = subprocess.run(["git", "worktree", "list", "--porcelain"],
                             cwd=self.repo, capture_output=True, text=True).stdout
        return [l.split(" ", 1)[1] for l in out.splitlines() if l.startswith("worktree ")]

    def reap(self, *extra):
        env = dict(os.environ, CREW_REAP_CONFIG=self.cfg_path)
        p = subprocess.run([REAP, "--repo", self.repo, *extra],
                           capture_output=True, text=True, env=env, cwd=self.tmp)
        return p.stdout + p.stderr

    def branches(self):
        return set(git(self.repo, "branch", "--format=%(refname:short)").split())

    # tests --------------------------------------------------------------
    def test_dry_run_changes_nothing(self):
        wt = self.worktree("idle")
        before = self.branches()
        out = self.reap()
        self.assertIn("dry run", out)
        self.assertTrue(os.path.isdir(wt), "dry run removed the worktree")
        self.assertEqual(before, self.branches())

    def test_clean_worktree_is_removed_and_its_branch_survives(self):
        wt = self.worktree("clean")
        self.write("build/big.bin", "x" * 5000, root=wt)  # gitignored
        self.reap("--apply")
        self.assertFalse(os.path.exists(wt), "worktree was not removed")
        self.assertIn("clean", self.branches(), "branch was deleted with the worktree")

    def test_uncommitted_work_is_committed_to_its_branch(self):
        wt = self.worktree("dirty")
        self.write("notes.md", "work that must survive", root=wt)
        self.reap("--apply")
        self.assertFalse(os.path.exists(wt))
        blob = git(self.repo, "show", "dirty:notes.md")
        self.assertEqual(blob, "work that must survive")

    def test_detached_head_work_is_saved_to_a_branch(self):
        wt = self.worktree("orphan", detach=True)
        self.write("orphan.md", "unreachable otherwise", root=wt)
        self.reap("--apply")
        self.assertIn("crew-rescued/orphan", self.branches(),
                      "detached commit was left unreachable")
        self.assertEqual(git(self.repo, "show", "crew-rescued/orphan:orphan.md"),
                         "unreachable otherwise")

    def test_protected_branch_is_never_advanced(self):
        git(self.repo, "switch", "-qc", "side")  # free up main
        wt = self.worktree("on-main", branch="main")
        self.write("leftovers.txt", "agent junk", root=wt)
        before = git(self.repo, "rev-parse", "main")
        self.reap("--apply")
        self.assertEqual(before, git(self.repo, "rev-parse", "main"),
                         "agent leftovers were committed onto main")
        self.assertIn("crew-rescued/on-main", self.branches())
        self.assertEqual(git(self.repo, "show", "crew-rescued/on-main:leftovers.txt"),
                         "agent junk")

    def test_a_clean_detached_worktree_keeps_its_commits(self):
        # Found by review: the rescue only ran on the dirty path, so a
        # detached worktree whose work was already committed was removed and
        # its commits became unreachable.
        wt = self.worktree("orphan-clean", detach=True)
        self.write("done.txt", "committed work", root=wt)
        git(wt, "add", "-A")
        git(wt, "commit", "-qm", "work the agent committed itself")
        sha = git(wt, "rev-parse", "HEAD")

        self.reap("--apply")
        self.assertFalse(os.path.exists(wt))
        self.assertIn("crew-rescued/orphan-clean", self.branches(),
                      "a clean detached worktree lost its commits")
        self.assertEqual(git(self.repo, "rev-parse", "crew-rescued/orphan-clean"),
                         sha)

    def test_a_colliding_rescue_branch_blocks_removal(self):
        # A rescue branch from an earlier reap can already occupy
        # crew-rescued/<name> pointing at a different commit. Removing the
        # worktree anyway would make its detached commit unreachable.
        wt = self.worktree("orphan-collide", detach=True)
        self.write("orphan.md", "unreachable if removed", root=wt)
        git(wt, "add", "-A")
        git(wt, "commit", "-qm", "detached work")
        detached_sha = git(wt, "rev-parse", "HEAD")

        git(self.repo, "branch", "crew-rescued/orphan-collide", "main")
        collider_sha = git(self.repo, "rev-parse", "crew-rescued/orphan-collide")
        self.assertNotEqual(detached_sha, collider_sha)

        self.reap("--apply")

        self.assertTrue(os.path.isdir(wt),
                        "worktree was removed despite a colliding rescue branch")
        self.assertEqual(git(self.repo, "rev-parse", "crew-rescued/orphan-collide"),
                         collider_sha, "the colliding branch was moved")
        self.assertEqual(git(wt, "rev-parse", "HEAD"), detached_sha,
                         "the detached commit is no longer reachable from the worktree")

    def test_a_lock_set_by_a_person_is_obeyed(self):
        # Found by review: any lock without a live pid was cleared and the
        # worktree removed, which defeats the point of locking one.
        wt = self.worktree("precious")
        self.write("keep.env", "secrets", root=wt)
        git(self.repo, "worktree", "lock", "--reason", "do not delete", wt)
        out = self.reap("--apply")
        self.assertTrue(os.path.isdir(wt), "a hand-locked worktree was removed")
        self.assertIn("locked by hand", out)

    def test_the_real_primary_is_safe_when_pointed_at_a_worktree(self):
        # Found by review: the guard compared against whatever --repo was, so
        # aiming it at a linked worktree made the actual checkout a candidate.
        wt = self.worktree("linked")
        # Uncommitted work in the primary, the way a person's checkout looks.
        # Without it nothing is committed and the bug hides.
        self.write("README.md", "work in progress, not committed")
        before = git(self.repo, "rev-parse", "--abbrev-ref", "HEAD")

        env = dict(os.environ, CREW_REAP_CONFIG=self.cfg_path)
        p = subprocess.run([REAP, "--repo", wt, "--apply"],
                           capture_output=True, text=True, env=env, cwd=self.tmp)

        self.assertTrue(os.path.isdir(self.repo), "the primary checkout was removed")
        self.assertNotIn("crew-rescued/repo", self.branches(),
                         "the primary's uncommitted work was committed to a "
                         "rescue branch")
        self.assertEqual(git(self.repo, "rev-parse", "--abbrev-ref", "HEAD"), before,
                         "the primary checkout was moved off its branch")
        self.assertEqual(git(self.repo, "--no-optional-locks", "status",
                             "--porcelain").count("README.md"), 1,
                         "the primary's uncommitted change was taken away")

    def test_missing_repo_path_does_not_abort_other_repos(self):
        # A typo'd or removed --repo path used to crash the whole run with a
        # Traceback (primary_checkout's subprocess.run(cwd=...) raises
        # FileNotFoundError for a nonexistent cwd), so a valid repo passed
        # alongside it never got reaped.
        wt = self.worktree("idle")
        missing = os.path.join(self.tmp, "does-not-exist")

        env = dict(os.environ, CREW_REAP_CONFIG=self.cfg_path)
        p = subprocess.run(
            [REAP, "--repo", self.repo, "--repo", missing, "--apply"],
            capture_output=True, text=True, env=env, cwd=self.tmp)
        out = p.stdout + p.stderr

        self.assertNotIn("Traceback", out)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn(missing, out)
        self.assertFalse(os.path.exists(wt), "the valid repo's worktree was not reaped")

    def test_overlapping_repo_args_are_deduplicated(self):
        # Passing the primary checkout and one of its own linked worktrees as
        # two --repo values both normalize (via primary_checkout) to the same
        # repo. Without deduplication that repo's worktrees are listed and
        # planned twice, so each candidate shows up twice in the output.
        idle = self.worktree("idle")
        editor = self.worktree("editor")

        out = self.reap("--repo", editor)
        self.assertEqual(out.count(idle), 1,
                         "worktree appeared more than once after overlapping --repo")
        self.assertEqual(out.count(editor), 1,
                         "linked worktree appeared more than once after overlapping --repo")

        p = subprocess.run(
            [REAP, "--repo", self.repo, "--repo", editor, "--apply"],
            capture_output=True, text=True,
            env=dict(os.environ, CREW_REAP_CONFIG=self.cfg_path), cwd=self.tmp)
        applied = p.stdout + p.stderr
        self.assertNotIn("Traceback", applied)
        self.assertEqual(p.returncode, 0)
        self.assertFalse(os.path.exists(idle))

    def test_run_from_inside_a_linked_worktree_finds_the_repo(self):
        # Run from inside a linked worktree with no --repo (roots guessed):
        # resolve_roots used to take the worktree's own parent directory. For
        # a worktree nested under the primary checkout, the way crew itself
        # creates them (<repo>/.claude/worktrees/<name>), that parent holds
        # no repo with a real .git directory, so the primary checkout and its
        # worktrees went undiscovered.
        wt = os.path.join(self.repo, ".claude", "worktrees", "idle")
        os.makedirs(os.path.dirname(wt))
        git(self.repo, "worktree", "add", "-q", wt, "-b", "idle")
        env = dict(os.environ, CREW_REAP_CONFIG=self.cfg_path)
        p = subprocess.run([REAP], capture_output=True, text=True, env=env, cwd=wt)
        out = p.stdout + p.stderr
        self.assertNotIn("no git repos found", out)
        self.assertIn(wt, out)

    def test_oversized_file_is_refused_and_worktree_kept(self):
        self.config({"max_file_mb": 1})
        wt = self.worktree("heavy")
        self.write("huge.bin", "x" * (2 * 1024 * 1024), root=wt)
        out = self.reap("--apply")
        self.assertTrue(os.path.isdir(wt), "worktree with an oversized file was removed")
        self.assertIn("over the 1MB per-file limit", out)

    def test_oversized_unicode_named_file_is_refused_and_worktree_kept(self):
        self.config({"max_file_mb": 1})
        wt = self.worktree("heavy-unicode")
        self.write("文件.bin", "x" * (2 * 1024 * 1024), root=wt)
        out = self.reap("--apply")
        self.assertTrue(os.path.isdir(wt), "worktree with an oversized Unicode-named file was removed")
        self.assertIn("over the 1MB per-file limit", out)

    def test_worktree_in_use_is_skipped(self):
        wt = self.worktree("busy")
        proc = subprocess.Popen(["python3", "-c", "import time; time.sleep(60)"], cwd=wt)
        try:
            out = self.reap("--apply")
            self.assertTrue(os.path.isdir(wt), "reaped a worktree that was in use")
            self.assertIn("in use by a running process", out)
        finally:
            proc.kill()
            proc.wait()

    def test_locked_by_dead_pid_is_unlocked_and_removed(self):
        wt = self.worktree("stale-lock")
        git(self.repo, "worktree", "lock", "--reason",
            "claude session x (pid 999999 start Wed Aug 12 00:00:00 2026)", wt)
        self.reap("--apply")
        self.assertFalse(os.path.exists(wt), "a lock from a dead pid blocked removal")

    def test_rescue_copies_only_files_the_primary_lacks(self):
        self.write("build/keep/shared.png", "original")
        self.config({"rescue": {os.path.realpath(self.repo): ["build/keep"]}})
        wt = self.worktree("art")
        self.write("build/keep/shared.png", "DIFFERENT", root=wt)
        self.write("build/keep/unique.png", "paid for this", root=wt)
        self.reap("--apply")
        with open(os.path.join(self.repo, "build/keep/unique.png")) as fh:
            self.assertEqual(fh.read(), "paid for this", "unique file was destroyed")
        with open(os.path.join(self.repo, "build/keep/shared.png")) as fh:
            self.assertEqual(fh.read(), "original", "existing file was overwritten")

    def test_rescue_still_finds_primary_when_repo_is_a_linked_worktree(self):
        # Found by review: passing --repo a linked worktree path used that
        # path, not the primary checkout, both to look up the configured
        # rescue list and as the copy destination. The configured rescue was
        # missed and any rescue that did trigger would have landed inside the
        # linked worktree instead of the primary.
        self.write("build/keep/shared.png", "original")
        self.config({"rescue": {os.path.realpath(self.repo): ["build/keep"]}})
        editor = self.worktree("editor")
        git(self.repo, "worktree", "lock", "--reason", "keep for this test", editor)
        wt = self.worktree("art")
        self.write("build/keep/unique.png", "paid for this", root=wt)

        env = dict(os.environ, CREW_REAP_CONFIG=self.cfg_path)
        subprocess.run([REAP, "--repo", editor, "--apply"],
                       capture_output=True, text=True, env=env, cwd=self.tmp)

        self.assertTrue(
            os.path.exists(os.path.join(self.repo, "build/keep/unique.png")),
            "rescue configured for the primary was missed when --repo named "
            "a linked worktree")
        self.assertFalse(
            os.path.exists(os.path.join(editor, "build/keep/unique.png")),
            "rescued file landed in the linked worktree instead of the primary")

    def test_repo_local_config_is_honoured(self):
        self.write(".crew.json", json.dumps({"rescue": ["build/keep"]}))
        wt = self.worktree("art2")
        self.write("build/keep/unique.png", "from repo config", root=wt)
        self.reap("--apply")
        self.assertTrue(os.path.exists(os.path.join(self.repo, "build/keep/unique.png")),
                        ".crew.json rescue list was ignored")

    def test_a_rescue_path_cannot_reach_outside_the_repo(self):
        # .crew.json is checked in, so its rescue list is whatever the repo's
        # author wrote. A ../ entry made reap read from one place and write to
        # another, both outside the repo, planting files anywhere nothing
        # existed yet. The worktree sits deeper than the repo here because
        # that is what makes the escaped source and destination differ.
        self.write(".crew.json", json.dumps({"rescue": ["../outside/planted"]}))
        wt = self.worktree("nest/deep/wt")
        self.write("evil.txt", "planted",
                   root=os.path.join(wt, "..", "outside", "planted"))
        escaped = os.path.join(self.tmp, "outside", "planted", "evil.txt")

        out = self.reap("--apply")

        self.assertFalse(os.path.exists(escaped),
                         f"a rescue path escaped the repo and planted a file:\n{out}")

    def test_scanning_does_not_make_worktrees_look_active(self):
        # Backdate the worktree's git metadata so it starts out plainly idle.
        # Without that, a freshly created worktree is correctly "active now"
        # and the test cannot tell self-poisoning from the truth.
        self.config({"idle_minutes": 60})
        self.worktree("quiet")
        gitdir = os.path.join(self.repo, ".git", "worktrees", "quiet")
        old = time.time() - 3 * 3600
        for name in ("index", "HEAD", "ORIG_HEAD"):
            p = os.path.join(gitdir, name)
            if os.path.exists(p):
                os.utime(p, (old, old))

        first = self.reap()
        self.assertNotIn("idle window", first, "backdating did not take effect")
        second = self.reap()
        self.assertNotIn("idle window", second,
                         "the first scan made the worktree look freshly active")
        self.assertEqual(first.count("remove"), second.count("remove"))

    def _run_with_fake_path(self, *extra, lsof_body=None):
        """Run with a PATH where lsof is missing, or replaced by lsof_body."""
        fake = os.path.join(self.tmp, "fakebin")
        os.makedirs(fake, exist_ok=True)
        for real in ("git", "python3"):
            found = shutil.which(real)
            if found and not os.path.exists(os.path.join(fake, real)):
                os.symlink(found, os.path.join(fake, real))
        if lsof_body is not None:
            p = os.path.join(fake, "lsof")
            with open(p, "w") as fh:
                fh.write(lsof_body)
            os.chmod(p, 0o755)
        env = dict(os.environ, CREW_REAP_CONFIG=self.cfg_path, PATH=fake)
        return subprocess.run([REAP, "--repo", self.repo, *extra],
                              capture_output=True, text=True, env=env, cwd=self.tmp)

    def test_refuses_to_run_without_lsof(self):
        wt = self.worktree("safe")
        p = self._run_with_fake_path("--apply")
        self.assertEqual(p.returncode, 2)
        self.assertIn("lsof", p.stdout + p.stderr)
        self.assertTrue(os.path.isdir(wt), "reaped without being able to check liveness")

    def test_refuses_to_run_when_lsof_returns_nothing(self):
        # A silent lsof failure is the dangerous case: an empty result would
        # otherwise read as "no process is using anything".
        wt = self.worktree("safe2")
        p = self._run_with_fake_path("--apply", lsof_body="#!/bin/sh\nexit 1\n")
        self.assertEqual(p.returncode, 2)
        self.assertTrue(os.path.isdir(wt), "an empty liveness result was treated as idle")

    def test_refuses_a_listing_that_does_not_include_its_own_process(self):
        # A truncated listing is the dangerous one: it looks like a complete
        # answer and every worktree it omits reads as idle. The exit code
        # cannot spot it, measured: lsof returns 0 with a complete listing and
        # 1 with a complete listing alike. It does always report the calling
        # process's own working directory, so a listing lacking that was cut
        # short. This fake returns one plausible entry and exits 0.
        wt = self.worktree("safe3")
        p = self._run_with_fake_path("--apply", lsof_body="#!/bin/sh\necho n/usr\n")
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertTrue(os.path.isdir(wt),
                        "a truncated liveness listing was treated as complete")

    def test_session_registry_protects_a_worktree_with_no_live_process(self):
        # A paused-but-resumable session leaves no process, so this is the
        # only thing standing between it and removal.
        wt = self.worktree("paused")
        reg = os.path.join(self.tmp, "sessions.json")
        with open(reg, "w") as fh:
            json.dump({"sessions": {"abc": {"cwd": wt, "pid": os.getpid()}}}, fh)
        self.config({"session_registries": [reg]})
        out = self.reap("--apply")
        self.assertTrue(os.path.isdir(wt), "reaped a worktree a live session claims")
        self.assertIn("in use by a running process", out)

    def test_session_registry_entry_for_a_dead_pid_does_not_protect(self):
        wt = self.worktree("finished")
        reg = os.path.join(self.tmp, "sessions.json")
        with open(reg, "w") as fh:
            json.dump({"sessions": {"abc": {"cwd": wt, "pid": 999999}}}, fh)
        self.config({"session_registries": [reg]})
        self.reap("--apply")
        self.assertFalse(os.path.exists(wt), "a dead session blocked removal forever")

    def test_hard_links_are_counted_once(self):
        # Build directories are full of hard links, and one inode is freed
        # once however many names point at it.
        d = os.path.join(self.tmp, "links")
        os.makedirs(d)
        real = os.path.join(d, "original.bin")
        with open(real, "w") as fh:
            fh.write("x" * 400000)
        for i in range(4):
            os.link(real, os.path.join(d, f"link{i}.bin"))
        self.assertEqual(reap_module().dir_size(d), 400000)

    def test_nested_git_repo_is_skipped_not_staged_as_gitlink(self):
        # A nested repo (e.g. a cloned dependency, or another agent's
        # worktree left inside this one) has its own .git. `git add -A`
        # doesn't add its files: it stages the nested repo itself as a
        # commit reference (a gitlink, mode 160000) pointing at a commit
        # that lives nowhere else, a dangling reference the moment this
        # worktree is removed.
        wt = self.worktree("has-nested")
        nested = os.path.join(wt, "vendor", "nested")
        os.makedirs(nested)
        git(nested, "init", "-q", "-b", "main")
        self.write("f.txt", "nested repo content", root=nested)
        git(nested, "add", "-A")
        git(nested, "commit", "-qm", "nested init")

        out = self.reap("--apply")

        self.assertTrue(os.path.isdir(wt), "worktree with a nested repo was removed")
        self.assertTrue(os.path.exists(os.path.join(nested, "f.txt")),
                        "nested repo's file did not survive")
        self.assertIn("skip", out)
        self.assertNotIn("FAILED", out)
        self.assertIn("has-nested", self.branches(), "branch was removed")
        ls_tree = git(self.repo, "ls-tree", "-r", "has-nested")
        self.assertNotIn("160000", ls_tree, "a gitlink was committed to the branch")

    def test_worktree_holding_another_worktree_is_not_removed(self):
        # crew puts worktrees at <repo>/.claude/worktrees/<name>, which is
        # gitignored, so an agent that itself spawns a worktree ends up with
        # one nested inside another. git status never mentions the inner one,
        # so the outer looks clean and `git worktree remove` deletes the whole
        # tree, inner worktree and its uncommitted work with it.
        outer = self.worktree("outer")
        inner = os.path.join(outer, "build", "inner")  # build/ is gitignored
        git(self.repo, "worktree", "add", "-q", inner, "-b", "inner")
        self.write("notes.md", "work inside the inner worktree", root=inner)
        # The inner one is spoken for, so nothing else would preserve it.
        git(self.repo, "worktree", "lock", "--reason", "do not delete", inner)

        self.reap("--apply")

        self.assertTrue(os.path.exists(os.path.join(inner, "notes.md")),
                        "the inner worktree's uncommitted work was destroyed")
        self.assertTrue(os.path.isdir(outer),
                        "a worktree holding another worktree was removed")

    def test_pruning_a_vanished_detached_worktree_keeps_its_commits(self):
        # A worktree record is a gc root for its HEAD. Someone rm -rf'd the
        # directory, so the commits now hang off nothing but that record, and
        # `git worktree prune` is what makes them collectable.
        wt = self.worktree("orphan-gone", detach=True)
        self.write("done.txt", "committed, then the directory was deleted", root=wt)
        git(wt, "add", "-A")
        git(wt, "commit", "-qm", "detached work")
        sha = git(wt, "rev-parse", "HEAD")
        shutil.rmtree(wt)

        self.reap("--apply")

        self.assertTrue(git(self.repo, "branch", "--contains", sha, check=False),
                        "the detached commits are no longer reachable from any ref")

    def test_a_worktree_removed_mid_run_fails_cleanly(self):
        # Another agent can remove its own worktree between the plan and the
        # apply. subprocess.run(cwd=<gone>) raises FileNotFoundError, which
        # aborted the whole pass, so every worktree queued behind this one was
        # silently left alone.
        reap = reap_module()
        wt = self.worktree("vanishing")
        self.write("notes.md", "work", root=wt)
        cfg = dict(reap.DEFAULTS, idle_minutes=0)
        wtd = next(t for t in reap.list_worktrees(self.repo)
                   if os.path.basename(t["path"]) == "vanishing")
        d = reap.plan_worktree(self.repo, wtd, cfg, set())
        self.assertEqual(d["action"], "commit+remove")

        shutil.rmtree(wt)
        ok, msg = reap.apply_plan(d)

        self.assertFalse(ok, "reported success for a worktree that had gone")
        self.assertTrue(msg)

    def test_a_deeply_nested_session_registry_does_not_abort_the_run(self):
        # Registry files belong to other tools and are read as found. A
        # syntactically valid but very deep one blows json's recursion limit,
        # and RecursionError is not a ValueError, so it escaped the guard and
        # took the whole run with it.
        wt = self.worktree("idle")
        reg = os.path.join(self.tmp, "sessions.json")
        with open(reg, "w") as fh:
            fh.write("[" * 20000 + "]" * 20000)
        self.config({"session_registries": [reg]})

        out = self.reap("--apply")

        self.assertNotIn("Traceback", out)
        self.assertFalse(os.path.exists(wt),
                         "an unreadable registry stopped anything being reaped")

    def test_an_out_of_range_pid_in_a_registry_does_not_abort_the_run(self):
        # os.kill() with a pid too large for a C pid_t raises OverflowError,
        # which pid_alive did not catch.
        wt = self.worktree("idle")
        reg = os.path.join(self.tmp, "sessions.json")
        with open(reg, "w") as fh:
            json.dump({"sessions": {"a": {"cwd": wt, "pid": 10 ** 30}}}, fh)
        self.config({"session_registries": [reg]})

        out = self.reap("--apply")

        self.assertNotIn("Traceback", out)
        self.assertFalse(os.path.exists(wt),
                         "a nonsense pid protected the worktree forever")

    def test_a_worktree_path_with_a_newline_is_parsed_and_reaped(self):
        # `git worktree list --porcelain` prints paths raw. A newline in one
        # splits the record: the head of the path became the "path", which
        # does not exist on disk, so the live worktree was mistaken for a
        # stale record and its uncommitted work was never committed.
        wt = os.path.join(self.tmp, "new\nline")
        git(self.repo, "worktree", "add", "-q", wt, "-b", "newline-branch")
        self.write("notes.md", "work behind a newline", root=wt)

        self.reap("--apply")

        self.assertEqual(git(self.repo, "show", "newline-branch:notes.md"),
                         "work behind a newline")
        self.assertFalse(os.path.exists(wt), "the worktree was never reaped")

    def test_a_directory_name_that_is_not_a_ref_name_still_rescues(self):
        # The rescue branch was the directory's basename verbatim. Directory
        # names allow characters ref names do not, so `git branch` refused and
        # the worktree could never be reaped: every run failed the same way.
        wt = self.worktree("has space", detach=True)
        self.write("done.txt", "committed detached work", root=wt)
        git(wt, "add", "-A")
        git(wt, "commit", "-qm", "detached work")
        sha = git(wt, "rev-parse", "HEAD")

        out = self.reap("--apply")

        self.assertTrue(git(self.repo, "branch", "--contains", sha, check=False),
                        "the detached commits were left unreachable")
        self.assertFalse(os.path.exists(wt), f"the worktree was never reaped:\n{out}")

    def test_second_apply_is_a_no_op(self):
        self.worktree("once")
        self.reap("--apply")
        out = self.reap("--apply")
        self.assertIn("nothing to reap", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
