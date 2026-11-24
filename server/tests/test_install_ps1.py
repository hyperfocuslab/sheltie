import base64
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
import uuid
import zipfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
INSTALL = os.path.join(ROOT, "install.ps1")
TEMPLATE = os.path.join(ROOT, "deploy", "windows-task.xml.in")
PS = shutil.which("powershell") or shutil.which("pwsh")

# 서버 대역: init은 설정을 만들고, pair는 PAIR_FAIL이 있으면 실패하고, serve는 /api/health에 토큰 검사 후 200.
STUB_SERVER = '''import json, os, sys, time
cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
p = os.path.expanduser("~/.config/sheltie/config.json")
if cmd == "init":
    if not os.path.exists(p):
        os.makedirs(os.path.dirname(p), exist_ok=True)
        json.dump({"token": "tok-0123456789abcdefghijkl", "port": 8790, "hc_path": "x"}, open(p, "w"))
elif cmd == "pair":
    if os.environ.get("PAIR_FAIL"):
        sys.exit(2)
    print("PAIR-OK")
elif cmd == "run":
    open(os.environ["ARGV_OUT"], "w").write(json.dumps(sys.argv[1:]))
    sys.exit(int(os.environ.get("RUN_RC", "0")))
elif cmd == "serve":
    from http.server import BaseHTTPRequestHandler, HTTPServer
    cfg = json.load(open(p))
    STARTED = time.time()
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            ok = self.path == "/api/health" and self.headers.get("Authorization") == "Bearer " + cfg["token"]
            self.send_response(200 if ok else 401)
            self.end_headers()
            started = 0 if os.environ.get("STUB_OLD_STARTED") else STARTED
            self.wfile.write(json.dumps({"ok": True, "started": started}).encode())
        def log_message(self, *a):
            pass
    HTTPServer(("127.0.0.1", int(cfg["port"])), H).serve_forever()
'''

# 가짜 schtasks / tailscale: 호출을 FAKE_LOG에 남기고, /End는 띄운 서버를 1.5초 뒤 끝내고, schtasks /Create는 XML을 복사하고, /Run은 서버 대역을 띄운다.
FAKE_HELPER = '''import os, shutil, subprocess, sys
name = sys.argv[1]
args = sys.argv[2:]
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as f:
    f.write(name + " " + " ".join(args) + "\\n")
if name == "schtasks":
    if args and args[0] == "/End":
        # like real /End: return at once, the process exits a bit later (never with SCHTASKS_END_NOKILL)
        if os.path.exists(os.environ["FAKE_PIDS"]) and not os.environ.get("SCHTASKS_END_NOKILL"):
            pids = open(os.environ["FAKE_PIDS"]).read().split()
            code = "import subprocess, time; time.sleep(%s)\\n" % os.environ.get("SCHTASKS_END_DELAY", "1.5")
            code += "".join("subprocess.run(['taskkill', '/F', '/PID', '%s'], capture_output=True)\\n" % x for x in pids)
            subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, close_fds=True, creationflags=0x00000008 | 0x00000200)
        sys.exit(0)
    if args and args[0] == "/Create":
        shutil.copy(args[args.index("/XML") + 1], os.environ["FAKE_XML"])
        sys.exit(int(os.environ.get("SCHTASKS_CREATE_RC", "0")))
    if args and args[0] == "/Run":
        if os.environ.get("SCHTASKS_NO_START"):
            sys.exit(0)
        server = os.path.join(os.environ["LOCALAPPDATA"], "sheltie", "sheltie.py")
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
        os.makedirs(os.path.join(self.tmp, "hcdir"))  # PATH 밖: PATH에 있으면 SHELTIE_HC가 비어도 찾아버린다
        self.hc = os.path.join(self.tmp, "hcdir", "hc.exe")
        open(self.hc, "w").close()
        for n in ("schtasks", "tailscale"):
            with open(os.path.join(self.bin, n + ".cmd"), "w", newline="") as f:
                f.write(FAKE_CMD)
        self.ts_serve_file = os.path.join(self.tmp, "ts_serve.txt")
        self.ts_serve_json = os.path.join(self.tmp, "ts_serve.json")
        self.port = str(free_port())
        # 실제 HKCU\Environment를 건드리지 않게 임시 키를 쓴다. 사용자 PATH처럼 변수가 안 펴진 ExpandString으로 시작.
        self.subkey = "Software\\SheltieTest\\" + uuid.uuid4().hex[:12]
        self.set_user_path(r"%USERPROFILE%\bin;C:\existing", "expand")
        # 기본 herdr-core 묶음은 해시가 안 맞는 가짜 zip(진짜 네트워크로 새지 않게). 진짜 herdr-core.exe는 못 만들어 성공 경로는 시험하지 않는다.
        self.hc_zip = os.path.join(self.tmp, "hc.zip")
        with zipfile.ZipFile(self.hc_zip, "w") as z:
            z.writestr("herdr-core.exe", "not a real exe")
            z.writestr("conpty/conpty.dll", "x")
        self.hc_zip_sha = hashlib.sha256(open(self.hc_zip, "rb").read()).hexdigest()

    def set_user_path(self, value, kind):
        import winreg
        k = winreg.CreateKey(winreg.HKEY_CURRENT_USER, self.subkey)
        winreg.SetValueEx(k, "Path", 0, winreg.REG_EXPAND_SZ if kind == "expand" else winreg.REG_SZ, value)
        winreg.CloseKey(k)

    def user_path(self):
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.subkey)
        try:
            v, t = winreg.QueryValueEx(k, "Path")
        finally:
            winreg.CloseKey(k)
        return v, ("expand" if t == winreg.REG_EXPAND_SZ else "string" if t == winreg.REG_SZ else t)

    def cmd_path(self):
        return os.path.join(self.local, "sheltie", "bin", "sheltie.cmd")

    def tearDown(self):
        import winreg
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, self.subkey)
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, "Software\\SheltieTest")  # 비었을 때만 지워진다
        except OSError:
            pass
        if os.path.exists(self.pids):
            for line in read(self.pids).split():
                subprocess.run(["taskkill", "/F", "/PID", line], capture_output=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def env(self, **extra):
        sysroot = os.environ.get("SystemRoot", r"C:\Windows")
        env = {k: v for k, v in os.environ.items() if not k.startswith("SHELTIE_")}
        env.update({
            "PATH": os.pathsep.join([self.bin, os.path.join(sysroot, "System32"), sysroot,
                                     os.path.join(sysroot, "System32", "WindowsPowerShell", "v1.0")]),
            "USERPROFILE": self.profile, "HOME": self.profile, "LOCALAPPDATA": self.local,
            # 가짜가 PATH에서 안 잡혀도 7950의 진짜 Tailscale(ProgramFiles)로 새지 않게 빈 폴더로 돌린다
            "ProgramFiles": os.path.join(self.tmp, "pf"), "ProgramFiles(x86)": os.path.join(self.tmp, "pf86"),
            "FAKE_LOG": self.log, "FAKE_XML": self.xml, "FAKE_PIDS": self.pids, "FAKE_HELPER": self.helper,
            "FAKE_PY": sys.executable, "TS_SERVE_FILE": self.ts_serve_file, "TS_SERVE_JSON": self.ts_serve_json,
            "SHELTIE_SRC": self.stub, "SHELTIE_PYTHON": sys.executable, "SHELTIE_HC": self.hc,
            "SHELTIE_PORT": self.port, "SHELTIE_HEALTH_TRIES": "3",
            "SHELTIE_ENV_KEY": "HKCU:\\" + self.subkey, "SHELTIE_HC_URL": self.hc_zip,
            "SHELTIE_HC_SHA256": self.hc_zip_sha,
        })
        env.update(extra)
        for k in [k for k, v in env.items() if v is None]:
            del env[k]
        return env

    def run_install(self, *args, **extra):
        cmd = [PS, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", INSTALL] + list(args)
        r = subprocess.run(cmd, env=self.env(**extra), capture_output=True, timeout=120)
        r.stdout = r.stdout.decode("utf-8", "replace")
        r.stderr = r.stderr.decode("utf-8", "replace")
        return r

    def run_iex(self, **extra):
        # irm | iex와 같은 경로: 파일을 UTF-8 문자열로 읽어 iex로 넘긴다(스크립트 경로 없음, 인자 없음).
        script = ("$env:SHELTIE_DRY_RUN='1'; [Console]::OutputEncoding=[Text.Encoding]::UTF8; "
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
        return os.path.join(self.local, "sheltie", "sheltie.py")

    def cfg_path(self):
        return os.path.join(self.profile, ".config", "sheltie", "config.json")

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
        self.assertIn("schtasks /Create /TN Sheltie /XML %s /F" %
                      os.path.join(self.local, "sheltie", "windows-task.xml"), c)
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
                     "<WorkingDirectory>%s</WorkingDirectory>" % os.path.join(self.local, "sheltie")):
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
        self.assertFalse(os.path.exists(os.path.join(self.local, "sheltie")))
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
        self.assertFalse(os.path.exists(os.path.join(self.local, "sheltie")))

    def test_iex_error_message_is_korean(self):
        r = self.run_iex(SHELTIE_PORT="abc")
        self.assertIn("SHELTIE_PORT는 숫자여야 한다: abc", r.stderr)

    def test_bad_port_rejected(self):
        for bad in ("abc", "0", "70000", "80;rm", "123456789012345678901"):
            r = self.run_install(SHELTIE_PORT=bad)
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
        r = self.run_install(SHELTIE_HC="")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("herdr-core 없이 설치한다", r.stdout)
        self.assertEqual(read_json(self.cfg_path())["hc_path"], "")
        self.assertTrue(any(x.startswith("schtasks /Create") for x in self.calls()))

    def hook_files(self):
        return (os.path.join(self.profile, ".claude", "settings.json"), os.path.join(self.profile, ".codex", "hooks.json"))

    def test_hooks_fresh_merge_reinstall_uninstall(self):
        cl, cx = self.hook_files()
        os.makedirs(os.path.dirname(cl))
        os.makedirs(os.path.dirname(cx))
        with open(cl, "w", encoding="utf-8") as f:
            json.dump({"model": "x", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "mine"}]}]}}, f)
        for _ in range(2):  # 두 번 설치해도 중복 없음
            r = self.run_install()
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        c = read_json(cl)
        want = '& "%s" "%s" hook claude' % (sys.executable, self.server_path())
        stop = c["hooks"]["Stop"]
        self.assertEqual(stop[0]["hooks"][0]["command"], "mine")
        self.assertEqual([h["command"] for g in stop for h in g["hooks"]].count(want), 1)
        self.assertEqual(c["hooks"]["PermissionRequest"][0]["hooks"][0]["shell"], "powershell")
        x = read_json(cx)
        cmd = x["hooks"]["PermissionRequest"][0]["hooks"][0]["command"]
        self.assertTrue(cmd.startswith("powershell.exe -NoProfile -Command "), cmd)
        self.assertIn("sheltie.py' hook codex", cmd)
        self.assertTrue(os.path.exists(cl + ".sheltie-bak"))
        r = self.run_install("-Uninstall")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(read_json(cl), {"model": "x", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "mine"}]}]}})
        self.assertEqual(read_json(cx), {})

    def test_hooks_broken_json_and_nohooks(self):
        cl, cx = self.hook_files()
        os.makedirs(os.path.dirname(cl))
        with open(cl, "w") as f:
            f.write("{ nope")
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(read(cl), "{ nope")
        self.assertIn("올바른 JSON이 아니라", r.stderr)
        os.remove(cl)
        r = self.run_install("-NoHooks")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(cl))

    def test_hooks_qwen_and_opencode(self):
        q = os.path.join(self.profile, ".qwen", "settings.json")
        oc = os.path.join(self.profile, ".config", "opencode")
        plugin = os.path.join(oc, "plugins", "sheltie.js")
        os.makedirs(os.path.dirname(q))
        os.makedirs(oc)
        with open(q, "w", encoding="utf-8") as f:
            json.dump({"model": "x", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "mine"}]}]}}, f)
        for _ in range(2):  # 두 번 설치해도 중복 없음
            r = self.run_install()
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        c = read_json(q)
        want = '& "%s" "%s" hook qwen' % (sys.executable, self.server_path())
        self.assertEqual(c["hooks"]["PermissionRequest"][0]["hooks"][0],
                         {"type": "command", "command": want, "shell": "powershell", "timeout": 600})
        self.assertEqual([h["command"] for g in c["hooks"]["Stop"] for h in g["hooks"]], ["mine", want])
        self.assertTrue(os.path.exists(q + ".sheltie-bak"))
        js = read(plugin)
        self.assertTrue(js.startswith("// sheltie-opencode-plugin"))
        self.assertIn("const SERVER = %s" % json.dumps(self.server_path()), js)
        r = self.run_install("-Uninstall")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(read_json(q), {"model": "x", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "mine"}]}]}})
        self.assertFalse(os.path.exists(plugin))

    def test_py_launcher_without_python(self):
        # py 런처는 있는데 파이썬이 없으면 출력이 비어도 죽지 않고 "python이 없다"로 끝나야 한다(7950 실측: null-valued 오류).
        with open(os.path.join(self.bin, "py.cmd"), "w", newline="") as f:
            f.write("@exit /b 1\r\n")
        r = self.run_install(SHELTIE_PYTHON="")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("python이 없다", r.stderr)
        self.assertNotIn("null", r.stderr)

    def test_hc_found_in_default_location(self):
        d = os.path.join(self.local, "Programs", "herdr-core")
        os.makedirs(d)
        hc = os.path.join(d, "hc.exe")
        open(hc, "w").close()
        r = self.run_install(SHELTIE_HC="")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(read_json(self.cfg_path())["hc_path"], hc)

    def test_tailscale_logged_out(self):
        r = self.run_install(TS_STATE="NeedsLogin")
        self.assertEqual(r.returncode, 1)
        self.assertIn("로그인", r.stderr)
        self.assertFalse(any(x.startswith("schtasks") for x in self.calls()))

    def test_no_tailscale(self):
        r = self.run_install(SHELTIE_TAILSCALE=os.path.join(self.tmp, "nonexistent.exe"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("tailscale", r.stderr)

    def test_skip_tailscale(self):
        r = self.run_install(SHELTIE_SKIP_TAILSCALE="1")
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

    def pid_alive(self, pid):
        out = subprocess.run(["tasklist", "/FI", "PID eq %s" % pid, "/NH"], capture_output=True, text=True).stdout
        return (" %s " % pid) in out

    def test_reinstall_waits_for_old_server(self):
        # 옛 서버가 /End 뒤 1.5초 더 살아 있어도, 설치기는 그 서버의 health를 성공으로 치지 않고 새 서버를 띄운다.
        self.assertEqual(self.run_install().returncode, 0)
        old = read(self.pids).split()
        open(self.log, "w").close()
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        new = [x for x in read(self.pids).split() if x not in old]
        self.assertEqual(len(new), 1, read(self.pids))
        self.assertFalse(any(self.pid_alive(x) for x in old), old)
        self.assertTrue(self.pid_alive(new[0]))
        self.assertLess(self.idx("schtasks /End"), self.idx("schtasks /Run"))

    def test_reinstall_kills_old_server_that_ignores_end(self):
        self.assertEqual(self.run_install().returncode, 0)
        old = read(self.pids).split()
        r = self.run_install(SCHTASKS_END_NOKILL="1", SHELTIE_STOP_WAIT="1")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertFalse(any(self.pid_alive(x) for x in old), old)

    def test_old_server_health_is_not_success(self):
        # started가 설치 시작보다 이른 서버(옛 서버)만 답하면 실패로 본다.
        r = self.run_install(STUB_OLD_STARTED="1")
        self.assertEqual(r.returncode, 7, r.stderr + r.stdout)

    def test_port_held_by_other_program(self):
        s = socket.socket()
        s.bind(("127.0.0.1", int(self.port)))
        s.listen(50)
        try:
            r = self.run_install(SHELTIE_STOP_WAIT="1")
        finally:
            s.close()
        self.assertEqual(r.returncode, 9, r.stderr + r.stdout)
        self.assertIn(self.port, r.stderr)
        self.assertFalse(any(x.startswith("schtasks /Run") for x in self.calls()))

    def test_http_source_rejected(self):
        r = self.run_install(SHELTIE_SRC="http://example.invalid/sheltie.py")
        self.assertEqual(r.returncode, 1)
        self.assertIn("https", r.stderr)
        self.assertFalse(any(x.startswith("schtasks") for x in self.calls()))

    def test_missing_source_aborts(self):
        r = self.run_install(SHELTIE_SRC=os.path.join(self.tmp, "nope.py"))
        self.assertEqual(r.returncode, 1)
        self.assertFalse(any(x.startswith("schtasks") for x in self.calls()))

    def test_empty_source_aborts(self):
        empty = os.path.join(self.tmp, "empty.py")
        open(empty, "w").close()
        r = self.run_install(SHELTIE_SRC=empty)
        self.assertEqual(r.returncode, 1)
        self.assertIn("비어", r.stderr)
        self.assertFalse(os.path.exists(self.server_path()))

    def test_syntax_error_source_aborts(self):
        bad = os.path.join(self.tmp, "bad.py")
        with open(bad, "w") as f:
            f.write("def (\n")
        r = self.run_install(SHELTIE_SRC=bad)
        self.assertEqual(r.returncode, 1)
        self.assertIn("파이썬", r.stderr)
        self.assertFalse(os.path.exists(self.server_path()))
        self.assertFalse(any(x.startswith("schtasks") for x in self.calls()))

    # ---- herdr-core 자동 설치(고정 버전 zip + SHA256 검증). --version에 답하는 진짜 exe는 시험에서 못 만들어 성공 경로는 뺐다.
    def hc_core_exe(self):
        return os.path.join(self.local, "Programs", "herdr-core", "herdr-core.exe")

    def test_hc_checksum_mismatch_installs_nothing_and_continues(self):
        r = self.run_install(SHELTIE_HC="", SHELTIE_HC_SHA256="0" * 64)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("체크섬", r.stdout)
        self.assertIn("자동 설치를 못 했다", r.stdout)
        self.assertIn("herdr-core 없이 설치한다", r.stdout)
        self.assertFalse(os.path.exists(self.hc_core_exe()))
        self.assertEqual(read_json(self.cfg_path())["hc_path"], "")
        self.assertTrue(any(x.startswith("schtasks /Create") for x in self.calls()))

    def test_hc_download_missing_local_file_continues(self):
        r = self.run_install(SHELTIE_HC="", SHELTIE_HC_URL=os.path.join(self.tmp, "nope.zip"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("파일이 없다", r.stdout)
        self.assertIn("herdr-core 없이 설치한다", r.stdout)

    def test_hc_zip_that_does_not_run_is_not_installed(self):
        r = self.run_install(SHELTIE_HC="")  # 해시는 맞지만 herdr-core.exe가 가짜라 --version이 안 돈다
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("자동 설치를 못 했다", r.stdout)
        self.assertFalse(os.path.exists(self.hc_core_exe()))
        self.assertEqual(read_json(self.cfg_path())["hc_path"], "")

    def test_hc_arm_skips_download(self):
        r = self.run_install(SHELTIE_HC="", SHELTIE_HC_URL=None, SHELTIE_HC_SHA256=None,
                             PROCESSOR_ARCHITEW6432="ARM64")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("이 CPU용 빌드가 없다", r.stdout)
        self.assertIn("herdr-core 없이 설치한다", r.stdout)

    def test_hc_http_source_rejected_install_continues(self):
        r = self.run_install(SHELTIE_HC="", SHELTIE_HC_URL="http://example.invalid/hc.zip")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("https로만", r.stdout)
        self.assertIn("herdr-core 없이 설치한다", r.stdout)

    def test_no_hc_switch_and_env_skip_download(self):
        for args, extra in (((), {"SHELTIE_NO_HC": "1"}), (("-NoHc",), {})):
            r = self.run_install(*args, SHELTIE_HC="", **extra)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertNotIn("체크섬", r.stdout)
            self.assertIn("herdr-core 없이 설치한다", r.stdout)

    def test_hc_default_source_is_pinned_url_in_dry_run(self):
        r = self.run_install("-DryRun", SHELTIE_HC="", SHELTIE_HC_URL=None, SHELTIE_HC_SHA256=None,
                             PROCESSOR_ARCHITEW6432="AMD64")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("+ herdr-core 0.1.21 설치(https://releases.herdr.dev/core/0.1.21/herdr-core-windows-x86_64.zip)",
                      r.stdout)

    def test_hc_dry_run_shows_mirror_only_for_pinned_source(self):
        # 실제 받기(Invoke-WebRequest)는 시험에서 가짜로 못 바꿔 네트워크로 나가지 않게 미러 경로는 dry-run으로만 본다.
        r = self.run_install("-DryRun", SHELTIE_HC="", SHELTIE_HC_URL=None, SHELTIE_HC_SHA256=None,
                             PROCESSOR_ARCHITEW6432="AMD64")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("+   실패하면 미러에서 받기(https://github.com/hyperfocuslab/sheltie/releases/download/hc-0.1.21/"
                      "herdr-core-windows-x86_64.zip)", r.stdout)
        r = self.run_install("-DryRun", SHELTIE_HC="")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("미러", r.stdout)

    def test_hc_override_local_file_failure_has_no_mirror(self):
        r = self.run_install(SHELTIE_HC="", SHELTIE_HC_SHA256="0" * 64)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("체크섬", r.stdout)
        self.assertNotIn("미러", r.stdout)

    def test_hc_dry_run_downloads_nothing(self):
        r = self.run_install("-DryRun", SHELTIE_HC="")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("+ herdr-core 0.1.21 설치(%s)" % self.hc_zip, r.stdout)
        self.assertFalse(os.path.exists(self.hc_core_exe()))

    def test_hc_present_does_not_download(self):
        r = self.run_install(SHELTIE_HC_SHA256="0" * 64)  # SHELTIE_HC=self.hc가 기본
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("체크섬", r.stdout)
        self.assertEqual(read_json(self.cfg_path())["hc_path"], self.hc)

    def test_existing_herdr_core_exe_is_kept_and_used(self):
        d = os.path.join(self.local, "Programs", "herdr-core")
        os.makedirs(d)
        open(self.hc_core_exe(), "w").write("mine")
        r = self.run_install(SHELTIE_HC="")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(read(self.hc_core_exe()), "mine")
        self.assertEqual(read_json(self.cfg_path())["hc_path"], self.hc_core_exe())
        self.assertNotIn("체크섬", r.stdout)

    def test_hc_not_installed_when_tailscale_check_fails(self):
        r = self.run_install(SHELTIE_HC="", TS_STATE="NeedsLogin")
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("체크섬", r.stdout)

    def test_uninstall_keeps_herdr_core(self):
        d = os.path.join(self.local, "Programs", "herdr-core")
        os.makedirs(d)
        open(self.hc_core_exe(), "w").write("mine")
        self.assertEqual(self.run_install(SHELTIE_HC="").returncode, 0)
        self.assertEqual(self.run_install("-Uninstall", SHELTIE_HC="").returncode, 0)
        self.assertTrue(os.path.exists(self.hc_core_exe()))

    # ---- sheltie 명령
    def test_sheltie_cmd_created_runs_and_passes_args_and_exit_code(self):
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        text = read(self.cmd_path())
        lines = text.splitlines()
        self.assertEqual(lines[0], "@echo off")
        self.assertTrue(lines[1].startswith("rem sheltie-command"))
        self.assertIn('"%s" "%s" %%*' % (sys.executable, self.server_path()), text)
        out = os.path.join(self.tmp, "argv.json")
        env = self.env(ARGV_OUT=out, RUN_RC="7")
        w = subprocess.run(["cmd", "/c", self.cmd_path(), "run", "gemini", "--flag"], env=env, capture_output=True, timeout=60)
        self.assertEqual(w.returncode, 7, w.stderr)
        self.assertEqual(read_json(out), ["run", "gemini", "--flag"])

    def test_sheltie_cmd_path_added_keeping_kind_and_unexpanded(self):
        bindir = os.path.join(self.local, "sheltie", "bin")
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.user_path(), (r"%USERPROFILE%\bin;C:\existing;" + bindir, "expand"))
        self.assertEqual(self.run_install().returncode, 0)  # 두 번째는 중복 없이
        self.assertEqual(self.user_path()[0].count(bindir), 1)

    def test_sheltie_cmd_path_keeps_plain_string_kind(self):
        self.set_user_path(r"C:\one", "string")
        self.assertEqual(self.run_install().returncode, 0)
        v, kind = self.user_path()
        self.assertEqual(kind, "string")
        self.assertTrue(v.startswith(r"C:\one;"))

    def test_sheltie_cmd_removed_on_uninstall_with_path_entry(self):
        self.assertEqual(self.run_install().returncode, 0)
        self.assertEqual(self.run_install("-Uninstall").returncode, 0)
        self.assertFalse(os.path.exists(self.cmd_path()))
        self.assertEqual(self.user_path(), (r"%USERPROFILE%\bin;C:\existing", "expand"))

    def test_sheltie_cmd_foreign_file_untouched(self):
        os.makedirs(os.path.dirname(self.cmd_path()))
        with open(self.cmd_path(), "wb") as f:
            f.write(b"@echo mine\r\n")
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(self.cmd_path(), "rb") as f:
            self.assertEqual(f.read(), b"@echo mine\r\n")
        self.assertIn("건드리지 않았다", r.stdout)
        self.assertEqual(self.user_path()[0], r"%USERPROFILE%\bin;C:\existing")
        self.assertEqual(self.run_install("-Uninstall").returncode, 0)
        self.assertTrue(os.path.exists(self.cmd_path()))

    def test_sheltie_cmd_reinstall_overwrites_ours(self):
        self.assertEqual(self.run_install().returncode, 0)
        with open(self.cmd_path(), "w") as f:
            f.write("@echo off\r\nrem sheltie-command: x\r\nold\r\n")
        self.assertEqual(self.run_install().returncode, 0)
        self.assertNotIn("old", read(self.cmd_path()))

    def test_sheltie_cmd_dry_run_writes_nothing(self):
        r = self.run_install("-DryRun")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("sheltie.cmd", r.stdout)
        self.assertFalse(os.path.exists(self.cmd_path()))
        self.assertEqual(self.user_path()[0], r"%USERPROFILE%\bin;C:\existing")

    def check_uninstall(self, *args, **extra):
        self.assertEqual(self.run_install().returncode, 0)
        open(self.log, "w").close()
        r = self.run_install(*args, **extra)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        c = self.calls()
        self.assertIn("schtasks /End /TN Sheltie", c)
        self.assertIn("schtasks /Delete /TN Sheltie /F", c)
        self.assertIn("tailscale serve --https=%s off" % self.port, c)
        self.assertFalse(os.path.exists(self.server_path()))
        self.assertTrue(os.path.exists(self.cfg_path()), "설정과 토큰은 남긴다")
        self.assertIn("해제 완료", r.stdout)

    def test_uninstall(self):
        self.check_uninstall("-Uninstall")

    def test_uninstall_by_env(self):
        self.check_uninstall(SHELTIE_UNINSTALL="1")


OLD_TASK = "Sheltie herdr-remote"  # 이전 설치 이름: 옮기기 시험용


@unittest.skipUnless(PS and os.name == "nt", "Windows PowerShell이 없다(Windows에서만 돈다)")
class InstallPs1MigrateTest(InstallPs1Test):
    """이전 설치(옛 경로)가 있는 가짜 프로필에서 설치·해제. 부모 테스트는 상속만 하므로 비운다."""

    def setUp(self):
        super().setUp()
        self.old_data = os.path.join(self.local, "herdr-remote")
        self.old_server = os.path.join(self.old_data, "herdr_remote.py")
        self.old_cfg_dir = os.path.join(self.profile, ".config", "herdr-remote")
        self.new_cfg_dir = os.path.join(self.profile, ".config", "sheltie")

    def make_old_install(self, hooks=True):
        os.makedirs(self.old_data)
        os.makedirs(self.old_cfg_dir)
        for n, text in (("herdr_remote.py", "# old\n"), ("windows-task.xml", "<Task/>"), ("herdr-remote.log", "old\n")):
            with open(os.path.join(self.old_data, n), "w") as f:
                f.write(text)
        cfg = {"token": "tok-OLD-keep-this-token-000", "port": int(self.port), "hc_path": "x",
               "apns_key_path": "~/.config/herdr-remote/AuthKey.p8",
               "extra_abs": os.path.join(self.old_cfg_dir, "sub", "file")}
        with open(os.path.join(self.old_cfg_dir, "config.json"), "w") as f:
            json.dump(cfg, f)
        with open(os.path.join(self.old_cfg_dir, "devices.json"), "w") as f:
            json.dump([{"token": "dev1"}], f)
        with open(os.path.join(self.old_cfg_dir, "AuthKey.p8"), "w") as f:
            f.write("KEY")
        if hooks:
            cl, cx = self.hook_files()
            os.makedirs(os.path.dirname(cl))
            os.makedirs(os.path.dirname(cx))
            old_cl = '& "%s" "%s" hook claude' % (sys.executable, self.old_server)
            old_cx = "powershell.exe -NoProfile -Command \"& '%s' '%s' hook codex\"" % (sys.executable, self.old_server)
            with open(cl, "w", encoding="utf-8") as f:
                json.dump({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "mine"}]},
                                              {"hooks": [{"type": "command", "command": old_cl, "shell": "powershell"}]}]}}, f)
            with open(cx, "w", encoding="utf-8") as f:
                json.dump({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": old_cx}]}]}}, f)

    def snapshot(self):
        out = {}
        for base in (self.profile, self.local):
            for root, dirs, files in os.walk(base):
                if root == self.profile and "AppData" in dirs:
                    dirs.remove("AppData")  # PowerShell이 스스로 만드는 캐시
                for n in dirs + files:
                    p = os.path.join(root, n)
                    out[p] = read(p) if os.path.isfile(p) else None
        return out

    def test_install_moves_old_install(self):
        self.make_old_install()
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        c = self.calls()
        i_end = c.index("schtasks /End /TN %s" % OLD_TASK)
        i_del = c.index("schtasks /Delete /TN %s /F" % OLD_TASK)
        # 이전 작업은 새 작업 등록 전에 멈추고, 지우는 건 새 서버가 뜬 뒤
        self.assertTrue(i_end < self.idx("schtasks /Create") < self.idx("schtasks /Run /TN Sheltie") < i_del, c)
        cfg = read_json(self.cfg_path())
        self.assertEqual(cfg["token"], "tok-OLD-keep-this-token-000")
        self.assertEqual(cfg["apns_key_path"], "~/.config/sheltie/AuthKey.p8")
        self.assertEqual(os.path.normcase(cfg["extra_abs"]),
                         os.path.normcase(os.path.join(self.new_cfg_dir, "sub", "file")))
        self.assertEqual(read_json(os.path.join(self.new_cfg_dir, "devices.json")), [{"token": "dev1"}])
        self.assertEqual(read(os.path.join(self.new_cfg_dir, "AuthKey.p8")), "KEY")
        self.assertFalse(os.path.exists(self.old_cfg_dir))
        self.assertFalse(os.path.exists(self.old_data))
        cl, cx = self.hook_files()
        for p in (cl, cx):
            self.assertNotIn("herdr_remote.py", read(p), p)
        stop = [h["command"] for g in read_json(cl)["hooks"]["Stop"] for h in g["hooks"]]
        self.assertEqual(stop, ["mine", '& "%s" "%s" hook claude' % (sys.executable, self.server_path())])
        self.assertEqual(len(read_json(cx)["hooks"]["Stop"]), 1)

    def test_existing_new_config_is_not_overwritten(self):
        self.make_old_install(hooks=False)
        os.makedirs(self.new_cfg_dir)
        with open(self.cfg_path(), "w") as f:
            json.dump({"token": "tok-NEW-0000000000000000", "port": int(self.port)}, f)
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("옮기지 않고", r.stdout)
        self.assertEqual(read_json(self.cfg_path())["token"], "tok-NEW-0000000000000000")
        self.assertEqual(read_json(os.path.join(self.old_cfg_dir, "config.json"))["token"], "tok-OLD-keep-this-token-000")
        self.assertFalse(os.path.exists(self.old_data))

    def test_move_failure_keeps_everything(self):
        self.make_old_install()
        # 다른 프로세스의 현재 폴더면 Windows는 그 폴더의 이름 바꾸기를 막는다
        holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], cwd=self.old_cfg_dir)
        try:
            r = self.run_install()
        finally:
            holder.kill()
            holder.wait()
        self.assertEqual(r.returncode, 1, r.stderr + r.stdout)
        self.assertIn("아무것도 지우지 않았다", r.stderr)
        self.assertEqual(read_json(os.path.join(self.old_cfg_dir, "config.json"))["token"], "tok-OLD-keep-this-token-000")
        self.assertTrue(os.path.exists(self.old_server))
        self.assertFalse(os.path.exists(self.new_cfg_dir))
        c = self.calls()
        self.assertIn("schtasks /Run /TN %s" % OLD_TASK, c)
        self.assertNotIn("schtasks /Delete /TN %s /F" % OLD_TASK, c)
        self.assertFalse(any(x.startswith("schtasks /Create") for x in c), c)
        self.assertIn("herdr_remote.py", read(self.hook_files()[0]))

    def assert_old_untouched(self):
        self.assertTrue(os.path.exists(self.old_server))
        self.assertEqual(read_json(os.path.join(self.old_cfg_dir, "config.json"))["token"], "tok-OLD-keep-this-token-000")
        self.assertFalse(os.path.exists(self.new_cfg_dir))

    def test_download_failure_leaves_old_install(self):
        self.make_old_install()
        r = self.run_install(SHELTIE_SRC=os.path.join(self.tmp, "missing.py"))
        self.assertEqual(r.returncode, 1, r.stderr + r.stdout)
        self.assert_old_untouched()
        self.assertFalse(any(x.startswith("schtasks") for x in self.calls()), self.calls())
        self.assertIn("herdr_remote.py", read(self.hook_files()[0]))

    def test_task_create_failure_keeps_old_files(self):
        self.make_old_install()
        r = self.run_install(SCHTASKS_CREATE_RC="1")
        self.assertEqual(r.returncode, 5, r.stderr + r.stdout)
        self.assertTrue(os.path.exists(self.old_server))
        self.assertNotIn("schtasks /Delete /TN %s /F" % OLD_TASK, self.calls())
        self.assertEqual(read_json(self.cfg_path())["token"], "tok-OLD-keep-this-token-000")

    def test_no_hooks_keeps_old_server_file(self):
        self.make_old_install()
        r = self.run_install("-NoHooks")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertTrue(os.path.exists(self.old_server))
        self.assertFalse(os.path.exists(os.path.join(self.old_data, "windows-task.xml")))
        self.assertIn("schtasks /Delete /TN %s /F" % OLD_TASK, self.calls())
        self.assertIn(self.hook_files()[0], r.stderr)
        self.assertIn(self.old_data, r.stderr)

    def test_linked_old_config_stops_install(self):
        self.make_old_install()
        real = os.path.join(self.tmp, "dotfiles-cfg")
        os.rename(self.old_cfg_dir, real)
        subprocess.run(["cmd", "/c", "mklink", "/J", self.old_cfg_dir, real], check=True, capture_output=True)
        r = self.run_install()
        self.assertEqual(r.returncode, 1, r.stderr + r.stdout)
        self.assertIn("링크", r.stderr)
        self.assertFalse(os.path.exists(self.new_cfg_dir))
        self.assertTrue(os.path.exists(self.old_server))
        self.assertFalse(any(x.startswith("schtasks") for x in self.calls()), self.calls())

    def test_uninstall_also_removes_old_task(self):
        self.make_old_install()
        r = self.run_install("-Uninstall")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        c = self.calls()
        self.assertIn("schtasks /End /TN %s" % OLD_TASK, c)
        self.assertIn("schtasks /Delete /TN %s /F" % OLD_TASK, c)
        self.assertFalse(os.path.exists(self.old_server))
        self.assertTrue(os.path.exists(os.path.join(self.old_cfg_dir, "config.json")), "설정과 토큰은 남긴다")
        self.assertIn(self.old_cfg_dir, r.stdout)
        cl, cx = self.hook_files()
        self.assertEqual(read_json(cl), {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "mine"}]}]}})
        self.assertEqual(read_json(cx), {})

    def test_dry_run_with_old_install_changes_nothing(self):
        self.make_old_install()
        before = self.snapshot()
        r = self.run_install("-DryRun")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("+ schtasks /End /TN %s" % OLD_TASK, r.stdout)
        self.assertIn("+ 이동 %s -> %s" % (self.old_cfg_dir, self.new_cfg_dir), r.stdout)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(any(x.startswith("schtasks") for x in self.calls()), self.calls())


for _n in [n for n in dir(InstallPs1Test) if n.startswith("test_")]:
    setattr(InstallPs1MigrateTest, _n, None)  # 상속된 기존 테스트는 InstallPs1Test에서 이미 돈다


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
        self.assertEqual(msgs["help1"], "Sheltie 설치기(Windows). 사용: powershell -File install.ps1 [-DryRun] [-Uninstall] [-Help]")
        self.assertTrue(msgs["no_hc"].startswith("hc(herdr-core)가 없다."))
        self.assertIn("%LOCALAPPDATA%\\Programs\\herdr-core\\hc.exe", msgs["no_hc"])

    def test_hc_installer_and_command_options_documented(self):
        t = read(INSTALL)
        for want in ("-NoHc", "SHELTIE_NO_HC", "SHELTIE_HC_URL", "SHELTIE_HC_SHA256", "HC_PIN_VERSION",
                     "SHELTIE_ENV_KEY", "DoNotExpandEnvironmentNames", "rem sheltie-command"):
            self.assertIn(want, t, want)
        self.assertIn("-NoHc", messages()["help6"])

    def test_hc_mirror_url_same_in_both_installers(self):
        urls = {}
        for name in ("install.sh", "install.ps1"):
            m = re.search(r'HC_MIRROR_URL\s*=\s*"([^"]+)"', read(os.path.join(ROOT, name)))
            self.assertIsNotNone(m, name)
            urls[name] = m.group(1)
        self.assertEqual(urls["install.sh"], urls["install.ps1"])
        self.assertEqual(urls["install.sh"], "https://github.com/hyperfocuslab/sheltie/releases/download/hc-$HC_PIN_VERSION")

    def test_hc_mirror_messages(self):
        msgs = messages()
        self.assertEqual(msgs["hc_mirror"], "herdr-core를 원래 주소에서 못 받았다({0}). 미러에서 받는다({1})")
        self.assertEqual(msgs["hc_dry_mirror"], "+   실패하면 미러에서 받기({0})")

    def test_dry_run_env_supported(self):
        self.assertIn("SHELTIE_DRY_RUN", read(INSTALL))

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
