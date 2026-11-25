"""sheltie.py codex-trust: codex app-server(JSON-RPC)에 hooks/list로 묻고 우리 Codex 훅만 config/batchWrite로 신뢰한다.
진짜 codex 대신 같은 프로토콜을 말하는 가짜(SHELTIE_CODEX_BIN)를 쓴다."""
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_sheltie import hr  # noqa: E402

# 가짜 codex: `codex app-server` 만 흉내 낸다. 상태(훅 목록)는 FAKE_CODEX_STATE, 받은 요청은 FAKE_CODEX_LOG 에.
FAKE_CODEX = r'''
import json, os, sys, time
mode = os.environ.get("FAKE_CODEX_MODE", "ok")
state_p = os.environ["FAKE_CODEX_STATE"]
log_p = os.environ["FAKE_CODEX_LOG"]
assert sys.argv[1:] == ["app-server"], sys.argv
def out(o):
    sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
def log(o):
    with open(log_p, "a") as f: f.write(json.dumps(o) + "\n")
if mode == "garbage":
    sys.stdout.write("not json\n{broken\n"); sys.stdout.flush(); sys.exit(3)
for line in sys.stdin:
    req = json.loads(line)
    log(req)
    if "id" not in req:
        continue
    if mode == "hang":
        time.sleep(60)
    m, rid = req.get("method"), req["id"]
    if m is None:
        continue  # 우리가 보낸 응답(서버 요청에 대한)
    if m == "initialize":
        out({"method": "remoteControl/status/changed", "params": {"status": "disabled"}})
        if mode == "server_request":
            out({"jsonrpc": "2.0", "id": 900, "method": "item/tool/requestUserInput", "params": {}})
        out({"id": rid, "result": {"userAgent": "fake"}})
    elif m == "hooks/list":
        st = json.load(open(state_p))
        if mode == "badlist":
            out({"id": rid, "result": {"nope": 1}}); continue
        out({"id": rid, "result": {"data": [{"cwd": req["params"]["cwds"][0], "hooks": st, "warnings": [], "errors": []}]}})
    elif m == "config/batchWrite":
        if mode == "write_error":
            out({"id": rid, "error": {"code": -32600, "message": "config locked"}}); continue
        st = json.load(open(state_p))
        if mode != "write_ignored":
            vals = req["params"]["edits"][0]["value"]
            for h in st:
                if h["key"] in vals and vals[h["key"]]["trusted_hash"] == h["currentHash"]:
                    h["trustStatus"] = "trusted"
            json.dump(st, open(state_p, "w"))
        out({"id": rid, "result": {"status": "ok", "version": "x", "filePath": "/x/config.toml"}})
    else:
        out({"id": rid, "error": {"code": -32601, "message": "unknown " + m}})
'''

PY_SHELTIE = '"/usr/bin/python3" "/Users/me/.local/share/sheltie/sheltie.py" hook codex'
PS_SHELTIE = ("powershell.exe -NoProfile -Command \"& 'C:\\Python\\python.exe' "
              "'C:\\Users\\me\\AppData\\Local\\sheltie\\sheltie.py' hook codex\"")


def hook(key, command, status, h=None):
    return {"key": key, "eventName": "stop", "handlerType": "command", "command": command,
            "currentHash": h or ("sha256:" + key), "trustStatus": status, "isManaged": False}


class FakeCodexTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state = os.path.join(self.tmp, "state.json")
        self.log = os.path.join(self.tmp, "log.jsonl")
        script = os.path.join(self.tmp, "fake_codex.py")
        with open(script, "w", encoding="utf-8") as f:  # 한국어 주석이 든다: 로케일(cp949)로 쓰지 않는다
            f.write(FAKE_CODEX)
        if os.name == "nt":
            self.bin = os.path.join(self.tmp, "codex.cmd")
            with open(self.bin, "w") as f:
                f.write('@"%s" "%s" %%*\r\n' % (sys.executable, script))
        else:
            self.bin = os.path.join(self.tmp, "codex")
            with open(self.bin, "w", encoding="utf-8") as f:
                f.write("#!%s\n%s" % (sys.executable, FAKE_CODEX))
            os.chmod(self.bin, 0o755)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def set_hooks(self, hooks):
        with open(self.state, "w") as f:
            json.dump(hooks, f)

    def hooks(self):
        with open(self.state) as f:
            return {h["key"]: h["trustStatus"] for h in json.load(f)}

    def requests(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return [json.loads(l) for l in f if l.strip()]

    def run_trust(self, mode="ok", timeout=10, **extra):
        env = dict(os.environ, SHELTIE_CODEX_BIN=self.bin, FAKE_CODEX_MODE=mode, FAKE_CODEX_STATE=self.state,
                   FAKE_CODEX_LOG=self.log)
        env.update(extra)
        out, err = io.StringIO(), io.StringIO()
        t0 = time.monotonic()
        rc = hr.cmd_codex_trust([], env=env, out=out, err=err, timeout=timeout)
        return rc, out.getvalue(), err.getvalue(), time.monotonic() - t0

    def test_trusts_only_sheltie_hooks_with_tui_params(self):
        self.set_hooks([
            hook("h.json:stop:0:0", "[ -x '/Applications/RobloxStudio.app/x' ] && roblox hook", "untrusted"),
            hook("h.json:stop:1:0", PY_SHELTIE, "untrusted"),
            hook("h.json:session_start:0:0", "\"/opt/my py/python3\" \"/Users/me/x y/sheltie.py\" hook codex", "modified"),
            hook("h.json:permission_request:0:0", PS_SHELTIE, "untrusted"),
            hook("h.json:session_end:0:0", PY_SHELTIE, "trusted"),
            hook("h.json:user_prompt_submit:0:0", PY_SHELTIE.replace("hook codex", "hook claude"), "untrusted"),
            hook("h.json:user_prompt_submit:1:0", "my-sheltie.py.bak hook codex", "untrusted"),
        ])
        rc, out, err, _ = self.run_trust()
        self.assertEqual(rc, 0, err)
        self.assertIn("3개", out)
        st = self.hooks()
        self.assertEqual(st["h.json:stop:0:0"], "untrusted")  # Roblox 훅은 그대로
        self.assertEqual(st["h.json:user_prompt_submit:0:0"], "untrusted")  # hook claude
        self.assertEqual(st["h.json:user_prompt_submit:1:0"], "untrusted")  # 비슷한 이름
        for k in ("h.json:stop:1:0", "h.json:session_start:0:0", "h.json:permission_request:0:0",
                  "h.json:session_end:0:0"):
            self.assertEqual(st[k], "trusted", k)
        reqs = self.requests()
        self.assertEqual([r.get("method") for r in reqs],
                         ["initialize", "initialized", "hooks/list", "config/batchWrite", "hooks/list"])
        self.assertEqual(reqs[0]["params"]["clientInfo"]["name"], "sheltie")
        self.assertNotIn("id", reqs[1])  # initialized는 알림
        self.assertEqual(reqs[2]["params"], {"cwds": [os.path.expanduser("~")]})
        self.assertEqual(reqs[3]["params"], {
            "edits": [{"keyPath": "hooks.state", "mergeStrategy": "upsert", "value": {
                "h.json:stop:1:0": {"trusted_hash": "sha256:h.json:stop:1:0"},
                "h.json:session_start:0:0": {"trusted_hash": "sha256:h.json:session_start:0:0"},
                "h.json:permission_request:0:0": {"trusted_hash": "sha256:h.json:permission_request:0:0"}}}],
            "filePath": None, "expectedVersion": None, "reloadUserConfig": True})

    def test_already_trusted_skips_write(self):
        self.set_hooks([hook("a", PY_SHELTIE, "trusted"), hook("b", PY_SHELTIE, "managed"),
                        hook("c", "other", "untrusted")])
        rc, out, err, _ = self.run_trust()
        self.assertEqual(rc, 0, err)
        self.assertIn("이미 신뢰", out)
        self.assertNotIn("config/batchWrite", [r.get("method") for r in self.requests()])

    def test_no_sheltie_hooks_fails(self):
        self.set_hooks([hook("c", "other", "untrusted")])
        rc, out, err, _ = self.run_trust()
        self.assertEqual(rc, 1)
        self.assertIn("찾지 못했다", err)
        self.assertEqual(self.hooks(), {"c": "untrusted"})

    def test_notifications_and_server_requests_are_skipped(self):
        self.set_hooks([hook("a", PY_SHELTIE, "untrusted")])
        rc, out, err, _ = self.run_trust(mode="server_request")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.hooks(), {"a": "trusted"})
        replies = [r for r in self.requests() if r.get("id") == 900]
        self.assertEqual(len(replies), 1)
        self.assertIn("error", replies[0])

    def test_write_error_fails(self):
        self.set_hooks([hook("a", PY_SHELTIE, "untrusted")])
        rc, out, err, _ = self.run_trust(mode="write_error")
        self.assertEqual(rc, 1)
        self.assertIn("config locked", err)

    def test_write_not_applied_fails(self):
        self.set_hooks([hook("a", PY_SHELTIE, "untrusted")])
        rc, out, err, _ = self.run_trust(mode="write_ignored")
        self.assertEqual(rc, 1)
        self.assertIn("아직 신뢰하지 않는다", err)

    def test_malformed_output_fails_fast(self):
        self.set_hooks([])
        rc, out, err, took = self.run_trust(mode="garbage")
        self.assertEqual(rc, 1)
        self.assertIn("답 없이 끝났다", err)
        self.assertLess(took, 8)

    def test_bad_list_shape_fails(self):
        self.set_hooks([hook("a", PY_SHELTIE, "untrusted")])
        rc, out, err, _ = self.run_trust(mode="badlist")
        self.assertEqual(rc, 1)
        self.assertIn("형식", err)

    def test_hang_times_out_and_kills_child(self):
        self.set_hooks([hook("a", PY_SHELTIE, "untrusted")])
        rc, out, err, took = self.run_trust(mode="hang", timeout=1.5)
        self.assertEqual(rc, 1)
        self.assertIn("답하지 않았다", err)
        self.assertLess(took, 10)
        self.assertEqual(err.count("\n"), 1)  # 한 줄

    def test_missing_binary(self):
        rc, out, err, took = self.run_trust(SHELTIE_CODEX_BIN=os.path.join(self.tmp, "nope"))
        self.assertEqual(rc, 1)
        self.assertIn("찾지 못했다", err)


class FindCodexBinTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.exe = "codex.exe" if os.name == "nt" else "codex"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def touch(self, *parts):
        p = os.path.join(self.tmp, *parts)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").close()
        os.chmod(p, 0o755)
        return p

    def test_newest_daemon_release_wins_over_path(self):
        home = os.path.join(self.tmp, "ch")
        rel = ("packages", "app-server-daemon", "releases")
        self.touch("ch", *rel, "0.99.0-aarch64-apple-darwin", "bin", self.exe)
        new = self.touch("ch", *rel, "0.172.0-aarch64-apple-darwin", "bin", self.exe)
        self.touch("ch", *rel, "0.171.0-aarch64-apple-darwin", "bin", self.exe)
        self.touch("pathdir", self.exe)
        env = {"CODEX_HOME": home, "PATH": os.path.join(self.tmp, "pathdir"), "HOME": self.tmp, "USERPROFILE": self.tmp}
        self.assertEqual(hr.find_codex_bin(env), new)

    def test_path_then_override(self):
        p = self.touch("pathdir", self.exe)
        env = {"CODEX_HOME": os.path.join(self.tmp, "none"), "PATH": os.path.join(self.tmp, "pathdir"),
               "HOME": self.tmp, "USERPROFILE": self.tmp}
        self.assertEqual(hr.find_codex_bin(env), p)
        env["SHELTIE_CODEX_BIN"] = p
        self.assertEqual(hr.find_codex_bin(env), p)
        env["SHELTIE_CODEX_BIN"] = os.path.join(self.tmp, "missing")
        self.assertIsNone(hr.find_codex_bin(env))

    def test_hook_matcher(self):
        self.assertTrue(hr.is_sheltie_codex_hook(PY_SHELTIE))
        self.assertTrue(hr.is_sheltie_codex_hook(PS_SHELTIE))
        self.assertFalse(hr.is_sheltie_codex_hook(PY_SHELTIE.replace("codex", "claude")))
        self.assertFalse(hr.is_sheltie_codex_hook('"x/sheltie.py" hook codexx'))
        self.assertFalse(hr.is_sheltie_codex_hook(None))


if __name__ == "__main__":
    unittest.main()
