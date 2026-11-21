"""OpenCode 지원. 화면은 실제 opencode 1.19.6을 hc 안에서(가짜 모델 서버로) 캡처한 것(194열, 오른쪽 사이드바 있음).
OpenCode는 OSC 7501을 안 보내고 제목도 "OC | <세션 제목>"으로 고정이라 서버가 화면으로 막힘·작업·끝남을 판정한다.
승인 버튼은 번호가 없고 커서가 돌며 지금 고른 버튼이 화면 글자로 안 보여, 뜰 때 자리(Allow once) 기준 키로 답한다.
대화 기록은 SQLite(opencode.db)다."""
import json
import os
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
        # 평소 화면, 작업 중, 여러 질문·복수 선택·리뷰 탭, PC에서 직접 입력 칸을 연 질문, 답한 뒤
        for name in ("idle-start", "hello2", "idle-plan", "working", "question-multi", "question-multi-tab2",
                     "question-multi-confirm", "question-custom-typing", "question-dismissed", "write-reject",
                     "plan-exit-yes"):
            self.assertIsNone(card(name), name)

    def test_parsers_do_not_cross(self):
        for name in ("exec", "write", "exec-always-confirm", "question", "question-text", "plan-exit"):
            for agent in (None, "codex", "gemini", "copilot", "qwen"):
                self.assertIsNone(hr.parse_prompt(oc(name), agent=agent), (name, agent))
        for f in ("claude-bash-2.1.329.txt", "codex-exec-0.171.0.txt", "gemini-exec-0.70.0.txt",
                  "gemini-question-0.70.0.txt", "copilot-exec-1.0.114.txt", "copilot-question-1.0.114.txt",
                  "qwen-exec-0.28.3.txt", "qwen-question-0.28.3.txt"):
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
        # 여러 질문은 카드가 없지만 막힘(제목이 없어 힌트 줄로 안다). 다음 탭으로 넘어가도 같은 막힘
        self.screen("question-multi")
        self.assertEqual([(s, c) for s, c, _ in self.states()], [("blocked", "HR_BLOCKED")])
        self.assertIsNone(self.pusher.payloads[0]["prompt_id"])
        self.assertEqual(self.later, [])
        self.assertIsNone(self.app.api_prompt("o1")["prompt"])
        self.screen("question-multi-tab2")
        self.screen("question-multi-confirm")
        self.assertEqual(len(self.pusher.payloads), 1)
        self.screen("hello2")
        self.assertEqual([x["state"] for x in self.pusher.payloads], ["blocked", "done"])

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
