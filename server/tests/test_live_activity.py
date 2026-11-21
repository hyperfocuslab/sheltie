"""Live Activity: 막힘 수 변화 → start/update/end 결정, 최소 간격 합치기, 토큰 등록·삭제, 발송기(APNs·중계) 모양."""
import json
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import herdr_remote as hr  # noqa: E402
import test_herdr_remote as base  # noqa: E402

DEV = "aa" * 32
ACT = "cc" * 32
START = "dd" * 32
PERM = base.PERM


class Wall:
    def __init__(self, t=1_700_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class LaPusher(base.FakePusher):
    """알림 푸시 + send_live_activity 기록."""

    def __init__(self):
        super().__init__()
        self.la = []
        self.result = (200, "")

    def send_live_activity(self, device, token, payload, priority=10, ttl=3600):
        self.la.append({"device": device["token"], "token": token, "payload": payload, "priority": priority})
        return self.result


def make(wall=None):
    tmp = tempfile.mkdtemp()
    app, hc, _ = base.make_app(tmp)
    pusher = LaPusher()
    app.pusher = pusher
    wall = wall or Wall()
    app.live_activity_mgr.pusher = pusher
    app.live_activity_mgr.wall = wall
    timers = []
    app.live_activity_mgr.later = lambda sec, fn: timers.append((sec, fn))
    app.devices.add(DEV, "sandbox", relay_sig=base.SIG)
    return app, hc, pusher, wall, timers


class DecisionTests(unittest.TestCase):
    def state(self, n, tid="t1", msg="m"):
        blocked = [(tid, "name", msg, 1000)] * min(n, 1)
        s = hr.live_activity_state(blocked, 100)
        s["blockedCount"] = n
        return s

    def test_state_picks_oldest(self):
        s = hr.live_activity_state([("b", "B", "x", 2000), ("a", "A", None, 1000)], 50)
        self.assertEqual((s["blockedCount"], s["oldestTerminal"], s["oldestName"], s["oldestSummary"], s["oldestSinceMs"]),
                         (2, "a", "A", "", 1000))
        self.assertEqual(s["updatedAt"], 50)
        z = hr.live_activity_state([], 50)
        self.assertEqual((z["blockedCount"], z["oldestName"]), (0, ""))

    def test_actions(self):
        act = lambda entry, n, sig=None, at=None, now=100.0: hr.live_activity_action(  # noqa: E731
            entry, self.state(n), sig, at, now)
        self.assertEqual(act({"activity_token": ACT}, 2), "update")
        self.assertEqual(act({"activity_token": ACT}, 0), "end")
        self.assertIsNone(act({"activity_token": ACT}, 1, sig=hr.live_activity_signature(self.state(1))))
        self.assertEqual(act({"start_token": START}, 1), "start")
        self.assertIsNone(act({"start_token": START}, 0))
        self.assertIsNone(act({}, 3))
        # 활성 Activity가 있으면 start하지 않는다
        self.assertEqual(act({"activity_token": ACT, "start_token": START}, 1), "update")
        # start 직후에는 다시 start하지 않는다
        self.assertIsNone(act({"start_token": START, "started_at": 80.0}, 1))
        # 같은 막힘 동안은 토큰이 끝내 안 와도(사용자가 실시간 현황을 허용 안 함) 다시 start하지 않는다. 중복 Activity가 생긴다.
        self.assertIsNone(act({"start_token": START, "started_at": 30.0}, 1))

    def test_start_again_after_blocked_clears(self):
        store = hr.LiveActivityStore.__new__(hr.LiveActivityStore)
        store.lock = threading.Lock()
        store.data = {DEV: {"start_token": START, "started_at": 10.0}}
        store._save = lambda: None
        store.clear_started(DEV)
        self.assertEqual(store.data[DEV], {"start_token": START})

    def test_min_gap_waits(self):
        s = self.state(1)
        self.assertEqual(hr.live_activity_action({"activity_token": ACT}, s, None, 97.0, 100.0), "wait")
        self.assertEqual(hr.live_activity_action({"activity_token": ACT}, s, None, 94.0, 100.0), "update")

    def test_refresh_before_stale(self):
        # 같은 상태여도 오래되면 stale-date를 늦추려고 다시 update한다
        s = self.state(1)
        sig = hr.live_activity_signature(s)
        self.assertIsNone(hr.live_activity_action({"activity_token": ACT}, s, sig, 100.0, 100.0 + hr.LIVE_ACTIVITY_REFRESH - 1))
        self.assertEqual(hr.live_activity_action({"activity_token": ACT}, s, sig, 100.0, 100.0 + hr.LIVE_ACTIVITY_REFRESH),
                         "update")
        self.assertLess(hr.LIVE_ACTIVITY_REFRESH, hr.LIVE_ACTIVITY_STALE)

    def test_payloads(self):
        s = self.state(2)
        up = hr.live_activity_payload("update", s, 123.9)
        self.assertEqual(up, {"aps": {"timestamp": 123, "event": "update", "content-state": s,
                                      "stale-date": 123 + hr.LIVE_ACTIVITY_STALE}})
        end = hr.live_activity_payload("end", self.state(0), 123)
        self.assertEqual(end["aps"]["event"], "end")
        self.assertEqual(end["aps"]["dismissal-date"], 123)  # 바로 내린다
        self.assertNotIn("stale-date", end["aps"])
        st = hr.live_activity_payload("start", s, 123, pc_name="Mac", server="abc")
        self.assertEqual(st["aps"]["stale-date"], 123 + hr.LIVE_ACTIVITY_STALE)
        self.assertEqual(st["aps"]["attributes-type"], "SheltieActivityAttributes")
        self.assertEqual(st["aps"]["attributes"], {"pcName": "Mac", "serverTag": "abc"})
        self.assertIn("alert", st["aps"])
        # push로 시작된 Activity가 갱신·종료용 push token을 받으려면 필요하다(iOS 18). 없으면 서버가 end를 못 보낸다.
        self.assertEqual(st["aps"]["input-push-token"], 1)
        self.assertNotIn("input-push-token", up["aps"])
        # content-state 키는 앱 확장의 ContentState와 같다
        self.assertEqual(set(s), {"blockedCount", "oldestTerminal", "oldestName", "oldestSummary", "oldestSinceMs", "updatedAt"})
        json.dumps(st)


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.app, self.hc, self.pusher, self.wall, self.timers = make()
        self.hc.add("t1", base.st("working"), name="build")
        self.hc.add("t2", base.st("working"), name="deploy")
        self.app.poll(initial=True)

    def block(self, tid, status=PERM):
        self.hc.set_status(tid, status)
        self.app.poll()

    def test_update_then_end(self):
        self.app.live_activity.set(DEV, activity_token=ACT)
        self.block("t1")
        self.assertEqual(len(self.pusher.la), 1)
        p = self.pusher.la[0]
        self.assertEqual((p["device"], p["token"], p["priority"]), (DEV, ACT, 10))
        self.assertEqual(p["payload"]["aps"]["event"], "update")
        cs = p["payload"]["aps"]["content-state"]
        self.assertEqual((cs["blockedCount"], cs["oldestName"], cs["oldestTerminal"]), (1, "build", "t1"))
        # 같은 상태 반복은 보내지 않는다
        self.wall.t += 10
        self.app.poll()
        self.assertEqual(len(self.pusher.la), 1)
        # 막힘이 풀리면 end, 토큰은 지운다
        self.wall.t += 10
        self.block("t1", base.st("working"))
        self.assertEqual([x["payload"]["aps"]["event"] for x in self.pusher.la], ["update", "end"])
        self.assertEqual(self.pusher.la[1]["payload"]["aps"]["content-state"]["blockedCount"], 0)
        self.assertIsNone(self.app.live_activity.get(DEV))

    def test_start_when_no_activity_and_not_repeated(self):
        self.app.live_activity.set(DEV, start_token=START)
        self.block("t1")
        self.assertEqual([x["payload"]["aps"]["event"] for x in self.pusher.la], ["start"])
        self.assertEqual(self.pusher.la[0]["token"], START)
        self.assertEqual(self.pusher.la[0]["payload"]["aps"]["attributes"]["serverTag"],
                         hr.server_tag(base.TOKEN))
        # 앱이 Activity 토큰을 올리기 전에 막힘이 늘어도 다시 start하지 않는다
        self.wall.t += 10
        self.block("t2")
        self.assertEqual(len(self.pusher.la), 1)
        # 앱이 Activity 토큰을 올리면 그 토큰으로 현재 상태가 간다
        self.app.api_live_activity({"device": DEV, "activity_token": ACT})
        self.assertEqual([x["payload"]["aps"]["event"] for x in self.pusher.la], ["start", "update"])
        self.assertEqual(self.pusher.la[1]["token"], ACT)
        self.assertEqual(self.pusher.la[1]["payload"]["aps"]["content-state"]["blockedCount"], 2)

    def test_start_without_activity_token_goes_stale(self):
        """2026-11-21 실기기: push-to-start 뒤 앱이 Activity 토큰을 안 올려 end를 못 보냈고 Dynamic Island에 남았다.
        서버가 end를 못 보내도 start에 넣은 stale-date가 지나면 Activity가 낡은 것으로 표시된다."""
        self.app.live_activity.set(DEV, start_token=START)
        self.block("t1")
        aps = self.pusher.la[0]["payload"]["aps"]
        self.assertEqual(aps["event"], "start")
        self.assertEqual(aps["stale-date"], int(self.wall.t) + hr.LIVE_ACTIVITY_STALE)
        # 토큰이 안 왔으면 막힘이 풀려도 보낼 곳이 없다(end 대상 없음)
        self.wall.t += 30
        self.block("t1", base.st("working"))
        self.assertEqual(len(self.pusher.la), 1)
        # 막힘이 풀리면 started_at을 지워 다음 막힘에 새로 start할 수 있게 한다
        self.assertEqual(self.app.live_activity.get(DEV), {"start_token": START})

    def test_start_then_token_then_end(self):
        """start 뒤 앱이 Activity 토큰을 올리면 update가 그 토큰으로 가고, 막힘이 풀리면 end(즉시 내림)가 간다."""
        self.app.live_activity.set(DEV, start_token=START)
        self.block("t1")
        self.wall.t += 2
        self.app.api_live_activity({"device": DEV, "activity_token": ACT})  # 새 토큰은 최소 간격과 상관없이 바로
        self.assertEqual([x["payload"]["aps"]["event"] for x in self.pusher.la], ["start", "update"])
        self.assertEqual(self.pusher.la[1]["token"], ACT)
        self.assertEqual(self.pusher.la[1]["payload"]["aps"]["stale-date"], int(self.wall.t) + hr.LIVE_ACTIVITY_STALE)
        # 오래 막혀 있으면 stale-date를 늦추는 update
        self.wall.t += hr.LIVE_ACTIVITY_REFRESH
        self.app.poll()
        self.assertEqual([x["payload"]["aps"]["event"] for x in self.pusher.la], ["start", "update", "update"])
        self.wall.t += 10
        self.block("t1", base.st("working"))
        end = self.pusher.la[-1]
        self.assertEqual((end["token"], end["payload"]["aps"]["event"]), (ACT, "end"))
        self.assertEqual(end["payload"]["aps"]["dismissal-date"], int(self.wall.t))
        self.assertEqual(self.app.live_activity.get(DEV), {"start_token": START})

    def test_no_start_when_nothing_blocked_or_no_tokens(self):
        self.app.live_activity.set(DEV, start_token=START)
        self.app.poll()
        self.assertEqual(self.pusher.la, [])
        self.app.live_activity.remove_device(DEV)
        self.block("t1")
        self.assertEqual(self.pusher.la, [])

    def test_coalesces_within_min_gap_and_sends_final_state_later(self):
        self.app.live_activity.set(DEV, activity_token=ACT)
        self.block("t1")  # 첫 update
        self.wall.t += 1
        self.block("t2")  # 1초 뒤 변화: 미룬다
        self.wall.t += 1
        self.block("t1", base.st("working"))  # 또 변화: 역시 미룬다
        self.assertEqual(len(self.pusher.la), 1)
        self.assertEqual(len(self.timers), 1)  # 합치기 타이머는 하나
        sec, fn = self.timers[0]
        self.assertAlmostEqual(sec, 5 - 1 + 0.05, places=2)  # 첫 미룸(1초 뒤) 기준
        self.wall.t += sec
        fn()
        self.assertEqual(len(self.pusher.la), 2)
        cs = self.pusher.la[1]["payload"]["aps"]["content-state"]
        self.assertEqual((cs["blockedCount"], cs["oldestName"]), (1, "deploy"))  # 마지막 상태 하나만

    def test_flap_inside_gap_sends_nothing_new(self):
        self.app.live_activity.set(DEV, activity_token=ACT)
        self.block("t1")
        self.wall.t += 1
        self.block("t1", base.st("working"))  # 1→0: end를 미룬다
        self.block("t1")  # 0→1: 처음 보낸 것과 같아져 보낼 게 없다
        sec, fn = self.timers[0]
        self.wall.t += sec
        fn()
        self.assertEqual(len(self.pusher.la), 1)

    def test_gone_terminal_ends_activity(self):
        self.app.live_activity.set(DEV, activity_token=ACT)
        self.block("t1")
        self.wall.t += 10
        del self.hc.infos["t1"]
        self.app.poll()
        self.assertEqual(self.pusher.la[-1]["payload"]["aps"]["event"], "end")

    def test_stale_token_deleted_on_410(self):
        self.app.live_activity.set(DEV, activity_token=ACT, start_token=START)
        self.pusher.result = (410, "Unregistered")
        self.block("t1")
        self.assertEqual(self.app.live_activity.get(DEV), {"start_token": START})

    def test_failure_keeps_token_and_respects_gap(self):
        self.app.live_activity.set(DEV, activity_token=ACT)
        self.pusher.result = (500, "InternalServerError")
        self.block("t1")
        self.assertEqual(self.app.live_activity.get(DEV), {"activity_token": ACT})
        self.wall.t += 1
        self.block("t2")
        self.assertEqual(len(self.pusher.la), 1)

    def test_unregistered_device_entry_dropped(self):
        other = "ee" * 32
        self.app.live_activity.set(other, activity_token=ACT)
        self.block("t1")
        self.assertIsNone(self.app.live_activity.get(other))
        self.assertEqual(self.pusher.la, [])

    def test_pusher_without_support_is_skipped(self):
        self.app.live_activity_mgr.pusher = base.FakePusher()
        self.app.live_activity.set(DEV, activity_token=ACT)
        self.block("t1")  # 예외 없이 지나간다


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.app, self.hc, self.pusher, self.wall, self.timers = make()

    def test_register_and_delete(self):
        self.hc.add("t1", base.st("blocked", "m"), name="n")  # 막힘이 있어야 등록 직후 end가 안 간다
        self.app.poll(initial=True)
        self.assertEqual(self.app.api_live_activity({"device": DEV.upper(), "start_token": START.upper(),
                                                     "activity_token": ACT}), {"ok": True})
        self.assertEqual(self.app.live_activity.get(DEV), {"start_token": START, "activity_token": ACT})
        # 파일에 남고 다시 읽힌다
        again = hr.LiveActivityStore(self.app.live_activity.path)
        self.assertEqual(again.get(DEV), {"start_token": START, "activity_token": ACT})
        # activity_token만 지운다
        self.assertEqual(self.app.api_delete_live_activity({"device": DEV, "activity_token": ACT}),
                         {"ok": True, "removed": True})
        self.assertEqual(self.app.live_activity.get(DEV), {"start_token": START})
        # 다른 Activity 토큰이면 지우지 않는다
        self.assertEqual(self.app.api_delete_live_activity({"device": DEV, "activity_token": "ff" * 32}),
                         {"ok": True, "removed": False})
        # null로도 지운다
        self.app.api_live_activity({"device": DEV, "start_token": None})
        self.assertIsNone(self.app.live_activity.get(DEV))
        # 기기 전체 삭제
        self.app.api_live_activity({"device": DEV, "start_token": START})
        self.assertEqual(self.app.api_delete_live_activity({"device": DEV}), {"ok": True, "removed": True})
        self.assertIsNone(self.app.live_activity.get(DEV))

    def test_validation(self):
        def code(body, fn=None):
            with self.assertRaises(hr.ApiError) as cm:
                (fn or self.app.api_live_activity)(body)
            return cm.exception.code
        self.assertEqual(code({"device": "zz", "start_token": START}), 400)
        self.assertEqual(code({"device": DEV, "start_token": "zz"}), 400)
        self.assertEqual(code({"device": DEV}), 400)
        self.assertEqual(code({"device": "ee" * 32, "start_token": START}), 404)  # 등록 안 된 기기
        self.assertEqual(code({"device": 5}, self.app.api_delete_live_activity), 400)

    def test_fcm_device_rejected(self):
        tok = "x" * 30
        self.app.devices.add(tok, "fcm")
        with self.assertRaises(hr.ApiError):
            self.app.api_live_activity({"device": tok, "start_token": START})

    def test_device_delete_clears_live_activity(self):
        self.app.api_live_activity({"device": DEV, "start_token": START})
        self.app.api_delete_device({"token": DEV})
        self.assertIsNone(self.app.live_activity.get(DEV))

    def test_store_file_private(self):
        self.app.api_live_activity({"device": DEV, "start_token": START})
        self.assertEqual(os.stat(self.app.live_activity.path).st_mode & 0o777, 0o600)


class PusherShapeTests(unittest.TestCase):
    def test_relay_request_shape(self):
        relay = base.FakeRelay()
        tmp = tempfile.mkdtemp()
        try:
            cfg = dict(hr.DEFAULTS, push_relay_url=relay.url, apns_key_path=os.path.join(tmp, "x.p8"), _dir=tmp)
            devices = hr.DeviceStore(os.path.join(tmp, "devices.json"))
            devices.add(DEV, "production", relay_sig=base.SIG)
            p = hr.RelayPusher(cfg, devices, sleep=lambda s: None)
            payload = hr.live_activity_payload("update", hr.live_activity_state([], 1), 1)
            code, reason = p.send_live_activity(devices.list()[0], ACT, payload, 10)
            self.assertEqual((code, reason), (200, ""))
            path, _, body = relay.requests[0]
            self.assertEqual(path, "/v1/push")
            self.assertEqual(body, {"push_type": "liveactivity", "device_token": ACT, "auth_token": DEV,
                                    "env": "production", "relay_sig": base.SIG, "priority": 10,
                                    "ttl": hr.LIVE_ACTIVITY_TTL, "payload": payload})
            # 서명이 없으면 보내지 않는다
            devices.add(DEV, "production")
            self.assertEqual(p.send_live_activity(devices.list()[0], ACT, payload), (0, "no_relay_sig"))
            self.assertEqual(len(relay.requests), 1)
            p._drop_conn()
        finally:
            relay.close()

    def test_apns_direct_headers(self):
        calls = []

        def runner(argv, input=None, **kw):
            calls.append(input)

            class R:
                stdout = "\n200"
                returncode = 0
            return R()
        tmp = tempfile.mkdtemp()
        cfg = dict(hr.DEFAULTS, apns_key_path=os.path.join(tmp, "x.p8"), _dir=tmp)
        devices = hr.DeviceStore(os.path.join(tmp, "devices.json"))
        p = hr.ApnsPusher(cfg, devices, runner=runner)
        p.jwt = lambda: "JWT"
        code, _ = p.send_live_activity({"token": DEV, "env": "sandbox"}, ACT, {"aps": {"event": "update"}}, 5)
        self.assertEqual(code, 200)
        conf = calls[0]
        self.assertIn("/3/device/" + ACT, conf)
        self.assertIn("apns-topic: %s.push-type.liveactivity" % cfg["topic"], conf)
        self.assertIn("apns-push-type: liveactivity", conf)
        self.assertIn("apns-priority: 5", conf)
        # 알림 발송은 그대로 alert
        p.send_one({"token": DEV, "env": "sandbox"}, hr.build_payload("t1", "n", "done", {}))
        self.assertIn("apns-push-type: alert", calls[1])
        self.assertIn("apns-topic: %s\"" % cfg["topic"], calls[1])


if __name__ == "__main__":
    unittest.main()
