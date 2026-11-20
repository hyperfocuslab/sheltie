import logging
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
import herdr_remote as hr  # noqa: E402
from test_herdr_remote import TOKEN, make_app, st  # noqa: E402

hr.log.addHandler(logging.NullHandler())
hr.log.propagate = False


class ServerTagTest(unittest.TestCase):
    def test_known_value_matches_app(self):
        # ios NotificationRoutingTests.testServerTagMatchesServerFormula와 같은 값.
        self.assertEqual(hr.server_tag("abc"), "92294253e4dc")
        self.assertNotIn("abc", hr.server_tag("abc"))

    def test_push_payload_carries_tag(self):
        app, hc, pusher = make_app()
        app._notify(hr.build_payload("t1", "n", "done", st("done", "끝")))
        app._flush()
        self.assertEqual(pusher.payloads[-1]["server"], hr.server_tag(TOKEN))


class MachineNameTest(unittest.TestCase):
    def test_strips_local(self):
        orig = hr.socket.gethostname
        try:
            hr.socket.gethostname = lambda: "hfl-mini.local"
            self.assertEqual(hr.machine_name(), "hfl-mini")
            hr.socket.gethostname = lambda: ""
            self.assertIsNone(hr.machine_name())
        finally:
            hr.socket.gethostname = orig


class GitBranchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def write(self, rel, text):
        p = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)
        return p

    def test_branch_from_subfolder(self):
        self.write("repo/.git/HEAD", "ref: refs/heads/feature/x\n")
        os.makedirs(os.path.join(self.tmp, "repo/src/a"))
        self.assertEqual(hr.git_branch(os.path.join(self.tmp, "repo/src/a")), "feature/x")

    def test_detached_head_short_sha(self):
        self.write("repo/.git/HEAD", "0123456789abcdef0123456789abcdef01234567\n")
        self.assertEqual(hr.git_branch(os.path.join(self.tmp, "repo")), "0123456")

    def test_worktree_gitdir_file(self):
        self.write("main/.git/worktrees/wt/HEAD", "ref: refs/heads/claude/wt\n")
        self.write("wt/.git", "gitdir: ../main/.git/worktrees/wt\n")
        self.assertEqual(hr.git_branch(os.path.join(self.tmp, "wt")), "claude/wt")

    def test_not_a_repo_or_bad_input(self):
        os.makedirs(os.path.join(self.tmp, "plain"))
        self.assertIsNone(hr.git_branch(os.path.join(self.tmp, "plain"), max_up=2))
        self.assertIsNone(hr.git_branch(None))
        self.assertIsNone(hr.git_branch("relative/path"))
        self.assertIsNone(hr.git_branch(os.path.join(self.tmp, "missing")))

    def _branch_with_timeout(self, path, timeout=3.0):
        import threading
        out = {}
        th = threading.Thread(target=lambda: out.setdefault("v", hr.git_branch(path)), daemon=True)
        th.start()
        th.join(timeout)
        self.assertFalse(th.is_alive(), "git_branch가 멈췄다")
        return out.get("v")

    def test_fifo_head_is_not_read(self):
        os.makedirs(os.path.join(self.tmp, "repo/.git"))
        os.mkfifo(os.path.join(self.tmp, "repo/.git/HEAD"))
        self.assertIsNone(self._branch_with_timeout(os.path.join(self.tmp, "repo")))

    def test_fifo_gitdir_file_target_is_not_read(self):
        os.makedirs(os.path.join(self.tmp, "gd"))
        os.mkfifo(os.path.join(self.tmp, "gd/HEAD"))
        self.write("wt/.git", "gitdir: ../gd\n")
        self.assertIsNone(self._branch_with_timeout(os.path.join(self.tmp, "wt")))

    def test_odd_head_content_is_dropped(self):
        for text in ["secret-password-line\n", "ref: refs/heads/a b\n", "ref: refs/heads/../../x\n",
                     "ref: refs/heads/" + "a" * 101 + "\n", "0123456789abcdef\n", "ref: \n", "\n",
                     "ref: refs/heads/한글\n"]:
            self.write("repo/.git/HEAD", text)
            self.assertIsNone(hr.git_branch(os.path.join(self.tmp, "repo")), text)

    def test_sha256_detached_head(self):
        self.write("repo/.git/HEAD", "ab" * 32 + "\n")
        self.assertEqual(hr.git_branch(os.path.join(self.tmp, "repo")), "ababab" + "a")

    def test_symlinked_head_outside_repo_does_not_leak(self):
        secret = self.write("outside/secret.txt", "API_KEY=sk-live-123\n")
        os.makedirs(os.path.join(self.tmp, "repo/.git"))
        os.symlink(secret, os.path.join(self.tmp, "repo/.git/HEAD"))
        self.assertIsNone(hr.git_branch(os.path.join(self.tmp, "repo")))
        # 링크로 가리킨 파일이 브랜치 형식이면 그것만 나간다(내용 일부가 새지 않는다).
        self.write("outside/secret.txt", "ref: refs/heads/main\n")
        self.assertEqual(hr.git_branch(os.path.join(self.tmp, "repo")), "main")

    def test_cache_reads_once_per_ttl(self):
        calls = []
        t = [0.0]
        c = hr.BranchCache(ttl=10, clock=lambda: t[0], reader=lambda cwd: calls.append(cwd) or "main")
        self.assertEqual(c.get("/x"), "main")
        self.assertEqual(c.get("/x"), "main")
        self.assertEqual(len(calls), 1)
        t[0] = 11.0
        c.get("/x")
        self.assertEqual(len(calls), 2)
        self.assertIsNone(c.get(None))

    def test_cache_survives_reader_error(self):
        def boom(cwd):
            raise RuntimeError("x")
        self.assertIsNone(hr.BranchCache(reader=boom).get("/x"))

    def test_api_terminals_has_branch(self):
        self.write("repo/.git/HEAD", "ref: refs/heads/main\n")
        app, hc, _ = make_app()
        hc.add("t1", st("working"))["cwd"] = os.path.join(self.tmp, "repo")
        hc.add("t2", st("working"))["cwd"] = self.tmp
        res = {t["id"]: t for t in app.api_terminals()["terminals"]}
        self.assertEqual(res["t1"]["branch"], "main")
        self.assertIn("branch", res["t2"])


if __name__ == "__main__":
    unittest.main()
