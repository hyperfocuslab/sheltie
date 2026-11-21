"""GitHub Copilot CLI 지원. 화면은 실제 copilot 1.0.114를 hc 안에서(BYOK 가짜 모델 서버로) 캡처한 것.
Copilot은 OSC 7501로 승인(permission)·질문·계획(question) 막힘을 보낸다. 폴더 신뢰(idle)·autopilot 권한(working) 상자는
hc가 모르므로 서버가 화면으로 덧씌운다."""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_herdr_remote import ev, fixture, make_app, hr  # noqa: E402

V = "1.0.114"
PERM = {"app": "copilot", "state": "blocked", "kind": "permission"}
QUESTION = {"app": "copilot", "state": "blocked", "kind": "question"}
IDLE = {"app": "copilot", "state": "idle"}
WORKING = {"app": "copilot", "state": "working"}
DONE = {"app": "copilot", "state": "done"}


def cp(name):
    return fixture("copilot-%s-%s.txt" % (name, V))


def card(name):
    return hr.parse_prompt(cp(name), agent="copilot")


def labels(p):
    return [o["label"] for o in p["options"]]


def status_ev(tid, st):
    return ev(tid, "status", {"status": st})


class CopilotAgentKindTests(unittest.TestCase):
    def test_detects_copilot(self):
        self.assertEqual(hr.agent_kind({"agent": "copilot"}), "copilot")
        self.assertEqual(hr.agent_kind({"status": {"app": "copilot", "state": "idle"}}), "copilot")
        self.assertEqual(hr.agent_kind({"foreground": {"argv": ["node", "/opt/homebrew/bin/copilot"]}}), "copilot")
        self.assertIsNone(hr.agent_kind({"foreground": {"argv": ["gh", "copilot"]}}))
        self.assertIsNone(hr.agent_kind({"foreground": {"argv": ["copilot-language-server"]}}))
        self.assertEqual(hr.agent_kind({"agent": "gemini"}), "gemini")


class CopilotParseTests(unittest.TestCase):
    def test_shell_approval(self):
        p = card("exec")
        self.assertEqual((p["kind"], p["agent"]), ("approval", "copilot"))
        self.assertEqual(p["title"], "Create notes.txt")
        self.assertEqual(p["question"], "Do you want to run this command?")
        self.assertEqual(p["body"], "touch notes.txt")
        self.assertEqual(labels(p), ["Yes", "Yes, and don't ask again for `touch` in this repo (~/work/demo)",
                                     "No, and tell Copilot what to do differently"])
        self.assertEqual([o["number"] for o in p["options"]], [1, 2, 3])
        # 2번은 permissions-config.json에 영구 저장(실측): 잠금화면 "계속 허용"에 붙이지 않는다
        self.assertEqual((p["selected"], p["always_index"], p["persist_index"]), (0, None, 1))
        # 3번은 입력 항목(숫자는 커서만): 거절 버튼은 esc, 이 줄은 직접 입력으로
        self.assertEqual((p["reject_index"], p["free_text_index"]), (None, 2))
        self.assertEqual(hr.push_category("blocked", PERM, p), "HR_PERMISSION")

    def test_file_approvals(self):
        for name, title, q, needle in (
                ("write", "Create file", "Do you want to create /Users/me/work/demo/hello.py?", 'print("hello")'),
                ("edit", "Edit file", "Do you want to edit /Users/me/work/demo/README.md?", "hello world")):
            p = card(name)
            self.assertEqual((p["kind"], p["title"], p["question"]), ("approval", title, q))
            self.assertIn(needle, p["body"])
            self.assertEqual(labels(p)[0], "Yes")
            self.assertEqual((p["always_index"], p["persist_index"], p["reject_index"], p["free_text_index"]),
                             (None, 1, None, 2))
        self.assertEqual(card("write-no")["selected"], 2)
        self.assertEqual(card("write-no")["id"], card("write")["id"])

    def test_trust(self):
        p = card("trust")
        self.assertEqual((p["kind"], p["title"]), ("approval", "Confirm folder trust"))
        self.assertEqual(p["question"], "Do you trust the files in this folder?")
        self.assertIn("/Users/me/work/demo", p["body"])
        self.assertEqual(labels(p), ["Yes", "Yes, and remember this folder for future sessions", "No"])
        # 2번은 config.json trustedFolders에 영구 저장(실측)
        self.assertEqual((p["always_index"], p["persist_index"], p["reject_index"], p["free_text_index"]),
                         (None, 1, 2, None))

    def test_question(self):
        p = card("question")
        self.assertEqual((p["kind"], p["title"], p["question"], p["body"]),
                         ("question", "Database", "Which database should we use?", "Pick the storage engine"))
        self.assertEqual(labels(p), ["PostgreSQL", "SQLite", "MySQL", "Other (type your answer)"])
        self.assertEqual([o["number"] for o in p["options"]], [None] * 4)
        self.assertEqual((p["selected"], p["free_text_index"]), (0, 3))
        o = card("question-other")  # 커서가 Other에 있고 아래 입력 칸이 열려 있다
        self.assertEqual((o["id"], o["selected"]), (p["id"], 3))
        y = card("question-yesno")
        self.assertEqual((y["title"], y["question"], labels(y)),
                         ("Proceed?", "Proceed with deploy?", ["Yes", "No", "Other (type your answer)"]))

    def test_text_question(self):
        p = card("question-text")
        self.assertEqual((p["kind"], p["title"], p["question"]),
                         ("question", "File name", "What should the file be called?"))
        self.assertEqual((labels(p), p["free_text_index"], p["selected"]), (["File name"], 0, None))

    def test_plan(self):
        p = card("plan")
        self.assertEqual((p["kind"], p["question"]), ("plan", "Plan Ready for Review"))
        self.assertIn("greet()", p["body"])
        self.assertIn("Add a test in test_hello.py", p["body"])
        self.assertTrue(labels(p)[0].startswith("Accept plan and continue in Interactive execution mode"))
        self.assertTrue(labels(p)[0].endswith("(recommended)"))
        self.assertEqual(labels(p)[3], "Suggest changes")
        self.assertEqual((p["free_text_index"], p["reject_index"], p["selected"]), (3, None, 0))
        self.assertEqual(card("plan-4")["selected"], 3)
        self.assertTrue(labels(card("plan-nofleet"))[0].startswith("Accept plan and continue in Autopilot"))
        self.assertEqual(hr.push_category("blocked", QUESTION, p), "HR_BLOCKED")

    def test_autopilot_permissions(self):
        p = card("autopilot-perm")
        self.assertEqual((p["kind"], p["title"], p["question"]), ("question", None, "Enable autopilot mode"))
        self.assertEqual(labels(p), ["Use Allow All for this session", "Use Assisted Permissions for this session",
                                     "Use Manual Approval for this session", "Cancel"])
        self.assertIn("Allow All permission policy", p["body"])
        self.assertIsNone(p["free_text_index"])

    def test_unknown_screens_have_no_card(self):
        # 여러 칸 폼(탭)·복수 선택, 3번에 PC에서 글자를 치는 중, 평소 화면
        for name in ("question-multi", "question-multi-tab2", "edit-3-typed", "idle", "working", "hello"):
            self.assertIsNone(card(name), name)

    def test_parsers_do_not_cross(self):
        for name in ("exec", "write", "trust", "question", "question-text", "plan", "autopilot-perm"):
            for agent in (None, "codex", "gemini"):
                self.assertIsNone(hr.parse_prompt(cp(name), agent=agent), (name, agent))
        for f in ("codex-exec-0.171.0.txt", "codex-plan-0.171.0.txt", "claude-bash-2.1.329.txt",
                  "claude-question-2.1.329.txt", "gemini-exec-0.70.0.txt", "gemini-question-0.70.0.txt",
                  "gemini-plan-0.70.0.txt"):
            self.assertIsNone(hr.parse_prompt(fixture(f), agent="copilot"), f)

    def test_answer_steps(self):
        self.assertEqual(hr.answer_steps(card("exec"), option_index=0), [("keys", ["1"])])
        self.assertEqual(hr.answer_steps(card("exec"), option_index=1), [("keys", ["2"])])
        self.assertEqual(hr.answer_steps(card("exec"), text="use hi.py"), [("keys", ["3"]), ("text", "use hi.py")])
        with self.assertRaises(hr.ApiError):
            hr.answer_steps(card("exec"), option_index=2)  # 입력 항목은 글이 있어야 한다
        self.assertEqual(hr.answer_steps(card("plan"), text="add docs"), [("keys", ["4"]), ("text", "add docs")])
        self.assertEqual(hr.answer_steps(card("plan"), option_index=2), [("keys", ["3"])])
        # 질문은 번호가 없어 숫자 키를 무시한다: 커서 이동 + enter
        self.assertEqual(hr.answer_steps(card("question"), option_index=1), [("keys", ["down", "enter"])])
        self.assertEqual(hr.answer_steps(card("question-other"), option_index=0),
                         [("keys", ["up", "up", "up", "enter"])])
        self.assertEqual(hr.answer_steps(card("question"), text="CockroachDB"),
                         [("keys", ["down", "down", "down"]), ("text", "CockroachDB")])
        self.assertEqual(hr.answer_steps(card("question-other"), text="CockroachDB"), [("text", "CockroachDB")])
        self.assertEqual(hr.answer_steps(card("question-text"), text="notes.md"), [("text", "notes.md")])
        self.assertEqual(hr.answer_steps(card("autopilot-perm"), option_index=2), [("keys", ["3"])])


class CopilotApiTests(unittest.TestCase):
    def setUp(self):
        self.app, self.hc, self.pusher = make_app()
        self.later = []
        self.app._later = lambda sec, fn: self.later.append((sec, fn))
        self.hc.add("p1", dict(WORKING), agent="copilot", name="cop", turn=1)
        self.hc.screens["p1"] = cp("working")
        self.hc.add("sh", None, agent=None, argv=["/bin/zsh"])
        self.app.poll(initial=True)

    def err(self, fn, *a):
        with self.assertRaises(hr.ApiError) as cm:
            fn(*a)
        return cm.exception.code, cm.exception.body["error"]

    def set(self, st, screen=None, turn=None):
        if screen is not None:
            self.hc.screens["p1"] = screen
        self.hc.set_status("p1", st, turn=turn)
        self.app.handle_event_line(status_ev("p1", st))

    def test_approval_push_card_and_answers(self):
        self.set(PERM, cp("exec"))
        self.assertEqual([(x["state"], x["category"], x["kind"], x["prompt_id"]) for x in self.pusher.payloads],
                         [("blocked", "HR_PERMISSION", "approval", card("exec")["id"])])
        pid = card("exec")["id"]
        self.assertEqual(self.app.api_prompt("p1")["prompt"]["id"], pid)
        self.assertEqual(self.app.api_answer("p1", {"action": "approve", "prompt_id": pid})["keys"], ["1"])
        self.assertEqual(self.err(self.app.api_answer, "p1", {"action": "always", "prompt_id": pid}),
                         (409, "no_always"))
        self.assertEqual(self.app.api_answer("p1", {"action": "reject", "prompt_id": pid})["keys"], ["esc"])
        self.app.api_prompt_answer("p1", {"prompt_id": pid, "text": "use hi.py instead"})
        self.app.api_prompt_answer("p1", {"prompt_id": pid, "option_index": 1})  # 영구 허용은 카드에서 직접 고를 때만
        self.assertEqual(self.hc.keys_calls, [("p1", ["1"]), ("p1", ["esc"]), ("p1", ["3"]), ("p1", ["2"])])
        self.assertEqual(self.hc.send_calls, [("p1", "use hi.py instead", "force")])
        # 옛 경로(prompt_id 없음): hc status에 msg가 없어 승인은 앱에서, 거절은 esc
        self.assertEqual(self.err(self.app.api_answer, "p1", {"action": "approve"}), (409, "needs_manual"))

    def test_recheck_before_keys(self):
        self.set(PERM, cp("write"))
        pid = card("write")["id"]
        self.hc.screens["p1"] = cp("edit")
        self.assertEqual(self.err(self.app.api_answer, "p1", {"action": "approve", "prompt_id": pid}),
                         (409, "prompt_changed"))
        self.hc.screens["p1"] = cp("edit-3-typed")  # PC에서 거절 지시를 치는 중
        self.assertIsNone(self.app.api_prompt("p1")["prompt"])
        self.set(DONE, cp("hello"), turn=2)
        self.assertIsNone(self.app.api_prompt("p1")["prompt"])
        self.assertEqual(self.err(self.app.api_answer, "p1", {"action": "approve", "prompt_id": pid}),
                         (409, "prompt_changed"))
        self.assertEqual(self.hc.keys_calls, [])

    def test_question_cards(self):
        self.set(QUESTION, cp("question"))
        self.assertEqual([(x["category"], x["kind"]) for x in self.pusher.payloads], [("HR_BLOCKED", "question")])
        p = self.app.api_prompt("p1")["prompt"]
        self.app.api_prompt_answer("p1", {"prompt_id": p["id"], "option_index": 1})
        self.app.api_prompt_answer("p1", {"prompt_id": p["id"], "text": "CockroachDB"})
        self.assertEqual(self.hc.keys_calls, [("p1", ["down", "enter"]), ("p1", ["down", "down", "down"])])
        self.assertEqual(self.hc.send_calls, [("p1", "CockroachDB", "force")])
        self.assertEqual(self.err(self.app.api_answer, "p1", {"action": "approve", "prompt_id": p["id"]}),
                         (409, "needs_manual"))
        self.hc.screens["p1"] = cp("question-text")
        q = self.app.api_prompt("p1")["prompt"]
        self.app.api_prompt_answer("p1", {"prompt_id": q["id"], "text": "notes.md"})
        self.assertEqual(self.hc.send_calls[-1], ("p1", "notes.md", "force"))
        self.hc.screens["p1"] = cp("question-multi")  # 여러 칸 폼: 막힘이지만 카드 없음
        self.assertIsNone(self.app.api_prompt("p1")["prompt"])

    def test_push_waits_for_question_drawn_after_blocked(self):
        # 실측: ask_user 상자가 blocked 이벤트보다 조금 늦게 그려질 때가 있다
        screens = [cp("working"), cp("working")]
        real_read = self.hc.read

        def read(tid, lines=0):
            if screens:
                return {"text": screens.pop(0)}
            return real_read(tid, lines)
        self.hc.read = read
        self.set(QUESTION, cp("question"))
        p = self.pusher.payloads[-1]
        self.assertEqual((p["category"], p["kind"], p["prompt_id"]), ("HR_BLOCKED", "question", card("question")["id"]))

    def test_plan_card(self):
        self.set(QUESTION, cp("plan"))
        p = self.app.api_prompt("p1")["prompt"]
        self.assertEqual(p["kind"], "plan")
        self.assertEqual(self.pusher.payloads[-1]["kind"], "plan")
        self.assertEqual(self.err(self.app.api_answer, "p1", {"action": "approve", "prompt_id": p["id"]}),
                         (409, "needs_manual"))
        self.assertEqual(self.app.api_answer("p1", {"action": "reject", "prompt_id": p["id"]})["keys"], ["esc"])
        self.app.api_prompt_answer("p1", {"prompt_id": p["id"], "option_index": 0})
        self.app.api_prompt_answer("p1", {"prompt_id": p["id"], "text": "also add docs"})
        self.assertEqual(self.hc.keys_calls, [("p1", ["esc"]), ("p1", ["1"]), ("p1", ["4"])])
        self.assertEqual(self.hc.send_calls, [("p1", "also add docs", "force")])

    def test_trust_dialog_while_idle_is_blocked(self):
        # 시작 화면 폴더 신뢰: hc는 idle. 재시작 직후 첫 폴링(initial)에도 알린다
        app, hc, pusher = make_app()
        hc.add("p2", dict(IDLE), agent="copilot", name="new", turn=0)
        hc.screens["p2"] = cp("trust")
        app.poll(initial=True)
        self.assertEqual([(x["state"], x["category"], x["kind"], x["prompt_id"]) for x in pusher.payloads],
                         [("blocked", "HR_PERMISSION", "approval", card("trust")["id"])])
        by = {t["id"]: t for t in app.api_terminals()["terminals"]}
        self.assertEqual((by["p2"]["status"]["state"], by["p2"]["agent_kind"]), ("blocked", "copilot"))
        self.assertIsNotNone(by["p2"]["blocked_since"])
        pid = card("trust")["id"]
        self.assertEqual(app.api_answer("p2", {"action": "approve", "prompt_id": pid})["keys"], ["1"])
        self.assertEqual(app.api_answer("p2", {"action": "approve"})["keys"], ["1"])  # 옛 경로: "approve Confirm…"
        self.assertEqual(app.api_answer("p2", {"action": "reject", "prompt_id": pid})["keys"], ["3"])
        with self.assertRaises(hr.ApiError) as cm:
            app.api_send("p2", {"text": "hello"})  # hc는 상자를 몰라 글자가 신뢰 메뉴에 들어간다
        self.assertEqual((cm.exception.code, cm.exception.body["error"]), (409, "hc_refused"))
        # 답한 뒤 상자가 사라지면 평소 idle: 끝남 알림도 없다
        hc.screens["p2"] = cp("idle")
        app.poll()
        self.assertIsNone(app.api_prompt("p2")["prompt"])
        self.assertEqual(app.api_send("p2", {"text": "hello"}), {"ok": True})
        self.assertEqual(len(pusher.payloads), 1)

    def test_autopilot_dialog_while_working_found_by_screen_poll(self):
        # 계획 수락(Autopilot) 뒤 "Enable autopilot mode": hc는 working 그대로라 상태 이벤트가 없다
        self.hc.screens["p1"] = cp("autopilot-perm")
        self.app.screen_poll()
        self.assertEqual([(x["state"], x["category"], x["kind"], x["prompt_id"]) for x in self.pusher.payloads],
                         [("blocked", "HR_BLOCKED", "question", card("autopilot-perm")["id"])])
        p = self.app.api_prompt("p1")["prompt"]
        self.app.api_prompt_answer("p1", {"prompt_id": p["id"], "option_index": 2})
        self.assertEqual(self.hc.keys_calls, [("p1", ["3"])])
        self.assertEqual(self.err(self.app.api_send, "p1", {"text": "x"}), (409, "hc_refused"))
        self.hc.screens["p1"] = cp("working")
        self.app.screen_poll()
        self.set(DONE, cp("hello"), turn=2)
        self.assertEqual([x["state"] for x in self.pusher.payloads], ["blocked", "done"])

    def test_plain_working_and_done(self):
        self.app.screen_poll()
        self.assertEqual(self.pusher.payloads, [])
        self.set(DONE, cp("hello"), turn=2)
        self.assertEqual([x["state"] for x in self.pusher.payloads], ["done"])
        self.assertEqual(self.later, [])
        self.assertEqual(self.app.api_send("p1", {"text": "next"}), {"ok": True})
        self.assertEqual(self.app.api_keys("p1", {"keys": ["esc"]}), {"ok": True})
        by = {t["id"]: t for t in self.app.api_terminals()["terminals"]}
        self.assertEqual((by["p1"]["claude"], by["p1"]["agent_kind"]), (True, "copilot"))
        self.assertEqual((by["sh"]["claude"], by["sh"]["agent_kind"]), (False, None))

    def test_codex_overlay_unchanged_by_copilot(self):
        # 덧씌우기 일반화 뒤에도 Codex는 계획 메뉴만, Copilot 신뢰 상자 모양을 Codex 화면으로 보지 않는다
        self.hc.add("c1", {"app": "codex", "state": "done"}, agent="codex")
        self.hc.screens["c1"] = cp("trust")
        self.assertIsNone(self.app.api_prompt("c1")["prompt"])
        self.assertEqual(self.app.api_send("c1", {"text": "x"}), {"ok": True})


class CopilotTranscriptTests(unittest.TestCase):
    SID = "21ce3bcb-0f15-4e2e-83f3-eb0c816a3a3e"
    OLD = "7c2bfb18-8d63-480f-bc2b-849d7e0b1842"

    def setUp(self):
        self.home = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)
        self.base = os.path.join(self.home, ".copilot", "session-state")
        for sid, cwd, mtime in ((self.SID, "/Users/me/work/demo", None), (self.OLD, "/Users/me/work/demo", 1),
                                ("aaaaaaaa-0000-0000-0000-000000000000", "/Users/me/other", None)):
            d = os.path.join(self.base, sid)
            os.makedirs(d)
            with open(os.path.join(d, "workspace.yaml"), "w") as f:
                f.write("id: %s\ncwd: %s\nname: hello\n" % (sid, cwd))
            path = os.path.join(d, "events.jsonl")
            if sid == self.SID:
                shutil.copy(os.path.join(os.path.dirname(__file__), "fixtures", "copilot-session-%s.jsonl" % V), path)
            else:
                with open(path, "w") as f:
                    f.write('{"type":"session.start","data":{}}\n')
            if mtime:
                os.utime(path, (mtime, mtime))
        self.path = os.path.join(self.base, self.SID, "events.jsonl")
        self.env = dict(os.environ)
        os.environ.pop("COPILOT_HOME", None)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self.env)))

    def test_parse(self):
        with open(self.path, encoding="utf-8") as f:
            turns, cut = hr.parse_transcript(f.read().split("\n"), 200)
        self.assertFalse(cut)
        self.assertEqual(turns[0], {"role": "user", "parts": [{"kind": "text", "text": "hello"}]})
        self.assertEqual(turns[1], {"role": "assistant", "parts": [{"kind": "text", "text": "Hi from mock."}]})
        tools = {p["input"].get("command") or p["input"].get("path") or p["input"].get("message"): p
                 for t in turns for p in t["parts"] if p["kind"] == "tool"}
        sh = tools["touch notes.txt"]
        self.assertEqual((sh["name"], sh["summary"], sh["state"]), ("bash", "touch notes.txt", "ok"))
        rejected = [p for t in turns for p in t["parts"] if p["kind"] == "tool" and p["state"] == "error"]
        self.assertIn("use hi.py instead", rejected[0]["output"])
        asks = [p["output"] for t in turns for p in t["parts"] if p["kind"] == "tool" and p["name"] == "ask_user"]
        self.assertEqual(asks[:2], ["User responded: sqlite", "User responded: CockroachDB"])
        self.assertEqual(tools["Which database should we use?"]["summary"], "Which database should we use?")
        users = [p["text"] for t in turns if t["role"] == "user" for p in t["parts"]]
        self.assertIn("plan greet", users)
        self.assertNotIn("", users)  # Copilot이 넣은 빈 재촉 메시지는 숨긴다
        self.assertTrue(all("current_datetime" not in u for u in users))
        self.assertEqual(hr.preview_from_lines(open(self.path, encoding="utf-8").read().split("\n")), "Slow hello.")

    def test_find_by_cwd_and_lock(self):
        info = {"agent": "copilot", "cwd": "/Users/me/work/demo", "pid": 4242}
        self.assertEqual(hr.find_agent_transcript(info, self.home), (self.SID, self.path))
        self.assertEqual(hr.find_agent_transcript({"agent": "copilot", "cwd": "/nope"}, self.home), (None, None))
        # 살아 있는 세션은 inuse.<pid>.lock으로 정확히 찾는다(같은 폴더의 더 최근 세션보다 먼저)
        open(os.path.join(self.base, self.OLD, "inuse.4242.lock"), "w").close()
        self.assertEqual(hr.find_agent_transcript(info, self.home)[0], self.OLD)
        fg = {"agent": "copilot", "cwd": "/x", "foreground": {"argv": ["node", "copilot"], "pid": 4242, "cwd": "/x"}}
        self.assertEqual(hr.find_agent_transcript(fg, self.home)[0], self.OLD)
        # npm 설치: node 런처(hc pid)가 띄운 자식 바이너리가 잠금을 잡는다(실측 1.0.114)
        os.rename(os.path.join(self.base, self.OLD, "inuse.4242.lock"), os.path.join(self.base, self.OLD, "inuse.4243.lock"))
        calls = []

        class R:
            stdout = "4243\n"

        def runner(argv, **kw):
            calls.append(argv)
            return R()
        self.assertEqual(hr.find_agent_transcript(info, self.home, runner=runner)[0], self.OLD)
        self.assertEqual(calls[0][1:], ["-P", "4242"])
        R.stdout = ""  # 다른 터미널의 잠금이면 폴더로 찾는다
        self.assertEqual(hr.find_agent_transcript(dict(info, pid=1), self.home, runner=runner)[0], self.SID)

    def test_copilot_home_env(self):
        other = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, other, True)
        shutil.copytree(self.base, os.path.join(other, "session-state"))
        shutil.rmtree(self.base)
        os.environ["COPILOT_HOME"] = other
        info = {"agent": "copilot", "cwd": "/Users/me/work/demo"}
        self.assertEqual(hr.find_agent_transcript(info, self.home)[0], self.SID)

    def test_api_transcript(self):
        app, hc, _ = make_app(home=self.home)
        i = hc.add("p1", dict(IDLE), agent="copilot")
        i["cwd"] = "/Users/me/work/demo"
        r = app.api_transcript("p1", 50)
        self.assertEqual(r["session_id"], self.SID)
        self.assertEqual(r["turns"][0]["parts"][0]["text"], "hello")


if __name__ == "__main__":
    unittest.main()
