"""끝남 알림을 사용자 말 하나당 한 번으로: 훅 세션(Claude·Codex·Qwen)은 사용자 말(UserPromptSubmit의 글,
폰에서 보낸 글) 뒤 첫 Stop만 알리고, 자동으로 깨어난 턴(<task-notification> 등)의 Stop은 상태만 바꾼다."""
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_hook_backend as hb  # noqa: E402
import test_hook_wake as hw  # noqa: E402

hr = hb.hr
load = hb.load
SID = hw.SID
TID = hw.TID

TASK_NOTE = ("<task-notification>\n<task-id>af884a0c59768e2e9</task-id>\n<status>completed</status>\n"
             "<summary>Agent \"x\" completed</summary>\n</task-notification>")


def cats(payloads):
    return [p["aps"]["category"] for p in payloads]


class PromptIsUserTests(unittest.TestCase):
    def test_real_text(self):
        self.assertTrue(hr.hook_prompt_is_user("이어서 해"))
        self.assertTrue(hr.hook_prompt_is_user("<system-reminder>x</system-reminder>\n고쳐줘"))
        self.assertTrue(hr.hook_prompt_is_user("/model"))
        self.assertTrue(hr.hook_prompt_is_user(None))  # prompt를 안 주는 버전: 막지 않는다

    def test_injected_only(self):
        for p in (TASK_NOTE,
                  "<system-reminder>Linked sessions now</system-reminder>",
                  "<side-sessions-event>a</side-sessions-event>",
                  "<ci-monitor-event>b</ci-monitor-event>",
                  "<system-reminder>r</system-reminder>\n" + TASK_NOTE,
                  "<local-command-stdout>Set model</local-command-stdout>",
                  "<command-name>/model</command-name>",
                  '<scheduled-task name="x" file="/y">run</scheduled-task>',
                  'Another Claude session sent a message: <cross-session-message from="a">hi</cross-session-message>',
                  "", "   "):
            self.assertFalse(hr.hook_prompt_is_user(p), p)

    def test_phone_wake_block_is_user(self):
        p = ('<system-reminder>\nStop hook blocking error from command "Stop": %s\n이어서 해\n</system-reminder>'
             % hr.HOOK_WAKE_MARK)
        self.assertTrue(hr.hook_prompt_is_user(p))


class TrackerGateTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.t = hr.Tracker(self.sent.append)

    def obs(self, tid, state, turn):
        return self.t.observe(tid, {"state": state}, turn=turn)

    def test_ungated_unchanged(self):
        for turn in (1, 2, 3):
            self.obs("t1", "working", turn)
            self.obs("t1", "done", turn)
        self.assertEqual(len(self.sent), 3)

    def test_gated_first_allowed_then_once_per_arm(self):
        self.t.gate_done("h1")
        self.obs("h1", "done", 1)  # 사용자 말을 아직 못 봤다(서버 재시작 등): 첫 끝남은 알린다
        self.obs("h1", "working", 1)
        self.obs("h1", "done", 2)
        self.assertEqual(len(self.sent), 1)
        self.t.arm_done("h1")
        self.obs("h1", "working", 2)
        self.obs("h1", "done", 3)
        self.obs("h1", "working", 3)
        self.obs("h1", "done", 4)
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.t.last["h1"], ("done", None))  # 상태는 그대로 바뀐다

    def test_blocked_not_gated(self):
        self.t.gate_done("h1")
        self.obs("h1", "done", 1)
        self.t.observe("h1", {"state": "blocked", "msg": "approve Bash"}, turn=1)
        self.obs("h1", "done", 2)
        self.assertEqual([p["state"] for p in self.sent], ["done", "blocked"])

    def test_forget_resets(self):
        self.t.gate_done("h1")
        self.obs("h1", "done", 1)
        self.t.forget("h1")
        self.obs("h1", "done", 2)
        self.assertEqual(len(self.sent), 2)


class HookDoneOnceTests(hw.WakeBase):
    def stop(self, pid, **over):
        return self.hook("claude", self.stop_body(prompt_id=pid, **over))

    def ups(self, prompt, pid):
        return self.hook("claude", load("claude-user-prompt-submit-2.1.329.json", prompt=prompt, prompt_id=pid))

    def test_claude_one_done_per_user_message(self):
        self.hook("claude", load("claude-session-start-2.1.329.json"))
        n = len(self.pusher.payloads)
        self.ups("고쳐줘", "p1")
        self.stop("p1")
        self.assertEqual(cats(self.pusher.payloads[n:]), ["HR_DONE"])
        # 백그라운드 작업 알림으로 깨어난 턴: 글이 내부 블록뿐인 UserPromptSubmit → 상태만
        self.ups(TASK_NOTE, "p2")
        self.assertEqual(self.status(TID)["state"], "working")
        self.stop("p2")
        self.assertEqual(self.status(TID)["state"], "done")
        # UserPromptSubmit 없이 새 prompt_id의 Stop만(자동으로 깨어난 턴) → 상태만
        self.stop("p3")
        self.ups("<side-sessions-event>x</side-sessions-event>", "p4")
        self.stop("p4")
        self.ups('<scheduled-task name="a">run</scheduled-task>', "p5")
        self.stop("p5")
        self.assertEqual(cats(self.pusher.payloads[n:]), ["HR_DONE"])
        # 다음 사용자 말 → 한 번 더
        self.ups("<system-reminder>r</system-reminder>\n다음 거 해", "p6")
        self.stop("p6")
        self.stop("p7")
        self.assertEqual(cats(self.pusher.payloads[n:]), ["HR_DONE", "HR_DONE"])

    def test_restart_first_done_allowed(self):
        # 서버가 다시 떠 세션을 처음 보는데 사용자 말을 못 봤다: 첫 끝남은 알리고 그다음 자동 턴은 알리지 않는다
        self.stop("p1")
        self.stop("p2")
        self.assertEqual(cats(self.pusher.payloads), ["HR_DONE"])

    def test_phone_send_rearms(self):
        self.ups("고쳐줘", "p1")
        self.stop("p1")
        self.stop("p2")  # 자동 턴
        n = len(self.pusher.payloads)
        box = self.wait_bg(self.stop_body(prompt_id="p2"))
        self.attached()
        self.assertEqual(self.send("이어서 해"), (200, {"ok": True, "queued": False}))
        self.assertEqual(self.done(box), (200, {"text": "이어서 해"}))
        self.stop("p3")  # 폰 글로 깨어난 턴(UserPromptSubmit이 안 와도)
        self.stop("p4")  # 그 뒤 자동 턴
        self.assertEqual(cats(self.pusher.payloads[n:]), ["HR_DONE"])

    def test_blocked_still_notified_after_done(self):
        self.ups("고쳐줘", "p1")
        self.stop("p1")
        n = len(self.pusher.payloads)
        self.ups(TASK_NOTE, "p2")  # 자동 턴에서 권한을 묻는다
        box = {}

        def run():
            try:
                box["r"] = self.hook("claude", load("claude-permission-bash-2.1.329.json", session_id=SID))
            except Exception as e:  # noqa: BLE001 - PC에서 풀려 응답 없이 끊긴다(HOOK_NO_REPLY)
                box["err"] = e
        box["th"] = threading.Thread(target=run, daemon=True)
        box["th"].start()
        self.blocked(TID)
        self.assertEqual(cats(self.pusher.payloads[n:]), ["HR_PERMISSION"])
        self.stop("p2")  # PC에서 답하고 턴 끝: 붙잡기는 응답 없이 풀린다
        box["th"].join(5)
        self.assertFalse(box["th"].is_alive())
        self.assertEqual(cats(self.pusher.payloads[n:]), ["HR_PERMISSION"])

    def test_qwen_gated(self):
        body = load("qwen-user-prompt-submit-0.28.3.json")
        tid = self.tid_of(body)
        n = len(self.pusher.payloads)
        self.hook("qwen", body)
        self.hook("qwen", load("qwen-stop-0.28.3.json"))
        self.hook("qwen", load("qwen-stop-0.28.3.json", prompt_id="q2"))
        self.assertEqual(self.status(tid)["state"], "done")
        self.assertEqual(cats(self.pusher.payloads[n:]), ["HR_DONE"])

    def test_opencode_not_gated(self):
        sid = "ses_done_once_opencode1"
        start = {"hook_event_name": "SessionStart", "session_id": sid, "cwd": self.home}
        self.hook("opencode", start)
        n = len(self.pusher.payloads)
        for i in (1, 2):
            self.hook("opencode", dict(start, hook_event_name="UserPromptSubmit"))
            self.hook("opencode", dict(start, hook_event_name="Stop", turn_id="%s#%d" % (sid, i)))
        self.assertEqual(cats(self.pusher.payloads[n:]), ["HR_DONE", "HR_DONE"])


if __name__ == "__main__":
    unittest.main()
