"""Codex CLI 지원: 메뉴 카드·답 키·푸시·대화 보기·메시지. 화면은 실제 codex-cli 0.171.0을 hc 안에서 캡처한 것."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_herdr_remote import fixture, make_app, status_ev, st, hr  # noqa: E402

V = "0.171.0"
BLOCKED_PERM = {"app": "codex", "kind": "permission", "state": "blocked"}
BLOCKED_Q = {"app": "codex", "kind": "question", "state": "blocked"}


def cx(name):
    return fixture("codex-%s-%s.txt" % (name, V))


def card(name):
    return hr.parse_prompt(cx(name), agent="codex")


class CodexAgentKindTests(unittest.TestCase):
    def test_detects_codex(self):
        self.assertEqual(hr.agent_kind({"agent": "codex"}), "codex")
        self.assertEqual(hr.agent_kind({"status": {"app": "codex"}}), "codex")
        self.assertEqual(hr.agent_kind({"foreground": {"argv": ["/opt/homebrew/bin/codex", "-a", "on-request"]}}),
                         "codex")
        self.assertEqual(hr.agent_kind({"agent": "claude"}), "claude")
        self.assertIsNone(hr.agent_kind({"agent": "bash", "foreground": {"argv": ["/bin/zsh"]}}))
        self.assertIsNone(hr.agent_kind({"foreground": {"argv": ["codex-helper"]}}))


class CodexParseTests(unittest.TestCase):
    def test_exec_approval(self):
        p = card("exec")
        self.assertEqual((p["kind"], p["title"], p["question"], p["agent"]),
                         ("approval", "Run command", "Would you like to run the following command?", "codex"))
        self.assertEqual([o["label"] for o in p["options"]],
                         ["Yes, proceed", "Yes, and don't ask again for commands that start with `touch`",
                          "No, and tell Codex what to do differently"])
        self.assertIn("$ touch b.txt", p["body"])
        self.assertIn("Reason: Do you want to allow creating b.txt in this folder?", p["body"])
        # (p)는 ~/.codex/rules에 영구 저장(실측): 세션 허용 버튼(always)으로 쓰지 않는다
        self.assertEqual((p["selected"], p["always_index"], p["persist_index"], p["reject_index"]), (0, None, 1, 2))
        self.assertEqual(hr.approve_index([o["label"] for o in p["options"]]), 0)
        self.assertEqual(card("exec-noprefix")["body"].splitlines()[-1], "$ rm -f b.txt")

    def test_patch_approval_has_session_allow(self):
        p = card("patch")
        self.assertEqual((p["kind"], p["title"]), ("approval", "Edit files"))
        self.assertIn("Destination: /Users/me/work/demo/c.txt", p["body"])
        self.assertEqual(p["options"][1]["label"], "Yes, and don't ask again for these files")
        self.assertEqual((p["always_index"], p["persist_index"], p["reject_index"]), (1, None, 2))
        self.assertEqual(hr.push_category("blocked", BLOCKED_PERM, p), "HR_PERMISSION_ALWAYS")
        self.assertEqual(hr.push_category("blocked", BLOCKED_PERM, card("exec")), "HR_PERMISSION")

    def test_question(self):
        p = card("question")
        self.assertEqual((p["kind"], p["title"], p["question"]), ("question", None, "Which database should we use?"))
        self.assertEqual([(o["label"], o["description"]) for o in p["options"]],
                         [("SQLite (Recommended)", "Single file, no server."),
                          ("Postgres", "Needs a running server."),
                          ("None of the above", "Optionally, add details in notes (tab)")])
        self.assertEqual((p["selected"], p["free_text_index"], p["multi_select"]), (0, 2, False))
        self.assertEqual(hr.push_category("blocked", BLOCKED_Q, p), "HR_BLOCKED")

    def test_multi_question_one_card_per_question(self):
        p1, p2 = card("question-multi"), card("question-multi-2")
        self.assertEqual((p1["title"], p1["question"]), ("Question 1/2", "Which database should we use?"))
        self.assertEqual((p2["title"], p2["question"]), ("Question 2/2", "Which language?"))
        self.assertNotEqual(p1["id"], p2["id"])

    def test_unknown_or_inactive_screens_have_no_card(self):
        # 메모 입력 중, 계획 실행 묻기(hc가 blocked로 보지 않는다), 평소 화면, 폴더 신뢰, 모델 바꾸기 권유
        for name in ("question-notes", "idle", "trust"):
            self.assertIsNone(card(name), name)
        self.assertIsNone(hr.parse_prompt(fixture("codex-ratelimit-0.166.0.txt"), agent="codex"))

    def test_parsers_do_not_cross(self):
        for name in ("exec", "patch", "question"):
            self.assertIsNone(hr.parse_prompt(cx(name)), name)  # Claude 파서는 Codex 화면을 안 읽는다
        for name in ("bash", "write", "question", "plan", "overwrite"):
            self.assertIsNone(hr.parse_prompt(fixture("claude-%s-2.1.329.txt" % name), agent="codex"), name)

    def test_stale_menu_above_new_output_is_not_active(self):
        lines = cx("exec").rstrip("\n").split("\n") + ["", "• Ran touch b.txt", "", "› Ask Codex to do anything"]
        self.assertIsNone(hr.parse_prompt("\n".join(lines), agent="codex"))


def plan_mode_idle():
    """플랜 모드 턴이 끝났는데 메뉴가 아직 안 그려진 화면(입력창 아래 줄에 Plan mode)."""
    lines = cx("idle").rstrip("\n").split("\n")
    i = max(k for k, ln in enumerate(lines) if "for shortcuts" in ln)
    lines[i] = "  ← for agents · ? for shortcuts" + " " * 100 + "Plan mode (⇧tab to cycle) · ⚠ 2 warnings · f2 to view"
    return "\n".join(lines) + "\n"


class CodexPlanParseTests(unittest.TestCase):
    def test_plan_menu(self):
        p = card("plan")
        self.assertEqual((p["kind"], p["question"], p["agent"]), ("plan", "Implement this plan?", "codex"))
        self.assertEqual([(o["label"], o["description"]) for o in p["options"]],
                         [("Yes, implement this plan", "Switch to Default and start coding"),
                          ("Yes, clear context and implement", "Fresh thread with this plan"),
                          ("No, stay in Plan mode", "Continue planning with the model")])
        self.assertIn("• Create b.txt with hello", p["body"])
        self.assertTrue(p["body"].startswith("# Add b.txt"))
        self.assertEqual((p["selected"], p["reject_index"], p["always_index"]), (0, 2, None))
        self.assertEqual(hr.push_category("blocked", {"app": "codex", "state": "blocked", "kind": "plan"}, p),
                         "HR_BLOCKED")  # Claude 계획처럼 잠금화면 허락 버튼은 없다(앱에서 고른다)

    def test_answered_plan_menu_is_gone(self):
        lines = cx("plan").rstrip("\n").split("\n") + ["", "› Ask Codex to do anything"]
        self.assertIsNone(hr.parse_prompt("\n".join(lines), agent="codex"))
        self.assertIsNone(hr.parse_prompt(plan_mode_idle(), agent="codex"))


class CodexPlanApiTests(unittest.TestCase):
    def setUp(self):
        self.app, self.hc, self.pusher = make_app()
        self.later = []
        self.app._later = lambda sec, fn: self.later.append((sec, fn))
        self.hc.add("c1", {"app": "codex", "state": "working"}, agent="codex", name="cx")
        self.hc.screens["c1"] = cx("idle")
        self.app.poll(initial=True)

    def err(self, fn, *a):
        with self.assertRaises(hr.ApiError) as cm:
            fn(*a)
        return cm.exception.code, cm.exception.body["error"]

    def finish(self, screen):
        self.hc.screens["c1"] = screen
        done = {"app": "codex", "state": "done"}
        self.hc.set_status("c1", done, turn=2)
        self.app.handle_event_line(status_ev("c1", done))

    def test_plan_menu_counts_as_blocked_for_card_answer_and_list(self):
        self.finish(cx("plan"))
        p = self.app.api_prompt("c1")["prompt"]
        self.assertEqual(p["kind"], "plan")
        by = {t["id"]: t for t in self.app.api_terminals()["terminals"]}
        self.assertEqual(by["c1"]["status"]["state"], "blocked")
        self.assertIsNotNone(by["c1"]["blocked_since"])
        for action in ("approve", "always"):
            self.assertEqual(self.err(self.app.api_answer, "c1", {"action": action, "prompt_id": p["id"]}),
                             (409, "needs_manual"))
        self.assertEqual(self.app.api_answer("c1", {"action": "reject", "prompt_id": p["id"]})["keys"], ["3"])
        self.app.api_prompt_answer("c1", {"prompt_id": p["id"], "option_index": 0})
        self.app.api_prompt_answer("c1", {"prompt_id": p["id"], "option_index": 1})
        self.assertEqual(self.hc.keys_calls, [("c1", ["3"]), ("c1", ["1"]), ("c1", ["2"])])
        # 메뉴가 사라지면(답한 뒤) 막힘도 카드도 아니다
        self.hc.screens["c1"] = cx("idle")
        self.assertIsNone(self.app.api_prompt("c1")["prompt"])
        self.assertEqual(self.err(self.app.api_answer, "c1", {"action": "reject", "prompt_id": p["id"]}),
                         (409, "prompt_changed"))

    def test_push_blocked_instead_of_done(self):
        self.finish(cx("plan"))
        self.assertEqual([(x["state"], x["category"], x["kind"]) for x in self.pusher.payloads],
                         [("blocked", "HR_BLOCKED", "plan")])
        self.assertEqual(self.pusher.payloads[0]["prompt_id"], card("plan")["id"])
        self.assertEqual(self.later, [])

    def test_plan_mode_done_waits_for_late_menu(self):
        self.finish(plan_mode_idle())
        self.assertEqual(self.pusher.payloads, [])  # 끝남을 바로 보내지 않는다
        self.assertEqual(len(self.later), 1)
        self.hc.screens["c1"] = cx("plan")
        self.later.pop()[1]()
        self.assertEqual([(x["state"], x["kind"]) for x in self.pusher.payloads], [("blocked", "plan")])

    def test_plan_mode_done_without_menu_sends_done_once(self):
        self.finish(plan_mode_idle())
        self.later.pop()[1]()
        self.assertEqual([x["state"] for x in self.pusher.payloads], ["done"])
        self.assertEqual(self.later, [])

    def test_default_mode_done_is_immediate(self):
        self.finish(cx("idle"))
        self.assertEqual([x["state"] for x in self.pusher.payloads], ["done"])
        self.assertEqual(self.later, [])


class CodexAnswerStepsTests(unittest.TestCase):
    def test_digits_and_free_text(self):
        self.assertEqual(hr.answer_steps(card("exec"), option_index=0), [("keys", ["1"])])
        self.assertEqual(hr.answer_steps(card("question"), option_index=1), [("keys", ["2"])])
        # 자유 입력: 커서를 None of the above로 옮기고 tab으로 메모 칸, 붙여넣기+enter
        self.assertEqual(hr.answer_steps(card("question"), text="DuckDB"),
                         [("keys", ["down", "down", "tab"]), ("text", "DuckDB")])


class CodexApiTests(unittest.TestCase):
    def setUp(self):
        self.app, self.hc, self.pusher = make_app()
        self.hc.add("c1", BLOCKED_PERM, agent="codex", name="cx")
        self.hc.screens["c1"] = cx("exec")
        self.hc.add("sh", None, agent=None, argv=["/bin/zsh"])

    def err(self, fn, *a):
        with self.assertRaises(hr.ApiError) as cm:
            fn(*a)
        return cm.exception.code, cm.exception.body["error"]

    def test_card_answer_approve_reject(self):
        p = self.app.api_prompt("c1")["prompt"]
        self.assertEqual(p["kind"], "approval")
        self.assertEqual(self.app.api_answer("c1", {"action": "approve", "prompt_id": p["id"]})["keys"], ["1"])
        self.assertEqual(self.app.api_answer("c1", {"action": "reject", "prompt_id": p["id"]})["keys"], ["3"])
        self.assertEqual(self.err(self.app.api_answer, "c1", {"action": "always", "prompt_id": p["id"]}),
                         (409, "no_always"))
        self.hc.screens["c1"] = cx("patch")
        q = self.app.api_prompt("c1")["prompt"]
        self.assertEqual(self.app.api_answer("c1", {"action": "always", "prompt_id": q["id"]})["keys"], ["2"])
        self.assertEqual(self.hc.keys_calls, [("c1", ["1"]), ("c1", ["3"]), ("c1", ["2"])])

    def test_recheck_before_keys(self):
        p = self.app.api_prompt("c1")["prompt"]
        self.hc.screens["c1"] = cx("exec-noprefix")  # 다른 명령으로 바뀜
        self.assertEqual(self.err(self.app.api_answer, "c1", {"action": "approve", "prompt_id": p["id"]}),
                         (409, "prompt_changed"))
        self.hc.screens["c1"] = fixture("codex-ratelimit-0.166.0.txt")  # 모르는 메뉴
        self.assertIsNone(self.app.api_prompt("c1")["prompt"])
        self.assertEqual(self.err(self.app.api_answer, "c1", {"action": "approve", "prompt_id": p["id"]}),
                         (409, "prompt_changed"))
        # prompt_id 없는 옛 승인 경로: Codex 상태에는 msg가 없어 고를 키를 모른다
        self.assertEqual(self.err(self.app.api_answer, "c1", {"action": "approve"}), (409, "needs_manual"))
        self.hc.set_status("c1", {"app": "codex", "state": "working"})
        self.hc.screens["c1"] = cx("exec")
        self.assertIsNone(self.app.api_prompt("c1")["prompt"])
        self.assertEqual(self.hc.keys_calls, [])

    def test_question_answer_and_text(self):
        self.hc.set_status("c1", BLOCKED_Q)
        self.hc.screens["c1"] = cx("question")
        p = self.app.api_prompt("c1")["prompt"]
        self.app.api_prompt_answer("c1", {"prompt_id": p["id"], "option_index": 1})
        self.app.api_prompt_answer("c1", {"prompt_id": p["id"], "text": "DuckDB"})
        self.assertEqual(self.hc.keys_calls, [("c1", ["2"]), ("c1", ["down", "down", "tab"])])
        self.assertEqual(self.hc.send_calls, [("c1", "DuckDB", "force")])
        self.assertEqual(self.err(self.app.api_answer, "c1", {"action": "approve", "prompt_id": p["id"]}),
                         (409, "needs_manual"))

    def test_send_and_keys_open_for_codex_only(self):
        self.assertEqual(self.app.api_send("c1", {"text": "hello"}), {"ok": True})
        self.assertEqual(self.app.api_keys("c1", {"keys": ["esc"]}), {"ok": True})
        self.assertEqual(self.hc.send_calls, [("c1", "hello")])
        self.assertEqual(self.err(self.app.api_send, "sh", {"text": "ls"}), (403, "not_claude"))
        self.assertEqual(self.err(self.app.api_keys, "sh", {"keys": ["enter"]}), (403, "not_claude"))
        self.assertEqual(self.err(self.app.api_prompt, "sh"), (403, "not_claude"))

    def test_terminals_flag_codex_as_capable(self):
        by = {t["id"]: t for t in self.app.api_terminals()["terminals"]}
        self.assertEqual((by["c1"]["claude"], by["c1"]["agent_kind"]), (True, "codex"))
        self.assertEqual((by["sh"]["claude"], by["sh"]["agent_kind"]), (False, None))

    def test_push_carries_codex_card(self):
        self.hc.set_status("c1", {"app": "codex", "state": "working"})
        self.app.poll(initial=True)
        self.hc.screens["c1"] = cx("patch")
        self.hc.set_status("c1", BLOCKED_PERM)
        self.app.handle_event_line(status_ev("c1", BLOCKED_PERM))
        p = self.pusher.payloads[-1]
        self.assertEqual((p["category"], p["kind"], p["prompt_id"]),
                         ("HR_PERMISSION_ALWAYS", "approval", card("patch")["id"]))

    def test_push_waits_for_menu_drawn_after_blocked(self):
        # 맥미니 실측: hc가 blocked를 알린 뒤 0.9초쯤 지나야 승인 메뉴가 그려진다
        self.hc.set_status("c1", {"app": "codex", "state": "working"})
        self.app.poll(initial=True)
        screens = [cx("idle"), cx("idle"), cx("idle")]
        real_read = self.hc.read

        def read(tid, lines=0):
            if screens:
                self.hc.read_calls += 1
                return {"text": screens.pop(0)}
            return real_read(tid, lines)
        self.hc.read = read
        self.hc.screens["c1"] = cx("exec")
        self.hc.set_status("c1", BLOCKED_PERM)
        self.app.handle_event_line(status_ev("c1", BLOCKED_PERM))
        p = self.pusher.payloads[-1]
        self.assertEqual((p["category"], p["prompt_id"]), ("HR_PERMISSION", card("exec")["id"]))
        self.assertEqual(self.hc.read_calls, 4)

    def test_push_without_menu_gives_up_within_wait(self):
        import time
        self.hc.set_status("c1", {"app": "codex", "state": "working"})
        self.app.poll(initial=True)
        self.hc.screens["c1"] = cx("idle")
        self.hc.set_status("c1", BLOCKED_PERM)
        t0 = time.monotonic()
        with mock.patch.object(hr, "CODEX_PUSH_PROMPT_WAIT", 0.5):
            self.app.handle_event_line(status_ev("c1", BLOCKED_PERM))
        self.assertLess(time.monotonic() - t0, 1.0)
        p = self.pusher.payloads[-1]
        self.assertEqual((p["category"], p["prompt_id"]), ("HR_BLOCKED", None))  # 카드 없으면 버튼 없는 알림

    def test_claude_terminal_still_uses_claude_parser(self):
        self.hc.add("t1", st("blocked", "approve Bash: mkdir sub", "permission"))
        self.hc.screens["t1"] = fixture("claude-bash-2.1.329.txt")
        p = self.app.api_prompt("t1")["prompt"]
        self.assertEqual((p["kind"], p.get("agent")), ("approval", None))
        self.assertEqual(p["id"], hr.parse_prompt(fixture("claude-bash-2.1.329.txt"))["id"])


class CodexTranscriptTests(unittest.TestCase):
    def setUp(self):
        self.home = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)
        self.day = os.path.join(self.home, ".codex", "sessions", "2026", "11", "21")
        os.makedirs(self.day)
        self.name = "rollout-2026-11-21T14-59-17-01a1fcb0-0a5f-74d1-ab8a-e5a7b55abe99.jsonl"
        self.path = os.path.join(self.day, self.name)
        shutil.copy(os.path.join(os.path.dirname(__file__), "fixtures", "codex-session-%s.jsonl" % V), self.path)
        hr._preview_by_file.clear()
        hr._preview_by_pid.clear()
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        os.environ.pop("CODEX_HOME", None)
        self.addCleanup(env.stop)

    def test_parse_rollout(self):
        with open(self.path, encoding="utf-8") as f:
            turns, cut = hr.parse_transcript(f.read().split("\n"))
        self.assertFalse(cut)
        users = [t["parts"][0]["text"] for t in turns if t["role"] == "user"]
        self.assertEqual(users, ["hi", "edit files", "x"])  # AGENTS.md·환경 주입·developer는 숨김
        last = turns[-1]
        self.assertEqual(last["role"], "assistant")
        tools = [p for p in last["parts"] if p["kind"] == "tool"]
        self.assertEqual([(t["name"], t["summary"], t["state"]) for t in tools],
                         [("exec_command", "touch d.txt", "ok")] * 2)
        self.assertIn("Process exited with code 0", tools[0]["output"])
        self.assertEqual(last["parts"][-1], {"kind": "text", "text": "ok"})
        self.assertEqual(hr.preview_for_path(self.path), "ok")

    def lsof(self, out, calls=None):
        def run(argv, **kw):
            if calls is not None:
                calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, out, "")
        return run

    @unittest.skipIf(hr.IS_WINDOWS, "lsof는 맥·리눅스")
    def test_find_by_open_file(self):
        calls = []
        info = {"agent": "codex", "pid": 4242, "cwd": "/elsewhere"}
        run = self.lsof("p4242\nfcwd\nn/Users/me\nf88\nn%s\n" % self.path, calls)
        self.assertEqual(hr.find_codex_transcript(info, self.home, runner=run),
                         ("01a1fcb0-0a5f-74d1-ab8a-e5a7b55abe99", self.path))
        self.assertEqual(calls[0][1:], ["-a", "-p", "4242", "-Fn"])
        # sessions 밖 파일, 심볼릭 링크는 거부
        out = os.path.join(self.home, self.name)
        shutil.copy(self.path, out)
        link = os.path.join(self.day, "rollout-2026-11-21T15-00-00-aaaaaaaa-0000-0000-0000-000000000000.jsonl")
        os.symlink(out, link)
        bad = self.lsof("n%s\nn%s\n" % (out, link))
        self.assertEqual(hr.find_codex_transcript(info, self.home, runner=bad), (None, None))

    def test_fallback_by_cwd(self):
        none = self.lsof("")
        info = {"agent": "codex", "pid": 4242, "cwd": "/Users/me/work/demo"}
        self.assertEqual(hr.find_codex_transcript(info, self.home, runner=none)[1], self.path)
        self.assertEqual(hr.find_codex_transcript(dict(info, cwd="/other"), self.home, runner=none), (None, None))

    def test_api_transcript_for_codex(self):
        app, hc, _ = make_app(home=self.home, runner=self.lsof("n%s\n" % self.path))
        hc.add("c1", {"app": "codex", "state": "idle"}, agent="codex")
        hc.infos["c1"]["pid"] = 4242
        r = app.api_transcript("c1", "10")
        self.assertEqual(r["session_id"], "01a1fcb0-0a5f-74d1-ab8a-e5a7b55abe99")
        self.assertEqual([t["role"] for t in r["turns"]][:2], ["user", "assistant"])
        if not hr.IS_WINDOWS:
            by = {t["id"]: t for t in app.api_terminals()["terminals"]}
            self.assertEqual(by["c1"]["preview"], "ok")


if __name__ == "__main__":
    unittest.main()
