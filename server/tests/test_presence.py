"""PC 앞에 있으면(입력이 presence_quiet_sec 안에 있었으면) 끝남 알림을 보내지 않는다.
막힘·재알림·종료 알림은 그대로. 건너뛴 끝남은 그 사용자 말 몫을 쓴 것으로 친다."""
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_done_once as do  # noqa: E402
import test_hook_backend as hb  # noqa: E402
import test_hook_wake as hw  # noqa: E402

hr = hb.hr
cats = do.cats

# 이 맥(macOS 27)의 ioreg -c IOHIDSystem -d 4 실제 출력 한 줄
IOREG_SAMPLE = ('+-o IOHIDSystem  <class IOHIDSystem, id 0x100000abc, registered, matched, active, busy 0 (0 ms), '
                'retain 20>\n    | {\n    | |     "HIDIdleTime" = 82482958\n    | |     "HIDParameters" = {}\n')


class Idle:
    def __init__(self, value):
        self.value = value
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.value


class TrackerPresenceTests(unittest.TestCase):
    def make(self, idle, quiet=120):
        self.sent = []
        self.idle = Idle(idle)
        self.t = hr.Tracker(self.sent.append, presence=self.idle, quiet_sec=quiet)
        return self.t

    def obs(self, tid, state, turn, msg=None):
        return self.t.observe(tid, {"state": state, "msg": msg}, turn=turn)

    def test_active_skips_done_and_logs(self):
        self.make(30)
        self.obs("t1", "working", 1)
        with self.assertLogs(hr.log, "INFO") as lg:
            self.assertIsNone(self.obs("t1", "done", 1))
        self.assertEqual(self.sent, [])
        self.assertIn("끝남 알림 생략 t1: PC 사용 중(입력 30초 전)", lg.output[0])
        self.assertEqual(self.t.last["t1"], ("done", None))  # 상태는 바뀐다

    def test_idle_long_enough_sends(self):
        self.make(120)
        self.obs("t1", "done", 1)
        self.assertEqual([p["state"] for p in self.sent], ["done"])

    def test_unknown_sends(self):
        self.make(None)
        self.obs("t1", "done", 1)
        self.assertEqual(len(self.sent), 1)

    def test_zero_disables(self):
        self.make(1, quiet=0)
        self.obs("t1", "done", 1)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.idle.calls, 0)

    def test_no_presence_unchanged(self):
        self.sent = []
        self.t = hr.Tracker(self.sent.append)
        self.obs("t1", "done", 1)
        self.assertEqual(len(self.sent), 1)

    def test_blocked_unaffected_while_active(self):
        self.make(1)
        self.obs("t1", "blocked", 1, msg="approve Bash")
        self.assertEqual([p["state"] for p in self.sent], ["blocked"])
        self.assertEqual(self.idle.calls, 0)

    def test_exited_unaffected_while_active(self):
        self.make(1)
        self.t.mark_claude("t1", "x")
        self.assertIsNotNone(self.t.exited("t1"))
        self.assertEqual(len(self.sent), 1)

    def test_skipped_counts_as_notified_for_user_message(self):
        t = self.make(5)
        t.gate_done("h1")
        t.arm_done("h1")
        self.obs("h1", "working", 1)
        self.obs("h1", "done", 1)  # 사용자가 PC 앞: 생략
        self.idle.value = 600  # 자리를 떴다
        self.obs("h1", "working", 2)
        self.obs("h1", "done", 2)  # 자동으로 깨어난 턴: 이 말의 끝남은 이미 처리했다
        self.assertEqual(self.sent, [])
        t.arm_done("h1")  # 다음 사용자 말(폰에서 보낸 글 등)
        self.obs("h1", "working", 3)
        self.obs("h1", "done", 3)
        self.assertEqual(len(self.sent), 1)

    def test_skipped_turn_not_resent_ungated(self):
        self.make(5)
        self.obs("t1", "done", 1)
        self.idle.value = 600
        self.obs("t1", "working", 1)
        self.obs("t1", "done", 1)  # 같은 turn의 done이 또 와도(이벤트·폴링 겹침) 다시 알리지 않는다
        self.assertEqual(self.sent, [])
        self.obs("t1", "working", 2)
        self.obs("t1", "done", 2)
        self.assertEqual(len(self.sent), 1)


class HookPresenceTests(hw.WakeBase):
    def test_claude_done_skipped_then_auto_turn_silent(self):
        idle = Idle(10)
        self.app.tracker.presence = idle
        self.app.tracker.quiet_sec = 120
        self.hook("claude", hb.load("claude-user-prompt-submit-2.1.329.json", prompt="고쳐줘", prompt_id="p1"))
        n = len(self.pusher.payloads)
        self.hook("claude", self.stop_body(prompt_id="p1"))
        self.assertEqual(self.status(do.TID)["state"], "done")
        idle.value = 900
        self.hook("claude", self.stop_body(prompt_id="p2"))  # 자리를 뜬 뒤 자동 턴
        self.assertEqual(cats(self.pusher.payloads[n:]), [])
        self.hook("claude", hb.load("claude-user-prompt-submit-2.1.329.json", prompt="다음", prompt_id="p3"))
        self.hook("claude", self.stop_body(prompt_id="p3"))
        self.assertEqual(cats(self.pusher.payloads[n:]), ["HR_DONE"])

    def test_app_reads_config(self):
        self.assertEqual(self.app.tracker.quiet_sec, hr.PRESENCE_QUIET_SEC)
        self.assertIsInstance(self.app.tracker.presence, hr.IdleProbe)
        self.assertIsNone(self.app.tracker.presence())  # 테스트는 pc_idle_sec를 모름으로 바꿔 둔다


class ParseTests(unittest.TestCase):
    def test_ioreg_sample(self):
        self.assertAlmostEqual(hr.parse_hid_idle(IOREG_SAMPLE), 0.082482958)

    def test_ioreg_missing(self):
        self.assertIsNone(hr.parse_hid_idle(""))
        self.assertIsNone(hr.parse_hid_idle(None))
        self.assertIsNone(hr.parse_hid_idle('"HIDIdleTime" = abc'))

    def test_mac_idle_runner(self):
        calls = []

        def runner(args, **kw):
            calls.append((args, kw.get("timeout")))
            return subprocess.CompletedProcess(args, 0, IOREG_SAMPLE.replace("82482958", "45000000000"), "")
        self.assertAlmostEqual(hr.mac_idle_sec(runner), 45.0)
        self.assertEqual(calls, [([hr.IOREG_PATH, "-c", "IOHIDSystem", "-d", "4"], hr.PRESENCE_CMD_TIMEOUT)])

    def test_mac_idle_failures(self):
        def bad(args, **kw):
            return subprocess.CompletedProcess(args, 1, "", "err")

        def timeout(args, **kw):
            raise subprocess.TimeoutExpired(args, 2)

        def missing(args, **kw):
            raise FileNotFoundError("ioreg")
        for r in (bad, timeout, missing):
            self.assertIsNone(hr.mac_idle_sec(r))

    def test_win_tick_math(self):
        self.assertEqual(hr.win_idle_ms(10_000, 7_000), 3_000)
        self.assertEqual(hr.win_idle_ms(5_000, 5_000), 0)
        # 켠 지 50일 넘어 GetTickCount(32비트)가 한 바퀴 돈 뒤: 하위 32비트끼리 뺀다
        tick64 = 2 ** 32 + 1_000
        self.assertEqual(hr.win_idle_ms(tick64, 2 ** 32 - 2_000), 3_000)
        self.assertEqual(hr.win_idle_ms(tick64, 500), 500)
        # 엉터리: 켜진 시간보다 긴 idle, 값 없음
        self.assertIsNone(hr.win_idle_ms(1_000, 2_000))
        self.assertIsNone(hr.win_idle_ms(None, 1))
        self.assertIsNone(hr.win_idle_ms(1, None))

    def test_config(self):
        self.assertEqual(hr.presence_quiet_sec({}), 120)
        self.assertEqual(hr.presence_quiet_sec({"presence_quiet_sec": 0}), 0)
        self.assertEqual(hr.presence_quiet_sec({"presence_quiet_sec": 300}), 300)
        with self.assertLogs(hr.log, "WARNING"):
            for bad in (-1, "60", True, None, [60]):
                self.assertEqual(hr.presence_quiet_sec({"presence_quiet_sec": bad}), 120)


class IdleProbeTests(unittest.TestCase):
    def test_cache_and_age(self):
        now = [100.0]
        reads = Idle(10.0)
        p = hr.IdleProbe(reads, clock=lambda: now[0], ttl=5)
        self.assertEqual(p(), 10.0)
        now[0] = 103.0
        self.assertEqual(p(), 13.0)  # 다시 읽지 않고 지난 시간만큼 더한다
        self.assertEqual(reads.calls, 1)
        now[0] = 105.0
        reads.value = 0.5
        self.assertEqual(p(), 0.5)
        self.assertEqual(reads.calls, 2)

    def test_unknown_cached(self):
        now = [0.0]
        reads = Idle(None)
        p = hr.IdleProbe(reads, clock=lambda: now[0], ttl=5)
        self.assertIsNone(p())
        now[0] = 1.0
        self.assertIsNone(p())
        self.assertEqual(reads.calls, 1)


if __name__ == "__main__":
    unittest.main()


class IoregPathTest(unittest.TestCase):
    def test_uses_absolute_ioreg_path(self):
        # launchd PATH에는 /usr/sbin이 없다: ioreg를 이름만으로 부르면 못 찾는다
        seen = []

        class P:
            returncode = 0
            stdout = '    | |   "HIDIdleTime" = 5000000000\n'

        def runner(argv, **kw):
            seen.append(argv[0])
            return P()
        self.assertEqual(hr.mac_idle_sec(runner=runner), 5.0)
        if os.path.exists(hr.IOREG_PATH):
            self.assertEqual(seen[0], "/usr/sbin/ioreg")
