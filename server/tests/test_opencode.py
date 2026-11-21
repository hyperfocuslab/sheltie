"""OpenCode 지원. 화면은 실제 opencode 1.19.6을 hc 안에서(가짜 모델 서버로) 캡처한 것(194열, 오른쪽 사이드바 있음).
OpenCode는 OSC 7501을 안 보내고 제목도 "OC | <세션 제목>"으로 고정이라 서버가 화면으로 막힘·작업·끝남을 판정한다.
승인 버튼은 번호가 없고 커서가 돌며 지금 고른 버튼이 화면 글자로 안 보여, 뜰 때 자리(Allow once) 기준 키로 답한다.
대화 기록은 SQLite(opencode.db)다."""
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_herdr_remote import FIX, ev, fixture, make_app, hr  # noqa: E402

V = "1.19.6"
CWD = "/Users/me/work/demo"


def oc(name):
    return fixture("opencode-%s-%s.txt" % (name, V))


def card(name):
    return hr.parse_prompt(oc(name), agent="opencode")


def labels(p):
    return [o["label"] for o in p["options"]]


class OpencodeAgentKindTests(unittest.TestCase):
    def test_detects_opencode(self):
        self.assertEqual(hr.agent_kind({"agent": "opencode"}), "opencode")
        self.assertEqual(hr.agent_kind({"foreground": {"argv": ["/opt/homebrew/bin/opencode"]}}), "opencode")
        self.assertIsNone(hr.agent_kind({"foreground": {"argv": ["opencode-helper"]}}))
        self.assertEqual(hr.agent_kind({"agent": "qwen"}), "qwen")
        self.assertEqual(hr.agent_kind({"agent": "gemini"}), "gemini")


class OpencodeParseTests(unittest.TestCase):
    def test_shell_approval(self):
        p = card("exec")
        self.assertEqual((p["kind"], p["agent"]), ("approval", "opencode"))
        self.assertEqual((p["title"], p["question"], p["body"]), ("Shell command", "Permission required",
                                                                  "$ touch notes.txt"))
        self.assertEqual(labels(p), ["Allow once", "Allow always", "Reject"])
        self.assertEqual([o["number"] for o in p["options"]], [None, None, None])
        # "Allow always"는 메모리에만(재시작 뒤 다시 묻는다): 세션 한정이라 always
        self.assertEqual((p["selected"], p["approve_index"], p["always_index"], p["reject_index"], p["persist_index"]),
                         (0, 0, 1, 2, None))
        self.assertEqual(hr.push_category("blocked", {}, p), "HR_PERMISSION_ALWAYS")
        self.assertEqual(card("sleep")["body"], "$ sleep 8")

    def test_file_approvals(self):
        p = card("write")
        self.assertEqual((p["title"], p["body"]), ("Edit hello.py", "1 + print('hello')"))
        e = card("edit")
        self.assertEqual(e["title"], "Edit README.md")
        self.assertTrue(e["body"].startswith("1 - # demo") and e["body"].endswith("1 + # demo project"))
        pw = card("plan-write")
        self.assertEqual(pw["title"], "Edit .opencode/plans/1795247430562-quiet-eagle.md")
        self.assertEqual(pw["body"], "1 + # Plan\n2 +\n3 + 1. Add hello.py\n4 + 2. Add tests")
        for x in (p, e, pw):
            self.assertEqual((x["always_index"], x["reject_index"]), (1, 2))

    def test_always_confirm(self):
        p = card("exec-always-confirm")
        self.assertEqual((p["kind"], p["title"]), ("approval", "Always allow"))
        self.assertEqual(p["question"], "This will allow the following patterns until OpenCode is restarted")
        self.assertEqual(p["body"], "- touch *")
        self.assertEqual(labels(p), ["Confirm", "Cancel"])
        self.assertEqual((p["approve_index"], p["always_index"], p["reject_index"]), (0, None, None))
        self.assertEqual(hr.push_category("blocked", {}, p), "HR_PERMISSION")
        q = card("edit-always-confirm")
        self.assertEqual((q["question"], q["body"]), ("This will allow edit until OpenCode is restarted.",) * 2)

    def test_question(self):
        p = card("question")
        self.assertEqual((p["kind"], p["title"]), ("question", None))
        self.assertEqual(p["question"], "Which language should the demo use?")
        # 사이드바(경로)가 같은 줄에 붙어 있어도 라벨에 안 섞인다
        self.assertEqual(labels(p), ["Python", "Go", "Rust", "Type your own answer"])
        self.assertEqual([o["description"] for o in p["options"]], ["Simple scripting", "Static binary",
                                                                    "Memory safe", None])
        self.assertEqual([o["number"] for o in p["options"]], [1, 2, 3, 4])
        self.assertEqual((p["selected"], p["free_text_index"]), (None, 3))
        t = card("question-text")
        self.assertEqual((t["question"], labels(t), t["free_text_index"]),
                         ("What should the project be called?", ["Type your own answer"], 0))

    def test_plan_exit(self):
        p = card("plan-exit")
        self.assertEqual(p["kind"], "plan")
        self.assertTrue(p["question"].startswith("Plan at .opencode/plans/1795247430562-quiet-eagle.md is complete."))
        self.assertEqual(labels(p), ["Yes", "No"])
        self.assertEqual(p["options"][1]["description"], "Stay with plan agent to continue refining the plan")
        self.assertEqual((p["reject_index"], p["free_text_index"]), (1, None))
        self.assertEqual(hr.push_category("blocked", {}, p), "HR_BLOCKED")

    def test_unknown_screens_have_no_card(self):
        # 평소 화면, 작업 중, PC에서 직접 입력 칸을 열었거나 글을 넣은 질문(질문 하나, 복수 선택 탭, 단일 선택 탭), 답한 뒤
        for name in ("idle-start", "hello2", "idle-plan", "working", "question-custom-typing", "question-multi-free-open",
                     "question-multi-free-filled", "question-multi-tab2-free-open", "question-dismissed", "write-reject",
                     "plan-exit-yes"):
            self.assertIsNone(card(name), name)

    def test_parsers_do_not_cross(self):
        for name in ("exec", "write", "exec-always-confirm", "question", "question-text", "plan-exit", "question-multi",
                     "question-multi-tab2", "question-multi-confirm", "question-multi-one", "question-multi3-q2"):
            for agent in (None, "codex", "gemini", "copilot", "qwen"):
                self.assertIsNone(hr.parse_prompt(oc(name), agent=agent), (name, agent))
        for f in ("claude-bash-2.1.329.txt", "codex-exec-0.171.0.txt", "gemini-exec-0.70.0.txt",
                  "gemini-question-0.70.0.txt", "copilot-exec-1.0.114.txt", "copilot-question-1.0.114.txt",
                  "qwen-exec-0.28.3.txt", "qwen-question-0.28.3.txt", "qwen-question-multi-0.28.3.txt",
                  "qwen-question-submit-0.28.3.txt", "claude-question-review-2.1.329.txt"):
            self.assertIsNone(hr.parse_prompt(fixture(f), agent="opencode"), f)

    def test_answer_steps(self):
        p = card("exec")
        # 숫자 키를 무시하고 커서가 돌아(←가 Allow once에서 Reject로 간다, 실측) 뜰 때 자리 기준
        self.assertEqual(hr.answer_steps(p, option_index=0), [("keys", ["enter"])])
        self.assertEqual(hr.answer_steps(p, option_index=1), [("keys", ["right", "enter", "enter"])])
        self.assertEqual(hr.answer_steps(p, option_index=2), [("keys", ["esc"])])
        c = card("exec-always-confirm")
        self.assertEqual(hr.answer_steps(c, option_index=0), [("keys", ["enter"])])
        self.assertEqual(hr.answer_steps(c, option_index=1), [("keys", ["esc"])])
        q = card("question")
        self.assertEqual(hr.answer_steps(q, option_index=1), [("keys", ["2"])])
        self.assertEqual(hr.answer_steps(q, text="Zig lang"), [("keys", ["4"]), ("text", "Zig lang")])
        self.assertEqual(hr.answer_steps(card("question-text"), text="Sheltie"), [("keys", ["1"]), ("text", "Sheltie")])
        self.assertEqual(hr.answer_steps(card("plan-exit"), option_index=1), [("keys", ["2"])])
        with self.assertRaises(hr.ApiError):
            hr.answer_steps(p, text="no")


class OpencodeMultiQuestionTests(unittest.TestCase):
    def test_answered_single_tab_label_has_no_check_mark(self):
        # 이미 답한 단일 선택 탭에 다시 오면 고른 항목이 `MIT ✓`로 그려진다(실측): 라벨에서 떼고 답했다는 것만 남긴다
        screen = oc("question-multi-tab2").replace("1. MIT", "1. MIT ✓", 1)
        p = hr.parse_prompt(screen, agent="opencode")
        self.assertEqual([o["label"] for o in p["options"]], ["MIT", "GPL", "Type your own answer"])
        self.assertTrue(p.get("answered_here"))
        self.assertEqual(p["id"], hr.parse_prompt(oc("question-multi-tab2"), agent="opencode")["id"])

    """여러 질문(탭)·복수 선택·Review(Confirm) 탭. 실측 1.19.6: 복수 선택은 숫자 키 = 토글, tab = 다음 탭.
    단일 선택 탭은 숫자 = 고르고 다음 탭. Review는 enter = 제출, esc = dismiss. 지금 탭은 화면 글자로 안 보인다."""

    def test_multi_tab_card(self):
        p = card("question-multi")
        self.assertEqual((p["kind"], p["agent"], p["title"], p["question"], p["multi_select"]),
                         ("question", "opencode", None, "Which features do you want?", True))
        self.assertIsNone(p["tabs"])  # 질문 탭이 여럿이면 지금 탭·답한 탭을 화면 글자로 모른다
        self.assertEqual(labels(p), ["Logging", "Tests", "CI"])  # 복수 선택의 직접 입력은 뺀다
        self.assertEqual([o["description"] for o in p["options"]], ["Structured logs", "Unit tests", "GitHub Actions"])
        self.assertEqual([o["number"] for o in p["options"]], [1, 2, 3])
        self.assertEqual((p["checked"], p["selected"], p["free_text_index"]), ([], None, None))
        c = card("question-multi-checked")  # PC에서 1·3 토글: 체크는 id에 안 들어간다
        self.assertEqual(c["checked"], [0, 2])
        self.assertEqual(c["id"], p["id"])
        q2 = card("question-multi3-q2")  # 가운데 탭의 복수 선택
        self.assertEqual((q2["question"], labels(q2), q2["multi_select"]), ("Which extras?", ["Cheese", "Bacon"], True))

    def test_multi_single_question_card(self):
        p = card("question-multi-one")  # 질문 하나 복수 선택도 탭 막대(`Langs   Confirm`): 탭 하나라 지금 탭을 안다
        self.assertEqual((p["title"], p["question"], p["multi_select"]), ("Langs", "Which languages?", True))
        self.assertEqual(p["tabs"], [{"label": "Langs", "answered": False}])
        self.assertEqual(labels(p), ["Python", "Go", "Rust"])

    def test_single_tab_card(self):
        p = card("question-multi-tab2")
        self.assertEqual((p["kind"], p["title"], p["question"], p["multi_select"], p["tabs"]),
                         ("question", None, "Which license?", False, None))
        self.assertEqual(labels(p), ["MIT", "GPL", "Type your own answer"])
        self.assertEqual((p["selected"], p["free_text_index"]), (None, 2))
        q1 = card("question-multi3-q1")
        self.assertEqual((q1["question"], labels(q1), q1["free_text_index"]),
                         ("Pick a size?", ["Small", "Large", "Type your own answer"], 2))

    def test_review_card(self):
        p = card("question-multi-confirm")
        self.assertEqual((p["kind"], p["title"], p["question"]), ("submit", None, "Review"))
        self.assertEqual(p["body"], "Features: Logging, CI\nLicense: GPL")
        self.assertEqual(labels(p), ["Submit", "Dismiss"])
        self.assertEqual((p["reject_index"], p["multi_select"]), (1, False))
        self.assertEqual(p["tabs"], [{"label": "Features", "answered": True}, {"label": "License", "answered": True}])
        e = card("question-multi-confirm-empty")
        self.assertEqual(e["body"], "Features: (not answered)\nLicense: (not answered)")
        self.assertEqual([t["answered"] for t in e["tabs"]], [False, False])
        self.assertNotEqual(e["id"], p["id"])
        t = card("question-multi3-confirm")
        self.assertEqual(t["body"], "Size: Large\nExtras: Cheese, Bacon\nDrink: Tea")
        self.assertEqual([x["label"] for x in t["tabs"]], ["Size", "Extras", "Drink"])
        self.assertEqual(card("question-multi-one-confirm")["tabs"], [{"label": "Langs", "answered": True}])

    def test_broken_screens_have_no_card(self):
        multi = oc("question-multi")
        for bad in (multi.replace("Features   License   Confirm", "Features   License"),  # Confirm 탭 없음
                    multi.replace(" (select all that apply)", ""),  # 복수 선택 표시 없음
                    multi.replace("enter toggle", "enter confirm"),  # 힌트와 줄 모양이 다름
                    multi.replace("4. [ ] Type your own answer", "4. [✓] Type your own answer"),  # PC에서 직접 입력 체크
                    multi.replace("4. [ ] Type your own answer", "4. [ ] Other")):  # 직접 입력 항목 없음
            self.assertIsNone(hr.parse_prompt(bad, agent="opencode"))
        rev = oc("question-multi-confirm")
        for bad in (rev.replace("Review", "Summary"), rev.replace("License: GPL", "Licence: GPL"),
                    rev.replace("enter submit", "enter confirm")):
            self.assertIsNone(hr.parse_prompt(bad, agent="opencode"))

    def test_answer_steps(self):
        m = card("question-multi")
        self.assertEqual(hr.answer_steps(m, option_indices=[2, 0]), [("keys", ["1", "3", "tab"])])
        self.assertEqual(hr.answer_steps(m, option_indices=[]), [("keys", ["tab"])])  # 안 골라도 넘어간다(Unanswered)
        c = card("question-multi-checked")  # 지금 [0, 2] → 원하는 [1, 2]: 1·2 토글 후 tab
        self.assertEqual(hr.answer_steps(c, option_indices=[1, 2]), [("keys", ["1", "2", "tab"])])
        self.assertEqual(hr.answer_steps(c, option_indices=[0, 2]), [("keys", ["tab"])])
        with self.assertRaises(hr.ApiError):
            hr.answer_steps(c, option_indices=[3])
        with self.assertRaises(hr.ApiError):
            hr.answer_steps(m, option_index=0)
        with self.assertRaises(hr.ApiError):
            hr.answer_steps(m, text="x")
        self.assertEqual(hr.answer_steps(card("question-multi-one"), option_indices=[1, 2]), [("keys", ["2", "3", "tab"])])
        t = card("question-multi-tab2")  # 단일 선택 탭: 숫자 = 고르고 다음 탭, 직접 입력은 숫자 → 붙여넣기+enter
        self.assertEqual(hr.answer_steps(t, option_index=1), [("keys", ["2"])])
        self.assertEqual(hr.answer_steps(t, text="Apache 2"), [("keys", ["3"]), ("text", "Apache 2")])
        with self.assertRaises(hr.ApiError):
            hr.answer_steps(t, option_indices=[0])
        r = card("question-multi-confirm")
        self.assertEqual(hr.answer_steps(r, option_index=0), [("keys", ["enter"])])
        self.assertEqual(hr.answer_steps(r, option_index=1), [("keys", ["esc"])])
        with self.assertRaises(hr.ApiError):
            hr.answer_steps(r, text="x")


class OpencodeScreenStatusTests(unittest.TestCase):
    """hc status·제목 없이 화면으로 막힘·작업·끝남 알림."""

    def setUp(self):
        self.app, self.hc, self.pusher = make_app()
        self.later = []
        self.app._later = lambda sec, fn: self.later.append((sec, fn))
        info = self.hc.add("o1", None, agent="opencode", name="oc", turn=0)
        info["title"] = "OpenCode"
        self.hc.screens["o1"] = oc("idle-start")
        self.hc.add("sh", None, agent=None, argv=["/bin/zsh"])
        self.app.poll(initial=True)

    def screen(self, name):
        self.hc.screens["o1"] = oc(name)
        self.app.screen_poll()

    def err(self, fn, *a):
        with self.assertRaises(hr.ApiError) as cm:
            fn(*a)
        return cm.exception.code, cm.exception.body["error"]

    def states(self):
        return [(x["state"], x["category"], x["kind"]) for x in self.pusher.payloads]

    def test_idle_start_sends_nothing(self):
        self.screen("idle-start")
        self.assertEqual(self.pusher.payloads, [])
        by = {t["id"]: t for t in self.app.api_terminals()["terminals"]}
        self.assertEqual((by["o1"]["claude"], by["o1"]["agent_kind"]), (True, "opencode"))
        self.assertEqual(by["o1"]["status"]["state"], "idle")

    def test_working_then_done(self):
        self.screen("working")
        by = {t["id"]: t for t in self.app.api_terminals()["terminals"]}
        self.assertEqual(by["o1"]["status"]["state"], "working")
        self.screen("hello2")
        self.screen("hello2")
        self.assertEqual([x["state"] for x in self.pusher.payloads], ["done"])

    def test_short_task_sent_from_app_gets_done(self):
        # 3초 화면 판정 사이에 끝나는 작업(실측: 가짜 모델 답 117ms)은 작업 중 화면을 못 본다.
        # 앱에서 보낸 순간을 작업 중으로 두면 다음 판정에서 끝남 알림이 간다. 보내자마자 본 화면(아직 시작 전)은 잠깐 기다린다
        now = [100.0]
        self.app.clock = lambda: now[0]
        self.screen("hello2")
        self.assertEqual(self.pusher.payloads, [])  # 작업을 못 봤다(시작 화면 → 끝난 화면): 알림 없음
        self.assertEqual(self.app.api_send("o1", {"text": "short task"}), {"ok": True})
        by = {t["id"]: t for t in self.app.api_terminals()["terminals"]}
        self.assertEqual(by["o1"]["status"]["state"], "working")
        now[0] += 0.5
        self.screen("hello2")  # 보낸 직후: 아직 그려지기 전일 수 있다
        self.assertEqual(self.pusher.payloads, [])
        now[0] += hr.SEND_SETTLE
        self.screen("hello2")
        self.assertEqual([x["state"] for x in self.pusher.payloads], ["done"])
        # 끝남 뒤 다시 짧은 작업: 두 번째 끝남도 간다
        self.app.api_send("o1", {"text": "again"})
        now[0] += hr.SEND_SETTLE + 1
        self.screen("hello2")
        self.assertEqual([x["state"] for x in self.pusher.payloads], ["done", "done"])
        # 보내기가 거절되면(대화 상자) 작업 중으로 두지 않는다
        self.screen("question")
        self.err(self.app.api_send, "o1", {"text": "x"})
        self.assertEqual(self.app.api_terminals()["terminals"][0]["status"]["state"], "blocked")

    def test_approval_push_card_and_answer(self):
        self.screen("working")
        self.screen("exec")
        self.assertEqual(self.states(), [("blocked", "HR_PERMISSION_ALWAYS", "approval")])
        pid = card("exec")["id"]
        self.assertEqual(self.pusher.payloads[0]["prompt_id"], pid)
        self.assertEqual(self.app.api_terminals()["terminals"][0]["status"]["msg"], "approve Shell command")
        self.assertEqual(self.app.api_answer("o1", {"action": "approve", "prompt_id": pid})["keys"], ["enter"])
        self.assertEqual(self.app.api_answer("o1", {"action": "always", "prompt_id": pid})["keys"],
                         ["right", "enter", "enter"])
        self.assertEqual(self.app.api_answer("o1", {"action": "reject", "prompt_id": pid})["keys"], ["esc"])
        # prompt_id 없는 옛 경로: 숫자 "1"은 무시되므로 enter
        self.assertEqual(self.app.api_answer("o1", {"action": "approve"})["keys"], ["enter"])
        self.assertEqual(self.app.api_answer("o1", {"action": "reject"})["keys"], ["esc"])
        self.assertEqual(self.hc.keys_calls, [("o1", ["enter"]), ("o1", ["right", "enter", "enter"]),
                                              ("o1", ["esc"]), ("o1", ["enter"]), ("o1", ["esc"])])
        # 같은 막힘이 이어지면 다시 알리지 않고, 끝나면 끝남 한 번
        self.screen("exec")
        self.screen("working")
        self.screen("hello2")
        self.assertEqual([x["state"] for x in self.pusher.payloads], ["blocked", "done"])

    def test_always_confirm_card(self):
        self.screen("exec-always-confirm")
        p = self.app.api_prompt("o1")["prompt"]
        self.assertEqual(p["title"], "Always allow")
        self.assertEqual(self.app.api_answer("o1", {"action": "approve", "prompt_id": p["id"]})["keys"], ["enter"])
        # 2차 확인의 esc는 Cancel(원래 메뉴로)이라 거절은 esc 두 번(실측)
        self.assertEqual(self.app.api_answer("o1", {"action": "reject", "prompt_id": p["id"]})["keys"], ["esc", "esc"])
        self.assertEqual(self.err(self.app.api_answer, "o1", {"action": "always", "prompt_id": p["id"]}),
                         (409, "no_always"))
        self.app.api_prompt_answer("o1", {"prompt_id": p["id"], "option_index": 1})
        self.assertEqual(self.hc.keys_calls[-1], ("o1", ["esc"]))

    def test_recheck_before_keys(self):
        self.screen("exec")
        pid = card("exec")["id"]
        self.hc.screens["o1"] = oc("write")
        self.assertEqual(self.err(self.app.api_answer, "o1", {"action": "approve", "prompt_id": pid}),
                         (409, "prompt_changed"))
        self.hc.screens["o1"] = oc("hello2")
        self.assertIsNone(self.app.api_prompt("o1")["prompt"])
        self.assertEqual(self.err(self.app.api_answer, "o1", {"action": "approve"}), (409, "not_blocked"))
        self.assertEqual(self.hc.keys_calls, [])

    def test_question_and_plan_cards(self):
        self.screen("question")
        p = self.app.api_prompt("o1")["prompt"]
        self.assertEqual(p["kind"], "question")
        self.app.api_prompt_answer("o1", {"prompt_id": p["id"], "option_index": 1})
        self.app.api_prompt_answer("o1", {"prompt_id": p["id"], "text": "Zig lang"})
        self.assertEqual(self.hc.keys_calls, [("o1", ["2"]), ("o1", ["4"])])
        self.assertEqual(self.hc.send_calls, [("o1", "Zig lang", "force")])
        self.assertEqual(self.err(self.app.api_answer, "o1", {"action": "approve", "prompt_id": p["id"]}),
                         (409, "needs_manual"))
        self.screen("plan-exit")
        q = self.app.api_prompt("o1")["prompt"]
        self.assertEqual(q["kind"], "plan")
        self.assertEqual(self.err(self.app.api_answer, "o1", {"action": "approve", "prompt_id": q["id"]}),
                         (409, "needs_manual"))
        self.assertEqual(self.app.api_answer("o1", {"action": "reject", "prompt_id": q["id"]})["keys"], ["2"])
        self.assertEqual([(x["category"], x["kind"]) for x in self.pusher.payloads],
                         [("HR_BLOCKED", "question"), ("HR_BLOCKED", "plan")])

    def test_dialog_without_card_blocks_once(self):
        # PC에서 직접 입력 칸을 연 여러 질문은 카드가 없지만 막힘(제목이 없어 힌트 줄로 안다). 다른 탭도 같은 막힘
        self.screen("question-multi-free-open")
        self.assertEqual([(s, c) for s, c, _ in self.states()], [("blocked", "HR_BLOCKED")])
        self.assertIsNone(self.pusher.payloads[0]["prompt_id"])
        self.assertEqual(self.later, [])
        self.assertIsNone(self.app.api_prompt("o1")["prompt"])
        self.screen("question-multi-free-filled")
        self.screen("question-multi-tab2-free-open")
        self.assertEqual(len(self.pusher.payloads), 1)
        self.screen("hello2")
        self.assertEqual([x["state"] for x in self.pusher.payloads], ["blocked", "done"])

    def test_multi_question_cards(self):
        self.screen("working")
        self.screen("question-multi")
        p = self.app.api_prompt("o1")["prompt"]
        self.assertEqual([(x["category"], x["kind"], x["prompt_id"]) for x in self.pusher.payloads],
                         [("HR_BLOCKED", "question", p["id"])])
        self.assertTrue(p["multi_select"])
        self.assertEqual(self.err(self.app.api_prompt_answer, "o1", {"prompt_id": p["id"], "option_index": 0}),
                         (400, "use_option_indices"))
        self.assertEqual(self.err(self.app.api_prompt_answer, "o1", {"prompt_id": p["id"], "text": "x"}),
                         (400, "no_free_text"))
        self.assertEqual(self.err(self.app.api_answer, "o1", {"action": "approve", "prompt_id": p["id"]}),
                         (409, "needs_manual"))
        self.hc.screens["o1"] = oc("question-multi-checked")  # PC에서 1·3 토글: 같은 카드, 지금 체크로 계산
        self.app.api_prompt_answer("o1", {"prompt_id": p["id"], "option_indices": [2]})
        self.assertEqual(self.hc.keys_calls, [("o1", ["1", "tab"])])
        # 다음 탭(단일 선택): 새 카드. 옛 카드 id로 답하면 prompt_changed
        self.hc.screens["o1"] = oc("question-multi-tab2")
        t = self.app.api_prompt("o1")["prompt"]
        self.assertNotEqual(t["id"], p["id"])
        self.assertEqual(self.err(self.app.api_prompt_answer, "o1", {"prompt_id": p["id"], "option_indices": [0]}),
                         (409, "prompt_changed"))
        self.app.api_prompt_answer("o1", {"prompt_id": t["id"], "text": "Apache 2"})
        self.assertEqual(self.hc.keys_calls[-1], ("o1", ["3"]))
        self.assertEqual(self.hc.send_calls, [("o1", "Apache 2", "force")])
        # Review 탭: 확인 카드, enter 제출 / esc dismiss
        self.hc.screens["o1"] = oc("question-multi-confirm")
        r = self.app.api_prompt("o1")["prompt"]
        self.assertEqual(r["kind"], "submit")
        self.app.api_prompt_answer("o1", {"prompt_id": r["id"], "option_index": 0})
        self.assertEqual(self.hc.keys_calls[-1], ("o1", ["enter"]))
        self.app.api_prompt_answer("o1", {"prompt_id": r["id"], "option_index": 1})
        self.assertEqual(self.hc.keys_calls[-1], ("o1", ["esc"]))
        self.hc.screens["o1"] = oc("question-multi-confirm-empty")  # 요약이 바뀌면 다른 카드
        self.assertEqual(self.err(self.app.api_prompt_answer, "o1", {"prompt_id": r["id"], "option_index": 0}),
                         (409, "prompt_changed"))
        # 탭을 옮겨도 같은 막힘(알림 한 번)
        self.screen("question-multi-tab2")
        self.screen("question-multi-confirm")
        self.assertEqual(len(self.pusher.payloads), 1)

    def test_typing_in_open_question_is_not_a_new_block(self):
        self.screen("question")
        self.screen("question-custom-typing")  # PC에서 직접 입력 칸을 열었다: 카드 없음, 같은 상자
        self.assertEqual([x["kind"] for x in self.pusher.payloads], ["question"])
        self.assertEqual(self.app.api_terminals()["terminals"][0]["status"]["state"], "blocked")

    def test_send_refused_while_dialog_open(self):
        self.screen("question")
        self.assertEqual(self.err(self.app.api_send, "o1", {"text": "hello"}), (409, "hc_refused"))
        self.screen("question-multi")
        self.assertEqual(self.err(self.app.api_send, "o1", {"text": "hello"}), (409, "hc_refused"))
        self.screen("hello2")
        self.assertEqual(self.app.api_send("o1", {"text": "hello"}), {"ok": True})
        self.assertEqual(self.app.api_keys("o1", {"keys": ["esc"]}), {"ok": True})
        self.assertEqual(self.hc.send_calls, [("o1", "hello")])

    def test_title_event_refreshes(self):
        self.hc.screens["o1"] = oc("exec")
        self.app.handle_event_line(ev("o1", "title", {"title": "OC | Hi from mock."}))
        self.assertEqual([x["category"] for x in self.pusher.payloads], ["HR_PERMISSION_ALWAYS"])


def build_db(path, jsonl_files):
    """capture export(첫 줄 session, 이후 {"message", "parts"})로 opencode.db 모양(session·message·part)을 만든다."""
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE session (id text PRIMARY KEY, project_id text NOT NULL, parent_id text, slug text NOT NULL,
            directory text NOT NULL, title text NOT NULL, version text NOT NULL, time_created integer NOT NULL,
            time_updated integer NOT NULL, time_archived integer);
        CREATE TABLE message (id text PRIMARY KEY, session_id text NOT NULL, time_created integer NOT NULL,
            time_updated integer NOT NULL, data text NOT NULL);
        CREATE TABLE part (id text PRIMARY KEY, message_id text NOT NULL, session_id text NOT NULL,
            time_created integer NOT NULL, time_updated integer NOT NULL, data text NOT NULL);
    """)
    sids = []
    for fn in jsonl_files:
        with open(os.path.join(FIX, fn), encoding="utf-8") as f:
            recs = [json.loads(line) for line in f if line.strip()]
        s = recs[0]["session"]
        sids.append(s["id"])
        con.execute("INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (s["id"], s["project_id"], s["parent_id"], s["slug"], s["directory"], s["title"], s["version"],
                     s["time_created"], s["time_updated"], s["time_archived"]))
        for r in recs[1:]:
            m = dict(r["message"])
            mid = m.pop("id")
            t = m["time"]["created"]
            con.execute("INSERT INTO message VALUES (?,?,?,?,?)", (mid, s["id"], t, t, json.dumps(m)))
            for pt in r["parts"]:
                pt = dict(pt)
                pid = pt.pop("id")
                con.execute("INSERT INTO part VALUES (?,?,?,?,?,?)", (pid, mid, s["id"], t, t, json.dumps(pt)))
    con.commit()
    con.close()
    return sids


class OpencodeStyledTabsTests(unittest.TestCase):
    """여러 질문 탭 막대의 지금 탭·답한 탭은 색으로만 보인다(실측 1.19.6). 앱 카드(/prompt)만 hc attach --observe 첫 그림
    (색 포함)을 한 번 읽어 tabs·title을 채운다. 지금 탭 = 배경이 다른 탭, 답 안 한 탭 = Confirm 탭과 같은 흐린 글자색."""

    @staticmethod
    def raw(name):
        with open(os.path.join(FIX, "opencode-question-multi-styled-%s-%s.ansi" % (name, V)), encoding="utf-8",
                  newline="") as f:
            return f.read()

    def styled(self, name):
        text = oc("question-multi-styled-" + name)
        p = hr.parse_prompt(text, agent="opencode")
        pid = p["id"]
        hr._opencode_style_tabs(p, text, self.raw(name))
        self.assertEqual(p["id"], pid)  # 색으로 채운 탭은 id에 안 넣는다(재확인·알림 카드와 같은 id)
        return p

    def test_first_tab_current(self):
        p = self.styled("t1")
        self.assertEqual((p["title"], p["question"], p["multi_select"]), ("Features", "Which features do you want?", True))
        self.assertEqual(p["tabs"], [{"label": "Features", "answered": False}, {"label": "License", "answered": False}])

    def test_second_tab_current_first_answered(self):
        p = self.styled("t2")
        self.assertEqual((p["title"], p["question"], p["multi_select"]), ("License", "Which license?", False))
        self.assertEqual(p["tabs"], [{"label": "Features", "answered": True}, {"label": "License", "answered": False}])

    def test_unreadable_colors_leave_tabs_empty(self):
        text = oc("question-multi-styled-t1")
        plain = re.sub(r"\x1b\[[0-9;]*m", "", self.raw("t1"))
        swapped = self.raw("t2")  # 다른 화면의 그림: 탭 라벨은 같지만 지금 탭이 다르다 → 그래도 색대로(같은 상자)
        for raw in ("", plain, "garbage\r\n"):
            p = hr.parse_prompt(text, agent="opencode")
            hr._opencode_style_tabs(p, text, raw)
            self.assertEqual((p["tabs"], p["title"]), (None, None), repr(raw[:40]))
        p = hr.parse_prompt(text, agent="opencode")
        hr._opencode_style_tabs(p, text, swapped.replace("48;2;157;124;216", "48;2;20;20;20"))  # 지금 탭이 안 보임
        self.assertEqual(p["tabs"], None)
        # 한 탭만 있는 상자·Review 카드는 이미 tabs가 있어 건드리지 않는다
        one = card("question-multi-one")
        hr._opencode_style_tabs(one, oc("question-multi-one"), self.raw("t1"))
        self.assertEqual(one["tabs"], [{"label": "Langs", "answered": False}])

    def test_hc_read_styled_cuts_stream_after_paint(self):
        seen = []

        def runner(argv, **kw):
            seen.append((argv, kw.get("timeout"), kw.get("stdin")))
            raise hr.subprocess.TimeoutExpired(argv, kw["timeout"], output="\x1b[0m그림".encode("utf-8"))
        hc = hr.Hc("/bin/hc", runner=runner)
        self.assertEqual(hc.read_styled("t3"), "\x1b[0m그림")
        self.assertEqual(seen, [(["/bin/hc", "attach", "--observe", "t3"], hr.HC_STYLED_WAIT, hr.subprocess.DEVNULL)])

        def empty(argv, **kw):
            return hr.subprocess.CompletedProcess(argv, 1, b"", b"no such terminal")
        with self.assertRaises(hr.HcError):
            hr.Hc("/bin/hc", runner=empty).read_styled("t3")

    def test_api_prompt_reads_colors_once(self):
        app, hc, _ = make_app()
        info = hc.add("o1", None, agent="opencode", name="oc", turn=0)
        info["title"] = "OC | x"
        hc.screens["o1"] = oc("idle-start")
        app.poll(initial=True)
        hc.screens["o1"] = oc("question-multi-styled-t2")
        app.screen_poll()
        calls = []
        hc.read_styled = lambda tid: calls.append(tid) or self.raw("t2")
        p = app.api_prompt("o1")["prompt"]
        self.assertEqual((p["title"], p["tabs"][0]["answered"]), ("License", True))
        self.assertEqual(calls, ["o1"])
        # 답 직전 재확인은 색을 안 읽고도 같은 id
        self.assertEqual(app.api_prompt_answer("o1", {"prompt_id": p["id"], "option_index": 1})["ok"], True)
        self.assertEqual(calls, ["o1"])
        self.assertEqual(hc.keys_calls, [("o1", ["2"])])

        def broken(tid):
            raise hr.HcError("attach failed")
        hc.read_styled = broken  # 색을 못 읽으면 탭 없이 카드는 그대로
        q = app.api_prompt("o1")["prompt"]
        self.assertEqual((q["id"], q["tabs"], q["title"]), (p["id"], None, None))


class OpencodeTranscriptTests(unittest.TestCase):
    def setUp(self):
        self.home = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)
        d = os.path.join(self.home, ".local", "share", "opencode")
        os.makedirs(d)
        self.db = os.path.join(d, "opencode.db")
        self.sid, self.sid_plan = build_db(self.db, ["opencode-session-%s.jsonl" % V,
                                                     "opencode-session-plan-%s.jsonl" % V])
        self.env = dict(os.environ)
        os.environ.pop("XDG_DATA_HOME", None)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self.env)))

    def test_parse(self):
        turns, cut = hr.read_opencode_transcript(self.db, self.sid, 200)
        self.assertFalse(cut)
        self.assertEqual(turns[0], {"role": "user", "parts": [{"kind": "text", "text": "hello"}]})
        self.assertEqual(turns[1], {"role": "assistant", "parts": [{"kind": "text", "text": "Hi from mock."}]})
        tools = [p for t in turns for p in t["parts"] if p["kind"] == "tool"]
        self.assertEqual([(t["name"], t["state"]) for t in tools][:3],
                         [("bash", "ok"), ("write", "error"), ("edit", "ok")])
        self.assertEqual((tools[0]["summary"], tools[0]["output"]), ("touch notes.txt", "(no output)"))
        self.assertEqual(tools[1]["summary"], "/Users/me/work/demo/hello.py")
        self.assertIn("rejected permission", tools[1]["output"])
        q = [t for t in tools if t["name"] == "question"]
        self.assertEqual(q[0]["summary"], "Which language should the demo use?")
        self.assertIn("dismissed", q[-1]["output"])
        # 한 턴의 assistant 메시지(도구 호출 단계, 답 단계)는 한 덩어리로
        roles = [t["role"] for t in turns]
        self.assertTrue(all(a != b for a, b in zip(roles, roles[1:])))

    def test_hides_synthetic_and_reminders(self):
        turns, _ = hr.read_opencode_transcript(self.db, self.sid_plan, 200)
        texts = [p["text"] for t in turns if t["role"] == "user" for p in t["parts"]]
        self.assertEqual(texts, ["hello", "plan hello", "write the plan", "done planning", "done planning again"])
        tools = [p for t in turns for p in t["parts"] if p["kind"] == "tool"]
        self.assertIn(("plan_exit", "ok"), [(t["name"], t["state"]) for t in tools])

    def test_limit(self):
        turns, cut = hr.read_opencode_transcript(self.db, self.sid, 3)
        self.assertTrue(cut)
        self.assertEqual(len(turns), 3)
        turns, cut = hr.read_opencode_transcript(self.db, self.sid, 50, max_messages=4)
        self.assertTrue(cut)
        self.assertEqual(turns[-1]["parts"][-1]["text"], "Waited.")

    def test_find_and_api(self):
        info = {"agent": "opencode", "cwd": CWD}
        # 같은 폴더의 가장 최근 세션(plan 세션이 나중에 바뀌었다)
        self.assertEqual(hr.find_agent_transcript(info, self.home), (self.sid_plan, self.db))
        self.assertEqual(hr.find_agent_transcript({"agent": "opencode", "cwd": "/nope"}, self.home), (None, None))
        self.assertEqual(hr.find_agent_transcript(info, "/nonexistent-home"), (None, None))
        os.environ["XDG_DATA_HOME"] = os.path.join(self.home, ".local", "share")
        self.assertEqual(hr.find_agent_transcript(info, "/nonexistent-home"), (self.sid_plan, self.db))
        app, hc, _ = make_app(home=self.home)
        i = hc.add("o1", None, agent="opencode")
        i["cwd"] = CWD
        hc.screens["o1"] = oc("hello2")
        r = app.api_transcript("o1", 50)
        self.assertEqual((r["session_id"], r["truncated"]), (self.sid_plan, False))
        self.assertEqual(r["turns"][0]["parts"][0]["text"], "hello")
        by = {t["id"]: t for t in app.api_terminals()["terminals"]}
        self.assertEqual(by["o1"]["preview"], "Starting implementation.")

    def test_db_is_not_modified(self):
        before = os.stat(self.db).st_mtime_ns
        hr.read_opencode_transcript(self.db, self.sid, 50)
        hr.opencode_preview(self.db, self.sid)
        self.assertEqual(os.stat(self.db).st_mtime_ns, before)
        self.assertEqual(hr.read_opencode_transcript(self.db, "../x", 50), ([], False))
        self.assertEqual(hr.read_opencode_transcript(os.path.join(self.home, "nope.db"), self.sid, 50), ([], False))


if __name__ == "__main__":
    unittest.main()
