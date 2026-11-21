import base64
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
INSTALL = os.path.join(ROOT, "install.ps1")
TEMPLATE = os.path.join(ROOT, "deploy", "windows-task.xml.in")
PS = shutil.which("powershell") or shutil.which("pwsh")

# 서버 대역: init은 설정을 만들고, pair는 PAIR_FAIL이 있으면 실패하고, serve는 /api/health에 토큰 검사 후 200.
STUB_SERVER = '''import json, os, sys
cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
p = os.path.expanduser("~/.config/herdr-remote/config.json")
if cmd == "init":
    if not os.path.exists(p):
        os.makedirs(os.path.dirname(p), exist_ok=True)
        json.dump({"token": "tok-0123456789abcdefghijkl", "port": 8790, "hc_path": "x"}, open(p, "w"))
elif cmd == "pair":
    if os.environ.get("PAIR_FAIL"):
        sys.exit(2)
    print("PAIR-OK")
elif cmd == "serve":
    from http.server import BaseHTTPRequestHandler, HTTPServer
    cfg = json.load(open(p))
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            ok = self.path == "/api/health" and self.headers.get("Authorization") == "Bearer " + cfg["token"]
            self.send_response(200 if ok else 401)
            self.end_headers()
            self.wfile.write(b"ok")
        def log_message(self, *a):
            pass
    HTTPServer(("127.0.0.1", int(cfg["port"])), H).serve_forever()
'''

# 가짜 schtasks / tailscale: 호출을 FAKE_LOG에 남기고, schtasks /Create는 XML을 복사하고, /Run은 서버 대역을 띄운다.
FAKE_HELPER = '''import os, shutil, subprocess, sys
name = sys.argv[1]
args = sys.argv[2:]
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as f:
    f.write(name + " " + " ".join(args) + "\\n")
if name == "schtasks":
    if args and args[0] == "/Create":
        shutil.copy(args[args.index("/XML") + 1], os.environ["FAKE_XML"])
        sys.exit(int(os.environ.get("SCHTASKS_CREATE_RC", "0")))
    if args and args[0] == "/Run":
        if os.environ.get("SCHTASKS_NO_START"):
            sys.exit(0)
        server = os.path.join(os.environ["LOCALAPPDATA"], "herdr-remote", "herdr_remote.py")
        p = subprocess.Popen([sys.executable, server, "serve"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, close_fds=True,
                             creationflags=0x00000008 | 0x00000200)  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        with open(os.environ["FAKE_PIDS"], "a") as f:
            f.write(str(p.pid) + "\\n")
    sys.exit(0)
if name == "tailscale":
    if args[:2] == ["status", "--json"]:
        print('{"BackendState":"%s"}' % os.environ.get("TS_STATE", "Running"))
    elif args[:3] == ["serve", "status", "--json"]:
        p = os.environ["TS_SERVE_JSON"]
        print(open(p, encoding="utf-8").read() if os.path.exists(p) else "{}")
    elif args[:2] == ["serve", "status"]:
        p = os.environ["TS_SERVE_FILE"]
        if os.path.exists(p):
            print(open(p, encoding="utf-8").read())
    elif args[:1] == ["serve"]:
        sys.exit(int(os.environ.get("TS_SERVE_RC", "0")))
'''

FAKE_CMD = '@echo off\r\n"%FAKE_PY%" "%FAKE_HELPER%" %~n0 %*\r\nexit /b %ERRORLEVEL%\r\n'


def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def read_json(p):
    return json.loads(read(p))


def messages():
    """install.ps1의 Base64 문구 표를 PowerShell 없이 읽는다."""
    t = read(INSTALL)
    body = t[t.index("# messages begin"):t.index("# messages end")]
    out = {}
    for k, v in re.findall(r"^\s*(\w+) = '([A-Za-z0-9+/=]+)'", body, re.M):
        out[k] = base64.b64decode(v, validate=True).decode("utf-8")
    return out


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@unittest.skipUnless(PS and os.name == "nt", "Windows PowerShell이 없다(Windows에서만 돈다)")
class InstallPs1Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.profile = os.path.join(self.tmp, "profile")
        self.local = os.path.join(self.tmp, "local")
        self.bin = os.path.join(self.tmp, "bin")
        for d in (self.profile, self.local, self.bin):
            os.makedirs(d)
        self.log = os.path.join(self.tmp, "calls.log")
        open(self.log, "w").close()
        self.xml = os.path.join(self.tmp, "task.xml")
        self.pids = os.path.join(self.tmp, "pids.txt")
        self.helper = os.path.join(self.tmp, "fake_helper.py")
        with open(self.helper, "w") as f:
            f.write(FAKE_HELPER)
        self.stub = os.path.join(self.tmp, "stub_server.py")
        with open(self.stub, "w") as f:
            f.write(STUB_SERVER)
        os.makedirs(os.path.join(self.tmp, "hcdir"))  # PATH 밖: PATH에 있으면 HERDR_REMOTE_HC가 비어도 찾아버린다
        self.hc = os.path.join(self.tmp, "hcdir", "hc.exe")
        open(self.hc, "w").close()
        for n in ("schtasks", "tailscale"):
            with open(os.path.join(self.bin, n + ".cmd"), "w", newline="") as f:
                f.write(FAKE_CMD)
        self.ts_serve_file = os.path.join(self.tmp, "ts_serve.txt")
        self.ts_serve_json = os.path.join(self.tmp, "ts_serve.json")
        self.port = str(free_port())

    def tearDown(self):
        if os.path.exists(self.pids):
            for line in read(self.pids).split():
                subprocess.run(["taskkill", "/F", "/PID", line], capture_output=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def env(self, **extra):
        sysroot = os.environ.get("SystemRoot", r"C:\Windows")
        env = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_REMOTE_")}
        env.update({
            "PATH": os.pathsep.join([self.bin, os.path.join(sysroot, "System32"), sysroot,
                                     os.path.join(sysroot, "System32", "WindowsPowerShell", "v1.0")]),
            "USERPROFILE": self.profile, "HOME": self.profile, "LOCALAPPDATA": self.local,
            # 가짜가 PATH에서 안 잡혀도 7950의 진짜 Tailscale(ProgramFiles)로 새지 않게 빈 폴더로 돌린다
            "ProgramFiles": os.path.join(self.tmp, "pf"), "ProgramFiles(x86)": os.path.join(self.tmp, "pf86"),
            "FAKE_LOG": self.log, "FAKE_XML": self.xml, "FAKE_PIDS": self.pids, "FAKE_HELPER": self.helper,
            "FAKE_PY": sys.executable, "TS_SERVE_FILE": self.ts_serve_file, "TS_SERVE_JSON": self.ts_serve_json,
            "HERDR_REMOTE_SRC": self.stub, "HERDR_REMOTE_PYTHON": sys.executable, "HERDR_REMOTE_HC": self.hc,
            "HERDR_REMOTE_PORT": self.port, "HERDR_REMOTE_HEALTH_TRIES": "3",
        })
        env.update(extra)
        return env

    def run_install(self, *args, **extra):
        cmd = [PS, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", INSTALL] + list(args)
        r = subprocess.run(cmd, env=self.env(**extra), capture_output=True, timeout=120)
        r.stdout = r.stdout.decode("utf-8", "replace")
        r.stderr = r.stderr.decode("utf-8", "replace")
        return r

    def run_iex(self, **extra):
        # irm | iex와 같은 경로: 파일을 UTF-8 문자열로 읽어 iex로 넘긴다(스크립트 경로 없음, 인자 없음).
        script = ("$env:HERDR_REMOTE_DRY_RUN='1'; [Console]::OutputEncoding=[Text.Encoding]::UTF8; "
                  "iex ([IO.File]::ReadAllText('%s', [Text.Encoding]::UTF8))" % INSTALL.replace("'", "''"))
        cmd = [PS, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script]
        r = subprocess.run(cmd, env=self.env(**extra), capture_output=True, timeout=120)
        r.stdout = r.stdout.decode("utf-8", "replace")
        r.stderr = r.stderr.decode("utf-8", "replace")
        return r

    def calls(self):
        with open(self.log, encoding="utf-8") as f:
            return [l.strip() for l in f if l.strip()]

    def idx(self, prefix):
        c = self.calls()
        return next(i for i, x in enumerate(c) if x.startswith(prefix))

    def server_path(self):
        return os.path.join(self.local, "herdr-remote", "herdr_remote.py")

    def cfg_path(self):
        return os.path.join(self.profile, ".config", "herdr-remote", "config.json")

    def task_xml(self):
        with open(self.xml, "rb") as f:
            raw = f.read()
        return raw, raw.decode("utf-16")

    def test_normal_install(self):
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        c = self.calls()
        i_end = self.idx("schtasks /End")
        i_create = self.idx("schtasks /Create")
        i_run = self.idx("schtasks /Run")
        i_serve = self.idx("tailscale serve --bg")
        self.assertTrue(i_end < i_create < i_run < i_serve, c)
        self.assertIn("schtasks /Create /TN Sheltie herdr-remote /XML %s /F" %
                      os.path.join(self.local, "herdr-remote", "windows-task.xml"), c)
        self.assertIn("tailscale serve --bg --https=%s http://127.0.0.1:%s" % (self.port, self.port), c)
        self.assertIn("PAIR-OK", r.stdout)
        self.assertTrue(os.path.exists(self.server_path()))
        raw, text = self.task_xml()
        self.assertTrue(raw.startswith(b"\xff\xfe"), "UTF-16 LE BOM이어야 schtasks가 읽는다")
        for want in ("<LogonType>InteractiveToken</LogonType>", "<RunLevel>LeastPrivilege</RunLevel>",
                     "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>", "<Count>999</Count>", "<Interval>PT1M</Interval>",
                     "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>",
                     "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>",
                     "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>", "<Hidden>true</Hidden>",
                     "<LogonTrigger>", '"%s" serve' % self.server_path(),
                     "<WorkingDirectory>%s</WorkingDirectory>" % os.path.join(self.local, "herdr-remote")):
            self.assertIn(want, text)
        self.assertRegex(text, r"<Command>[^<]*pythonw?\.exe</Command>")
        self.assertNotRegex(text, r"@[A-Z]+@")
        user = re.search(r"<Principal id=\"Author\">\s*<UserId>([^<]+)</UserId>", text).group(1)
        self.assertTrue(user)
        cfg = read_json(self.cfg_path())
        self.assertEqual(cfg["hc_path"], self.hc)
        self.assertEqual(cfg["port"], int(self.port))
        self.assertEqual(cfg["token"], "tok-0123456789abcdefghijkl")

    def test_dry_run_changes_nothing(self):
        r = self.run_install("-DryRun")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        c = self.calls()
        self.assertFalse(any(x.startswith("schtasks") for x in c), c)
        self.assertFalse(any(x.startswith("tailscale serve --bg") for x in c), c)
        self.assertFalse(os.path.exists(os.path.join(self.local, "herdr-remote")))
        self.assertFalse(os.path.exists(self.cfg_path()))
        self.assertIn("+ schtasks /Create", r.stdout)
        self.assertIn("+ ", r.stdout)

    def test_iex_dry_run_has_no_errors_and_korean_intact(self):
        r = self.run_iex()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(r.stderr.strip(), "", r.stderr)
        self.assertIn("+ schtasks /Create", r.stdout)
        self.assertIn("+ 복사 ", r.stdout)
        self.assertIn("(토큰 인증)", r.stdout)
        self.assertNotIn("\ufffd", r.stdout)
        self.assertFalse(any(x.startswith("schtasks") for x in self.calls()), self.calls())
        self.assertFalse(os.path.exists(os.path.join(self.local, "herdr-remote")))

    def test_iex_error_message_is_korean(self):
        r = self.run_iex(HERDR_REMOTE_PORT="abc")
        self.assertIn("HERDR_REMOTE_PORT는 숫자여야 한다: abc", r.stderr)

    def test_bad_port_rejected(self):
        for bad in ("abc", "0", "70000", "80;rm", "123456789012345678901"):
            r = self.run_install(HERDR_REMOTE_PORT=bad)
            self.assertEqual(r.returncode, 2, bad)

    def test_unknown_option(self):
        r = self.run_install("-Bogus")
        self.assertEqual(r.returncode, 2)
        self.assertIn("알 수 없는 옵션", r.stderr)

    def test_help(self):
        r = self.run_install("-Help")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("-DryRun", r.stdout)
        self.assertEqual(self.calls(), [])

    def test_no_hc(self):
        r = self.run_install(HERDR_REMOTE_HC="")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("herdr-core", r.stderr)
        self.assertFalse(any(x.startswith("schtasks") for x in self.calls()))

    def test_py_launcher_without_python(self):
        # py 런처는 있는데 파이썬이 없으면 출력이 비어도 죽지 않고 "python이 없다"로 끝나야 한다(7950 실측: null-valued 오류).
        with open(os.path.join(self.bin, "py.cmd"), "w", newline="") as f:
            f.write("@exit /b 1\r\n")
        r = self.run_install(HERDR_REMOTE_PYTHON="")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("python이 없다", r.stderr)
        self.assertNotIn("null", r.stderr)

    def test_hc_found_in_default_location(self):
        d = os.path.join(self.local, "Programs", "herdr-core")
        os.makedirs(d)
        hc = os.path.join(d, "hc.exe")
        open(hc, "w").close()
        r = self.run_install(HERDR_REMOTE_HC="")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(read_json(self.cfg_path())["hc_path"], hc)

    def test_tailscale_logged_out(self):
        r = self.run_install(TS_STATE="NeedsLogin")
        self.assertEqual(r.returncode, 1)
        self.assertIn("로그인", r.stderr)
        self.assertFalse(any(x.startswith("schtasks") for x in self.calls()))

    def test_no_tailscale(self):
        r = self.run_install(HERDR_REMOTE_TAILSCALE=os.path.join(self.tmp, "nonexistent.exe"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("tailscale", r.stderr)

    def test_skip_tailscale(self):
        r = self.run_install(HERDR_REMOTE_SKIP_TAILSCALE="1")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertFalse(any(x.startswith("tailscale") for x in self.calls()))
        self.assertNotIn("PAIR-OK", r.stdout)

    def test_schtasks_create_failure(self):
        r = self.run_install(SCHTASKS_CREATE_RC="1")
        self.assertEqual(r.returncode, 5, r.stderr + r.stdout)
        self.assertIn("schtasks /Create", r.stderr)
        self.assertFalse(any(x.startswith("tailscale serve --bg") for x in self.calls()))

    def test_serve_failure(self):
        r = self.run_install(TS_SERVE_RC="1")
        self.assertEqual(r.returncode, 6, r.stderr + r.stdout)
        self.assertIn("tailscale serve", r.stderr)

    def test_health_failure(self):
        r = self.run_install(SCHTASKS_NO_START="1")
        self.assertEqual(r.returncode, 7, r.stderr + r.stdout)
        self.assertIn("응답하지 않는다", r.stderr)

    def test_pair_failure(self):
        r = self.run_install(PAIR_FAIL="1")
        self.assertEqual(r.returncode, 4, r.stderr + r.stdout)
        self.assertIn("pair", r.stderr)

    def test_funnel_on_port_stops_install(self):
        with open(self.ts_serve_json, "w") as f:
            json.dump({"TCP": {self.port: {"HTTPS": True}},
                       "AllowFunnel": {"x.ts.net:%s" % self.port: True}}, f)
        r = self.run_install()
        self.assertEqual(r.returncode, 8, r.stderr + r.stdout)
        self.assertIn("Funnel", r.stderr)
        self.assertFalse(any(x.startswith("tailscale serve --bg") for x in self.calls()))

    def test_funnel_on_other_port_is_fine(self):
        with open(self.ts_serve_json, "w") as f:
            json.dump({"AllowFunnel": {"x.ts.net:1": True}}, f)
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)

    def test_rerun_idempotent(self):
        self.assertEqual(self.run_install().returncode, 0)
        tok = read_json(self.cfg_path())["token"]
        first = self.task_xml()[1]
        with open(self.ts_serve_file, "w") as f:
            f.write("https://x.ts.net:%s (tailnet only)\n|-- / proxy http://127.0.0.1:%s\n" % (self.port, self.port))
        open(self.log, "w").close()
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(read_json(self.cfg_path())["token"], tok)
        self.assertEqual(self.task_xml()[1], first)
        self.assertFalse(any(x.startswith("tailscale serve --bg") for x in self.calls()))

    def test_http_source_rejected(self):
        r = self.run_install(HERDR_REMOTE_SRC="http://example.invalid/herdr_remote.py")
        self.assertEqual(r.returncode, 1)
        self.assertIn("https", r.stderr)
        self.assertFalse(any(x.startswith("schtasks") for x in self.calls()))

    def test_missing_source_aborts(self):
        r = self.run_install(HERDR_REMOTE_SRC=os.path.join(self.tmp, "nope.py"))
        self.assertEqual(r.returncode, 1)
        self.assertFalse(any(x.startswith("schtasks") for x in self.calls()))

    def test_empty_source_aborts(self):
        empty = os.path.join(self.tmp, "empty.py")
        open(empty, "w").close()
        r = self.run_install(HERDR_REMOTE_SRC=empty)
        self.assertEqual(r.returncode, 1)
        self.assertIn("비어", r.stderr)
        self.assertFalse(os.path.exists(self.server_path()))

    def test_syntax_error_source_aborts(self):
        bad = os.path.join(self.tmp, "bad.py")
        with open(bad, "w") as f:
            f.write("def (\n")
        r = self.run_install(HERDR_REMOTE_SRC=bad)
        self.assertEqual(r.returncode, 1)
        self.assertIn("파이썬", r.stderr)
        self.assertFalse(os.path.exists(self.server_path()))
        self.assertFalse(any(x.startswith("schtasks") for x in self.calls()))

    def check_uninstall(self, *args, **extra):
        self.assertEqual(self.run_install().returncode, 0)
        open(self.log, "w").close()
        r = self.run_install(*args, **extra)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        c = self.calls()
        self.assertIn("schtasks /End /TN Sheltie herdr-remote", c)
        self.assertIn("schtasks /Delete /TN Sheltie herdr-remote /F", c)
        self.assertIn("tailscale serve --https=%s off" % self.port, c)
        self.assertFalse(os.path.exists(self.server_path()))
        self.assertTrue(os.path.exists(self.cfg_path()), "설정과 토큰은 남긴다")
        self.assertIn("해제 완료", r.stdout)

    def test_uninstall(self):
        self.check_uninstall("-Uninstall")

    def test_uninstall_by_env(self):
        self.check_uninstall(HERDR_REMOTE_UNINSTALL="1")


class InstallPs1StaticTest(unittest.TestCase):
    """PowerShell이 없는 맥에서도 도는 파일 검사."""

    def test_builtin_task_xml_matches_template(self):
        t = read(INSTALL)
        m = re.search(r"@'\r?\n(.*?)\r?\n'@", t, re.S)
        self.assertIsNotNone(m)
        msgs = messages()
        # 내장 사본의 한글 자리(@XML_COMMENT@, @XML_DESC@)는 표에서 채운다.
        builtin = m.group(1).replace("@XML_COMMENT@", msgs["xml_comment"]).replace("@XML_DESC@", msgs["xml_desc"])
        norm = lambda s: s.replace("\r\n", "\n").strip("\n")
        self.assertEqual(norm(builtin), norm(read(TEMPLATE)))

    def test_script_is_pure_ascii_without_bom(self):
        # BOM이 있으면 irm | iex가 첫 줄을 명령으로 읽어 오류가 나고,
        # BOM 없이 비ASCII가 있으면 PS 5.1 -File이 cp949로 읽어 깨진다.
        with open(INSTALL, "rb") as f:
            raw = f.read()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        self.assertTrue(all(b < 0x80 for b in raw), "ASCII 밖 바이트가 있다")

    def test_messages_decode_as_utf8(self):
        msgs = messages()
        self.assertGreater(len(msgs), 40)
        for k, v in msgs.items():
            self.assertTrue(v.strip(), k)
        used = set(re.findall(r"\(?T '(\w+)'", read(INSTALL)))
        self.assertFalse(used - set(msgs), used - set(msgs))

    def test_representative_messages(self):
        msgs = messages()
        self.assertEqual(msgs["unknown_opt"], "알 수 없는 옵션: {0} (-DryRun, -Uninstall)")
        self.assertEqual(msgs["uninstalled"], "해제 완료. 설정과 토큰은 남겼다: {0} (지우려면 직접 삭제)")
        self.assertEqual(msgs["src_empty"], "받은 서버 파일이 비어 있다: {0}")
        self.assertEqual(msgs["no_health"], '서버가 응답하지 않는다. 로그를 확인해라: Get-Content -Tail 30 "{0}"')
        self.assertEqual(msgs["help1"], "herdr-remote 설치기(Windows). 사용: powershell -File install.ps1 [-DryRun] [-Uninstall] [-Help]")
        self.assertTrue(msgs["no_hc"].startswith("hc(herdr-core)가 없다."))
        self.assertIn("%LOCALAPPDATA%\\Programs\\herdr-core\\hc.exe", msgs["no_hc"])

    def test_dry_run_env_supported(self):
        self.assertIn("HERDR_REMOTE_DRY_RUN", read(INSTALL))

    def test_no_ps7_only_syntax(self):
        t = read(INSTALL)
        self.assertNotIn("&&", t)
        self.assertNotIn("??", t)
        self.assertNotRegex(t, r"\?\s[^\n]*\s:\s")  # 삼항 연산자

    def test_no_personal_strings(self):
        for p in (INSTALL, TEMPLATE):
            t = read(p)
            for bad in ("yejun", "hfl-mini", "hfl-win", "taild2204d", "100.77."):
                self.assertNotIn(bad, t, (p, bad))


if __name__ == "__main__":
    unittest.main()
