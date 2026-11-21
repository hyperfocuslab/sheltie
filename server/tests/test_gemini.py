"""Gemini CLI 지원. 화면은 실제 gemini-cli 0.70.0을 hc 안에서(가짜 모델 서버로) 캡처한 것.
Gemini는 OSC 7501 상태를 보내지 않아 hc status가 늘 비어 있다: 서버가 화면과 터미널 제목으로 막힘·작업·끝남을 판정한다."""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_herdr_remote import ev, fixture, make_app, hr  # noqa: E402

V = "0.70.0"
T_READY = "◇  Ready (demo)"
T_WORK = "✦  Working… (demo)"
T_ACTION = "✋  Action Required (demo)"


def gm(name):
    return fixture("gemini-%s-%s.txt" % (name, V))


def card(name):
    return hr.parse_prompt(gm(name), agent="gemini")


def labels(p):
    return [o["label"] for o in p["options"]]


class GeminiAgentKindTests(unittest.TestCase):
    def test_detects_gemini(self):
        self.assertEqual(hr.agent_kind({"agent": "gemini"}), "gemini")
        self.assertEqual(hr.agent_kind({"foreground": {"argv": ["node", "/opt/homebrew/bin/gemini"]}}), "gemini")
        self.assertEqual(hr.agent_kind({"foreground": {"argv": ["node", "--no-warnings", "/x/gemini", "-y"]}}),
                         "gemini")
        self.assertIsNone(hr.agent_kind({"foreground": {"argv": ["node", "/srv/server.js"]}}))
        self.assertIsNone(hr.agent_kind({"foreground": {"argv": ["gemini-helper"]}}))
        self.assertEqual(hr.agent_kind({"agent": "claude"}), "claude")


class GeminiParseTests(unittest.TestCase):
    def test_shell_approval(self):
        p = card("exec")
        self.assertEqual(p["kind"], "approval")
        self.assertEqual(p["title"], "Shell")
        self.assertEqual(p["question"], "Allow execution of [Shell]?")
        self.assertIn("mkdir -p build && echo hi > build/x.txt", p["body"])
        self.assertEqual(labels(p), ["Allow once", "Allow for this session", "No, suggest changes"])
        self.assertEqual([o["number"] for o in p["options"]], [1, 2, 3])
        self.assertEqual((p["selected"], p["always_index"], p["reject_index"]), (0, 1, 2))
        self.assertEqual(p["agent"], "gemini")
        self.assertEqual(card("exec2")["body"].strip(), "touch notes.txt")

    def test_file_approvals_hide_external_editor(self):
        for name, needle in (("write", 'print("hello")'), ("edit", "x = 2")):
            p = card(name)
            self.assertEqual(p["kind"], "approval")
            self.assertEqual(p["question"], "Apply this change?")
            self.assertIn(needle, p["body"])
            self.assertEqual(labels(p), ["Allow once", "Allow for this session", "No, suggest changes"])
            self.assertEqual([o["number"] for o in p["options"]], [1, 2, 4])
            self.assertEqual((p["always_index"], p["reject_index"]), (1, 2))
        self.assertEqual(card("write")["title"], "WriteFile")
        self.assertEqual(card("edit")["title"], "Edit")

    def test_enter_plan_mode_approval(self):
        p = card("enterplan")
        self.assertEqual(p["kind"], "approval")
        self.assertEqual(p["question"], "Do you want to proceed?")
        self.assertIn("read-only tools", p["body"])
        self.assertEqual(p["reject_index"], 2)

    def test_question(self):
        p = card("question")
        self.assertEqual(p["kind"], "question")
        self.assertEqual(p["question"], "Which database should we use?")
        self.assertEqual(labels(p), ["Postgres", "SQLite", "Enter a custom value"])
        self.assertEqual(p["options"][0]["description"], "Relational, mature")
        self.assertEqual(p["free_text_index"], 2)
        y = card("question-yesno")
        self.assertEqual((y["question"], labels(y)), ("Ship it this week?", ["Yes", "No", "Enter a custom value"]))

    def test_text_question(self):
        p = card("question-text")
        self.assertEqual(p["kind"], "question")
        self.assertEqual(p["question"], "What should the app be called?")
        self.assertEqual(p["free_text_index"], 0)
        self.assertEqual(p["options"][0]["number"], None)

    def test_plan(self):
        p = card("plan")
        self.assertEqual(p["kind"], "plan")
        self.assertEqual(p["question"], "Ready to start implementation?")
        self.assertIn("Add login", p["body"])
        self.assertIn("Run unit tests", p["body"])
        self.assertEqual(labels(p), ["Yes, automatically accept edits", "Yes, manually accept edits",
                                     "Type your feedback..."])
        self.assertEqual(p["free_text_index"], 2)

    def test_unknown_screens_have_no_card(self):
        # 여러 질문·복수 선택(목록이 스크롤돼 일부만 보인다), 입력창만 있는 화면
        self.assertIsNone(card("question-multi"))
        self.assertIsNone(card("idle"))

    def test_parsers_do_not_cross(self):
        for name in ("exec", "write", "question", "plan"):
            self.assertIsNone(hr.parse_prompt(gm(name), agent="codex"))
            self.assertIsNone(hr.parse_prompt(gm(name)))
        self.assertIsNone(hr.parse_prompt(fixture("codex-exec-0.171.0.txt"), agent="gemini"))
        self.assertIsNone(hr.parse_prompt(fixture("claude-bash-2.1.329.txt"), agent="gemini"))

    def test_answer_steps(self):
        self.assertEqual(hr.answer_steps(card("exec"), option_index=1), [("keys", ["2"])])
        self.assertEqual(hr.answer_steps(card("write"), option_index=2), [("keys", ["4"])])
        self.assertEqual(hr.answer_steps(card("question"), text="MongoDB"), [("keys", ["3"]), ("text", "MongoDB")])
        self.assertEqual(hr.answer_steps(card("question-text"), text="Sheltie"), [("text", "Sheltie")])
        self.assertEqual(hr.answer_steps(card("plan"), text="add tests"), [("keys", ["3"]), ("text", "add tests")])


def title_ev(tid, title):
    return ev(tid, "title", {"title": title})


class GeminiScreenStatusTests(unittest.TestCase):
    """hc status 없이 화면·제목으로 막힘·끝남 알림."""

    def setUp(self):
        self.app, self.hc, self.pusher = make_app()
        self.later = []
        self.app._later = lambda sec, fn: self.later.append((sec, fn))
        info = self.hc.add("g1", None, agent="gemini", name="gem", turn=0)
        info["title"] = T_READY
        self.hc.screens["g1"] = gm("idle")
        self.hc.add("sh", None, agent=None, argv=["/bin/zsh"])
        self.app.poll(initial=True)

    def title(self, t, screen=None):
        self.hc.infos["g1"]["title"] = t
        if screen is not None:
            self.hc.screens["g1"] = screen
        self.app.handle_event_line(title_ev("g1", t))

    def err(self, fn, *a):
        with self.assertRaises(hr.ApiError) as cm:
            fn(*a)
        return cm.exception.code, cm.exception.body["error"]

    def test_idle_start_sends_nothing(self):
        self.assertEqual(self.pusher.payloads, [])
        by = {t["id"]: t for t in self.app.api_terminals()["terminals"]}
        self.assertEqual((by["g1"]["claude"], by["g1"]["agent_kind"]), (True, "gemini"))
        self.assertEqual(by["g1"]["status"]["state"], "idle")

    def test_approval_push_card_and_answer(self):
        self.title(T_WORK)
        self.title(T_ACTION, gm("exec"))
        self.assertEqual([(x["state"], x["category"], x["kind"]) for x in self.pusher.payloads],
                         [("blocked", "HR_PERMISSION_ALWAYS", "approval")])
        self.assertEqual(self.pusher.payloads[0]["prompt_id"], card("exec")["id"])
        by = {t["id"]: t for t in self.app.api_terminals()["terminals"]}
        self.assertEqual(by["g1"]["status"]["state"], "blocked")
        self.assertIsNotNone(by["g1"]["blocked_since"])
        pid = card("exec")["id"]
        self.assertEqual(self.app.api_answer("g1", {"action": "approve", "prompt_id": pid})["keys"], ["1"])
        self.assertEqual(self.app.api_answer("g1", {"action": "always", "prompt_id": pid})["keys"], ["2"])
        self.assertEqual(self.app.api_answer("g1", {"action": "reject", "prompt_id": pid})["keys"], ["3"])
        # 옛 경로(prompt_id 없음)도 화면 판정 status로 답한다
        self.assertEqual(self.app.api_answer("g1", {"action": "approve"})["keys"], ["1"])
        # 같은 막힘이 이어지면 다시 알리지 않고, 끝나면 끝남 한 번
        self.title(T_ACTION)
        self.title(T_WORK, gm("idle"))
        self.title(T_READY)
        self.title(T_READY)
        self.assertEqual([x["state"] for x in self.pusher.payloads], ["blocked", "done"])

    def test_recheck_before_keys(self):
        self.title(T_ACTION, gm("exec"))
        pid = card("exec")["id"]
        self.hc.screens["g1"] = gm("exec2")
        self.assertEqual(self.err(self.app.api_answer, "g1", {"action": "approve", "prompt_id": pid}),
                         (409, "prompt_changed"))
        self.hc.infos["g1"]["title"] = T_WORK
        self.hc.screens["g1"] = gm("idle")
        self.assertIsNone(self.app.api_prompt("g1")["prompt"])
        self.assertEqual(self.err(self.app.api_answer, "g1", {"action": "approve"}), (409, "not_blocked"))
        self.assertEqual(self.hc.keys_calls, [])

    def test_question_and_plan_cards(self):
        self.title(T_ACTION, gm("question"))
        p = self.app.api_prompt("g1")["prompt"]
        self.app.api_prompt_answer("g1", {"prompt_id": p["id"], "option_index": 1})
        self.app.api_prompt_answer("g1", {"prompt_id": p["id"], "text": "MongoDB"})
        self.assertEqual(self.hc.keys_calls, [("g1", ["2"]), ("g1", ["3"])])
        self.assertEqual(self.hc.send_calls, [("g1", "MongoDB", "force")])
        self.assertEqual(self.err(self.app.api_answer, "g1", {"action": "approve", "prompt_id": p["id"]}),
                         (409, "needs_manual"))
        self.title(T_ACTION, gm("plan"))
        q = self.app.api_prompt("g1")["prompt"]
        self.assertEqual(q["kind"], "plan")
        self.assertEqual(self.app.api_answer("g1", {"action": "reject", "prompt_id": q["id"]})["keys"], ["esc"])
        self.assertEqual([(x["category"], x["kind"]) for x in self.pusher.payloads],
                         [("HR_BLOCKED", "question"), ("HR_BLOCKED", "plan")])

    def test_unknown_dialog_waits_then_blocks_without_card(self):
        self.title(T_ACTION, gm("question-multi"))
        self.assertEqual(self.pusher.payloads, [])  # 메뉴가 늦게 그려질 수 있어 잠깐 뒤 다시 본다
        self.assertEqual(len(self.later), 1)
        self.later.pop()[1]()
        self.assertEqual([(x["state"], x["category"], x["prompt_id"]) for x in self.pusher.payloads],
                         [("blocked", "HR_BLOCKED", None)])
        self.assertIsNone(self.app.api_prompt("g1")["prompt"])

    def test_typing_in_open_dialog_is_not_a_new_block(self):
        # 직접 입력 줄을 고르면 힌트가 "Enter to submit"으로 바뀌어 카드가 안 읽히지만 같은 상자다(실측)
        self.title(T_ACTION, gm("question"))
        self.hc.screens["g1"] = gm("question").replace("Enter to select · ↑/↓ to navigate · Esc to cancel",
                                                       "Enter to submit · Esc to cancel")
        self.app.screen_poll()
        self.assertEqual(self.later, [])
        self.assertEqual([x["kind"] for x in self.pusher.payloads], ["question"])

    def test_menu_drawn_late_gets_one_push(self):
        self.title(T_ACTION, gm("idle"))
        self.hc.screens["g1"] = gm("exec")
        self.later.pop()[1]()
        self.assertEqual([(x["category"], x["prompt_id"]) for x in self.pusher.payloads],
                         [("HR_PERMISSION_ALWAYS", card("exec")["id"])])

    def test_send_refused_while_dialog_open(self):
        self.title(T_ACTION, gm("exec"))
        self.assertEqual(self.err(self.app.api_send, "g1", {"text": "hello"}), (409, "hc_refused"))
        self.title(T_READY, gm("idle"))
        self.assertEqual(self.app.api_send("g1", {"text": "hello"}), {"ok": True})
        self.assertEqual(self.app.api_keys("g1", {"keys": ["esc"]}), {"ok": True})
        self.assertEqual(self.hc.send_calls, [("g1", "hello")])

    def test_poll_catches_menu_without_title_event(self):
        self.hc.infos["g1"]["title"] = T_ACTION
        self.hc.screens["g1"] = gm("exec")
        self.app.screen_poll()
        self.assertEqual([x["category"] for x in self.pusher.payloads], ["HR_PERMISSION_ALWAYS"])

    def test_claude_title_events_do_not_refresh(self):
        self.hc.add("c1", {"app": "claude-code", "state": "working"}, agent="claude")
        self.app.poll()
        calls = self.hc.read_calls
        self.app.handle_event_line(title_ev("c1", "✳ Thinking"))
        self.assertEqual(self.hc.read_calls, calls)


class GeminiTranscriptTests(unittest.TestCase):
    def setUp(self):
        self.home = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)
        g = os.path.join(self.home, ".gemini")
        self.chats = os.path.join(g, "tmp", "demo", "chats")
        os.makedirs(self.chats)
        with open(os.path.join(g, "projects.json"), "w") as f:
            json.dump({"projects": {"/Users/me/work/demo": "demo"}}, f)
        self.path = os.path.join(self.chats, "session-2026-11-21T07-25-0310d97f.jsonl")
        shutil.copy(os.path.join(os.path.dirname(__file__), "fixtures", "gemini-session-%s.jsonl" % V), self.path)
        old = os.path.join(self.chats, "session-2026-11-20T01-00-aaaaaaaa.jsonl")
        with open(old, "w") as f:
            f.write("{}\n")
        os.utime(old, (1, 1))
        self.env = dict(os.environ)
        os.environ.pop("GEMINI_CLI_HOME", None)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self.env)))

    def test_parse(self):
        with open(self.path, encoding="utf-8") as f:
            turns, cut = hr.parse_transcript(f.read().split("\n"), 200)
        self.assertFalse(cut)
        self.assertEqual(turns[0], {"role": "user", "parts": [{"kind": "text", "text": "hi"}]})
        self.assertEqual(turns[1]["role"], "assistant")
        self.assertEqual(turns[1]["parts"][0], {"kind": "text", "text": "Hi there."})
        tools = [p for t in turns for p in t["parts"] if p["kind"] == "tool"]
        self.assertEqual(tools[0]["name"], "run_shell_command")
        self.assertEqual(tools[0]["summary"], "ls -la")
        self.assertEqual(tools[0]["state"], "ok")
        self.assertIn("README.md", tools[0]["output"])
        self.assertEqual(tools[2]["state"], "error")  # 거절
        self.assertTrue(all("session_context" not in p.get("text", "") for t in turns for p in t["parts"]))
        texts = [p["text"] for t in turns for p in t["parts"] if p["kind"] == "text"]
        self.assertEqual(texts.count("Hi there."), 2)  # 같은 id의 갱신 줄을 두 번 세지 않는다

    def test_find_and_api(self):
        info = {"agent": "gemini", "cwd": "/Users/me/work/demo"}
        sid, path = hr.find_agent_transcript(info, self.home)
        self.assertEqual(path, self.path)
        self.assertEqual(sid, "0310d97f-14cd-424a-ba51-659360038a16")
        self.assertEqual(hr.find_agent_transcript({"agent": "gemini", "cwd": "/nope"}, self.home), (None, None))
        app, hc, _ = make_app(home=self.home)
        i = hc.add("g1", None, agent="gemini")
        i["cwd"] = "/Users/me/work/demo"
        r = app.api_transcript("g1", 50)
        self.assertEqual(r["session_id"], sid)
        self.assertEqual(r["turns"][0]["parts"][0]["text"], "hi")


if __name__ == "__main__":
    unittest.main()
