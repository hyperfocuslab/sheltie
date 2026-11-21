"""Cursor Agent CLI 지원. 화면은 실제 cursor-agent 2026.11.19-f005e9d를 hc 안에서(실제 계정, 모델 auto) 캡처한 것(200열).
Cursor는 OSC 7501을 안 보내고 제목도 대화 제목으로 고정이라 서버가 화면으로 막힘·작업·끝남을 판정한다.
승인 메뉴는 번호 없이 괄호 안 글자 키(y·tab·n)로 고르고, 계획 승인은 숫자 키가 바로 제출한다.
대화 기록은 SQLite(~/.cursor/chats/<md5(cwd)>/<chatId>/store.db)다."""
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_herdr_remote import FIX, ev, fixture, make_app, hr  # noqa: E402

V = "2026.11.19"
CWD = "/Users/me/work/demo"
CHAT = "4f125805-2702-4582-b895-b77d435f6df3"
ARGV = ["/Users/me/.local/bin/cursor-agent", "--disable-sigusr1", "--use-system-ca",
        "/Users/me/.local/share/cursor-agent/versions/2026.11.19-f005e9d/index.js", "--model", "auto"]


def cu(name):
    return fixture("cursor-%s-%s.txt" % (name, V))


def card(name):
    return hr.parse_prompt(cu(name), agent="cursor")


def labels(p):
    return [o["label"] for o in p["options"]]


class CursorAgentKindTests(unittest.TestCase):
    def test_detects_cursor(self):
        self.assertEqual(hr.agent_kind({"foreground": {"argv": ARGV, "name": "node"}}), "cursor")
        self.assertEqual(hr.agent_kind({"agent": "cursor-agent"}), "cursor")
        self.assertEqual(hr.agent_kind({"foreground": {"argv": ["node", ARGV[3], "--model", "auto"]}}), "cursor")
        # `agent`는 다른 CLI(Grok 링크 등)와 이름이 겹쳐 보지 않는다
        self.assertIsNone(hr.agent_kind({"foreground": {"argv": ["/Users/me/.local/bin/agent"]}}))
        self.assertIsNone(hr.agent_kind({"foreground": {"argv": ["node", "/x/other/index.js"]}}))
        self.assertEqual(hr.agent_kind({"agent": "grok"}), "grok")
        self.assertEqual(hr.agent_kind({"agent": "opencode"}), "opencode")


class CursorParseTests(unittest.TestCase):
    def test_shell_approval(self):
        p = card("exec")
        self.assertEqual((p["kind"], p["agent"], p["title"], p["question"]),
                         ("approval", "cursor", "Shell", "Run this command?"))
        self.assertEqual(p["body"], "touch notes.txt in .\n\nNot in allowlist: touch")
        # "Run Everything"(shift+tab)은 저장 방식을 몰라 카드에서 뺀다
        self.assertEqual(labels(p), ["Run (once)", "Add Shell(touch) to allowlist?",
                                     "Skip & tell the agent what to do instead"])
        self.assertEqual([o["number"] for o in p["options"]], [None, None, None])
        self.assertEqual(p["option_keys"], ["y", "tab", "n"])
        # allowlist는 전역 cli-config.json에 영구 저장: always가 아니라 persist. 거절 줄은 "대신 할 말" 입력 항목
        self.assertEqual((p["selected"], p["approve_index"], p["always_index"], p["reject_index"],
                          p["persist_index"], p["free_text_index"]), (0, 0, None, None, 1, 2))
        self.assertEqual(hr.push_category("blocked", {}, p), "HR_PERMISSION")
        self.assertEqual(card("exec2")["id"], p["id"])  # 같은 명령, 위 대화가 달라도 같은 카드

    def test_write_outside_approval(self):
        p = card("write-outside")
        self.assertEqual((p["title"], p["question"]), ("Write", "Write to this file?"))
        self.assertEqual(p["body"], "/Users/me/work/cursor-outside.txt\n\nhi")
        self.assertEqual(labels(p), ["Proceed", "Reject & propose changes", "Add Write(/Users/...) to allowlist?"])
        self.assertEqual(p["option_keys"], ["y", "n", "tab"])
        self.assertEqual((p["approve_index"], p["always_index"], p["reject_index"], p["persist_index"],
                          p["free_text_index"]), (0, None, None, 2, 1))

    def test_switch_mode_approval(self):
        p = card("switchmode")
        self.assertEqual((p["title"], p["question"], p["body"]),
                         ("SwitchMode", "Switch to Plan mode?", "User requested switching to Plan mode."))
        self.assertEqual(labels(p), ["Approve mode switch", "Reject"])
        self.assertEqual(p["option_keys"], ["y", "n"])
        self.assertEqual((p["approve_index"], p["reject_index"], p["free_text_index"]), (0, 1, None))

    def test_plan(self):
        p = card("plan")
        self.assertEqual((p["kind"], p["title"], p["question"]),
                         ("plan", "Add greet function-4f125805.plan.md", "Ready to build?"))
        self.assertTrue(p["body"].startswith("Add greet(name) to hello.py\n\nUpdate hello.py (hello.py) only."))
        self.assertTrue(p["body"].endswith("3. Skip docstring and tests."))
        self.assertNotIn("Saved to", p["body"])
        self.assertEqual(labels(p), ["Yes, build locally", "Yes, build in cloud", "No, propose changes"])
        self.assertEqual([o["number"] for o in p["options"]], [1, 2, 3])
        self.assertEqual((p["selected"], p["reject_index"], p["free_text_index"]), (0, None, 2))
        self.assertEqual(hr.push_category("blocked", {}, p), "HR_BLOCKED")
        r = card("plan-review")  # /plan으로 다시 띄운 박스(todos 앞부분이 같이 보인다)
        self.assertEqual(r["kind"], "plan")
        self.assertTrue(r["body"].startswith("todos:"))
        self.assertNotEqual(r["id"], p["id"])

    def test_trust(self):
        p = card("trust")
        self.assertEqual((p["kind"], p["title"], p["question"], p["body"]),
                         ("approval", "Workspace Trust Required", "Do you trust the contents of this directory?",
                          "/Users/me/work/demo"))
        self.assertEqual(labels(p), ["Trust this workspace", "Quit"])
        self.assertEqual(p["option_keys"], ["a", "q"])
        self.assertEqual((p["selected"], p["approve_index"], p["reject_index"]), (0, 0, 1))

    def test_unknown_screens_have_no_card(self):
        # 평소·작업 중·끝남, 폴더 안 쓰기(승인 없음), 거절 뒤 입력칸, 계획 거절·수정 뒤, 모드 전환 뒤, 본문 질문
        for name in ("idle", "working", "write", "edit", "exec-skip", "exec-skip-reply", "exec2-allowlisted",
                     "write-outside-reject", "write-outside-done", "plan-mode-idle", "question-inline",
                     "plan-key3", "plan2", "plan-built", "switchmode-done"):
            self.assertIsNone(card(name), name)

    def test_parsers_do_not_cross(self):
        for name in ("exec", "write-outside", "switchmode", "plan", "trust"):
            for agent in (None, "codex", "gemini", "copilot", "qwen", "opencode", "grok"):
                self.assertIsNone(hr.parse_prompt(cu(name), agent=agent), (name, agent))
        for f in ("claude-bash-2.1.329.txt", "codex-exec-0.171.0.txt", "gemini-exec-0.70.0.txt",
                  "gemini-question-0.70.0.txt", "copilot-exec-1.0.114.txt", "copilot-question-1.0.114.txt",
                  "qwen-exec-0.28.3.txt", "qwen-question-0.28.3.txt", "opencode-exec-1.19.6.txt",
                  "opencode-question-1.19.6.txt", "grok-exec-1.0.77.txt", "grok-plan-1.0.77.txt"):
            self.assertIsNone(hr.parse_prompt(fixture(f), agent="cursor"), f)

    def test_answer_steps(self):
        p = card("exec")
        self.assertEqual(hr.answer_steps(p, option_index=0), [("keys", ["y"])])
        self.assertEqual(hr.answer_steps(p, option_index=1), [("keys", ["tab"])])
        # 거절 줄 n → "대신 할 말" 입력칸 → 붙여넣기+enter(모델이 "Rejected: <글>"로 받는다, 실측)
        self.assertEqual(hr.answer_steps(p, text="do not run"), [("keys", ["n"]), ("text", "do not run")])
        with self.assertRaises(hr.ApiError):
            hr.answer_steps(p, option_index=2)  # 입력 항목은 글이 필요하다
        w = card("write-outside")
        self.assertEqual(hr.answer_steps(w, option_index=0), [("keys", ["y"])])
        self.assertEqual(hr.answer_steps(w, option_index=2), [("keys", ["tab"])])
        self.assertEqual(hr.answer_steps(w, text="use ./x"), [("keys", ["n"]), ("text", "use ./x")])
        s = card("switchmode")
        self.assertEqual(hr.answer_steps(s, option_index=1), [("keys", ["n"])])
        pl = card("plan")
        self.assertEqual(hr.answer_steps(pl, option_index=0), [("keys", ["1"])])
        self.assertEqual(hr.answer_steps(pl, option_index=1), [("keys", ["2"])])
        self.assertEqual(hr.answer_steps(pl, text="add tests"), [("keys", ["3"]), ("text", "add tests")])
        t = card("trust")
        self.assertEqual(hr.answer_steps(t, option_index=0), [("keys", ["a"])])
        self.assertEqual(hr.answer_steps(t, option_index=1), [("keys", ["q"])])
        with self.assertRaises(hr.ApiError):
            hr.answer_steps(s, text="no")


class CursorScreenStatusTests(unittest.TestCase):
    """hc status·제목 없이 화면으로 막힘·작업·끝남 알림."""

    def setUp(self):
        self.app, self.hc, self.pusher = make_app()
        self.later = []
        self.app._later = lambda sec, fn: self.later.append((sec, fn))
        info = self.hc.add("c1", None, agent=None, name="cu", turn=0, argv=ARGV)
        info["title"] = "Cursor Agent"
        self.hc.screens["c1"] = cu("idle")
        self.hc.add("sh", None, agent=None, argv=["/bin/zsh"])
        self.app.poll(initial=True)

    def screen(self, name):
        self.hc.screens["c1"] = cu(name)
        self.app.screen_poll()

    def settle(self):
        later, self.later = self.later, []
        for _, fn in later:
            fn()

    def err(self, fn, *a):
        with self.assertRaises(hr.ApiError) as cm:
            fn(*a)
        return cm.exception.code, cm.exception.body["error"]

    def states(self):
        return [(x["state"], x["category"], x["kind"]) for x in self.pusher.payloads]

    def by(self):
        return {t["id"]: t for t in self.app.api_terminals()["terminals"]}

    def test_idle_sends_nothing(self):
        self.screen("idle")
        self.assertEqual(self.pusher.payloads, [])
        t = self.by()["c1"]
        self.assertEqual((t["claude"], t["agent_kind"], t["status"]["state"]), (True, "cursor", "idle"))

    def test_working_then_done(self):
        self.screen("working")
        self.assertEqual(self.by()["c1"]["status"]["state"], "working")
        self.screen("write")
        self.screen("write")
        self.assertEqual([x["state"] for x in self.pusher.payloads], ["done"])

    def test_shell_push_card_and_answers(self):
        self.screen("working")
        self.screen("exec")
        self.assertEqual(self.states(), [("blocked", "HR_PERMISSION", "approval")])
        pid = card("exec")["id"]
        self.assertEqual(self.pusher.payloads[0]["prompt_id"], pid)
        self.assertEqual(self.by()["c1"]["status"]["msg"], "approve Shell")
        self.assertEqual(self.app.api_answer("c1", {"action": "approve", "prompt_id": pid})["keys"], ["y"])
        # 거절 버튼: n → 빈 "대신 할 말" 칸에 enter("empty to skip")
        self.assertEqual(self.app.api_answer("c1", {"action": "reject", "prompt_id": pid})["keys"], ["n", "enter"])
        self.assertEqual(self.err(self.app.api_answer, "c1", {"action": "always", "prompt_id": pid}),
                         (409, "no_always"))
        # prompt_id 없는 옛 경로: 숫자 "1"·esc가 아니라 지금 화면 카드의 글자 키
        self.assertEqual(self.app.api_answer("c1", {"action": "approve"})["keys"], ["y"])
        self.assertEqual(self.app.api_answer("c1", {"action": "reject"})["keys"], ["n", "enter"])
        self.app.api_prompt_answer("c1", {"prompt_id": pid, "option_index": 1})
        self.app.api_prompt_answer("c1", {"prompt_id": pid, "text": "Never mind. Reply SKIPPED."})
        self.assertEqual(self.hc.keys_calls, [("c1", ["y"]), ("c1", ["n", "enter"]), ("c1", ["y"]),
                                              ("c1", ["n", "enter"]), ("c1", ["tab"]), ("c1", ["n"])])
        self.assertEqual(self.hc.send_calls, [("c1", "Never mind. Reply SKIPPED.", "force")])
        # 같은 막힘이 이어지면(거절 입력칸 포함) 다시 알리지 않고, 끝나면 끝남 한 번
        self.screen("exec")
        self.screen("exec-skip")
        self.assertEqual(self.by()["c1"]["status"]["state"], "blocked")
        self.screen("exec-skip-reply")
        # 막힘 바로 뒤 끝남 장면은 답 직후 작업 표시 전일 수 있어 0.6초 뒤 한 번 더 본다(실측)
        self.assertEqual([x["state"] for x in self.pusher.payloads], ["blocked"])
        self.assertEqual([sec for sec, _ in self.later], [hr.SCREEN_SETTLE])
        self.settle()
        self.assertEqual([x["state"] for x in self.pusher.payloads], ["blocked", "done"])

    def test_short_task_sent_from_app_gets_done(self):
        # 3초 화면 판정 사이에 끝난 작업: 앱에서 보낸 순간을 작업 중으로 둬 다음 판정에서 끝남 알림(OpenCode와 같다)
        now = [50.0]
        self.app.clock = lambda: now[0]
        self.assertEqual(self.app.api_send("c1", {"text": "hi"}), {"ok": True})
        self.screen("idle")
        self.assertEqual(self.pusher.payloads, [])
        now[0] += hr.SEND_SETTLE
        self.screen("idle")
        self.assertEqual([x["state"] for x in self.pusher.payloads], ["done"])

    def test_transient_frame_after_answer_is_not_done(self):
        self.screen("exec")
        self.screen("exec-skip-reply")  # 답 직후 한 장면
        self.hc.screens["c1"] = cu("working")  # 0.6초 뒤에는 작업 중
        self.settle()
        self.screen("write")
        self.screen("write")
        self.assertEqual([x["state"] for x in self.pusher.payloads], ["blocked", "done"])

    def test_trust_then_idle_is_not_done(self):
        self.screen("trust")
        self.screen("idle")
        self.screen("idle")
        self.assertEqual([x["state"] for x in self.pusher.payloads], ["blocked"])
        self.assertEqual(self.by()["c1"]["status"]["state"], "idle")
        self.screen("working")
        self.screen("write")
        self.assertEqual([x["state"] for x in self.pusher.payloads], ["blocked", "done"])

    def test_write_and_switch_cards(self):
        self.screen("write-outside")
        p = self.app.api_prompt("c1")["prompt"]
        self.assertEqual(p["title"], "Write")
        self.assertEqual(self.app.api_answer("c1", {"action": "approve", "prompt_id": p["id"]})["keys"], ["y"])
        self.assertEqual(self.app.api_answer("c1", {"action": "reject", "prompt_id": p["id"]})["keys"], ["n", "enter"])
        self.screen("working")
        self.screen("switchmode")
        s = self.app.api_prompt("c1")["prompt"]
        self.assertEqual(s["title"], "SwitchMode")
        self.assertEqual(self.app.api_answer("c1", {"action": "reject", "prompt_id": s["id"]})["keys"], ["n"])
        self.assertEqual(self.app.api_answer("c1", {"action": "approve"})["keys"], ["y"])
        self.assertEqual(self.app.api_answer("c1", {"action": "reject"})["keys"], ["n"])
        self.assertEqual([x["category"] for x in self.pusher.payloads if x["state"] == "blocked"],
                         ["HR_PERMISSION", "HR_PERMISSION"])

    def test_plan_card(self):
        self.screen("working")
        self.screen("plan")
        self.assertEqual(self.states(), [("blocked", "HR_BLOCKED", "plan")])
        p = self.app.api_prompt("c1")["prompt"]
        self.assertEqual(self.err(self.app.api_answer, "c1", {"action": "approve", "prompt_id": p["id"]}),
                         (409, "needs_manual"))
        # 글 없는 거절이 없다(3 → 빈 칸 enter는 무시, esc는 메뉴로, 실측): 수정 글로 답한다
        self.assertEqual(self.err(self.app.api_answer, "c1", {"action": "reject", "prompt_id": p["id"]}),
                         (409, "needs_manual"))
        self.assertEqual(self.err(self.app.api_answer, "c1", {"action": "approve"}), (409, "needs_manual"))
        self.assertEqual(self.err(self.app.api_answer, "c1", {"action": "reject"}), (409, "needs_manual"))
        self.app.api_prompt_answer("c1", {"prompt_id": p["id"], "option_index": 0})
        self.app.api_prompt_answer("c1", {"prompt_id": p["id"], "text": "Keep print as is"})
        self.assertEqual(self.hc.keys_calls, [("c1", ["1"]), ("c1", ["3"])])
        self.assertEqual(self.hc.send_calls, [("c1", "Keep print as is", "force")])
        # 3 뒤 수정 입력칸(카드 없음)은 같은 막힘, 수정 끝나면 끝남
        self.screen("plan-key3")
        self.screen("plan2")
        self.settle()
        self.assertEqual([x["state"] for x in self.pusher.payloads], ["blocked", "done"])

    def test_trust_card(self):
        self.screen("trust")
        self.assertEqual(self.states(), [("blocked", "HR_PERMISSION", "approval")])
        p = self.app.api_prompt("c1")["prompt"]
        self.assertEqual(self.app.api_answer("c1", {"action": "approve", "prompt_id": p["id"]})["keys"], ["a"])
        self.assertEqual(self.app.api_answer("c1", {"action": "reject", "prompt_id": p["id"]})["keys"], ["q"])
        self.assertEqual(self.app.api_answer("c1", {"action": "approve"})["keys"], ["a"])

    def test_dialog_without_card(self):
        # PC에서 거절 입력칸을 연 상태로 처음 보면 카드 없는 막힘. 카드 없는 답은 409(키를 보내지 않는다)
        self.screen("exec-skip")
        self.assertEqual([(s, c) for s, c, _ in self.states()], [("blocked", "HR_BLOCKED")])
        self.assertIsNone(self.pusher.payloads[0]["prompt_id"])
        self.assertIsNone(self.app.api_prompt("c1")["prompt"])
        self.assertEqual(self.err(self.app.api_answer, "c1", {"action": "approve"}), (409, "needs_manual"))
        self.assertEqual(self.err(self.app.api_answer, "c1", {"action": "reject"}), (409, "needs_manual"))
        self.assertEqual(self.hc.keys_calls, [])
        self.screen("write-outside-reject")
        self.screen("plan-key3")
        self.assertEqual(len(self.pusher.payloads), 1)

    def test_recheck_before_keys(self):
        self.screen("exec")
        pid = card("exec")["id"]
        self.hc.screens["c1"] = cu("write-outside")
        self.assertEqual(self.err(self.app.api_answer, "c1", {"action": "approve", "prompt_id": pid}),
                         (409, "prompt_changed"))
        self.hc.screens["c1"] = cu("exec2-allowlisted")
        self.assertIsNone(self.app.api_prompt("c1")["prompt"])
        self.assertEqual(self.err(self.app.api_answer, "c1", {"action": "approve"}), (409, "not_blocked"))
        self.assertEqual(self.hc.keys_calls, [])

    def test_send_refused_while_dialog_open(self):
        for name in ("exec", "exec-skip", "plan", "plan-key3", "switchmode"):
            self.screen(name)
            self.assertEqual(self.err(self.app.api_send, "c1", {"text": "hello"}), (409, "hc_refused"), name)
        self.screen("plan2")
        self.assertEqual(self.app.api_send("c1", {"text": "hello"}), {"ok": True})
        self.assertEqual(self.app.api_keys("c1", {"keys": ["esc"]}), {"ok": True})
        self.assertEqual(self.hc.send_calls, [("c1", "hello")])

    def test_title_event_refreshes(self):
        self.hc.screens["c1"] = cu("exec")
        self.app.handle_event_line(ev("c1", "title", {"title": "Shell Command Touch"}))
        self.assertEqual([x["category"] for x in self.pusher.payloads], ["HR_PERMISSION"])


def _pb_bytes(field, data):
    return bytes([(field << 3) | 2, len(data)]) + data


def build_store(path, jsonl):
    """capture 사본(root 순서 메시지 jsonl)으로 store.db 모양(blobs·meta, root protobuf field 1 = 메시지 id)을 만든다."""
    with open(os.path.join(FIX, jsonl), encoding="utf-8") as f:
        msgs = [json.loads(line) for line in f if line.strip()]
    con = sqlite3.connect(path)
    con.executescript("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB);"
                      "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);")
    root = b""
    for m in msgs:
        data = json.dumps(m).encode("utf-8")
        h = hashlib.sha256(data).digest()
        con.execute("INSERT OR IGNORE INTO blobs VALUES (?, ?)", (h.hex(), data))
        root += _pb_bytes(1, h)
    root += bytes([8 << 3, 7]) + _pb_bytes(15, b"/Users/me/work/demo/hello.py")  # 그 밖의 field는 건너뛴다
    rid = hashlib.sha256(root).hexdigest()
    con.execute("INSERT INTO blobs VALUES (?, ?)", (rid, root))
    meta = {"agentId": CHAT, "latestRootBlobId": rid, "name": "Shell Command Touch", "mode": "plan"}
    con.execute("INSERT INTO meta VALUES ('0', ?)", (json.dumps(meta).encode("utf-8").hex(),))
    con.commit()
    con.close()


def multi_first():
    """복수 선택이 첫 질문(마지막 아님)인 화면. 캡처는 마지막 질문(2/2)뿐이라 같은 박스에서 번호만 바꿔 만든다(키는 실측)."""
    return cu("question-multi-q2").replace("Question 2 of 2", "Question 1 of 2").replace(
        "2. Which features?", "1. Which features?")


class CursorQuestionParseTests(unittest.TestCase):
    """AskQuestion 박스(grok-4.7 모델, 실측 2026.11.19). 숫자 키 무시, ↑/↓ + enter(고르고 다음/제출), space 토글,
    tab 다음 질문, 끝에 늘 "Other: (type to answer)"."""

    def test_single_question(self):
        p = card("question-single")
        self.assertEqual((p["kind"], p["agent"], p["title"], p["question"], p["body"]),
                         ("question", "cursor", "Clarifying Questions", "Which language?", None))
        self.assertEqual(labels(p), ["Python", "Go", "Rust", "Other"])
        self.assertEqual([o["number"] for o in p["options"]], [None] * 4)
        self.assertEqual((p["selected"], p["multi_select"], p["checked"], p["free_text_index"], p["tabs"],
                          p["reject_index"], p["last_tab"]), (0, False, [], 3, None, None, True))
        self.assertEqual(hr.push_category("blocked", {}, p), "HR_BLOCKED")
        # PC에서 space로 Go를 골라 둔 화면: 커서 1. Other에 글을 넣으면 어느 쪽이 갈지 몰라 직접 입력 없음
        s = card("question-single-space")
        self.assertEqual((labels(s), s["selected"], s["free_text_index"], s["checked"]),
                         (["Python", "Go", "Rust"], 1, None, []))

    def test_multi_questions(self):
        q1 = card("question-multi-q1")
        self.assertEqual((q1["title"], q1["question"], labels(q1), q1["free_text_index"], q1["last_tab"]),
                         ("Setup 1/2", "Which database?", ["SQLite", "Postgres", "Other"], 2, False))
        m = hr.parse_prompt(multi_first(), agent="cursor")
        self.assertEqual((m["title"], m["question"], labels(m), m["multi_select"], m["checked"],
                          m["free_text_index"], m["selected"], m["last_tab"]),
                         ("Setup 1/2", "Which features?", ["Auth", "Logging", "Cache"], True, [], None, 0, False))
        checked = hr.parse_prompt(multi_first().replace("› [ ] Auth", "› [x] Auth"), agent="cursor")
        self.assertEqual((checked["checked"], checked["id"]), ([0], m["id"]))

    def test_no_card(self):
        # 마지막 질문의 복수 선택(enter 한 번에 제출이 안 된 적이 있다, 원인 미확인), PC에서 Other에 글을 넣은 화면,
        # 답한 뒤 기록
        for name in ("question-multi-q2", "question-multi-q2-selected", "question-multi-q2-enter1",
                     "question-other-typed", "question-single-answered"):
            self.assertIsNone(card(name), name)
        # 커서가 없거나 질문 번호가 안 맞는 박스
        self.assertIsNone(hr.parse_prompt(cu("question-single").replace("›", " "), agent="cursor"))
        self.assertIsNone(hr.parse_prompt(cu("question-single").replace("1. Which", "2. Which"), agent="cursor"))

    def test_answer_steps(self):
        p = card("question-single")
        # 숫자 키 무시: 커서 이동 + enter(커서 항목을 고르고 제출, 실측)
        self.assertEqual(hr.answer_steps(p, option_index=0), [("keys", ["enter"])])
        self.assertEqual(hr.answer_steps(p, option_index=2), [("keys", ["down", "down", "enter"])])
        # Other: 커서를 옮긴 뒤 붙여넣기+enter(hc send --force, 실측)
        self.assertEqual(hr.answer_steps(p, text="Zig"), [("keys", ["down", "down", "down"]), ("text", "Zig")])
        with self.assertRaises(hr.ApiError):
            hr.answer_steps(p, option_index=3)  # Other는 글이 필요하다
        s = card("question-single-space")
        self.assertEqual(hr.answer_steps(s, option_index=0), [("keys", ["up", "enter"])])
        with self.assertRaises(hr.ApiError):
            hr.answer_steps(s, text="Zig")
        q1 = card("question-multi-q1")
        self.assertEqual(hr.answer_steps(q1, option_index=1), [("keys", ["down", "enter"])])
        self.assertEqual(hr.answer_steps(q1, text="DuckDB embedded"), [("keys", ["down", "down"]),
                                                                       ("text", "DuckDB embedded")])
        # 복수 선택: ↑/↓ + space 토글, tab = 다음 질문(enter는 커서 항목까지 고른다)
        m = hr.parse_prompt(multi_first(), agent="cursor")
        self.assertEqual(hr.answer_steps(m, option_indices=[2, 0]), [("keys", ["space", "down", "down", "space", "tab"])])
        self.assertEqual(hr.answer_steps(m, option_indices=[]), [("keys", ["tab"])])
        c = hr.parse_prompt(multi_first().replace("› [ ] Auth", "› [x] Auth"), agent="cursor")
        self.assertEqual(hr.answer_steps(c, option_indices=[1]), [("keys", ["space", "down", "space", "tab"])])
        for bad in ({"text": "x"}, {"option_index": 0}, {"option_indices": [3]}):
            with self.assertRaises(hr.ApiError):
                hr.answer_steps(m, **bad)

    def test_dialog_group(self):
        g = hr._cursor_dialog_group
        self.assertEqual(g(cu("question-multi-q1")), ("cursor-questions", "Setup", 2))
        self.assertEqual(g(cu("question-multi-q2-selected")), g(cu("question-multi-q1")))
        for name in ("question-single", "exec", "plan", "idle"):
            self.assertIsNone(g(cu(name)), name)

    def test_parsers_do_not_cross(self):
        for name in ("question-single", "question-multi-q1"):
            for agent in (None, "codex", "gemini", "copilot", "qwen", "opencode", "grok", "amp"):
                self.assertIsNone(hr.parse_prompt(cu(name), agent=agent), (name, agent))
        for f in ("gemini-question-0.70.0.txt", "copilot-question-1.0.114.txt", "qwen-question-0.28.3.txt",
                  "opencode-question-1.19.6.txt", "claude-question-multi-2.1.329.txt"):
            self.assertIsNone(hr.parse_prompt(fixture(f), agent="cursor"), f)


class CursorQuestionApiTests(unittest.TestCase):
    """질문 박스 막힘·카드·답(화면 판정 "full"). 계정 한도로 실제 답은 못 해 봤다(fixture 기반)."""
    setUp, screen, settle, err, states, by = (CursorScreenStatusTests.setUp, CursorScreenStatusTests.screen,
                                              CursorScreenStatusTests.settle, CursorScreenStatusTests.err,
                                              CursorScreenStatusTests.states, CursorScreenStatusTests.by)

    def test_single_question_push_and_answers(self):
        self.screen("working")
        self.screen("question-single")
        p = card("question-single")
        self.assertEqual([(x["state"], x["category"], x["kind"], x["prompt_id"]) for x in self.pusher.payloads],
                         [("blocked", "HR_BLOCKED", "question", p["id"])])
        self.assertEqual(self.app.api_prompt("c1")["prompt"]["id"], p["id"])
        self.app.api_prompt_answer("c1", {"prompt_id": p["id"], "option_index": 1})
        self.app.api_prompt_answer("c1", {"prompt_id": p["id"], "text": "Zig"})
        self.assertEqual(self.hc.keys_calls, [("c1", ["down", "enter"]), ("c1", ["down", "down", "down"])])
        self.assertEqual(self.hc.send_calls, [("c1", "Zig", "force")])
        # 승인·거절 버튼 없음(esc 건너뛰기는 실측 못 했다)
        self.assertEqual(self.err(self.app.api_answer, "c1", {"action": "reject", "prompt_id": p["id"]}),
                         (409, "needs_manual"))
        self.assertEqual(self.err(self.app.api_answer, "c1", {"action": "reject"}), (409, "needs_manual"))
        self.assertEqual(self.err(self.app.api_answer, "c1", {"action": "approve"}), (409, "needs_manual"))
        self.assertEqual(self.err(self.app.api_send, "c1", {"text": "hi"}), (409, "hc_refused"))

    def test_recheck_before_keys(self):
        self.screen("question-single")
        pid = card("question-single")["id"]
        self.hc.screens["c1"] = cu("question-single-space")  # PC에서 커서를 옮기고 골랐다
        self.assertEqual(self.err(self.app.api_prompt_answer, "c1", {"prompt_id": pid, "option_index": 0}),
                         (409, "prompt_changed"))
        self.assertEqual(self.hc.keys_calls, [])

    def test_multi_questions_one_push(self):
        self.screen("working")
        self.screen("question-multi-q1")
        p1 = card("question-multi-q1")
        self.app.api_prompt_answer("c1", {"prompt_id": p1["id"], "text": "DuckDB embedded"})
        # 다음 질문(마지막 복수 선택: 카드 없음)도 같은 박스: 새 알림 없음, 막힘 유지
        self.screen("question-multi-q2")
        self.assertIsNone(self.app.api_prompt("c1")["prompt"])
        self.assertEqual(self.by()["c1"]["status"]["state"], "blocked")
        self.screen("question-multi-q2-selected")
        self.assertEqual(self.err(self.app.api_prompt_answer, "c1", {"prompt_id": p1["id"], "option_index": 0}),
                         (409, "prompt_changed"))
        self.screen("question-multi-q1")  # PC에서 ←로 돌아감
        self.assertEqual([x["state"] for x in self.pusher.payloads], ["blocked"])
        self.screen("question-single-answered")
        self.settle()
        self.assertEqual([x["state"] for x in self.pusher.payloads], ["blocked", "done"])

    def test_multi_select_card_answer(self):
        self.hc.screens["c1"] = multi_first()
        self.app.screen_poll()
        m = self.app.api_prompt("c1")["prompt"]
        self.assertTrue(m["multi_select"])
        self.app.api_prompt_answer("c1", {"prompt_id": m["id"], "option_indices": [0, 2]})
        self.assertEqual(self.hc.keys_calls, [("c1", ["space", "down", "down", "space", "tab"])])

    def test_last_multi_select_without_card(self):
        # 질문 하나짜리 복수 선택도 마지막 질문이라 카드 없음: 막힘 알림만
        self.screen("question-multi-q2")
        self.assertEqual([(s, c) for s, c, _ in self.states()], [("blocked", "HR_BLOCKED")])
        self.assertIsNone(self.pusher.payloads[0]["prompt_id"])
        self.assertEqual(self.err(self.app.api_answer, "c1", {"action": "approve"}), (409, "needs_manual"))


class CursorTranscriptTests(unittest.TestCase):
    def setUp(self):
        self.home = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)
        self.chats = os.path.join(self.home, ".cursor", "chats", hashlib.md5(CWD.encode()).hexdigest())
        d = os.path.join(self.chats, CHAT)
        os.makedirs(d)
        self.db = os.path.join(d, "store.db")
        build_store(self.db, "cursor-session-store-%s.jsonl" % V)

    def test_parse(self):
        turns, cut = hr.read_cursor_transcript(self.db, CHAT, 200)
        self.assertFalse(cut)
        users = [p["text"] for t in turns if t["role"] == "user" for p in t["parts"]]
        # user_info·system_reminder·첨부는 숨기고 <user_query> 안 글만
        self.assertEqual(users[0], "Run the shell command: touch notes.txt")
        self.assertEqual(users[1], "OK now actually run: touch notes.txt")
        self.assertIn("A, type hints only. Write the plan now (short, 3 steps max).", users)
        self.assertFalse(any("<" in u.split("\n")[0] for u in users))
        self.assertEqual(turns[1]["role"], "assistant")
        tools = [p for t in turns for p in t["parts"] if p["kind"] == "tool"]
        self.assertEqual((tools[0]["name"], tools[0]["summary"], tools[0]["state"]),
                         ("Shell", "touch notes.txt", "error"))
        self.assertEqual(tools[0]["output"], "Rejected: Never mind, do not run anything. Just reply with the word SKIPPED.")
        self.assertEqual((tools[1]["name"], tools[1]["state"]), ("Shell", "ok"))
        self.assertTrue(tools[1]["output"].startswith("Exit code: 0"))
        names = [t["name"] for t in tools]
        self.assertIn("Write", names)
        self.assertIn("StrReplace", names)
        self.assertIn("CreatePlan", names)
        # CallDynamicTool은 실제 도구 이름(TodoWrite·SwitchMode)으로
        self.assertIn("SwitchMode", names)
        self.assertNotIn("CallDynamicTool", names)
        self.assertTrue(all(t["state"] in ("ok", "error") for t in tools))
        texts = [p["text"] for p in turns[-1]["parts"] if p["kind"] == "text"]
        self.assertEqual(texts[-1], "Switched to Plan mode.")
        roles = [t["role"] for t in turns]
        self.assertTrue(all(a != b for a, b in zip(roles, roles[1:])))

    def test_limit(self):
        turns, cut = hr.read_cursor_transcript(self.db, CHAT, 3)
        self.assertTrue(cut)
        self.assertEqual(len(turns), 3)
        turns, cut = hr.read_cursor_transcript(self.db, CHAT, 50, max_messages=4)
        self.assertTrue(cut)
        self.assertEqual(turns[-1]["parts"][-1]["text"], "Switched to Plan mode.")

    def test_find_and_api(self):
        info = {"foreground": {"argv": ARGV, "pid": 0}, "cwd": CWD}
        self.assertEqual(hr.find_agent_transcript(info, self.home), (CHAT, self.db))
        self.assertEqual(hr.find_agent_transcript(dict(info, cwd="/nope"), self.home), (None, None))
        # 열린 store.db(lsof)가 있으면 그 대화
        other = "0a0b0c0d-1111-2222-3333-444455556666"
        od = os.path.join(self.chats, other)
        os.makedirs(od)
        shutil.copy(self.db, os.path.join(od, "store.db"))
        os.utime(self.db, (2000000000, 2000000000))

        def lsof(argv, **kw):
            return subprocess.CompletedProcess(argv, 0, "p77\nn%s\nn%s-wal\n" % (os.path.join(od, "store.db"),
                                                                                os.path.join(od, "store.db")), "")
        pinfo = {"foreground": {"argv": ARGV, "pid": 77}, "cwd": CWD}
        self.assertEqual(hr.find_agent_transcript(info, self.home), (CHAT, self.db))  # 가장 최근
        self.assertEqual(hr.find_agent_transcript(pinfo, self.home, runner=lsof),
                         (other, os.path.join(od, "store.db")))
        app, hc, _ = make_app(home=self.home)
        i = hc.add("c1", None, agent=None, argv=ARGV)
        i["cwd"] = CWD
        i["foreground"]["pid"] = 0
        hc.screens["c1"] = cu("idle")
        r = app.api_transcript("c1", 50)
        self.assertEqual((r["session_id"], r["truncated"]), (CHAT, False))
        self.assertEqual(r["turns"][0]["parts"][0]["text"], "Run the shell command: touch notes.txt")
        self.assertEqual(self.by(app)["c1"]["preview"], "Switched to Plan mode.")

    @staticmethod
    def by(app):
        return {t["id"]: t for t in app.api_terminals()["terminals"]}

    def test_db_is_not_modified_and_bad_input(self):
        before = os.stat(self.db).st_mtime_ns
        hr.read_cursor_transcript(self.db, CHAT, 50)
        hr.cursor_preview(self.db, CHAT)
        self.assertEqual(os.stat(self.db).st_mtime_ns, before)
        self.assertEqual(hr.read_cursor_transcript(self.db, "../x", 50), ([], False))
        self.assertEqual(hr.read_cursor_transcript(os.path.join(self.home, "nope.db"), CHAT, 50), ([], False))
        con = sqlite3.connect(self.db)
        con.execute("UPDATE meta SET value = 'zz' WHERE key = '0'")
        con.commit()
        con.close()
        self.assertEqual(hr.read_cursor_transcript(self.db, CHAT, 50), ([], False))

    def test_protobuf_walk(self):
        self.assertEqual(hr._pb_fields(_pb_bytes(1, b"ab") + bytes([8 << 3, 0x96, 0x01])),
                         [(1, 2, b"ab"), (8, 0, 150)])
        self.assertEqual(hr._pb_fields(bytes([0x0a, 0x05, 0x61])), [])  # 길이가 넘친다


if __name__ == "__main__":
    unittest.main()
