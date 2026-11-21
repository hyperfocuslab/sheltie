"""Windows 분기를 맥에서 돌린다: hr.IS_WINDOWS를 monkeypatch하고 경로·함수는 주입한다."""
import io
import json
import logging
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
import herdr_remote as hr  # noqa: E402

hr.log.addHandler(logging.NullHandler())
hr.log.propagate = False


def touch(d, name):
    p = os.path.join(d, name)
    with open(p, "w") as f:
        f.write("x")
    os.chmod(p, 0o755)
    return p


class WinCase(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(hr, "IS_WINDOWS", True)
        p.start()
        self.addCleanup(p.stop)


class DefaultsTests(WinCase):
    def test_log_path_localappdata(self):
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": "/L"}):
            self.assertEqual(hr.default_log_path(), os.path.join("/L", "herdr-remote", "herdr-remote.log"))
            self.assertEqual(hr.default_hc_path(), os.path.join("/L", "Programs", "herdr-core", "hc.exe"))

    def test_log_file_is_utf8(self):
        # 로케일 기본(Windows cp949)으로 쓰면 한글 로그가 깨졌다(7950 실측).
        d = tempfile.mkdtemp()
        path = os.path.join(d, "x.log")
        before = list(hr.log.handlers)
        hr.setup_logging(path)
        added = [h for h in hr.log.handlers if h not in before]
        self.addCleanup(lambda: [hr.log.removeHandler(h) or h.close() for h in added])
        hr.log.info("알림 t1")
        for h in added:
            h.flush()
        with open(path, "rb") as f:
            self.assertIn("알림 t1".encode("utf-8"), f.read())

    def test_mac_defaults_unchanged(self):
        with mock.patch.object(hr, "IS_WINDOWS", False):
            self.assertEqual(hr.default_log_path(), "~/Library/Logs/herdr-remote.log")
            self.assertEqual(hr.default_hc_path(), "~/.local/bin/hc")
            self.assertEqual(hr.subprocess_kwargs(text=True), {})

    def test_subprocess_kwargs(self):
        self.assertEqual(hr.subprocess_kwargs(), {"creationflags": 0x08000000})
        kw = hr.subprocess_kwargs(text=True)
        self.assertEqual(kw["creationflags"], 0x08000000)
        self.assertEqual(kw["encoding"], "utf-8")

    def test_hc_runner_gets_creationflags(self):
        seen = {}

        def runner(argv, **kw):
            seen.update(kw)
            return mock.Mock(returncode=0, stdout="{}", stderr="")
        hr.Hc("hc", runner=runner).get("t1")
        self.assertEqual(seen["creationflags"], 0x08000000)

    def test_tailscale_path_listed(self):
        self.assertIn("C:\\Program Files\\Tailscale\\tailscale.exe", hr.TAILSCALE_PATHS)

    def test_hidden_appdata(self):
        self.assertIn("AppData", hr.hidden_home_dirs())
        with mock.patch.object(hr, "IS_WINDOWS", False):
            self.assertNotIn("AppData", hr.hidden_home_dirs())

    def test_relay_even_with_key_file(self):
        with tempfile.NamedTemporaryFile() as k:
            self.assertEqual(hr.resolve_relay_url("", k.name), hr.DEFAULT_RELAY_URL)
            self.assertEqual(hr.resolve_relay_url("off", k.name), "")

    def test_no_reuse_address(self):
        # 클래스 속성이라 로드 시점 플랫폼 값이다(맥 True, Windows False)
        self.assertEqual(hr.Server.allow_reuse_address, os.name != "nt")


class AgentTests(WinCase):
    def test_find_exe_preferred_over_cmd(self):
        a, b = tempfile.mkdtemp(), tempfile.mkdtemp()
        touch(a, "claude.cmd")
        exe = touch(b, "claude.exe")
        self.assertEqual(hr.find_executable("claude", [a, b]), exe)

    def test_find_cmd_only(self):
        a = tempfile.mkdtemp()
        cmd = touch(a, "codex.cmd")
        self.assertEqual(hr.find_executable("codex", [a]), cmd)
        self.assertIsNone(hr.find_executable("gemini", [a]))

    def test_mac_needs_exact_name(self):
        a = tempfile.mkdtemp()
        touch(a, "claude.exe")
        with mock.patch.object(hr, "IS_WINDOWS", False):
            self.assertIsNone(hr.find_executable("claude", [a]))

    def test_extra_dirs_env_expand(self):
        home = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"APPDATA": "/AD"}, clear=False):
            os.environ.pop("LOCALAPPDATA", None)
            dirs = hr.agent_search_dirs(home, "", hr.AGENT_EXTRA_DIRS_WIN)
        self.assertIn(os.path.join("/AD", "npm"), dirs)
        self.assertIn(os.path.join(home, ".local/bin"), dirs)
        self.assertFalse(any("%" in d for d in dirs))

    def test_launch_argv_absolute(self):
        home, b = tempfile.mkdtemp(), tempfile.mkdtemp()
        exe = touch(b, "codex.exe")
        cat = hr.AgentCatalog(home, path_env=b, extra_dirs=())
        self.assertEqual(cat.launch_argv("codex", "hi"), [exe, "--", "hi"])
        self.assertEqual(cat.executable("claude"), None)

    def test_launch_argv_mac_name(self):
        home, b = tempfile.mkdtemp(), tempfile.mkdtemp()
        touch(b, "codex")
        with mock.patch.object(hr, "IS_WINDOWS", False):
            cat = hr.AgentCatalog(home, path_env=b, extra_dirs=())
            self.assertEqual(cat.launch_argv("codex"), ["codex"])

    def test_run_claude_exe(self):
        calls = []

        def runner(argv, **kw):
            calls.append(argv)
            return mock.Mock(returncode=0, stdout=json.dumps({"address": "t1"}), stderr="")
        hc = hr.Hc("hc", runner=runner)
        hc.run_claude("C:\\w", "p", exe="C:\\u\\claude.exe")
        self.assertEqual(calls[0][-3:], ["C:\\u\\claude.exe", "--", "p"])
        self.assertIn("C:\\u\\claude.exe", calls[0])
        hc.run_claude("C:\\w")
        self.assertEqual(calls[1][-1], "claude")


class IsClaudeTests(WinCase):
    def fg(self, a0):
        return {"foreground": {"argv": [a0, "--", "x"]}}

    def test_windows_paths(self):
        self.assertTrue(hr.is_claude(self.fg("C:\\Users\\y\\.local\\bin\\claude.exe")))
        self.assertTrue(hr.is_claude(self.fg("C:/Users/y/CLAUDE.EXE")))
        self.assertTrue(hr.is_claude(self.fg("claude")))
        self.assertFalse(hr.is_claude(self.fg("C:\\x\\claude-helper.exe")))

    def test_mac_strict(self):
        with mock.patch.object(hr, "IS_WINDOWS", False):
            self.assertTrue(hr.is_claude(self.fg("/usr/bin/claude")))
            self.assertFalse(hr.is_claude(self.fg("/usr/bin/Claude.exe")))


class TranscriptTests(WinCase):
    def setUp(self):
        super().setUp()
        self.home = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.home, ".claude", "sessions"))
        cwd = "C:\\Users\\y\\hctest"
        self.dir = os.path.join(self.home, ".claude", "projects", "C--Users-y-hctest")
        os.makedirs(self.dir)
        self.sid = "11111111-2222-3333-4444-555555555555"
        self.jsonl = os.path.join(self.dir, self.sid + ".jsonl")
        open(self.jsonl, "w").close()
        with open(os.path.join(self.home, ".claude", "sessions", "19040.json"), "w") as f:
            json.dump({"kind": "interactive", "sessionId": self.sid, "cwd": cwd,
                       "procStart": "134397050731892483"}, f)
        self.info = {"foreground": {"pid": 19040, "cwd": cwd}}

    def test_match(self):
        r = hr.find_transcript(self.info, self.home, filetime=lambda pid: 134397050731892483)
        self.assertEqual(r, (self.sid, self.jsonl))

    def test_pid_reused(self):
        r = hr.find_transcript(self.info, self.home, filetime=lambda pid: 134397050731892484)
        self.assertEqual(r, (None, None))

    def test_unavailable(self):
        self.assertEqual(hr.find_transcript(self.info, self.home, filetime=lambda pid: None), (None, None))


class PathTests(WinCase):
    def test_is_abs(self):
        self.assertTrue(hr.is_abs_path("C:\\Users\\y"))
        self.assertTrue(hr.is_abs_path("C:/Users/y"))
        self.assertFalse(hr.is_abs_path("Users\\y"))
        if os.name != "nt":  # 실제 Windows에서는 os.path가 ntpath라 비Windows 규칙을 재현할 수 없다
            with mock.patch.object(hr, "IS_WINDOWS", False):
                self.assertFalse(hr.is_abs_path("C:\\Users\\y"))
                self.assertTrue(hr.is_abs_path("/Users/y"))

    def test_git_branch_relative_rejected_absolute_goes_on(self):
        # 맥에서 흉내 낼 때 "C:\..."는 상대 경로라 현재 폴더(저장소 루트면 .git이 있다)에서 위로 올라간다.
        old = os.getcwd()
        os.chdir(tempfile.mkdtemp())
        self.addCleanup(os.chdir, old)
        self.assertIsNone(hr.git_branch("repo\\x"))
        self.assertIsNone(hr.git_branch("C:\\no\\such\\dir"))  # 절대 경로 판정을 통과하고 저장소를 못 찾아 None

    def test_rel_rejected(self):
        home = os.path.realpath(tempfile.mkdtemp())
        os.makedirs(os.path.join(home, "a"))
        for bad in ("a\\b", "C:", "a/C:/x", "a:stream", "\\\\srv\\share", "/abs"):
            with self.assertRaises(hr.ApiError, msg=bad):
                hr.resolve_home_path(bad, home)
        self.assertEqual(hr.resolve_home_path("a", home), os.path.join(home, "a"))


class PairTests(WinCase):
    cfg = {"token": "t" * 32, "public_url": "https://pc.tail.ts.net:8790", "port": 8790}

    def test_vt_fail_disables_ansi(self):
        out = io.StringIO()
        self.assertEqual(hr.cmd_pair(out=out, cfg=self.cfg, enable_vt=lambda: False), 0)
        self.assertNotIn("\x1b[", out.getvalue())

    def test_vt_ok_keeps_ansi(self):
        out = io.StringIO()
        hr.cmd_pair(out=out, cfg=self.cfg, enable_vt=lambda: True)
        self.assertIn("\x1b[30;47m", out.getvalue())

    def test_reconfigure_utf8(self):
        class S(io.StringIO):
            encoding = "cp949"
            reconfigured = None

            def reconfigure(self, **kw):
                self.reconfigured = kw
        out = S()
        hr.cmd_pair(out=out, cfg=self.cfg, enable_vt=lambda: False)
        self.assertEqual(out.reconfigured, {"encoding": "utf-8"})

    def test_mac_keeps_ansi(self):
        with mock.patch.object(hr, "IS_WINDOWS", False):
            out = io.StringIO()
            hr.cmd_pair(out=out, cfg=self.cfg)
            self.assertIn("\x1b[30;47m", out.getvalue())



class WindowsScreenTest(unittest.TestCase):
    """7950(Windows 11, Claude Code 2.1.330)에서 실측한 PowerShell 승인 화면."""

    def test_powershell_approval_card(self):
        path = os.path.join(os.path.dirname(__file__), "fixtures", "claude-powershell-win-2.1.330.txt")
        with open(path, encoding="utf-8") as f:
            card = hr.parse_prompt(f.read())
        self.assertEqual(card["kind"], "approval")
        self.assertEqual(card["title"], "PowerShell command")
        self.assertIn("echo sheltie-test > out.txt", card["body"])
        self.assertEqual([o["label"] for o in card["options"]][0], "Yes")
        self.assertIsNone(card["always_index"])  # "don’t ask again for"는 영구 허용이라 세션 한정 아님
        self.assertEqual(card["persist_index"], 1)
        self.assertEqual(card["reject_index"], 2)

    def test_powershell_status_is_permission(self):
        st = {"app": "claude-code", "kind": "permission", "state": "blocked",
              "msg": "approve PowerShell: echo sheltie-test > out.txt; Get-Content out.txt"}
        self.assertTrue(hr.is_permission(st))
        self.assertEqual(hr.decide_answer({"status": st}, "approve"), ["1"])


class PrivateDirTest(unittest.TestCase):
    def test_windows_inherits_profile_acl(self):
        calls = []
        with mock.patch.object(hr, "IS_WINDOWS", True), \
                mock.patch.object(hr.os, "makedirs", lambda d, **kw: calls.append(kw)), \
                mock.patch.object(hr.os, "chmod", lambda *a: calls.append("chmod")):
            hr.ensure_private_dir("x")
        self.assertEqual(calls, [{"exist_ok": True}])  # mode 없음(3.13 소유자 전용 ACL 방지), chmod 없음

if __name__ == "__main__":
    unittest.main()
