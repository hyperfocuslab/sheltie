"""Grok Build 지원. 화면은 실제 grok 1.0.77을 hc 안에서(가짜 모델 서버로) 캡처한 것.
Grok은 OSC 7501로 승인·계획·폴더 신뢰(permission)·질문(question) 막힘을 보낸다: hc status를 그대로 쓰고 덧씌우기는 없다.
승인·질문 메뉴는 화면 끝 힌트 줄 위 `┃` 막대 안에, 계획 검토는 위쪽 plan.md 미리보기 상자 + a/s/q 글자 키."""
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_sheltie import ev, fixture, make_app, hr  # noqa: E402

V = "1.0.77"
APP = "grok"
PERM = {"app": APP, "state": "blocked", "kind": "permission", "msg": "Write hello into notes.txt"}
PERM_RM = {"app": APP, "state": "blocked", "kind": "permission", "msg": "Delete copy.md"}
PERM_WRITE = {"app": APP, "state": "blocked", "kind": "permission", "msg": "Allow Edit to /Users/me/work/demo/hello.py?"}
PERM_EDIT = {"app": APP, "state": "blocked", "kind": "permission", "msg": "Allow Edit to hello.py?"}
PERM_PLAN = {"app": APP, "state": "blocked", "kind": "permission", "msg": "Review the plan"}
TRUST = {"app": APP, "state": "blocked", "kind": "permission", "msg": "Trust this folder?"}
QUESTION = {"app": APP, "state": "blocked", "kind": "question", "msg": "Which language should the demo use?"}
QUESTION_TEXT = {"app": APP, "state": "blocked", "kind": "question", "msg": "What should the project be called?"}
QUESTION_MULTI = {"app": APP, "state": "blocked", "kind": "question", "msg": "Which features to include?"}
IDLE = {"app": APP, "state": "idle"}
WORKING = {"app": APP, "state": "working", "msg": "Waiting for response…"}
DONE = {"app": APP, "state": "done", "msg": "Turn complete in 1.7s."}


def gk(name):
    return fixture("grok-%s-%s.txt" % (name, V))


def card(name):
    return hr.parse_prompt(gk(name), agent="grok")


def labels(p):
    return [o["label"] for o in p["options"]]


def status_ev(tid, st):
    return ev(tid, "status", {"status": st})


class GrokAgentKindTests(unittest.TestCase):
    def test_detects_grok(self):
        self.assertEqual(hr.agent_kind({"agent": "grok"}), "grok")
        self.assertEqual(hr.agent_kind({"status": {"app": "grok", "state": "idle"}}), "grok")
        # 실측 hc get: agent 없이 앞 프로그램 argv가 ~/.local/bin/grok(실제 바이너리 이름 grok-macos-aarch64)
        self.assertEqual(hr.agent_kind({"foreground": {"argv": ["/Users/me/.local/bin/grok"],
                                                       "name": "grok-macos-aarch64"}}), "grok")
        self.assertIsNone(hr.agent_kind({"foreground": {"argv": ["grok-tool"]}}))
        self.assertEqual(hr.agent_kind({"agent": "qwen"}), "qwen")
        self.assertEqual(hr.agent_kind({"agent": "claude"}), "claude")


class GrokParseTests(unittest.TestCase):
    def test_shell_approval(self):
        p = card("exec")
        self.assertEqual((p["kind"], p["agent"]), ("approval", "grok"))
        self.assertEqual((p["title"], p["question"], p["body"]),
                         ("Shell", "Write hello into notes.txt", "echo hello > notes.txt"))
        self.assertEqual(labels(p), ["Yes, and don't ask again for anything (always-approve mode)", "Yes, proceed",
                                     "No, reject", "Never allow: echo"])
        self.assertEqual([o["number"] for o in p["options"]], [1, 2, 3, 4])
        # always-approve 모드는 $GROK_HOME/config.toml에 전역 영구 저장(실측): 잠금화면 "계속 허용"에 안 붙인다
        # 거절 줄은 입력 항목(메모와 함께 거절): 거절 버튼은 reject_keys로 그 번호
        self.assertEqual((p["selected"], p["approve_index"], p["always_index"], p["persist_index"], p["reject_index"],
                          p["free_text_index"]), (1, 1, None, 0, None, 2))
        self.assertEqual(hr.push_category("blocked", PERM, p), "HR_PERMISSION")

    def test_shell_approval_with_remembered_rule(self):
        # 항목이 5개이고 Yes가 3번: 번호가 아니라 라벨로 맞춘다
        p = card("exec-alwaysallow")
        self.assertEqual((p["question"], p["body"]), ("Delete copy.md", "rm copy.md"))
        self.assertEqual(labels(p), ["Yes, and don't ask again for anything (always-approve mode)",
                                     "Always allow: rm copy.md", "Yes, proceed", "No, reject",
                                     "Never allow: rm copy.md"])
        self.assertEqual((p["selected"], p["approve_index"], p["always_index"], p["persist_index"], p["reject_index"],
                          p["free_text_index"]), (2, 2, None, 0, None, 3))
        # ←/→로 범위를 바꾸면 커서·라벨이 바뀐다: 다른 카드
        p2 = card("exec-scope-right")
        self.assertEqual((labels(p2)[3], p2["selected"], p2["approve_index"]), ("Never allow: echo hello", 3, 1))
        self.assertNotEqual(p2["id"], card("exec")["id"])

    def test_file_approvals(self):
        for name, q, needles in (("write", "Allow Edit to /Users/me/work/demo/hello.py?",
                                  ("/Users/me/work/demo/hello.py", 'print("hello")')),
                                 ("edit", "Allow Edit to hello.py?", ("hello.py", 'print("hello, world")'))):
            p = card(name)
            self.assertEqual((p["kind"], p["title"], p["question"]), ("approval", "Edit", q), name)
            for n in needles:
                self.assertIn(n, p["body"])
            self.assertEqual(labels(p), ["Yes, and don't ask again for anything (always-approve mode)",
                                         "Yes, allow all edits during this session", "Yes", "No, reject"])
            # "allow all edits during this session"은 메모리만(실측: 파일 변화 없음): 세션 한정 허용
            self.assertEqual((p["selected"], p["approve_index"], p["always_index"], p["persist_index"],
                              p["reject_index"], p["free_text_index"]), (2, 2, 1, 0, None, 3))
            self.assertEqual(hr.push_category("blocked", PERM_WRITE, p), "HR_PERMISSION_ALWAYS")
        self.assertTrue(card("edit")["body"].startswith("hello.py\n\n"))

    def test_trust(self):
        p = card("trust")
        self.assertEqual((p["kind"], p["title"], p["question"]),
                         ("approval", None, "Do you trust the contents of this directory?"))
        self.assertEqual(p["body"], "/Users/me/work/demo\n\nGrok Build may run or modify contents in this directory, "
                                    "posing security risks.")
        self.assertEqual(labels(p), ["Yes, proceed", "No, quit"])
        self.assertEqual([o["number"] for o in p["options"]], [None, None])
        self.assertEqual((p["approve_index"], p["always_index"], p["persist_index"], p["reject_index"]),
                         (0, None, None, 1))
        self.assertEqual(hr.push_category("blocked", TRUST, p), "HR_PERMISSION")

    def test_question(self):
        p = card("question")
        self.assertEqual((p["kind"], p["title"], p["question"]),
                         ("question", None, "Which language should the demo use?"))
        self.assertEqual(labels(p), ["Python", "Go", "Rust", "Type your answer here"])
        self.assertEqual([o["description"] for o in p["options"]],
                         ["Simple script, no build step", "Single static binary", "Fast, more setup", None])
        self.assertEqual([o["number"] for o in p["options"]], [1, 2, 3, None])
        self.assertEqual((p["selected"], p["free_text_index"], p["multi_select"]), (None, 3, False))
        self.assertEqual(hr.push_category("blocked", QUESTION, p), "HR_BLOCKED")

    def test_text_only_question(self):
        p = card("question-text")
        self.assertEqual((p["kind"], p["question"]), ("question", "What should the project be called?"))
        self.assertEqual(labels(p), ["Type your answer here"])
        self.assertEqual((p["free_text_index"], p["options"][0]["number"]), (0, None))

    def test_plan(self):
        p = card("plan")
        self.assertEqual((p["kind"], p["title"], p["question"]), ("plan", "plan.md", "Review the plan"))
        self.assertTrue(p["body"].startswith("Plan: add greeting CLI\n\nContext\nThe demo needs a CLI entry point."))
        self.assertIn("1. Add main.py with argparse", p["body"])
        self.assertTrue(p["body"].endswith("Run python3 main.py --name me"))
        self.assertEqual(labels(p), ["Approve", "Request changes", "Quit plan"])
        self.assertEqual((p["selected"], p["free_text_index"], p["reject_index"]), (None, 1, 2))
        self.assertEqual(hr.push_category("blocked", PERM_PLAN, p), "HR_BLOCKED")

    def test_unknown_screens_have_no_card(self):
        # Esc로 포커스를 스크롤백으로 옮긴 승인(숫자 키 무시), 패턴 편집 중, z 입력 칸을 연 질문,
        # 계획 검토 중 입력창 포커스(s), 평소·작업 중·끝난 화면, 거절 뒤, 질문 거절 뒤
        for name in ("exec-after-esc", "exec-editpattern", "question-custom-z",
                     "plan-s", "idle", "hello", "done", "working", "exec-feedback", "question-ctrlc",
                     "plan-approved", "plan-quit"):
            self.assertIsNone(card(name), name)

    def test_parsers_do_not_cross(self):
        for name in ("exec", "exec-alwaysallow", "write", "edit", "trust", "question", "question-text", "plan"):
            for agent in (None, "codex", "gemini", "copilot", "qwen", "opencode"):
                self.assertIsNone(hr.parse_prompt(gk(name), agent=agent), (name, agent))
        for f in ("codex-exec-0.171.0.txt", "codex-plan-0.171.0.txt", "claude-bash-2.1.329.txt",
                  "claude-question-2.1.329.txt", "claude-plan-2.1.329.txt", "gemini-exec-0.70.0.txt",
                  "gemini-question-0.70.0.txt", "gemini-plan-0.70.0.txt", "copilot-exec-1.0.114.txt",
                  "copilot-trust-1.0.114.txt", "copilot-question-1.0.114.txt", "copilot-plan-1.0.114.txt",
                  "qwen-exec-0.28.3.txt", "qwen-question-0.28.3.txt", "qwen-plan-0.28.3.txt",
                  "qwen-trust-0.28.3.txt", "opencode-exec-1.19.6.txt", "opencode-question-1.19.6.txt"):
            self.assertIsNone(hr.parse_prompt(fixture(f), agent="grok"), f)

    def test_answer_steps(self):
        self.assertEqual(hr.answer_steps(card("exec"), option_index=1), [("keys", ["2"])])
        self.assertEqual(hr.answer_steps(card("exec"), option_index=3), [("keys", ["4"])])
        self.assertEqual(hr.answer_steps(card("exec-alwaysallow"), option_index=2), [("keys", ["3"])])
        with self.assertRaises(hr.ApiError):
            hr.answer_steps(card("exec"), option_index=2)  # 거절 줄은 직접 입력 항목(거절 버튼은 reject_keys)
        # 메모와 함께 거절: 커서를 거절 줄로 → 글자 하나로 입력 칸을 열고 지운 뒤 붙여넣기+enter(실측)
        self.assertEqual(hr.answer_steps(card("exec"), text="use printf"),
                         [("keys", ["down", "x", "backspace"]), ("text", "use printf")])
        self.assertEqual(hr.answer_steps(card("exec-scope-right"), text="no"),
                         [("keys", ["up", "x", "backspace"]), ("text", "no")])
        self.assertEqual(hr.answer_steps(card("write"), option_index=1), [("keys", ["2"])])
        # 질문: 숫자 키가 고르고 바로 제출, 직접 입력은 z(입력 칸으로) → 붙여넣기+enter(실측)
        self.assertEqual(hr.answer_steps(card("question"), option_index=1), [("keys", ["2"])])
        self.assertEqual(hr.answer_steps(card("question"), text="Kotlin please"),
                         [("keys", ["z"]), ("text", "Kotlin please")])
        with self.assertRaises(hr.ApiError):
            hr.answer_steps(card("question"), option_index=3)  # 직접 입력 줄은 글이 있어야 한다
        # 옵션 없는 질문: 커서가 이미 z 줄이라 z는 글자로 들어간다. enter(편집 시작) → 붙여넣기+enter(실측)
        self.assertEqual(hr.answer_steps(card("question-text"), text="sheltie-demo"),
                         [("keys", ["enter"]), ("text", "sheltie-demo")])
        # 계획: a 승인, q 포기, 수정 요청은 s(입력창으로) → 붙여넣기+enter(실측)
        self.assertEqual(hr.answer_steps(card("plan"), option_index=0), [("keys", ["a"])])
        self.assertEqual(hr.answer_steps(card("plan"), option_index=2), [("keys", ["q"])])
        self.assertEqual(hr.answer_steps(card("plan"), text="add --shout"), [("keys", ["s"]), ("text", "add --shout")])
        # 폴더 신뢰: y/n 글자 키
        self.assertEqual(hr.answer_steps(card("trust"), option_index=0), [("keys", ["y"])])
        self.assertEqual(hr.answer_steps(card("trust"), option_index=1), [("keys", ["n"])])


QUESTION_LICENSE = {"app": APP, "state": "blocked", "kind": "question", "msg": "Which license?"}
QUESTION_COLOR = {"app": APP, "state": "blocked", "kind": "question", "msg": "Which color?"}


class GrokMultiQuestionTests(unittest.TestCase):
    """여러 질문(`[1/2] … ←/→ question`)·복수 선택(`1 [x] Tests`). 카드는 지금 질문 하나(제목 "k/N"), 실측 1.0.77."""

    def test_multi_select_tab(self):
        p = card("question-multi")
        self.assertEqual((p["kind"], p["title"], p["question"]), ("question", "1/2", "Which features to include?"))
        self.assertEqual(labels(p), ["Tests", "CI", "Docs"])  # 직접 입력(z) 줄은 체크박스라 뺀다(Claude와 같다)
        self.assertEqual([o["description"] for o in p["options"]], ["Add unit tests", "GitHub Actions", "README section"])
        self.assertEqual([o["number"] for o in p["options"]], [1, 2, 3])
        self.assertEqual((p["multi_select"], p["checked"], p["free_text_index"], p["tabs"], p["last_tab"]),
                         (True, [], None, None, False))
        c = card("question-multi-checked")  # 1·3 체크: 힌트가 Enter:next, Esc:unselect로 바뀐다
        self.assertEqual((c["title"], c["checked"], c["multi_select"]), ("1/2", [0, 2], True))
        self.assertEqual(c["id"], p["id"])

    def test_single_tabs(self):
        p = card("question-multi-q2")  # 마지막 질문(단일 선택): 숫자 = 고르고 전체 제출
        self.assertEqual((p["title"], p["question"], p["multi_select"], p["last_tab"]),
                         ("2/2", "Which license?", False, True))
        self.assertEqual(labels(p), ["MIT", "Apache-2.0", "Type your answer here"])
        self.assertEqual([o["description"] for o in p["options"]], ["Permissive", "Patent grant", None])
        self.assertEqual((p["free_text_index"], p["selected"]), (2, None))
        p1 = card("question-tabs-single")  # 첫 질문(단일 선택, Enter:select): 숫자 = 고르고 다음 질문
        self.assertEqual((p1["title"], p1["question"], labels(p1), p1["free_text_index"], p1["last_tab"]),
                         ("1/2", "Which color?", ["Red", "Blue", "Type your answer here"], 2, False))

    def test_multi_select_last_and_single_question(self):
        p = card("question-tabs-multi-last")
        self.assertEqual((p["title"], p["multi_select"], p["checked"], p["last_tab"], p["free_text_index"]),
                         ("2/2", True, [], True, None))
        self.assertEqual(card("question-tabs-multi-last-checked")["checked"], [0, 1])
        one = card("question-multi-one")  # 질문 하나·복수 선택: 탭 표시 없음
        self.assertEqual((one["title"], one["question"], labels(one), one["multi_select"], one["last_tab"]),
                         (None, "Which features to include?", ["Tests", "CI", "Docs"], True, True))
        self.assertEqual(card("question-multi-one-checked")["checked"], [0])

    def test_answer_steps(self):
        # 복수 선택: 숫자 키 = 토글. 고른 게 있으면 enter = 다음 질문(마지막이면 제출), 없으면 → = 다음 질문(실측)
        m, mc = card("question-multi"), card("question-multi-checked")
        self.assertEqual(hr.answer_steps(m, option_indices=[2, 0]), [("keys", ["1", "3", "enter"])])
        self.assertEqual(hr.answer_steps(m, option_indices=[]), [("keys", ["right"])])
        self.assertEqual(hr.answer_steps(mc, option_indices=[1]), [("keys", ["1", "2", "3", "enter"])])
        self.assertEqual(hr.answer_steps(mc, option_indices=[]), [("keys", ["1", "3", "right"])])
        last, lc, one = card("question-tabs-multi-last"), card("question-tabs-multi-last-checked"), card("question-multi-one")
        self.assertEqual(hr.answer_steps(last, option_indices=[0, 1]), [("keys", ["1", "2", "enter"])])
        self.assertEqual(hr.answer_steps(lc, option_indices=[0]), [("keys", ["2", "enter"])])
        self.assertEqual(hr.answer_steps(one, option_indices=[2]), [("keys", ["3", "enter"])])
        for c in (last, one):
            with self.assertRaises(hr.ApiError) as cm:
                hr.answer_steps(c, option_indices=[])  # 아무것도 안 고르고 enter면 커서 줄이 골라져 제출된다(실측)
            self.assertEqual(cm.exception.body["error"], "bad_option")
        for c in (m, one):
            with self.assertRaises(hr.ApiError) as cm:
                hr.answer_steps(c, text="Lint")
            self.assertEqual(cm.exception.body["error"], "no_free_text")
            with self.assertRaises(hr.ApiError):
                hr.answer_steps(c, option_index=0)
        # 단일 선택 탭: 숫자 = 고르고 다음(마지막이면 제출), 직접 입력은 z → 붙여넣기+enter(실측: 다음 질문으로)
        self.assertEqual(hr.answer_steps(card("question-tabs-single"), option_index=1), [("keys", ["2"])])
        self.assertEqual(hr.answer_steps(card("question-tabs-single"), text="Green"), [("keys", ["z"]), ("text", "Green")])
        self.assertEqual(hr.answer_steps(card("question-multi-q2"), option_index=1), [("keys", ["2"])])

    def test_dialog_group(self):
        g = hr._grok_dialog_group
        self.assertEqual(g(gk("question-multi")), g(gk("question-multi-q2")))
        self.assertIsNotNone(g(gk("question-multi")))
        self.assertEqual(g(gk("question-tabs-single")), g(gk("question-tabs-multi-last-checked")))
        for name in ("question", "question-multi-one", "question-text", "exec", "plan", "done"):
            self.assertIsNone(g(gk(name)), name)

    def test_parsers_do_not_cross(self):
        for name in ("question-multi", "question-multi-checked", "question-tabs-single", "question-tabs-multi-last",
                     "question-multi-one"):
            for agent in (None, "codex", "gemini", "copilot", "qwen", "opencode"):
                self.assertIsNone(hr.parse_prompt(gk(name), agent=agent), (name, agent))


class GrokApiTests(unittest.TestCase):
    def setUp(self):
        self.app, self.hc, self.pusher = make_app()
        self.later = []
        self.app._later = lambda sec, fn: self.later.append((sec, fn))
        self.hc.add("g1", dict(WORKING), agent="grok", name="gk", turn=1)
        self.hc.screens["g1"] = gk("working")
        self.hc.add("sh", None, agent=None, argv=["/bin/zsh"])
        self.app.poll(initial=True)

    def err(self, fn, *a):
        with self.assertRaises(hr.ApiError) as cm:
            fn(*a)
        return cm.exception.code, cm.exception.body["error"]

    def set(self, st, screen=None, turn=None):
        if screen is not None:
            self.hc.screens["g1"] = screen
        self.hc.set_status("g1", st, turn=turn)
        self.app.handle_event_line(status_ev("g1", st))

    def test_approval_push_card_and_answers(self):
        self.set(PERM, gk("exec"))
        pid = card("exec")["id"]
        self.assertEqual([(x["state"], x["category"], x["kind"], x["prompt_id"], x["msg"]) for x in self.pusher.payloads],
                         [("blocked", "HR_PERMISSION", "approval", pid, "Write hello into notes.txt")])
        self.assertEqual(self.app.api_prompt("g1")["prompt"]["id"], pid)
        self.assertEqual(self.app.api_answer("g1", {"action": "approve", "prompt_id": pid})["keys"], ["2"])
        self.assertEqual(self.err(self.app.api_answer, "g1", {"action": "always", "prompt_id": pid}),
                         (409, "no_always"))
        self.assertEqual(self.app.api_answer("g1", {"action": "reject", "prompt_id": pid})["keys"], ["3"])
        self.app.api_prompt_answer("g1", {"prompt_id": pid, "option_index": 3})  # 영구 규칙은 카드에서 직접 고를 때만
        self.app.api_prompt_answer("g1", {"prompt_id": pid, "text": "use printf instead"})
        self.assertEqual(self.hc.keys_calls, [("g1", ["2"]), ("g1", ["3"]), ("g1", ["4"]),
                                              ("g1", ["down", "x", "backspace"])])
        self.assertEqual(self.hc.send_calls, [("g1", "use printf instead", "force")])

    def test_approval_numbers_follow_labels(self):
        self.set(PERM_RM, gk("exec-alwaysallow"))
        pid = card("exec-alwaysallow")["id"]
        self.assertEqual(self.app.api_answer("g1", {"action": "approve", "prompt_id": pid})["keys"], ["3"])
        self.assertEqual(self.app.api_answer("g1", {"action": "reject", "prompt_id": pid})["keys"], ["4"])

    def test_no_card_answers_do_not_send_esc(self):
        # 옛 경로(prompt_id 없음): msg가 "approve …"가 아니라 승인은 앱에서. Esc는 Grok에서 거절이 아니라 포커스를
        # 스크롤백으로 옮겨 숫자 키가 안 먹게 되므로 보내지 않는다
        self.set(PERM, gk("exec"))
        self.assertEqual(self.err(self.app.api_answer, "g1", {"action": "approve"}), (409, "needs_manual"))
        self.assertEqual(self.err(self.app.api_answer, "g1", {"action": "reject"}), (409, "needs_manual"))
        self.set(TRUST, gk("trust"))  # Claude 폴더 신뢰 msg("trust this folder")와 섞이지 않는다
        self.assertEqual(self.err(self.app.api_answer, "g1", {"action": "approve"}), (409, "needs_manual"))
        self.assertEqual(self.hc.keys_calls, [])
        self.assertFalse(hr.is_permission(TRUST))

    def test_session_always_for_file_edits(self):
        self.set(PERM_WRITE, gk("write"))
        self.assertEqual(self.pusher.payloads[-1]["category"], "HR_PERMISSION_ALWAYS")
        pid = card("write")["id"]
        self.assertEqual(self.app.api_answer("g1", {"action": "always", "prompt_id": pid})["keys"], ["2"])
        self.assertEqual(self.app.api_answer("g1", {"action": "approve", "prompt_id": pid})["keys"], ["3"])

    def test_recheck_before_keys(self):
        self.set(PERM_WRITE, gk("write"))
        pid = card("write")["id"]
        self.hc.screens["g1"] = gk("edit")
        self.assertEqual(self.err(self.app.api_answer, "g1", {"action": "approve", "prompt_id": pid}),
                         (409, "prompt_changed"))
        self.hc.screens["g1"] = gk("exec-after-esc")  # PC에서 Esc를 눌러 포커스가 스크롤백으로 갔다
        self.assertIsNone(self.app.api_prompt("g1")["prompt"])
        self.set(DONE, gk("done"), turn=2)
        self.assertIsNone(self.app.api_prompt("g1")["prompt"])
        self.assertEqual(self.err(self.app.api_answer, "g1", {"action": "approve", "prompt_id": pid}),
                         (409, "prompt_changed"))
        self.assertEqual(self.hc.keys_calls, [])

    def test_question_cards(self):
        self.set(QUESTION, gk("question"))
        self.assertEqual([(x["category"], x["kind"]) for x in self.pusher.payloads], [("HR_BLOCKED", "question")])
        p = self.app.api_prompt("g1")["prompt"]
        self.app.api_prompt_answer("g1", {"prompt_id": p["id"], "option_index": 1})
        self.app.api_prompt_answer("g1", {"prompt_id": p["id"], "text": "Kotlin please"})
        self.assertEqual(self.hc.keys_calls, [("g1", ["2"]), ("g1", ["z"])])
        self.assertEqual(self.hc.send_calls, [("g1", "Kotlin please", "force")])
        self.assertEqual(self.err(self.app.api_answer, "g1", {"action": "approve", "prompt_id": p["id"]}),
                         (409, "needs_manual"))
        self.hc.screens["g1"] = gk("question-custom-z")  # PC에서 z로 입력 칸을 열었다: 카드가 바뀐다
        self.assertIsNone(self.app.api_prompt("g1")["prompt"])
        self.assertEqual(self.err(self.app.api_prompt_answer, "g1", {"prompt_id": p["id"], "option_index": 0}),
                         (409, "prompt_changed"))
        self.set(QUESTION_MULTI, gk("question-multi"))  # 다른 질문 상자(여러 질문·복수 선택): 새 막힘, 카드 있음
        self.assertEqual((self.pusher.payloads[-1]["category"], self.pusher.payloads[-1].get("prompt_id")),
                         ("HR_BLOCKED", card("question-multi")["id"]))

    def test_multi_question_one_push_and_answers(self):
        # hc msg는 지금 질문 문장이라 탭을 넘기면 바뀐다(실측): 같은 상자면 새 막힘이 아니다(알림 한 번)
        self.set(QUESTION_MULTI, gk("question-multi"))
        p = self.app.api_prompt("g1")["prompt"]
        self.assertEqual([(x["category"], x["kind"], x["prompt_id"]) for x in self.pusher.payloads],
                         [("HR_BLOCKED", "question", p["id"])])
        self.assertEqual((p["multi_select"], p["title"]), (True, "1/2"))
        self.app.api_prompt_answer("g1", {"prompt_id": p["id"], "option_indices": [0, 2]})
        self.assertEqual(self.hc.keys_calls, [("g1", ["1", "3", "enter"])])
        self.set(QUESTION_LICENSE, gk("question-multi-q2"))
        self.assertEqual(len(self.pusher.payloads), 1)
        self.assertEqual(self.err(self.app.api_prompt_answer, "g1", {"prompt_id": p["id"], "option_indices": [1]}),
                         (409, "prompt_changed"))
        p2 = self.app.api_prompt("g1")["prompt"]
        self.assertEqual((p2["title"], p2["question"]), ("2/2", "Which license?"))
        self.app.api_prompt_answer("g1", {"prompt_id": p2["id"], "option_index": 1})
        self.assertEqual(self.hc.keys_calls[-1], ("g1", ["2"]))
        self.set(QUESTION_MULTI, gk("question-multi"))  # PC에서 ←로 돌아갔다: 같은 상자
        self.assertEqual(len(self.pusher.payloads), 1)
        self.set(WORKING, gk("working"))
        self.set(DONE, gk("done"), turn=2)
        self.set(QUESTION_COLOR, gk("question-tabs-single"))  # 다음 턴의 새 질문 상자: 다시 알린다
        self.assertEqual([x["state"] for x in self.pusher.payloads], ["blocked", "done", "blocked"])
        self.assertEqual(self.pusher.payloads[-1]["prompt_id"], card("question-tabs-single")["id"])
        self.set(QUESTION_MULTI, gk("question-tabs-multi-last"))
        self.assertEqual(len(self.pusher.payloads), 3)
        p3 = self.app.api_prompt("g1")["prompt"]
        self.assertEqual(self.err(self.app.api_prompt_answer, "g1", {"prompt_id": p3["id"], "option_indices": []}),
                         (400, "bad_option"))
        self.app.api_prompt_answer("g1", {"prompt_id": p3["id"], "option_indices": [1]})
        self.assertEqual(self.hc.keys_calls[-1], ("g1", ["2", "enter"]))

    def test_single_multi_select_question_is_its_own_dialog(self):
        # 질문 하나(탭 없음)는 묶음이 아니다: msg가 바뀌면 새 막힘(기존과 같다)
        self.set(QUESTION_MULTI, gk("question-multi-one"))
        self.set(QUESTION, gk("question"))
        self.assertEqual([x["kind"] for x in self.pusher.payloads], ["question", "question"])

    def test_text_only_question(self):
        self.set(QUESTION_TEXT, gk("question-text"))
        p = self.app.api_prompt("g1")["prompt"]
        self.app.api_prompt_answer("g1", {"prompt_id": p["id"], "text": "sheltie-demo"})
        self.assertEqual(self.hc.keys_calls, [("g1", ["enter"])])
        self.assertEqual(self.hc.send_calls, [("g1", "sheltie-demo", "force")])

    def test_plan_card(self):
        self.set(PERM_PLAN, gk("plan"))
        p = self.app.api_prompt("g1")["prompt"]
        self.assertEqual(p["kind"], "plan")
        self.assertEqual((self.pusher.payloads[-1]["kind"], self.pusher.payloads[-1]["category"]),
                         ("plan", "HR_BLOCKED"))
        self.assertEqual(self.err(self.app.api_answer, "g1", {"action": "approve", "prompt_id": p["id"]}),
                         (409, "needs_manual"))
        self.assertEqual(self.app.api_answer("g1", {"action": "reject", "prompt_id": p["id"]})["keys"], ["q"])
        self.app.api_prompt_answer("g1", {"prompt_id": p["id"], "option_index": 0})
        self.app.api_prompt_answer("g1", {"prompt_id": p["id"], "text": "add --shout"})
        self.assertEqual(self.hc.keys_calls, [("g1", ["q"]), ("g1", ["a"]), ("g1", ["s"])])
        self.assertEqual(self.hc.send_calls, [("g1", "add --shout", "force")])

    def test_trust_dialog_at_start(self):
        app, hc, pusher = make_app()
        hc.add("g2", dict(TRUST), agent="grok", name="new", turn=0)
        hc.screens["g2"] = gk("trust")
        app.poll(initial=True)
        pid = card("trust")["id"]
        self.assertEqual([(x["state"], x["category"], x["kind"], x["prompt_id"]) for x in pusher.payloads],
                         [("blocked", "HR_PERMISSION", "approval", pid)])
        self.assertEqual(app.api_answer("g2", {"action": "approve", "prompt_id": pid})["keys"], ["y"])
        self.assertEqual(app.api_answer("g2", {"action": "reject", "prompt_id": pid})["keys"], ["n"])
        self.assertEqual(self.err(app.api_answer, "g2", {"action": "always", "prompt_id": pid}), (409, "no_always"))
        self.assertEqual(self.err(app.api_send, "g2", {"text": "hello"}), (409, "hc_refused"))

    def test_send_refused_while_blocked(self):
        self.set(QUESTION, gk("question"))
        self.assertEqual(self.err(self.app.api_send, "g1", {"text": "hello"}), (409, "hc_refused"))
        self.assertEqual(self.hc.send_calls, [])
        self.set(DONE, gk("done"), turn=2)
        self.assertEqual(self.app.api_send("g1", {"text": "next"}), {"ok": True})

    def test_plain_working_and_done(self):
        self.app.screen_poll()  # 화면 판정 대상이 아니다
        self.assertEqual(self.pusher.payloads, [])
        self.set(DONE, gk("done"), turn=2)
        self.assertEqual([x["state"] for x in self.pusher.payloads], ["done"])
        self.assertEqual(self.later, [])
        self.assertIsNone(self.app.api_prompt("g1")["prompt"])
        self.assertEqual(self.app.api_keys("g1", {"keys": ["esc"]}), {"ok": True})
        by = {t["id"]: t for t in self.app.api_terminals()["terminals"]}
        self.assertEqual((by["g1"]["claude"], by["g1"]["agent_kind"], by["g1"]["status"]["state"]),
                         (True, "grok", "done"))
        self.assertEqual((by["sh"]["claude"], by["sh"]["agent_kind"]), (False, None))


class GrokTranscriptTests(unittest.TestCase):
    SID = "01a1fd15-5018-7ca3-aa5e-661d86258f96"
    OLD = "01a1fd00-0000-7000-8000-000000000000"
    CWD = "/Users/me/work/demo"

    def setUp(self):
        self.home = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)
        self.base = os.path.join(self.home, ".grok")
        self.cwd_dir = os.path.join(self.base, "sessions", urllib.parse.quote(self.CWD, safe=""))
        self.assertTrue(self.cwd_dir.endswith("%2FUsers%2Fme%2Fwork%2Fdemo"))
        os.makedirs(os.path.join(self.cwd_dir, self.SID))
        self.path = os.path.join(self.cwd_dir, self.SID, "updates.jsonl")
        shutil.copy(os.path.join(os.path.dirname(__file__), "fixtures", "grok-session-%s.jsonl" % V), self.path)
        os.makedirs(os.path.join(self.cwd_dir, self.OLD))
        self.old = os.path.join(self.cwd_dir, self.OLD, "updates.jsonl")
        with open(self.old, "w") as f:
            f.write(json.dumps(self.rec("user_message_chunk", content={"type": "text", "text": "old"})) + "\n")
        os.utime(self.old, (1, 1))
        with open(os.path.join(self.cwd_dir, "prompt_history.jsonl"), "w") as f:  # 세션 폴더가 아니다
            f.write("{}\n")
        other = os.path.join(self.base, "sessions", urllib.parse.quote("/Users/me/other", safe=""),
                             "aaaaaaaa-0000-0000-0000-000000000000")
        os.makedirs(other)
        with open(os.path.join(other, "updates.jsonl"), "w") as f:
            f.write("{}\n")
        self.env = dict(os.environ)
        os.environ.pop("GROK_HOME", None)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self.env)))

    @staticmethod
    def rec(kind, **kw):
        u = {"sessionUpdate": kind}
        u.update(kw)
        return {"timestamp": 1, "method": "session/update", "params": {"sessionId": "x", "update": u}}

    def test_parse(self):
        with open(self.path, encoding="utf-8") as f:
            turns, cut = hr.parse_transcript(f.read().split("\n"), 200)
        self.assertFalse(cut)
        self.assertEqual(turns[0], {"role": "user", "parts": [{"kind": "text", "text": "write hello.py"}]})
        tools = [p for t in turns for p in t["parts"] if p["kind"] == "tool"]
        self.assertEqual((tools[0]["name"], tools[0]["summary"], tools[0]["state"]),
                         ("write", "/Users/me/work/demo/hello.py", "ok"))
        sh = [p for p in tools if p["name"] == "run_terminal_command"][0]
        self.assertEqual((sh["summary"], sh["state"], sh["output"]), ("rm -f hello.py", "ok", "exit: 0\n"))
        asks = [p["output"] for p in tools if p["name"] == "ask_user_question"]
        self.assertIn('"Which language should the demo use?"="Go"', asks[0])
        self.assertIn('user notes: Kotlin please', asks[1])
        self.assertIn("User declined to answer the questions", "\n".join(asks))
        self.assertTrue(all(p["state"] == "ok" for p in tools))
        users = [p["text"] for t in turns if t["role"] == "user" for p in t["parts"]]
        self.assertEqual(users[:3], ["write hello.py", "edit hello", "delete hello"])
        self.assertEqual(len(users), 14)
        texts = [p["text"] for t in turns if t["role"] == "assistant" for p in t["parts"] if p["kind"] == "text"]
        self.assertIn("Wrote hello.py.", texts)
        self.assertTrue(all(t["role"] in ("user", "assistant") for t in turns))
        for a, b in zip(turns, turns[1:]):
            self.assertNotEqual(a["role"], b["role"])
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(hr.preview_from_lines(f.read().split("\n")), texts[-1])

    def test_chunks_are_joined(self):
        # 실제 모델은 답을 조각(chunk) 여러 줄로 쓴다: 이어 붙인다. 다음 사용자 입력(promptIndex)은 새 턴
        R = self.rec
        lines = [json.dumps(r) for r in (
            R("user_message_chunk", content={"type": "text", "text": "hi "}, _meta={"promptIndex": 0}),
            R("user_message_chunk", content={"type": "text", "text": "there"}, _meta={"promptIndex": 0}),
            R("agent_thought_chunk", content={"type": "text", "text": "hm"}),
            R("agent_thought_chunk", content={"type": "text", "text": "m"}),
            R("agent_message_chunk", content={"type": "text", "text": "Hel"}),
            R("agent_message_chunk", content={"type": "text", "text": "lo."}),
            R("tool_call", toolCallId="c1", title="run_terminal_command",
              rawInput={"command": "false", "description": "fail"}),
            R("tool_call_update", toolCallId="c1", status="completed",
              rawOutput={"type": "Bash", "output_for_prompt": "exit: 1\n", "exit_code": 1}),
            R("agent_message_chunk", content={"type": "text", "text": "Done."}),
            {"timestamp": 2, "method": "_x.ai/session/update",
             "params": {"update": {"sessionUpdate": "turn_completed"}}},
            R("user_message_chunk", content={"type": "text", "text": "again"}, _meta={"promptIndex": 1}),
        )]
        turns, _ = hr.parse_transcript(lines, 50)
        self.assertEqual(turns[0], {"role": "user", "parts": [{"kind": "text", "text": "hi there"}]})
        a = turns[1]["parts"]
        self.assertEqual([(p["kind"], p.get("text")) for p in a if p["kind"] != "tool"],
                         [("thinking", "hmm"), ("text", "Hello."), ("text", "Done.")])
        self.assertEqual([p["state"] for p in a if p["kind"] == "tool"], ["error"])
        self.assertEqual(turns[2], {"role": "user", "parts": [{"kind": "text", "text": "again"}]})

    def test_find_by_cwd(self):
        info = {"agent": "grok", "cwd": self.CWD}
        self.assertEqual(hr.find_agent_transcript(info, self.home), (self.SID, self.path))
        self.assertEqual(hr.find_agent_transcript({"agent": "grok", "cwd": "/nope"}, self.home), (None, None))

    def test_find_long_cwd_by_dot_cwd_file(self):
        # 255바이트가 넘는 cwd는 slug+hash 폴더 이름에 `.cwd` 파일로 원래 경로를 둔다
        long_cwd = "/Users/me/" + "x" * 300
        d = os.path.join(self.base, "sessions", "xxxx-1234abcd")
        os.makedirs(os.path.join(d, self.OLD))
        with open(os.path.join(d, ".cwd"), "w") as f:
            f.write(long_cwd)
        with open(os.path.join(d, self.OLD, "updates.jsonl"), "w") as f:
            f.write("{}\n")
        self.assertEqual(hr.find_agent_transcript({"agent": "grok", "cwd": long_cwd}, self.home)[0], self.OLD)

    def test_find_by_open_events_file(self):
        # 실행 중인 grok은 지금 세션의 events.jsonl을 열어 둔다(lsof): 같은 폴더의 더 최근 기록보다 먼저
        calls = []
        old_dir = os.path.dirname(self.old)

        class R:
            stdout = "p4242\nn/dev/ttys001\nn%s\nn%s\n" % (os.path.join(self.base, "logs", "unified.jsonl"),
                                                         os.path.join(old_dir, "events.jsonl"))

        def runner(argv, **kw):
            calls.append(argv)
            return R()
        info = {"agent": "grok", "cwd": "/elsewhere", "pid": 4242}
        self.assertEqual(hr.find_agent_transcript(info, self.home, runner=runner), (self.OLD, self.old))
        self.assertEqual(calls[0][1:], ["-a", "-p", "4242", "-Fn"])
        # 다른 곳의 events.jsonl은 무시하고 폴더의 최근 기록으로
        R.stdout = "n/tmp/x/%s/events.jsonl\n" % self.OLD
        self.assertEqual(hr.find_agent_transcript({"agent": "grok", "cwd": self.CWD, "pid": 4242}, self.home,
                                                  runner=runner), (self.SID, self.path))

    def test_grok_home_env(self):
        other = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, other, True)
        shutil.copytree(os.path.join(self.base, "sessions"), os.path.join(other, "sessions"))
        shutil.rmtree(self.base)
        os.environ["GROK_HOME"] = other
        self.assertEqual(hr.find_agent_transcript({"agent": "grok", "cwd": self.CWD}, self.home)[0], self.SID)

    def test_api_transcript(self):
        app, hc, _ = make_app(home=self.home)
        i = hc.add("g1", dict(IDLE), agent="grok")
        i["cwd"] = self.CWD
        r = app.api_transcript("g1", 50)
        self.assertEqual(r["session_id"], self.SID)
        self.assertEqual(r["turns"][0]["parts"][0]["text"], "write hello.py")
        by = {t["id"]: t for t in app.api_terminals()["terminals"]}
        self.assertEqual(by["g1"]["preview"], "Listed.")  # 마지막 답


if __name__ == "__main__":
    unittest.main()
