"""`sheltie.py run <agent> [args]`: hc(herdr-core)를 숨겨 쓰는 런처. 가짜 hc(runner 주입)로 인자·이름·환경·종료 코드·hc 없음을 본다."""
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import sheltie  # noqa: E402


class FakeHc:
    """runner(argv, **kw)로 불린다. connect는 stdin JSON-RPC 한 줄을 받아 결과를 돌려준다."""

    def __init__(self, live=(), get_state="finished", exit_code=0, signal=None, run_error=None):
        self.calls = []
        self.requests = []
        self.live = list(live)
        self.get_state = get_state
        self.exit_code = exit_code
        self.signal = signal
        self.run_error = run_error

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        cmd = argv[1]
        out = ""
        if cmd == "ls":
            out = json.dumps({"terminals": [{"address": "t%d" % i, "name": n} for i, n in enumerate(self.live)]})
        elif cmd == "connect":
            req = json.loads(kw["input"].strip().splitlines()[0])
            self.requests.append(req)
            if self.run_error:
                out = json.dumps({"jsonrpc": "2.0", "id": req["id"], "error": self.run_error})
            else:
                out = json.dumps({"jsonrpc": "2.0", "id": req["id"],
                                  "result": {"address": "t9", "name": req["params"].get("name")}})
        elif cmd == "get":
            out = json.dumps({"address": "t9", "state": self.get_state, "exit_code": self.exit_code,
                              "signal": self.signal})
        elif cmd == "rm":
            out = json.dumps({"removed": "t9"})
        return subprocess.CompletedProcess(argv, 0, out, "")


class RunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        self.agent = self._exe("gemini")
        self.hc = self._exe("hc")
        self.cwd = os.path.join(self.tmp, "My Project")
        os.makedirs(self.cwd)
        self.attached = []

    def _exe(self, name):
        p = os.path.join(self.bin, name + (".exe" if os.name == "nt" else ""))
        with open(p, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(p, 0o755)
        return p

    def attach(self, argv):
        self.attached.append(list(argv))
        return 0

    def run_cmd(self, args, fake, env=None, hc_path="default"):
        err = io.StringIO()
        e = {"PATH": self.bin, "HOME": self.tmp, "API_KEY": "k1"}
        e.update(env or {})
        rc = sheltie.cmd_run(args, runner=fake, attach=self.attach, env=e, cwd=self.cwd,
                             hc_path=self.hc if hc_path == "default" else hc_path, err=err,
                             term_size=(100, 40))
        return rc, err.getvalue()

    def test_starts_agent_in_cwd_and_attaches(self):
        fake = FakeHc()
        rc, _ = self.run_cmd(["gemini", "--model", "x", "-p", "hi there"], fake)
        self.assertEqual(rc, 0)
        req = fake.requests[0]
        self.assertEqual(req["method"], "terminal.run")
        p = req["params"]
        self.assertEqual(p["argv"], [self.agent, "--model", "x", "-p", "hi there"])  # 인자 그대로, 절대 경로
        self.assertEqual(p["cwd"], self.cwd)
        self.assertTrue(p["no_parent"])
        self.assertEqual(p["size"], {"cols": 100, "rows": 40})
        self.assertEqual(p["name"], "gemini-my-project")
        self.assertEqual(self.attached, [[self.hc, "attach", "t9"]])
        # 끝난 터미널은 지운다
        self.assertIn([self.hc, "rm", "t9"], fake.calls)

    def test_env_passed_without_terminal_identity(self):
        fake = FakeHc()
        self.run_cmd(["gemini"], fake, env={"TERM": "xterm-ghostty", "TERM_PROGRAM": "iTerm.app",
                                            "COLORTERM": "truecolor", "HC_TERMINAL_ID": "t1", "TMUX": "x",
                                            "PWD": "/elsewhere", "GEMINI_API_KEY": "secret"})
        env = fake.requests[0]["params"]["env"]
        self.assertEqual(env["API_KEY"], "k1")
        self.assertEqual(env["GEMINI_API_KEY"], "secret")
        self.assertEqual(env["PATH"], self.bin)
        for k in ("TERM", "TERM_PROGRAM", "COLORTERM", "HC_TERMINAL_ID", "TMUX", "PWD"):
            self.assertNotIn(k, env)
        # 비밀이 argv(ps·hc get에 보임)로 새지 않는다
        for c in fake.calls:
            self.assertNotIn("secret", " ".join(c))

    def test_name_unique_among_live(self):
        fake = FakeHc(live=["gemini-my-project", "gemini-my-project-2"])
        self.run_cmd(["gemini"], fake)
        self.assertEqual(fake.requests[0]["params"]["name"], "gemini-my-project-3")

    def test_name_rules(self):
        self.assertEqual(sheltie.run_name("gemini", "/x/한글 폴더", set()), "gemini")
        self.assertEqual(sheltie.run_name("Copilot.EXE", "C:\\work\\App_1", set(), win=True), "copilot-app_1")
        n = sheltie.run_name("a" * 40, "/x/" + "b" * 40, set())
        self.assertLessEqual(len(n), 32)
        self.assertRegex(n, r"^[a-z0-9][a-z0-9_.-]*$")
        self.assertFalse(n.endswith("-"))
        self.assertEqual(sheltie.run_name("t7", "/", set()), "t7-run")  # 주소(t7) 모양 금지
        self.assertEqual(sheltie.run_name("...", "/", set()), "run")

    def test_explicit_name(self):
        fake = FakeHc()
        self.run_cmd(["--name", "mine", "gemini", "--name", "x"], fake)
        p = fake.requests[0]["params"]
        self.assertEqual(p["name"], "mine")
        self.assertEqual(p["argv"], [self.agent, "--name", "x"])  # 에이전트 뒤 옵션은 에이전트 것

    def test_exit_code_passed_through(self):
        rc, _ = self.run_cmd(["gemini"], FakeHc(exit_code=7))
        self.assertEqual(rc, 7)

    def test_signal_exit(self):
        rc, _ = self.run_cmd(["gemini"], FakeHc(exit_code=None, signal=2))
        self.assertEqual(rc, 130)

    def test_detached_keeps_terminal(self):
        fake = FakeHc(get_state="live")
        rc, err = self.run_cmd(["gemini"], fake)
        self.assertEqual(rc, 0)
        self.assertNotIn([self.hc, "rm", "t9"], fake.calls)
        self.assertIn("hc attach gemini-my-project", err)

    def test_no_hc(self):
        fake = FakeHc()
        rc, err = self.run_cmd(["gemini"], fake, hc_path=None)
        self.assertEqual(rc, 1)
        self.assertEqual(fake.calls, [])
        self.assertIn("herdr-core", err)
        self.assertIn(sheltie.HC_INSTALL_HINT, err)

    def test_hc_path_not_executable(self):
        rc, err = self.run_cmd(["gemini"], FakeHc(), hc_path=os.path.join(self.tmp, "nope"))
        self.assertEqual(rc, 1)
        self.assertIn("herdr-core", err)

    def test_program_not_found(self):
        fake = FakeHc()
        rc, err = self.run_cmd(["nosuchagent"], fake)
        self.assertEqual(rc, 127)
        self.assertIn("nosuchagent", err)
        self.assertEqual(fake.requests, [])

    def test_usage(self):
        rc, err = self.run_cmd([], FakeHc())
        self.assertEqual(rc, 2)
        self.assertIn("sheltie run", err)
        rc, _ = self.run_cmd(["--name"], FakeHc())
        self.assertEqual(rc, 2)

    def test_spawn_error_reported(self):
        fake = FakeHc(run_error={"code": -32000, "message": "cannot start", "data": {"kind": "spawn_failed"}})
        rc, err = self.run_cmd(["gemini"], fake)
        self.assertEqual(rc, 1)
        self.assertIn("cannot start", err)
        self.assertEqual(self.attached, [])

    def test_hc_path_lookup(self):
        cfg_hc = self._exe("hc-cfg")
        self.assertEqual(sheltie.run_hc_path({"hc_path": cfg_hc}, {"PATH": ""}), cfg_hc)
        # 설정이 비었으면 PATH의 hc
        self.assertEqual(sheltie.run_hc_path({"hc_path": ""}, {"PATH": self.bin}, default=""), self.hc)
        self.assertIsNone(sheltie.run_hc_path({"hc_path": ""}, {"PATH": ""}, default=""))

    def test_main_dispatches_run(self):
        called = []
        orig = sheltie.cmd_run
        sheltie.cmd_run = lambda a, **kw: called.append(a) or 5
        try:
            self.assertEqual(sheltie.main(["run", "gemini", "-x"]), 5)
        finally:
            sheltie.cmd_run = orig
        self.assertEqual(called, [["gemini", "-x"]])


@unittest.skipIf(os.name == "nt", "가짜 hc 셸 스크립트")
class RunRealProcessTest(unittest.TestCase):
    """기본 runner(subprocess.run)·attach로 가짜 hc 실행 파일을 끝까지 부른다."""

    def test_end_to_end_with_fake_hc_binary(self):
        tmp = tempfile.mkdtemp()
        log = os.path.join(tmp, "log")
        hc = os.path.join(tmp, "hc")
        with open(hc, "w") as f:
            f.write("""#!/usr/bin/env python3
import json, sys
a = sys.argv[1:]
open(%r, "a").write(" ".join(a) + "\\n")
if a[0] == "ls": print(json.dumps({"terminals": []}))
elif a[0] == "connect":
    req = json.loads(sys.stdin.readline())
    open(%r, "a").write("REQ " + json.dumps(req["params"]["argv"]) + "\\n")
    print(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": {"address": "t4"}}))
elif a[0] == "get": print(json.dumps({"state": "finished", "exit_code": 3}))
elif a[0] == "attach": sys.exit(0)
else: print("{}")
""" % (log, log))
        os.chmod(hc, 0o755)
        rc = sheltie.cmd_run(["sh", "-c", "exit 0"], hc_path=hc, env={"PATH": "/bin:/usr/bin"}, cwd=tmp,
                             err=io.StringIO(), term_size=(80, 24))
        self.assertEqual(rc, 3)
        text = open(log).read()
        self.assertIn('REQ ["/bin/sh", "-c", "exit 0"]', text)
        self.assertIn("attach t4", text)
        self.assertIn("rm t4", text)


class HcVersionTest(unittest.TestCase):
    def runner(self, out, rc=0):
        return lambda argv, **kw: subprocess.CompletedProcess(argv, rc, out, "")

    def setUp(self):
        fd, self.hc = tempfile.mkstemp()
        os.close(fd)

    def test_parse(self):
        self.assertEqual(sheltie.parse_hc_version("hc 0.1.21\n"), "0.1.21")
        self.assertIsNone(sheltie.parse_hc_version("hc dev"))

    def test_tested_version_no_warning(self):
        with self.assertNoLogs(sheltie.log, "WARNING"):
            r = sheltie.hc_version_check(self.hc, runner=self.runner("hc 0.1.21\n"))
        self.assertEqual(r, {"hc_version": "0.1.21", "hc_tested": True})

    def test_untested_version_warns_once_line(self):
        with self.assertLogs(sheltie.log, "WARNING") as cm:
            r = sheltie.hc_version_check(self.hc, runner=self.runner("hc 0.1.22\n"))
        self.assertEqual(r, {"hc_version": "0.1.22", "hc_tested": False})
        self.assertEqual(len(cm.output), 1)
        self.assertIn("0.1.22", cm.output[0])

    def test_missing_or_unreadable(self):
        self.assertEqual(sheltie.hc_version_check(""), {"hc_version": None, "hc_tested": None})
        with self.assertLogs(sheltie.log, "WARNING"):
            r = sheltie.hc_version_check(self.hc, runner=self.runner("", rc=1))
        self.assertEqual(r, {"hc_version": None, "hc_tested": None})

    def test_installers_pin_tested_version(self):
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
        import re
        for name in ("install.sh", "install.ps1"):
            with open(os.path.join(root, name), encoding="utf-8") as f:
                m = re.search(r"HC_PIN_VERSION\s*=\s*['\"]([0-9.]+)['\"]", f.read())
            self.assertIsNotNone(m, name)
            self.assertIn(m.group(1), sheltie.HC_TESTED_VERSIONS, name)

    def test_health_has_hc_fields(self):
        import threading
        import urllib.request
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from test_sheltie import make_app
        app, _, _ = make_app(tempfile.mkdtemp())
        app.hc_version = {"hc_version": "0.1.22", "hc_tested": False}
        srv = sheltie.make_server(app, "127.0.0.1", 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        req = urllib.request.Request("http://127.0.0.1:%d/api/health" % srv.server_address[1],
                                     headers={"Authorization": "Bearer " + app.cfg["token"]})
        with urllib.request.urlopen(req) as r:
            body = json.load(r)
        self.assertEqual((body["hc_version"], body["hc_tested"]), ("0.1.22", False))


if __name__ == "__main__":
    unittest.main()
