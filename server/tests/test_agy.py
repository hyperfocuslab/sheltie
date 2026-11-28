"""Antigravity CLI(agy 1.5.8) 훅 세션: camelCase 본문 변환, 목록·working·done, 승인 붙잡기와 agy 결정 JSON,
설치기의 ~/.gemini/config/hooks.json 병합. fixture: tests/fixtures/hooks/agy-*-1.5.8.json(실측, 경로는 가짜)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_hook_backend as hb  # noqa: E402
from test_hook_backend import load, wait_for  # noqa: E402

hr = hb.hr
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CID = "1c09bec5-4e8a-4f27-ae4d-2033b6312b3d"
TID = "h2033b6312b3d"


class AgyHookTests(hb.HookBase):
    def post(self, name, **over):
        return self.hook("agy", load("agy-%s-1.5.8.json" % name, **over))

    def test_body_conversion(self):
        b = hr.agy_hook_body(load("agy-pretooluse-run-command-1.5.8.json"))
        self.assertEqual((b["hook_event_name"], b["session_id"], b["cwd"], b["tool_name"]),
                         ("PreToolUse", CID, "/Users/me/proj", "run_command"))
        self.assertEqual(b["tool_input"]["command"], "ls")
        self.assertEqual(b["tool_input"]["CommandLine"], "ls")
        w = hr.agy_hook_body(load("agy-pretooluse-write-file-1.5.8.json"))["tool_input"]
        self.assertEqual((w["file_path"], w["content"]), (w["Path"], w["Content"]))
        # PreInvocation은 invocationNum 0만 턴 시작, 나머지·Post* 이벤트는 쓰지 않는다
        pre = load("agy-pre-invocation-1.5.8.json")
        self.assertEqual(hr.agy_hook_body(pre)["hook_event_name"], "UserPromptSubmit")
        self.assertEqual(hr.agy_hook_body(pre)["prompt"], pre["lastUserInput"])
        self.assertIsNone(hr.agy_hook_body(dict(pre, invocationNum=1)))
        self.assertIsNone(hr.agy_hook_body(load("agy-post-invocation-1.5.8.json")))
        self.assertIsNone(hr.agy_hook_body(load("agy-posttooluse-run-command-1.5.8.json")))
        self.assertEqual(hr.agy_hook_body({"hookEventName": "Stop"}), {})

    def test_session_listed_working_then_done(self):
        self.assertEqual(self.post("session-start"), (200, None))
        code, body = self.req("GET", "/api/terminals")
        h = {t["id"]: t for t in body["terminals"]}[TID]
        self.assertEqual((h["caps"], h["agent_kind"], h["name"], h["cwd"], h["status"]),
                         ([], "agy", "proj", "/Users/me/proj", {"state": "idle", "app": "agy"}))
        self.assertEqual(self.post("pre-invocation"), (200, None))
        self.assertEqual(self.status(TID), {"state": "working", "app": "agy"})
        # 같은 턴의 두 번째 모델 호출은 상태를 바꾸지 않는다
        self.assertEqual(self.post("pre-invocation", invocationNum=1), (200, None))
        self.assertEqual(self.app.hooks.sessions[TID]["turn"], 0)
        self.assertEqual(self.post("stop"), (200, None))
        self.assertEqual(self.status(TID), {"state": "done", "app": "agy"})
        self.assertEqual(self.app.hooks.sessions[TID]["turn"], 1)
        # 끝남 알림: 사용자 말(PreInvocation 0)을 본 턴의 Stop이 한 번
        done = [p for p in self.pusher.payloads if p.get("state") == "done"]
        self.assertEqual(len(done), 1, self.pusher.payloads)
        self.assertEqual(done[0]["terminal"], TID)

    def test_post_events_and_unknown_do_not_create_session(self):
        for name in ("post-invocation", "posttooluse-run-command"):
            self.assertEqual(self.post(name), (200, None))
        self.assertEqual(self.app.hooks.sessions, {})
        self.assertEqual(self.hook("agy", {"hookEventName": "Stop"})[0], 400)

    def test_unheld_tool_returns_no_output(self):
        # `{}`는 agy가 거부로 읽는다: 읽기 도구는 본문 없이 200
        self.assertEqual(self.post("pretooluse-read-file"), (200, None))
        self.assertEqual(self.app.hooks.queued(TID), 0)

    def test_run_command_hold_allow_and_deny(self):
        body = load("agy-pretooluse-run-command-1.5.8.json", toolCall={
            "id": "call_1", "name": "run_command", "args": {"CommandLine": "touch made_by_agy.txt",
                                                            "toolSummary": "Create a file"}})
        box = self.hook_bg("agy", body)
        p = self.blocked(TID)
        self.assertEqual((p["kind"], p["title"], p["reject_index"], p["always_index"]),
                         ("approval", "Shell command", 1, None))
        self.assertIn("touch made_by_agy.txt", p["body"])
        self.assertEqual(self.status(TID), {"state": "blocked", "kind": "permission", "msg": "approve run_command",
                                            "app": "agy"})
        self.assertEqual(self.pusher.payloads[-1]["aps"]["category"], "HR_PERMISSION")
        self.assertEqual(self.answer(TID, {"action": "approve", "prompt_id": p["id"]})[0], 200)
        self.assertEqual(self.result(box), (200, {"decision": "allow"}))
        self.assertEqual(self.status(TID)["state"], "working")
        box = self.hook_bg("agy", body)
        p = self.blocked(TID)
        self.assertEqual(self.answer(TID, {"action": "always", "prompt_id": p["id"]})[1]["error"], "no_always")
        self.assertEqual(self.prompt_answer(TID, {"prompt_id": p["id"], "option_index": 1})[0], 200)
        self.assertEqual(self.result(box), (200, {"decision": "deny", "reason": hr.HOOK_REJECT_MSG}))

    def test_write_file_card_body_and_stop_releases(self):
        body = load("agy-pretooluse-write-file-1.5.8.json")
        box = self.hook_bg("agy", body)
        p = self.blocked(TID)
        self.assertEqual(p["title"], "Write file")
        self.assertIn("README.txt", p["body"])
        # PC에서 먼저 답해 턴이 끝났다: 붙잡기는 풀리고 done
        self.post("stop")
        wait_for(lambda: not box["th"].is_alive())
        self.assertEqual(self.status(TID)["state"], "done")

    def test_no_device_returns_ask(self):
        with mock.patch.object(self.app, "_hook_push_reachable", return_value=False):
            code, res = self.post("pretooluse-run-command")
        self.assertEqual((code, res), (200, {"decision": "ask"}))

    def test_wake_unsupported(self):
        self.post("session-start")
        code, res = self.req("POST", "/hook/agy/wait", load("agy-stop-1.5.8.json"))
        self.assertEqual((code, res), (200, {"end": "unsupported"}))


class AgyCommandTests(unittest.TestCase):
    """`sheltie.py hook agy`: 서버가 없으면 출력 없이 0(agy는 빈 출력을 허용으로 읽는다)."""

    def test_hook_without_server_prints_nothing(self):
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home, True)
        r = subprocess.run([sys.executable, os.path.join(ROOT, "server", "sheltie.py"), "hook", "agy"],
                           input=json.dumps(load("agy-pretooluse-run-command-1.5.8.json")).encode(),
                           capture_output=True, env=dict(os.environ, HOME=home, USERPROFILE=home), timeout=30)
        self.assertEqual((r.returncode, r.stdout), (0, b""))


class AgyInstallTests(unittest.TestCase):
    """hooks_merge(install.sh에 박힌 것)을 직접 돌려 ~/.gemini/config/hooks.json 병합을 본다."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, ".gemini", "config", "hooks.json")
        text = open(os.path.join(ROOT, "install.sh"), encoding="utf-8").read()
        a = text.index("# ---- hooks_merge.py begin")
        b = text.index("# ---- hooks_merge.py end")
        self.script = os.path.join(self.tmp, "hooks_merge.py")
        with open(self.script, "w", encoding="utf-8") as f:
            f.write(text[a:b])

    def run_merge(self, action, flavor="posix", dry=False):
        args = [sys.executable, self.script, action, "agy", self.path, "/usr/bin/python3", "/h/sheltie.py", flavor]
        if dry:
            args.append("dry")
        r = subprocess.run(args, capture_output=True, text=True, timeout=30)
        return r.stdout.strip()

    def write(self, doc):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(doc, f)

    def read(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def test_fresh_install_shape(self):
        self.assertEqual(self.run_merge("install"), "added")
        d = self.read()
        self.assertEqual(list(d), ["sheltie"])  # 최상위 키가 훅 이름: "hooks" 래핑 없음
        e = d["sheltie"]
        cmd = '"/usr/bin/python3" "/h/sheltie.py" hook agy'
        h = {"type": "command", "command": cmd}
        self.assertEqual(e["SessionStart"], [dict(h, timeout=10)])
        self.assertEqual(e["PreInvocation"], [dict(h, timeout=10)])
        self.assertEqual(e["Stop"], [dict(h, timeout=10)])
        self.assertNotIn("PreToolUse", e)  # 모든 실행·쓰기를 붙잡으면 PC agy가 폰 답을 기다리며 멈춘다
        self.assertFalse(os.path.exists(self.path + ".sheltie-bak"))

    def test_keeps_other_hooks_backup_once_and_is_idempotent(self):
        other = {"lint": {"PostToolUse": [{"matcher": "run_command", "hooks": [{"command": "./lint.sh"}]}]}}
        self.write(other)
        raw = open(self.path, encoding="utf-8").read()
        self.assertEqual(self.run_merge("install"), "added")
        d = self.read()
        self.assertEqual(d["lint"], other["lint"])
        self.assertEqual(open(self.path + ".sheltie-bak", encoding="utf-8").read(), raw)
        self.assertEqual(self.run_merge("install"), "unchanged")
        self.assertEqual(self.read(), d)

    def test_update_when_command_changes(self):
        self.assertEqual(self.run_merge("install"), "added")
        self.assertEqual(self.run_merge("install", flavor="windows"), "updated")
        c = self.read()["sheltie"]["Stop"][0]["command"]
        self.assertTrue(c.startswith("powershell.exe -NoProfile -Command"), c)
        self.assertIn("hook agy", c)

    def test_uninstall_keeps_others_and_removes_empty_file(self):
        other = {"lint": {"Stop": [{"command": "./x.sh"}]}}
        self.write(other)
        self.run_merge("install")
        self.assertEqual(self.run_merge("uninstall"), "removed")
        self.assertEqual(self.read(), other)
        self.assertEqual(self.run_merge("uninstall"), "absent")
        os.remove(self.path)
        os.remove(self.path + ".sheltie-bak")
        self.run_merge("install")
        self.assertEqual(self.run_merge("uninstall"), "removed")
        self.assertFalse(os.path.exists(self.path))

    def test_foreign_sheltie_named_hook_and_broken_json_untouched(self):
        mine = {"sheltie": {"Stop": [{"command": "./mine.sh"}]}}
        self.write(mine)
        self.assertEqual(self.run_merge("install"), "error")
        self.assertEqual(self.run_merge("uninstall"), "absent")
        self.assertEqual(self.read(), mine)
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{ // comment\n}")
        self.assertEqual(self.run_merge("install"), "broken")
        self.assertEqual(open(self.path, encoding="utf-8").read(), "{ // comment\n}")

    def test_dry_run_writes_nothing(self):
        self.assertEqual(self.run_merge("install", dry=True), "added")
        self.assertFalse(os.path.exists(os.path.dirname(self.path)))


if __name__ == "__main__":
    unittest.main()
