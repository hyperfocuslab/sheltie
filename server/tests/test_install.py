import json
import re
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
INSTALL = os.path.join(ROOT, "install.sh")

# 서버 대역: init은 설정을 만들고, pair는 PAIR_FAIL이 있으면 실패한다.
STUB_SERVER = '''#!/usr/bin/env python3
import json, os, sys
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
elif cmd == "codex-trust":
    with open(os.environ["FAKE_LOG"], "a") as f:
        f.write("server codex-trust\\n")
    if os.environ.get("CODEX_TRUST_FAIL"):
        print("codex 실행 파일을 찾지 못했다", file=sys.stderr)
        sys.exit(1)
    print("Codex 훅 신뢰 등록: 5개")
'''

FAKE_SH = '#!/bin/sh\necho "$(basename "$0") $*" >> "$FAKE_LOG"\n%s\n'


def read(p):
    with open(p) as f:
        return f.read()


def read_json(p):
    return json.loads(read(p))


@unittest.skipIf(os.name == "nt", "맥 전용: install.sh(sh·launchd·plist). Windows 설치기는 test_install_ps1.py")
class InstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, "home")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.home)
        os.makedirs(self.bin)
        self.log = os.path.join(self.tmp, "calls.log")
        open(self.log, "w").close()
        self.stub = os.path.join(self.tmp, "stub_server.py")
        with open(self.stub, "w") as f:
            f.write(STUB_SERVER)
        os.symlink(sys.executable, os.path.join(self.bin, "python3"))
        self.fake("launchctl", '[ "$1" = print ] && exit 113; exit ${LAUNCHCTL_RC:-0}')
        self.fake("curl", "exit 0")
        self.fake("hc", "echo '{\"terminals\":[]}'")
        self.fake("tailscale",
                  'case "$1" in status) if [ "$2" = "--json" ]; then echo "{\\"BackendState\\":\\"${TS_STATE:-Running}\\"}"; '
                  'fi;; '
                  'serve) if [ "$2" = status ] && [ "${3:-}" = --json ]; then cat "$TS_SERVE_JSON" 2>/dev/null || echo "{}"; '
                  'elif [ "$2" = status ]; then cat "$TS_SERVE_FILE" 2>/dev/null; '
                  'else [ "${TS_SERVE_RC:-0}" = 0 ] || exit 1; fi;; esac')
        self.ts_serve_file = os.path.join(self.tmp, "ts_serve.txt")
        self.ts_serve_json = os.path.join(self.tmp, "ts_serve.json")
        self.sys_dir = os.path.join(self.tmp, "SysLaunchAgents")  # 실제 /Library/LaunchAgents 대신

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fake(self, name, body):
        p = os.path.join(self.bin, name)
        with open(p, "w") as f:
            f.write(FAKE_SH % body)
        os.chmod(p, 0o755)

    def run_install(self, *args, **extra):
        env = {"HOME": self.home, "PATH": self.bin + ":/usr/bin:/bin", "FAKE_LOG": self.log,
               "TS_SERVE_FILE": self.ts_serve_file, "SHELTIE_SRC": self.stub,
               "SHELTIE_HEALTH_TRIES": "2", "TS_SERVE_JSON": self.ts_serve_json,
               "SHELTIE_SYS_AGENTS_DIR": self.sys_dir}
        env.update(extra)
        return subprocess.run(["sh", INSTALL] + list(args), env=env, capture_output=True, text=True, timeout=60,
                              start_new_session=True)  # 제어 터미널을 끊는다: 설치기가 /dev/tty로 묻다 멈추지 않게

    def calls(self):
        with open(self.log) as f:
            return [l.strip() for l in f if l.strip()]

    def plist(self):
        return os.path.join(self.home, "Library/LaunchAgents/com.hyperfocuslab.sheltie.plist")

    def test_normal_install_order(self):
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        c = self.calls()
        uid = str(os.getuid())
        i_boot = next(i for i, x in enumerate(c) if x.startswith("launchctl bootout"))
        i_strap = next(i for i, x in enumerate(c) if x.startswith("launchctl bootstrap"))
        i_serve = next(i for i, x in enumerate(c) if x.startswith("tailscale serve --bg"))
        i_health = next(i for i, x in enumerate(c) if x.startswith("curl"))
        self.assertTrue(i_boot < i_strap < i_serve < i_health, c)
        self.assertIn("launchctl bootstrap gui/%s %s" % (uid, self.plist()), c)
        self.assertIn("tailscale serve --bg --https=8790 http://127.0.0.1:8790", c)
        self.assertIn("PAIR-OK", r.stdout)
        server = os.path.join(self.home, ".local/share/sheltie/sheltie.py")
        self.assertEqual(stat.S_IMODE(os.stat(server).st_mode), 0o755)
        text = read(self.plist())
        self.assertIn("<string>%s</string><string>serve</string>" % server, text)
        self.assertIn(os.path.join(self.home, "Library/Logs/sheltie.log"), text)
        self.assertIn(self.bin, text)  # hc 위치가 PATH에 들어간다
        self.assertNotRegex(text, r"@[A-Z]+@")
        cfg = read_json(os.path.join(self.home, ".config/sheltie/config.json"))
        self.assertEqual(cfg["hc_path"], os.path.join(self.bin, "hc"))

    def test_health_sends_token_via_stdin_not_argv(self):
        self.run_install()
        curl = [x for x in self.calls() if x.startswith("curl")]
        self.assertTrue(curl)
        self.assertNotIn("tok-0123456789", " ".join(curl))

    def test_custom_port(self):
        r = self.run_install(SHELTIE_PORT="18790")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("tailscale serve --bg --https=18790 http://127.0.0.1:18790", self.calls())
        cfg = read_json(os.path.join(self.home, ".config/sheltie/config.json"))
        self.assertEqual(cfg["port"], 18790)

    def test_bad_port_rejected(self):
        for bad in ("abc", "0", "70000", "80;rm"):
            r = self.run_install(SHELTIE_PORT=bad)
            self.assertEqual(r.returncode, 2, bad)

    def test_no_hc(self):
        os.remove(os.path.join(self.bin, "hc"))
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("herdr-core 없이 설치한다", r.stdout)
        self.assertTrue(os.path.exists(self.plist()))
        cfg = read_json(os.path.join(self.home, ".config/sheltie/config.json"))
        self.assertEqual(cfg["hc_path"], "")

    def test_no_tailscale(self):
        os.remove(os.path.join(self.bin, "tailscale"))
        r = self.run_install(SHELTIE_TAILSCALE="/nonexistent/tailscale")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("tailscale", r.stderr)
        self.assertIn("설치", r.stderr)

    def test_tailscale_logged_out(self):
        r = self.run_install(TS_STATE="NeedsLogin")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("로그인", r.stderr)
        self.assertFalse(any(x.startswith("launchctl") for x in self.calls()))

    def test_bootstrap_failure_prints_sudo(self):
        r = self.run_install(LAUNCHCTL_RC="5")
        self.assertEqual(r.returncode, 5)
        self.assertIn("sudo install -o root -g wheel", r.stderr)
        self.assertIn("/Library/LaunchAgents", r.stderr)
        self.assertFalse(os.path.exists(self.plist()))
        self.assertFalse(any(x.startswith("tailscale serve --bg") for x in self.calls()))

    def test_serve_failure(self):
        r = self.run_install(TS_SERVE_RC="1")
        self.assertEqual(r.returncode, 6)
        self.assertIn("tailscale serve", r.stderr)

    def test_pair_missing_not_ignored(self):
        r = self.run_install(PAIR_FAIL="1")
        self.assertEqual(r.returncode, 4)
        self.assertIn("pair", r.stderr)

    def test_rerun_idempotent(self):
        self.assertEqual(self.run_install().returncode, 0)
        cfg_path = os.path.join(self.home, ".config/sheltie/config.json")
        tok = read_json(cfg_path)["token"]
        first = read(self.plist())
        with open(self.ts_serve_file, "w") as f:
            f.write("https://x.ts.net:8790 (tailnet only)\n|-- / proxy http://127.0.0.1:8790\n")
        open(self.log, "w").close()
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(read_json(cfg_path)["token"], tok)
        self.assertEqual(read(self.plist()), first)
        self.assertFalse(any(x.startswith("tailscale serve --bg") for x in self.calls()))

    def test_rerun_with_system_plist_keeps_service(self):
        # 외장 홈 맥: sudo로 /Library/LaunchAgents에 둔 뒤 재실행하면 bootout 없이 kickstart만
        os.makedirs(self.sys_dir)
        with open(os.path.join(self.sys_dir, "com.hyperfocuslab.sheltie.plist"), "w") as f:
            f.write("<plist/>")
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        c = self.calls()
        uid = str(os.getuid())
        launch = [x for x in c if x.startswith("launchctl")]
        self.assertEqual(launch, ["launchctl kickstart -k gui/%s/com.hyperfocuslab.sheltie" % uid])
        self.assertFalse(os.path.exists(self.plist()))
        i_kick = c.index(launch[0])
        i_serve = next(i for i, x in enumerate(c) if x.startswith("tailscale serve --bg"))
        i_health = next(i for i, x in enumerate(c) if x.startswith("curl"))
        self.assertTrue(i_kick < i_serve < i_health, c)
        self.assertIn("PAIR-OK", r.stdout)

    def test_system_plist_kickstart_failure(self):
        os.makedirs(self.sys_dir)
        with open(os.path.join(self.sys_dir, "com.hyperfocuslab.sheltie.plist"), "w") as f:
            f.write("<plist/>")
        r = self.run_install(LAUNCHCTL_RC="3")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("launchctl bootstrap", r.stderr)
        self.assertFalse(any(x.startswith("launchctl bootout") for x in self.calls()))

    def test_funnel_on_port_stops_install(self):
        with open(self.ts_serve_file, "w") as f:
            f.write("https://x.ts.net:8790 (Funnel on)\n|-- / proxy http://127.0.0.1:8790\n")
        with open(self.ts_serve_json, "w") as f:
            json.dump({"TCP": {"8790": {"HTTPS": True}},
                       "Web": {"x.ts.net:8790": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8790"}}}},
                       "AllowFunnel": {"x.ts.net:8790": True}}, f)
        r = self.run_install()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Funnel", r.stderr)
        c = self.calls()
        self.assertFalse(any(x.startswith("tailscale serve --bg") or x.endswith(" off") for x in c), c)
        # 다른 포트의 Funnel은 상관없다
        with open(self.ts_serve_json, "w") as f:
            json.dump({"AllowFunnel": {"x.ts.net:443": True}}, f)
        open(self.log, "w").close()
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_uninstall(self):
        self.run_install()
        open(self.log, "w").close()
        r = self.run_install("--uninstall")
        self.assertEqual(r.returncode, 0, r.stderr)
        c = self.calls()
        self.assertTrue(any(x.startswith("launchctl bootout") for x in c))
        self.assertIn("tailscale serve --https=8790 off", c)
        self.assertFalse(os.path.exists(self.plist()))
        self.assertTrue(os.path.exists(os.path.join(self.home, ".config/sheltie/config.json")))

    def test_dry_run_changes_nothing(self):
        r = self.run_install("--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("launchctl bootstrap", r.stdout)
        self.assertIn("tailscale serve --bg --https=8790", r.stdout)
        self.assertFalse(any(x.startswith(("launchctl", "curl")) for x in self.calls()))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".local")))
        self.assertFalse(os.path.exists(self.plist()))

    def test_builtin_plist_matches_template(self):
        # 템플릿 파일 없이(curl | sh 경로) 만든 plist가 템플릿으로 만든 것과 같아야 한다.
        self.assertEqual(self.run_install().returncode, 0)
        with_tpl = read(self.plist())
        lone = os.path.join(self.tmp, "lone")
        os.makedirs(lone)
        shutil.copy(INSTALL, lone)
        env = {"HOME": self.home, "PATH": self.bin + ":/usr/bin:/bin", "FAKE_LOG": self.log,
               "TS_SERVE_FILE": self.ts_serve_file, "SHELTIE_SRC": self.stub, "SHELTIE_HEALTH_TRIES": "2",
               "TS_SERVE_JSON": self.ts_serve_json, "SHELTIE_SYS_AGENTS_DIR": self.sys_dir}
        r = subprocess.run(["sh", os.path.join(lone, "install.sh")], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(read(self.plist()), with_tpl)

    def lone_install(self, **extra):
        lone = os.path.join(self.tmp, "lone2")
        os.makedirs(lone, exist_ok=True)
        shutil.copy(INSTALL, lone)
        env = {"HOME": self.home, "PATH": self.bin + ":/usr/bin:/bin", "FAKE_LOG": self.log,
               "TS_SERVE_FILE": self.ts_serve_file, "SHELTIE_HEALTH_TRIES": "2", "STUB_SRC": self.stub,
               "TS_SERVE_JSON": self.ts_serve_json, "SHELTIE_SYS_AGENTS_DIR": self.sys_dir,
               "STUB_MANIFEST": self.stub_manifest()}
        env.update(extra)
        return subprocess.run(["sh", os.path.join(lone, "install.sh")], env=env, capture_output=True, text=True)

    def stub_manifest(self, sha=None):
        """서버 대역의 릴리스 매니페스트(manifest.json) 경로."""
        import hashlib
        p = os.path.join(self.tmp, "manifest-%s.json" % (sha or "ok"))
        with open(self.stub, "rb") as f:
            h = sha or hashlib.sha256(f.read()).hexdigest()
        with open(p, "w") as f:
            json.dump({"version": "1.0.0", "files": {"sheltie.py": h, "install.sh": "0" * 64}}, f)
        return p

    # 가짜 curl: -o 파일에 URL이 manifest.json이면 $STUB_MANIFEST, 아니면 $STUB_SRC를 쓴다.
    FAKE_CURL_DL = ('url=; out=; while [ $# -gt 0 ]; do case "$1" in -o) out=$2; shift;; https://*) url=$1;; esac; '
                    'shift; done; [ -z "$out" ] || [ "$out" = /dev/null ] || case "$url" in '
                    '*/manifest.json) [ -f "${STUB_MANIFEST:-}" ] || exit 22; cat "$STUB_MANIFEST" > "$out";; '
                    '*) cat "$STUB_SRC" > "$out";; esac; exit 0')
    RELEASE_URL = "https://github.com/hyperfocuslab/sheltie/releases/latest/download/"

    def test_default_source_is_github_raw_when_piped(self):
        self.fake("curl", self.FAKE_CURL_DL)
        r = self.lone_install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(any(x.endswith(self.RELEASE_URL + "sheltie.py") for x in self.calls()), self.calls())
        self.assertTrue(any(x.endswith(self.RELEASE_URL + "manifest.json") for x in self.calls()), self.calls())
        self.assertFalse(any("raw.githubusercontent.com" in x for x in self.calls()))

    def test_release_server_hash_mismatch_aborts(self):
        self.fake("curl", self.FAKE_CURL_DL)
        r = self.lone_install(STUB_MANIFEST=self.stub_manifest("a" * 64))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("manifest.json", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".local/share/sheltie/sheltie.py")))
        self.assertFalse(any(x.startswith("launchctl") for x in self.calls()))

    def test_release_manifest_missing_aborts(self):
        self.fake("curl", self.FAKE_CURL_DL)
        r = self.lone_install(STUB_MANIFEST=os.path.join(self.tmp, "none.json"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("manifest.json", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".local/share/sheltie/sheltie.py")))

    def test_piped_from_folder_with_server_file_still_uses_github(self):
        # curl | sh 는 $0이 "sh"라서, 서버 파일이 있는 폴더(이 저장소 루트)에서 돌려도 그 파일을 쓰면 안 된다.
        self.fake("curl", self.FAKE_CURL_DL)
        env = {"HOME": self.home, "PATH": self.bin + ":/usr/bin:/bin", "FAKE_LOG": self.log,
               "TS_SERVE_FILE": self.ts_serve_file, "SHELTIE_HEALTH_TRIES": "2", "STUB_SRC": self.stub,
               "TS_SERVE_JSON": self.ts_serve_json, "SHELTIE_SYS_AGENTS_DIR": self.sys_dir,
               "STUB_MANIFEST": self.stub_manifest()}
        r = subprocess.run(["sh", "-s"], input=read(INSTALL), cwd=ROOT, env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(any(x.endswith(self.RELEASE_URL + "sheltie.py") for x in self.calls()), self.calls())

    def test_http_source_rejected(self):
        r = self.lone_install(SHELTIE_SRC="http://example.invalid/sheltie.py")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("https", r.stderr)
        self.assertFalse(any(x.startswith("launchctl") for x in self.calls()))

    def test_empty_download_aborts(self):
        self.fake("curl", 'while [ $# -gt 0 ]; do [ "$1" = -o ] && : > "$2"; shift; done; exit 0')
        r = self.lone_install()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("비어", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".local/share/sheltie/sheltie.py")))

    def test_syntax_error_download_aborts(self):
        self.fake("curl", 'while [ $# -gt 0 ]; do [ "$1" = -o ] && echo "def (" > "$2"; shift; done; exit 0')
        r = self.lone_install()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("파이썬", r.stderr)
        self.assertFalse(any(x.startswith("launchctl") for x in self.calls()))

    def test_no_personal_strings(self):
        for p in (INSTALL, os.path.join(ROOT, "deploy", "user-launchagent.plist.in")):
            t = read(p)
            for bad in ("yejun", "hfl-mini", "taild2204d", "/Volumes/", "100.77."):
                self.assertNotIn(bad, t, (p, bad))

    def test_url_src_uses_curl(self):
        # 가짜 curl이 -o 파일을 만들어 준다.
        self.fake("curl", self.FAKE_CURL_DL)
        r = self.run_install(SHELTIE_SRC="https://example.invalid/sheltie.py", STUB_SRC=self.stub)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(any("https://example.invalid/sheltie.py" in x for x in self.calls()))
        self.assertFalse(any("manifest.json" in x for x in self.calls()))  # 시험용 SHELTIE_SRC는 그대로 받는다


    # ---- herdr-core 자동 설치(고정 버전 받기 + SHA256 검증)
    def hc_tarball(self, version_out="hc 0.1.21", name="hc.tar.gz"):
        """안에 herdr-core 셸 스크립트가 든 가짜 tar.gz. (경로, sha256)를 돌려준다."""
        import hashlib
        import io
        import tarfile
        body = ('#!/bin/sh\nif [ "$1" = --version ]; then echo "%s"; exit 0; fi\n'
                'echo core-ran >> "$FAKE_LOG"\n' % version_out).encode()
        p = os.path.join(self.tmp, name)
        with tarfile.open(p, "w:gz") as t:
            ti = tarfile.TarInfo("herdr-core")
            ti.size = len(body)
            ti.mode = 0o755
            t.addfile(ti, io.BytesIO(body))
        with open(p, "rb") as f:
            return p, hashlib.sha256(f.read()).hexdigest()

    def local_bin(self, name=""):
        return os.path.join(self.home, ".local/bin", name)

    def test_hc_auto_install_from_local_tarball(self):
        os.remove(os.path.join(self.bin, "hc"))
        tar, sha = self.hc_tarball()
        r = self.run_install(SHELTIE_HC_URL=tar, SHELTIE_HC_SHA256=sha)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        core = self.local_bin("herdr-core")
        self.assertEqual(stat.S_IMODE(os.stat(core).st_mode), 0o755)
        self.assertTrue(os.path.islink(self.local_bin("hc")))
        self.assertEqual(os.readlink(self.local_bin("hc")), "herdr-core")
        self.assertNotIn("core-ran", self.calls())  # --version 확인 말고는 코어를 띄우지 않는다
        cfg = read_json(os.path.join(self.home, ".config/sheltie/config.json"))
        self.assertEqual(cfg["hc_path"], self.local_bin("hc"))
        self.assertIn(os.path.dirname(self.local_bin("hc")), read(self.plist()))
        self.assertNotIn("herdr-core 없이", r.stdout)
        self.assertNotIn("herdr-core.new", os.listdir(self.local_bin()))

    def test_hc_checksum_mismatch_installs_nothing_and_continues(self):
        os.remove(os.path.join(self.bin, "hc"))
        tar, sha = self.hc_tarball()
        r = self.run_install(SHELTIE_HC_URL=tar, SHELTIE_HC_SHA256="0" * 64)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("체크섬", r.stdout)
        self.assertIn("자동 설치를 못 했다", r.stdout)
        self.assertIn("herdr-core 없이 설치한다", r.stdout)
        self.assertFalse(os.path.exists(self.local_bin("herdr-core")))
        self.assertFalse(os.path.lexists(self.local_bin("hc")))
        cfg = read_json(os.path.join(self.home, ".config/sheltie/config.json"))
        self.assertEqual(cfg["hc_path"], "")
        self.assertTrue(os.path.exists(self.plist()))

    def test_hc_download_missing_local_file_continues(self):
        os.remove(os.path.join(self.bin, "hc"))
        r = self.run_install(SHELTIE_HC_URL=os.path.join(self.tmp, "nope.tar.gz"), SHELTIE_HC_SHA256="0" * 64)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("파일이 없다", r.stdout)
        self.assertIn("herdr-core 없이 설치한다", r.stdout)

    def test_hc_binary_that_does_not_run_is_not_installed(self):
        os.remove(os.path.join(self.bin, "hc"))
        tar, sha = self.hc_tarball(version_out="x")
        # --version이 실패하는 herdr-core
        import hashlib
        import io
        import tarfile
        body = b"#!/bin/sh\nexit 1\n"
        with tarfile.open(tar, "w:gz") as t:
            ti = tarfile.TarInfo("herdr-core")
            ti.size = len(body)
            ti.mode = 0o755
            t.addfile(ti, io.BytesIO(body))
        sha = hashlib.sha256(open(tar, "rb").read()).hexdigest()
        r = self.run_install(SHELTIE_HC_URL=tar, SHELTIE_HC_SHA256=sha)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("실행되지 않는다", r.stdout)
        self.assertFalse(os.path.exists(self.local_bin("herdr-core")))

    def test_hc_default_source_is_pinned_url_and_failure_continues(self):
        os.remove(os.path.join(self.bin, "hc"))
        self.fake("curl", 'case "$*" in *herdr*) exit 22;; esac; exit 0')
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        arch = subprocess.run(["uname", "-m"], capture_output=True, text=True).stdout.strip()
        target = {"arm64": "macos-aarch64", "aarch64": "macos-aarch64", "x86_64": "macos-x86_64"}[arch]
        url = "https://releases.herdr.dev/core/0.1.21/herdr-core-%s.tar.gz" % target
        curl = [x for x in self.calls() if x.startswith("curl") and "herdr" in x]
        self.assertTrue(any(url in x and "--proto =https" in x and "--tlsv1.2" in x for x in curl), curl)
        self.assertNotIn("install.sh", " ".join(curl))
        self.assertIn("받지 못했다", r.stdout)
        self.assertIn("herdr-core 없이 설치한다", r.stdout)
        self.assertTrue(os.path.exists(self.plist()))

    def test_hc_pinned_checksums_in_installer(self):
        t = read(INSTALL)
        for h in ("1ab38dd9270f63a8f794d6d60e8702eb0d1c85a72bda85d735e5eea43f05ffed",
                  "e377e1d342cddce0346da5b2f3c281b9701e4731c00024c7c5028265e77a217f"):
            self.assertIn(h, t)
        self.assertIn('HC_PIN_VERSION="0.1.21"', t)

    def test_hc_unsupported_cpu_skips_download(self):
        os.remove(os.path.join(self.bin, "hc"))
        self.fake("uname", 'if [ "$1" = -m ]; then echo riscv64; else echo Darwin; fi')
        self.fake("curl", 'case "$*" in *herdr*) exit 22;; esac; exit 0')
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertFalse(any(x.startswith("curl") and "herdr" in x for x in self.calls()))
        self.assertIn("herdr-core 없이 설치한다", r.stdout)

    def test_hc_http_source_rejected_install_continues(self):
        os.remove(os.path.join(self.bin, "hc"))
        r = self.run_install(SHELTIE_HC_URL="http://example.invalid/hc.tar.gz", SHELTIE_HC_SHA256="0" * 64)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("https로만", r.stdout)
        self.assertFalse(any(x.startswith("curl") and "example.invalid" in x for x in self.calls()))

    def test_no_hc_option_and_env_skip_download(self):
        os.remove(os.path.join(self.bin, "hc"))
        tar, sha = self.hc_tarball()
        for args, extra in (((), {"SHELTIE_NO_HC": "1"}), (("--no-hc",), {})):
            r = self.run_install(*args, SHELTIE_HC_URL=tar, SHELTIE_HC_SHA256=sha, **extra)
            self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
            self.assertFalse(os.path.exists(self.local_bin("herdr-core")))
            self.assertIn("herdr-core 없이 설치한다", r.stdout)

    def test_hc_dry_run_downloads_nothing(self):
        os.remove(os.path.join(self.bin, "hc"))
        self.fake("curl", 'case "$*" in *herdr*) exit 22;; esac; exit 0')
        r = self.run_install("--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertRegex(r.stdout, r"\+ herdr-core 0\.1\.21 설치\(https://releases\.herdr\.dev/core/0\.1\.21/herdr-core-macos-(aarch64|x86_64)\.tar\.gz\)")
        self.assertFalse(any(x.startswith("curl") for x in self.calls()))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".local")))

    def test_hc_dry_run_shows_override_url(self):
        os.remove(os.path.join(self.bin, "hc"))
        tar, sha = self.hc_tarball()
        r = self.run_install("--dry-run", SHELTIE_HC_URL=tar, SHELTIE_HC_SHA256=sha)
        self.assertIn("+ herdr-core 0.1.21 설치(%s)" % tar, r.stdout)
        self.assertFalse(os.path.exists(self.local_bin()))

    # ---- 원래 주소가 실패하면 미러(같은 파일, 같은 해시)
    # 가짜 curl: 원래 주소는 HC_PRIMARY(fail=받기 실패, bad=다른 내용)대로, 미러는 HC_MIRROR_TAR가 있으면 그 파일을 준다.
    FAKE_CURL_HC = ('out=; url=; while [ $# -gt 0 ]; do case "$1" in -o) out=$2; shift;; *) url=$1;; esac; shift; done\n'
                    'case "$url" in\n'
                    '  https://releases.herdr.dev/core/*) if [ "${HC_PRIMARY:-fail}" = bad ]; then echo junk > "$out"; exit 0; fi; exit 22;;\n'
                    '  https://github.com/hyperfocuslab/sheltie/releases/download/hc-0.1.21/*)\n'
                    '    if [ -n "${HC_MIRROR_TAR:-}" ]; then cat "$HC_MIRROR_TAR" > "$out"; exit 0; fi; exit 22;;\n'
                    'esac\nexit 0')

    def hc_target(self):
        arch = subprocess.run(["uname", "-m"], capture_output=True, text=True).stdout.strip()
        return {"arm64": "macos-aarch64", "aarch64": "macos-aarch64", "x86_64": "macos-x86_64"}[arch]

    def hc_urls(self):
        name = "herdr-core-%s.tar.gz" % self.hc_target()
        return ("https://releases.herdr.dev/core/0.1.21/" + name,
                "https://github.com/hyperfocuslab/sheltie/releases/download/hc-0.1.21/" + name)

    def hc_curl_urls(self):
        return [x.split()[-1] for x in self.calls() if x.startswith("curl") and x.split()[-1].endswith(".tar.gz")]

    def assert_hc_installed(self, r):
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertTrue(os.path.exists(self.local_bin("herdr-core")))
        cfg = read_json(os.path.join(self.home, ".config/sheltie/config.json"))
        self.assertEqual(cfg["hc_path"], self.local_bin("hc"))
        self.assertNotIn("herdr-core 없이", r.stdout)
        self.assertNotIn("자동 설치를 못 했다", r.stdout)

    def test_hc_primary_download_fails_mirror_installs(self):
        os.remove(os.path.join(self.bin, "hc"))
        tar, sha = self.hc_tarball()
        self.fake("curl", self.FAKE_CURL_HC)
        r = self.run_install(SHELTIE_HC_SHA256=sha, HC_MIRROR_TAR=tar)
        self.assert_hc_installed(r)
        primary, mirror = self.hc_urls()
        self.assertEqual(self.hc_curl_urls(), [primary, mirror])
        self.assertIn("미러에서 받는다(%s)" % mirror, r.stdout)
        self.assertTrue(all("--proto =https" in x and "--tlsv1.2" in x
                            for x in self.calls() if x.startswith("curl") and "herdr-core-" in x))

    def test_hc_primary_checksum_mismatch_mirror_installs(self):
        os.remove(os.path.join(self.bin, "hc"))
        tar, sha = self.hc_tarball()
        self.fake("curl", self.FAKE_CURL_HC)
        r = self.run_install(SHELTIE_HC_SHA256=sha, HC_MIRROR_TAR=tar, HC_PRIMARY="bad")
        self.assert_hc_installed(r)
        self.assertEqual(self.hc_curl_urls(), list(self.hc_urls()))
        self.assertIn("체크섬", r.stdout)

    def test_hc_mirror_with_wrong_checksum_is_rejected(self):
        os.remove(os.path.join(self.bin, "hc"))
        tar, sha = self.hc_tarball()
        self.fake("curl", self.FAKE_CURL_HC)
        r = self.run_install(SHELTIE_HC_SHA256="0" * 64, HC_MIRROR_TAR=tar)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(self.hc_curl_urls(), list(self.hc_urls()))
        self.assertIn("체크섬", r.stdout)
        self.assertIn("herdr-core 없이 설치한다", r.stdout)
        self.assertFalse(os.path.exists(self.local_bin("herdr-core")))

    def test_hc_primary_and_mirror_fail_install_continues(self):
        os.remove(os.path.join(self.bin, "hc"))
        self.fake("curl", self.FAKE_CURL_HC)
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        primary, mirror = self.hc_urls()
        self.assertEqual(self.hc_curl_urls(), [primary, mirror])
        self.assertIn("받지 못했다: %s" % mirror, r.stdout)
        self.assertIn("자동 설치를 못 했다", r.stdout)
        self.assertIn("herdr-core 없이 설치한다", r.stdout)
        self.assertFalse(os.path.exists(self.local_bin("herdr-core")))
        cfg = read_json(os.path.join(self.home, ".config/sheltie/config.json"))
        self.assertEqual(cfg["hc_path"], "")
        self.assertTrue(os.path.exists(self.plist()))

    def test_hc_override_url_has_no_mirror_fallback(self):
        os.remove(os.path.join(self.bin, "hc"))
        tar, sha = self.hc_tarball()
        self.fake("curl", self.FAKE_CURL_HC)
        r = self.run_install(SHELTIE_HC_URL="https://releases.herdr.dev/core/0.1.21/x.tar.gz",
                             SHELTIE_HC_SHA256=sha, HC_MIRROR_TAR=tar)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(self.hc_curl_urls(), ["https://releases.herdr.dev/core/0.1.21/x.tar.gz"])
        self.assertNotIn("미러", r.stdout)
        self.assertIn("herdr-core 없이 설치한다", r.stdout)
        self.assertFalse(os.path.exists(self.local_bin("herdr-core")))

    def test_hc_dry_run_shows_mirror(self):
        os.remove(os.path.join(self.bin, "hc"))
        r = self.run_install("--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("+   실패하면 미러에서 받기(%s)" % self.hc_urls()[1], r.stdout)
        tar, sha = self.hc_tarball()
        r = self.run_install("--dry-run", SHELTIE_HC_URL=tar, SHELTIE_HC_SHA256=sha)
        self.assertNotIn("미러", r.stdout)

    def test_hc_present_does_not_download(self):
        tar, sha = self.hc_tarball()
        r = self.run_install(SHELTIE_HC_URL=tar, SHELTIE_HC_SHA256=sha)  # PATH에 가짜 hc가 있다
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertFalse(os.path.exists(self.local_bin("herdr-core")))
        cfg = read_json(os.path.join(self.home, ".config/sheltie/config.json"))
        self.assertEqual(cfg["hc_path"], os.path.join(self.bin, "hc"))

    def test_existing_herdr_core_is_kept_and_used(self):
        os.remove(os.path.join(self.bin, "hc"))
        os.makedirs(self.local_bin())
        core = self.local_bin("herdr-core")
        with open(core, "w") as f:
            f.write("#!/bin/sh\necho mine\n")
        os.chmod(core, 0o755)
        tar, sha = self.hc_tarball()
        r = self.run_install(SHELTIE_HC_URL=tar, SHELTIE_HC_SHA256=sha)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(read(core), "#!/bin/sh\necho mine\n")
        self.assertFalse(os.path.lexists(self.local_bin("hc")))
        cfg = read_json(os.path.join(self.home, ".config/sheltie/config.json"))
        self.assertEqual(cfg["hc_path"], core)
        self.assertNotIn("herdr-core 없이", r.stdout)

    def test_hc_not_installed_when_tailscale_check_fails(self):
        os.remove(os.path.join(self.bin, "hc"))
        tar, sha = self.hc_tarball()
        r = self.run_install(SHELTIE_HC_URL=tar, SHELTIE_HC_SHA256=sha, TS_STATE="NeedsLogin")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(self.local_bin("herdr-core")))

    def test_uninstall_keeps_herdr_core(self):
        os.remove(os.path.join(self.bin, "hc"))
        tar, sha = self.hc_tarball()
        self.run_install(SHELTIE_HC_URL=tar, SHELTIE_HC_SHA256=sha)
        core = self.local_bin("herdr-core")
        self.assertTrue(os.path.exists(core))
        self.assertEqual(self.run_install("--uninstall").returncode, 0)
        self.assertTrue(os.path.exists(core))

    # ---- sheltie 명령
    def cmd_path(self):
        return os.path.join(self.home, ".local/bin/sheltie")

    def test_sheltie_command_created_and_passes_args(self):
        # 서버 대역이 받은 인자를 기록하게 바꾼다.
        with open(self.stub, "a") as f:
            f.write('\nif cmd == "run":\n    open(os.environ["ARGV_OUT"], "w").write(json.dumps(sys.argv[1:]))\n')
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        p = self.cmd_path()
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o755)
        lines = read(p).splitlines()
        self.assertEqual(lines[0], "#!/bin/sh")
        self.assertTrue(lines[1].startswith("# sheltie-command:"))
        out = os.path.join(self.tmp, "argv.json")
        w = subprocess.run([p, "run", "gemini", "--flag", "a b"], env={"HOME": self.home, "ARGV_OUT": out},
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(w.returncode, 0, w.stderr)
        self.assertEqual(read_json(out), ["run", "gemini", "--flag", "a b"])
        self.assertIn("PATH에 없다", r.stdout)

    def test_sheltie_command_quotes_apostrophe_in_path(self):
        home = os.path.join(self.tmp, "it's home")
        os.makedirs(home)
        self.home = home
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        w = subprocess.run([self.cmd_path(), "init"], env={"HOME": self.home}, capture_output=True, text=True)
        self.assertEqual(w.returncode, 0, w.stderr)

    def test_sheltie_command_no_path_hint_when_on_path(self):
        r = self.run_install(PATH=os.path.join(self.home, ".local/bin") + ":" + self.bin + ":/usr/bin:/bin")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertNotIn("PATH에 없다", r.stdout)

    def test_sheltie_command_foreign_file_untouched(self):
        os.makedirs(os.path.dirname(self.cmd_path()))
        with open(self.cmd_path(), "w") as f:
            f.write("#!/bin/sh\necho mine\n")
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(read(self.cmd_path()), "#!/bin/sh\necho mine\n")
        self.assertIn("건드리지 않았다", r.stdout)
        self.assertEqual(self.run_install("--uninstall").returncode, 0)
        self.assertTrue(os.path.exists(self.cmd_path()))

    def test_sheltie_command_reinstall_overwrites_ours(self):
        self.run_install()
        with open(self.cmd_path(), "w") as f:
            f.write("#!/bin/sh\n# sheltie-command: x\nexec old\n")
        self.assertEqual(self.run_install().returncode, 0)
        self.assertNotIn("exec old", read(self.cmd_path()))
        self.assertIn("sheltie.py", read(self.cmd_path()))

    def test_sheltie_command_removed_on_uninstall(self):
        self.run_install()
        self.assertTrue(os.path.exists(self.cmd_path()))
        self.assertEqual(self.run_install("--uninstall").returncode, 0)
        self.assertFalse(os.path.exists(self.cmd_path()))

    def test_sheltie_command_dry_run_writes_nothing(self):
        r = self.run_install("--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertFalse(os.path.exists(self.cmd_path()))


HOOK_EVENTS_CLAUDE = {"PermissionRequest", "PreToolUse", "SessionStart", "SessionEnd", "UserPromptSubmit", "Stop"}
HOOK_EVENTS_CODEX = {"PermissionRequest", "SessionStart", "SessionEnd", "UserPromptSubmit", "Stop"}
HOOK_EVENTS_QWEN = HOOK_EVENTS_CODEX


@unittest.skipIf(os.name == "nt", "맥 전용")
class InstallHooksTest(InstallTest):
    """Claude Code·Codex 훅 등록. 임시 HOME 안에서만 돈다. 부모 테스트는 상속만 하므로 다시 돌지 않게 비운다."""

    def setUp(self):
        super().setUp()
        self.claude = os.path.join(self.home, ".claude")
        self.codex = os.path.join(self.home, ".codex")
        self.server = os.path.join(self.home, ".local/share/sheltie/sheltie.py")

    def cmd(self, agent):
        return '"%s" "%s" hook %s' % (self.py(), self.server, agent)

    def wait_cmd(self):
        return '"%s" "%s" hook-wait claude' % (self.py(), self.server)

    def py(self):
        return os.path.join(self.bin, "python3")

    def write_json(self, p, obj):
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            json.dump(obj, f)

    def settings(self):
        return os.path.join(self.claude, "settings.json")

    def hooks_json(self):
        return os.path.join(self.codex, "hooks.json")

    def ours(self, groups):
        return [h for g in groups for h in g["hooks"] if "sheltie.py\" hook " in h["command"]]

    def test_fresh_install_creates_both(self):
        os.makedirs(self.claude)
        os.makedirs(self.codex)
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        c = read_json(self.settings())
        self.assertEqual(set(c["hooks"]), HOOK_EVENTS_CLAUDE)
        self.assertEqual(c["hooks"]["PermissionRequest"],
                         [{"matcher": "*", "hooks": [{"type": "command", "command": self.cmd("claude"), "timeout": 600}]}])
        self.assertEqual(c["hooks"]["PreToolUse"][0]["matcher"], "AskUserQuestion|ExitPlanMode")
        self.assertEqual(c["hooks"]["Stop"],
                         [{"hooks": [{"type": "command", "command": self.cmd("claude"), "timeout": 10},
                                     {"type": "command", "command": self.wait_cmd(), "asyncRewake": True,
                                      "timeout": 86400}]}])
        x = read_json(self.hooks_json())
        self.assertEqual(set(x["hooks"]), HOOK_EVENTS_CODEX)
        self.assertEqual(x["hooks"]["PermissionRequest"][0]["hooks"][0]["command"], self.cmd("codex"))
        self.assertEqual(x["hooks"]["SessionEnd"][0]["hooks"][0]["timeout"], 3)
        self.assertFalse(os.path.exists(self.settings() + ".sheltie-bak"), "원본이 없으면 백업도 없다")
        self.assertIn("새로 시작", r.stdout)
        self.assertEqual(read(self.settings())[:4], '{\n  ')  # 들여쓰기 2칸

    def test_codex_trust_after_codex_hooks(self):
        os.makedirs(self.codex)
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(self.calls().count("server codex-trust"), 1)
        self.assertIn("Codex 훅 신뢰 등록: 5개", r.stdout)
        # 훅이 이미 있어도(unchanged) 다시 신뢰를 확인한다: 예전 설치는 신뢰 없이 훅만 넣었다
        r = self.run_install()
        self.assertIn("codex 훅이 이미 등록돼 있다", r.stdout)
        self.assertEqual(self.calls().count("server codex-trust"), 2)

    def test_codex_trust_failure_warns_and_install_continues(self):
        os.makedirs(self.codex)
        r = self.run_install(CODEX_TRUST_FAIL="1")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("신뢰를 등록하지 못했다(codex 실행 파일을 찾지 못했다)", r.stderr)
        self.assertIn("/hooks", r.stderr)
        self.assertTrue(os.path.exists(self.plist()))

    def test_codex_trust_skipped_without_codex_dryrun_nohooks_uninstall(self):
        os.makedirs(self.claude)
        self.run_install()
        self.assertNotIn("server codex-trust", self.calls())
        os.makedirs(self.codex)
        r = self.run_install("--dry-run")
        self.assertIn("(dry-run) Codex에 Sheltie 훅 신뢰 등록", r.stdout)
        self.run_install("--no-hooks")
        self.run_install("--uninstall")
        self.assertNotIn("server codex-trust", self.calls())

    def test_agent_dirs_missing_are_skipped(self):
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(self.claude))
        self.assertFalse(os.path.exists(self.codex))

    def test_merge_keeps_user_settings_and_hooks_and_backs_up_once(self):
        orig = {"model": "opus", "zeta": 1, "alpha": {"x": [1, 2]},
                "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}],
                          "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "lint.sh"}]}],
                          "Notification": [{"hooks": [{"type": "command", "command": "n.sh"}]}]}}
        self.write_json(self.settings(), orig)
        before = read(self.settings())
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr)
        c = read_json(self.settings())
        self.assertEqual(list(c)[:3], ["model", "zeta", "alpha"])  # 키 순서 보존
        self.assertEqual(c["alpha"], {"x": [1, 2]})
        self.assertEqual(c["hooks"]["Notification"], orig["hooks"]["Notification"])
        stop = c["hooks"]["Stop"]
        self.assertEqual(stop[0], orig["hooks"]["Stop"][0])
        self.assertEqual(len(self.ours(stop)), 1)
        pre = c["hooks"]["PreToolUse"]
        self.assertEqual(pre[0], orig["hooks"]["PreToolUse"][0])
        self.assertEqual(len(self.ours(pre)), 1)
        self.assertEqual(read(self.settings() + ".sheltie-bak"), before)
        # 두 번째 설치는 백업을 덮지 않는다
        self.run_install()
        self.assertEqual(read(self.settings() + ".sheltie-bak"), before)

    def test_reinstall_no_duplicates_and_replaces_ours(self):
        os.makedirs(self.claude)
        os.makedirs(self.codex)
        self.run_install()
        first = read(self.settings())
        # 예전 경로로 등록된 우리 항목은 새 경로로 교체된다(중복 금지)
        c = read_json(self.settings())
        c["hooks"]["Stop"][0]["hooks"][0]["command"] = '"/old/python3" "/old/sheltie.py" hook claude'
        c["hooks"]["Stop"].insert(0, {"hooks": [{"type": "command", "command": "mine.sh"}]})
        self.write_json(self.settings(), c)
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr)
        c = read_json(self.settings())
        self.assertEqual(len(self.ours(c["hooks"]["Stop"])), 1)
        self.assertEqual(self.ours(c["hooks"]["Stop"])[0]["command"], self.cmd("claude"))
        self.assertEqual(c["hooks"]["Stop"][0]["hooks"][0]["command"], "mine.sh")
        self.assertEqual(len(c["hooks"]["Stop"]), 2)
        for ev, groups in c["hooks"].items():
            self.assertEqual(len(self.ours(groups)), 1, ev)
        # 그대로면 파일이 안 바뀐다
        self.run_install()
        self.assertEqual(len(self.ours(read_json(self.settings())["hooks"]["Stop"])), 1)
        self.assertEqual(first.count("sheltie.py"), 7)  # 6개 이벤트 + Stop의 hook-wait
        self.assertEqual(first.count("hook-wait claude"), 1)

    def waits(self, doc):
        return [h for ev, groups in doc.get("hooks", {}).items() for g in groups for h in g["hooks"]
                if "hook-wait" in h["command"]]

    def test_hook_wait_only_claude_stop_once_idempotent_and_uninstalled(self):
        os.makedirs(self.claude)
        os.makedirs(self.codex)
        os.makedirs(os.path.join(self.home, ".qwen"))
        self.run_install()
        c = read_json(self.settings())
        self.assertEqual(self.waits(c), [{"type": "command", "command": self.wait_cmd(), "asyncRewake": True,
                                          "timeout": 86400}])
        self.assertEqual([h for h in c["hooks"]["Stop"][0]["hooks"] if "hook-wait" in h["command"]],
                         self.waits(c))  # 동기 Stop 항목과 같은 그룹
        self.assertEqual(self.waits(read_json(self.hooks_json())), [])
        self.assertEqual(self.waits(read_json(os.path.join(self.home, ".qwen", "settings.json"))), [])
        # 예전 경로의 hook-wait가 따로 남아 있어도 새 것 하나로 바뀐다
        c["hooks"]["Stop"].append({"hooks": [{"type": "command", "command": '"/old/py" "/old/sheltie.py" hook-wait claude',
                                              "asyncRewake": True, "timeout": 86400}]})
        self.write_json(self.settings(), c)
        self.run_install()
        self.run_install()
        c = read_json(self.settings())
        self.assertEqual([h["command"] for h in self.waits(c)], [self.wait_cmd()])
        self.assertEqual(len(c["hooks"]["Stop"]), 1)
        r = self.run_install("--uninstall")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertNotIn("hook-wait", read(self.settings()))

    def test_broken_json_untouched_install_continues(self):
        os.makedirs(self.claude)
        os.makedirs(self.codex)
        with open(self.settings(), "w") as f:
            f.write("{ not json")
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(read(self.settings()), "{ not json")
        self.assertFalse(os.path.exists(self.settings() + ".sheltie-bak"))
        self.assertIn("올바른 JSON이 아니라", r.stderr)
        self.assertTrue(os.path.exists(self.hooks_json()), "다른 쪽과 나머지 설치는 계속")
        self.assertTrue(os.path.exists(self.plist()))

    def test_no_hooks_option(self):
        os.makedirs(self.claude)
        os.makedirs(self.codex)
        r = self.run_install("--no-hooks")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(self.settings()))
        self.assertFalse(os.path.exists(self.hooks_json()))
        self.assertNotIn("새로 시작", r.stdout)

    def test_dry_run_hooks_changes_nothing(self):
        os.makedirs(self.claude)
        os.makedirs(self.codex)
        self.write_json(self.settings(), {"a": 1})
        before = read(self.settings())
        r = self.run_install("--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("(dry-run) claude 훅 등록", r.stdout)
        self.assertIn("(dry-run) codex 훅 등록", r.stdout)
        self.assertEqual(read(self.settings()), before)
        self.assertFalse(os.path.exists(self.settings() + ".sheltie-bak"))
        self.assertFalse(os.path.exists(self.hooks_json()))

    def test_uninstall_removes_only_ours(self):
        self.write_json(self.settings(), {"model": "x", "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "mine.sh"}]}]}})
        os.makedirs(self.codex)
        self.run_install()
        self.assertTrue(os.path.exists(self.hooks_json()))
        r = self.run_install("--uninstall")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        c = read_json(self.settings())
        self.assertEqual(c, {"model": "x", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "mine.sh"}]}]}})
        # 우리 것만 있던 파일은 빈 객체가 되고, 백업(설치 전 원본)은 남는다
        self.assertEqual(read_json(self.hooks_json()), {})
        self.assertTrue(os.path.exists(self.settings() + ".sheltie-bak"))
        self.assertIn("훅 해제", r.stdout)

    def test_uninstall_no_hooks_option_keeps_them(self):
        os.makedirs(self.claude)
        self.run_install()
        self.run_install("--uninstall", "--no-hooks")
        self.assertIn("hook claude", read(self.settings()))

    def test_uninstall_broken_json_untouched(self):
        os.makedirs(self.claude)
        with open(self.settings(), "w") as f:
            f.write("[oops")
        r = self.run_install("--uninstall")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(read(self.settings()), "[oops")

    # --- Qwen Code(~/.qwen/settings.json)·OpenCode(~/.config/opencode/plugins/sheltie.js)
    def qwen_settings(self):
        return os.path.join(self.home, ".qwen", "settings.json")

    def oc_dir(self):
        return os.path.join(self.home, ".config", "opencode")

    def oc_plugin(self):
        return os.path.join(self.oc_dir(), "plugins", "sheltie.js")

    def test_qwen_and_opencode_fresh_install(self):
        os.makedirs(os.path.join(self.home, ".qwen"))
        os.makedirs(self.oc_dir())
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        q = read_json(self.qwen_settings())
        self.assertEqual(set(q["hooks"]), HOOK_EVENTS_QWEN)
        self.assertEqual(q["hooks"]["PermissionRequest"],
                         [{"matcher": "*", "hooks": [{"type": "command", "command": self.cmd("qwen"), "timeout": 600}]}])
        self.assertEqual(q["hooks"]["Stop"], [{"hooks": [{"type": "command", "command": self.cmd("qwen"), "timeout": 10}]}])
        self.assertFalse(os.path.exists(self.qwen_settings() + ".sheltie-bak"))
        js = read(self.oc_plugin())
        self.assertTrue(js.startswith("// sheltie-opencode-plugin\n"))
        self.assertIn("const PY = %s\n" % json.dumps(self.py()), js)
        self.assertIn("const SERVER = %s\n" % json.dumps(self.server), js)
        self.assertIn("qwen 훅 등록", r.stdout)
        self.assertIn("opencode 훅 등록", r.stdout)
        self.assertIn("OpenCode를 새로 시작", r.stdout)
        self.assertEqual(os.listdir(os.path.dirname(self.oc_plugin())), ["sheltie.js"])  # 임시·백업 파일 없음

    def test_qwen_opencode_dirs_missing_are_skipped(self):
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".qwen")))
        self.assertFalse(os.path.exists(self.oc_dir()))
        self.assertIn("opencode: %s 없음" % self.oc_dir(), r.stdout)

    def test_opencode_uses_xdg_config_home(self):
        xdg = os.path.join(self.tmp, "xdg")
        os.makedirs(os.path.join(xdg, "opencode"))
        r = self.run_install(XDG_CONFIG_HOME=xdg)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(read(os.path.join(xdg, "opencode", "plugins", "sheltie.js")).startswith("// sheltie-opencode-plugin"))
        self.assertFalse(os.path.exists(self.oc_dir()))

    # --- Antigravity CLI(agy): ~/.gemini/config/hooks.json, 최상위 키가 훅 이름
    def agy_hooks(self):
        return os.path.join(self.home, ".gemini", "config", "hooks.json")

    def test_agy_install_keeps_other_hooks_idempotent_and_uninstall(self):
        other = {"lint": {"PostToolUse": [{"matcher": "run_command", "hooks": [{"command": "./lint.sh"}]}]}}
        self.write_json(self.agy_hooks(), other)
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        d = read_json(self.agy_hooks())
        self.assertEqual(d["lint"], other["lint"])
        self.assertEqual(set(d["sheltie"]), {"SessionStart", "PreInvocation", "Stop"})  # PreToolUse 없음(PC agy가 멈춘다)
        self.assertNotIn("hooks", d)
        self.assertEqual(d["sheltie"]["Stop"], [{"type": "command", "command": self.cmd("agy"), "timeout": 10}])
        self.assertIn("agy 훅 등록", r.stdout)
        r = self.run_install()
        self.assertIn("agy 훅이 이미 등록돼 있다", r.stdout)
        self.assertEqual(read_json(self.agy_hooks()), d)
        r = self.run_install("--uninstall")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(read_json(self.agy_hooks()), other)

    def test_agy_dir_missing_is_skipped(self):
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".gemini")))
        self.assertIn("agy: %s 없음" % os.path.join(self.home, ".gemini"), r.stdout)

    def test_agy_fresh_file_created_when_gemini_dir_exists(self):
        os.makedirs(os.path.join(self.home, ".gemini"))
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(list(read_json(self.agy_hooks())), ["sheltie"])
        self.assertEqual(self.run_install("--uninstall").returncode, 0)
        self.assertFalse(os.path.exists(self.agy_hooks()))

    def test_agy_installed_without_gemini_dir_still_registers(self):
        # agy를 깔았지만 아직 안 돌려 ~/.gemini가 없다: agy 명령이 있으면 등록한다(설치 순서와 상관없이)
        b = os.path.join(self.home, ".local", "bin")
        os.makedirs(b)
        with open(os.path.join(b, "agy"), "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(os.path.join(b, "agy"), 0o755)
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(list(read_json(self.agy_hooks())), ["sheltie"])
        self.assertIn("agy 훅 등록", r.stdout)

    def test_qwen_merge_keeps_other_hooks_backup_once_no_duplicates(self):
        orig = {"model": {"name": "qwen3"}, "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}],
            "PreToolUse": [{"matcher": "run_shell_command", "hooks": [{"type": "command", "command": "lint.sh"}]}]}}
        self.write_json(self.qwen_settings(), orig)
        before = read(self.qwen_settings())
        for _ in range(2):
            r = self.run_install()
            self.assertEqual(r.returncode, 0, r.stderr)
        q = read_json(self.qwen_settings())
        self.assertEqual(q["model"], {"name": "qwen3"})
        self.assertEqual(q["hooks"]["PreToolUse"], orig["hooks"]["PreToolUse"])
        self.assertEqual(q["hooks"]["Stop"][0], orig["hooks"]["Stop"][0])
        for ev, groups in q["hooks"].items():
            self.assertEqual(len(self.ours(groups)), 0 if ev == "PreToolUse" else 1, ev)
        self.assertEqual(read(self.qwen_settings() + ".sheltie-bak"), before)
        self.assertIn("qwen 훅이 이미 등록돼 있다", r.stdout)

    def test_opencode_reinstall_and_update(self):
        os.makedirs(self.oc_dir())
        self.run_install()
        first = read(self.oc_plugin())
        r = self.run_install()
        self.assertIn("opencode 훅이 이미 등록돼 있다", r.stdout)
        self.assertEqual(read(self.oc_plugin()), first)
        # 예전 내용(우리 표시 있음)은 새 내용으로 바꾼다
        with open(self.oc_plugin(), "w") as f:
            f.write("// sheltie-opencode-plugin\n// old\n")
        r = self.run_install()
        self.assertIn("opencode 훅 갱신", r.stdout)
        self.assertEqual(read(self.oc_plugin()), first)

    def test_opencode_foreign_file_untouched(self):
        os.makedirs(os.path.dirname(self.oc_plugin()))
        with open(self.oc_plugin(), "w") as f:
            f.write("export const Mine = async () => ({})\n")
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(read(self.oc_plugin()), "export const Mine = async () => ({})\n")
        self.assertIn(self.oc_plugin(), r.stderr)
        r = self.run_install("--uninstall")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(read(self.oc_plugin()), "export const Mine = async () => ({})\n")

    def test_qwen_broken_json_untouched(self):
        os.makedirs(os.path.join(self.home, ".qwen"))
        with open(self.qwen_settings(), "w") as f:
            f.write("{ // comment\n}")
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(read(self.qwen_settings()), "{ // comment\n}")
        self.assertFalse(os.path.exists(self.qwen_settings() + ".sheltie-bak"))
        self.assertIn("올바른 JSON이 아니라", r.stderr)

    def test_qwen_opencode_no_hooks_and_dry_run(self):
        os.makedirs(os.path.join(self.home, ".qwen"))
        os.makedirs(self.oc_dir())
        r = self.run_install("--no-hooks")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(self.qwen_settings()))
        self.assertFalse(os.path.exists(os.path.join(self.oc_dir(), "plugins")))
        self.write_json(self.qwen_settings(), {"a": 1})
        before = read(self.qwen_settings())
        r = self.run_install("--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("(dry-run) qwen 훅 등록", r.stdout)
        self.assertIn("(dry-run) opencode 훅 등록", r.stdout)
        self.assertEqual(read(self.qwen_settings()), before)
        self.assertFalse(os.path.exists(self.qwen_settings() + ".sheltie-bak"))
        self.assertFalse(os.path.exists(os.path.join(self.oc_dir(), "plugins")))

    def test_qwen_opencode_uninstall_removes_only_ours(self):
        self.write_json(self.qwen_settings(), {"model": "x", "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "mine.sh"}]}]}})
        os.makedirs(os.path.join(self.oc_dir(), "plugins"))
        other = os.path.join(self.oc_dir(), "plugins", "other.js")
        with open(other, "w") as f:
            f.write("// other\n")
        self.run_install()
        self.assertTrue(os.path.exists(self.oc_plugin()))
        r = self.run_install("--uninstall", "--dry-run")
        self.assertIn("(dry-run) opencode 훅 해제", r.stdout)
        self.assertTrue(os.path.exists(self.oc_plugin()))
        r = self.run_install("--uninstall")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(read_json(self.qwen_settings()),
                         {"model": "x", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "mine.sh"}]}]}})
        self.assertTrue(os.path.exists(self.qwen_settings() + ".sheltie-bak"))
        self.assertFalse(os.path.exists(self.oc_plugin()))
        self.assertEqual(read(other), "// other\n")
        self.assertIn("opencode 훅 해제", r.stdout)
        r = self.run_install("--uninstall")
        self.assertIn("opencode: 우리 훅이 없다", r.stdout)

    def test_no_bare_variable_before_non_ascii(self):
        # UTF-8 로캘의 sh(bash 3.2)는 "$VAR로"의 한글 바이트를 변수 이름으로 읽는다(set -u면 죽는다). ${VAR}로 쓴다.
        # 서버가 띄우는 설치기는 파이썬이 LC_CTYPE=UTF-8을 넘겨 이 로캘로 돈다(자동 업데이트 되돌리기에서 실측).
        for p in (INSTALL, os.path.join(ROOT, "ops", "release-server.sh"), os.path.join(ROOT, "ops", "hc-watch.sh")):
            bad = [(i + 1, l) for i, l in enumerate(read(p).splitlines())
                   if not l.lstrip().startswith("#") and re.search(r"\$[A-Za-z_][A-Za-z0-9_]*[^\x00-\x7f]", l)]
            self.assertEqual(bad, [], p)

    def test_hooks_merge_script_identical_in_ps1(self):
        def block(path):
            t = read(path)
            a = t.index("# ---- hooks_merge.py begin")
            b = t.index("# ---- hooks_merge.py end")
            return t[a:b].replace("\r\n", "\n")
        self.assertEqual(block(INSTALL), block(os.path.join(ROOT, "install.ps1")))


# 버전 줄이 있는 서버 대역(옛·새). init·pair는 STUB_SERVER와 같다.
def versioned_stub(version, extra=""):
    return STUB_SERVER + 'SERVER_VERSION = "%s"\n%s' % (version, extra)


# 가짜 launchctl(상태 있음): $HEALTH_FILE.loaded가 있으면 서비스가 올라가 있다. bootstrap·kickstart는 설치된 서버 파일의
# 버전으로 health 응답 파일을 쓴다(BROKEN 버전은 응답 없음). 실제 launchd처럼 올라가 있는데 bootstrap하면 5(EIO)로 실패하고,
# BOOTOUT_LAG초면 bootout이 그만큼 늦게 끝난다(비동기). BOOTSTRAP_FAILS=N: 처음 N번 bootstrap 실패.
# BOOTSTRAP_FAIL_GREP: plist에 그 글자가 있으면 bootstrap 실패(새 plist가 망가진 경우). print는 올라가 있으면 0, 아니면 113.
FAKE_LAUNCHCTL_HEALTH = r"""
L="$HEALTH_FILE.loaded"; U="$HEALTH_FILE.unloading"; N="$HEALTH_FILE.bsfails"
if [ -f "$U" ] && [ "$(date +%s)" -ge "$(cat "$U")" ]; then rm -f "$U" "$L"; fi
health() {
  v=$(sed -n 's/^SERVER_VERSION = "\(.*\)"$/\1/p' "$HOME/.local/share/sheltie/sheltie.py")
  case " ${BROKEN:-} " in *" $v "*) rm -f "$HEALTH_FILE";;
  *) printf '{"ok":true,"server_version":"%s","started":%s}' "$v" "$(date +%s)" > "$HEALTH_FILE";; esac
}
case "$1" in
  print) [ -f "$L" ] || exit 113 ;;
  bootout) [ -f "$L" ] && [ ! -f "$U" ] || exit 3; rm -f "$HEALTH_FILE"
    if [ -n "${BOOTOUT_LAG:-}" ]; then echo $(( $(date +%s) + BOOTOUT_LAG )) > "$U"; else rm -f "$L"; fi ;;
  bootstrap) [ ! -f "$L" ] || { echo "Bootstrap failed: 5: Input/output error" >&2; exit 5; }
    n=$(cat "$N" 2>/dev/null || echo 0)
    if [ "$n" -lt "${BOOTSTRAP_FAILS:-0}" ]; then echo $((n + 1)) > "$N"; echo "Bootstrap failed: 5" >&2; exit 5; fi
    if [ -n "${BOOTSTRAP_FAIL_GREP:-}" ] && grep -q "$BOOTSTRAP_FAIL_GREP" "$3"; then echo "Bootstrap failed: 5" >&2; exit 5; fi
    touch "$L"; health ;;
  kickstart) [ -f "$L" ] || exit 113; health ;;
esac
exit ${LAUNCHCTL_RC:-0}
"""
# 가짜 curl: health 응답 파일이 있으면 내보내고, 없으면 연결 실패처럼 끝난다.
FAKE_CURL_HEALTH = '[ -f "$HEALTH_FILE" ] || exit 7; cat "$HEALTH_FILE"'


@unittest.skipIf(os.name == "nt", "맥 전용")
class InstallUpdateTest(InstallTest):
    """--update(서버 자동 업데이트가 부르는 모드)와 --no-auto-update. 부모 테스트는 비운다."""

    def setUp(self):
        super().setUp()
        self.health = os.path.join(self.tmp, "health.json")
        self.fake("launchctl", FAKE_LAUNCHCTL_HEALTH)
        self.fake("curl", FAKE_CURL_HEALTH)
        self.old_src = self.write_src("old.py", versioned_stub("1.0.0"))
        self.new_src = self.write_src("new.py", versioned_stub("1.0.1"))
        self.server = os.path.join(self.home, ".local/share/sheltie/sheltie.py")
        self.cfg_dir = os.path.join(self.home, ".config/sheltie")
        self.status_path = os.path.join(self.cfg_dir, "update_status.json")

    def write_src(self, name, text):
        p = os.path.join(self.tmp, name)
        with open(p, "w") as f:
            f.write(text)
        return p

    def run_install(self, *args, **extra):
        extra.setdefault("LANG", "en_US.UTF-8")  # 서버가 띄우는 설치기와 같은 UTF-8 로캘(sh가 "$VAR한글"을 다르게 읽는다)
        extra.setdefault("HEALTH_FILE", self.health)
        extra.setdefault("SHELTIE_UPDATE_WAIT", "3")
        return super().run_install(*args, **extra)

    def install_old(self, *args):
        r = self.run_install(*args, SHELTIE_SRC=self.old_src)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        with open(os.path.join(self.cfg_dir, "devices.json"), "w") as f:
            json.dump([{"token": "ab" * 32, "env": "production"}], f)
        open(self.log, "w").close()

    def update(self, src=None, **extra):
        return self.run_install("--update", SHELTIE_SRC=src or self.new_src, **extra)

    def cfg_files(self):
        return {n: read(os.path.join(self.cfg_dir, n)) for n in ("config.json", "devices.json")}

    def status(self):
        return read_json(self.status_path)

    def test_update_does_not_touch_shell_rc(self):
        self.install_old()
        rc = os.path.join(self.home, ".zshrc")
        self.assertIn("sheltie: claude", read(rc))  # 설치 때는 들어간다
        with open(rc, "w") as f:
            f.write("mine=1\n")
        r = self.update()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(read(rc), "mine=1\n")
        os.remove(rc)
        self.assertEqual(self.update(self.write_src("newer.py", versioned_stub("1.0.2"))).returncode, 0)
        self.assertFalse(os.path.exists(rc))

    def test_update_replaces_server_and_keeps_config_tokens_devices(self):
        self.install_old()
        before = self.cfg_files()
        plist = read(self.plist())
        r = self.update()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(read(self.server), read(self.new_src))
        self.assertEqual(read(self.server + ".prev"), read(self.old_src))
        self.assertEqual(stat.S_IMODE(os.stat(self.server).st_mode), 0o755)
        self.assertEqual(self.cfg_files(), before)
        st = self.status()
        self.assertEqual((st["result"], st["version"], st["from"]), ("ok", "1.0.1", "1.0.0"))
        self.assertEqual(stat.S_IMODE(os.stat(self.status_path).st_mode), 0o600)
        c = self.calls()
        uid = os.getuid()
        # plist가 같으면 kickstart -k만(bootout 없이)
        self.assertEqual([x for x in c if x.startswith("launchctl")],
                         ["launchctl kickstart -k gui/%s/com.hyperfocuslab.sheltie" % uid])
        self.assertEqual(read(self.plist()), plist)
        self.assertFalse(any(x.startswith("tailscale") for x in c), c)
        self.assertNotIn("PAIR-OK", r.stdout)
        self.assertNotIn("herdr-core", r.stdout)

    def test_update_rolls_back_when_new_server_does_not_answer(self):
        self.install_old()
        before = self.cfg_files()
        r = self.update(BROKEN="1.0.1")
        self.assertEqual(r.returncode, 3, r.stderr + r.stdout)
        self.assertEqual(read(self.server), read(self.old_src))
        self.assertEqual(self.cfg_files(), before)
        st = self.status()
        self.assertEqual((st["result"], st["version"], st["from"]), ("rolled_back", "1.0.1", "1.0.0"))
        self.assertIn("이전 서버로 되돌렸다", r.stdout)
        kicks = [x for x in self.calls() if x.startswith("launchctl kickstart")]
        self.assertEqual(len(kicks), 2)  # 새 서버로 한 번, 되돌린 뒤 한 번
        self.assertEqual(read_json(self.health)["server_version"], "1.0.0")

    def test_rollback_status_written_before_old_server_restarts(self):
        # 다시 뜬 옛 서버가 실패한 버전을 읽으려면 결과 파일이 재시작보다 먼저 있어야 한다
        self.install_old()
        self.fake("launchctl", 'case "$1" in kickstart) '
                  '[ -f "$HOME/.config/sheltie/update_status.json" ] && echo has-status >> "$FAKE_LOG";; esac; '
                  + FAKE_LAUNCHCTL_HEALTH)
        r = self.update(BROKEN="1.0.1")
        self.assertEqual(r.returncode, 3, r.stderr + r.stdout)
        c = self.calls()
        self.assertEqual(c.count("has-status"), 1, c)
        self.assertLess(c.index("launchctl kickstart -k gui/%s/com.hyperfocuslab.sheltie" % os.getuid()),
                        c.index("has-status"))

    def test_changed_plist_reloads_and_rollback_restores_it(self):
        self.install_old()
        old_plist = read(self.plist()).replace("    <key>HOME</key><string>%s</string>\n" % self.home, "")
        with open(self.plist(), "w") as f:
            f.write(old_plist)  # 이 기능 전 plist(HOME 없음)
        r = self.update()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("<key>HOME</key>", read(self.plist()))
        self.assertEqual(read(self.plist()), read(os.path.join(self.home, ".local/share/sheltie",
                                                                "com.hyperfocuslab.sheltie.plist")))
        launch = [x.split()[1] for x in self.calls() if x.startswith("launchctl") and " print " not in x + " "]
        self.assertEqual(launch, ["bootout", "bootstrap"])
        self.assertFalse(os.path.exists(self.plist() + ".prev"))
        # 고장 난 다음 버전: plist도 되돌린다
        with open(self.plist(), "w") as f:
            f.write(old_plist)
        newer = self.write_src("newer.py", versioned_stub("1.0.2"))
        r = self.update(newer, BROKEN="1.0.2")
        self.assertEqual(r.returncode, 3, r.stderr + r.stdout)
        self.assertEqual(read(self.plist()), old_plist)
        self.assertEqual(read(self.server), read(self.new_src))

    def test_update_refuses_same_or_lower_version(self):
        self.install_old()
        for v in ("1.0.0", "0.9.9"):
            r = self.update(self.write_src("v.py", versioned_stub(v)))
            self.assertEqual(r.returncode, 1, r.stderr + r.stdout)
            self.assertIn("버전이 올라가지 않는다", r.stderr)
            self.assertEqual(read(self.server), read(self.old_src))
            self.assertEqual(self.status()["result"], "failed")
        self.assertFalse(any(x.startswith("launchctl") for x in self.calls()))

    def test_update_bad_sources_change_nothing(self):
        self.install_old()
        before = self.cfg_files()
        for src, msg in ((self.write_src("bad.py", versioned_stub("1.0.1", "def (:")), "올바른 파이썬이 아니다"),
                         (self.write_src("nover.py", STUB_SERVER), "SERVER_VERSION이 없다"),
                         (os.path.join(self.tmp, "missing.py"), "새 서버 파일이 없다")):
            r = self.update(src)
            self.assertEqual(r.returncode, 1, msg)
            self.assertIn(msg, r.stderr)
            self.assertEqual(read(self.server), read(self.old_src))
            self.assertFalse(os.path.exists(self.server + ".prev"))
        self.assertEqual(self.cfg_files(), before)
        self.assertEqual(self.status()["result"], "failed")
        self.assertFalse(any(x.startswith("launchctl") for x in self.calls()))

    def test_update_without_install_fails_and_creates_nothing(self):
        r = self.update()
        self.assertEqual(r.returncode, 1)
        self.assertIn("설정이 없다", r.stderr)
        self.assertFalse(os.path.exists(self.cfg_dir))
        self.assertFalse(os.path.exists(self.server))

    def test_update_reregisters_hooks_unless_installed_without(self):
        self.install_old()
        claude = os.path.join(self.home, ".claude")
        os.makedirs(claude)
        r = self.update()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("sheltie.py\\\" hook claude", read(os.path.join(claude, "settings.json")))

    def test_update_respects_no_hooks_install(self):
        self.install_old("--no-hooks")
        self.assertIs(read_json(os.path.join(self.cfg_dir, "config.json"))["hooks"], False)
        claude = os.path.join(self.home, ".claude")
        os.makedirs(claude)
        r = self.update()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertFalse(os.path.exists(os.path.join(claude, "settings.json")))
        # 훅과 함께 다시 설치하면 표시를 지운다
        r = self.run_install(SHELTIE_SRC=self.new_src)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertNotIn("hooks", read_json(os.path.join(self.cfg_dir, "config.json")))

    def test_no_auto_update_option_kept_on_reinstall(self):
        self.install_old("--no-auto-update")
        cfg = os.path.join(self.cfg_dir, "config.json")
        self.assertIs(read_json(cfg)["auto_update"], False)
        r = self.run_install(SHELTIE_SRC=self.old_src)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIs(read_json(cfg)["auto_update"], False)

    def test_default_install_has_no_auto_update_key(self):
        self.install_old()
        self.assertNotIn("auto_update", read_json(os.path.join(self.cfg_dir, "config.json")))

    def test_update_dry_run_changes_nothing(self):
        self.install_old()
        before = read(self.server)
        r = self.run_install("--update", "--dry-run", SHELTIE_SRC=self.new_src)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(read(self.server), before)
        self.assertFalse(os.path.exists(self.status_path))
        self.assertFalse(any(x.startswith("launchctl") for x in self.calls()))

    def test_system_plist_update_uses_kickstart(self):
        self.install_old()
        os.makedirs(self.sys_dir)
        with open(os.path.join(self.sys_dir, "com.hyperfocuslab.sheltie.plist"), "w") as f:
            f.write("<plist/>")
        r = self.update()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual([x for x in self.calls() if x.startswith("launchctl")],
                         ["launchctl kickstart -k gui/%s/com.hyperfocuslab.sheltie" % os.getuid()])

    def test_test_label_used_for_service_and_passed_to_server(self):
        r = self.run_install(SHELTIE_SRC=self.old_src, SHELTIE_LABEL="agent-sheltie-test")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        p = os.path.join(self.home, "Library/LaunchAgents/agent-sheltie-test.plist")
        text = read(p)
        self.assertIn("<string>agent-sheltie-test</string>", text)
        self.assertIn("<key>SHELTIE_LABEL</key><string>agent-sheltie-test</string>", text)
        self.assertIn("launchctl bootstrap gui/%s %s" % (os.getuid(), p), self.calls())
        self.assertFalse(any("com.hyperfocuslab.sheltie" in x for x in self.calls()))
        self.assertEqual(self.run_install(SHELTIE_LABEL="bad label;x").returncode, 2)

    # ---- 서비스 다시 올리기(bootout은 비동기: 끝날 때까지 기다리고, bootstrap은 다시 시도하고, 안 되면 이전 plist로)
    def force_reload(self):
        """설치된 plist를 이 기능 전 모양(HOME 없음)으로 바꿔 다음 업데이트가 bootout+bootstrap하게 한다. 그 plist를 돌려준다."""
        old = read(self.plist()).replace("    <key>HOME</key><string>%s</string>\n" % self.home, "")
        with open(self.plist(), "w") as f:
            f.write(old)
        return old

    def loaded(self):
        return os.path.exists(self.health + ".loaded")

    def test_reload_waits_for_async_bootout(self):
        self.install_old()
        self.force_reload()
        r = self.update(BOOTOUT_LAG="2")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(self.status()["result"], "ok")
        self.assertTrue(self.loaded())
        launch = [x.split()[1] for x in self.calls() if x.startswith("launchctl")]
        self.assertEqual(launch[0], "bootout")
        self.assertIn("print", launch)
        self.assertEqual(launch[-1], "bootstrap")
        self.assertEqual(launch.count("bootstrap"), 1)  # 내려간 걸 확인한 뒤 한 번에 된다

    def test_reload_retries_bootstrap(self):
        self.install_old()
        self.force_reload()
        r = self.update(BOOTSTRAP_FAILS="2")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(self.status()["result"], "ok")
        self.assertTrue(self.loaded())
        self.assertEqual([x.split()[1] for x in self.calls() if x.startswith("launchctl bootstrap")],
                         ["bootstrap"] * 3)

    def test_new_plist_that_never_loads_falls_back_to_previous_plist(self):
        self.install_old()
        old_plist = self.force_reload()
        r = self.update(BOOTSTRAP_FAIL_GREP="<key>HOME</key>")
        self.assertTrue(self.loaded(), r.stderr + r.stdout)  # 서비스가 내려간 채 끝나지 않는다
        self.assertEqual(read(self.plist()), old_plist)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)  # 이전 plist로 새 서버가 떴다
        self.assertEqual(self.status()["result"], "ok")
        self.assertEqual(read_json(self.health)["server_version"], "1.0.1")

    def test_rollback_reload_waits_and_service_stays_up(self):
        self.install_old()
        self.force_reload()
        r = self.update(BROKEN="1.0.1", BOOTOUT_LAG="1")
        self.assertEqual(r.returncode, 3, r.stderr + r.stdout)
        self.assertTrue(self.loaded())
        self.assertEqual(read(self.server), read(self.old_src))
        self.assertEqual(read_json(self.health)["server_version"], "1.0.0")
        self.assertIn("이전 서버로 되돌렸다", r.stdout)

    # ---- 교체 뒤 쓰기 실패(set -e로 중간에 끝나지 않는다)
    def test_write_failure_after_replace_still_restarts_and_records(self):
        self.install_old()
        self.force_reload()
        copy = os.path.join(self.home, ".local/share/sheltie/com.hyperfocuslab.sheltie.plist")
        os.chmod(copy, 0o444)  # 서비스 정의 사본을 못 쓴다(디스크·권한 오류 흉내)
        r = self.update()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(self.status()["result"], "ok")
        self.assertTrue(self.loaded())
        self.assertEqual(read_json(self.health)["server_version"], "1.0.1")

    def test_write_failure_during_rollback_still_restarts_and_records(self):
        self.install_old()
        self.force_reload()
        copy = os.path.join(self.home, ".local/share/sheltie/com.hyperfocuslab.sheltie.plist")
        os.chmod(copy, 0o444)
        r = self.update(BROKEN="1.0.1")
        self.assertEqual(r.returncode, 3, r.stderr + r.stdout)
        st = self.status()
        self.assertEqual((st["result"], st["transient"]), ("rolled_back", True))
        self.assertTrue(self.loaded())
        self.assertEqual(read_json(self.health)["server_version"], "1.0.0")

    def test_keep_prev_failure_is_transient_and_changes_nothing(self):
        self.install_old()
        prev = self.server + ".prev"
        with open(prev, "w") as f:
            f.write("old")
        os.chmod(prev, 0o444)
        r = self.update()
        self.assertEqual(r.returncode, 1, r.stderr + r.stdout)
        self.assertEqual(read(self.server), read(self.old_src))
        st = self.status()
        self.assertEqual((st["result"], st["transient"]), ("failed", True))
        self.assertFalse(any(x.startswith("launchctl") for x in self.calls()))

    def test_status_transient_flags(self):
        self.install_old()
        r = self.update(self.write_src("same.py", versioned_stub("1.0.0")))
        self.assertEqual(r.returncode, 1)
        self.assertIs(self.status()["transient"], False)  # 릴리스가 잘못됐다: 바로 영구
        r = self.update(BROKEN="1.0.1")
        self.assertEqual(r.returncode, 3)
        self.assertIs(self.status()["transient"], True)  # 응답 시간 초과: 다시 해 본다

    def test_default_wait_is_120_seconds(self):
        self.install_old()
        env_wait = {"SHELTIE_UPDATE_WAIT": ""}
        r = self.run_install("--update", "--dry-run", SHELTIE_SRC=self.new_src, **env_wait)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("120초", r.stdout)

    # ---- 지금 서비스가 쓰는 파이썬을 그대로 쓴다
    def other_python(self):
        d = os.path.join(self.tmp, "bin2")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "python3")
        with open(p, "w") as f:
            f.write('#!/bin/sh\necho "otherpy" >> "$FAKE_LOG"\nexec "%s" "$@"\n' % sys.executable)
        os.chmod(p, 0o755)
        return d

    def test_update_uses_service_python_not_path_python(self):
        self.install_old()
        plist = read(self.plist())
        d = self.other_python()
        r = self.update(PATH=d + ":" + self.bin + ":/usr/bin:/bin")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertNotIn("otherpy", self.calls())
        self.assertEqual(read(self.plist()), plist)  # plist(파이썬 경로)가 그대로라 다시 올리지 않았다
        self.assertIn(os.path.join(self.bin, "python3"), read(os.path.join(self.home, ".local/bin/sheltie")))

    def test_update_finds_python_when_service_python_missing(self):
        self.install_old()
        with open(self.plist(), "w") as f:
            f.write(read(self.plist()).replace(os.path.join(self.bin, "python3"), os.path.join(self.tmp, "gone/python3")))
        d = self.other_python()
        r = self.update(PATH=d + ":" + self.bin + ":/usr/bin:/bin")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("otherpy", self.calls())
        self.assertIn(os.path.join(d, "python3"), read(self.plist()))

    def test_plist_sets_home(self):
        self.install_old()
        self.assertIn("<key>HOME</key><string>%s</string>" % self.home, read(self.plist()))
        self.assertNotIn("SHELTIE_LABEL", read(self.plist()))



OLD_LABEL = "com.hyperfocuslab.herdr-remote"  # 이전 설치 이름: 옮기기 시험용


@unittest.skipIf(os.name == "nt", "맥 전용")
class InstallMigrateTest(InstallTest):
    """이전 설치(옛 경로)가 있는 가짜 HOME에서 설치·해제. 부모 테스트는 상속만 하므로 비운다."""

    def setUp(self):
        super().setUp()
        h = self.home
        self.old_plist = os.path.join(h, "Library/LaunchAgents", OLD_LABEL + ".plist")
        self.old_data = os.path.join(h, ".local/share/herdr-remote")
        self.old_cfg_dir = os.path.join(h, ".config/herdr-remote")
        self.old_log = os.path.join(h, "Library/Logs/herdr-remote.log")
        self.new_cfg_dir = os.path.join(h, ".config/sheltie")
        self.old_server = os.path.join(self.old_data, "herdr_remote.py")

    def make_old_install(self, hooks=True):
        for d in (os.path.dirname(self.old_plist), self.old_data, os.path.dirname(self.old_log)):
            os.makedirs(d, exist_ok=True)
        os.makedirs(self.old_cfg_dir, mode=0o700)
        os.chmod(self.old_cfg_dir, 0o700)
        for p in (self.old_plist, os.path.join(self.old_data, OLD_LABEL + ".plist")):
            with open(p, "w") as f:
                f.write("<plist/>")
        with open(self.old_server, "w") as f:
            f.write("# old\n")
        with open(self.old_log, "w") as f:
            f.write("old log line\n")
        cfg = {"token": "tok-OLD-keep-this-token-000", "port": 8790, "hc_path": "x",
               "apns_key_path": "~/.config/herdr-remote/AuthKey.p8",
               "extra_abs": os.path.join(self.old_cfg_dir, "sub/file"),
               "unrelated": "~/.config/herdr-remote-other/x"}
        self.write_secret(os.path.join(self.old_cfg_dir, "config.json"), json.dumps(cfg))
        self.write_secret(os.path.join(self.old_cfg_dir, "devices.json"), json.dumps([{"token": "dev1"}]))
        self.write_secret(os.path.join(self.old_cfg_dir, "AuthKey.p8"), "KEY")
        if hooks:
            old_cmd = '"/usr/bin/python3" "%s" hook claude' % self.old_server
            settings = os.path.join(self.home, ".claude/settings.json")
            os.makedirs(os.path.dirname(settings))
            with open(settings, "w") as f:
                json.dump({"hooks": {
                    "Stop": [{"hooks": [{"type": "command", "command": "mine.sh"}]},
                             {"hooks": [{"type": "command", "command": old_cmd, "timeout": 10}]}],
                    "PermissionRequest": [{"matcher": "*", "hooks": [{"type": "command", "command": old_cmd}]}]}}, f)

    def write_secret(self, p, text):
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)

    def snapshot(self):
        out = {}
        for root, dirs, files in os.walk(self.home):
            for n in dirs + files:
                p = os.path.join(root, n)
                st = os.lstat(p)
                out[p] = (stat.S_IMODE(st.st_mode), read(p) if os.path.isfile(p) else None)
        return out

    def test_install_moves_old_install(self):
        self.make_old_install()
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        uid = str(os.getuid())
        c = self.calls()
        i_old = c.index("launchctl bootout gui/%s/%s" % (uid, OLD_LABEL))
        i_new = c.index("launchctl bootstrap gui/%s %s" % (uid, self.plist()))
        self.assertLess(i_old, i_new)
        # 설정·토큰·기기 목록·키가 새 폴더로 옮겨지고 권한이 유지된다
        cfg = read_json(os.path.join(self.new_cfg_dir, "config.json"))
        self.assertEqual(cfg["token"], "tok-OLD-keep-this-token-000")
        self.assertEqual(cfg["apns_key_path"], "~/.config/sheltie/AuthKey.p8")
        self.assertEqual(cfg["extra_abs"], os.path.join(self.new_cfg_dir, "sub/file"))
        self.assertEqual(cfg["unrelated"], "~/.config/herdr-remote-other/x")
        self.assertEqual(cfg["hc_path"], os.path.join(self.bin, "hc"))
        self.assertEqual(read_json(os.path.join(self.new_cfg_dir, "devices.json")), [{"token": "dev1"}])
        self.assertEqual(read(os.path.join(self.new_cfg_dir, "AuthKey.p8")), "KEY")
        self.assertEqual(stat.S_IMODE(os.stat(self.new_cfg_dir).st_mode), 0o700)
        for n in ("config.json", "devices.json", "AuthKey.p8"):
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.new_cfg_dir, n)).st_mode), 0o600, n)
        # 이전 서비스·설치 폴더·설정 폴더·로그가 없어진다
        for p in (self.old_plist, self.old_data, self.old_cfg_dir, self.old_log):
            self.assertFalse(os.path.exists(p), p)
        self.assertIn("old log line", read(os.path.join(self.home, "Library/Logs/sheltie.log")))
        self.assertTrue(os.path.exists(self.plist()))
        # 이전 훅 항목은 새 항목으로 바뀌고 사용자 훅은 남는다
        settings = os.path.join(self.home, ".claude/settings.json")
        text = read(settings)
        self.assertNotIn("herdr_remote.py", text)
        s = read_json(settings)
        new_cmd = '"%s" "%s" hook claude' % (os.path.join(self.bin, "python3"),
                                              os.path.join(self.home, ".local/share/sheltie/sheltie.py"))
        stop = [h["command"] for g in s["hooks"]["Stop"] for h in g["hooks"]]
        self.assertEqual(stop, ["mine.sh", new_cmd, new_cmd.replace(" hook claude", " hook-wait claude")])
        self.assertEqual([h["command"] for g in s["hooks"]["PermissionRequest"] for h in g["hooks"]], [new_cmd])
        self.assertIn("훅 갱신", r.stdout)
        # 다시 설치해도 그대로
        open(self.log, "w").close()
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("launchctl bootout gui/%s/%s" % (uid, OLD_LABEL), self.calls())
        self.assertEqual(read_json(os.path.join(self.new_cfg_dir, "config.json"))["token"], "tok-OLD-keep-this-token-000")

    def test_existing_new_config_is_not_overwritten(self):
        self.make_old_install(hooks=False)
        os.makedirs(self.new_cfg_dir)
        self.write_secret(os.path.join(self.new_cfg_dir, "config.json"),
                          json.dumps({"token": "tok-NEW-0000000000000000", "port": 8790}))
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("옮기지 않고", r.stdout)
        self.assertEqual(read_json(os.path.join(self.new_cfg_dir, "config.json"))["token"], "tok-NEW-0000000000000000")
        self.assertEqual(read_json(os.path.join(self.old_cfg_dir, "config.json"))["token"], "tok-OLD-keep-this-token-000")
        self.assertTrue(os.path.exists(os.path.join(self.old_cfg_dir, "AuthKey.p8")))
        self.assertFalse(os.path.exists(self.old_plist))
        self.assertFalse(os.path.exists(self.old_data))

    def test_move_failure_keeps_everything(self):
        self.make_old_install()
        before_settings = read(os.path.join(self.home, ".claude/settings.json"))
        parent = os.path.dirname(self.old_cfg_dir)
        os.chmod(parent, 0o500)  # ~/.config에 쓸 수 없으면 rename이 실패한다
        try:
            r = self.run_install()
        finally:
            os.chmod(parent, 0o755)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("아무것도 지우지 않았다", r.stderr)
        self.assertEqual(read_json(os.path.join(self.old_cfg_dir, "config.json"))["token"], "tok-OLD-keep-this-token-000")
        self.assertTrue(os.path.exists(os.path.join(self.old_cfg_dir, "devices.json")))
        self.assertFalse(os.path.exists(self.new_cfg_dir))
        for p in (self.old_plist, self.old_server, self.old_log):
            self.assertTrue(os.path.exists(p), p)
        self.assertEqual(read(os.path.join(self.home, ".claude/settings.json")), before_settings)
        uid = str(os.getuid())
        c = self.calls()
        self.assertIn("launchctl bootstrap gui/%s %s" % (uid, self.old_plist), c)  # 이전 서비스를 되살린다
        self.assertFalse(os.path.exists(self.plist()))
        self.assertFalse(any(x.startswith("tailscale serve --bg") for x in c))

    def test_old_system_plist_prints_sudo_and_continues(self):
        self.make_old_install(hooks=False)
        os.remove(self.old_plist)
        os.makedirs(self.sys_dir)
        sys_old = os.path.join(self.sys_dir, OLD_LABEL + ".plist")
        with open(sys_old, "w") as f:
            f.write("<plist/>")
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("sudo rm '%s'" % sys_old, r.stdout)
        self.assertTrue(os.path.exists(sys_old), "sudo가 필요한 파일은 지우지 않는다")
        self.assertIn("launchctl bootout gui/%s/%s" % (os.getuid(), OLD_LABEL), self.calls())
        self.assertTrue(os.path.exists(self.plist()), "새 서비스는 사용자 위치에 설치된다")

    def test_uninstall_also_removes_old_service(self):
        self.make_old_install()
        r = self.run_install("--uninstall")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        uid = str(os.getuid())
        c = self.calls()
        self.assertIn("launchctl bootout gui/%s/%s" % (uid, OLD_LABEL), c)
        self.assertIn("tailscale serve --https=8790 off", c)
        self.assertFalse(os.path.exists(self.old_plist))
        self.assertFalse(os.path.exists(os.path.join(self.old_data, OLD_LABEL + ".plist")))
        self.assertTrue(os.path.exists(os.path.join(self.old_cfg_dir, "config.json")), "설정과 토큰은 남긴다")
        self.assertIn(self.old_cfg_dir, r.stdout)
        s = read_json(os.path.join(self.home, ".claude/settings.json"))
        self.assertEqual(s, {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "mine.sh"}]}]}})

    def assert_old_untouched(self):
        for p in (self.old_plist, self.old_server, self.old_log, os.path.join(self.old_cfg_dir, "devices.json")):
            self.assertTrue(os.path.exists(p), p)
        self.assertEqual(read_json(os.path.join(self.old_cfg_dir, "config.json"))["token"], "tok-OLD-keep-this-token-000")
        self.assertFalse(os.path.exists(self.new_cfg_dir))

    def test_download_failure_leaves_old_install(self):
        # 새 서버 파일을 못 받으면 이전 설치는 하나도 건드리지 않는다
        self.make_old_install()
        r = self.run_install(SHELTIE_SRC=os.path.join(self.tmp, "missing.py"))
        self.assertNotEqual(r.returncode, 0)
        self.assert_old_untouched()
        self.assertFalse(any(x.startswith("launchctl") for x in self.calls()), self.calls())
        self.assertIn("herdr_remote.py", read(os.path.join(self.home, ".claude/settings.json")))

    def test_bootstrap_failure_keeps_old_files(self):
        # 새 서비스가 안 뜨면 이전 plist·설치 폴더·로그는 지우지 않는다(설정은 새 위치로 옮겨져 있다)
        self.make_old_install()
        r = self.run_install(LAUNCHCTL_RC="1")
        self.assertEqual(r.returncode, 5, r.stderr + r.stdout)
        for p in (self.old_plist, self.old_server, self.old_log):
            self.assertTrue(os.path.exists(p), p)
        self.assertEqual(read_json(os.path.join(self.new_cfg_dir, "config.json"))["token"], "tok-OLD-keep-this-token-000")

    def test_no_hooks_keeps_old_server_file(self):
        # 훅이 아직 이전 서버 파일을 부르면 그 파일(이전 설치 폴더)은 지우지 않고 경고한다
        self.make_old_install()
        r = self.run_install("--no-hooks")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertTrue(os.path.exists(self.old_server))
        self.assertFalse(os.path.exists(self.old_plist))
        self.assertFalse(os.path.exists(os.path.join(self.old_data, OLD_LABEL + ".plist")))
        self.assertIn(os.path.join(self.home, ".claude/settings.json"), r.stderr)
        self.assertIn(self.old_data, r.stderr)

    def test_qwen_hooks_file_with_old_server_keeps_old_folder(self):
        self.make_old_install(hooks=False)
        q = os.path.join(self.home, ".qwen", "settings.json")
        os.makedirs(os.path.dirname(q))
        with open(q, "w") as f:
            json.dump({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": '"/x/python3" "%s" hook qwen'
                                                      % self.old_server}]}]}}, f)
        r = self.run_install("--no-hooks")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertTrue(os.path.exists(self.old_server))
        self.assertIn(q, r.stderr)

    def test_broken_hooks_file_keeps_old_server_file(self):
        self.make_old_install(hooks=False)
        codex = os.path.join(self.home, ".codex")
        os.makedirs(codex)
        with open(os.path.join(codex, "hooks.json"), "w") as f:
            f.write('{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "\\"%s\\" hook codex"}]}], '
                    '"Custom": "x"}}' % self.old_server)
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertTrue(os.path.exists(self.old_server))
        self.assertIn(os.path.join(codex, "hooks.json"), r.stderr)

    def test_symlinked_old_config_stops_install(self):
        # 이전 설정 폴더가 링크면 옮기지 않고(새 토큰을 만들지 않고) 멈춘다
        self.make_old_install()
        real = os.path.join(self.tmp, "dotfiles-cfg")
        os.rename(self.old_cfg_dir, real)
        os.symlink(real, self.old_cfg_dir)
        r = self.run_install()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("링크", r.stderr)
        self.assertTrue(os.path.islink(self.old_cfg_dir))
        self.assertFalse(os.path.lexists(self.new_cfg_dir))
        self.assertTrue(os.path.exists(self.old_plist))
        self.assertTrue(os.path.exists(self.old_server))
        self.assertFalse(any(x.startswith("launchctl") for x in self.calls()), self.calls())

    def test_move_failure_restores_system_old_service(self):
        self.make_old_install(hooks=False)
        os.remove(self.old_plist)
        os.makedirs(self.sys_dir)
        sys_old = os.path.join(self.sys_dir, OLD_LABEL + ".plist")
        with open(sys_old, "w") as f:
            f.write("<plist/>")
        parent = os.path.dirname(self.old_cfg_dir)
        os.chmod(parent, 0o500)
        try:
            r = self.run_install()
        finally:
            os.chmod(parent, 0o755)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("launchctl bootstrap gui/%s %s" % (os.getuid(), sys_old), self.calls())
        self.assertTrue(os.path.exists(self.old_server))

    def test_dry_run_with_old_install_changes_nothing(self):
        self.make_old_install()
        before = self.snapshot()
        r = self.run_install("--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("+ launchctl bootout gui/%s/%s" % (os.getuid(), OLD_LABEL), r.stdout)
        self.assertIn("+ mv %s %s" % (self.old_cfg_dir, self.new_cfg_dir), r.stdout)
        self.assertIn("+ rm -rf %s" % self.old_data, r.stdout)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(any(x.startswith(("launchctl", "curl")) for x in self.calls()))
        r = self.run_install("--uninstall", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(self.snapshot(), before)

WRAP_BEGIN = "# >>> sheltie: claude >>>"
WRAP_END = "# <<< sheltie: claude <<<"


class InstallWrapClaudeTest(InstallTest):
    """claude 셸 함수(~/.zshrc 등): 넣기·교체·거절·해제와, 넣은 함수가 zsh·bash에서 어디로 보내는지(진짜 tty로)."""

    def rc(self, name=".zshrc"):
        return os.path.join(self.home, name)

    def block_in(self, text):
        return text.count(WRAP_BEGIN), text.count(WRAP_END)

    def test_install_creates_zshrc_with_block(self):
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        text = read(self.rc())
        self.assertEqual(self.block_in(text), (1, 1))
        self.assertIn("sheltie run claude", text)
        self.assertEqual(stat.S_IMODE(os.stat(self.rc()).st_mode), 0o644)
        self.assertIn("새 터미널부터", r.stdout)
        self.assertIn("SHELTIE_NO_WRAP=1 claude", r.stdout)
        self.assertFalse(os.path.exists(self.rc(".bashrc")))  # 없던 bash 파일은 만들지 않는다
        self.assertFalse(os.path.exists(self.rc(".bash_profile")))
        self.assertNotIn("wrap_claude", read_json(os.path.join(self.home, ".config/sheltie/config.json")))

    def test_install_keeps_existing_content_and_mode(self):
        orig = "export A=1\nalias x='echo \"hi\"'\n# 한글 주석\nlast line without newline"
        with open(self.rc(), "w", encoding="utf-8") as f:
            f.write(orig)
        os.chmod(self.rc(), 0o600)
        with open(self.rc(".bashrc"), "w") as f:
            f.write("b=1\n")
        self.assertEqual(self.run_install().returncode, 0)
        for name, head in ((".zshrc", orig), (".bashrc", "b=1\n")):
            text = read(self.rc(name))
            self.assertTrue(text.startswith(head), name)
            self.assertEqual(self.block_in(text), (1, 1), name)
        self.assertEqual(stat.S_IMODE(os.stat(self.rc()).st_mode), 0o600)
        leftovers = [n for n in os.listdir(self.home) if n.startswith(".zshrc.") or n.startswith(".bashrc.")]
        self.assertEqual(leftovers, [])
        self.assertEqual(self.run_install("--uninstall").returncode, 0)
        self.assertEqual(read(self.rc(".bashrc")), "b=1\n")
        self.assertEqual(read(self.rc()).rstrip("\n"), orig.rstrip("\n"))

    def test_reinstall_replaces_without_duplicates(self):
        self.run_install()
        text = read(self.rc())
        # 오래된 내용으로 바꿔 둔 블록(앞뒤에 사용자 줄)
        old = "before=1\n" + WRAP_BEGIN + "\nclaude() { echo OLD; }\n" + WRAP_END + "\nafter=1\n"
        with open(self.rc(), "w") as f:
            f.write(old)
        self.assertEqual(self.run_install().returncode, 0)
        text = read(self.rc())
        self.assertEqual(self.block_in(text), (1, 1))
        self.assertNotIn("echo OLD", text)
        self.assertTrue(text.startswith("before=1\n"))
        self.assertTrue(text.rstrip("\n").endswith(WRAP_END + "\nafter=1") or "after=1" in text)
        self.assertEqual(text.count("before=1"), 1)
        self.assertEqual(text.count("after=1"), 1)
        again = text
        self.run_install()
        self.assertEqual(read(self.rc()), again)  # 두 번째는 바뀌는 게 없다

    def test_block_without_end_marker_left_alone(self):
        body = "x=1\n" + WRAP_BEGIN + "\nuser stuff\n"
        with open(self.rc(), "w") as f:
            f.write(body)
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(read(self.rc()), body)
        self.assertIn("끝 표시가 없어", r.stderr)

    def test_symlinked_rc_not_touched(self):
        real = os.path.join(self.tmp, "dotfiles_zshrc")
        with open(real, "w") as f:
            f.write("dot=1\n")
        os.symlink(real, self.rc())
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.islink(self.rc()))
        self.assertEqual(read(real), "dot=1\n")
        self.assertIn("심볼릭 링크", r.stdout)

    def test_no_wrap_option_records_false_and_sticks(self):
        r = self.run_install("--no-wrap-claude")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertFalse(os.path.exists(self.rc()))
        cfg = read_json(os.path.join(self.home, ".config/sheltie/config.json"))
        self.assertIs(cfg["wrap_claude"], False)
        self.assertNotIn("새 터미널부터", r.stdout)
        # 다음 설치는 옵션 없이도 넣지 않는다
        self.assertEqual(self.run_install().returncode, 0)
        self.assertFalse(os.path.exists(self.rc()))

    def test_declined_install_removes_existing_block(self):
        self.run_install()
        self.assertEqual(self.block_in(read(self.rc())), (1, 1))
        self.run_install("--no-wrap-claude")
        self.assertEqual(self.block_in(read(self.rc())), (0, 0))

    def test_uninstall_removes_only_block(self):
        with open(self.rc(), "w") as f:
            f.write("keep=1\n")
        self.run_install()
        r = self.run_install("--uninstall")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(read(self.rc()), "keep=1\n")

    def test_dry_run_changes_no_rc_and_does_not_ask(self):
        with open(self.rc(), "w") as f:
            f.write("keep=1\n")
        r = self.run_install("--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(read(self.rc()), "keep=1\n")
        self.assertIn(self.rc(), r.stdout)
        r = self.run_install("--uninstall", "--dry-run")
        self.assertEqual(read(self.rc()), "keep=1\n")

    def install_on_tty(self, answer):
        """진짜 tty(script) 위에서 설치기를 돌리고, 물음이 뜬 뒤에 answer를 친다. stdin은 curl | sh처럼 파이프."""
        env = dict(os.environ, HOME=self.home, PATH=self.bin + ":/usr/bin:/bin", FAKE_LOG=self.log,
                   TS_SERVE_FILE=self.ts_serve_file, SHELTIE_SRC=self.stub, SHELTIE_HEALTH_TRIES="2",
                   TS_SERVE_JSON=self.ts_serve_json, SHELTIE_SYS_AGENTS_DIR=self.sys_dir)
        p = subprocess.Popen(["script", "-q", "/dev/null", "sh", INSTALL], env=env, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        seen = b""
        try:
            while b"[Y/n]" not in seen:
                ch = os.read(p.stdout.fileno(), 4096)
                if not ch:
                    break
                seen += ch
            p.stdin.write(answer.encode())
            p.stdin.flush()
            rest, _ = p.communicate(timeout=60)
        finally:
            p.kill()
            p.wait()
        return p.returncode, (seen + rest).decode("utf-8", "replace")

    def test_prompt_no_via_tty_records_false(self):
        rc, out = self.install_on_tty("n\n")
        self.assertEqual(rc, 0, out)
        self.assertIn("claude를 Sheltie로 감쌀까요?", out)
        self.assertFalse(os.path.exists(self.rc()))
        cfg = read_json(os.path.join(self.home, ".config/sheltie/config.json"))
        self.assertIs(cfg["wrap_claude"], False)

    def test_prompt_enter_via_tty_installs(self):
        rc, out = self.install_on_tty("\n")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.block_in(read(self.rc())), (1, 1))

    # ---- 함수 동작
    def shell_run(self, shell, cmd, with_sheltie=True, with_hc=True, hc_in_home=False, env_extra=None, tty=True):
        """블록을 넣은 ~/.zshrc를 source하고 cmd를 실행. 가짜 claude·sheltie·hc는 받은 인자를 로그에 남긴다."""
        fb = os.path.join(self.tmp, "fakebin")
        shutil.rmtree(fb, ignore_errors=True)
        os.makedirs(fb)

        def mk(name, d=fb):
            q = os.path.join(d, name)
            with open(q, "w") as f:
                f.write(FAKE_SH % ":")
            os.chmod(q, 0o755)

        stale = os.path.join(self.home, ".local/bin/hc")
        if os.path.exists(stale):
            os.remove(stale)
        mk("claude")
        if with_sheltie:
            mk("sheltie")
        if with_hc:
            if hc_in_home:
                os.makedirs(os.path.join(self.home, ".local/bin"), exist_ok=True)
                mk("hc", os.path.join(self.home, ".local/bin"))
            else:
                mk("hc")
        open(self.log, "w").close()
        self.assertEqual(self.run_install().returncode, 0)
        open(self.log, "w").close()
        env = {"HOME": self.home, "PATH": fb + ":/usr/bin:/bin", "FAKE_LOG": self.log}
        env.update(env_extra or {})
        argv = [shell] + (["-f"] if shell == "zsh" else ["--norc", "--noprofile"]) + ["-c", '. "$HOME/.zshrc"; ' + cmd]
        if tty:
            argv = ["script", "-q", "/dev/null"] + argv
            subprocess.run(argv, env=env, input="", capture_output=True, text=True, timeout=30)
        else:
            subprocess.run(argv, env=env, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
        return self.calls()

    def each_shell(self):
        for sh in ("zsh", "bash"):
            if shutil.which(sh, path="/bin:/usr/bin:/usr/local/bin:/opt/homebrew/bin"):
                yield sh

    def test_function_wraps_plain_claude(self):
        for sh in self.each_shell():
            self.assertEqual(self.shell_run(sh, "claude"), ["sheltie run claude"], sh)
            self.assertEqual(self.shell_run(sh, 'claude "fix it" --model opus'),
                             ["sheltie run claude fix it --model opus"], sh)
            self.assertEqual(self.shell_run(sh, "claude -c"), ["sheltie run claude -c"], sh)

    def test_function_passes_through_noninteractive(self):
        for sh in self.each_shell():
            for cmd in ("claude -p hi", "claude --print hi", "claude hi -p", "claude -v", "claude --version",
                        "claude --help", "claude -h", "claude --bg x", "claude mcp list", "claude config", "claude update",
                        "claude doctor", "claude install", "claude setup-token", "claude auth login", "claude plugin list"):
                got = self.shell_run(sh, cmd)
                self.assertEqual(got, ["claude " + cmd[len("claude "):]], (sh, cmd))

    def test_function_passes_through_when_unwrappable(self):
        for sh in self.each_shell():
            self.assertEqual(self.shell_run(sh, "claude x", env_extra={"HC_TERMINAL_ID": "t1"}), ["claude x"], sh)
            self.assertEqual(self.shell_run(sh, "claude x", env_extra={"SHELTIE_NO_WRAP": "1"}), ["claude x"], sh)
            self.assertEqual(self.shell_run(sh, "claude x", with_sheltie=False), ["claude x"], sh)
            self.assertEqual(self.shell_run(sh, "claude x", with_hc=False), ["claude x"], sh)
            self.assertEqual(self.shell_run(sh, "claude x", tty=False), ["claude x"], sh)  # stdin·stdout이 터미널이 아님
            self.assertEqual(self.shell_run(sh, "claude x | cat", ), ["claude x"], sh)  # stdout만 파이프
            self.assertEqual(self.shell_run(sh, "claude x", with_hc=True, hc_in_home=True), ["sheltie run claude x"], sh)

    def test_command_claude_bypasses_function(self):
        for sh in self.each_shell():
            self.assertEqual(self.shell_run(sh, "command claude y"), ["claude y"], sh)

for _n in [n for n in dir(InstallTest) if n.startswith("test_")]:
    setattr(InstallHooksTest, _n, None)  # 상속된 기존 테스트는 InstallTest에서 이미 돈다
    setattr(InstallMigrateTest, _n, None)
    setattr(InstallUpdateTest, _n, None)
    setattr(InstallWrapClaudeTest, _n, None)


if __name__ == "__main__":
    unittest.main()


class ShellVarBeforeHangulTest(unittest.TestCase):
    """UTF-8 로캘의 sh는 `$NEWV로`처럼 변수 바로 뒤 한글을 변수 이름으로 읽는다(자동 업데이트 재현 2026-11-25). `${NEWV}로`로 쓴다."""

    def test_no_bare_var_followed_by_hangul(self):
        bad = []
        for rel in ("install.sh", "ops/release-server.sh", "ops/hc-watch.sh"):
            with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
                for n, line in enumerate(f, 1):
                    if line.lstrip().startswith("#"):
                        continue
                    if re.search(r"\$[A-Za-z_][A-Za-z0-9_]*[가-힣]", line):
                        bad.append("%s:%d" % (rel, n))
        self.assertEqual(bad, [])
