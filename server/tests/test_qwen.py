"""Qwen Code 지원. 화면은 실제 qwen-code 0.28.3을 hc 안에서(가짜 모델 서버로) 캡처한 것.
Qwen은 Gemini CLI 포크지만 OSC 7501로 승인(permission)·질문(question)·폴더 신뢰(permission) 막힘을 보낸다:
hc status를 그대로 쓰고 화면 덧씌우기는 없다. 승인·질문 메뉴는 상자 없이 `? <도구> … ←` 줄 아래에 그려진다."""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_herdr_remote import ev, fixture, make_app, hr  # noqa: E402

V = "0.28.3"
APP = "qwen-code"
PERM = {"app": APP, "state": "blocked", "kind": "permission", "msg": "Permission needed: Shell"}
PERM_WRITE = {"app": APP, "state": "blocked", "kind": "permission", "msg": "Permission needed: WriteFile"}
PERM_PLAN = {"app": APP, "state": "blocked", "kind": "permission", "msg": "Permission needed: ExitPlanMode"}
QUESTION = {"app": APP, "state": "blocked", "kind": "question", "msg": "Waiting for your answer"}
TRUST = {"app": APP, "state": "blocked", "kind": "permission", "msg": "Approval needed"}
IDLE = {"app": APP, "state": "idle"}
WORKING = {"app": APP, "state": "working"}
DONE = {"app": APP, "state": "done"}


def qw(name):
    return fixture("qwen-%s-%s.txt" % (name, V))


def card(name):
    return hr.parse_prompt(qw(name), agent="qwen")


def labels(p):
    return [o["label"] for o in p["options"]]


def status_ev(tid, st):
    return ev(tid, "status", {"status": st})


class QwenAgentKindTests(unittest.TestCase):
    def test_detects_qwen(self):
        self.assertEqual(hr.agent_kind({"agent": "qwen"}), "qwen")
        self.assertEqual(hr.agent_kind({"status": {"app": "qwen-code", "state": "idle"}}), "qwen")
        self.assertEqual(hr.agent_kind({"foreground": {"argv": ["node", "/opt/homebrew/bin/qwen", "--approval-mode",
                                                                 "default"]}}), "qwen")
        # Qwen이 메모리 옵션을 붙여 자기 자신을 다시 띄운 자식(실측)
        self.assertEqual(hr.agent_kind({"foreground": {"argv": [
            "/opt/homebrew/bin/node", "--expose-gc",
            "/opt/homebrew/lib/node_modules/@qwen-code/qwen-code/cli.js", "--approval-mode", "default"]}}), "qwen")
        self.assertIsNone(hr.agent_kind({"foreground": {"argv": ["node", "/x/cli.js"]}}))
        self.assertIsNone(hr.agent_kind({"foreground": {"argv": ["qwen-tool"]}}))
        self.assertEqual(hr.agent_kind({"agent": "gemini"}), "gemini")
        self.assertEqual(hr.agent_kind({"agent": "copilot"}), "copilot")


class QwenParseTests(unittest.TestCase):
    def test_shell_approval(self):
        p = card("exec")
        self.assertEqual((p["kind"], p["agent"]), ("approval", "qwen"))
        self.assertEqual((p["title"], p["question"]), ("Shell", "Allow execution of: 'touch'?"))
        self.assertEqual(p["body"], "touch notes.txt (Create notes.txt)\n\ntouch notes.txt")
        self.assertEqual(labels(p), ["Yes, allow once", "Always allow run 'touch *' commands in this project",
                                     "Always allow run 'touch *' commands for this user", "No, suggest changes"])
        self.assertEqual([o["number"] for o in p["options"]], [1, 2, 3, 4])
        # 2·3번은 <cwd>/.qwen/settings.json·$QWEN_HOME/settings.json에 영구 저장(실측): 잠금화면 "계속 허용"에 안 붙인다
        self.assertEqual((p["selected"], p["approve_index"], p["always_index"], p["persist_index"]), (0, 0, None, 1))
        self.assertEqual((p["reject_index"], p["free_text_index"]), (3, None))
        self.assertEqual(hr.push_category("blocked", PERM, p), "HR_PERMISSION")
        p2 = card("exec2")
        self.assertEqual(p2["question"], "Allow execution of: 'python3'?")
        self.assertIn('python3 -c "import time; time.sleep(8)', p2["body"])
        self.assertEqual(labels(p2)[1], "Always allow run 'python3 *' commands in this project")

    def test_file_approvals(self):
        for name, title, needles in (("write", "WriteFile", ("Writing to hello.txt", "1 hello\n2 world")),
                                     ("edit", "Edit", ("hello.txt", "2 - world", "2 + qwen"))):
            p = card(name)
            self.assertEqual((p["kind"], p["title"], p["question"]), ("approval", title, "Apply this change?"))
            for n in needles:
                self.assertIn(n, p["body"])
            self.assertEqual(labels(p), ["Yes, allow once", "Yes, allow always", "No, suggest changes"])
            # "Yes, allow always"는 파일 저장 없이 이 세션만 auto-accept edits로 바뀐다(실측): 세션 한정 허용
            self.assertEqual((p["approve_index"], p["always_index"], p["persist_index"], p["reject_index"]),
                             (0, 1, None, 2))
            self.assertEqual(hr.push_category("blocked", PERM_WRITE, p), "HR_PERMISSION_ALWAYS")

    def test_trust(self):
        p = card("trust")
        self.assertEqual((p["kind"], p["question"]), ("approval", "Do you trust this folder?"))
        self.assertIn("Trusting a folder allows Qwen Code to execute commands", p["body"])
        self.assertEqual(labels(p), ["Trust folder (demo)", "Trust parent folder (lab)", "Don't trust"])
        self.assertEqual((p["approve_index"], p["always_index"], p["persist_index"], p["reject_index"]),
                         (0, None, 1, 2))
        self.assertEqual(hr.push_category("blocked", TRUST, p), "HR_PERMISSION")

    def test_plan(self):
        p = card("plan")
        self.assertEqual((p["kind"], p["title"], p["question"]), ("plan", "ExitPlanMode", "Would you like to proceed?"))
        self.assertIn("1. Add greet.py with a greet() function", p["body"])
        self.assertIn("3. Run the test", p["body"])
        self.assertNotIn("open full plan in editor", p["body"])  # PC 편집기 열기 안내
        self.assertEqual(labels(p), ["Yes, restore previous mode (yolo)", "Yes, and auto-accept edits",
                                     "Yes, and manually approve edits", "No, keep planning"])
        self.assertEqual((p["selected"], p["reject_index"], p["free_text_index"]), (0, 3, None))
        self.assertEqual(hr.push_category("blocked", PERM_PLAN, p), "HR_BLOCKED")

    def test_question(self):
        p = card("question")
        self.assertEqual((p["kind"], p["title"], p["question"]), ("question", "Database",
                                                                 "Which database should we use?"))
        self.assertEqual(labels(p), ["SQLite", "Postgres", "DuckDB", "Type something..."])
        self.assertEqual([o["description"] for o in p["options"]],
                         ["File-based, zero setup", "Server, full featured", "Analytics oriented", None])
        self.assertEqual([o["number"] for o in p["options"]], [1, 2, 3, 4])
        self.assertEqual((p["selected"], p["free_text_index"], p["multi_select"]), (0, 3, False))

    def test_unknown_screens_have_no_card(self):
        # PC에서 직접 입력 칸을 연 상태, 여러 질문(탭)·복수 선택·제출 탭, 평소 화면, 작업 중, 피드백 줄
        for name in ("question-other", "question-multi", "question-multi-tab2", "question-submit", "idle",
                     "idle-auto", "working", "enterplan", "feedback"):
            self.assertIsNone(card(name), name)

    def test_parsers_do_not_cross(self):
        for name in ("exec", "write", "edit", "trust", "question", "plan"):
            for agent in (None, "codex", "gemini", "copilot"):
                self.assertIsNone(hr.parse_prompt(qw(name), agent=agent), (name, agent))
        for f in ("codex-exec-0.171.0.txt", "codex-plan-0.171.0.txt", "claude-bash-2.1.329.txt",
                  "claude-question-2.1.329.txt", "claude-plan-2.1.329.txt", "gemini-exec-0.70.0.txt",
                  "gemini-question-0.70.0.txt", "gemini-plan-0.70.0.txt", "copilot-exec-1.0.114.txt",
                  "copilot-trust-1.0.114.txt", "copilot-question-1.0.114.txt", "copilot-plan-1.0.114.txt"):
            self.assertIsNone(hr.parse_prompt(fixture(f), agent="qwen"), f)

    def test_answer_steps(self):
        self.assertEqual(hr.answer_steps(card("exec"), option_index=0), [("keys", ["1"])])
        self.assertEqual(hr.answer_steps(card("exec"), option_index=3), [("keys", ["4"])])
        with self.assertRaises(hr.ApiError):
            hr.answer_steps(card("exec"), text="use hi.py")  # 승인에는 직접 입력이 없다(4번은 esc와 같다)
        self.assertEqual(hr.answer_steps(card("write"), option_index=1), [("keys", ["2"])])
        self.assertEqual(hr.answer_steps(card("plan"), option_index=2), [("keys", ["3"])])
        # 단일 질문: 숫자 키가 바로 고르고 제출, 직접 입력은 숫자 → 붙여넣기+enter(실측)
        self.assertEqual(hr.answer_steps(card("question"), option_index=1), [("keys", ["2"])])
        self.assertEqual(hr.answer_steps(card("question"), text="MySQL 8"), [("keys", ["4"]), ("text", "MySQL 8")])
        with self.assertRaises(hr.ApiError):
            hr.answer_steps(card("question"), option_index=3)  # 직접 입력 줄은 글이 있어야 한다
        self.assertEqual(hr.answer_steps(card("trust"), option_index=0), [("keys", ["1"])])


class QwenApiTests(unittest.TestCase):
    def setUp(self):
        self.app, self.hc, self.pusher = make_app()
        self.later = []
        self.app._later = lambda sec, fn: self.later.append((sec, fn))
        self.hc.add("q1", dict(WORKING), agent="qwen", name="qw", turn=1)
        self.hc.screens["q1"] = qw("working")
        self.hc.add("sh", None, agent=None, argv=["/bin/zsh"])
        self.app.poll(initial=True)

    def err(self, fn, *a):
        with self.assertRaises(hr.ApiError) as cm:
            fn(*a)
        return cm.exception.code, cm.exception.body["error"]

    def set(self, st, screen=None, turn=None):
        if screen is not None:
            self.hc.screens["q1"] = screen
        self.hc.set_status("q1", st, turn=turn)
        self.app.handle_event_line(status_ev("q1", st))

    def test_approval_push_card_and_answers(self):
        self.set(PERM, qw("exec"))
        pid = card("exec")["id"]
        self.assertEqual([(x["state"], x["category"], x["kind"], x["prompt_id"], x["msg"]) for x in self.pusher.payloads],
                         [("blocked", "HR_PERMISSION", "approval", pid, "Permission needed: Shell")])
        self.assertEqual(self.app.api_prompt("q1")["prompt"]["id"], pid)
        self.assertEqual(self.app.api_answer("q1", {"action": "approve", "prompt_id": pid})["keys"], ["1"])
        self.assertEqual(self.err(self.app.api_answer, "q1", {"action": "always", "prompt_id": pid}),
                         (409, "no_always"))
        self.assertEqual(self.app.api_answer("q1", {"action": "reject", "prompt_id": pid})["keys"], ["4"])
        self.app.api_prompt_answer("q1", {"prompt_id": pid, "option_index": 1})  # 영구 허용은 카드에서 직접 고를 때만
        self.assertEqual(self.err(self.app.api_prompt_answer, "q1", {"prompt_id": pid, "text": "x"}),
                         (400, "no_free_text"))
        self.assertEqual(self.hc.keys_calls, [("q1", ["1"]), ("q1", ["4"]), ("q1", ["2"])])
        self.assertEqual(self.hc.send_calls, [])
        # 옛 경로(prompt_id 없음): msg가 "approve …"가 아니라 승인은 앱에서, 거절은 esc
        self.assertEqual(self.err(self.app.api_answer, "q1", {"action": "approve"}), (409, "needs_manual"))
        self.assertEqual(self.app.api_answer("q1", {"action": "reject"})["keys"], ["esc"])

    def test_session_always_for_file_edits(self):
        self.set(PERM_WRITE, qw("write"))
        self.assertEqual(self.pusher.payloads[-1]["category"], "HR_PERMISSION_ALWAYS")
        pid = card("write")["id"]
        self.assertEqual(self.app.api_answer("q1", {"action": "always", "prompt_id": pid})["keys"], ["2"])

    def test_recheck_before_keys(self):
        self.set(PERM_WRITE, qw("write"))
        pid = card("write")["id"]
        self.hc.screens["q1"] = qw("edit")
        self.assertEqual(self.err(self.app.api_answer, "q1", {"action": "approve", "prompt_id": pid}),
                         (409, "prompt_changed"))
        self.set(DONE, qw("idle"), turn=2)
        self.assertIsNone(self.app.api_prompt("q1")["prompt"])
        self.assertEqual(self.err(self.app.api_answer, "q1", {"action": "approve", "prompt_id": pid}),
                         (409, "prompt_changed"))
        self.assertEqual(self.hc.keys_calls, [])

    def test_question_cards(self):
        self.set(QUESTION, qw("question"))
        self.assertEqual([(x["category"], x["kind"]) for x in self.pusher.payloads], [("HR_BLOCKED", "question")])
        p = self.app.api_prompt("q1")["prompt"]
        self.app.api_prompt_answer("q1", {"prompt_id": p["id"], "option_index": 1})
        self.app.api_prompt_answer("q1", {"prompt_id": p["id"], "text": "MySQL 8"})
        self.assertEqual(self.hc.keys_calls, [("q1", ["2"]), ("q1", ["4"])])
        self.assertEqual(self.hc.send_calls, [("q1", "MySQL 8", "force")])
        self.assertEqual(self.err(self.app.api_answer, "q1", {"action": "approve", "prompt_id": p["id"]}),
                         (409, "needs_manual"))
        self.hc.screens["q1"] = qw("question-other")  # PC에서 직접 입력 칸을 열었다: 카드가 바뀐다
        self.assertIsNone(self.app.api_prompt("q1")["prompt"])
        self.assertEqual(self.err(self.app.api_prompt_answer, "q1", {"prompt_id": p["id"], "option_index": 0}),
                         (409, "prompt_changed"))
        self.hc.screens["q1"] = qw("question-multi")  # 여러 질문: 막힘이지만 카드 없음
        self.assertIsNone(self.app.api_prompt("q1")["prompt"])

    def test_plan_card(self):
        self.set(PERM_PLAN, qw("plan"))
        p = self.app.api_prompt("q1")["prompt"]
        self.assertEqual(p["kind"], "plan")
        self.assertEqual((self.pusher.payloads[-1]["kind"], self.pusher.payloads[-1]["category"]),
                         ("plan", "HR_BLOCKED"))
        self.assertEqual(self.err(self.app.api_answer, "q1", {"action": "approve", "prompt_id": p["id"]}),
                         (409, "needs_manual"))
        self.assertEqual(self.app.api_answer("q1", {"action": "reject", "prompt_id": p["id"]})["keys"], ["4"])
        self.app.api_prompt_answer("q1", {"prompt_id": p["id"], "option_index": 2})
        self.assertEqual(self.hc.keys_calls, [("q1", ["4"]), ("q1", ["3"])])

    def test_trust_dialog_at_start(self):
        # 시작 화면 폴더 신뢰: Qwen이 permission "Approval needed"를 보낸다(화면 덧씌우기 없이 hc status로)
        app, hc, pusher = make_app()
        hc.add("q2", dict(TRUST), agent="qwen", name="new", turn=0)
        hc.screens["q2"] = qw("trust")
        app.poll(initial=True)
        pid = card("trust")["id"]
        self.assertEqual([(x["state"], x["category"], x["kind"], x["prompt_id"]) for x in pusher.payloads],
                         [("blocked", "HR_PERMISSION", "approval", pid)])
        self.assertEqual(app.api_answer("q2", {"action": "approve", "prompt_id": pid})["keys"], ["1"])
        self.assertEqual(app.api_answer("q2", {"action": "reject", "prompt_id": pid})["keys"], ["3"])
        self.assertEqual(self.err(app.api_answer, "q2", {"action": "always", "prompt_id": pid}), (409, "no_always"))
        self.assertEqual(self.err(app.api_send, "q2", {"text": "hello"}), (409, "hc_refused"))

    def test_push_waits_for_trust_box_drawn_after_blocked(self):
        # 실측: 시작 신뢰 상자는 blocked 이벤트보다 30~50ms 늦게 그려진다
        screens = [qw("idle-auto"), qw("idle-auto")]
        real_read = self.hc.read

        def read(tid, lines=0):
            if screens:
                return {"text": screens.pop(0)}
            return real_read(tid, lines)
        self.hc.read = read
        self.set(TRUST, qw("trust"))
        p = self.pusher.payloads[-1]
        self.assertEqual((p["category"], p["kind"], p["prompt_id"]), ("HR_PERMISSION", "approval", card("trust")["id"]))

    def test_send_refused_while_blocked(self):
        self.set(QUESTION, qw("question"))
        self.assertEqual(self.err(self.app.api_send, "q1", {"text": "hello"}), (409, "hc_refused"))
        self.assertEqual(self.hc.send_calls, [])
        self.set(DONE, qw("idle"), turn=2)
        self.assertEqual(self.app.api_send("q1", {"text": "next"}), {"ok": True})

    def test_plain_working_and_done(self):
        self.app.screen_poll()  # 화면 판정 대상이 아니다
        self.assertEqual(self.pusher.payloads, [])
        self.set(DONE, qw("enterplan"), turn=2)  # 피드백 줄이 있어도 끝남
        self.assertEqual([x["state"] for x in self.pusher.payloads], ["done"])
        self.assertEqual(self.later, [])
        self.assertIsNone(self.app.api_prompt("q1")["prompt"])
        self.assertEqual(self.app.api_keys("q1", {"keys": ["esc"]}), {"ok": True})
        by = {t["id"]: t for t in self.app.api_terminals()["terminals"]}
        self.assertEqual((by["q1"]["claude"], by["q1"]["agent_kind"], by["q1"]["status"]["state"]),
                         (True, "qwen", "done"))
        self.assertEqual((by["sh"]["claude"], by["sh"]["agent_kind"]), (False, None))


class QwenTranscriptTests(unittest.TestCase):
    SID = "eb7a4ab4-e03b-4fa3-a549-e028ae88c4de"
    OLD = "f22dfbd4-997c-47d4-a42d-b6ce22909f1a"
    CWD = "/Users/me/work/demo"

    def setUp(self):
        self.home = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)
        self.base = os.path.join(self.home, ".qwen")
        self.chats = os.path.join(self.base, "projects", "-Users-me-work-demo", "chats")
        os.makedirs(self.chats)
        self.path = os.path.join(self.chats, self.SID + ".jsonl")
        shutil.copy(os.path.join(os.path.dirname(__file__), "fixtures", "qwen-session-%s.jsonl" % V), self.path)
        self.old = os.path.join(self.chats, self.OLD + ".jsonl")
        with open(self.old, "w") as f:
            f.write('{"type":"user","sessionId":"%s","message":{"role":"user","parts":[{"text":"old"}]}}\n' % self.OLD)
        os.utime(self.old, (1, 1))
        for sid in (self.SID, self.OLD):  # 같은 폴더의 다른 파일(.runtime.json, .title.json)은 기록이 아니다
            with open(os.path.join(self.chats, sid + ".title.json"), "w") as f:
                f.write('{"displayName":"x"}')
        other = os.path.join(self.base, "projects", "-Users-me-other", "chats")
        os.makedirs(other)
        with open(os.path.join(other, "aaaaaaaa-0000-0000-0000-000000000000.jsonl"), "w") as f:
            f.write("{}\n")
        self.env = dict(os.environ)
        os.environ.pop("QWEN_HOME", None)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self.env)))

    def write_session(self, pid, sid, cwd=None):
        d = os.path.join(self.base, "sessions")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "%d.json" % pid), "w") as f:
            json.dump({"pid": pid, "sessionId": sid, "cwd": cwd or self.CWD, "kind": "tui"}, f)

    def test_parse(self):
        with open(self.path, encoding="utf-8") as f:
            turns, cut = hr.parse_transcript(f.read().split("\n"), 200)
        self.assertFalse(cut)
        self.assertEqual(turns[0], {"role": "user", "parts": [{"kind": "text", "text": "make notes"}]})
        tools = [p for t in turns for p in t["parts"] if p["kind"] == "tool"]
        sh = tools[0]
        self.assertEqual((sh["name"], sh["summary"], sh["state"]), ("run_shell_command", "touch notes.txt", "error"))
        self.assertIn("User did not allow tool call", sh["output"])
        ok = [p for p in tools if p["name"] == "write_file" and p["state"] == "ok"]
        self.assertEqual(ok[0]["summary"], "/Users/me/work/demo/hello.txt")
        self.assertIn("Successfully created", ok[0]["output"])
        asks = [p["output"] for p in tools if p["name"] == "ask_user_question"]
        self.assertIn("**Database**: MySQL 8", asks[0])
        self.assertIn("**Deploy**: Vercel", asks[2])
        self.assertEqual(tools[-1]["state"], "ok")
        self.assertIn("time.sleep(8)", tools[-1]["summary"])
        users = [p["text"] for t in turns if t["role"] == "user" for p in t["parts"]]
        self.assertEqual(users[:3], ["make notes", "write hello", "write hello again"])
        self.assertEqual(len(users), 11)
        texts = [p["text"] for t in turns if t["role"] == "assistant" for p in t["parts"] if p["kind"] == "text"]
        self.assertIn("Written.", texts)
        self.assertTrue(all(t["role"] in ("user", "assistant") for t in turns))
        for a, b in zip(turns, turns[1:]):
            self.assertNotEqual(a["role"], b["role"])  # 도구 결과 줄이 따로 턴을 만들지 않는다
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(hr.preview_from_lines(f.read().split("\n")), "Finished.")

    def test_thought_parts_and_hidden(self):
        lines = [json.dumps(r) for r in (
            {"type": "user", "provenance": "real_user", "message": {"role": "user", "parts": [{"text": "hi"}]}},
            {"type": "user", "message": {"role": "user", "parts": [{"text": "<system-reminder>x</system-reminder>"}]}},
            {"type": "assistant", "message": {"role": "model", "parts": [{"text": "hmm", "thought": True},
                                                                         {"text": "Hello."}]}},
        )]
        turns, _ = hr.parse_transcript(lines, 50)
        self.assertEqual(turns, [{"role": "user", "parts": [{"kind": "text", "text": "hi"}]},
                                 {"role": "assistant", "parts": [{"kind": "thinking", "text": "hmm"},
                                                                 {"kind": "text", "text": "Hello."}]}])

    def test_find_by_cwd(self):
        info = {"agent": "qwen", "cwd": self.CWD, "pid": 4242}
        self.assertEqual(hr.find_agent_transcript(info, self.home), (self.SID, self.path))
        self.assertEqual(hr.find_agent_transcript({"agent": "qwen", "cwd": "/nope"}, self.home), (None, None))

    def test_find_by_pid_session_file(self):
        # 실행 중에는 $QWEN_HOME/sessions/<pid>.json이 지금 세션을 알려 준다(같은 폴더의 더 최근 기록보다 먼저)
        self.write_session(4242, self.OLD)
        info = {"agent": "qwen", "cwd": "/elsewhere", "pid": 4242}
        self.assertEqual(hr.find_agent_transcript(info, self.home), (self.OLD, self.old))
        fg = {"agent": "qwen", "cwd": "/x", "foreground": {"argv": ["node", "qwen"], "pid": 4242, "cwd": "/x"}}
        self.assertEqual(hr.find_agent_transcript(fg, self.home)[0], self.OLD)
        # 자식으로 다시 띄운 경우(node --expose-gc …/cli.js): 자식 pid의 세션 파일
        os.rename(os.path.join(self.base, "sessions", "4242.json"), os.path.join(self.base, "sessions", "4243.json"))
        calls = []

        class R:
            stdout = "4243\n"

        def runner(argv, **kw):
            calls.append(argv)
            return R()
        self.assertEqual(hr.find_agent_transcript(info, self.home, runner=runner)[0], self.OLD)
        self.assertEqual(calls[0][1:], ["-P", "4242"])
        # 세션 파일이 가리키는 기록이 아직 없으면(첫 입력 전) 폴더의 최근 기록으로
        self.write_session(5555, "bbbbbbbb-0000-0000-0000-000000000000")
        R.stdout = ""
        self.assertEqual(hr.find_agent_transcript({"agent": "qwen", "cwd": self.CWD, "pid": 5555}, self.home,
                                                  runner=runner)[0], self.SID)

    def test_bad_session_file_ignored(self):
        self.write_session(4242, "../../etc/passwd")
        info = {"agent": "qwen", "cwd": self.CWD, "pid": 4242}
        self.assertEqual(hr.find_agent_transcript(info, self.home), (self.SID, self.path))

    def test_qwen_home_env(self):
        other = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, other, True)
        shutil.copytree(os.path.join(self.base, "projects"), os.path.join(other, "projects"))
        shutil.rmtree(self.base)
        os.environ["QWEN_HOME"] = other
        info = {"agent": "qwen", "cwd": self.CWD}
        self.assertEqual(hr.find_agent_transcript(info, self.home)[0], self.SID)

    def test_api_transcript(self):
        app, hc, _ = make_app(home=self.home)
        i = hc.add("q1", dict(IDLE), agent="qwen")
        i["cwd"] = self.CWD
        r = app.api_transcript("q1", 50)
        self.assertEqual(r["session_id"], self.SID)
        self.assertEqual(r["turns"][0]["parts"][0]["text"], "make notes")
        by = {t["id"]: t for t in app.api_terminals()["terminals"]}
        self.assertEqual(by["q1"]["preview"], "Finished.")


if __name__ == "__main__":
    unittest.main()
