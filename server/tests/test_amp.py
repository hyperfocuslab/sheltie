"""Amp CLI 지원. 화면은 실제 amp 0.0.1795233642-g42d75f를 hc 안에서(무료 계정, low 모드) 캡처한 것.
Amp은 OSC 7501로 막힘을 보내지만 kind가 없다(msg "Input needed"): 종류는 화면 상자 제목으로 가른다.
메뉴는 번호 없이 커서(‣) ↓/↑ + enter. 대화 본문은 Amp 서버에만 있어 `amp threads export`로 읽는다."""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_herdr_remote import ev, fixture, make_app, hr  # noqa: E402

V = "0.0.1795233642"
APP = "amp"
BLOCKED = {"app": APP, "msg": "Input needed", "state": "blocked"}
WORKING = {"app": APP, "msg": "README copy", "state": "working"}
DONE = {"app": APP, "msg": "README copy", "state": "done"}
THREAD = "T-01a1fd2e-ab90-7770-a569-5292f24f799e"


def ap(name):
    return fixture("amp-%s-%s.txt" % (name, V))


def card(name):
    return hr.parse_prompt(ap(name), agent="amp")


def labels(p):
    return [o["label"] for o in p["options"]]


def status_ev(tid, st):
    return ev(tid, "status", {"status": st})


class AmpAgentKindTests(unittest.TestCase):
    def test_detects_amp(self):
        self.assertEqual(hr.agent_kind({"agent": "amp"}), "amp")
        self.assertEqual(hr.agent_kind({"status": {"app": "amp", "state": "idle"}}), "amp")
        # 실측 hc get: agent "amp", 앞 프로그램 /opt/homebrew/bin/amp -m low
        self.assertEqual(hr.agent_kind({"foreground": {"argv": ["/opt/homebrew/bin/amp", "-m", "low"],
                                                       "name": "amp"}}), "amp")
        self.assertIsNone(hr.agent_kind({"foreground": {"argv": ["ampx"]}}))
        self.assertEqual(hr.agent_kind({"agent": "grok"}), "grok")
        self.assertEqual(hr.agent_kind({"agent": "claude"}), "claude")


class AmpParseTests(unittest.TestCase):
    def test_shell_approval(self):
        p = card("shell")
        self.assertEqual((p["kind"], p["agent"]), ("approval", "amp"))
        self.assertEqual((p["title"], p["question"], p["body"]),
                         ("Bash", "Approval Required", "cp README.md copy.md\nWorking directory: /Users/me/work/demo"))
        # "Allow All for Every Session"은 settings.json에 amp.dangerouslyAllowAll을 쓰는 전역 스위치: 카드에서 뺀다
        self.assertEqual(labels(p), ["Allow Once", "Reject with feedback", "Allow All for This Session"])
        self.assertEqual([o["number"] for o in p["options"]], [None, None, None])
        self.assertEqual((p["selected"], p["approve_index"], p["always_index"], p["persist_index"], p["reject_index"],
                          p["free_text_index"]), (0, 0, 2, None, None, 1))
        self.assertEqual(hr.push_category("blocked", BLOCKED, p), "HR_PERMISSION_ALWAYS")
        # blocked를 보자마자 읽은 화면, Up next 줄이 없는 화면도 같은 카드
        self.assertEqual(card("blocked-immediate")["id"], p["id"])
        self.assertEqual(card("shell-b")["id"], p["id"])
        self.assertEqual(card("shell-new")["body"],
                         "cp README.md copy.md && ls -l copy.md\nWorking directory: /Users/me/work/demo")

    def test_patch_approval(self):
        p = card("edit")
        self.assertEqual((p["kind"], p["title"], p["question"]), ("approval", "Apply Patch", "Approval Required"))
        self.assertEqual(p["body"], "*** Begin Patch\n*** Update File: hello.txt\n@@\n-hi\n+hello\n*** End Patch")
        self.assertEqual(labels(p), ["Allow Once", "Reject with feedback", "Allow All for This Session"])
        self.assertEqual((p["selected"], p["always_index"], p["free_text_index"]), (0, 2, 1))

    def test_question(self):
        p = card("question")
        self.assertEqual((p["kind"], p["title"], p["question"], p["body"]), ("question", None, "Which color?", None))
        self.assertEqual(labels(p), ["Red (warm color)", "Blue (cool color)"])
        self.assertEqual((p["selected"], p["free_text_index"], p["multi_select"]), (0, None, False))
        self.assertEqual(hr.push_category("blocked", BLOCKED, p), "HR_BLOCKED")
        self.assertEqual(labels(card("question2")), ["Small", "Medium", "Large"])
        self.assertEqual(card("question2")["question"], "Pick a size?")
        self.assertEqual(card("question-after2")["id"], p["id"])  # 숫자 키는 아무것도 안 한다(실측)

    def test_unknown_screens_have_no_card(self):
        # 피드백 입력칸, 평소·작업 중·끝난 화면, 거절·허용 뒤, 첫 실행 안내, 명령 팔레트
        for name in ("shell-reject-feedback", "idle", "working", "after-feedback", "shell-esc", "session-allow",
                     "shell-allowonce", "write", "edit-after", "question-answered", "question-esc", "welcome",
                     "palette"):
            self.assertIsNone(card(name), name)

    def test_cursor_on_every_session_row(self):
        # PC에서 커서를 "Allow All for Every Session"으로 옮겼다: 커서 위치를 카드 항목으로 모른다
        t = ap("shell").replace("│ ‣ Allow Once ", "│   Allow Once ").replace("│   Allow All for Every Session",
                                                                               "│ ‣ Allow All for Every Session")
        p = hr.parse_prompt(t, agent="amp")
        self.assertIsNone(p["selected"])
        with self.assertRaises(hr.ApiError) as cm:
            hr.answer_steps(p, option_index=0)
        self.assertEqual(cm.exception.body["error"], "no_cursor")
        t2 = ap("shell").replace("│ ‣ Allow Once ", "│   Allow Once ").replace("│   Reject with feedback",
                                                                               "│ ‣ Reject with feedback")
        self.assertEqual(hr.answer_steps(hr.parse_prompt(t2, agent="amp"), option_index=0), [("keys", ["up", "enter"])])

    def test_parsers_do_not_cross(self):
        for name in ("shell", "edit", "question", "question2"):
            for agent in (None, "codex", "gemini", "copilot", "qwen", "opencode", "grok", "cursor"):
                self.assertIsNone(hr.parse_prompt(ap(name), agent=agent), (name, agent))
        for f in ("codex-exec-0.171.0.txt", "codex-plan-0.171.0.txt", "claude-bash-2.1.329.txt",
                  "claude-question-2.1.329.txt", "claude-plan-2.1.329.txt", "gemini-exec-0.70.0.txt",
                  "gemini-question-0.70.0.txt", "gemini-plan-0.70.0.txt", "copilot-exec-1.0.114.txt",
                  "copilot-trust-1.0.114.txt", "copilot-question-1.0.114.txt", "copilot-plan-1.0.114.txt",
                  "qwen-exec-0.28.3.txt", "qwen-question-0.28.3.txt", "qwen-plan-0.28.3.txt",
                  "qwen-trust-0.28.3.txt", "opencode-exec-1.19.6.txt", "opencode-question-1.19.6.txt",
                  "grok-exec-1.0.77.txt", "grok-question-1.0.77.txt", "grok-plan-1.0.77.txt",
                  "cursor-exec-2026.11.19.txt", "cursor-plan-2026.11.19.txt", "cursor-trust-2026.11.19.txt"):
            self.assertIsNone(hr.parse_prompt(fixture(f), agent="amp"), f)

    def test_answer_steps(self):
        # 숫자 키는 무시된다: 커서(늘 0) 이동 + enter(실측)
        self.assertEqual(hr.answer_steps(card("shell"), option_index=0), [("keys", ["enter"])])
        self.assertEqual(hr.answer_steps(card("shell"), option_index=2), [("keys", ["down", "down", "enter"])])
        with self.assertRaises(hr.ApiError):
            hr.answer_steps(card("shell"), option_index=1)  # 피드백 거절은 글이 있어야 한다
        # 피드백 거절: down enter로 입력칸을 연 뒤 붙여넣기+enter(실측)
        self.assertEqual(hr.answer_steps(card("shell"), text="use rsync"),
                         [("keys", ["down", "enter"]), ("wait", hr.AMP_FEEDBACK_WAIT), ("text", "use rsync")])
        self.assertEqual(hr.answer_steps(card("question"), option_index=1), [("keys", ["down", "enter"])])
        self.assertEqual(hr.answer_steps(card("question2"), option_index=2), [("keys", ["down", "down", "enter"])])
        with self.assertRaises(hr.ApiError):
            hr.answer_steps(card("question"), text="Green")  # 직접 입력 없음


class AmpApiTests(unittest.TestCase):
    def setUp(self):
        self.app, self.hc, self.pusher = make_app()
        self.later = []
        self.app._later = lambda sec, fn: self.later.append((sec, fn))
        self.slept = []
        self.app._sleep = self.slept.append
        self.hc.add("a1", dict(WORKING), agent="amp", name="am", turn=1)
        self.hc.screens["a1"] = ap("working")
        self.hc.add("sh", None, agent=None, argv=["/bin/zsh"])
        self.app.poll(initial=True)

    def err(self, fn, *a):
        with self.assertRaises(hr.ApiError) as cm:
            fn(*a)
        return cm.exception.code, cm.exception.body["error"]

    def set(self, st, screen=None, turn=None):
        if screen is not None:
            self.hc.screens["a1"] = screen
        self.hc.set_status("a1", st, turn=turn)
        self.app.handle_event_line(status_ev("a1", st))

    def test_approval_push_card_and_answers(self):
        self.set(BLOCKED, ap("shell"))
        pid = card("shell")["id"]
        self.assertEqual([(x["state"], x["category"], x["kind"], x["prompt_id"], x["msg"]) for x in self.pusher.payloads],
                         [("blocked", "HR_PERMISSION_ALWAYS", "approval", pid, "approve Bash")])
        self.assertEqual(self.app.api_prompt("a1")["prompt"]["id"], pid)
        self.assertEqual(self.app.api_answer("a1", {"action": "approve", "prompt_id": pid})["keys"], ["enter"])
        self.assertEqual(self.app.api_answer("a1", {"action": "always", "prompt_id": pid})["keys"],
                         ["down", "down", "enter"])
        self.assertEqual(self.app.api_answer("a1", {"action": "reject", "prompt_id": pid})["keys"], ["esc"])
        self.app.api_prompt_answer("a1", {"prompt_id": pid, "text": "use rsync instead"})
        self.assertEqual(self.err(self.app.api_prompt_answer, "a1", {"prompt_id": pid, "option_index": 3}),
                         (400, "bad_option"))  # 전역 무승인 스위치는 카드에 없다
        self.assertEqual(self.hc.keys_calls, [("a1", ["enter"]), ("a1", ["down", "down", "enter"]), ("a1", ["esc"]),
                                              ("a1", ["down", "enter"])])
        self.assertEqual(self.hc.send_calls, [("a1", "use rsync instead", "force")])
        self.assertEqual(self.slept, [hr.AMP_FEEDBACK_WAIT])  # 입력칸이 그려진 뒤 붙여넣기(실측: 바로 넣으면 빈 거절)

    def test_no_card_answers(self):
        # 옛 경로(prompt_id 없음): 화면 카드로 status를 만든다(hc msg "Input needed"에는 kind가 없다)
        self.set(BLOCKED, ap("edit"))
        self.assertEqual(self.pusher.payloads[-1]["msg"], "approve Apply Patch")
        self.assertEqual(self.app.api_answer("a1", {"action": "approve", "expect_msg": "approve Apply Patch"})["keys"],
                         ["enter"])
        self.assertEqual(self.app.api_answer("a1", {"action": "reject"})["keys"], ["esc"])
        self.assertEqual(self.err(self.app.api_answer, "a1", {"action": "always"}), (400, "needs_prompt_id"))
        self.assertEqual(self.err(self.app.api_answer, "a1", {"action": "approve", "expect_msg": "Input needed"}),
                         (409, "changed"))
        by = {t["id"]: t for t in self.app.api_terminals()["terminals"]}
        self.assertEqual((by["a1"]["status"]["kind"], by["a1"]["status"]["msg"]), ("permission", "approve Apply Patch"))

    def test_feedback_box_keeps_blocked_without_new_push(self):
        self.set(BLOCKED, ap("shell"))
        self.assertEqual(len(self.pusher.payloads), 1)
        # PC에서 "Reject with feedback"을 골라 입력칸이 열렸다: hc는 계속 blocked. 새 알림은 없고 카드·카드 없는 답도 없다
        self.hc.screens["a1"] = ap("shell-reject-feedback")
        self.app.poll()
        self.app.handle_event_line(status_ev("a1", BLOCKED))
        self.assertEqual(len(self.pusher.payloads), 1)
        self.assertIsNone(self.app.api_prompt("a1")["prompt"])
        self.assertEqual(self.err(self.app.api_answer, "a1", {"action": "approve"}), (409, "needs_manual"))
        self.assertEqual(self.err(self.app.api_answer, "a1", {"action": "reject"}), (409, "needs_manual"))
        self.assertEqual(self.err(self.app.api_send, "a1", {"text": "hello"}), (409, "hc_refused"))
        self.assertEqual(self.hc.keys_calls, [])
        # 답한 뒤 작업 중 → 다음 승인은 새 알림
        self.set(WORKING, ap("working"))
        self.set(BLOCKED, ap("shell-new"))
        self.assertEqual([x["prompt_id"] for x in self.pusher.payloads][1:], [card("shell-new")["id"]])

    def test_blocked_without_card_at_first(self):
        # 카드를 못 읽은 막힘(캡처 안 한 화면): hc status 그대로, 버튼 없는 알림. 답은 앱에서
        self.set(BLOCKED, ap("shell-reject-feedback"))
        self.assertEqual(self.pusher.payloads, [])  # 상자가 늦게 그려질 수 있어 잠깐 뒤 한 번 더 본다
        self.assertEqual([sec for sec, _ in self.later], [hr.SCREEN_SETTLE])
        self.later.pop()[1]()
        self.assertEqual([(x["category"], x["prompt_id"], x["msg"]) for x in self.pusher.payloads],
                         [("HR_BLOCKED", None, "Input needed")])
        self.assertEqual(self.err(self.app.api_answer, "a1", {"action": "approve"}), (409, "needs_manual"))
        self.assertEqual(self.err(self.app.api_answer, "a1", {"action": "reject"}), (409, "needs_manual"))

    def test_menu_drawn_late(self):
        # 실측: blocked 이벤트 때 화면에 상자가 아직 없었다 → 알림은 카드를 읽은 뒤 한 번만
        self.set(BLOCKED, ap("working"))
        self.assertEqual(self.pusher.payloads, [])
        self.hc.screens["a1"] = ap("shell")
        self.later.pop()[1]()
        self.app.poll()
        self.app.handle_event_line(ev("a1", "turn", {"state": "blocked", "turn": 1}))
        self.assertEqual([(x["prompt_id"], x["msg"]) for x in self.pusher.payloads], [(card("shell")["id"], "approve Bash")])

    def test_recheck_before_keys(self):
        self.set(BLOCKED, ap("shell"))
        pid = card("shell")["id"]
        self.hc.screens["a1"] = ap("shell-new")
        self.assertEqual(self.err(self.app.api_answer, "a1", {"action": "approve", "prompt_id": pid}),
                         (409, "prompt_changed"))
        self.set(DONE, ap("shell-allowonce"), turn=2)
        self.assertIsNone(self.app.api_prompt("a1")["prompt"])
        self.assertEqual(self.err(self.app.api_answer, "a1", {"action": "approve", "prompt_id": pid}),
                         (409, "prompt_changed"))
        self.assertEqual(self.hc.keys_calls, [])

    def test_question_cards(self):
        self.set(BLOCKED, ap("question"))
        self.assertEqual([(x["category"], x["kind"], x["msg"]) for x in self.pusher.payloads],
                         [("HR_BLOCKED", "question", "Which color?")])
        p = self.app.api_prompt("a1")["prompt"]
        self.app.api_prompt_answer("a1", {"prompt_id": p["id"], "option_index": 1})
        self.assertEqual(self.err(self.app.api_prompt_answer, "a1", {"prompt_id": p["id"], "text": "Green"}),
                         (400, "no_free_text"))
        self.assertEqual(self.err(self.app.api_answer, "a1", {"action": "approve", "prompt_id": p["id"]}),
                         (409, "needs_manual"))
        self.assertEqual(self.err(self.app.api_answer, "a1", {"action": "approve"}), (409, "needs_manual"))
        self.assertEqual(self.err(self.app.api_answer, "a1", {"action": "reject", "prompt_id": p["id"]}),
                         (409, "needs_manual"))  # 질문 카드는 앱에서 고른다(다른 에이전트와 같다)
        self.assertEqual(self.app.api_answer("a1", {"action": "reject"})["keys"], ["esc"])  # 옛 경로: 고르지 않고 닫기
        self.assertEqual(self.hc.keys_calls, [("a1", ["down", "enter"]), ("a1", ["esc"])])

    def test_send_refused_while_blocked(self):
        self.set(BLOCKED, ap("question"))
        self.assertEqual(self.err(self.app.api_send, "a1", {"text": "hello"}), (409, "hc_refused"))
        self.assertEqual(self.hc.send_calls, [])
        self.set(DONE, ap("question-answered"), turn=2)
        self.assertEqual(self.app.api_send("a1", {"text": "next"}), {"ok": True})

    def test_plain_working_and_done(self):
        self.app.screen_poll()  # 화면 판정 대상이 아니다
        self.assertEqual(self.pusher.payloads, [])
        self.set(DONE, ap("shell-allowonce"), turn=2)
        self.assertEqual([(x["state"], x["msg"]) for x in self.pusher.payloads], [("done", "README copy")])
        self.assertEqual(self.later, [])
        self.assertIsNone(self.app.api_prompt("a1")["prompt"])
        self.assertEqual(self.app.api_keys("a1", {"keys": ["esc"]}), {"ok": True})
        by = {t["id"]: t for t in self.app.api_terminals()["terminals"]}
        self.assertEqual((by["a1"]["claude"], by["a1"]["agent_kind"], by["a1"]["status"]["state"]),
                         (True, "amp", "done"))
        self.assertEqual((by["sh"]["claude"], by["sh"]["agent_kind"]), (False, None))


class AmpTranscriptTests(unittest.TestCase):
    def setUp(self):
        hr._amp_export_cache.clear()
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.exe = os.path.join(self.tmp, "amp")
        with open(self.exe, "w") as f:
            f.write("#!/bin/sh\nexit 1\n")
        os.chmod(self.exe, 0o755)
        with open(os.path.join(os.path.dirname(__file__), "fixtures", "amp-session-%s.json" % V),
                  encoding="utf-8") as f:
            self.export = f.read()
        self.calls = []
        self.lsof = "p4242\nn/dev/ttys001\nn/Users/me/.cache/amp/logs/cli.log\nn/Users/me/.cache/amp/logs/threads/%s.log\n" \
                    % THREAD

        class R:
            def __init__(s, out, code=0):
                s.stdout, s.returncode = out, code

        def runner(argv, **kw):
            self.calls.append((argv, kw))
            if argv[0].endswith("lsof"):
                return R(self.lsof)
            return R(self.export)
        self.runner = runner
        orig_exe, orig_export = hr.amp_executable, hr.amp_thread_export
        hr.amp_executable = lambda home: self.exe
        hr.amp_thread_export = lambda exe, tid: orig_export(exe, tid, runner=runner)
        self.addCleanup(setattr, hr, "amp_executable", orig_exe)
        self.addCleanup(setattr, hr, "amp_thread_export", orig_export)
        self.orig_export = orig_export

    def test_find_by_open_thread_log(self):
        info = {"agent": "amp", "cwd": "/Users/me/work/demo", "pid": 4242}
        self.assertEqual(hr.find_agent_transcript(info, self.tmp, runner=self.runner), (THREAD, self.exe))
        self.assertEqual(self.calls[0][0][1:], ["-a", "-p", "4242", "-Fn"])
        # 다른 폴더의 같은 이름·ID 모양이 아닌 파일은 무시. 로그를 못 찾으면 (None, None)(폴더로 찾을 기록이 로컬에 없다)
        self.lsof = "n/tmp/x/%s.log\nn/Users/me/.cache/amp/logs/threads/T-nope.log\n" % THREAD
        self.assertEqual(hr.find_agent_transcript(info, self.tmp, runner=self.runner), (None, None))
        self.assertEqual(hr.find_agent_transcript({"agent": "amp", "cwd": "/w"}, self.tmp, runner=self.runner),
                         (None, None))

    def test_export_args_cache_and_guards(self):
        clock = [100.0]
        d = self.orig_export(self.exe, THREAD, runner=self.runner, clock=lambda: clock[0])
        self.assertEqual(d["title"], "README copy")
        self.assertEqual(self.calls[-1][0], [self.exe, "threads", "export", THREAD])
        self.assertEqual((self.calls[-1][1]["timeout"], self.calls[-1][1]["shell"]), (hr.AMP_EXPORT_TIMEOUT, False))
        n = len(self.calls)
        clock[0] += 1
        self.assertIs(self.orig_export(self.exe, THREAD, runner=self.runner, clock=lambda: clock[0]), d)
        self.assertEqual(len(self.calls), n)  # 짧은 시간 안 같은 스레드는 다시 부르지 않는다
        clock[0] += hr.AMP_EXPORT_TTL
        self.orig_export(self.exe, THREAD, runner=self.runner, clock=lambda: clock[0])
        self.assertEqual(len(self.calls), n + 1)
        # 인자 검증: 스레드 ID 모양, 실행 파일 절대 경로
        for bad in ("--help", "T-1", THREAD + "; rm", "../" + THREAD):
            self.assertIsNone(self.orig_export(self.exe, bad, runner=self.runner))
        self.assertIsNone(self.orig_export("amp", THREAD, runner=self.runner))
        self.assertIsNone(self.orig_export(os.path.join(self.tmp, "nope"), THREAD, runner=self.runner))
        self.assertEqual(len(self.calls), n + 1)
        # 실패·다른 스레드 JSON·깨진 출력은 None
        hr._amp_export_cache.clear()
        for out in ("", "not json", json.dumps({"id": "T-00000000-0000-0000-0000-000000000000", "messages": []})):
            self.export = out
            self.assertIsNone(self.orig_export(self.exe, THREAD, runner=self.runner))

        def timeout(argv, **kw):
            raise hr.subprocess.TimeoutExpired(argv, kw.get("timeout"))
        self.assertIsNone(self.orig_export(self.exe, THREAD, runner=timeout))

    def test_parse(self):
        turns, cut = hr._parse_amp_thread(json.loads(self.export), 200)
        self.assertFalse(cut)
        self.assertEqual(turns[0], {"role": "user", "parts": [
            {"kind": "text", "text": "Run exactly this one shell command and nothing else: cp README.md copy.md"}]})
        for a, b in zip(turns, turns[1:]):
            self.assertNotEqual(a["role"], b["role"])
        tools = [p for t in turns for p in t["parts"] if p["kind"] == "tool"]
        first = tools[0]
        self.assertEqual((first["name"], first["summary"], first["state"], first["output"]),
                         ("shell_command", "cp README.md copy.md", "error",
                          "Tool rejected by plugin: Do not copy. Instead reply with the word BANANA only."))
        patch = [p for p in tools if p["name"] == "apply_patch"][0]
        self.assertEqual((patch["state"], patch["output"]), ("ok", "add: /Users/me/work/demo/hello.txt (+1/-0)"))
        cat = [p for p in tools if p["summary"] == "cat hello.txt"][0]
        self.assertEqual((cat["state"], cat["output"]), ("ok", "hi\n"))
        asks = [p for p in tools if p["name"] == "ask_user_choice"]
        self.assertEqual([(p["summary"], p["output"]) for p in asks],
                         [("Which color?", "User selected option 2: Blue (cool color)"),
                          ("Pick a size?", "User dismissed the question without choosing.")])
        texts = [p["text"] for t in turns if t["role"] == "assistant" for p in t["parts"] if p["kind"] == "text"]
        self.assertEqual(texts[:2], ["BANANA", "Created `hello.txt` with `hi`."])  # 추천 답 블록은 뺀다
        self.assertTrue(all("<suggested-replies>" not in t for t in texts))
        self.assertEqual(texts[-1], "OK")
        thinking = [p for t in turns for p in t["parts"] if p["kind"] == "thinking"]
        self.assertEqual(len(thinking), 1)  # 빈 추론(암호화)은 뺀다
        users = [p["text"] for t in turns if t["role"] == "user" for p in t["parts"]]
        self.assertEqual(len(users), 9)
        turns2, cut2 = hr._parse_amp_thread(json.loads(self.export), 3)
        self.assertTrue(cut2)
        self.assertEqual(turns2, turns[-3:])

    def test_api_transcript_and_no_preview(self):
        app, hc, _ = make_app(home=self.tmp, runner=self.runner)
        i = hc.add("a1", dict(DONE), agent="amp")
        i["pid"] = 4242
        by = {t["id"]: t for t in app.api_terminals()["terminals"]}
        self.assertIsNone(by["a1"]["preview"])
        self.assertFalse(any(c[0][0] == self.exe for c in self.calls))  # 목록에서는 export를 부르지 않는다
        r = app.api_transcript("a1", 50)
        self.assertEqual(r["session_id"], THREAD)
        self.assertEqual(r["turns"][0]["parts"][0]["text"],
                         "Run exactly this one shell command and nothing else: cp README.md copy.md")
        self.assertFalse(r["truncated"])
        self.assertTrue(any(c[0][0] == self.exe for c in self.calls))
        # amp 실행 파일이 없으면 빈 목록
        hr._preview_by_pid.clear()
        hr.amp_executable = lambda home: None
        self.assertEqual(app.api_transcript("a1", 50), {"session_id": THREAD, "turns": [], "truncated": False})


if __name__ == "__main__":
    unittest.main()
