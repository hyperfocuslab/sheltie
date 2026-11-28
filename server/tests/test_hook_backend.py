"""훅 백엔드(herdr 없이): /hook/<agent> 붙잡기, 폰 답 → 훅 결정, guard, hook 하위 명령.

fixture: tests/fixtures/hooks/ (실측 *-2.1.329·*-0.171.0, 문서 기준 *-doc. 경로는 가짜로 바꿨다)."""
import io
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_sheltie as base  # noqa: E402

hr = base.hr
TOKEN = base.TOKEN
HOOK_FIX = os.path.join(base.FIX, "hooks")
SERVER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sheltie.py")


def load(name, **over):
    with open(os.path.join(HOOK_FIX, name), encoding="utf-8") as f:
        d = json.load(f)
    d.update(over)
    return d


def wait_for(fn, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.02)
    raise AssertionError("timed out")


class HookBase(unittest.TestCase):
    hc_factory = None

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.realpath(tempfile.mkdtemp())
        self.app, self.hc, self.pusher = base.make_app(self.tmp, home=self.home)
        self.app.devices.add("ab" * 32, "sandbox")  # 알림 받을 기기가 있어야 붙잡는다
        self.hc.add("t1", base.st("working"))
        self.srv = hr.make_server(self.app, "127.0.0.1", 0)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        for h in list(self.app.hooks.sessions.values()):
            for hold in h["holds"]:
                self.app.hooks.finish(hold, "gone")
        self.srv.shutdown()
        self.srv.server_close()

    # --- HTTP
    def req(self, method, path, body=None, token=TOKEN, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), data=data, method=method)
        if token is not None:
            r.add_header("Authorization", "Bearer " + token)
        if data is not None:
            r.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            r.add_header(k, v)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            with e:
                raw = e.read()
                return e.code, (json.loads(raw) if raw else None)

    def hook(self, agent, body, **kw):
        return self.req("POST", "/hook/" + agent, body, **kw)

    def hook_bg(self, agent, body):
        """붙잡히는 훅 요청을 스레드로. box["r"]에 (code, body)."""
        box = {}

        def run():
            box["r"] = self.hook(agent, body)
        th = threading.Thread(target=run, daemon=True)
        th.start()
        box["th"] = th
        return box

    def result(self, box, timeout=5.0):
        box["th"].join(timeout)
        self.assertFalse(box["th"].is_alive(), "hook still held")
        return box["r"]

    def tid_of(self, body):
        return hr.hook_session_tid(body["session_id"])

    def blocked(self, tid):
        return wait_for(lambda: self.app.hooks.current_prompt(tid))

    def status(self, tid):
        return self.app.hooks.get(tid)["status"]

    def answer(self, tid, body):
        return self.req("POST", "/api/terminals/%s/answer" % tid, body)

    def prompt_answer(self, tid, body):
        return self.req("POST", "/api/terminals/%s/prompt/answer" % tid, body)


class PermissionTests(HookBase):
    def test_claude_allow_and_push_category(self):
        body = load("claude-permission-bash-2.1.329.json")
        tid = self.tid_of(body)
        self.assertEqual(tid, "h770cf18f2997")
        box = self.hook_bg("claude", body)
        p = self.blocked(tid)
        self.assertEqual(p["kind"], "approval")
        self.assertIn("touch made_by_hook.txt", p["body"])
        self.assertIsNone(p["always_index"])  # addDirectories·setMode만: 계속 허용 없음
        self.assertEqual([o["label"] for o in p["options"]], ["Yes", "No"])
        self.assertEqual(self.status(tid), {"state": "blocked", "kind": "permission", "msg": "approve Bash",
                                            "app": "claude-code"})
        push = self.pusher.payloads[-1]
        self.assertEqual((push["aps"]["category"], push["terminal"], push["prompt_id"], push["state"]),
                         ("HR_PERMISSION", tid, p["id"], "blocked"))
        self.assertEqual(push["aps"]["interruption-level"], "time-sensitive")
        self.assertEqual(self.req("GET", "/api/terminals/%s/prompt" % tid), (200, {"prompt": p}))
        self.assertEqual(self.answer(tid, {"action": "approve", "prompt_id": p["id"]})[0], 200)
        self.assertEqual(self.result(box), (200, {"hookSpecificOutput": {
            "hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}}))
        wait_for(lambda: self.status(tid)["state"] == "working")
        # 이미 풀렸다: 같은 답을 다시 보내면 409
        self.assertEqual(self.answer(tid, {"action": "approve", "prompt_id": p["id"]})[0], 409)
        self.assertEqual(self.answer(tid, {"action": "approve"})[1]["error"], "not_blocked")

    def test_claude_deny_without_prompt_id(self):
        body = load("claude-permission-bash-2.1.329.json")
        tid = self.tid_of(body)
        box = self.hook_bg("claude", body)
        self.blocked(tid)
        code, err = self.answer(tid, {"action": "approve", "expect_msg": "approve Write"})
        self.assertEqual((code, err["error"]), (409, "changed"))
        self.assertEqual(self.answer(tid, {"action": "reject", "expect_msg": "approve Bash"})[0], 200)
        self.assertEqual(self.result(box)[1], {"hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "deny", "message": "Rejected from phone (Sheltie)"}}})

    def test_claude_always_with_add_rules(self):
        body = load("claude-permission-addrules-doc.json")
        tid = self.tid_of(body)
        box = self.hook_bg("claude", body)
        p = self.blocked(tid)
        self.assertEqual(p["always_index"], 1)
        self.assertEqual(p["options"][1]["label"], "Yes, and don't ask again this session for Bash(npm test)")
        self.assertEqual(self.pusher.payloads[-1]["aps"]["category"], "HR_PERMISSION_ALWAYS")
        self.assertEqual(self.answer(tid, {"action": "always"})[1]["error"], "needs_prompt_id")
        self.assertEqual(self.answer(tid, {"action": "always", "prompt_id": p["id"]})[0], 200)
        self.assertEqual(self.result(box)[1], {"hookSpecificOutput": {
            "hookEventName": "PermissionRequest", "decision": {"behavior": "allow", "updatedPermissions": [
                {"type": "addRules", "rules": [{"toolName": "Bash", "ruleContent": "npm test"}],
                 "behavior": "allow", "destination": "session"}]}}})

    def test_always_rules_stay_in_session(self):
        # "이 세션 동안 허용"이 설정 파일에 영구 규칙으로 남지 않게: 어느 destination이든 session으로 돌려준다.
        rule = {"type": "addRules", "rules": [{"toolName": "Bash", "ruleContent": "ls"}], "behavior": "allow"}
        sugg = [dict(rule, destination=d) for d in ("localSettings", "projectSettings", "userSettings", "session")]
        sugg.append(dict(rule))  # destination 없음
        out = hr.hook_always_rules(sugg)
        self.assertEqual([s["destination"] for s in out], ["session"] * 5)
        self.assertEqual(sugg[0]["destination"], "localSettings")  # 입력은 그대로
        body = load("claude-permission-addrules-doc.json")
        for s in body["permission_suggestions"]:
            s["destination"] = "userSettings"
        tid = self.tid_of(body)
        box = self.hook_bg("claude", body)
        p = self.blocked(tid)
        self.assertEqual(self.prompt_answer(tid, {"prompt_id": p["id"], "option_index": p["always_index"]})[0], 200)
        perms = self.result(box)[1]["hookSpecificOutput"]["decision"]["updatedPermissions"]
        self.assertEqual({x["destination"] for x in perms}, {"session"})

    def test_claude_always_via_card_option(self):
        body = load("claude-permission-addrules-doc.json")
        tid = self.tid_of(body)
        box = self.hook_bg("claude", body)
        p = self.blocked(tid)
        self.assertEqual(self.prompt_answer(tid, {"prompt_id": p["id"], "option_index": 1})[0], 200)
        self.assertEqual(self.result(box)[1]["hookSpecificOutput"]["decision"]["updatedPermissions"][0]["type"],
                         "addRules")

    def test_claude_always_without_add_rules_is_409(self):
        body = load("claude-permission-bash-2.1.329.json")
        tid = self.tid_of(body)
        box = self.hook_bg("claude", body)
        p = self.blocked(tid)
        code, err = self.answer(tid, {"action": "always", "prompt_id": p["id"]})
        self.assertEqual((code, err["error"]), (409, "no_always"))
        self.assertEqual(self.prompt_answer(tid, {"prompt_id": p["id"], "option_index": 1})[0], 200)  # "No"
        self.assertEqual(self.result(box)[1]["hookSpecificOutput"]["decision"]["behavior"], "deny")

    def test_codex_allow_deny_no_always(self):
        body = load("codex-permission-bash-doc.json")
        tid = self.tid_of(body)
        box = self.hook_bg("codex", body)
        p = self.blocked(tid)
        self.assertEqual((p["title"], p["always_index"]), ("Run command", None))
        self.assertEqual(self.status(tid)["app"], "codex")
        self.assertEqual(self.pusher.payloads[-1]["aps"]["category"], "HR_PERMISSION")
        self.assertEqual(self.answer(tid, {"action": "always", "prompt_id": p["id"]})[1]["error"], "no_always")
        self.assertEqual(self.answer(tid, {"action": "approve", "prompt_id": p["id"]})[0], 200)
        out = self.result(box)[1]
        self.assertEqual(out, {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                                      "decision": {"behavior": "allow"}}})
        # 두 번째 요청: 거절
        box = self.hook_bg("codex", body)
        wait_for(lambda: self.app.hooks.active(tid) and self.app.hooks.active(tid).state == "waiting")
        self.assertEqual(self.answer(tid, {"action": "reject"})[0], 200)
        d = self.result(box)[1]["hookSpecificOutput"]["decision"]
        self.assertEqual(d, {"behavior": "deny", "message": "Rejected from phone (Sheltie)"})

    def test_codex_always_ignores_add_rules(self):
        body = load("claude-permission-addrules-doc.json", session_id="cdx-1234567890ab")
        tid = self.tid_of(body)
        box = self.hook_bg("codex", body)
        p = self.blocked(tid)
        self.assertIsNone(p["always_index"])
        self.assertEqual(self.answer(tid, {"action": "always", "prompt_id": p["id"]})[1]["error"], "no_always")
        self.answer(tid, {"action": "approve"})
        self.assertNotIn("updatedPermissions", self.result(box)[1]["hookSpecificOutput"]["decision"])

    def test_stale_prompt_id_409(self):
        body = load("claude-permission-bash-2.1.329.json")
        tid = self.tid_of(body)
        box = self.hook_bg("claude", body)
        p = self.blocked(tid)
        code, err = self.answer(tid, {"action": "approve", "prompt_id": "0123456789ab"})
        self.assertEqual((code, err["error"], err["prompt"]["id"]), (409, "prompt_changed", p["id"]))
        code, err = self.prompt_answer(tid, {"prompt_id": "0123456789ab", "option_index": 0})
        self.assertEqual((code, err["error"]), (409, "prompt_changed"))
        self.assertEqual(self.answer(tid, {"action": "reject", "prompt_id": p["id"]})[0], 200)
        self.result(box)
        # 같은 명령을 다시 물으면 카드 id가 바뀐다: 앞 알림의 답이 새 요청에 들어가지 않는다
        box = self.hook_bg("claude", body)
        wait_for(lambda: self.app.hooks.active(tid) and self.app.hooks.active(tid).state == "waiting")
        p2 = self.app.hooks.current_prompt(tid)
        self.assertNotEqual(p2["id"], p["id"])
        self.assertEqual(self.answer(tid, {"action": "approve", "prompt_id": p["id"]})[1]["error"], "prompt_changed")
        self.answer(tid, {"action": "reject", "prompt_id": p2["id"]})
        self.result(box)

    def test_disconnect_means_pc_answered(self):
        body = load("claude-permission-bash-2.1.329.json")
        tid = self.tid_of(body)
        data = json.dumps(body).encode()
        s = socket.create_connection(("127.0.0.1", self.port))
        s.sendall(b"POST /hook/claude HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer %s\r\n"
                  b"Content-Type: application/json\r\nContent-Length: %d\r\n\r\n" % (TOKEN.encode(), len(data)) + data)
        p = self.blocked(tid)
        s.close()  # 훅 프로세스가 kill된 것과 같다
        t0 = time.monotonic()
        wait_for(lambda: self.status(tid)["state"] == "working", timeout=3)
        self.assertLess(time.monotonic() - t0, 1.5)
        self.assertIsNone(self.app.hooks.current_prompt(tid))
        self.assertEqual(self.answer(tid, {"action": "approve", "prompt_id": p["id"]})[1]["error"], "prompt_changed")
        self.assertEqual(self.answer(tid, {"action": "approve"})[1]["error"], "not_blocked")

    def test_timeout_returns_empty_and_needs_pc(self):
        now = [100.0]
        self.app.hook_clock = lambda: now[0]
        body = load("claude-permission-bash-2.1.329.json")
        tid = self.tid_of(body)
        box = self.hook_bg("claude", body)
        p = self.blocked(tid)
        now[0] += 539
        time.sleep(0.5)
        self.assertTrue(box["th"].is_alive())
        now[0] += 2
        self.assertEqual(self.result(box), (200, {}))
        self.assertEqual(self.status(tid)["state"], "blocked")  # PC 대화상자는 아직 떠 있다
        self.assertEqual(self.answer(tid, {"action": "approve", "prompt_id": p["id"]})[1]["error"], "needs_pc")
        self.assertEqual(self.answer(tid, {"action": "reject"})[1]["error"], "needs_pc")
        self.assertEqual(self.req("GET", "/api/terminals/%s/prompt" % tid), (200, {"prompt": None}))
        # PC에서 답한 뒤 다음 이벤트가 오면 막힘이 풀린다
        self.hook("claude", load("claude-stop-2.1.329.json", session_id=body["session_id"]))
        self.assertEqual(self.status(tid)["state"], "done")

    def hold_bg(self, body):
        """붙잡히는 Claude 권한 요청. PC에서 풀리면 서버가 아무것도 쓰지 않고 끊는다: box["err"]에 예외."""
        box = {}

        def run():
            try:
                box["r"] = self.hook("claude", body)
            except Exception as e:  # noqa: BLE001 - 응답 없이 끊김(HOOK_NO_REPLY)
                box["err"] = e
        th = threading.Thread(target=run, daemon=True)
        th.start()
        box["th"] = th
        return box

    def assert_released_by(self, event_fixture):
        """붙잡은 채로 PC에서 승인(훅 프로세스는 살아 있음) → 다음 이벤트가 붙잡기를 gone으로 끝낸다."""
        body = load("claude-permission-bash-2.1.329.json")
        tid = self.tid_of(body)
        box = self.hold_bg(body)
        p = self.blocked(tid)
        hold = self.app.hooks.active(tid)
        self.assertEqual(self.status(tid)["state"], "blocked")
        n = len(self.pusher.payloads)
        self.assertEqual(self.hook("claude", load(event_fixture, session_id=body["session_id"])), (200, None))
        box["th"].join(5)
        self.assertFalse(box["th"].is_alive(), "hook still held")
        self.assertNotIn("r", box)  # HOOK_NO_REPLY: 응답 없이 끊는다
        self.assertEqual(hold.state, "gone")
        self.assertEqual(self.app.hooks.queued(tid), 0)
        self.assertIsNone(self.app.hooks.current_prompt(tid))
        self.assertEqual(self.answer(tid, {"action": "approve", "prompt_id": p["id"]})[1]["error"], "prompt_changed")
        return tid, n

    def test_stop_releases_hold_answered_on_pc(self):
        tid, n = self.assert_released_by("claude-stop-2.1.329.json")
        self.assertEqual(self.status(tid)["state"], "done")
        time.sleep(0.3)  # 붙잡던 스레드의 관측까지 끝난 뒤
        self.assertEqual(self.status(tid)["state"], "done")
        cats = [p["aps"]["category"] for p in self.pusher.payloads[n:]]
        self.assertEqual(cats.count("HR_DONE"), 1, cats)
        self.assertEqual([c for c in cats if c.startswith("HR_PERMISSION")], [])

    def test_user_prompt_submit_releases_hold_answered_on_pc(self):
        tid, n = self.assert_released_by("claude-user-prompt-submit-2.1.329.json")
        time.sleep(0.3)
        self.assertEqual(self.status(tid)["state"], "working")
        self.assertEqual([p["aps"]["category"] for p in self.pusher.payloads[n:]
                          if p["aps"].get("category", "").startswith("HR_PERMISSION")], [])

    def test_stop_drops_expired_hold(self):
        now = [100.0]
        self.app.hook_clock = lambda: now[0]
        body = load("claude-permission-bash-2.1.329.json")
        tid = self.tid_of(body)
        box = self.hook_bg("claude", body)
        self.blocked(tid)
        now[0] += hr.HOOK_HOLD_SEC + 1
        self.assertEqual(self.result(box), (200, {}))
        self.assertEqual(self.status(tid)["state"], "blocked")
        n = len(self.pusher.payloads)
        self.hook("claude", load("claude-stop-2.1.329.json", session_id=body["session_id"]))
        self.assertEqual(self.status(tid)["state"], "done")
        self.assertEqual([p["aps"]["category"] for p in self.pusher.payloads[n:]].count("HR_DONE"), 1)

    def test_post_tool_use_releases_same_tool_hold(self):
        """PC에서 승인해 실행된 요청: 같은 도구·입력의 PostToolUse가 붙잡기를 풀고 카드·다시 알림이 사라진다."""
        body = load("claude-permission-bash-2.1.329.json")
        tid = self.tid_of(body)
        box = self.hold_bg(body)
        p = self.blocked(tid)
        n = len(self.pusher.payloads)
        post = load("claude-posttooluse-bash-2.1.332.json", session_id=body["session_id"])
        self.assertEqual(self.hook("claude", post), (200, None))  # 출력 없음
        box["th"].join(5)
        self.assertFalse(box["th"].is_alive())
        self.assertNotIn("r", box)
        self.assertEqual(self.app.hooks.queued(tid), 0)
        self.assertIsNone(self.app.hooks.current_prompt(tid))
        wait_for(lambda: self.status(tid)["state"] == "idle")  # 세션 기준선(이 세션엔 UserPromptSubmit이 없었다)
        self.assertEqual(self.answer(tid, {"action": "approve", "prompt_id": p["id"]})[1]["error"], "prompt_changed")
        self.app.reminders.clock = lambda: 10_000.0
        self.app.check_reminders()
        self.assertEqual([x for x in self.pusher.payloads[n:] if x["aps"].get("category", "").startswith("HR_PERMISSION")], [])

    def test_post_tool_use_failure_releases_hold(self):
        body = load("claude-permission-bash-2.1.329.json")
        tid = self.tid_of(body)
        box = self.hold_bg(body)
        self.blocked(tid)
        post = load("claude-posttooluse-failure-bash-2.1.332.json", session_id=body["session_id"])
        self.assertEqual(self.hook("claude", post), (200, None))
        box["th"].join(5)
        self.assertFalse(box["th"].is_alive())
        self.assertEqual(self.app.hooks.queued(tid), 0)

    def test_post_tool_use_other_input_keeps_hold(self):
        body = load("claude-permission-bash-2.1.329.json")
        tid = self.tid_of(body)
        box = self.hold_bg(body)
        self.blocked(tid)
        post = load("claude-posttooluse-bash-2.1.332.json", session_id=body["session_id"],
                    tool_input={"command": "ls"})
        self.assertEqual(self.hook("claude", post), (200, None))
        post = load("claude-posttooluse-bash-2.1.332.json", session_id=body["session_id"], tool_name="Read")
        self.assertEqual(self.hook("claude", post), (200, None))
        time.sleep(0.5)
        self.assertTrue(box["th"].is_alive())
        self.assertEqual(self.status(tid)["state"], "blocked")
        self.answer(tid, {"action": "reject"})
        self.result(box)

    def test_post_tool_use_releases_only_matching_parallel_hold(self):
        body = load("claude-permission-bash-2.1.329.json")
        tid = self.tid_of(body)
        b1 = self.hold_bg(body)
        self.blocked(tid)
        b2 = self.hold_bg(dict(body, tool_input={"command": "touch two"}))
        wait_for(lambda: self.app.hooks.queued(tid) == 2)
        # 두 번째 요청만 PC에서 승인돼 실행됐다
        post = load("claude-posttooluse-bash-2.1.332.json", session_id=body["session_id"],
                    tool_input={"command": "touch two"})
        self.hook("claude", post)
        b2["th"].join(5)
        self.assertFalse(b2["th"].is_alive())
        self.assertTrue(b1["th"].is_alive())
        self.assertEqual(self.app.hooks.queued(tid), 1)
        self.assertEqual(self.status(tid)["state"], "blocked")
        self.assertIn("made_by_hook", self.app.hooks.current_prompt(tid)["body"])
        self.answer(tid, {"action": "reject"})
        self.result(b1)

    def test_post_tool_use_clears_expired_hold(self):
        """시간 초과로 {}를 돌려준 뒤에도 PC에서 승인되면 카드가 사라진다."""
        now = [100.0]
        self.app.hook_clock = lambda: now[0]
        body = load("claude-permission-bash-2.1.329.json")
        tid = self.tid_of(body)
        box = self.hook_bg("claude", body)
        self.blocked(tid)
        now[0] += hr.HOOK_HOLD_SEC + 1
        self.assertEqual(self.result(box), (200, {}))
        self.assertEqual(self.status(tid)["state"], "blocked")
        self.hook("claude", load("claude-posttooluse-bash-2.1.332.json", session_id=body["session_id"]))
        self.assertEqual(self.status(tid)["state"], "idle")
        self.assertEqual(self.app.hooks.queued(tid), 0)

    def test_post_tool_use_without_session_creates_nothing(self):
        post = load("claude-posttooluse-bash-2.1.332.json", session_id="5e5e5e5e-0000-4000-8000-000000000001")
        self.assertEqual(self.hook("claude", post), (200, None))
        self.assertFalse(self.app.hooks.has(self.tid_of(post)))

    def test_new_permission_request_keeps_queued_holds(self):
        body = load("claude-permission-bash-2.1.329.json")
        tid = self.tid_of(body)
        b1 = self.hook_bg("claude", body)
        p1 = self.blocked(tid)
        b2 = self.hook_bg("claude", dict(body, tool_input={"command": "touch two"}))
        wait_for(lambda: self.app.hooks.queued(tid) == 2)
        b3 = self.hook_bg("claude", dict(body, tool_input={"command": "touch three"}))
        wait_for(lambda: self.app.hooks.queued(tid) == 3)
        holds = list(self.app.hooks.sessions[tid]["holds"])
        self.assertEqual([h.state for h in holds], ["waiting"] * 3)
        self.assertEqual(self.app.hooks.current_prompt(tid)["id"], p1["id"])
        for b in (b1, b2, b3):
            self.assertTrue(b["th"].is_alive())
        for _ in range(3):
            self.answer(tid, {"action": "reject"})
            wait_for(lambda: not self.app.hooks.active(tid) or self.app.hooks.active(tid).state == "waiting")
        for b in (b1, b2, b3):
            self.assertEqual(self.result(b)[1]["hookSpecificOutput"]["decision"]["behavior"], "deny")

    def test_queued_requests_answer_in_order(self):
        body = load("claude-permission-bash-2.1.329.json")
        tid = self.tid_of(body)
        b1 = self.hook_bg("claude", body)
        p1 = self.blocked(tid)
        b2 = self.hook_bg("claude", dict(body, tool_input={"command": "touch two"}))
        wait_for(lambda: self.app.hooks.queued(tid) == 2)
        self.assertEqual(self.app.hooks.current_prompt(tid)["id"], p1["id"])
        self.answer(tid, {"action": "approve", "prompt_id": p1["id"]})
        self.assertEqual(self.result(b1)[1]["hookSpecificOutput"]["decision"]["behavior"], "allow")
        p2 = wait_for(lambda: (self.app.hooks.current_prompt(tid) or {}).get("id") != p1["id"]
                      and self.app.hooks.current_prompt(tid))
        self.assertIn("touch two", p2["body"])
        self.answer(tid, {"action": "reject", "prompt_id": p2["id"]})
        self.assertEqual(self.result(b2)[1]["hookSpecificOutput"]["decision"]["behavior"], "deny")

    def test_reminder_for_hook_session(self):
        now = [0.0]
        self.app.reminders.clock = lambda: now[0]
        body = load("claude-permission-bash-2.1.329.json")
        tid = self.tid_of(body)
        box = self.hook_bg("claude", body)
        p = self.blocked(tid)
        n = len(self.pusher.payloads)
        now[0] = 301
        self.app.check_reminders()
        self.assertEqual(len(self.pusher.payloads), n + 1)
        r = self.pusher.payloads[-1]
        self.assertEqual((r["reminder"], r["prompt_id"], r["aps"]["category"]), (1, p["id"], "HR_PERMISSION"))
        self.answer(tid, {"action": "reject"})
        self.result(box)


class NoDeviceTests(HookBase):
    """알림 받을 기기가 없으면 붙잡지 않고 바로 {}(PC 대화상자)."""

    def no_devices(self):
        for d in self.app.devices.list():
            self.app.devices.remove(d["token"])

    def assert_not_held(self, agent, body):
        t0 = time.monotonic()
        self.assertEqual(self.hook(agent, body), (200, {}))
        self.assertLess(time.monotonic() - t0, 2)
        tid = self.tid_of(body)
        self.assertIsNone(self.app.hooks.current_prompt(tid))
        self.assertEqual(self.app.hooks.queued(tid), 0)

    def test_no_devices_permission_not_held(self):
        self.no_devices()
        self.assert_not_held("claude", load("claude-permission-bash-2.1.329.json"))
        self.assertEqual(self.pusher.payloads, [])

    def test_no_devices_question_not_held(self):
        self.no_devices()
        self.assert_not_held("claude", load("claude-ask-single-doc.json"))

    def test_unreachable_devices_not_held(self):
        self.pusher.can_reach = lambda d: False
        self.assert_not_held("claude", load("claude-permission-bash-2.1.329.json"))

    def test_one_reachable_device_holds(self):
        self.app.devices.add("cd" * 32, "production")
        self.pusher.can_reach = lambda d: d["token"] == "cd" * 32
        body = load("claude-permission-bash-2.1.329.json")
        box = self.hook_bg("claude", body)
        p = self.blocked(self.tid_of(body))
        self.assertEqual(self.answer(self.tid_of(body), {"action": "approve", "prompt_id": p["id"]})[0], 200)
        self.assertEqual(self.result(box)[1]["hookSpecificOutput"]["decision"], {"behavior": "allow"})

    def test_pusher_can_reach(self):
        apns = {"token": "ab" * 32, "env": "production"}
        fcm = {"token": "x" * 40, "env": hr.FCM_ENV}
        signed = lambda d: dict(d, relay_sig="s")  # noqa: E731
        cfg = dict(hr.DEFAULTS, apns_key_path=os.path.join(self.tmp, "nokey.p8"), fcm_relay_url="")
        ap = hr.ApnsPusher(cfg, self.app.devices)
        self.assertFalse(ap.can_reach(apns))  # 키 파일 없음
        self.assertFalse(ap.can_reach(signed(fcm)))  # 중계 꺼짐
        open(cfg["apns_key_path"], "w").close()
        self.assertTrue(ap.can_reach(apns))
        ap2 = hr.ApnsPusher(dict(cfg, fcm_relay_url="https://relay.invalid"), self.app.devices)
        self.assertFalse(ap2.can_reach(fcm))  # 서명 없음
        self.assertTrue(ap2.can_reach(signed(fcm)))
        rp = hr.RelayPusher(dict(cfg, push_relay_url="https://relay.invalid", fcm_relay_url="https://relay.invalid"),
                            self.app.devices)
        self.assertFalse(rp.can_reach(apns))  # 서명 없음(relay_sig_bad로 지워진 경우 포함)
        self.assertTrue(rp.can_reach(signed(apns)))
        self.assertTrue(rp.can_reach(signed(fcm)))


class QuestionPlanTests(HookBase):
    def test_question_single(self):
        body = load("claude-ask-single-doc.json")
        tid = self.tid_of(body)
        box = self.hook_bg("claude", body)
        p = self.blocked(tid)
        self.assertEqual((p["kind"], p["title"], p["question"], p["multi_select"], p["free_text_index"]),
                         ("question", "Framework", "Which framework?", False, 2))
        self.assertEqual([o["label"] for o in p["options"]], ["React", "Vue", "Type something."])
        self.assertEqual(self.status(tid)["kind"], "question")
        self.assertEqual(self.pusher.payloads[-1]["aps"]["category"], "HR_BLOCKED")
        self.assertEqual(self.answer(tid, {"action": "approve", "prompt_id": p["id"]})[1]["error"], "needs_manual")
        self.assertEqual(self.prompt_answer(tid, {"prompt_id": p["id"], "option_index": 2})[1]["error"], "needs_text")
        self.assertEqual(self.prompt_answer(tid, {"prompt_id": p["id"], "option_indices": [0]})[1]["error"],
                         "not_multi_select")
        self.assertEqual(self.prompt_answer(tid, {"prompt_id": p["id"], "option_index": 1})[0], 200)
        self.assertEqual(self.result(box)[1], {"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "allow",
            "updatedInput": dict(body["tool_input"], answers={"Which framework?": "Vue"})}})

    def test_question_free_text(self):
        body = load("claude-ask-single-doc.json")
        tid = self.tid_of(body)
        box = self.hook_bg("claude", body)
        p = self.blocked(tid)
        self.assertEqual(self.prompt_answer(tid, {"prompt_id": p["id"], "text": "Svelte 쓰자"})[0], 200)
        out = self.result(box)[1]["hookSpecificOutput"]
        self.assertEqual(out["updatedInput"]["answers"], {"Which framework?": "Svelte 쓰자"})
        self.assertEqual(out["updatedInput"]["questions"], body["tool_input"]["questions"])

    def test_question_multi_select_then_next_tab(self):
        body = load("claude-ask-multi-doc.json")
        tid = self.tid_of(body)
        box = self.hook_bg("claude", body)
        p = self.blocked(tid)
        self.assertEqual((p["multi_select"], p["free_text_index"], len(p["options"])), (True, None, 3))
        self.assertEqual(p["tabs"], [{"label": "Features", "answered": False}, {"label": "DB", "answered": False}])
        n = len(self.pusher.payloads)
        self.assertEqual(self.prompt_answer(tid, {"prompt_id": p["id"], "option_index": 0})[1]["error"],
                         "use_option_indices")
        self.assertEqual(self.prompt_answer(tid, {"prompt_id": p["id"], "text": "x"})[1]["error"], "no_free_text")
        self.assertEqual(self.prompt_answer(tid, {"prompt_id": p["id"], "option_indices": [2, 0]})[0], 200)
        time.sleep(0.3)
        self.assertTrue(box["th"].is_alive())  # 두 번째 질문이 남았다
        p2 = self.app.hooks.current_prompt(tid)
        self.assertEqual((p2["question"], p2["tabs"][0]["answered"]), ("Which database?", True))
        self.assertEqual(len(self.pusher.payloads), n)  # 같은 막힘: 다시 알리지 않는다
        self.assertEqual(self.prompt_answer(tid, {"prompt_id": p["id"], "option_indices": [1]})[1]["error"],
                         "prompt_changed")
        self.assertEqual(self.prompt_answer(tid, {"prompt_id": p2["id"], "option_index": 1})[0], 200)
        self.assertEqual(self.result(box)[1]["hookSpecificOutput"]["updatedInput"]["answers"],
                         {"Which features?": "Auth, Search", "Which database?": "SQLite"})

    def test_question_reject_denies(self):
        body = load("claude-ask-single-doc.json")
        tid = self.tid_of(body)
        box = self.hook_bg("claude", body)
        self.blocked(tid)
        self.answer(tid, {"action": "reject"})
        self.assertEqual(self.result(box)[1]["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_exit_plan_allow(self):
        body = load("claude-exit-plan-doc.json")
        tid = self.tid_of(body)
        box = self.hook_bg("claude", body)
        p = self.blocked(tid)
        self.assertEqual((p["kind"], p["body"]), ("plan", body["tool_input"]["plan"]))
        self.assertEqual(self.status(tid)["kind"], "plan")
        self.assertEqual(self.answer(tid, {"action": "approve", "prompt_id": p["id"]})[0], 200)
        self.assertEqual(self.result(box)[1], {"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "allow", "updatedInput": body["tool_input"]}})

    def test_exit_plan_deny_and_feedback(self):
        body = load("claude-exit-plan-doc.json")
        tid = self.tid_of(body)
        box = self.hook_bg("claude", body)
        p = self.blocked(tid)
        self.assertEqual(self.prompt_answer(tid, {"prompt_id": p["id"], "option_index": 1})[0], 200)
        self.assertEqual(self.result(box)[1], {"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason": "Rejected from phone (Sheltie)"}})
        box = self.hook_bg("claude", body)
        wait_for(lambda: self.app.hooks.active(tid) and self.app.hooks.active(tid).state == "waiting")
        p = self.app.hooks.current_prompt(tid)
        self.assertEqual(self.prompt_answer(tid, {"prompt_id": p["id"], "text": "테스트 단계 먼저"})[0], 200)
        self.assertEqual(self.result(box)[1]["hookSpecificOutput"]["permissionDecisionReason"], "테스트 단계 먼저")

    def test_other_pretooluse_passes_through(self):
        body = load("claude-pretooluse-bash-2.1.329.json")
        self.assertEqual(self.hook("claude", body), (200, {}))
        self.assertIsNone(self.app.hooks.current_prompt(self.tid_of(body)))
        # 질문·계획 도구의 PermissionRequest는 붙잡지 않는다(PreToolUse가 맡는다)
        self.assertEqual(self.hook("claude", dict(body, hook_event_name="PermissionRequest",
                                                  tool_name="AskUserQuestion")), (200, {}))


class LifecycleTests(HookBase):
    def test_auth_and_proxy_headers(self):
        body = load("claude-session-start-2.1.329.json")
        self.assertEqual(self.hook("claude", body, token=None)[0], 401)
        self.assertEqual(self.hook("claude", body, token="wrong")[0], 401)
        for h in ("Tailscale-User-Login", "Tailscale-User-Name", "X-Forwarded-For"):
            with self.subTest(h=h):
                self.assertEqual(self.hook("claude", body, headers={h: "x"}), (403, {"error": "forbidden"}))
        self.assertEqual(self.app.hooks.sessions, {})
        self.assertEqual(self.hook("gemini", body)[0], 404)
        self.assertEqual(self.hook("claude", {"hook_event_name": "Stop"})[0], 400)

    def test_session_lifecycle_and_terminals(self):
        sid = "d43c1295-83b6-4d1a-aec5-887cb1b3a97b"
        tid = "h887cb1b3a97b"
        proj = os.path.join(self.home, ".claude", "projects", "-Users-me-proj")
        os.makedirs(proj)
        tpath = os.path.join(proj, sid + ".jsonl")
        with open(tpath, "w", encoding="utf-8") as f:
            f.write(base.fixture("claude-session-2.1.329.jsonl"))
        start = load("claude-session-start-2.1.329.json", transcript_path=tpath)
        self.assertEqual(self.hook("claude", start), (200, None))
        self.assertEqual(self.status(tid)["state"], "idle")
        code, body = self.req("GET", "/api/terminals")
        by = {t["id"]: t for t in body["terminals"]}
        self.assertEqual(by["t1"]["caps"], ["read", "keys", "send"])
        h = by[tid]
        self.assertEqual((h["caps"], h["agent_kind"], h["claude"], h["name"], h["cwd"], h["status"]["state"]),
                         ([], "claude", True, "proj", "/Users/me/proj", "idle"))
        self.assertTrue(h["preview"])
        # 화면·키·글·새 작업 없음
        self.assertEqual(self.req("GET", "/api/terminals/%s/screen" % tid), (409, {"error": "unsupported"}))
        self.assertEqual(self.req("POST", "/api/terminals/%s/keys" % tid, {"keys": ["1"]}),
                         (409, {"error": "unsupported"}))
        self.assertEqual(self.req("POST", "/api/terminals/%s/send" % tid, {"text": "hi"}),
                         (409, {"error": "unsupported"}))
        self.assertEqual(self.hc.keys_calls, [])
        tr = self.req("GET", "/api/terminals/%s/transcript" % tid)[1]
        self.assertEqual(tr["session_id"], sid)
        self.assertTrue(tr["turns"])
        self.hook("claude", load("claude-user-prompt-submit-2.1.329.json"))
        self.assertEqual(self.status(tid)["state"], "working")
        n = len(self.pusher.payloads)
        self.hook("claude", load("claude-stop-2.1.329.json"))
        self.assertEqual(self.status(tid)["state"], "done")
        self.assertEqual(self.pusher.payloads[n]["aps"]["category"], "HR_DONE")
        # 다른 Stop 훅이 막아 같은 prompt_id의 Stop이 또 오면(실측) 끝남을 두 번 알리지 않는다
        self.hook("claude", load("claude-stop-2.1.329.json", stop_hook_active=True))
        self.assertEqual([p["aps"]["category"] for p in self.pusher.payloads[n:]], ["HR_DONE"])
        self.hook("claude", load("claude-session-end-2.1.329.json"))
        self.assertFalse(self.app.hooks.has(tid))
        self.assertEqual(self.pusher.payloads[-1]["aps"]["category"], "HR_EXITED")
        self.assertNotIn(tid, [t["id"] for t in self.req("GET", "/api/terminals")[1]["terminals"]])
        self.assertEqual(self.req("GET", "/api/terminals/%s/prompt" % tid)[0], 404)

    def test_clear_does_not_notify_exit(self):
        self.hook("claude", load("claude-session-start-2.1.329.json"))
        n = len(self.pusher.payloads)
        self.hook("claude", load("claude-session-end-2.1.329.json", reason="clear"))
        self.assertEqual(len(self.pusher.payloads), n)
        self.assertEqual(self.app.hooks.sessions, {})

    def test_unknown_session_auto_registers(self):
        body = load("codex-user-prompt-submit-0.171.0.json")
        self.assertEqual(self.hook("codex", body), (200, None))
        tid = self.tid_of(body)
        self.assertEqual((self.app.hooks.get(tid)["agent"], self.status(tid)["state"]), ("codex", "working"))
        self.assertEqual(self.hook("claude", body)[0], 409)  # 같은 세션을 다른 에이전트로
        n = len(self.pusher.payloads)
        self.hook("codex", load("codex-stop-doc.json"))
        # UserPromptSubmit 없이 다음 턴의 Stop만 오면(turn_id가 다르다) 새 턴으로 보지만, 사용자 말이 없던 턴이라
        # 끝남은 다시 알리지 않는다(사용자 말 하나당 한 번). 새 사용자 말 뒤에는 다시 한 번
        self.hook("codex", load("codex-stop-doc.json", turn_id="01a1fd04-0000-7000-8000-000000000002"))
        self.assertEqual(self.status(tid)["state"], "done")
        self.hook("codex", load("codex-user-prompt-submit-0.171.0.json", turn_id="01a1fd04-0000-7000-8000-000000000003"))
        self.hook("codex", load("codex-stop-doc.json", turn_id="01a1fd04-0000-7000-8000-000000000003"))
        self.hook("codex", load("codex-stop-doc.json", turn_id="01a1fd04-0000-7000-8000-000000000003"))
        self.assertEqual([p["aps"]["category"] for p in self.pusher.payloads[n:]], ["HR_DONE", "HR_DONE"])
        n = len(self.pusher.payloads)
        self.hook("codex", load("codex-session-end-0.171.0.json"))
        self.assertFalse(self.app.hooks.has(tid))
        self.assertEqual(len(self.pusher.payloads), n)  # Codex 종료는 hc 경로처럼 알리지 않는다

    def test_transcript_outside_home_ignored(self):
        outside = os.path.join(self.tmp, "x.jsonl")
        with open(outside, "w", encoding="utf-8") as f:
            f.write(base.fixture("claude-session-2.1.329.jsonl"))
        body = load("claude-session-start-2.1.329.json", transcript_path=outside)
        self.hook("claude", body)
        tid = self.tid_of(body)
        self.assertEqual(self.req("GET", "/api/terminals/%s/transcript" % tid)[1]["turns"], [])

    def test_prune_stale_sessions(self):
        self.hook("claude", load("claude-session-start-2.1.329.json"))
        self.app.hooks.wall = lambda: time.time() + hr.HOOK_STALE_SEC + 10
        self.app.poll()
        self.assertEqual(self.app.hooks.sessions, {})


class NoHcTests(unittest.TestCase):
    def test_runs_with_hooks_only_and_warns_once(self):
        tmp = tempfile.mkdtemp()
        cfg = dict(hr.DEFAULTS, token=TOKEN, _dir=tmp, _path=os.path.join(tmp, "config.json"), home=tmp)
        for path in ("", "/nonexistent/hc"):
            with self.subTest(path=path):
                app = hr.App(cfg, hr.Hc(path), hr.DeviceStore(os.path.join(tmp, "d.json")), base.FakePusher())
                with self.assertLogs("sheltie", level="WARNING") as cm:
                    app.poll()
                    app.poll()
                    self.assertEqual(app.api_terminals(), {"terminals": []})
                self.assertEqual(len([m for m in cm.output if "hc가 없다" in m]), 1)
                app.hook_event("claude", load("claude-session-start-2.1.329.json"))
                self.assertEqual([t["id"] for t in app.api_terminals()["terminals"]], ["h887cb1b3a97b"])
                with self.assertRaises(hr.ApiError) as e:
                    app.api_agents({"path": ""})
                self.assertEqual((e.exception.code, e.exception.body), (409, {"error": "unsupported"}))
                with self.assertRaises(hr.ApiError) as e:
                    app.api_screen("t1", None)
                self.assertEqual(e.exception.code, 404)


class HookCommandTests(HookBase):
    def setUp(self):
        super().setUp()
        self.cfg = os.path.join(self.tmp, "config.json")
        self.write_cfg(self.port)

    def write_cfg(self, port):
        with open(self.cfg, "w") as f:
            json.dump({"token": TOKEN, "port": port, "bind": "127.0.0.1", "push_relay_url": "off",
                       "hc_path": ""}, f)

    def run_cmd(self, agent, body, env=None):
        out = io.BytesIO()
        with mock.patch.dict(os.environ, {"SHELTIE_CONFIG": self.cfg}):
            rc = hr.cmd_hook([agent], stdin=io.BytesIO(json.dumps(body).encode()), stdout=out,
                             env=env if env is not None else {})
        return rc, out.getvalue()

    def test_inside_herdr_prints_nothing(self):
        rc, out = self.run_cmd("claude", load("claude-session-start-2.1.329.json"), env={"HC_TERMINAL_ID": "t3"})
        self.assertEqual((rc, out), (0, b""))
        self.assertEqual(self.app.hooks.sessions, {})

    def test_grok_borrowing_claude_hooks_ignored(self):
        # Grok Build는 ~/.claude 훅을 같이 부른다: 환경변수로도, 입력 필드로도 걸러져 세션이 생기지 않는다.
        rc, out = self.run_cmd("claude", load("claude-permission-bash-2.1.329.json"),
                               env={"GROK_SESSION_ID": "01a1fd55-ac01-7f50-854c-d469f50e165c"})
        self.assertEqual((rc, out), (0, b""))
        grok = dict(load("claude-permission-bash-2.1.329.json"), hookEventName="permission_request",
                    sessionId="01a1fd55-ac01-7f50-854c-d469f50e165c", workspaceRoot="/tmp/w")
        rc, out = self.run_cmd("claude", grok)
        self.assertEqual((rc, out), (0, b""))
        self.assertEqual(self.app.hooks.sessions, {})

    def test_headless_claude_ignored(self):
        # claude -p·Agent SDK 실행은 세션을 만들지 않는다(끝날 때 종료 알림이 가지 않는다). 대화형·데스크톱은 그대로.
        for ep in ("sdk-cli", "sdk-ts", "sdk-py"):
            rc, out = self.run_cmd("claude", load("claude-session-start-2.1.329.json"), env={"CLAUDE_CODE_ENTRYPOINT": ep})
            self.assertEqual((rc, out), (0, b""))
        self.assertEqual(self.app.hooks.sessions, {})
        for ep in ("cli", "claude-desktop"):
            self.run_cmd("claude", load("claude-session-start-2.1.329.json"), env={"CLAUDE_CODE_ENTRYPOINT": ep})
        self.assertEqual(len(self.app.hooks.sessions), 1)

    def test_server_down_prints_nothing(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        self.write_cfg(port)
        rc, out = self.run_cmd("claude", load("claude-permission-bash-2.1.329.json"))
        self.assertEqual((rc, out), (0, b""))
        with mock.patch.dict(os.environ, {"SHELTIE_CONFIG": os.path.join(self.tmp, "none.json")}):
            self.assertEqual(hr.cmd_hook(["claude"], stdin=io.BytesIO(b"{}"), stdout=io.BytesIO(), env={}), 0)
        self.assertEqual(hr.cmd_hook(["gemini"], stdin=io.BytesIO(b"{}"), stdout=io.BytesIO(), env={}), 0)

    def test_round_trip_prints_decision(self):
        rc, out = self.run_cmd("claude", load("claude-session-start-2.1.329.json"))
        self.assertEqual((rc, out), (0, b""))  # 즉시 응답, 빈 본문
        self.assertTrue(self.app.hooks.has("h887cb1b3a97b"))
        body = load("claude-permission-bash-2.1.329.json")
        tid = self.tid_of(body)
        box = {}
        th = threading.Thread(target=lambda: box.update(r=self.run_cmd("claude", body)), daemon=True)
        th.start()
        p = self.blocked(tid)
        self.answer(tid, {"action": "approve", "prompt_id": p["id"]})
        th.join(5)
        rc, out = box["r"]
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out), {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                                                  "decision": {"behavior": "allow"}}})

    def test_subprocess_no_log_file(self):
        log_path = os.path.join(self.tmp, "logs", "sheltie.log")
        env = dict(os.environ, SHELTIE_CONFIG=self.cfg, SHELTIE_LOG=log_path)
        env.pop("HC_TERMINAL_ID", None)
        body = load("codex-permission-bash-doc.json")
        tid = self.tid_of(body)
        proc = subprocess.Popen([sys.executable, SERVER, "hook", "codex"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        proc.stdin.write(json.dumps(body).encode())
        proc.stdin.close()
        p = self.blocked(tid)
        self.answer(tid, {"action": "reject", "prompt_id": p["id"]})
        out = proc.stdout.read()
        self.assertEqual(proc.wait(10), 0)
        proc.stdout.close()
        proc.stderr.close()
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["decision"]["behavior"], "deny")
        self.assertFalse(os.path.exists(log_path))


# ---------------------------------------------------------------- Qwen Code·OpenCode·다른 에이전트


def qwen_chat_path(home, sid):
    return os.path.join(home, ".qwen", "projects", "-Users-me-work-demo", "chats", sid + ".jsonl")


class QwenHookTests(HookBase):
    """Qwen Code 0.28.3 공식 훅(Claude와 같은 스키마, 실측 fixture qwen-*-0.28.3)."""

    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("QWEN_HOME", None)

    def test_shell_allow_card_status_push(self):
        body = load("qwen-permission-shell-0.28.3.json")
        tid = self.tid_of(body)
        box = self.hook_bg("qwen", body)
        p = self.blocked(tid)
        self.assertEqual((p["kind"], p["title"], p["always_index"]), ("approval", "Shell", None))
        self.assertIn("touch hook1.txt", p["body"])
        self.assertEqual([o["label"] for o in p["options"]], ["Yes, allow once", "No"])
        self.assertEqual(self.status(tid), {"state": "blocked", "kind": "permission",
                                            "msg": "approve run_shell_command", "app": "qwen-code"})
        push = self.pusher.payloads[-1]
        self.assertEqual((push["aps"]["category"], push["terminal"], push["prompt_id"]), ("HR_PERMISSION", tid, p["id"]))
        self.assertEqual(self.answer(tid, {"action": "approve", "prompt_id": p["id"]})[0], 200)
        self.assertEqual(self.result(box), (200, {"hookSpecificOutput": {
            "hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}}))
        wait_for(lambda: self.status(tid)["state"] == "working")

    def test_write_and_edit_cards_and_deny(self):
        body = load("qwen-permission-write-0.28.3.json")
        tid = self.tid_of(body)
        box = self.hook_bg("qwen", body)
        p = self.blocked(tid)
        self.assertEqual(p["title"], "WriteFile")
        self.assertIn("/Users/me/work/demo/hook3.txt", p["body"])
        self.assertIn("hi", p["body"].split("\n\n")[1])
        self.assertEqual(self.prompt_answer(tid, {"prompt_id": p["id"], "option_index": 1})[0], 200)  # No
        self.assertEqual(self.result(box)[1], {"hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "deny", "message": "Rejected from phone (Sheltie)"}}})
        box = self.hook_bg("qwen", load("qwen-permission-edit-doc.json"))
        wait_for(lambda: self.app.hooks.active(tid) and self.app.hooks.active(tid).state == "waiting")
        p = self.app.hooks.current_prompt(tid)
        self.assertEqual(p["title"], "Edit")
        self.assertIn("- hi\n+ hello", p["body"])
        self.assertEqual(self.answer(tid, {"action": "reject"})[0], 200)
        self.assertEqual(self.result(box)[1]["hookSpecificOutput"]["decision"]["behavior"], "deny")

    def test_no_always(self):
        body = load("qwen-permission-shell-0.28.3.json")
        tid = self.tid_of(body)
        box = self.hook_bg("qwen", body)
        p = self.blocked(tid)
        self.assertEqual(self.answer(tid, {"action": "always", "prompt_id": p["id"]})[1]["error"], "no_always")
        self.answer(tid, {"action": "approve", "prompt_id": p["id"]})
        self.assertNotIn("updatedPermissions", self.result(box)[1]["hookSpecificOutput"]["decision"])

    def test_interactive_tools_not_held(self):
        # allow를 줘도 Qwen이 대화상자를 띄우는 도구(ask_user_question 등)는 붙잡지 않는다
        for tool in ("ask_user_question", "exit_plan_mode", "propose_goal"):
            with self.subTest(tool=tool):
                body = load("qwen-permission-shell-0.28.3.json", tool_name=tool, tool_input={"x": 1})
                self.assertEqual(self.hook("qwen", body), (200, {}))
                self.assertIsNone(self.app.hooks.current_prompt(self.tid_of(body)))
                self.assertNotEqual(self.status(self.tid_of(body))["state"], "blocked")

    def test_timeout_and_disconnect(self):
        now = [100.0]
        self.app.hook_clock = lambda: now[0]
        body = load("qwen-permission-shell-0.28.3.json")
        tid = self.tid_of(body)
        box = self.hook_bg("qwen", body)
        p = self.blocked(tid)
        now[0] += 541
        self.assertEqual(self.result(box), (200, {}))
        self.assertEqual(self.answer(tid, {"action": "approve", "prompt_id": p["id"]})[1]["error"], "needs_pc")
        self.hook("qwen", load("qwen-stop-0.28.3.json"))
        self.assertEqual(self.status(tid)["state"], "done")
        # 연결 끊김 = PC에서 답함
        data = json.dumps(body).encode()
        s = socket.create_connection(("127.0.0.1", self.port))
        s.sendall(b"POST /hook/qwen HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer %s\r\n"
                  b"Content-Type: application/json\r\nContent-Length: %d\r\n\r\n" % (TOKEN.encode(), len(data)) + data)
        p = wait_for(lambda: self.app.hooks.active(tid) and self.app.hooks.active(tid).state == "waiting"
                     and self.app.hooks.current_prompt(tid))
        s.close()
        wait_for(lambda: self.status(tid)["state"] == "working", timeout=3)
        self.assertEqual(self.answer(tid, {"action": "approve", "prompt_id": p["id"]})[1]["error"], "prompt_changed")

    def test_stale_prompt_id_409(self):
        body = load("qwen-permission-shell-0.28.3.json")
        tid = self.tid_of(body)
        box = self.hook_bg("qwen", body)
        p = self.blocked(tid)
        code, err = self.answer(tid, {"action": "approve", "prompt_id": "0123456789ab"})
        self.assertEqual((code, err["error"]), (409, "prompt_changed"))
        self.answer(tid, {"action": "reject", "prompt_id": p["id"]})
        self.result(box)

    def test_lifecycle_transcript_and_terminals(self):
        start = load("qwen-session-start-0.28.3.json")
        sid = start["session_id"]
        tid = self.tid_of(start)
        tpath = qwen_chat_path(self.home, sid)
        os.makedirs(os.path.dirname(tpath))
        with open(tpath, "w", encoding="utf-8") as f:
            f.write(base.fixture("qwen-session-0.28.3.jsonl"))
        self.assertEqual(self.hook("qwen", dict(start, transcript_path=tpath)), (200, None))
        h = {t["id"]: t for t in self.req("GET", "/api/terminals")[1]["terminals"]}[tid]
        self.assertEqual((h["caps"], h["agent"], h["agent_kind"], h["claude"], h["name"], h["status"]),
                         ([], "qwen", "qwen", True, "demo", {"state": "idle", "app": "qwen-code"}))
        self.assertTrue(h["preview"])
        tr = self.req("GET", "/api/terminals/%s/transcript" % tid)[1]
        self.assertEqual(tr["session_id"], sid)
        self.assertTrue(tr["turns"])
        self.hook("qwen", load("qwen-user-prompt-submit-0.28.3.json"))
        self.assertEqual(self.status(tid)["state"], "working")
        n = len(self.pusher.payloads)
        stop = load("qwen-stop-0.28.3.json")
        self.hook("qwen", stop)
        self.hook("qwen", stop)  # 같은 prompt_id의 Stop: 다시 알리지 않는다
        self.hook("qwen", dict(stop, prompt_id=sid + "########2"))  # 사용자 말 없는 다음 턴: 알리지 않는다
        self.hook("qwen", load("qwen-user-prompt-submit-0.28.3.json", prompt_id=sid + "########3"))
        self.hook("qwen", dict(stop, prompt_id=sid + "########3"))  # 사용자 말 뒤 다음 턴: 다시 알린다
        self.assertEqual([p["aps"]["category"] for p in self.pusher.payloads[n:]], ["HR_DONE", "HR_DONE"])
        self.assertEqual(self.hook("claude", stop)[1]["error"], "agent_mismatch")
        n = len(self.pusher.payloads)
        self.hook("qwen", load("qwen-session-end-0.28.3.json"))
        self.assertFalse(self.app.hooks.has(tid))
        self.assertEqual(len(self.pusher.payloads), n)

    def test_transcript_outside_qwen_projects_ignored(self):
        start = load("qwen-session-start-0.28.3.json")
        tid = self.tid_of(start)
        for path in (os.path.join(self.tmp, "x.jsonl"), os.path.join(self.home, ".qwen", "x.jsonl")):
            with self.subTest(path=path):
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(base.fixture("qwen-session-0.28.3.jsonl"))
                self.hook("qwen", dict(start, transcript_path=path))
                self.assertEqual(self.req("GET", "/api/terminals/%s/transcript" % tid)[1]["turns"], [])


class OpencodeHookTests(HookBase):
    """OpenCode 1.19.6 플러그인(sheltie.js)이 보내는 본문(fixture opencode-permission-bash-plugin)."""

    def setUp(self):
        super().setUp()
        import test_opencode
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("XDG_DATA_HOME", None)
        d = os.path.join(self.home, ".local", "share", "opencode")
        os.makedirs(d)
        self.db = os.path.join(d, "opencode.db")
        self.sid = test_opencode.build_db(self.db, ["opencode-session-1.19.6.jsonl"])[0]

    def body(self, **over):
        return load("opencode-permission-bash-plugin.json", **dict({"session_id": self.sid, "opencode_db": self.db}, **over))

    def test_always_card_and_answers(self):
        body = self.body()
        tid = self.tid_of(body)
        box = self.hook_bg("opencode", body)
        p = self.blocked(tid)
        self.assertEqual((p["kind"], p["title"], p["always_index"], p["reject_index"]),
                         ("approval", "Shell command", 1, 2))
        self.assertEqual([o["label"] for o in p["options"]], ["Allow once", "Allow always: touch *", "Reject"])
        self.assertIn("touch hooko1.txt", p["body"])
        self.assertEqual(self.status(tid), {"state": "blocked", "kind": "permission", "msg": "approve bash",
                                            "app": "opencode"})
        self.assertEqual(self.pusher.payloads[-1]["aps"]["category"], "HR_PERMISSION_ALWAYS")
        self.assertEqual(self.answer(tid, {"action": "always", "prompt_id": p["id"]})[0], 200)
        self.assertEqual(self.result(box)[1], {"hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "allow", "updatedPermissions": ["touch *"]}}})
        # once(카드 0번)·reject
        box = self.hook_bg("opencode", body)
        wait_for(lambda: self.app.hooks.active(tid) and self.app.hooks.active(tid).state == "waiting")
        p = self.app.hooks.current_prompt(tid)
        self.assertEqual(self.prompt_answer(tid, {"prompt_id": p["id"], "option_index": 0})[0], 200)
        self.assertEqual(self.result(box)[1]["hookSpecificOutput"]["decision"], {"behavior": "allow"})
        box = self.hook_bg("opencode", body)
        wait_for(lambda: self.app.hooks.active(tid) and self.app.hooks.active(tid).state == "waiting")
        p = self.app.hooks.current_prompt(tid)
        self.assertEqual(self.prompt_answer(tid, {"prompt_id": p["id"], "option_index": 2})[0], 200)
        self.assertEqual(self.result(box)[1]["hookSpecificOutput"]["decision"]["behavior"], "deny")

    def test_no_always_patterns_is_409(self):
        body = self.body(always=[])
        tid = self.tid_of(body)
        box = self.hook_bg("opencode", body)
        p = self.blocked(tid)
        self.assertIsNone(p["always_index"])
        self.assertEqual([o["label"] for o in p["options"]], ["Allow once", "Reject"])
        self.assertEqual(self.answer(tid, {"action": "always", "prompt_id": p["id"]})[1]["error"], "no_always")
        self.answer(tid, {"action": "approve", "prompt_id": p["id"]})
        self.assertEqual(self.result(box)[1]["hookSpecificOutput"]["decision"], {"behavior": "allow"})

    def test_edit_card_body(self):
        body = self.body(tool_name="edit", always=["*"], tool_input={
            "patterns": ["hello.py"], "metadata": {"filepath": "/Users/me/work/demo/hello.py",
                                                   "diff": "-print(1)\n+print(2)"}})
        tid = self.tid_of(body)
        box = self.hook_bg("opencode", body)
        p = self.blocked(tid)
        self.assertEqual(p["title"], "Edit file")
        self.assertIn("/Users/me/work/demo/hello.py", p["body"])
        self.assertIn("+print(2)", p["body"])
        self.answer(tid, {"action": "reject"})
        self.result(box)

    def test_terminals_transcript_preview_and_screen_poll(self):
        start = self.body(hook_event_name="SessionStart")
        for k in ("tool_name", "tool_input", "always", "request_id"):
            start.pop(k)
        tid = self.tid_of(start)
        self.assertEqual(self.hook("opencode", start), (200, None))
        code, body = self.req("GET", "/api/terminals")
        self.assertEqual(code, 200, body)
        h = {t["id"]: t for t in body["terminals"]}[tid]
        self.assertEqual((h["caps"], h["agent_kind"], h["claude"], h["status"]),
                         ([], "opencode", True, {"state": "idle", "app": "opencode"}))
        self.assertTrue(h["preview"])
        tr = self.req("GET", "/api/terminals/%s/transcript" % tid)[1]
        self.assertEqual(tr["session_id"], self.sid)
        self.assertEqual(tr["turns"][0], {"role": "user", "parts": [{"kind": "text", "text": "hello"}]})
        # 화면이 없는 훅 세션은 화면 판정 주기에서 빠진다(hc read를 부르지 않는다)
        self.app.screen_poll()
        self.assertEqual(self.req("GET", "/api/terminals/%s/prompt" % tid), (200, {"prompt": None}))
        # 턴: busy → UserPromptSubmit, idle → Stop(플러그인이 센 turn_id)
        self.hook("opencode", dict(start, hook_event_name="UserPromptSubmit"))
        self.assertEqual(self.status(tid)["state"], "working")
        n = len(self.pusher.payloads)
        self.hook("opencode", dict(start, hook_event_name="Stop", turn_id=self.sid + "#1"))
        self.assertEqual(self.status(tid)["state"], "done")
        self.assertEqual([p["aps"]["category"] for p in self.pusher.payloads[n:]], ["HR_DONE"])
        self.hook("opencode", dict(start, hook_event_name="SessionEnd", reason="exit"))
        self.assertFalse(self.app.hooks.has(tid))

    def test_db_path_must_be_opencode_db_under_home(self):
        other = os.path.join(self.home, "other.db")
        shutil.copy(self.db, other)
        outside = os.path.join(self.tmp, "opencode.db")
        shutil.copy(self.db, outside)
        link_dir = os.path.join(self.home, "lnk")
        os.makedirs(link_dir)
        os.symlink(outside, os.path.join(link_dir, "opencode.db"))
        for path in (other, outside, os.path.join(link_dir, "opencode.db"), os.path.join(self.home, "opencode.db"),
                     None, 5):
            with self.subTest(path=path):
                self.app.hooks.sessions.clear()
                start = self.body(hook_event_name="SessionStart", opencode_db=path)
                tid = self.tid_of(start)
                self.hook("opencode", start)
                self.assertEqual(self.req("GET", "/api/terminals/%s/transcript" % tid)[1]["turns"], [])

    def test_agent_mismatch(self):
        body = self.body(hook_event_name="SessionStart")
        self.hook("opencode", body)
        self.assertEqual(self.hook("qwen", body)[1]["error"], "agent_mismatch")


class ForeignHookTests(HookBase):
    def test_cursor_borrowing_claude_hooks_makes_no_session(self):
        # Cursor 2026.11.19는 ~/.claude/settings.json의 훅을 Claude 훅으로 같이 부른다(cursor_version·conversation_id)
        for ev in ("UserPromptSubmit", "Stop", "SessionStart", "PermissionRequest"):
            with self.subTest(ev=ev):
                self.assertEqual(self.hook("claude", load("cursor-claude-compat-doc.json", hook_event_name=ev)),
                                 (200, None))
        self.assertEqual(self.app.hooks.sessions, {})

    def test_unknown_event_makes_no_session(self):
        body = load("claude-session-start-2.1.329.json")
        for ev in ("beforeShellExecution", "Notification", "PostToolUse", "SubagentStop"):
            with self.subTest(ev=ev):
                self.assertEqual(self.hook("claude", dict(body, hook_event_name=ev)), (200, None))
        self.assertEqual(self.app.hooks.sessions, {})
        self.assertEqual(self.pusher.payloads, [])


class HookCommandMoreTests(HookCommandTests):
    """`sheltie.py hook qwen|opencode` 왕복(부모 테스트는 상속만 하므로 다시 돌지 않게 비운다)."""

    def test_qwen_and_opencode_round_trip(self):
        for agent, body in (("qwen", load("qwen-permission-shell-0.28.3.json")),
                            ("opencode", load("opencode-permission-bash-plugin.json"))):
            with self.subTest(agent=agent):
                tid = self.tid_of(body)
                box = {}
                th = threading.Thread(target=lambda: box.update(r=self.run_cmd(agent, body)), daemon=True)
                th.start()
                p = self.blocked(tid)
                self.answer(tid, {"action": "approve", "prompt_id": p["id"]})
                th.join(5)
                rc, out = box["r"]
                self.assertEqual(rc, 0)
                self.assertEqual(json.loads(out)["hookSpecificOutput"]["decision"], {"behavior": "allow"})
        rc, out = self.run_cmd("qwen", load("qwen-permission-shell-0.28.3.json"), env={"HC_TERMINAL_ID": "t3"})
        self.assertEqual((rc, out), (0, b""))


for _n in [n for n in dir(HookCommandTests) if n.startswith("test_")]:
    setattr(HookCommandMoreTests, _n, None)


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 가짜 sheltie.py: stdin 본문을 LOG에 한 줄 쓰고, request_id로 답을 고른다(per_hold는 오래 붙잡는다).
FAKE_HOOK_SERVER = r"""import json, os, sys, time
body = json.loads(sys.stdin.read())
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "body": body}) + "\n")
rid = body.get("request_id")
if rid == "per_hold":
    time.sleep(30)
d = {"per_once": {"behavior": "allow"}, "per_always": {"behavior": "allow", "updatedPermissions": ["touch *"]},
     "per_deny": {"behavior": "deny", "message": "no"}}.get(rid)
if d:
    sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": d}}))
"""

PLUGIN_HARNESS = r"""
const mod = await import(process.argv[2])
const calls = []
const client = { postSessionIdPermissionsPermissionId: async (a) => { calls.push(a); return { data: true } } }
const hooks = await mod.SheltiePlugin({ directory: "/Users/me/work/demo", client: client, serverUrl: new URL("http://localhost:4096") })
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const ev = (type, properties) => hooks.event({ event: { id: "evt_1", type: type, properties: properties } })
const sid = "ses_df72331a4fferjkvc85gV6PDCf"
const asked = (id) => ({ id: id, sessionID: sid, permission: "bash", patterns: ["touch x"], metadata: { command: "touch x" },
  always: ["touch *"], tool: { messageID: "msg_1", callID: "call_1" } })
await ev("session.created", { sessionID: sid, info: { id: sid, directory: "/Users/me/work/demo" } })
await ev("session.created", { sessionID: "ses_child00000001", info: { id: "ses_child00000001", parentID: sid } })
await ev("session.status", { sessionID: sid, status: { type: "busy" } })
await ev("session.status", { sessionID: sid, status: { type: "busy" } })
await ev("session.status", { sessionID: "ses_child00000001", status: { type: "busy" } })
for (const id of ["per_once", "per_always", "per_deny"]) await ev("permission.asked", asked(id))
await ev("permission.asked", Object.assign(asked("per_child"), { sessionID: "ses_child00000001" }))
await ev("permission.asked", asked("per_hold"))
const t0 = Date.now()
while (calls.length < 3 && Date.now() - t0 < 10000) await sleep(50)
await sleep(300)
await ev("permission.replied", { sessionID: sid, requestID: "per_hold", reply: "once" })
await sleep(500)
await ev("session.status", { sessionID: sid, status: { type: "idle" } })
await ev("session.idle", { sessionID: sid })
await ev("session.idle", { sessionID: sid })
await sleep(1500)
console.log(JSON.stringify({ calls: calls, ms: Date.now() - t0 }))
process.exit(0)
"""


@unittest.skipUnless(shutil.which("node"), "node가 없다")
class OpencodePluginTests(unittest.TestCase):
    """설치기가 쓰는 OpenCode 플러그인(sheltie.js)을 node로 돌린다. 가짜 client·가짜 sheltie.py."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.log = os.path.join(self.tmp, "log.jsonl")
        self.server = os.path.join(self.tmp, "fake sheltie.py")
        with open(self.server, "w") as f:
            f.write(FAKE_HOOK_SERVER)
        with open(os.path.join(ROOT, "install.sh"), encoding="utf-8") as f:
            t = f.read()
        merge = t[t.index("# ---- hooks_merge.py begin"):t.index("# ---- hooks_merge.py end")]
        self.plugin = os.path.join(self.tmp, "plugins", "sheltie.js")
        r = subprocess.run([sys.executable, "-c", merge, "install", "opencode", self.plugin, sys.executable,
                            self.server, "posix"], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.stdout.strip(), "added", r.stderr)

    def run_harness(self):
        mjs = os.path.join(self.tmp, "plugin.mjs")
        shutil.copy(self.plugin, mjs)
        h = os.path.join(self.tmp, "harness.mjs")
        with open(h, "w") as f:
            f.write(PLUGIN_HARNESS)
        env = dict(os.environ, FAKE_LOG=self.log, HOME=self.tmp, USERPROFILE=self.tmp)  # node os.homedir(): 윈도우는 USERPROFILE
        env.pop("XDG_DATA_HOME", None)
        r = subprocess.run(["node", h, "file://" + mjs], capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout.strip().splitlines()[-1])

    def test_marker_first_line(self):
        with open(self.plugin, encoding="utf-8") as f:
            self.assertEqual(f.readline().rstrip("\n"), "// sheltie-opencode-plugin")

    def test_asked_replied_turns(self):
        out = self.run_harness()
        got = {c["path"]["permissionID"]: (c["path"]["id"], c["body"]["response"]) for c in out["calls"]}
        sid = "ses_df72331a4fferjkvc85gV6PDCf"
        self.assertEqual(got, {"per_once": (sid, "once"), "per_always": (sid, "always"), "per_deny": (sid, "reject")})
        with open(self.log) as f:
            recs = [json.loads(line) for line in f]
        self.assertTrue(all(r["argv"] == ["hook", "opencode"] for r in recs))
        bodies = [r["body"] for r in recs]
        evs = [b["hook_event_name"] for b in bodies]
        self.assertEqual(evs.count("SessionStart"), 1)       # 하위 세션(parentID)은 세션을 만들지 않는다
        self.assertEqual(evs.count("UserPromptSubmit"), 1)   # busy가 두 번 와도 한 번
        self.assertEqual(evs.count("Stop"), 1)               # idle이 두 번 와도 한 번
        self.assertEqual(evs.count("PermissionRequest"), 5)
        perm = next(b for b in bodies if b.get("request_id") == "per_always")
        self.assertEqual(perm, {"hook_event_name": "PermissionRequest", "session_id": sid,
                                "cwd": "/Users/me/work/demo",
                                "opencode_db": os.path.join(self.tmp, ".local", "share", "opencode", "opencode.db"),
                                "tool_name": "bash", "tool_input": {"patterns": ["touch x"],
                                                                    "metadata": {"command": "touch x"}},
                                "always": ["touch *"], "request_id": "per_always"})
        child = next(b for b in bodies if b.get("request_id") == "per_child")
        self.assertEqual(child["session_id"], sid)  # 하위 세션 요청은 최상위 세션 카드로(답은 하위 세션 id로)
        self.assertTrue(all(b["session_id"] == sid for b in bodies))
        stop = next(b for b in bodies if b["hook_event_name"] == "Stop")
        self.assertTrue(stop.get("turn_id"))
        self.assertLess(out["ms"], 25000)  # per_hold(30초 붙잡기)는 replied에서 끊겼다
        # 끝날 때 SessionEnd
        self.assertEqual(bodies[-1]["hook_event_name"], "SessionEnd")


if __name__ == "__main__":
    unittest.main()
