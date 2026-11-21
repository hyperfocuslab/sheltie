import json
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
p = os.path.expanduser("~/.config/herdr-remote/config.json")
if cmd == "init":
    if not os.path.exists(p):
        os.makedirs(os.path.dirname(p), exist_ok=True)
        json.dump({"token": "tok-0123456789abcdefghijkl", "port": 8790, "hc_path": "x"}, open(p, "w"))
elif cmd == "pair":
    if os.environ.get("PAIR_FAIL"):
        sys.exit(2)
    print("PAIR-OK")
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
        self.fake("launchctl", "exit ${LAUNCHCTL_RC:-0}")
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
               "TS_SERVE_FILE": self.ts_serve_file, "HERDR_REMOTE_SRC": self.stub,
               "HERDR_REMOTE_HEALTH_TRIES": "2", "TS_SERVE_JSON": self.ts_serve_json,
               "HERDR_REMOTE_SYS_AGENTS_DIR": self.sys_dir}
        env.update(extra)
        return subprocess.run(["sh", INSTALL] + list(args), env=env, capture_output=True, text=True, timeout=60)

    def calls(self):
        with open(self.log) as f:
            return [l.strip() for l in f if l.strip()]

    def plist(self):
        return os.path.join(self.home, "Library/LaunchAgents/com.hyperfocuslab.herdr-remote.plist")

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
        server = os.path.join(self.home, ".local/share/herdr-remote/herdr_remote.py")
        self.assertEqual(stat.S_IMODE(os.stat(server).st_mode), 0o755)
        text = read(self.plist())
        self.assertIn("<string>%s</string><string>serve</string>" % server, text)
        self.assertIn(os.path.join(self.home, "Library/Logs/herdr-remote.log"), text)
        self.assertIn(self.bin, text)  # hc 위치가 PATH에 들어간다
        self.assertNotRegex(text, r"@[A-Z]+@")
        cfg = read_json(os.path.join(self.home, ".config/herdr-remote/config.json"))
        self.assertEqual(cfg["hc_path"], os.path.join(self.bin, "hc"))

    def test_health_sends_token_via_stdin_not_argv(self):
        self.run_install()
        curl = [x for x in self.calls() if x.startswith("curl")]
        self.assertTrue(curl)
        self.assertNotIn("tok-0123456789", " ".join(curl))

    def test_custom_port(self):
        r = self.run_install(HERDR_REMOTE_PORT="18790")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("tailscale serve --bg --https=18790 http://127.0.0.1:18790", self.calls())
        cfg = read_json(os.path.join(self.home, ".config/herdr-remote/config.json"))
        self.assertEqual(cfg["port"], 18790)

    def test_bad_port_rejected(self):
        for bad in ("abc", "0", "70000", "80;rm"):
            r = self.run_install(HERDR_REMOTE_PORT=bad)
            self.assertEqual(r.returncode, 2, bad)

    def test_no_hc(self):
        os.remove(os.path.join(self.bin, "hc"))
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("herdr-core 없이 설치한다", r.stdout)
        self.assertTrue(os.path.exists(self.plist()))
        cfg = read_json(os.path.join(self.home, ".config/herdr-remote/config.json"))
        self.assertEqual(cfg["hc_path"], "")

    def test_no_tailscale(self):
        os.remove(os.path.join(self.bin, "tailscale"))
        r = self.run_install(HERDR_REMOTE_TAILSCALE="/nonexistent/tailscale")
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
        cfg_path = os.path.join(self.home, ".config/herdr-remote/config.json")
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
        with open(os.path.join(self.sys_dir, "com.hyperfocuslab.herdr-remote.plist"), "w") as f:
            f.write("<plist/>")
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        c = self.calls()
        uid = str(os.getuid())
        launch = [x for x in c if x.startswith("launchctl")]
        self.assertEqual(launch, ["launchctl kickstart -k gui/%s/com.hyperfocuslab.herdr-remote" % uid])
        self.assertFalse(os.path.exists(self.plist()))
        i_kick = c.index(launch[0])
        i_serve = next(i for i, x in enumerate(c) if x.startswith("tailscale serve --bg"))
        i_health = next(i for i, x in enumerate(c) if x.startswith("curl"))
        self.assertTrue(i_kick < i_serve < i_health, c)
        self.assertIn("PAIR-OK", r.stdout)

    def test_system_plist_kickstart_failure(self):
        os.makedirs(self.sys_dir)
        with open(os.path.join(self.sys_dir, "com.hyperfocuslab.herdr-remote.plist"), "w") as f:
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
        self.assertTrue(os.path.exists(os.path.join(self.home, ".config/herdr-remote/config.json")))

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
               "TS_SERVE_FILE": self.ts_serve_file, "HERDR_REMOTE_SRC": self.stub, "HERDR_REMOTE_HEALTH_TRIES": "2",
               "TS_SERVE_JSON": self.ts_serve_json, "HERDR_REMOTE_SYS_AGENTS_DIR": self.sys_dir}
        r = subprocess.run(["sh", os.path.join(lone, "install.sh")], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(read(self.plist()), with_tpl)

    def lone_install(self, **extra):
        lone = os.path.join(self.tmp, "lone2")
        os.makedirs(lone, exist_ok=True)
        shutil.copy(INSTALL, lone)
        env = {"HOME": self.home, "PATH": self.bin + ":/usr/bin:/bin", "FAKE_LOG": self.log,
               "TS_SERVE_FILE": self.ts_serve_file, "HERDR_REMOTE_HEALTH_TRIES": "2", "STUB_SRC": self.stub,
               "TS_SERVE_JSON": self.ts_serve_json, "HERDR_REMOTE_SYS_AGENTS_DIR": self.sys_dir}
        env.update(extra)
        return subprocess.run(["sh", os.path.join(lone, "install.sh")], env=env, capture_output=True, text=True)

    FAKE_CURL_DL = ('while [ $# -gt 0 ]; do [ "$1" = -o ] && out=$2; shift; done; '
                    '[ -z "${out:-}" ] || [ "$out" = /dev/null ] || cat "$STUB_SRC" > "$out"; exit 0')

    def test_default_source_is_github_raw_when_piped(self):
        self.fake("curl", self.FAKE_CURL_DL)
        r = self.lone_install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(any("https://raw.githubusercontent.com/" in x and x.endswith("server/herdr_remote.py")
                            for x in self.calls()))

    def test_piped_from_folder_with_server_file_still_uses_github(self):
        # curl | sh 는 $0이 "sh"라서, 서버 파일이 있는 폴더(이 저장소 루트)에서 돌려도 그 파일을 쓰면 안 된다.
        self.fake("curl", self.FAKE_CURL_DL)
        env = {"HOME": self.home, "PATH": self.bin + ":/usr/bin:/bin", "FAKE_LOG": self.log,
               "TS_SERVE_FILE": self.ts_serve_file, "HERDR_REMOTE_HEALTH_TRIES": "2", "STUB_SRC": self.stub,
               "TS_SERVE_JSON": self.ts_serve_json, "HERDR_REMOTE_SYS_AGENTS_DIR": self.sys_dir}
        r = subprocess.run(["sh", "-s"], input=read(INSTALL), cwd=ROOT, env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(any("https://raw.githubusercontent.com/" in x and x.endswith("server/herdr_remote.py")
                            for x in self.calls()))

    def test_http_source_rejected(self):
        r = self.lone_install(HERDR_REMOTE_SRC="http://example.invalid/herdr_remote.py")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("https", r.stderr)
        self.assertFalse(any(x.startswith("launchctl") for x in self.calls()))

    def test_empty_download_aborts(self):
        self.fake("curl", 'while [ $# -gt 0 ]; do [ "$1" = -o ] && : > "$2"; shift; done; exit 0')
        r = self.lone_install()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("비어", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".local/share/herdr-remote/herdr_remote.py")))

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
        r = self.run_install(HERDR_REMOTE_SRC="https://example.invalid/herdr_remote.py", STUB_SRC=self.stub)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(any("https://example.invalid/herdr_remote.py" in x for x in self.calls()))


HOOK_EVENTS_CLAUDE = {"PermissionRequest", "PreToolUse", "SessionStart", "SessionEnd", "UserPromptSubmit", "Stop"}
HOOK_EVENTS_CODEX = {"PermissionRequest", "SessionStart", "SessionEnd", "UserPromptSubmit", "Stop"}


@unittest.skipIf(os.name == "nt", "맥 전용")
class InstallHooksTest(InstallTest):
    """Claude Code·Codex 훅 등록. 임시 HOME 안에서만 돈다. 부모 테스트는 상속만 하므로 다시 돌지 않게 비운다."""

    def setUp(self):
        super().setUp()
        self.claude = os.path.join(self.home, ".claude")
        self.codex = os.path.join(self.home, ".codex")
        self.server = os.path.join(self.home, ".local/share/herdr-remote/herdr_remote.py")

    def cmd(self, agent):
        return '"%s" "%s" hook %s' % (self.py(), self.server, agent)

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
        return [h for g in groups for h in g["hooks"] if "herdr_remote.py\" hook " in h["command"]]

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
                         [{"hooks": [{"type": "command", "command": self.cmd("claude"), "timeout": 10}]}])
        x = read_json(self.hooks_json())
        self.assertEqual(set(x["hooks"]), HOOK_EVENTS_CODEX)
        self.assertEqual(x["hooks"]["PermissionRequest"][0]["hooks"][0]["command"], self.cmd("codex"))
        self.assertEqual(x["hooks"]["SessionEnd"][0]["hooks"][0]["timeout"], 3)
        self.assertFalse(os.path.exists(self.settings() + ".sheltie-bak"), "원본이 없으면 백업도 없다")
        self.assertIn("새로 시작", r.stdout)
        self.assertEqual(read(self.settings())[:4], '{\n  ')  # 들여쓰기 2칸

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
        c["hooks"]["Stop"][0]["hooks"][0]["command"] = '"/old/python3" "/old/herdr_remote.py" hook claude'
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
        self.assertEqual(first.count("herdr_remote.py"), 6)

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

    def test_hooks_merge_script_identical_in_ps1(self):
        def block(path):
            t = read(path)
            a = t.index("# ---- hooks_merge.py begin")
            b = t.index("# ---- hooks_merge.py end")
            return t[a:b].replace("\r\n", "\n")
        self.assertEqual(block(INSTALL), block(os.path.join(ROOT, "install.ps1")))


for _n in [n for n in dir(InstallTest) if n.startswith("test_")]:
    setattr(InstallHooksTest, _n, None)  # 상속된 기존 테스트는 InstallTest에서 이미 돈다


if __name__ == "__main__":
    unittest.main()
