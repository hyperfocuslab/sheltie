"""훅 백엔드(herdr 없이): /hook/<agent> 붙잡기, 폰 답 → 훅 결정, guard, hook 하위 명령.

fixture: tests/fixtures/hooks/ (실측 *-2.1.329·*-0.171.0, 문서 기준 *-doc. 경로는 가짜로 바꿨다)."""
import io
import json
import logging
import os
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
import test_herdr_remote as base  # noqa: E402

hr = base.hr
TOKEN = base.TOKEN
HOOK_FIX = os.path.join(base.FIX, "hooks")
SERVER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "herdr_remote.py")


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
        self.assertEqual(p["options"][1]["label"], "Yes, and don't ask again for Bash(npm test)")
        self.assertEqual(self.pusher.payloads[-1]["aps"]["category"], "HR_PERMISSION_ALWAYS")
        self.assertEqual(self.answer(tid, {"action": "always"})[1]["error"], "needs_prompt_id")
        self.assertEqual(self.answer(tid, {"action": "always", "prompt_id": p["id"]})[0], 200)
        self.assertEqual(self.result(box)[1], {"hookSpecificOutput": {
            "hookEventName": "PermissionRequest", "decision": {"behavior": "allow", "updatedPermissions": [
                {"type": "addRules", "rules": [{"toolName": "Bash", "ruleContent": "npm test"}],
                 "behavior": "allow", "destination": "localSettings"}]}}})

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
        # UserPromptSubmit 없이 다음 턴의 Stop만 와도(turn_id가 다르다) 끝남을 다시 알린다. 같은 turn_id는 한 번
        self.hook("codex", load("codex-stop-doc.json", turn_id="01a1fd04-0000-7000-8000-000000000002"))
        self.hook("codex", load("codex-stop-doc.json", turn_id="01a1fd04-0000-7000-8000-000000000002"))
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
                with self.assertLogs("herdr-remote", level="WARNING") as cm:
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
        with mock.patch.dict(os.environ, {"HERDR_REMOTE_CONFIG": self.cfg}):
            rc = hr.cmd_hook([agent], stdin=io.BytesIO(json.dumps(body).encode()), stdout=out,
                             env=env if env is not None else {})
        return rc, out.getvalue()

    def test_inside_herdr_prints_nothing(self):
        rc, out = self.run_cmd("claude", load("claude-session-start-2.1.329.json"), env={"HC_TERMINAL_ID": "t3"})
        self.assertEqual((rc, out), (0, b""))
        self.assertEqual(self.app.hooks.sessions, {})

    def test_server_down_prints_nothing(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        self.write_cfg(port)
        rc, out = self.run_cmd("claude", load("claude-permission-bash-2.1.329.json"))
        self.assertEqual((rc, out), (0, b""))
        with mock.patch.dict(os.environ, {"HERDR_REMOTE_CONFIG": os.path.join(self.tmp, "none.json")}):
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
        log_path = os.path.join(self.tmp, "logs", "herdr-remote.log")
        env = dict(os.environ, HERDR_REMOTE_CONFIG=self.cfg, HERDR_REMOTE_LOG=log_path)
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


if __name__ == "__main__":
    unittest.main()
