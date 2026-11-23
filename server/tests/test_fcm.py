"""Android(env "fcm") 기기: 등록 검증, 항상 중계로 보내기(APNs 키가 있어도), off 처리, 토큰 무효 시 삭제."""
import json
import os
import shutil
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
APNS_TOK = "aa" * 32
FCM_TOK = "dXyZ_AbC-12:APA91bHkQw" + "Rt" * 70  # 대소문자·콜론이 섞인 FCM 모양


def did(tok):
    return hr.device_id(tok)


def make_app(tmp, **cfg_over):
    cfg = dict(hr.DEFAULTS)
    cfg.update(token=TOKEN, bind="127.0.0.1", port=0, _dir=tmp, _path=os.path.join(tmp, "config.json"),
               apns_key_path=os.path.join(tmp, "AuthKey.p8"))
    cfg.update(cfg_over)
    devices = hr.DeviceStore(os.path.join(tmp, "devices.json"))
    return hr.App(cfg, base.FakeHc(), devices, base.FakePusher())


class FcmRegisterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.app = make_app(self.tmp)

    def test_fcm_token_kept_as_is_with_sig(self):
        self.assertEqual(self.app.api_add_device({"token": FCM_TOK, "env": "fcm", "relay_sig": SIG}), {"ok": True})
        devs = self.app.devices.list()
        self.assertEqual([(d["token"], d["env"], d["relay_sig"]) for d in devs], [(FCM_TOK, "fcm", SIG)])

    def test_fcm_token_shape(self):
        for bad in ("short", "has space" + "x" * 30, "x" * 1025, "a/b" + "x" * 30, 123, None):
            with self.assertRaises(hr.ApiError) as cm:
                self.app.api_add_device({"token": bad, "env": "fcm"})
            self.assertEqual(cm.exception.body, {"error": "bad_token"})
            self.assertEqual(self.app.devices.list(), [])
        with self.assertRaises(hr.ApiError):
            self.app.api_add_device({"token": FCM_TOK, "env": "fcm", "relay_sig": "v1.bad"})

    def test_apns_still_hex_only_and_lowercased(self):
        with self.assertRaises(hr.ApiError):
            self.app.api_add_device({"token": FCM_TOK, "env": "sandbox"})
        with self.assertRaises(hr.ApiError):
            self.app.api_add_device({"token": APNS_TOK, "env": "android"})
        self.app.api_add_device({"token": APNS_TOK.upper(), "env": "production"})
        self.assertEqual(self.app.devices.list()[0]["token"], APNS_TOK)

    def test_delete_fcm_and_apns(self):
        self.app.api_add_device({"token": FCM_TOK, "env": "fcm"})
        self.app.api_add_device({"token": APNS_TOK, "env": "sandbox"})
        self.assertEqual(self.app.api_delete_device({"token": FCM_TOK.lower()}), {"ok": True, "removed": False})
        self.assertEqual(self.app.api_delete_device({"token": FCM_TOK}), {"ok": True, "removed": True})
        self.assertEqual(self.app.api_delete_device({"token": APNS_TOK.upper()}), {"ok": True, "removed": True})
        self.assertEqual(self.app.devices.list(), [])


class FcmConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "config.json")
        self.key = os.path.join(self.tmp, "AuthKey.p8")

    def load(self, extra, key=False):
        if key:
            with open(self.key, "w") as f:
                f.write("k")
        with open(self.path, "w") as f:
            json.dump(dict({"token": "x" * 32, "hc_path": "/nonexistent/hc", "apns_key_path": self.key}, **extra), f)
        return hr.load_config(self.path)

    def test_fcm_relay_url(self):
        cfg = self.load({"push_relay_url": ""})
        self.assertEqual((cfg["push_relay_url"], cfg["fcm_relay_url"]), (hr.DEFAULT_RELAY_URL, hr.DEFAULT_RELAY_URL))
        # 키가 있어 APNs 직접이어도 fcm은 기본 중계
        cfg = self.load({}, key=True)
        # Windows는 APNs 직접 모드가 없어 키가 있어도 APNs도 기본 중계
        apns = hr.DEFAULT_RELAY_URL if hr.IS_WINDOWS else ""
        self.assertEqual((cfg["push_relay_url"], cfg["fcm_relay_url"]), (apns, hr.DEFAULT_RELAY_URL))
        cfg = self.load({"push_relay_url": "https://relay.example.com/"}, key=True)
        self.assertEqual((cfg["push_relay_url"], cfg["fcm_relay_url"]), ("https://relay.example.com",) * 2)
        for key in (False, True):
            self.assertEqual(self.load({"push_relay_url": "off"}, key=key)["fcm_relay_url"], "")


class FcmRoutingTests(unittest.TestCase):
    """APNs 키가 있는 직접 모드에서도 fcm 기기는 중계로 간다."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.relay = base.FakeRelay()
        self.addCleanup(self.relay.close)
        self.key = os.path.join(self.tmp, "AuthKey.p8")
        open(self.key, "w").close()
        self.devices = hr.DeviceStore(os.path.join(self.tmp, "devices.json"))
        self.pl = hr.PushLog(os.path.join(self.tmp, "push_status.json"))
        self.curl_calls = []

    def apns_pusher(self, fcm_relay_url):
        cfg = dict(hr.DEFAULTS, apns_key_path=self.key, fcm_relay_url=fcm_relay_url)

        def runner(argv, **kw):
            self.curl_calls.append(kw["input"])
            return subprocess.CompletedProcess(argv, 0, "\n200", "")

        p = hr.ApnsPusher(cfg, self.devices, runner=runner, push_log=self.pl)
        p.jwt = lambda: "JWT"
        self.addCleanup(lambda: p._fcm and p._fcm._drop_conn())
        return p

    def test_key_present_fcm_goes_to_relay(self):
        p = self.apns_pusher(self.relay.url)
        self.devices.add(APNS_TOK, "sandbox")
        self.devices.add(FCM_TOK, "fcm", relay_sig=SIG)
        payload = hr.build_payload("t1", "n", "blocked", {})
        self.assertEqual(p.push(payload), 2)
        self.assertEqual(len(self.curl_calls), 1)
        self.assertIn(APNS_TOK, self.curl_calls[0])
        self.assertNotIn(FCM_TOK, self.curl_calls[0])
        self.assertEqual(len(self.relay.requests), 1)
        path, ctype, body = self.relay.requests[0]
        self.assertEqual((path, ctype), ("/v1/push", "application/json"))
        self.assertEqual(body["device_token"], FCM_TOK)  # 대소문자 그대로
        self.assertEqual(body["env"], "fcm")
        self.assertEqual(body["relay_sig"], SIG)
        self.assertEqual(body["payload"], payload)
        self.assertRegex(body["collapse_id"], r"^[0-9a-f]{32}$")
        self.assertIsNotNone(self.pl.device(did(FCM_TOK))["last_success_at"])
        self.assertEqual(body["ttl"], hr.PUSH_TTL_BLOCKED)  # fcm 기기도 알림 종류별 유효기간
        self.assertIn('"apns-expiration: ', self.curl_calls[0])

    def test_off_records_failure_and_sends_nothing(self):
        p = self.apns_pusher("")
        self.devices.add(FCM_TOK, "fcm", relay_sig=SIG)
        self.assertEqual(p.push(hr.build_payload("t1", "n", "done", {})), 0)
        self.assertEqual(self.relay.requests, [])
        self.assertEqual(self.curl_calls, [])
        err = self.pl.device(did(FCM_TOK))["last_error"]
        self.assertEqual((err["code"], err["reason"]), (0, "fcm_relay_off"))
        self.assertEqual(len(self.devices.list()), 1)  # 기기는 지우지 않는다

    def test_no_key_and_relay_off_still_reports_fcm(self):
        os.unlink(self.key)
        p = self.apns_pusher("")
        self.devices.add(APNS_TOK, "sandbox")
        self.devices.add(FCM_TOK, "fcm", relay_sig=SIG)
        with self.assertLogs(hr.log, "WARNING"):
            self.assertEqual(p.push(hr.build_payload("t1", "n", "done", {})), 0)
        self.assertEqual(self.pl.device(did(FCM_TOK))["last_error"]["reason"], "fcm_relay_off")
        self.assertIsNone(self.pl.device(did(APNS_TOK)))  # APNs 기기는 지금처럼 기록 없이 건너뛴다

    def test_invalid_token_removes_device(self):
        p = self.apns_pusher(self.relay.url)
        for resp in ((410, {"ok": False, "reason": "Unregistered"}), (400, {"ok": False, "reason": "BadDeviceToken"})):
            self.devices.add(FCM_TOK, "fcm", relay_sig=SIG)
            self.relay.responses = [resp + (False,)]
            self.assertEqual(p.push(hr.build_payload("t1", "n", "done", {})), 0)
            self.assertEqual(self.devices.list(), [])

    def test_non_token_errors_keep_device(self):
        p = self.apns_pusher(self.relay.url)
        self.devices.add(FCM_TOK, "fcm", relay_sig=SIG)
        p._fcm_pusher().sleep = lambda s: None
        for resp in ((400, {"ok": False, "reason": "InvalidArgument"}), (403, {"ok": False, "reason": "SenderIdMismatch"}),
                     (503, {"error": "fcm_unconfigured"})):
            self.relay.responses = [resp + (False,)] * 2
            p.push(hr.build_payload("t1", "n", "done", {}))
            self.assertEqual(len(self.devices.list()), 1)
        self.assertEqual(self.pl.device(did(FCM_TOK))["last_error"]["reason"], "relay:fcm_unconfigured")

    def test_bad_sig_marks_resign(self):
        p = self.apns_pusher(self.relay.url)
        self.devices.add(FCM_TOK, "fcm", relay_sig=SIG)
        self.relay.responses = [(403, {"error": "bad_sig"}, False)]
        with self.assertLogs(hr.log, "WARNING"):
            p.push(hr.build_payload("t1", "n", "done", {}))
        self.assertEqual(self.devices.relay_resign(), [did(FCM_TOK)])

    def test_no_sig_skipped(self):
        p = self.apns_pusher(self.relay.url)
        self.devices.add(FCM_TOK, "fcm")
        with self.assertLogs(hr.log, "WARNING"):
            self.assertEqual(p.push(hr.build_payload("t1", "n", "done", {})), 0)
        self.assertEqual(self.relay.requests, [])
        self.assertEqual(self.pl.device(did(FCM_TOK))["last_error"]["reason"], "no_relay_sig")

    def test_relay_mode_same_url_uses_same_pusher(self):
        cfg = dict(hr.DEFAULTS, push_relay_url=self.relay.url, fcm_relay_url=self.relay.url)
        p = hr.RelayPusher(cfg, self.devices, push_log=self.pl)
        self.addCleanup(p._drop_conn)
        self.devices.add(APNS_TOK, "production", relay_sig=SIG)
        self.devices.add(FCM_TOK, "fcm", relay_sig=SIG)
        self.assertIs(p._fcm_pusher(), p)
        self.assertEqual(p.push(hr.build_payload("t1", "n", "done", {})), 2)
        self.assertEqual([r[2]["env"] for r in self.relay.requests], ["production", "fcm"])
        self.assertEqual(self.relay.connections, 1)


class FcmHttpTests(unittest.TestCase):
    """/api/devices, /api/push/test, /api/push/status, /api/health가 fcm 기기에서도 같게 돈다."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.relay = base.FakeRelay()
        self.addCleanup(self.relay.close)

    def start(self, push_relay_url, fcm_relay_url, key=False):
        self.app = make_app(self.tmp, push_relay_url=push_relay_url, fcm_relay_url=fcm_relay_url)
        if key:
            open(self.app.cfg["apns_key_path"], "w").close()
        self.app.push_log = hr.PushLog(os.path.join(self.tmp, "push_status.json"))
        cls = hr.RelayPusher if push_relay_url else hr.ApnsPusher
        self.app.pusher = cls(self.app.cfg, self.app.devices, push_log=self.app.push_log)
        self.addCleanup(lambda: self.app.pusher._fcm and self.app.pusher._fcm._drop_conn())
        self.srv = hr.make_server(self.app, "127.0.0.1", 0)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def req(self, *a, **k):
        return base.HttpTests.req(self, *a, **k)

    def test_register_test_push_status_health_with_apns_key(self):
        self.start("", self.relay.url, key=True)
        self.assertEqual(self.req("POST", "/api/devices", {"token": FCM_TOK, "env": "fcm", "relay_sig": SIG}),
                         (200, {"ok": True}))
        code, h = self.req("GET", "/api/health")
        self.assertEqual(code, 200)
        self.assertNotIn("push_relay_url", h)
        self.assertEqual(h["fcm_relay_url"], self.relay.url)
        self.assertEqual(h["relay_resign"], [])
        code, body = self.req("POST", "/api/push/test", {"device": did(FCM_TOK)})
        self.assertEqual((code, body["ok"], body["code"]), (200, True, 200))
        self.assertEqual(self.relay.requests[-1][2]["payload"]["category"], "HR_TEST")
        self.assertEqual(self.relay.requests[-1][2]["env"], "fcm")
        self.assertEqual(self.relay.requests[-1][2]["ttl"], 300)
        code, st = self.req("GET", "/api/push/status?device=" + did(FCM_TOK))
        self.assertTrue(st["device"]["registered"])
        self.assertIsNotNone(st["device"]["last_success_at"])
        self.assertFalse(self.req("GET", "/api/health?device=" + did(FCM_TOK))[1]["push"]["failing"])

    def test_relay_off_test_push_reports_failure(self):
        self.start("", "")  # 키 없음 + off: APNs 모드 none
        self.req("POST", "/api/devices", {"token": FCM_TOK, "env": "fcm", "relay_sig": SIG})
        code, body = self.req("POST", "/api/push/test", {"device": did(FCM_TOK)})
        self.assertEqual((code, body["ok"], body["code"], body["reason"]), (200, False, 0, "fcm_relay_off"))
        self.assertEqual(self.relay.requests, [])
        h = self.req("GET", "/api/health?device=" + did(FCM_TOK))[1]
        self.assertTrue(h["push"]["failing"])
        self.assertNotIn("fcm_relay_url", h)
        # APNs 기기는 지금처럼 409
        self.req("POST", "/api/devices", {"token": APNS_TOK, "env": "sandbox"})
        self.assertEqual(self.req("POST", "/api/push/test", {"device": did(APNS_TOK)}),
                         (409, {"error": "push_unavailable"}))

    def test_bad_sig_then_resign_in_health(self):
        self.start(self.relay.url, self.relay.url)
        self.req("POST", "/api/devices", {"token": FCM_TOK, "env": "fcm", "relay_sig": SIG})
        self.relay.responses = [(403, {"error": "bad_sig"}, False)]
        with self.assertLogs(hr.log, "WARNING"):
            self.req("POST", "/api/push/test", {"device": did(FCM_TOK)})
        h = self.req("GET", "/api/health")[1]
        self.assertEqual(h["relay_resign"], [did(FCM_TOK)])
        self.assertEqual(h["push_relay_url"], self.relay.url)


if __name__ == "__main__":
    unittest.main()
