"""알림 무음 실패 방지: 발송 기록(PushLog), 중계 5xx 재시도, 미답 재알림, /api/push/*, health의 push."""
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import sheltie as hr  # noqa: E402
import test_sheltie as base  # noqa: E402 - 가짜 hc·중계·HTTP 도우미 재사용

TOKEN = base.TOKEN
SIG = base.SIG
TOK_A = "aa" * 32
TOK_B = "bb" * 32


def did(tok):
    return hr.device_id(tok)


class Wall:
    def __init__(self, t=1_700_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


# ---------------------------------------------------------------- PushLog

class PushLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "push_status.json")
        self.wall = Wall()
        self.log = hr.PushLog(self.path, wall=self.wall)

    def test_device_id_matches_relay_resign(self):
        devices = hr.DeviceStore(os.path.join(self.tmp, "devices.json"))
        devices.add(TOK_A, "sandbox", relay_sig=SIG)
        devices.mark_relay_sig_bad(TOK_A, SIG)
        self.assertEqual(devices.relay_resign(), [did(TOK_A)])
        self.assertEqual(hr.device_id(TOK_A.upper()), did(TOK_A))

    def test_success_failure_and_relay_limit(self):
        self.log.record(TOK_A, 200, "")
        t_ok = self.log.now_ms()
        e = self.log.device(did(TOK_A))
        self.assertEqual(e, {"last_success_at": t_ok, "last_attempt_at": t_ok, "last_error": None})
        self.wall.t += 5
        self.log.record(TOK_A, 410, "Unregistered" + "x" * 200)
        t_err = self.log.now_ms()
        e = self.log.device(did(TOK_A))
        self.assertEqual(e["last_success_at"], t_ok)
        self.assertEqual(e["last_attempt_at"], t_err)
        self.assertEqual(e["last_error"]["at"], t_err)
        self.assertEqual(e["last_error"]["code"], 410)
        self.assertEqual(len(e["last_error"]["reason"]), 100)
        # 성공해도 last_error는 남는다(앱이 시각으로 판단)
        self.wall.t += 5
        self.log.record(TOK_A, 200, "")
        e = self.log.device(did(TOK_A))
        self.assertEqual(e["last_error"]["at"], t_err)
        self.assertGreater(e["last_success_at"], e["last_error"]["at"])
        self.log.relay_limited()
        s = self.log.summary()
        self.assertEqual(s, {"last_success_at": e["last_success_at"], "last_error_at": t_err,
                             "relay_limited_at": self.log.now_ms()})
        self.assertIsNone(self.log.device(did(TOK_B)))

    def test_saved_0600_reloaded_and_no_token(self):
        self.log.record(TOK_A, 200, "")
        self.log.record(TOK_B, 0, "no_relay_sig")
        self.log.relay_limited()
        if os.name != "nt":  # Windows는 POSIX 권한 비트가 없다
            self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        with open(self.path) as f:
            raw = f.read()
        self.assertNotIn(TOK_A, raw)
        self.assertNotIn(TOK_B, raw)
        again = hr.PushLog(self.path, wall=self.wall)
        self.assertEqual(again.summary(), self.log.summary())
        self.assertEqual(again.device(did(TOK_B)), self.log.device(did(TOK_B)))
        self.assertEqual(again.device(did(TOK_B))["last_error"]["reason"], "no_relay_sig")

    def test_bad_file_starts_empty(self):
        with open(self.path, "w") as f:
            f.write("{nope")
        self.assertEqual(hr.PushLog(self.path).summary(),
                         {"last_success_at": None, "last_error_at": None, "relay_limited_at": None})
        with open(self.path, "w") as f:
            json.dump({"last_success_at": "x", "devices": {"../x": {}, did(TOK_A): {"last_error": {"at": True}}}}, f)
        pl = hr.PushLog(self.path)
        self.assertIsNone(pl.summary()["last_success_at"])
        self.assertEqual(pl.device(did(TOK_A)), {"last_success_at": None, "last_attempt_at": None, "last_error": None})
        self.assertIsNone(pl.device("../x"))

    def test_save_failure_does_not_raise(self):
        blocker = os.path.join(self.tmp, "file")
        open(blocker, "w").close()
        pl = hr.PushLog(os.path.join(blocker, "push_status.json"))  # 부모가 파일이라 저장 불가
        with self.assertLogs(hr.log, "WARNING"):
            pl.record(TOK_A, 200, "")
        self.assertIsNotNone(pl.summary()["last_success_at"])

    def test_device_cap(self):
        for i in range(hr.PUSH_LOG_MAX_DEVICES + 3):
            self.wall.t += 1
            self.log.record("%064x" % i, 200, "")
        self.assertEqual(len(self.log.data["devices"]), hr.PUSH_LOG_MAX_DEVICES)
        self.assertIsNone(self.log.device(did("%064x" % 0)))
        self.assertIsNotNone(self.log.device(did("%064x" % (hr.PUSH_LOG_MAX_DEVICES + 2))))


# ---------------------------------------------------------------- 발송이 결과를 남긴다

class ApnsRecordTests(unittest.TestCase):
    def test_push_records_every_device(self):
        tmp = tempfile.mkdtemp()
        cfg = dict(hr.DEFAULTS, apns_key_path=os.path.join(tmp, "AuthKey.p8"))
        open(cfg["apns_key_path"], "w").close()
        devices = hr.DeviceStore(os.path.join(tmp, "devices.json"))
        for t in ("aa", "bb", "cc"):
            devices.add(t * 32, "sandbox")
        outs = ["\n200", '{"reason":"Unregistered"}\n410', None]

        def runner(argv, **kw):
            o = outs.pop(0)
            if o is None:
                raise subprocess.TimeoutExpired(argv, 30)
            return subprocess.CompletedProcess(argv, 0, o, "")

        pl = hr.PushLog(os.path.join(tmp, "push_status.json"))
        p = hr.ApnsPusher(cfg, devices, runner=runner, push_log=pl)
        p.jwt = lambda: "JWT"
        self.assertEqual(p.push(hr.build_payload("t1", "n", "done", {})), 1)
        self.assertIsNone(pl.device(did("aa" * 32))["last_error"])
        self.assertEqual(pl.device(did("bb" * 32))["last_error"]["code"], 410)
        self.assertEqual(pl.device(did("cc" * 32))["last_error"],
                         {"at": pl.device(did("cc" * 32))["last_attempt_at"], "code": 0, "reason": "TimeoutExpired"})
        self.assertEqual([d["token"] for d in devices.list()], ["aa" * 32, "cc" * 32])  # 410 삭제는 그대로
        with open(pl.path) as f:
            raw = f.read()
        for t in ("aa", "bb", "cc"):
            self.assertNotIn(t * 32, raw)


class RelayRetryTests(unittest.TestCase):
    def setUp(self):
        self.relay = base.FakeRelay()
        self.tmp = tempfile.mkdtemp()
        cfg = dict(hr.DEFAULTS, push_relay_url=self.relay.url, _dir=self.tmp)
        self.devices = hr.DeviceStore(os.path.join(self.tmp, "devices.json"))
        self.pl = hr.PushLog(os.path.join(self.tmp, "push_status.json"))
        self.sleeps = []
        self.p = hr.RelayPusher(cfg, self.devices, sleep=self.sleeps.append, push_log=self.pl)
        self.devices.add(TOK_A, "sandbox", relay_sig=SIG)

    def tearDown(self):
        self.p._drop_conn()
        self.relay.close()

    def test_500_then_200_retries_once_with_same_collapse_id(self):
        self.relay.responses = [(500, {"error": "internal"}, False), (200, {"ok": True}, False)]
        self.assertEqual(self.p.push(hr.build_payload("t1", "n", "done", {})), 1)
        self.assertEqual(len(self.relay.requests), 2)
        a, b = (r[2] for r in self.relay.requests)
        self.assertEqual(a, b)  # 본문(collapse_id 포함)이 같다
        self.assertEqual(self.sleeps, [1.0])
        e = self.pl.device(did(TOK_A))
        self.assertIsNone(e["last_error"])
        self.assertIsNotNone(e["last_success_at"])

    def test_500_twice_records_failure(self):
        self.relay.responses = [(502, {"error": "upstream_failed"}, False), (503, {"error": "busy"}, False)]
        self.assertEqual(self.p.push(hr.build_payload("t1", "n", "done", {})), 0)
        self.assertEqual(len(self.relay.requests), 2)
        self.assertEqual(self.sleeps, [1.0])
        err = self.pl.device(did(TOK_A))["last_error"]
        self.assertEqual((err["code"], err["reason"]), (503, "relay:busy"))
        self.assertIsNone(self.pl.summary()["last_success_at"])
        self.assertEqual(len(self.devices.list()), 1)

    def test_429_no_retry_and_marks_relay_limited(self):
        self.relay.responses = [(429, {"error": "rate_limited", "scope": "server"}, False)]
        self.assertEqual(self.p.push(hr.build_payload("t1", "n", "done", {})), 0)
        self.assertEqual(len(self.relay.requests), 1)
        self.assertEqual(self.sleeps, [])
        self.assertIsNotNone(self.pl.summary()["relay_limited_at"])
        err = self.pl.device(did(TOK_A))["last_error"]
        self.assertEqual((err["code"], err["reason"]), (429, "relay:rate_limited"))

    def test_bad_sig_and_no_sig_recorded(self):
        self.devices.add(TOK_B, "sandbox")  # 서명 없음
        self.relay.responses = [(403, {"error": "bad_sig"}, False)]
        self.assertEqual(self.p.push(hr.build_payload("t1", "n", "done", {})), 0)
        self.assertEqual(self.pl.device(did(TOK_A))["last_error"]["reason"], "relay:bad_sig")
        self.assertEqual(self.pl.device(did(TOK_B))["last_error"]["reason"], "no_relay_sig")

    def test_push_device_sends_to_one(self):
        self.devices.add(TOK_B, "sandbox", relay_sig=SIG)
        dev = [d for d in self.devices.list() if d["token"] == TOK_B][0]
        self.assertEqual(self.p.push_device(dev, {"x": 1}), (200, ""))
        self.assertEqual([r[2]["device_token"] for r in self.relay.requests], [TOK_B])
        self.assertRegex(self.relay.requests[0][2]["collapse_id"], r"^[0-9a-f]{32}$")
        self.assertIsNotNone(self.pl.device(did(TOK_B))["last_success_at"])
        self.assertIsNone(self.pl.device(did(TOK_A)))


# ---------------------------------------------------------------- 미답 재알림

def make_app(**cfg_over):
    tmp = tempfile.mkdtemp()
    cfg = dict(hr.DEFAULTS)
    cfg.update(token=TOKEN, bind="127.0.0.1", port=0, _dir=tmp, _path=os.path.join(tmp, "config.json"))
    cfg.update(cfg_over)
    hc = base.FakeHc()
    pusher = base.FakePusher()
    devices = hr.DeviceStore(os.path.join(tmp, "devices.json"))
    return hr.App(cfg, hc, devices, pusher), hc, pusher


class ReminderTests(unittest.TestCase):
    def setUp(self):
        self.app, self.hc, self.pusher = make_app()
        self.now = [1000.0]
        self.app.reminders.clock = lambda: self.now[0]
        self.hc.add("t1", base.st("working"), name="hrtest")
        self.app.poll(initial=True)

    def at(self, t):
        self.now[0] = 1000.0 + t
        self.app.poll()
        self.app.check_reminders()
        return [p for p in self.pusher.payloads if "reminder" in p]

    def block(self, status=base.PERM):
        self.hc.set_status("t1", status)
        self.app.poll()

    def test_schedule_5_and_30_minutes_then_stop(self):
        self.hc.screens["t1"] = base.approval_screen(["Yes", "No"])
        self.block()
        first = self.pusher.payloads[0]
        self.assertNotIn("reminder", first)
        self.assertEqual(self.at(299), [])
        r = self.at(300)
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0]["reminder"], 1)
        self.assertEqual(r[0]["aps"]["alert"]["subtitle"], "Reminder · Permission needed")
        self.assertEqual(r[0]["aps"]["alert"]["subtitle-loc-key"], "push.subtitle.reminder.permission")
        self.assertEqual(r[0]["aps"]["thread-id"], "t1")
        self.assertEqual(r[0]["aps"]["alert"]["title"], "hrtest")
        self.assertEqual(r[0]["server"], hr.server_tag(TOKEN))
        self.assertEqual((r[0]["prompt_id"], r[0]["kind"]), (first["prompt_id"], first["kind"]))
        self.assertIsNotNone(r[0]["prompt_id"])
        self.assertEqual(len(self.at(600)), 1)
        self.assertEqual(len(self.at(1799)), 1)
        r = self.at(1800)
        self.assertEqual([p["reminder"] for p in r], [1, 2])
        self.assertEqual(len(self.at(10000)), 2)
        self.assertEqual(len(self.pusher.payloads), 3)

    def test_card_is_reread_for_reminder(self):
        self.block()
        reads = self.hc.read_calls
        self.at(300)
        self.assertEqual(self.hc.read_calls, reads + 1)

    def test_resolved_cancels(self):
        self.block()
        self.at(100)
        self.hc.set_status("t1", base.st("working"))
        self.assertEqual(self.at(300), [])
        self.assertEqual(self.at(2000), [])

    def test_resolved_between_poll_and_check_skips(self):
        self.block()
        self.now[0] = 1300.0
        self.hc.set_status("t1", base.st("working"))  # 폴링 전에 풀렸다
        self.app.check_reminders()
        self.assertEqual([p for p in self.pusher.payloads if "reminder" in p], [])

    def test_blocked_reminder_loc_key(self):
        orig = hr.PUSH_KIND_KEYS
        hr.PUSH_KIND_KEYS = True  # 질문·계획 키는 새 앱이 퍼진 뒤 켠다: 켰을 때의 동작
        self.addCleanup(setattr, hr, "PUSH_KIND_KEYS", orig)
        self.block({"app": "claude-code", "kind": "question", "msg": "which one?", "state": "blocked"})
        r = self.at(300)
        a = r[0]["aps"]["alert"]
        self.assertEqual((a["subtitle"], a["subtitle-loc-key"]), ("Reminder · Question", "push.subtitle.reminder.question"))

    def test_msg_change_restarts(self):
        self.block()
        self.now[0] = 1200.0
        self.block(dict(base.PERM, msg="approve Bash: ls"))
        self.assertEqual(len(self.pusher.payloads), 2)  # 새 막힘 첫 알림
        self.assertEqual(self.at(300), [])  # 새 막힘 기준 100초
        r = self.at(500)
        self.assertEqual([(p["reminder"], p["msg"]) for p in r], [(1, "approve Bash: ls")])

    def test_terminal_gone_cancels(self):
        self.block()
        del self.hc.infos["t1"]
        self.assertEqual(self.at(300), [])

    def test_late_check_sends_once_with_last_number(self):
        self.block()
        r = self.at(5000)
        self.assertEqual([p["reminder"] for p in r], [2])
        self.assertEqual(len(self.at(9000)), 1)

    def test_first_push_failure_still_schedules(self):
        class FailPusher(base.FakePusher):
            def push(self, payload):
                self.payloads.append(payload)
                return 0
        self.app.pusher = FailPusher()
        self.pusher = self.app.pusher
        self.block()
        self.assertEqual([p["reminder"] for p in self.at(300)], [1])

    def test_initial_blocked_counts_from_startup(self):
        app, hc, pusher = make_app()
        now = [50.0]
        app.reminders.clock = lambda: now[0]
        hc.add("t1", base.PERM)
        app.poll(initial=True)
        self.assertEqual(len(pusher.payloads), 1)
        now[0] = 349.0
        app.poll()
        app.check_reminders()
        self.assertEqual(len(pusher.payloads), 1)
        now[0] = 350.0
        app.poll()
        app.check_reminders()
        self.assertEqual(pusher.payloads[-1]["reminder"], 1)

    def test_done_is_not_reminded(self):
        self.hc.set_status("t1", base.st("done"), turn=2)
        self.app.poll()
        self.assertEqual(self.at(2000), [])

    def test_config_empty_disables_and_custom_intervals(self):
        app, hc, pusher = make_app(reminder_after_sec=[])
        app.reminders.clock = lambda: 1e9
        hc.add("t1", base.PERM)
        app.poll(initial=True)
        app.reminders.clock = lambda: 1e9 + 99999
        app.check_reminders()
        self.assertEqual(len(pusher.payloads), 1)
        self.assertEqual(hr.reminder_intervals({"reminder_after_sec": [60, 10]}), [10, 60])
        with self.assertLogs(hr.log, "WARNING"):
            self.assertEqual(hr.reminder_intervals({"reminder_after_sec": [0]}), [300, 1800])
        with self.assertLogs(hr.log, "WARNING"):
            self.assertEqual(hr.reminder_intervals({"reminder_after_sec": "5"}), [300, 1800])
        self.assertEqual(hr.reminder_intervals({}), [300, 1800])


# ---------------------------------------------------------------- HTTP

class PushHttpTests(unittest.TestCase):
    def setUp(self):
        self.relay = base.FakeRelay()
        self.addCleanup(self.relay.close)
        self.app, self.hc, _ = make_app(push_relay_url=self.relay.url)
        self.wall = Wall()
        self.app.push_log = hr.PushLog(os.path.join(self.app.cfg["_dir"], "push_status.json"), wall=self.wall)
        self.app.pusher = hr.RelayPusher(self.app.cfg, self.app.devices, sleep=lambda s: None,
                                         push_log=self.app.push_log)
        self.addCleanup(self.app.pusher._drop_conn)
        self.now = [100.0]
        self.app.clock = lambda: self.now[0]
        self.srv = hr.make_server(self.app, "127.0.0.1", 0)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def req(self, *a, **k):
        return base.HttpTests.req(self, *a, **k)

    def test_status_without_device(self):
        self.app.devices.add(TOK_A, "sandbox", relay_sig=SIG)
        self.assertEqual(self.req("GET", "/api/push/status"),
                         (200, {"mode": "relay", "devices": 1, "last_success_at": None, "relay_limited_at": None,
                                "reminder_after_sec": [300, 1800], "device": None}))
        self.assertEqual(self.req("GET", "/api/push/status", token=None)[0], 401)

    def test_status_device_registered_and_not(self):
        self.app.devices.add(TOK_A, "sandbox", relay_sig=SIG)
        code, body = self.req("GET", "/api/push/status?device=" + did(TOK_A))
        self.assertEqual(code, 200)
        self.assertEqual(body["device"], {"id": did(TOK_A), "registered": True, "last_success_at": None,
                                          "last_attempt_at": None, "last_error": None})
        code, body = self.req("GET", "/api/push/status?device=" + did(TOK_B).upper())
        self.assertEqual(body["device"]["id"], did(TOK_B))
        self.assertFalse(body["device"]["registered"])
        self.assertEqual(self.req("GET", "/api/push/status?device=xyz"), (400, {"error": "bad_device"}))

    def test_push_test_success_and_too_soon(self):
        self.app.devices.add(TOK_A, "production", relay_sig=SIG)
        self.app.devices.add(TOK_B, "sandbox", relay_sig=SIG)
        code, body = self.req("POST", "/api/push/test", {"device": did(TOK_A)})
        self.assertEqual(code, 200)
        self.assertEqual((body["ok"], body["code"], body["reason"]), (True, 200, ""))
        self.assertIsInstance(body["elapsed_ms"], int)
        self.assertEqual(len(self.relay.requests), 1)
        sent = self.relay.requests[0][2]
        self.assertEqual(sent["device_token"], TOK_A)
        self.assertEqual(sent["env"], "production")
        p = sent["payload"]
        self.assertEqual(p["aps"], {"alert": {"title": "Sheltie", "subtitle": "Test notification", "body": "Notifications are working",
                                              "subtitle-loc-key": "push.subtitle.test", "loc-key": "push.body.test"},
                                    "sound": "default", "category": "HR_TEST", "thread-id": "sheltie-test"})
        self.assertEqual((p["state"], p["terminal"], p["server"]), ("test", None, hr.server_tag(TOKEN)))
        # 기록
        code, st = self.req("GET", "/api/push/status?device=" + did(TOK_A))
        self.assertEqual(st["device"]["last_success_at"], self.wall.t * 1000)
        self.assertEqual(st["last_success_at"], self.wall.t * 1000)
        # 10초 안에 다시 → 429, 다른 기기는 괜찮다
        self.now[0] += 9.9
        self.assertEqual(self.req("POST", "/api/push/test", {"device": did(TOK_A)}), (429, {"error": "too_soon"}))
        self.assertEqual(self.req("POST", "/api/push/test", {"device": did(TOK_B)})[0], 200)
        self.now[0] += 0.2
        self.assertEqual(self.req("POST", "/api/push/test", {"device": did(TOK_A)})[0], 200)
        self.assertEqual(len(self.relay.requests), 3)

    def test_push_test_failure_recorded(self):
        self.app.devices.add(TOK_A, "sandbox", relay_sig=SIG)
        self.relay.responses = [(403, {"error": "bad_sig"}, False)]
        code, body = self.req("POST", "/api/push/test", {"device": did(TOK_A)})
        self.assertEqual(code, 200)
        self.assertEqual((body["ok"], body["code"], body["reason"]), (False, 403, "relay:bad_sig"))
        st = self.req("GET", "/api/push/status?device=" + did(TOK_A))[1]
        self.assertEqual(st["device"]["last_error"]["reason"], "relay:bad_sig")

    def test_push_test_errors(self):
        self.assertEqual(self.req("POST", "/api/push/test", {"device": did(TOK_A)}, token=None)[0], 401)
        self.assertEqual(self.req("POST", "/api/push/test", {"device": did(TOK_A)}),
                         (404, {"error": "device_not_registered"}))
        self.assertEqual(self.req("POST", "/api/push/test", {}), (400, {"error": "bad_device"}))
        self.app.devices.add(TOK_A, "sandbox", relay_sig=SIG)
        self.app.cfg["push_relay_url"] = ""
        self.app.cfg["apns_key_path"] = os.path.join(self.app.cfg["_dir"], "missing.p8")
        self.assertEqual(self.req("POST", "/api/push/test", {"device": did(TOK_A)}),
                         (409, {"error": "push_unavailable"}))
        self.assertEqual(self.req("GET", "/api/push/status")[1]["mode"], "none")
        open(self.app.cfg["apns_key_path"], "w").close()
        self.assertEqual(self.req("GET", "/api/push/status")[1]["mode"], "apns")
        self.assertEqual(self.relay.requests, [])

    def test_health_push_field(self):
        def push():
            return self.req("GET", "/api/health")[1]["push"]
        self.assertEqual(push(), {"last_success_at": None, "relay_limited_at": None, "failing": False})
        # 기기 없이 실패 기록만 있으면 failing 아님
        self.app.push_log.record(TOK_A, 500, "x")
        self.assertFalse(push()["failing"])
        self.app.devices.add(TOK_A, "sandbox", relay_sig=SIG)
        self.assertTrue(push()["failing"])
        self.wall.t += 1
        self.app.push_log.record(TOK_A, 200, "")
        self.assertEqual(push(), {"last_success_at": self.wall.t * 1000, "relay_limited_at": None, "failing": False})
        self.wall.t += 1
        self.app.push_log.record(TOK_A, 0, "TimeoutError")
        self.app.push_log.relay_limited()
        p = push()
        self.assertTrue(p["failing"])
        self.assertEqual(p["relay_limited_at"], self.wall.t * 1000)
        self.wall.t += 24 * 3600 + 1  # 24시간 넘은 실패는 failing 아님
        self.assertFalse(push()["failing"])
        # 기존 필드는 그대로
        h = self.req("GET", "/api/health")[1]
        self.assertEqual((h["ok"], h["push_relay_url"], h["relay_resign"]), (True, self.relay.url, []))

    def test_health_push_failing_per_device(self):
        """?device=를 주면 그 기기 기록으로만 판정: 서명 없는 옛 기기의 실패로 다른 폰에 경고가 뜨지 않는다."""
        def push(q):
            return self.req("GET", "/api/health" + q)[1]["push"]
        self.app.devices.add(TOK_A, "sandbox", relay_sig=SIG)
        self.app.devices.add(TOK_B, "sandbox")
        self.app.push_log.record(TOK_A, 200, "")
        self.wall.t += 1
        self.app.push_log.record(TOK_B, 0, "no_relay_sig")
        self.assertTrue(push("")["failing"])
        self.assertFalse(push("?device=" + did(TOK_A))["failing"])
        self.assertTrue(push("?device=" + did(TOK_B))["failing"])
        self.assertTrue(push("?device=zz")["failing"])  # 형식이 틀리면 전체 기준

    def test_health_relay_limit_cleared_by_later_success(self):
        """한도(429)에 걸린 뒤 성공했으면 relay_limited_at을 내보내지 않는다(앱이 지난 경고를 1시간 띄우지 않게).
        did를 주면 그 기기의 마지막 성공 기준."""
        def push(q=""):
            return self.req("GET", "/api/health" + q)[1]["push"]
        self.app.devices.add(TOK_A, "sandbox", relay_sig=SIG)
        self.app.devices.add(TOK_B, "sandbox", relay_sig=SIG)
        self.app.push_log.relay_limited()
        t_lim = self.wall.t * 1000
        self.assertEqual(push()["relay_limited_at"], t_lim)  # 성공 기록 없음: 그대로
        self.wall.t += 60
        self.app.push_log.record(TOK_B, 200, "")  # B만 성공
        self.wall.t += 22 * 60
        self.app.push_log.relay_limited()
        t_lim2 = self.wall.t * 1000
        self.wall.t += 21 * 60
        self.app.push_log.record(TOK_A, 200, "")  # 한도 21분 뒤 A 성공
        self.assertIsNone(push()["relay_limited_at"])
        self.assertIsNone(push("?device=" + did(TOK_A))["relay_limited_at"])
        self.assertEqual(push("?device=" + did(TOK_B))["relay_limited_at"], t_lim2)  # B의 성공은 한도 전
        self.assertNotEqual(t_lim, t_lim2)
        self.wall.t += 1
        self.app.push_log.relay_limited()  # 성공 뒤 또 걸리면 다시 보인다
        self.assertEqual(push()["relay_limited_at"], self.wall.t * 1000)
        # /api/push/status는 기록 그대로(설정 화면 표시용, 경고 판정에 안 쓰인다)
        self.assertEqual(self.req("GET", "/api/push/status")[1]["relay_limited_at"], self.wall.t * 1000)


class BuildAppTests(unittest.TestCase):
    def test_build_app_shares_push_log_file(self):
        tmp = tempfile.mkdtemp()
        cfg = dict(hr.DEFAULTS, _dir=tmp, hc_path="/nonexistent/hc", push_relay_url="https://relay.example.com")
        app = hr.build_app(cfg)
        self.assertIs(app.push_log, app.pusher.push_log)
        self.assertEqual(app.push_log.path, os.path.join(tmp, "push_status.json"))
        cfg["push_relay_url"] = ""
        app = hr.build_app(cfg)
        self.assertIs(app.push_log, app.pusher.push_log)


if __name__ == "__main__":
    unittest.main()
