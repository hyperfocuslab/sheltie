import base64
import hashlib
import http.server
import io
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import herdr_remote as hr  # noqa: E402

hr.log.addHandler(logging.NullHandler())
hr.log.propagate = False

TOKEN = "test-token-0123456789abcdefghij"
FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fixture(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return f.read()

PERM = {"app": "claude-code", "kind": "permission", "msg": "approve Write: /tmp/x.py", "state": "blocked"}
TRUST = {"app": "claude-code", "kind": "permission", "msg": "trust this folder", "state": "blocked"}
DIALOG = {"app": "claude-code", "kind": "permission", "msg": "dialog open", "state": "blocked"}


def st(state, msg=None, kind=None):
    return {"app": "claude-code", "kind": kind, "msg": msg, "state": state}


class FakeHc:
    """hc 가짜. 실제 hc를 부르지 않는다."""

    def __init__(self):
        self.infos = {}
        self.keys_calls = []
        self.send_calls = []
        self.send_error = None
        self.screens = {}
        self.read_delay = 0
        self.read_calls = 0
        self.run_calls = []

    def add(self, tid, status=None, agent="claude", name=None, turn=1, argv=None):
        info = {"address": tid, "name": name or tid, "cwd": "/w", "state": "live", "status": status,
                "status_ms": 1, "title": "t", "turn": turn}
        if agent:
            info["agent"] = agent
        if argv:
            info["foreground"] = {"argv": argv, "name": argv[0], "pid": 1}
        self.infos[tid] = info
        return info

    def set_status(self, tid, status, turn=None):
        self.infos[tid]["status"] = status
        if turn is not None:
            self.infos[tid]["turn"] = turn

    def ls(self):
        return [{"address": i["address"], "name": i["name"], "cwd": i["cwd"], "agent": i.get("agent"),
                 "state": "live", "turn": i["turn"]} for i in self.infos.values()]

    def get(self, tid):
        if tid not in self.infos:
            raise hr.HcError("no terminal")
        return json.loads(json.dumps(self.infos[tid]))

    def read(self, tid, lines=0):
        self.read_calls += 1
        if self.read_delay:
            import time
            time.sleep(self.read_delay)
        if tid in self.screens:
            return {"complete": True, "how": "screen", "text": self.screens[tid]}
        return {"complete": True, "how": "screen", "text": "screen of %s (%d)" % (tid, lines)}

    def run_claude(self, cwd, prompt=None, exe=None):
        self.run_calls.append((cwd, prompt))
        return {"address": "t99", "agent": "claude", "cwd": cwd, "state": "live", "turn": 0}

    def keys(self, tid, keys):
        self.keys_calls.append((tid, list(keys)))

    def send(self, tid, text, force=False):
        if self.send_error:
            raise self.send_error
        self.send_calls.append((tid, text) if not force else (tid, text, "force"))

    def events_argv(self):
        return ["/nonexistent/hc", "events"]


class FakePusher:
    def __init__(self):
        self.payloads = []

    def push(self, payload):
        self.payloads.append(payload)
        return 1

    def available(self):
        return True


def make_app(tmp=None, home=None, runner=None):
    tmp = tmp or tempfile.mkdtemp()
    cfg = dict(hr.DEFAULTS)
    cfg.update(token=TOKEN, bind="127.0.0.1", port=0, _dir=tmp, _path=os.path.join(tmp, "config.json"))
    if home:
        cfg["home"] = home
    hc = FakeHc()
    pusher = FakePusher()
    devices = hr.DeviceStore(os.path.join(tmp, "devices.json"))
    return hr.App(cfg, hc, devices, pusher, runner=runner), hc, pusher


def ev(tid, typ, data):
    return json.dumps({"jsonrpc": "2.0", "method": "event",
                       "params": {"data": data, "seq": 1, "terminal": tid, "type": typ}})


def status_ev(tid, status):
    return ev(tid, "status", {"records": {}, "reported": True, "status": status})


# ---------------------------------------------------------------- 전이 판정

class TransitionTests(unittest.TestCase):
    def setUp(self):
        self.app, self.hc, self.pusher = make_app()
        self.hc.add("t1", st("working"))
        self.app.poll(initial=True)
        self.assertEqual(self.pusher.payloads, [])

    def test_blocked_event_notifies_once_and_repeat_is_ignored(self):
        self.hc.set_status("t1", PERM)
        self.app.handle_event_line(status_ev("t1", PERM))
        self.assertEqual(len(self.pusher.payloads), 1)
        p = self.pusher.payloads[0]
        self.assertEqual(p["aps"]["category"], "HR_PERMISSION")
        self.assertEqual(p["terminal"], "t1")
        # 같은 상태 반복 이벤트·폴링
        self.app.handle_event_line(status_ev("t1", PERM))
        self.app.handle_event_line(ev("t1", "turn", {"state": "blocked", "turn": 1}))
        self.app.poll()
        self.assertEqual(len(self.pusher.payloads), 1)

    def test_event_says_working_but_get_is_blocked(self):
        self.hc.set_status("t1", PERM)
        self.app.handle_event_line(status_ev("t1", st("working")))
        self.assertEqual(len(self.pusher.payloads), 1)
        self.assertEqual(self.pusher.payloads[0]["state"], "blocked")

    def test_event_done_but_get_already_idle(self):
        self.hc.set_status("t1", st("idle"))
        self.app.handle_event_line(status_ev("t1", st("done")))
        self.assertEqual([p["aps"]["category"] for p in self.pusher.payloads], ["HR_DONE"])
        self.assertEqual(self.pusher.payloads[0]["aps"]["alert"]["body"], "Task finished")
        # 같은 turn의 done이 다시 와도(이벤트 중복, turn 이벤트) 추가 알림 없음
        self.app.handle_event_line(status_ev("t1", st("done")))
        self.app.handle_event_line(ev("t1", "turn", {"state": "done", "turn": 1}))
        self.app.poll()
        self.assertEqual(len(self.pusher.payloads), 1)

    def test_done_again_in_next_turn(self):
        self.hc.set_status("t1", st("done"), turn=1)
        self.app.handle_event_line(status_ev("t1", st("done")))
        self.hc.set_status("t1", st("working"), turn=2)
        self.app.handle_event_line(status_ev("t1", st("working")))
        self.hc.set_status("t1", st("done"), turn=2)
        self.app.handle_event_line(status_ev("t1", st("done")))
        self.assertEqual(len(self.pusher.payloads), 2)

    def test_blocked_working_blocked_same_msg_notifies_twice(self):
        for s in (PERM, st("working"), PERM):
            self.hc.set_status("t1", s)
            self.app.handle_event_line(status_ev("t1", s))
        self.assertEqual(len(self.pusher.payloads), 2)

    def test_poll_safety_net_catches_missed_event(self):
        self.hc.set_status("t1", st("blocked", "which option?", "question"))
        self.app.poll()
        self.assertEqual(len(self.pusher.payloads), 1)
        self.assertEqual(self.pusher.payloads[0]["aps"]["category"], "HR_BLOCKED")

    def test_removed_forgets(self):
        self.hc.set_status("t1", PERM)
        self.app.poll()
        self.app.handle_event_line(ev("t1", "removed", {}))
        self.assertNotIn("t1", self.app.tracker.known())

    def test_bad_lines_ignored(self):
        self.app.handle_event_line('{"channel":null,"seq":68,"terminals":[]}')
        self.app.handle_event_line('{"method":"heartbeat"}')
        self.app.handle_event_line("not json")
        self.app.handle_event_line(status_ev("../x", PERM))
        self.assertEqual(self.pusher.payloads, [])


class StartupBaselineTests(unittest.TestCase):
    def test_initial_blocked_notifies_but_initial_done_does_not(self):
        app, hc, pusher = make_app()
        hc.add("t1", PERM)
        hc.add("t2", st("done"))
        hc.add("t3", None, agent=None)
        app.poll(initial=True)
        self.assertEqual([p["terminal"] for p in pusher.payloads], ["t1"])
        app.poll()
        self.assertEqual(len(pusher.payloads), 1)


# ---------------------------------------------------------------- 답·검증 단위

class DecideAnswerTests(unittest.TestCase):
    def info(self, status):
        return {"agent": "claude", "status": status}

    def test_cases(self):
        self.assertEqual(hr.decide_answer(self.info(PERM), "approve"), ["1"])
        self.assertEqual(hr.decide_answer(self.info(TRUST), "approve"), ["down", "enter"])
        self.assertEqual(hr.decide_answer(self.info(PERM), "reject"), ["esc"])
        self.assertEqual(hr.decide_answer(self.info(DIALOG), "reject"), ["esc"])
        for status, exp in ((st("working"), "not_blocked"), (None, "not_blocked"),
                            (DIALOG, "needs_manual"), (st("blocked", "Pick one", "question"), "needs_manual")):
            with self.assertRaises(hr.ApiError) as cm:
                hr.decide_answer(self.info(status), "approve")
            self.assertEqual((cm.exception.code, cm.exception.body["error"]), (409, exp))
        with self.assertRaises(hr.ApiError) as cm:
            hr.decide_answer(self.info(PERM), "approve", expect_msg="approve Bash: rm")
        self.assertEqual(cm.exception.body["error"], "changed")

    def test_is_claude(self):
        self.assertTrue(hr.is_claude({"agent": "claude"}))
        self.assertTrue(hr.is_claude({"status": {"app": "claude-code"}}))
        self.assertTrue(hr.is_claude({"foreground": {"argv": ["/usr/local/bin/claude", "-c"]}}))
        self.assertFalse(hr.is_claude({"foreground": {"argv": ["zsh"]}, "status": None}))
        self.assertFalse(hr.is_claude({"foreground": {"argv": ["claude-evil"]}}))

    def test_text(self):
        self.assertEqual(hr.validate_text("hi\n\tthere 한글"), "hi\n\tthere 한글")
        for bad in ("", "a\x00b", "a\rb", "\x1b[A", "x" * 4001, 5, "a\x7fb", "a\x9bb"):
            with self.assertRaises(hr.ApiError):
                hr.validate_text(bad)


class HcWrapperTests(unittest.TestCase):
    def test_argv_shell_false_timeout_and_dashdash(self):
        calls = []

        def runner(argv, **kw):
            calls.append((argv, kw))
            return subprocess.CompletedProcess(argv, 0, "{}", "")

        h = hr.Hc("/x/hc", runner=runner)
        h.send("t1", "--force rm -rf /")
        h.keys("t1", ["1"])
        argv, kw = calls[0]
        self.assertEqual(argv, ["/x/hc", "send", "t1", "--", "--force rm -rf /"])
        self.assertIs(kw["shell"], False)
        self.assertEqual(kw["timeout"], 10)
        self.assertEqual(calls[1][0], ["/x/hc", "keys", "t1", "1"])

    def test_nonzero_raises(self):
        h = hr.Hc("/x/hc", runner=lambda argv, **kw: subprocess.CompletedProcess(argv, 3, "", "blocked"))
        with self.assertRaises(hr.HcError):
            h.send("t1", "x")


# ---------------------------------------------------------------- bind·설정

class BindAndConfigTests(unittest.TestCase):
    def test_bind_rules(self):
        for ok in ("127.0.0.1", "100.96.65.28", "100.64.0.1", "100.127.255.254"):
            hr.check_bind(ok)
        for bad in ("0.0.0.0", "::", "192.168.0.2", "100.128.0.1", "127.0.0.2", "localhost", ""):
            with self.assertRaises(ValueError):
                hr.check_bind(bad)
        app, _, _ = make_app()
        with self.assertRaises(ValueError):
            hr.make_server(app, "0.0.0.0", 0)

    def test_bind_retry(self):
        app, _, _ = make_app()
        sleeps = []
        with self.assertRaises(OSError):
            # 이 기기에 없는 Tailscale 주소 → bind 실패 → 재시도
            hr.make_server_retry(app, "100.100.100.123", 0, sleep=sleeps.append, attempts=3)
        self.assertEqual(sleeps, [10, 10])

    def test_init_creates_private_files_and_does_not_overwrite(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "conf", "config.json")
        out = io.StringIO()
        self.assertTrue(hr.init_config(path, out=out))
        cfg = hr.load_config(path)
        self.assertGreaterEqual(len(cfg["token"]), 40)
        self.assertIn(cfg["token"], out.getvalue())
        if os.name != "nt":  # Windows는 POSIX 권한 비트가 없다(사용자 프로필 ACL을 따른다)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode), 0o700)
        self.assertEqual(cfg["apns_key_id"], "5BN2SS5X65")
        self.assertEqual(cfg["team_id"], "2CU9248L85")
        out2 = io.StringIO()
        self.assertFalse(hr.init_config(path, out=out2))
        self.assertEqual(hr.load_config(path)["token"], cfg["token"])
        self.assertNotIn(cfg["token"], out2.getvalue())

    def test_devices_store(self):
        tmp = tempfile.mkdtemp()
        ds = hr.DeviceStore(os.path.join(tmp, "d", "devices.json"))
        ds.add("ab" * 32, "sandbox")
        ds.add("ab" * 32, "production")
        self.assertEqual([(d["token"], d["env"]) for d in ds.list()], [("ab" * 32, "production")])
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(os.stat(ds.path).st_mode), 0o600)
        self.assertTrue(ds.remove("ab" * 32))
        self.assertEqual(ds.list(), [])


# ---------------------------------------------------------------- APNs

def _openssl_bins():
    bins = []
    for b in (shutil.which("openssl"), "/usr/bin/openssl"):
        if b and os.path.exists(b) and b not in bins:
            bins.append(b)
    return bins


class PushTtlTests(unittest.TestCase):
    def test_ttl_by_category(self):
        ttl = lambda *a, **k: hr.push_ttl(hr.build_payload("t1", "n", *a, **k))  # noqa: E731
        self.assertEqual(ttl("blocked", {"kind": "question"}), 3600)  # HR_BLOCKED
        self.assertEqual(ttl("blocked", PERM), 3600)  # HR_PERMISSION
        always = {"id": "p1", "kind": "approval", "body": "rm x", "always_index": 1}
        p = hr.build_payload("t1", "n", "blocked", PERM, prompt=always)
        self.assertEqual(p["aps"]["category"], "HR_PERMISSION_ALWAYS")
        self.assertEqual(hr.push_ttl(p), 3600)
        self.assertEqual(ttl("done", {}), 6 * 3600)  # HR_DONE
        self.assertEqual(ttl("exited", None, exit_code=1), 6 * 3600)  # HR_EXITED
        self.assertEqual(hr.push_ttl({"aps": {"category": "HR_TEST"}}), 300)
        self.assertEqual(hr.push_ttl({"aps": {"category": "HR_NEW"}}), 3600)  # 모르는 종류
        self.assertEqual(hr.push_ttl({"aps": {}}), 3600)
        self.assertEqual(hr.push_ttl({"category": "HR_DONE"}), 6 * 3600)

    def test_all_categories_have_ttl(self):
        for cat in hr.SUBTITLES:
            self.assertIn(cat, hr.PUSH_TTLS)


class ApnsTests(unittest.TestCase):
    def test_der_to_raw(self):
        r = b"\x80" + b"\x11" * 31          # 앞 비트 1 → DER에서 0x00 패딩
        s = b"\x00\x00" + b"\x22" * 30      # 앞이 0 → DER에서 짧아짐
        der = hr.raw_to_der(r + s)
        self.assertEqual(der[3], 33)
        self.assertEqual(hr.der_to_raw(der), r + s)
        with self.assertRaises(ValueError):
            hr.der_to_raw(b"\x31\x00")
        with self.assertRaises(ValueError):
            hr.der_to_raw(der + b"\x00")

    def test_payload_loc_keys(self):
        def alert(*a, **k):
            return hr.build_payload("t1", "n", *a, **k)["aps"]["alert"]
        a = alert("blocked", {"kind": "question"})
        self.assertEqual((a["subtitle-loc-key"], a["loc-key"]), ("push.subtitle.blocked", "push.body.blocked"))
        self.assertEqual(alert("blocked", PERM)["subtitle-loc-key"], "push.subtitle.permission")
        a = alert("done", {})
        self.assertEqual((a["subtitle-loc-key"], a["loc-key"], a["body"]),
                         ("push.subtitle.done", "push.body.done", "Task finished"))
        a = alert("done", {"msg": "빌드 완료"})
        self.assertEqual((a["body"], a["subtitle-loc-key"]), ("빌드 완료", "push.subtitle.done"))
        self.assertNotIn("loc-key", a)
        a = alert("exited", None)
        self.assertEqual((a["subtitle-loc-key"], a["loc-key"]), ("push.subtitle.exited", "push.body.exited"))
        self.assertNotIn("loc-args", a)
        a = alert("exited", None, exit_code=2)
        self.assertEqual((a["loc-key"], a["loc-args"], a["body"]),
                         ("push.body.exited.code", ["2"], "Terminal exited (exit 2)"))

    def test_interruption_level(self):
        def level(*a, **k):
            return hr.build_payload("t1", "n", *a, **k)["aps"].get("interruption-level")
        self.assertEqual(level("blocked", PERM), "time-sensitive")
        self.assertEqual(level("blocked", {"kind": "question"}), "time-sensitive")
        self.assertEqual(level("blocked", {}, prompt={"id": "p", "kind": "approval", "body": "x", "always_index": 1}),
                         "time-sensitive")
        self.assertIsNone(level("done", {}))
        self.assertIsNone(level("exited", None, exit_code=1))

    def test_plain_fallback_has_no_hangul(self):
        import re
        for args, kw in [(("done", {}), {}), (("blocked", PERM), {}), (("blocked", {}), {}),
                         (("exited", None), {"exit_code": 1})]:
            a = hr.build_payload("t1", "n", *args, **kw)["aps"]["alert"]
            self.assertIsNone(re.search("[가-힣]", a["subtitle"] + a["body"]), a)
        t = hr.test_alert()
        self.assertIsNone(re.search("[가-힣]", t["subtitle"] + t["body"]))
        self.assertEqual((t["subtitle-loc-key"], t["loc-key"]), ("push.subtitle.test", "push.body.test"))

    def test_category_and_payload(self):
        self.assertEqual(hr.push_category("blocked", PERM), "HR_PERMISSION")
        self.assertEqual(hr.push_category("blocked", TRUST), "HR_PERMISSION")
        self.assertEqual(hr.push_category("blocked", DIALOG), "HR_BLOCKED")
        self.assertEqual(hr.push_category("blocked", {"kind": "question", "msg": "approve x"}), "HR_BLOCKED")
        self.assertEqual(hr.push_category("done", {}), "HR_DONE")
        p = hr.build_payload("t12", "hrtest", "blocked", dict(PERM, msg="approve " + "x" * 500))
        self.assertEqual(p["aps"]["alert"]["title"], "hrtest")
        self.assertEqual(p["aps"]["alert"]["subtitle"], "Needs permission")
        self.assertEqual(p["aps"]["alert"]["subtitle-loc-key"], "push.subtitle.permission")
        self.assertNotIn("loc-key", p["aps"]["alert"])  # 에이전트가 쓴 msg는 번역하지 않는다
        self.assertEqual(len(p["aps"]["alert"]["body"]), 200)
        self.assertEqual(p["aps"]["thread-id"], "t12")
        self.assertEqual(p["aps"]["sound"], "default")
        # 이름이 없으면 id 대신 "이름 없는 터미널"(앱 언어로)
        a = hr.build_payload("t12", None, "done", None)["aps"]["alert"]
        self.assertEqual((a["title"], a["title-loc-key"]), ("Untitled terminal", "push.title.untitled"))
        self.assertNotIn("title-loc-key", hr.build_payload("t12", "hrtest", "done", None)["aps"]["alert"])

    def _pusher(self, tmp, responses, hosts=None):
        cfg = dict(hr.DEFAULTS, apns_key_path=os.path.join(tmp, "AuthKey.p8"))
        if hosts:
            cfg["apns_hosts"] = hosts
        open(cfg["apns_key_path"], "w").close()
        devices = hr.DeviceStore(os.path.join(tmp, "devices.json"))
        calls = []

        def runner(argv, **kw):
            calls.append((argv, kw))
            return subprocess.CompletedProcess(argv, 0, responses.pop(0), "")

        p = hr.ApnsPusher(cfg, devices, runner=runner)
        p.jwt = lambda: "JWT.SECRET.SIG"
        return p, devices, calls

    def test_send_hides_secrets_from_argv_and_picks_host(self):
        tmp = tempfile.mkdtemp()
        p, devices, calls = self._pusher(tmp, ["\n200", "\n200"])
        devices.add("aa" * 32, "sandbox")
        devices.add("bb" * 32, "production")
        self.assertEqual(p.push(hr.build_payload("t1", "n", "done", {})), 2)
        for argv, kw in calls:
            joined = " ".join(argv)
            self.assertNotIn("aa" * 32, joined)
            self.assertNotIn("bb" * 32, joined)
            self.assertNotIn("JWT", joined)
            self.assertIs(kw["shell"], False)
            self.assertIn("--http2", argv)
        self.assertIn('url = "https://api.sandbox.push.apple.com/3/device/' + "aa" * 32, calls[0][1]["input"])
        self.assertIn('url = "https://api.push.apple.com/3/device/' + "bb" * 32, calls[1][1]["input"])
        self.assertIn('"authorization: bearer JWT.SECRET.SIG"', calls[0][1]["input"])
        self.assertIn('"apns-topic: com.hyperfocuslab.sheltie"', calls[0][1]["input"])

    def test_apns_expiration_header(self):
        tmp = tempfile.mkdtemp()
        p, devices, calls = self._pusher(tmp, ["\n200"] * 4)
        p.clock = lambda: 1_700_000_000.7
        devices.add("aa" * 32, "sandbox")
        p.push(hr.build_payload("t1", "n", "blocked", PERM))
        p.push(hr.build_payload("t1", "n", "done", {}))
        p.push(hr.build_payload("t1", "n", "done", {}), ttl=hr.PUSH_TTL_TEST)
        p.push_device(devices.list()[0], {"aps": {"category": "HR_TEST"}})
        exp = [re.search(r'"apns-expiration: (\d+)"', kw["input"]).group(1) for _, kw in calls]
        self.assertEqual(exp, ["1700003600", "1700021600", "1700000300", "1700000300"])

    def test_configurable_hosts(self):
        tmp = tempfile.mkdtemp()
        p, devices, calls = self._pusher(tmp, ["\n200"], hosts={"sandbox": "https://apns.example.test:2197/",
                                                                  "production": "https://prod.example.test"})
        devices.add("aa" * 32, "sandbox")
        p.push(hr.build_payload("t1", "n", "done", {}))
        self.assertIn('url = "https://apns.example.test:2197/3/device/' + "aa" * 32, calls[0][1]["input"])
        # load_config는 일부만 준 apns_hosts에 기본값을 채운다
        path = os.path.join(tmp, "config.json")
        with open(path, "w") as f:
            json.dump({"token": "x", "apns_hosts": {"sandbox": "https://s.test"}}, f)
        cfg = hr.load_config(path)
        self.assertEqual(cfg["apns_hosts"], {"sandbox": "https://s.test", "production": "https://api.push.apple.com"})

    def test_bad_device_removed(self):
        tmp = tempfile.mkdtemp()
        p, devices, _ = self._pusher(tmp, ['{"reason":"BadDeviceToken"}\n400', '{"reason":"Unregistered"}\n410',
                                           '{"reason":"TooManyRequests"}\n429'])
        for t in ("aa", "bb", "cc"):
            devices.add(t * 32, "sandbox")
        self.assertEqual(p.push(hr.build_payload("t1", "n", "done", {})), 0)
        self.assertEqual([d["token"] for d in devices.list()], ["cc" * 32])

    def test_no_key_file_no_push(self):
        tmp = tempfile.mkdtemp()
        cfg = dict(hr.DEFAULTS, apns_key_path=os.path.join(tmp, "missing.p8"))
        devices = hr.DeviceStore(os.path.join(tmp, "devices.json"))
        devices.add("aa" * 32, "sandbox")
        p = hr.ApnsPusher(cfg, devices, runner=lambda *a, **k: self.fail("must not run"))
        self.assertEqual(p.push({"terminal": "t"}), 0)

    def test_jwt_cache(self):
        tmp = tempfile.mkdtemp()
        cfg = dict(hr.DEFAULTS, apns_key_path=os.path.join(tmp, "k.p8"))
        now = [1000.0]
        made = []
        p = hr.ApnsPusher(cfg, hr.DeviceStore(os.path.join(tmp, "d.json")), clock=lambda: now[0])
        orig = hr.make_jwt
        hr.make_jwt = lambda *a, **k: made.append(k["now"]) or "J%d" % len(made)
        try:
            self.assertEqual(p.jwt(), "J1")
            now[0] += 49 * 60
            self.assertEqual(p.jwt(), "J1")
            now[0] += 2 * 60
            self.assertEqual(p.jwt(), "J2")
        finally:
            hr.make_jwt = orig

    @unittest.skipUnless(_openssl_bins(), "openssl 없음")
    def test_real_openssl_jwt_verifies(self):
        base = os.path.expanduser("~/Documents/github/_scratch/herdr-remote")
        os.makedirs(base, exist_ok=True)
        tmp = tempfile.mkdtemp(dir=base)
        try:
            for ossl in _openssl_bins():
                with self.subTest(openssl=ossl):
                    key = os.path.join(tmp, "AuthKey.p8")
                    pub = os.path.join(tmp, "pub.pem")
                    gen = subprocess.run([ossl, "ecparam", "-name", "prime256v1", "-genkey", "-noout"],
                                         capture_output=True, check=True)
                    p8 = subprocess.run([ossl, "pkcs8", "-topk8", "-nocrypt"], input=gen.stdout,
                                        capture_output=True, check=True)
                    with open(key, "wb") as f:
                        f.write(p8.stdout)
                    self.assertIn(b"BEGIN PRIVATE KEY", p8.stdout)
                    subprocess.run([ossl, "ec", "-in", key, "-pubout", "-out", pub], capture_output=True, check=True)
                    jwt = hr.make_jwt(key, "5BN2SS5X65", "2CU9248L85", now=1700000000, openssl=ossl)
                    h, c, s = jwt.split(".")
                    pad = lambda x: x + "=" * (-len(x) % 4)  # noqa: E731
                    self.assertEqual(json.loads(base64.urlsafe_b64decode(pad(h))), {"alg": "ES256", "kid": "5BN2SS5X65"})
                    self.assertEqual(json.loads(base64.urlsafe_b64decode(pad(c))), {"iss": "2CU9248L85", "iat": 1700000000})
                    raw = base64.urlsafe_b64decode(pad(s))
                    self.assertEqual(len(raw), 64)
                    sig = os.path.join(tmp, "sig.der")
                    msg = os.path.join(tmp, "msg")
                    with open(sig, "wb") as f:
                        f.write(hr.raw_to_der(raw))
                    with open(msg, "wb") as f:
                        f.write(("%s.%s" % (h, c)).encode())
                    v = subprocess.run([ossl, "dgst", "-sha256", "-verify", pub, "-signature", sig, msg],
                                       capture_output=True, text=True)
                    self.assertEqual(v.returncode, 0, v.stdout + v.stderr)
                    # 내용이 바뀌면 검증 실패해야 한다
                    with open(msg, "wb") as f:
                        f.write(("%s.%sx" % (h, c)).encode())
                    v2 = subprocess.run([ossl, "dgst", "-sha256", "-verify", pub, "-signature", sig, msg],
                                        capture_output=True, text=True)
                    self.assertNotEqual(v2.returncode, 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 푸시 중계

SIG = "v1." + "A" * 40 + "-_z"


class FakeRelay:
    """127.0.0.1 임의 포트의 가짜 중계. 요청과 TCP 연결 수를 기록하고, 준비한 응답을 차례로 돌려준다."""

    def __init__(self):
        self.requests = []
        self.connections = 0
        self.responses = []  # (code, body dict, 응답 뒤 연결 끊기)
        relay = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self):
                relay.connections += 1
                super().setup()

            def log_message(self, *a):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                relay.requests.append((self.path, self.headers.get("Content-Type"), json.loads(raw)))
                code, body, drop = relay.responses.pop(0) if relay.responses else (200, {"ok": True}, False)
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                if drop:  # Connection: close 없이 끊는다(유휴 연결을 서버가 닫은 것처럼)
                    self.close_connection = True

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.relay = FakeRelay()
        self.tmp = tempfile.mkdtemp()
        self.cfg = dict(hr.DEFAULTS, push_relay_url=self.relay.url + "/",
                        apns_key_path=os.path.join(self.tmp, "missing.p8"), _dir=self.tmp)
        self.devices = hr.DeviceStore(os.path.join(self.tmp, "devices.json"))
        self.sleeps = []
        self.p = hr.RelayPusher(dict(self.cfg), self.devices, sleep=self.sleeps.append)

    def tearDown(self):
        self.p._drop_conn()
        self.relay.close()

    def test_request_shape(self):
        self.devices.add("aa" * 32, "production", relay_sig=SIG)
        payload = hr.build_payload("t1", "n", "done", {"msg": "끝"})
        self.assertTrue(self.p.available())
        self.assertEqual(self.p.push(payload), 1)
        path, ctype, body = self.relay.requests[0]
        self.assertEqual(path, "/v1/push")
        self.assertEqual(ctype, "application/json")
        self.assertRegex(body.pop("collapse_id"), r"^[0-9a-f]{32}$")
        self.assertEqual(body, {"device_token": "aa" * 32, "env": "production", "relay_sig": SIG,
                                "ttl": 6 * 3600, "payload": payload})

    def test_ttl_by_kind_and_override(self):
        self.devices.add("aa" * 32, "production", relay_sig=SIG)
        self.p.push(hr.build_payload("t1", "n", "blocked", PERM))
        self.p.push_device(self.devices.list()[0], {"aps": {"category": "HR_TEST"}, "category": "HR_TEST"})
        self.p.push(hr.build_payload("t1", "n", "done", {}), ttl=hr.PUSH_TTL_TEST)  # test-push 명령
        self.assertEqual([r[2]["ttl"] for r in self.relay.requests], [3600, 300, 300])

    def test_collapse_id_per_push_and_same_on_retry(self):
        self.devices.add("aa" * 32, "sandbox", relay_sig=SIG)
        self.devices.add("bb" * 32, "sandbox", relay_sig=SIG)
        payload = hr.build_payload("t1", "n", "done", {})
        self.relay.responses = [(200, {"ok": True}, False), (200, {"ok": True}, True)]
        self.assertEqual(self.p.push(payload), 2)  # 두 번째 응답 뒤 서버가 연결을 끊는다
        self.assertEqual(self.p.push(payload), 2)  # 첫 요청은 끊긴 연결 → 재시도
        ids = [r[2]["collapse_id"] for r in self.relay.requests]
        self.assertEqual(len(ids), 4)
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(ids[2], ids[3])
        self.assertNotEqual(ids[0], ids[2])
        # 재시도는 같은 본문(같은 collapse_id)을 다시 보낸다
        sent = []
        orig = self.p._new_conn

        def new_conn():
            c = orig()
            real = c.request

            def request(method, url, body=None, headers=None):
                sent.append(json.loads(body)["collapse_id"])
                if len(sent) == 1:
                    raise ConnectionResetError("simulated")
                return real(method, url, body=body, headers=headers)
            c.request = request
            return c

        self.p._drop_conn()
        self.p._new_conn = new_conn
        self.devices.remove("bb" * 32)
        self.assertEqual(self.p.push(payload), 1)
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0], sent[1])
        self.assertEqual(self.relay.requests[-1][2]["collapse_id"], sent[0])

    def test_idle_connection_replaced(self):
        now = [1000.0]
        self.p = hr.RelayPusher(dict(self.cfg), self.devices, clock=lambda: now[0])
        self.devices.add("aa" * 32, "sandbox", relay_sig=SIG)
        payload = hr.build_payload("t1", "n", "done", {})
        self.assertEqual(self.p.push(payload), 1)
        now[0] += 59
        self.assertEqual(self.p.push(payload), 1)
        self.assertEqual(self.relay.connections, 1)
        now[0] += 60  # 마지막 사용 후 60초 이하 → 재사용
        self.assertEqual(self.p.push(payload), 1)
        self.assertEqual(self.relay.connections, 1)
        now[0] += 61  # 60초 넘게 쉼 → 닫고 새로
        old = self.p._conn
        self.assertEqual(self.p.push(payload), 1)
        self.assertEqual(self.relay.connections, 2)
        self.assertIsNot(self.p._conn, old)

    def test_bad_sig_clears_sig_and_flags_device(self):
        self.devices.add("aa" * 32, "sandbox", relay_sig=SIG)
        self.devices.add("bb" * 32, "sandbox", relay_sig=SIG)
        self.relay.responses = [(403, {"error": "bad_sig"}, False), (200, {"ok": True}, False)]
        self.assertEqual(self.p.push(hr.build_payload("t1", "n", "done", {})), 1)
        a, b = self.devices.list()
        self.assertNotIn("relay_sig", a)
        self.assertTrue(a["relay_sig_bad"])
        self.assertEqual(b["relay_sig"], SIG)
        self.assertNotIn("relay_sig_bad", b)
        self.assertEqual(self.devices.relay_resign(),
                         [hashlib.sha256(("aa" * 32).encode()).hexdigest()[:16]])
        # 다음 푸시: 서명 없는 기기는 요청 없이 건너뛴다
        n = len(self.relay.requests)
        self.assertEqual(self.p.push(hr.build_payload("t1", "n", "done", {})), 1)
        self.assertEqual([r[2]["device_token"] for r in self.relay.requests[n:]], ["bb" * 32])
        # 새 서명으로 다시 등록되면 플래그가 사라진다
        sig2 = "v1." + "B" * 43
        self.devices.add("aa" * 32, "sandbox", relay_sig=sig2)
        self.assertEqual(self.devices.relay_resign(), [])
        # 그 사이 다시 등록돼 서명이 바뀌었으면 거절 표시가 새 서명을 지우지 않는다
        self.assertFalse(self.devices.mark_relay_sig_bad("aa" * 32, SIG))
        self.assertEqual(self.devices.list()[1]["relay_sig"], sig2)

    def test_connection_reused_and_retry_after_close(self):
        self.devices.add("aa" * 32, "sandbox", relay_sig=SIG)
        payload = hr.build_payload("t1", "n", "done", {})
        for _ in range(3):
            self.assertEqual(self.p.push(payload), 1)
        self.assertEqual(self.relay.connections, 1)
        self.relay.responses = [(200, {"ok": True}, True)]
        self.assertEqual(self.p.push(payload), 1)  # 이 응답 뒤 서버가 끊는다
        self.assertEqual(self.p.push(payload), 1)  # 끊긴 연결 → 새 연결로 재시도
        self.assertEqual(self.relay.connections, 2)
        self.assertEqual(len(self.relay.requests), 5)

    def test_connection_refused_raises_inside_push(self):
        self.relay.close()
        self.devices.add("aa" * 32, "sandbox", relay_sig=SIG)
        self.assertEqual(self.p.push(hr.build_payload("t1", "n", "done", {})), 0)
        self.assertEqual(len(self.devices.list()), 1)

    def test_status_handling(self):
        cases = [("aa", (410, {"ok": False, "reason": "Unregistered"}, False), False),
                 ("bb", (400, {"ok": False, "reason": "BadDeviceToken"}, False), False),
                 ("cc", (403, {"error": "bad_sig"}, False), True),
                 ("dd", (429, {"error": "rate_limited", "scope": "device"}, False), True),
                 ("ee", (502, {"error": "upstream_failed"}, False), True),
                 ("ff", (400, {"error": "bad_request"}, False), True)]
        for t, _, _ in cases:
            self.devices.add(t * 32, "sandbox", relay_sig=SIG)
        # 5xx는 한 번 다시 보낸다(같은 응답을 한 번 더 준비)
        self.relay.responses = [r for c in cases for r in ([c[1]] * (2 if c[1][0] >= 500 else 1))]
        self.assertEqual(self.p.push(hr.build_payload("t1", "n", "done", {})), 0)
        devs = self.devices.list()
        self.assertEqual([d["token"] for d in devs], [t * 32 for t, _, keep in cases if keep])
        self.assertEqual([d.get("relay_sig") for d in devs], [None, SIG, SIG, SIG])  # bad_sig만 서명을 지운다

    def test_send_one_reasons(self):
        dev = {"token": "aa" * 32, "env": "sandbox", "relay_sig": SIG}
        self.relay.responses = [(403, {"error": "bad_sig"}, False), (410, {"ok": False, "reason": "Unregistered"}, False),
                                (200, {"ok": True, "apns_id": "x"}, False)]
        self.assertEqual(self.p.send_one(dev, {}), (403, "relay:bad_sig"))
        self.assertEqual(self.p.send_one(dev, {}), (410, "Unregistered"))
        self.assertEqual(self.p.send_one(dev, {}), (200, ""))

    def test_device_without_sig_skipped(self):
        self.devices.add("aa" * 32, "sandbox")
        self.devices.add("bb" * 32, "sandbox", relay_sig=SIG)
        with self.assertLogs(hr.log, "WARNING") as cm:
            self.assertEqual(self.p.push(hr.build_payload("t1", "n", "done", {})), 1)
            self.assertEqual(self.p.push(hr.build_payload("t1", "n", "done", {})), 1)
            hr.log.warning("marker")
        self.assertEqual([r[2]["device_token"] for r in self.relay.requests], ["bb" * 32] * 2)
        self.assertEqual(sum("relay_sig" in m for m in cm.output), 1)  # 기기마다 한 번
        self.assertEqual(len(self.devices.list()), 2)

    def test_logs_hide_token_sig_and_body(self):
        self.devices.add("aa" * 32, "sandbox", relay_sig=SIG)
        self.relay.responses = [(403, {"error": "bad_sig"}, False)]
        with self.assertLogs(hr.log, "INFO") as cm:
            self.p.push(hr.build_payload("t1", "n", "done", {"msg": "비밀 본문"}))
        joined = "\n".join(cm.output)
        self.assertNotIn("aa" * 32, joined)
        self.assertNotIn(SIG, joined)
        self.assertNotIn("비밀 본문", joined)
        self.assertIn("다시 등록", joined)

    def test_load_config_relay_url(self):
        path = os.path.join(self.tmp, "config.json")
        key = os.path.join(self.tmp, "AuthKey.p8")
        with open(key, "w") as f:
            f.write("k")
        for url, want in (("https://relay.example.com/", "https://relay.example.com"),
                          ("https://relay.example.com/base", "https://relay.example.com/base"),
                          ("http://127.0.0.1:8787", "http://127.0.0.1:8787"),
                          ("http://localhost:8787/", "http://localhost:8787"),
                          ("", hr.DEFAULT_RELAY_URL if os.name == "nt" else "")):  # Windows는 키 파일이 있어도 중계
            with open(path, "w") as f:
                json.dump({"token": "x", "push_relay_url": url, "apns_key_path": key}, f)
            self.assertEqual(hr.load_config(path)["push_relay_url"], want)
        with open(path, "w") as f:
            json.dump({"token": "x", "apns_key_path": key}, f)
        self.assertEqual(hr.load_config(path)["push_relay_url"], hr.DEFAULT_RELAY_URL if os.name == "nt" else "")
        for bad in ("http://example.com", "ftp://relay.example.com", "https://", "relay.example.com",
                    "https://u:p@relay.example.com", "https://relay.example.com?x=1", "http://127.0.0.2",
                    "https://relay.example.com:abc", 5):
            with open(path, "w") as f:
                json.dump({"token": "x", "push_relay_url": bad}, f)
            with self.assertRaises(ValueError, msg=repr(bad)):
                hr.load_config(path)

    def test_build_app_picks_pusher(self):
        cfg = dict(self.cfg, hc_path="/nonexistent/hc")
        self.assertIsInstance(hr.build_app(cfg).pusher, hr.RelayPusher)
        cfg["push_relay_url"] = ""
        app = hr.build_app(cfg)
        self.assertIs(type(app.pusher), hr.ApnsPusher)


class RelayHttpTests(unittest.TestCase):
    def start(self, relay_url):
        self.tmp = tempfile.mkdtemp()
        self.app, _, _ = make_app(self.tmp)
        self.app.cfg["push_relay_url"] = relay_url
        self.srv = hr.make_server(self.app, "127.0.0.1", 0)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def req(self, *a, **k):
        return HttpTests.req(self, *a, **k)

    def test_health_includes_relay_url(self):
        self.start("https://relay.example.com")
        self.assertEqual(self.req("GET", "/api/health"),
                         (200, {"ok": True, "name": hr.machine_name(), "started": round(hr.PROCESS_STARTED, 3), "push_relay_url": "https://relay.example.com", "relay_resign": [],
                                "push": {"last_success_at": None, "relay_limited_at": None, "failing": False}}))
        self.app.devices.add("ab" * 32, "sandbox", relay_sig=SIG)
        self.app.devices.add("cd" * 32, "sandbox", relay_sig=SIG)
        self.app.devices.mark_relay_sig_bad("cd" * 32, SIG)
        want = hashlib.sha256(("cd" * 32).encode()).hexdigest()[:16]
        self.assertEqual(self.req("GET", "/api/health")[1]["relay_resign"], [want])

    def test_health_without_relay_url(self):
        self.start("")
        self.assertEqual(self.req("GET", "/api/health"), (200, {"ok": True, "name": hr.machine_name(), "started": round(hr.PROCESS_STARTED, 3),
                                                               "push": {"last_success_at": None, "relay_limited_at": None, "failing": False}}))

    def test_add_device_relay_sig(self):
        self.start("https://relay.example.com")
        tok = "AB" * 32
        with self.assertLogs(hr.log, "INFO") as cm:
            self.assertEqual(self.req("POST", "/api/devices", {"token": tok, "env": "sandbox", "relay_sig": SIG}),
                             (200, {"ok": True}))
        self.assertNotIn(SIG, "\n".join(cm.output))
        self.assertEqual([(d["token"], d["relay_sig"]) for d in self.app.devices.list()], [(tok.lower(), SIG)])
        for bad in ("v1." + "A" * 42, "v1." + "A" * 44, "v2." + "A" * 43, "v1." + "A" * 42 + "=", 7, ""):
            self.assertEqual(self.req("POST", "/api/devices", {"token": tok, "env": "sandbox", "relay_sig": bad}),
                             (400, {"error": "bad_relay_sig"}), bad)
        self.assertEqual(self.app.devices.list()[0]["relay_sig"], SIG)  # 거절된 요청은 기존 항목을 안 건드린다
        # relay_sig 없이 재등록하면 기존처럼 교체(서명 없음)
        self.assertEqual(self.req("POST", "/api/devices", {"token": tok, "env": "production"}), (200, {"ok": True}))
        devs = self.app.devices.list()
        self.assertEqual(len(devs), 1)
        self.assertEqual(devs[0]["env"], "production")
        self.assertNotIn("relay_sig", devs[0])


# ---------------------------------------------------------------- HTTP end-to-end

class HttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.app, self.hc, self.pusher = make_app(self.tmp)
        self.hc.add("t12", PERM, name="hrtest")
        self.hc.add("t13", TRUST)
        self.hc.add("t14", st("working"))
        self.hc.add("sh1", None, agent=None, argv=["/bin/zsh"])
        self.srv = hr.make_server(self.app, "127.0.0.1", 0)
        self.port = self.srv.server_address[1]
        self.th = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.th.start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def req(self, method, path, body=None, token=TOKEN, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        r = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), data=data, method=method)
        if token is not None:
            r.add_header("Authorization", "Bearer " + token)
        if data is not None:
            r.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"{}")

    def test_auth(self):
        self.assertEqual(self.req("GET", "/api/health", token=None)[0], 401)
        self.assertEqual(self.req("GET", "/api/health", token="wrong")[0], 401)
        self.assertEqual(self.req("POST", "/api/terminals/t12/keys", {"keys": ["1"]}, token="x")[0], 401)
        self.assertEqual(self.hc.keys_calls, [])
        self.assertEqual(self.req("GET", "/api/health"), (200, {"ok": True, "name": hr.machine_name(), "started": round(hr.PROCESS_STARTED, 3),
                                                               "push": {"last_success_at": None, "relay_limited_at": None, "failing": False}}))

    def test_terminals_and_screen(self):
        code, body = self.req("GET", "/api/terminals")
        self.assertEqual(code, 200)
        by = {t["id"]: t for t in body["terminals"]}
        self.assertEqual(by["t12"]["name"], "hrtest")
        self.assertEqual(by["t12"]["status"]["state"], "blocked")
        self.assertTrue(by["t12"]["claude"])
        self.assertFalse(by["sh1"]["claude"])
        self.assertEqual(self.req("GET", "/api/terminals/t12/screen?lines=50"),
                         (200, {"text": "screen of t12 (50)", "complete": True}))
        self.assertEqual(self.req("GET", "/api/terminals/t12/screen?lines=301")[0], 400)
        self.assertEqual(self.req("GET", "/api/terminals/t12/screen?lines=-1")[0], 400)
        self.assertEqual(self.req("GET", "/api/terminals/nope/screen")[0], 404)

    def test_live_cwd_prefers_foreground(self):
        # Herdr에서 새 Space를 ~에서 열고 cd한 뒤 claude를 띄운 경우: 터미널 cwd는 ~, 지금 폴더는 foreground.cwd
        info = {"cwd": "/Users/me", "foreground": {"argv": ["claude"], "cwd": "/Users/me/proj", "pid": 1}}
        self.assertEqual(hr.live_cwd(info), "/Users/me/proj")
        self.assertEqual(hr.live_cwd({"cwd": "/Users/me"}), "/Users/me")
        self.assertEqual(hr.live_cwd({"cwd": "/Users/me", "foreground": {"cwd": ""}}), "/Users/me")
        self.assertEqual(hr.live_cwd({"cwd": "/Users/me", "foreground": None}), "/Users/me")
        self.assertIsNone(hr.live_cwd(None))

    def test_bad_input_never_calls_hc_keys(self):
        cases = [
            ("/api/terminals/t12/keys", {"keys": ["ctrl+c"]}, 400),
            ("/api/terminals/t12/keys", {"keys": ["1"] * 11}, 400),
            ("/api/terminals/t12/keys", {"keys": []}, 400),
            ("/api/terminals/t12/send", {"text": "rm\x00"}, 400),
            ("/api/terminals/t12/send", {"text": "\x1b[201~"}, 400),
            ("/api/terminals/t12/send", {"text": ""}, 400),
            ("/api/terminals/mini/t12/keys", {"keys": ["1"]}, 400),
            ("/api/terminals/mini%2Ft12/keys", {"keys": ["1"]}, 400),
            ("/api/terminals/..%2F..%2Fx/keys", {"keys": ["1"]}, 400),
            ("/api/terminals/t99/keys", {"keys": ["1"]}, 404),
            ("/api/terminals/t99/send", {"text": "hi"}, 404),
            ("/api/terminals/t99/answer", {"action": "approve"}, 404),
            ("/api/terminals/t12/answer", {"action": "rm"}, 400),
        ]
        for path, body, exp in cases:
            with self.subTest(path=path, body=body):
                self.assertEqual(self.req("POST", path, body)[0], exp)
        self.assertEqual(self.req("POST", "/api/terminals/t12/keys", raw=b"{nope")[0], 400)
        self.assertEqual(self.hc.keys_calls, [])
        self.assertEqual(self.hc.send_calls, [])

    def test_body_limit(self):
        code, body = self.req("POST", "/api/terminals/t12/send", raw=b"x" * (64 * 1024 + 1))
        self.assertEqual((code, body["error"]), (413, "too_large"))

    def test_shell_terminal_forbidden(self):
        self.assertEqual(self.req("POST", "/api/terminals/sh1/send", {"text": "ls"})[0], 403)
        self.assertEqual(self.req("POST", "/api/terminals/sh1/keys", {"keys": ["enter"]})[0], 403)
        self.assertEqual(self.req("POST", "/api/terminals/sh1/answer", {"action": "reject"})[0], 403)
        self.assertEqual(self.hc.keys_calls, [])
        self.assertEqual(self.hc.send_calls, [])

    def test_keys_and_send_ok(self):
        self.assertEqual(self.req("POST", "/api/terminals/t14/keys", {"keys": ["up", "enter", "space"]})[0], 200)
        self.assertEqual(self.req("POST", "/api/terminals/t14/send", {"text": "계속해\n줄"})[0], 200)
        self.assertEqual(self.hc.keys_calls, [("t14", ["up", "enter", "space"])])
        self.assertEqual(self.hc.send_calls, [("t14", "계속해\n줄")])
        self.hc.send_error = hr.HcError("blocked")
        self.assertEqual(self.req("POST", "/api/terminals/t14/send", {"text": "x"})[0], 409)

    def test_answer(self):
        code, body = self.req("POST", "/api/terminals/t14/answer", {"action": "approve"})
        self.assertEqual((code, body["error"]), (409, "not_blocked"))
        self.assertEqual(self.hc.keys_calls, [])
        code, body = self.req("POST", "/api/terminals/t12/answer", {"action": "approve", "expect_msg": "approve Bash: x"})
        self.assertEqual((code, body["error"]), (409, "changed"))
        self.assertEqual(self.hc.keys_calls, [])
        self.assertEqual(self.req("POST", "/api/terminals/t12/answer",
                                  {"action": "approve", "expect_msg": PERM["msg"]})[0], 200)
        self.assertEqual(self.req("POST", "/api/terminals/t13/answer", {"action": "approve"})[0], 200)
        self.assertEqual(self.req("POST", "/api/terminals/t12/answer", {"action": "reject"})[0], 200)
        self.assertEqual(self.hc.keys_calls, [("t12", ["1"]), ("t13", ["down", "enter"]), ("t12", ["esc"])])
        self.hc.set_status("t12", DIALOG)
        code, body = self.req("POST", "/api/terminals/t12/answer", {"action": "approve"})
        self.assertEqual((code, body["error"]), (409, "needs_manual"))

    def test_devices(self):
        tok = "AB" * 32
        self.assertEqual(self.req("POST", "/api/devices", {"token": tok, "env": "sandbox"})[0], 200)
        self.assertEqual(self.req("POST", "/api/devices", {"token": tok, "env": "production"})[0], 200)
        self.assertEqual([(d["token"], d["env"]) for d in self.app.devices.list()], [(tok.lower(), "production")])
        self.assertEqual(self.req("POST", "/api/devices", {"token": "zz" * 32, "env": "sandbox"})[0], 400)
        self.assertEqual(self.req("POST", "/api/devices", {"token": "ab" * 8, "env": "sandbox"})[0], 400)
        self.assertEqual(self.req("POST", "/api/devices", {"token": tok, "env": "dev"})[0], 400)
        self.assertEqual(self.req("DELETE", "/api/devices", {"token": tok}), (200, {"ok": True, "removed": True}))
        self.assertEqual(self.app.devices.list(), [])


# ---------------------------------------------------------------- 메뉴 카드 파싱(실제 화면 2.1.329)

class ParsePromptTests(unittest.TestCase):
    def p(self, name, **kw):
        return hr.parse_prompt(fixture(name), **kw)

    def test_side_panel_is_cut(self):
        lines = hr.screen_lines(fixture("claude-bash-2.1.329.txt"))
        self.assertFalse(any("│" in ln for ln in lines if "Do you want" in ln or "1. Yes" in ln))
        self.assertTrue(all("펼침" not in ln for ln in lines))
        # 패널이 없는 화면(계획, 200열 전부 본문)은 그대로
        plan = fixture("claude-plan-2.1.329.txt").split("\n")
        self.assertEqual(hr.strip_side_panel(plan), plan)

    def test_approval_bash(self):
        p = self.p("claude-bash-2.1.329.txt")
        self.assertEqual((p["kind"], p["title"], p["question"]), ("approval", "Bash command", "Do you want to proceed?"))
        self.assertEqual(p["body"], "Create sub folder with empty a.txt\nmkdir sub && touch sub/a.txt && ls -la sub")
        self.assertEqual([o["label"][:30] for o in p["options"]],
                         ["Yes", "Yes, and always allow access t", "Yes, and switch to auto mode ·", "No"])
        self.assertTrue(p["options"][1]["label"].endswith("hrtest/sub from this project"))
        # "always allow access to <dir> from this project"는 프로젝트 영구 허용: 세션 한정(always)이 아니다
        self.assertEqual((p["selected"], p["always_index"], p["persist_index"], p["reject_index"]), (0, None, 1, 3))
        self.assertRegex(p["id"], r"^[0-9a-f]{12}$")

    def test_approval_write_and_overwrite(self):
        p = self.p("claude-write-2.1.329.txt")
        self.assertEqual((p["kind"], p["title"], p["question"]), ("approval", "Create file", "Do you want to create hello.txt?"))
        self.assertIn("1 hi", p["body"])
        self.assertEqual((p["always_index"], p["persist_index"], p["reject_index"], len(p["options"])), (1, None, 2, 3))
        self.assertTrue(p["options"][1]["label"].endswith("for this session (shift+tab)"))
        # 마지막 줄이 이름 붙은 가로줄(── change-hello-txt-to-hey ─)이어도 힌트를 찾는다
        o = self.p("claude-overwrite-2.1.329.txt")
        self.assertEqual((o["title"], o["question"]), ("Overwrite file", "Do you want to overwrite hello.txt?"))
        self.assertIn("1 -hi", o["body"])
        self.assertIn("1 +hey", o["body"])

    def test_approval_session_vs_persist_labels(self):
        cases = [
            (["Yes", "Yes, allow all edits during this session (shift+tab)", "No"], 1, None),
            (["Yes", "Yes, and don't ask again for npm test commands in /Users/dev/proj", "No"], None, 1),
            (["Yes", "Yes, and don\u2019t ask again this session", "No"], 1, None),
            (["Yes", "Yes, and switch to accept edits for this session (shift+tab)",
              "Yes, and always allow access to /tmp/x from this project", "No"], 1, 2),
            (["Yes", "Yes, and switch to auto mode", "No"], None, None),
        ]
        for labels, always, persist in cases:
            with self.subTest(labels=labels):
                p = hr.parse_prompt(approval_screen(labels))
                self.assertEqual((p["always_index"], p["persist_index"]), (always, persist))

    def test_question_tabs(self):
        p = self.p("claude-question-2.1.329.txt")
        self.assertEqual((p["kind"], p["question"], p["multi_select"]), ("question", "좋아하는 색은 뭐야?", False))
        self.assertEqual([(o["label"], o["description"]) for o in p["options"]],
                         [("빨강", "빨간색"), ("파랑", "파란색"), ("초록", "초록색"), ("Type something.", None)])
        self.assertEqual((p["free_text_index"], p["selected"]), (3, 0))
        self.assertEqual(p["tabs"], [{"label": "색", "answered": False}, {"label": "과일", "answered": False}])
        self.assertIsNone(p["title"])

    def test_question_single_chip(self):
        p = self.p("claude-question-single-2.1.329.txt")
        self.assertEqual((p["title"], p["question"], p["tabs"], p["free_text_index"]),
                         ("계절", "좋아하는 계절은 뭐야?", None, 3))

    def test_question_multi(self):
        p = self.p("claude-question-multi-2.1.329.txt")
        self.assertTrue(p["multi_select"])
        self.assertEqual([o["label"] for o in p["options"]], ["사과", "배", "귤"])
        self.assertEqual((p["checked"], p["free_text_index"]), ([], None))
        self.assertEqual(p["tabs"][0], {"label": "색", "answered": True})
        one = self.p("claude-question-multi-single-2.1.329.txt")
        self.assertEqual((one["title"], one["tabs"]), ("동물", [{"label": "동물", "answered": False}]))
        checked = fixture("claude-question-multi-single-2.1.329.txt").replace("1. [ ] 개", "1. [✔] 개") \
            .replace("3. [ ] 새", "3. [✔] 새")
        c = hr.parse_prompt(checked)
        self.assertEqual(c["checked"], [0, 2])
        self.assertEqual(c["id"], one["id"])  # 체크 상태는 id에 안 들어간다
        # 커서가 Submit 줄에 있으면 ❯가 0개여도 카드
        on_submit = checked.replace("❯ 1. [✔] 개", "  1. [✔] 개").replace("     Submit", "   ❯ Submit")
        s = hr.parse_prompt(on_submit)
        self.assertIsNotNone(s)
        self.assertIsNone(s["selected"])

    def test_review(self):
        p = self.p("claude-question-review-2.1.329.txt")
        self.assertEqual((p["kind"], p["question"]), ("submit", "Review your answers"))
        self.assertEqual([o["label"] for o in p["options"]], ["Submit answers", "Cancel"])
        self.assertIn("→ 사과, 귤", p["body"])

    def test_plan_screen_and_file(self):
        p = self.p("claude-plan-2.1.329.txt")
        self.assertEqual((p["kind"], p["title"], p["free_text_index"]), ("plan", "Ready to code?", 2))
        self.assertTrue(p["body"].startswith("hello.txt 내용을 bye로 변경"))
        self.assertEqual(p["options"][2]["description"], "shift+tab to approve with this feedback")
        asked = []
        f = self.p("claude-plan-2.1.329.txt", plan_reader=lambda path: asked.append(path) or "# 파일 본문")
        self.assertEqual(asked, ["~/.claude/plans/hello-txt-bye-smooth-ullman.md"])
        self.assertEqual(f["body"], "# 파일 본문")
        self.assertNotEqual(f["id"], p["id"])

    def test_plan_reader_stays_in_plans_dir(self):
        home = tempfile.mkdtemp()
        plans = os.path.join(home, ".claude", "plans")
        os.makedirs(plans)
        with open(os.path.join(plans, "a.md"), "w") as f:
            f.write("x" * 20000)
        secret = os.path.join(home, "secret.txt")
        with open(secret, "w") as f:
            f.write("secret")
        os.symlink(secret, os.path.join(plans, "link.md"))
        r = hr.make_plan_reader(home)
        self.assertEqual(len(r("~/.claude/plans/a.md")), 12000)
        self.assertIsNone(r("~/.claude/plans/link.md"))
        self.assertIsNone(r("~/.claude/plans/../../secret.txt"))
        self.assertIsNone(r("/etc/passwd"))

    def test_inactive_screens_are_none(self):
        # 답한 뒤 입력창이 다시 나온 화면: 지나간 메뉴는 카드가 아니다
        lines = fixture("claude-bash-2.1.329.txt").split("\n")
        hint = max(i for i, ln in enumerate(lines) if "Esc to cancel" in ln)
        idle = lines[:hint + 1] + ["", "⏺ 실행했어.", "", "─" * 50, "❯ ", "─" * 50, "  Opus 5.5", "  ⏸ manual mode on"]
        self.assertIsNone(hr.parse_prompt("\n".join(idle)))
        self.assertIsNone(hr.parse_prompt(""))
        self.assertIsNone(hr.parse_prompt("hello\nworld"))
        # 계획 메뉴 아래에 다른 글이 생기면 지나간 메뉴
        plan = fixture("claude-plan-2.1.329.txt") + "\n⏺ 작업 중\n"
        self.assertIsNone(hr.parse_prompt(plan))
        # 번호가 끊기면 None
        broken = fixture("claude-write-2.1.329.txt").replace("2. Yes, and switch", "5. Yes, and switch")
        self.assertIsNone(hr.parse_prompt(broken))
        # 커서가 둘이면 None
        two = fixture("claude-write-2.1.329.txt").replace("  3. No", "❯ 3. No")
        self.assertIsNone(hr.parse_prompt(two))

    def test_confirm_menu(self):
        screen = "\n".join([
            "─" * 60,
            " Do you trust the files in this folder?",
            "",
            " /Users/x/proj",
            "",
            " ❯ Yes, proceed",
            "   No, exit",
            "",
            " Enter to confirm · Esc to exit",
        ])
        p = hr.parse_prompt(screen)
        self.assertEqual((p["kind"], p["selected"], p["reject_index"]), ("confirm", 0, 1))
        self.assertEqual([o["label"] for o in p["options"]], ["Yes, proceed", "No, exit"])
        self.assertEqual(hr.answer_steps(p, option_index=1), [("keys", ["down", "enter"])])

    def test_ansi_removed(self):
        colored = fixture("claude-write-2.1.329.txt").replace("Do you want", "\x1b[1mDo you want\x1b[0m")
        self.assertEqual(hr.parse_prompt(colored)["id"], self.p("claude-write-2.1.329.txt")["id"])


class AnswerStepsTests(unittest.TestCase):
    def test_steps(self):
        a = hr.parse_prompt(fixture("claude-bash-2.1.329.txt"))
        self.assertEqual(hr.answer_steps(a, option_index=1), [("keys", ["2"])])
        q = hr.parse_prompt(fixture("claude-question-single-2.1.329.txt"))
        self.assertEqual(hr.answer_steps(q, text="-겨울"), [("keys", ["4"]), ("text", "-겨울")])
        with self.assertRaises(hr.ApiError):
            hr.answer_steps(q, option_index=3)  # 자유 입력 줄은 text로
        with self.assertRaises(hr.ApiError):
            hr.answer_steps(q, text="a\nb")
        with self.assertRaises(hr.ApiError):
            hr.answer_steps(q, option_index=9)
        m = hr.parse_prompt(fixture("claude-question-multi-single-2.1.329.txt")
                            .replace("1. [ ] 개", "1. [✔] 개"))
        # 지금 [0] 체크 → 원하는 [1,2]: 0·1·2 토글 후 right(다음 탭/리뷰)
        self.assertEqual(hr.answer_steps(m, option_indices=[1, 2]), [("keys", ["1", "2", "3", "right"])])
        with self.assertRaises(hr.ApiError):
            hr.answer_steps(m, option_index=0)
        with self.assertRaises(hr.ApiError):
            hr.answer_steps(m, text="x")
        plan = hr.parse_prompt(fixture("claude-plan-2.1.329.txt"))
        self.assertEqual(hr.answer_steps(plan, text="yo 대신 hey"), [("keys", ["3"]), ("text", "yo 대신 hey")])
        r = hr.parse_prompt(fixture("claude-question-review-2.1.329.txt"))
        self.assertEqual(hr.answer_steps(r, option_index=0), [("keys", ["1"])])


# ---------------------------------------------------------------- 알림: 카드·지연 상한·종료

class PushPromptTests(unittest.TestCase):
    def setUp(self):
        self.app, self.hc, self.pusher = make_app()
        self.hc.add("t1", st("working"), name="hrtest")
        self.app.poll(initial=True)

    def test_blocked_push_carries_prompt_id_and_always_category(self):
        self.hc.screens["t1"] = fixture("claude-write-2.1.329.txt")
        self.hc.set_status("t1", dict(PERM, msg="approve Bash: mkdir sub"))
        self.app.handle_event_line(status_ev("t1", PERM))
        p = self.pusher.payloads[-1]
        card = hr.parse_prompt(fixture("claude-write-2.1.329.txt"))
        self.assertEqual((p["aps"]["category"], p["category"], p["prompt_id"], p["kind"]),
                         ("HR_PERMISSION_ALWAYS", "HR_PERMISSION_ALWAYS", card["id"], "approval"))
        # 프로젝트 영구 허용만 있는 카드는 잠금화면 "세션 동안 허용" 버튼을 주지 않는다
        self.assertEqual(hr.push_category("blocked", PERM, hr.parse_prompt(fixture("claude-bash-2.1.329.txt"))),
                         "HR_PERMISSION")
        self.assertNotIn("options", p)  # 카드 내용은 푸시에 안 넣는다
        self.assertLess(len(json.dumps(p, ensure_ascii=True)), 4096)
        self.assertEqual(self.hc.read_calls, 1)

    def test_question_push_is_blocked_category(self):
        self.hc.screens["t1"] = fixture("claude-question-2.1.329.txt")
        self.hc.set_status("t1", st("blocked", "answer: 색?", "question"))
        self.app.poll()
        p = self.pusher.payloads[-1]
        self.assertEqual((p["aps"]["category"], p["kind"]), ("HR_BLOCKED", "question"))

    def test_slow_screen_read_does_not_delay_push(self):
        self.hc.screens["t1"] = fixture("claude-bash-2.1.329.txt")
        self.hc.read_delay = 1.5
        self.hc.set_status("t1", PERM)
        logs = []
        h = logging.Handler()
        h.emit = lambda rec: logs.append(rec.getMessage())
        hr.log.addHandler(h)
        old_level = hr.log.level
        hr.log.setLevel(logging.INFO)
        try:
            import time
            t0 = time.monotonic()
            self.app.handle_event_line(status_ev("t1", PERM))
            took = time.monotonic() - t0
        finally:
            hr.log.removeHandler(h)
            hr.log.setLevel(old_level)
        p = self.pusher.payloads[-1]
        self.assertLess(took, hr.PUSH_PROMPT_BUDGET + 0.25)
        self.assertEqual((p["aps"]["category"], p["prompt_id"], p["kind"]), ("HR_PERMISSION", None, None))
        lat = [m for m in logs if "감지→발송" in m]
        self.assertEqual(len(lat), 1)
        self.assertRegex(lat[0], r"^알림 t1 blocked HR_PERMISSION 감지→발송 \d+ms$")
        self.assertLess(int(re.search(r"(\d+)ms", lat[0]).group(1)), int((hr.PUSH_PROMPT_BUDGET + 0.25) * 1000))
        self.assertFalse(any("Do you want" in m for m in logs))

    def test_failed_read_falls_back(self):
        def boom(tid, lines=0):
            raise hr.HcError("x")
        self.hc.read = boom
        self.hc.set_status("t1", PERM)
        self.app.poll()
        self.assertEqual((self.pusher.payloads[-1]["aps"]["category"], self.pusher.payloads[-1]["prompt_id"]),
                         ("HR_PERMISSION", None))


class ExitNotifyTests(unittest.TestCase):
    def setUp(self):
        self.app, self.hc, self.pusher = make_app()
        self.hc.add("t1", st("idle"), name="hrtest")
        self.hc.add("sh", None, agent=None, argv=["/bin/zsh"])
        self.app.poll(initial=True)

    def exited(self):
        return [p for p in self.pusher.payloads if p["aps"]["category"] == "HR_EXITED"]

    def test_finished_event_then_removed_and_poll_notify_once(self):
        self.app.handle_event_line(ev("t1", "finished", {"exit_code": 129, "reason": "killed"}))
        self.app.handle_event_line(ev("t1", "removed", {}))
        del self.hc.infos["t1"]
        self.app.poll()
        ex = self.exited()
        self.assertEqual(len(ex), 1)
        self.assertEqual((ex[0]["state"], ex[0]["exit_code"], ex[0]["aps"]["alert"]["title"], ex[0]["terminal"]),
                         ("exited", 129, "hrtest", "t1"))

    def test_vanished_from_poll(self):
        self.hc.infos["t1"]["exit_code"] = 0
        info = self.hc.infos.pop("t1")
        orig_get = self.hc.get
        self.hc.get = lambda tid: dict(info, state="finished") if tid == "t1" else orig_get(tid)
        self.app.poll()
        self.app.poll()
        ex = self.exited()
        self.assertEqual([(p["terminal"], p["exit_code"]) for p in ex], [("t1", 0)])

    def test_non_claude_never(self):
        self.app.handle_event_line(ev("sh", "finished", {"exit_code": 0}))
        self.app.handle_event_line(ev("t9", "removed", {}))
        self.assertEqual(self.exited(), [])


# ---------------------------------------------------------------- 대화 기록

class TranscriptTests(unittest.TestCase):
    def test_parse_fixture(self):
        turns, cut = hr.parse_transcript(fixture("claude-session-2.1.329.jsonl").split("\n"), 50)
        self.assertFalse(cut)
        self.assertEqual([t["role"] for t in turns][:4], ["user", "assistant", "user", "assistant"])
        self.assertEqual(turns[0]["parts"], [{"kind": "text", "text": "hello.txt 파일을 만들고 hi라고 써줘."}])
        write = turns[1]["parts"][0]
        self.assertEqual((write["kind"], write["name"], write["state"]), ("tool", "Write", "ok"))
        self.assertTrue(write["summary"].endswith("hello.txt"))
        self.assertTrue(write["output"].startswith("File created successfully"))
        texts = [p.get("text", "") for t in turns for p in t["parts"]]
        self.assertFalse(any(x.startswith(("<command-", "<local-command", "<task-")) for x in texts))
        self.assertNotIn("SIDECHAIN", texts)
        last = turns[-1]["parts"]
        self.assertEqual([(p["name"], p["state"]) for p in last if p["kind"] == "tool"][-2:],
                         [("ExitPlanMode", "pending"), ("Bash", "error")])
        # 연속 assistant 기록(질문 앞의 글 + AskUserQuestion + 답 받은 뒤 글)은 한 턴
        ask = [t for t in turns if any(p.get("name") == "AskUserQuestion" for p in t["parts"])][0]
        self.assertEqual([p["kind"] for p in ask["parts"]], ["text", "tool", "text"])
        self.assertEqual(ask["parts"][1]["summary"], "좋아하는 색은 뭐야?")
        turns2, cut2 = hr.parse_transcript(fixture("claude-session-2.1.329.jsonl").split("\n"), 2)
        self.assertEqual((len(turns2), cut2), (2, True))

    def test_caps(self):
        big = json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "x" * 9000},
            {"type": "tool_use", "id": "a", "name": "Write", "input": {"file_path": "/f", "content": "y" * 9000}}]}})
        res = json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "a", "content": "z" * 9000}]}})
        turns, _ = hr.parse_transcript([big, res, "not json", "{}"], 50)
        parts = turns[0]["parts"]
        self.assertEqual(len(parts[0]["text"]), 8000)
        self.assertEqual(len(parts[1]["input"]["content"]), 8000)
        self.assertEqual(len(parts[1]["output"]), 8000)

    def test_tool_output_strips_ansi(self):
        use = json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "b", "name": "Bash", "input": {"command": "npm test"}}]}})
        out = "\x1b[32m✓ ok\x1b[0m\r\n\x1b]0;title\x07\x1b[1;31mFAIL\x1b[0m a\x1b(B\rb\x1b[2K"
        res = json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "b",
             "content": [{"type": "text", "text": out}]}]}})
        turns, _ = hr.parse_transcript([use, res], 50)
        self.assertEqual(turns[0]["parts"][0]["output"], "✓ ok\nFAIL a\nb")
        self.assertNotIn("\x1b", turns[0]["parts"][0]["output"])

    def setUp(self):
        if os.name == "nt":  # Windows의 procStart는 프로세스 생성 FILETIME: ps 대신 이 값을 돌려주게 한다
            p = mock.patch.object(hr, "proc_create_filetime", lambda pid: self.WIN_START)
            p.start()
            self.addCleanup(p.stop)

    WIN_START = 134397050731892483

    def _home(self, proc_start=None, kind="interactive"):
        if proc_start is None:
            proc_start = str(self.WIN_START) if os.name == "nt" else "Fri Nov 20 10:01:38 2026"
        home = tempfile.mkdtemp()
        os.makedirs(os.path.join(home, ".claude", "sessions"))
        cwd = "/Users/dev/_scratch/hrtest"
        with open(os.path.join(home, ".claude", "sessions", "4242.json"), "w") as f:
            json.dump({"pid": 4242, "sessionId": "577f6a83-38f4-419d-8e6c-7174fb0ce537", "cwd": cwd,
                       "procStart": proc_start, "kind": kind}, f)
        proj = os.path.join(home, ".claude", "projects", "-Users-dev--scratch-hrtest")
        os.makedirs(proj)
        shutil.copy(os.path.join(FIX, "claude-session-2.1.329.jsonl"),
                    os.path.join(proj, "577f6a83-38f4-419d-8e6c-7174fb0ce537.jsonl"))
        return home

    def runner(self, out="Fri Nov 20 10:01:38 2026    \n"):
        calls = []

        def run(argv, **kw):
            calls.append((argv, kw))
            return subprocess.CompletedProcess(argv, 0, out, "")
        run.calls = calls
        return run

    def test_find_by_pid_and_procstart(self):
        home = self._home()
        run = self.runner()
        sid, path = hr.find_transcript({"pid": 4242, "cwd": "/w"}, home, runner=run)
        self.assertEqual(sid, "577f6a83-38f4-419d-8e6c-7174fb0ce537")
        self.assertEqual(os.path.basename(os.path.dirname(path)), "-Users-dev--scratch-hrtest")
        self.assertEqual(os.path.basename(path), "577f6a83-38f4-419d-8e6c-7174fb0ce537.jsonl")
        if os.name != "nt":  # Windows는 ps를 부르지 않는다(test_windows.py가 FILETIME 경로를 본다)
            argv, kw = run.calls[0]
            self.assertEqual(argv, ["/bin/ps", "-o", "lstart=", "-p", "4242"])
            self.assertEqual((kw["env"]["LC_ALL"], kw["env"]["TZ"], kw["shell"]), ("C", "UTC", False))
        # pid 재사용(시작 시각 다름) → 못 찾음
        with mock.patch.object(hr, "proc_create_filetime", lambda pid: self.WIN_START + 1):
            self.assertEqual(hr.find_transcript({"pid": 4242}, home, runner=self.runner("Sat Nov 21 00:00:00 2026\n")),
                             (None, None))
        self.assertEqual(hr.find_transcript({"pid": 1}, home, runner=run), (None, None))
        self.assertEqual(hr.find_transcript({}, home, runner=run), (None, None))
        self.assertEqual(hr.find_transcript({"pid": 4242}, self._home(kind="print"), runner=run), (None, None))

    def test_fallback_search_and_tail_and_symlink(self):
        home = self._home()
        proj = os.path.join(home, ".claude", "projects")
        os.rename(os.path.join(proj, "-Users-dev--scratch-hrtest"), os.path.join(proj, "other"))
        sid, path = hr.find_transcript({"pid": 4242}, home, runner=self.runner())
        self.assertEqual(os.path.relpath(path, proj), os.path.join("other", "577f6a83-38f4-419d-8e6c-7174fb0ce537.jsonl"))
        # 심볼릭 링크 jsonl은 안 연다
        link = os.path.join(home, "l.jsonl")
        os.symlink(path, link)
        with self.assertRaises(OSError):
            hr.read_transcript(link)
        # 4MB 넘으면 끝만 읽고 첫 잘린 줄은 버린다
        big = os.path.join(home, "big.jsonl")
        line = json.dumps({"type": "user", "message": {"content": "q" * 1000}}) + "\n"
        with open(big, "w") as f:
            for _ in range(5000):
                f.write(line)
            f.write(json.dumps({"type": "user", "message": {"content": "마지막"}}) + "\n")
        turns, cut = hr.read_transcript(big, 3)
        self.assertTrue(cut)
        self.assertEqual(turns[-1]["parts"][0]["text"], "마지막")

    def test_http(self):
        home = self._home()
        app, hc, _ = make_app(home=home, runner=self.runner())
        hc.add("t1", st("idle"))
        hc.infos["t1"]["pid"] = 4242
        srv = hr.make_server(app, "127.0.0.1", 0)
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        try:
            r = urllib.request.Request("http://127.0.0.1:%d/api/terminals/t1/transcript?limit=3" % srv.server_address[1])
            r.add_header("Authorization", "Bearer " + TOKEN)
            with urllib.request.urlopen(r, timeout=5) as resp:
                body = json.loads(resp.read())
            self.assertEqual(body["session_id"], "577f6a83-38f4-419d-8e6c-7174fb0ce537")
            self.assertEqual((len(body["turns"]), body["truncated"]), (3, True))
        finally:
            srv.shutdown()
            srv.server_close()


# ---------------------------------------------------------------- QR

def _png(mod, path, scale=4, quiet=4):
    import struct
    import zlib
    n = len(mod) + 2 * quiet
    rows = b""
    for y in range(n):
        line = bytearray([0])
        for x in range(n):
            mx, my = x - quiet, y - quiet
            d = 0 <= mx < len(mod) and 0 <= my < len(mod) and mod[my][mx]
            line += bytes([0 if d else 255]) * scale
        rows += bytes(line) * scale

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    w = n * scale
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, w, 8, 0, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def _zbarimg():
    for p in ("/opt/homebrew/bin/zbarimg", "/usr/local/bin/zbarimg", shutil.which("zbarimg")):
        if p and os.path.exists(p):
            return p
    return None


class QrTests(unittest.TestCase):
    def test_structure(self):
        for n, size in ((1, 21), (20, 25), (100, 41), (213, 57)):
            m = hr.qr_encode("a" * n)
            self.assertEqual((len(m), len(m[0])), (size, size))
            # 세 모서리 찾기 패턴과 어두운 모듈
            for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):
                self.assertTrue(m[cy][cx])
                self.assertFalse(m[cy - 2][cx])
            self.assertTrue(m[size - 8][8])
            # 형식 정보 15비트가 두 곳에 같고 BCH가 맞다(ECC M)
            a = [m[i][8] for i in range(6)] + [m[7][8], m[8][8], m[8][7]] + [m[8][14 - i] for i in range(9, 15)]
            b = [m[8][size - 1 - i] for i in range(8)] + [m[size - 15 + i][8] for i in range(8, 15)]
            self.assertEqual(a, b)
            bits = sum(1 << i for i, v in enumerate(a) if v) ^ 0x5412
            self.assertEqual(bits >> 13, 0)  # M = 00
            rem = bits >> 10
            for _ in range(10):
                rem = (rem << 1) ^ ((rem >> 9) * 0x537)
            self.assertEqual(((bits >> 10) << 10) | rem, bits)
        with self.assertRaises(ValueError):
            hr.qr_encode("a" * 214)

    @unittest.skipUnless(_zbarimg(), "zbarimg 없음(brew install zbar)")
    def test_zbar_decodes(self):
        base = os.path.expanduser("~/Documents/github/_scratch/herdr-remote")
        os.makedirs(base, exist_ok=True)
        tmp = tempfile.mkdtemp(dir=base)
        try:
            link = hr.pair_url("https://my-mac.tailnet.ts.net:8790", "tok-_" + "A" * 40)
            for s in ("a", "https://x.test/" + "b" * 60, link, "한글" * 20, "z" * 213):
                with self.subTest(n=len(s)):
                    p = os.path.join(tmp, "q.png")
                    _png(hr.qr_encode(s), p)
                    r = subprocess.run([_zbarimg(), "-q", "--raw", p], capture_output=True)
                    self.assertEqual(r.returncode, 0)
                    self.assertEqual(r.stdout.decode("utf-8").rstrip("\n"), s)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_halfblocks(self):
        m = hr.qr_encode("hi")
        out = hr.qr_halfblocks(m, quiet=2, ansi=False).split("\n")
        self.assertEqual(len(out), (21 + 4 + 1) // 2)
        self.assertTrue(all(len(x) == 25 for x in out))
        self.assertEqual(out[1][2:9], "█▀▀▀▀▀█")
        self.assertIn("\x1b[30;47m", hr.qr_halfblocks(m))

    def test_pair_url_and_address(self):
        self.assertEqual(hr.pair_url("https://h.ts.net:8790", "a+b/c"),
                         "sheltie://pair?u=https%3A%2F%2Fh.ts.net%3A8790&t=a%2Bb%2Fc")
        cfg = {"port": 8790}
        self.assertEqual(hr.server_public_url(dict(cfg, public_url="https://p.example/"), runner=lambda *a, **k: 1 / 0),
                         "https://p.example")
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, json.dumps({"Self": {"DNSName": "my-mac.tailnet.ts.net."}}), "")
        self.assertEqual(hr.server_public_url(cfg, runner=run, tailscale="/x/tailscale"),
                         "https://my-mac.tailnet.ts.net:8790")
        self.assertEqual(calls, [["/x/tailscale", "status", "--json"]])
        bad = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, '{"Self":{"DNSName":"a b;rm"}}', "")  # noqa: E731
        self.assertIsNone(hr.server_public_url(cfg, runner=bad, tailscale="/x/tailscale"))

    def test_cmd_pair(self):
        out = io.StringIO()
        cfg = {"token": "T" * 43, "port": 8790, "public_url": "https://h.ts.net:8790"}
        self.assertEqual(hr.cmd_pair(out=out, cfg=cfg), 0)
        text = out.getvalue()
        self.assertIn("sheltie://pair?u=https%3A%2F%2Fh.ts.net%3A8790&t=" + "T" * 43, text)
        self.assertIn("▀", text)


# ---------------------------------------------------------------- HTTP: 카드·폴더·새 에이전트

class PromptHttpTests(unittest.TestCase):
    def setUp(self):
        self.home = os.path.realpath(tempfile.mkdtemp())
        for d in ("proj/a", "proj/b", "proj/Library", ".hidden", "Docs", "Library"):
            os.makedirs(os.path.join(self.home, d))
        self.outside = os.path.realpath(tempfile.mkdtemp())
        os.symlink(self.outside, os.path.join(self.home, "escape"))
        os.symlink(os.path.join(self.home, "proj"), os.path.join(self.home, "plink"))
        self.app, self.hc, self.pusher = make_app(home=self.home)
        self.hc.add("t1", PERM, name="hrtest")
        self.hc.screens["t1"] = fixture("claude-bash-2.1.329.txt")
        self.hc.add("t2", st("blocked", "answer: 계절", "question"))
        self.hc.screens["t2"] = fixture("claude-question-single-2.1.329.txt")
        self.hc.add("t3", st("working"))
        self.hc.screens["t3"] = fixture("claude-bash-2.1.329.txt")
        self.hc.infos["t1"]["cwd"] = os.path.join(self.home, "proj", "a")
        self.hc.infos["t3"]["cwd"] = self.outside
        self.hc.add("sh1", None, agent=None, argv=["/bin/zsh"])
        self.srv = hr.make_server(self.app, "127.0.0.1", 0)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    req = HttpTests.req

    def card(self, tid):
        code, body = self.req("GET", "/api/terminals/%s/prompt" % tid)
        self.assertEqual(code, 200)
        return body["prompt"]

    def test_get_prompt(self):
        p = self.card("t1")
        self.assertEqual((p["kind"], p["always_index"], p["persist_index"]), ("approval", None, 1))
        self.assertIsNone(self.card("t3"))  # blocked가 아니면 null
        self.assertEqual(self.req("GET", "/api/terminals/sh1/prompt")[0], 403)
        self.assertEqual(self.req("GET", "/api/terminals/t1/prompt", token="x")[0], 401)

    def test_prompt_answer(self):
        pid = self.card("t1")["id"]
        self.assertEqual(self.req("POST", "/api/terminals/t1/prompt/answer", {"prompt_id": pid, "option_index": 1}),
                         (200, {"ok": True}))
        self.assertEqual(self.hc.keys_calls, [("t1", ["2"])])
        # 화면이 바뀌면 409 + 지금 카드
        self.hc.screens["t1"] = fixture("claude-write-2.1.329.txt")
        code, body = self.req("POST", "/api/terminals/t1/prompt/answer", {"prompt_id": pid, "option_index": 0})
        self.assertEqual((code, body["error"], body["prompt"]["title"]), (409, "prompt_changed", "Create file"))
        self.hc.set_status("t1", st("working"))
        code, body = self.req("POST", "/api/terminals/t1/prompt/answer", {"prompt_id": pid, "option_index": 0})
        self.assertEqual((code, body["error"], body["prompt"]), (409, "prompt_changed", None))
        self.assertEqual(len(self.hc.keys_calls), 1)

    def test_prompt_answer_text(self):
        pid = self.card("t2")["id"]
        self.assertEqual(self.req("POST", "/api/terminals/t2/prompt/answer", {"prompt_id": pid, "text": "-겨울"})[0], 200)
        self.assertEqual(self.hc.keys_calls, [("t2", ["4"])])
        self.assertEqual(self.hc.send_calls, [("t2", "-겨울", "force")])

    def test_prompt_answer_bad_input(self):
        pid = self.card("t1")["id"]
        for body in ({"option_index": 0}, {"prompt_id": "zz", "option_index": 0},
                     {"prompt_id": pid}, {"prompt_id": pid, "option_index": 0, "text": "x"},
                     {"prompt_id": pid, "option_index": 7}, {"prompt_id": pid, "option_index": True},
                     {"prompt_id": pid, "option_index": "1"}, {"prompt_id": pid, "text": "a\x1b"},
                     {"prompt_id": pid, "option_indices": [0]}):
            with self.subTest(body=body):
                self.assertEqual(self.req("POST", "/api/terminals/t1/prompt/answer", body)[0], 400)
        self.assertEqual(self.req("POST", "/api/terminals/sh1/prompt/answer", {"prompt_id": pid, "option_index": 0})[0], 403)
        self.assertEqual(self.hc.keys_calls, [])
        self.assertEqual(self.hc.send_calls, [])

    def test_legacy_answer_with_prompt_id(self):
        pid = self.card("t1")["id"]
        # 프로젝트 영구 허용은 always(세션 동안 허용)로 누르지 않는다
        code, body = self.req("POST", "/api/terminals/t1/answer", {"action": "always", "prompt_id": pid})
        self.assertEqual((code, body["error"]), (409, "no_always"))
        self.assertEqual(self.hc.keys_calls, [])
        self.assertEqual(self.req("POST", "/api/terminals/t1/answer", {"action": "approve", "prompt_id": pid})[1]["keys"], ["1"])
        self.assertEqual(self.req("POST", "/api/terminals/t1/answer", {"action": "reject", "prompt_id": pid})[1]["keys"], ["4"])
        code, body = self.req("POST", "/api/terminals/t1/answer", {"action": "approve", "prompt_id": "0" * 12})
        self.assertEqual((code, body["error"]), (409, "prompt_changed"))
        self.hc.screens["t1"] = fixture("claude-plan-2.1.329.txt")
        plan = self.card("t1")["id"]
        # 계획 카드: /answer의 approve·always는 auto mode를 고를 수 있어 앱에서 직접(needs_manual), reject만 esc
        for action in ("always", "approve"):
            code, body = self.req("POST", "/api/terminals/t1/answer", {"action": action, "prompt_id": plan})
            self.assertEqual((code, body["error"]), (409, "needs_manual"), action)
        self.assertEqual(self.req("POST", "/api/terminals/t1/answer", {"action": "reject", "prompt_id": plan})[1]["keys"], ["esc"])
        q = self.card("t2")["id"]
        code, body = self.req("POST", "/api/terminals/t2/answer", {"action": "approve", "prompt_id": q})
        self.assertEqual((code, body["error"]), (409, "needs_manual"))
        # prompt_id 없이 always는 거부, approve는 기존 동작(expect_msg)
        self.assertEqual(self.req("POST", "/api/terminals/t1/answer", {"action": "always"})[0], 400)
        self.assertEqual(self.req("POST", "/api/terminals/t1/answer", {"action": "approve", "expect_msg": PERM["msg"]})[1]["keys"], ["1"])

    def test_tool_summary_tilde(self):
        home = os.path.expanduser("~")
        self.assertEqual(hr._tool_summary("Edit", {"file_path": home + "/p/README.md"}), "~/p/README.md")
        self.assertEqual(hr._tool_summary("Grep", {"pattern": "x", "path": home}), "~")
        self.assertEqual(hr._tool_summary("Read", {"file_path": home + "x/a"}), home + "x/a")  # 접두사만 같은 다른 폴더
        self.assertEqual(hr._tool_summary("Read", {"file_path": "/etc/hosts"}), "/etc/hosts")
        self.assertEqual(hr._tool_summary("Bash", {"command": "cat " + home + "/a"}), "cat " + home + "/a")

    def test_folders_recent_skips_home(self):
        self.hc.infos["t2"]["cwd"] = self.home  # 홈에서 시작한 터미널은 빈 경로라 "최근 폴더"에 넣지 않는다
        self.assertEqual(self.req("GET", "/api/folders")[1]["recent"], ["proj/a"])

    def test_folders(self):
        code, body = self.req("GET", "/api/folders")
        self.assertEqual(code, 200)
        self.assertEqual(body["path"], "")
        self.assertEqual(body["dirs"], ["Docs", "plink", "proj"])  # 숨김·홈 밖 링크·홈의 Library 제외
        self.assertEqual(body["recent"], ["proj/a"])  # 홈 밖 cwd 제외
        self.assertEqual(self.req("GET", "/api/folders?path=proj")[1]["dirs"], ["a", "b", "Library"])  # 홈 아래가 아니면 보인다
        for bad in ("..", "../x", "/etc", ".hidden", "escape", "proj/../..", "nope"):
            with self.subTest(path=bad):
                code, body = self.req("GET", "/api/folders?path=" + urllib.parse.quote(bad))
                self.assertEqual((code, body["error"]), (400, "bad_path"))

    def test_agents(self):
        code, body = self.req("POST", "/api/agents", {"path": "proj/a", "prompt": "--force 해줘"})
        self.assertEqual((code, body), (200, {"id": "t99"}))
        self.assertEqual(self.hc.run_calls, [(os.path.join(self.home, "proj", "a"), "--force 해줘")])
        self.assertEqual(self.req("POST", "/api/agents", {"path": ""})[0], 200)
        self.assertEqual(self.hc.run_calls[-1], (self.home, None))
        for body in ({"path": "escape"}, {"path": "../"}, {"path": "proj", "prompt": "\x00"}, {"path": 3}):
            with self.subTest(body=body):
                self.assertEqual(self.req("POST", "/api/agents", body)[0], 400)
        self.assertEqual(len(self.hc.run_calls), 2)

    def test_hc_run_argv(self):
        calls = []

        def runner(argv, **kw):
            calls.append((argv, kw))
            return subprocess.CompletedProcess(argv, 0, '{"address":"t5"}', "")
        h = hr.Hc("/x/hc", runner=runner)
        self.assertEqual(h.run_claude("/h/p", "-rf 해"), {"address": "t5"})
        h.run_claude("/h/p")
        h.send("t1", "-x", force=True)
        self.assertEqual(calls[0][0], ["/x/hc", "run", "--bg", "--json", "--cwd", "/h/p", "claude", "--", "-rf 해"])
        self.assertEqual(calls[1][0], ["/x/hc", "run", "--bg", "--json", "--cwd", "/h/p", "claude"])
        self.assertEqual(calls[2][0], ["/x/hc", "send", "--force", "t1", "--", "-x"])
        self.assertIs(calls[0][1]["shell"], False)


# ---------------------------------------------------------------- 카드 답: 안전한 항목 선택·잠금·화면 번호

def approval_screen(labels, cursor=0, panel=True):
    lines = ["⏺ Bash(ls -la)", "  ⎿  $ ls -la", ""]
    if panel:
        lines += ["─" * 60, " Bash command", "", "   ls -la", "   List files", ""]
    lines.append(" Do you want to proceed?")
    for k, lb in enumerate(labels):
        lines.append((" ❯ " if k == cursor else "   ") + "%d. %s" % (k + 1, lb))
    lines += ["", " Esc to cancel · Tab to amend", ""]
    return "\n".join(lines)


def confirm_screen(labels, cursor=0):
    lines = ["─" * 60, " Do you trust the files in this folder?", "", " /Users/x/proj", ""]
    for k, lb in enumerate(labels):
        lines.append((" ❯ " if k == cursor else "   ") + lb)
    lines += ["", " Enter to confirm · Esc to exit"]
    return "\n".join(lines)


def question_screen(labels, cursor=0, multi=False):
    lines = ["─" * 60, " ☐ 테스트", "", "고르세요?", ""]
    for k, lb in enumerate(labels):
        box = "[ ] " if multi and lb != "Chat about this" else ""
        lines.append(("❯ " if k == cursor else "  ") + "%d. %s%s" % (k + 1, box, lb))
    lines += ["", "Enter to select · ↑/↓ to navigate · Esc to cancel"]
    return "\n".join(lines)


class CardAnswerTests(unittest.TestCase):
    def setUp(self):
        self.app, self.hc, self.pusher = make_app()
        self.hc.add("t1", PERM, name="hrtest")

    def show(self, screen):
        self.hc.screens["t1"] = screen
        p = self.app.api_prompt("t1")["prompt"]
        self.assertIsNotNone(p)
        return p

    def answer(self, action, pid):
        return self.app.api_answer("t1", {"action": action, "prompt_id": pid})

    def err(self, fn, *a):
        with self.assertRaises(hr.ApiError) as cm:
            fn(*a)
        return cm.exception.code, cm.exception.body["error"]

    def test_approval_approve_reject_pick_safe_labels(self):
        p = self.show(approval_screen(["No", "No, and don't ask again", "Yes"]))
        self.assertEqual(p["reject_index"], 0)
        self.assertEqual(self.answer("approve", p["id"])["keys"], ["3"])
        self.assertEqual(self.answer("reject", p["id"])["keys"], ["1"])
        # 정확한 "Yes"가 없고 영구 설정만 있으면 approve는 직접 보라고(409), reject는 안전한 "No, ..."
        p = self.show(approval_screen(["Yes, and don't ask again for ls", "No, and tell Claude what to do"]))
        self.assertEqual(self.err(self.answer, "approve", p["id"]), (409, "needs_manual"))
        self.assertEqual(self.answer("reject", p["id"])["keys"], ["2"])
        # 영구 거절만 있으면 esc
        p = self.show(approval_screen(["Yes, and switch to auto mode", "No, and don't ask again", "No, never"]))
        self.assertIsNone(p["reject_index"])
        self.assertEqual(self.err(self.answer, "approve", p["id"]), (409, "needs_manual"))
        self.assertEqual(self.answer("reject", p["id"])["keys"], ["esc"])
        # "Yes"로 시작하는 일회성 항목은 approve
        p = self.show(approval_screen(["Always allow", "Yes, allow once", "No"]))
        self.assertEqual(self.answer("approve", p["id"])["keys"], ["2"])
        self.assertEqual(self.answer("reject", p["id"])["keys"], ["3"])

    def test_confirm_approve_reject_same_rules(self):
        p = self.show(confirm_screen(["Yes, proceed", "No, exit"]))
        self.assertEqual(self.answer("approve", p["id"])["keys"], ["enter"])
        self.assertEqual(self.answer("reject", p["id"])["keys"], ["down", "enter"])
        p = self.show(confirm_screen(["Yes, always trust", "No, never ask again", "Not now"]))
        self.assertIsNone(p["reject_index"])
        self.assertEqual(self.err(self.answer, "approve", p["id"]), (409, "needs_manual"))
        self.assertEqual(self.answer("reject", p["id"])["keys"], ["esc"])

    def test_approval_without_panel_needs_manual(self):
        p = self.show(approval_screen(["Yes", "No"], panel=False))
        self.assertEqual((p["kind"], p["body"]), ("approval", None))
        for action in ("approve", "reject"):
            self.assertEqual(self.err(self.answer, action, p["id"]), (409, "needs_manual"), action)
        self.assertEqual(self.err(self.app.api_prompt_answer, "t1", {"prompt_id": p["id"], "option_index": 0}),
                         (409, "needs_manual"))
        self.assertEqual(self.hc.keys_calls, [])
        self.assertEqual(hr.push_category("blocked", PERM, p), "HR_BLOCKED")
        ok = self.show(approval_screen(["Yes", "Yes, and always allow", "No"]))
        self.assertEqual(hr.push_category("blocked", PERM, ok), "HR_PERMISSION")
        ok = self.show(approval_screen(["Yes", "Yes, allow all edits during this session", "No"]))
        self.assertEqual(hr.push_category("blocked", PERM, ok), "HR_PERMISSION_ALWAYS")

    def test_plan_answer_needs_manual_but_explicit_option_ok(self):
        self.hc.screens["t1"] = fixture("claude-plan-2.1.329.txt")
        p = self.app.api_prompt("t1")["prompt"]
        for action in ("approve", "always"):
            self.assertEqual(self.err(self.answer, action, p["id"]), (409, "needs_manual"), action)
        self.assertEqual(self.hc.keys_calls, [])
        self.assertEqual(self.answer("reject", p["id"])["keys"], ["esc"])
        self.app.api_prompt_answer("t1", {"prompt_id": p["id"], "option_index": 1})
        self.assertEqual(self.hc.keys_calls[-1], ("t1", ["2"]))

    def test_option_numbers_follow_screen_not_index(self):
        p = self.show(question_screen(["봄", "Chat about this", "가을", "Type something."]))
        self.assertEqual([(o["label"], o["number"]) for o in p["options"]],
                         [("봄", 1), ("가을", 3), ("Type something.", 4)])
        self.assertEqual(hr.answer_steps(p, option_index=1), [("keys", ["3"])])
        self.assertEqual(hr.answer_steps(p, text="겨울"), [("keys", ["4"]), ("text", "겨울")])
        m = self.show(question_screen(["개", "Type something", "새", "Chat about this"], multi=True))
        self.assertEqual([(o["label"], o["number"]) for o in m["options"]], [("개", 1), ("새", 3)])
        self.assertEqual(hr.answer_steps(m, option_indices=[1]), [("keys", ["3", "right"])])
        # 9번 넘는 항목은 커서 이동: 숨긴 줄까지 화면 번호 차이만큼 내려간다
        labels = ["a%d" % k for k in range(1, 10)]
        labels[1] = "Chat about this"
        labels.append("a10")
        b = self.show(question_screen(labels))
        last = len(b["options"]) - 1
        self.assertEqual(b["options"][last]["number"], 10)
        self.assertEqual(hr.answer_steps(b, option_index=last), [("keys", ["down"] * 9 + ["enter"])])
        # 승인 카드도 number를 가진다
        a = self.show(approval_screen(["Yes", "No"]))
        self.assertEqual([o["number"] for o in a["options"]], [1, 2])

    def test_concurrent_answers_second_rechecks_screen(self):
        hc = self.hc
        hc.screens["t1"] = approval_screen(["Yes", "No"])
        pid = self.app.api_prompt("t1")["prompt"]["id"]
        real_keys = hc.keys
        import time

        def slow_keys(tid, keys):
            time.sleep(0.3)
            real_keys(tid, keys)
            hc.screens[tid] = "⏺ 실행했어.\n\n❯ \n"
            hc.set_status(tid, st("working"))
        hc.keys = slow_keys
        results = []
        start = threading.Barrier(2)

        def go(fn, *a):
            start.wait()
            try:
                results.append(("ok", fn(*a)))
            except hr.ApiError as e:
                results.append((e.code, e.body["error"]))
        ths = [threading.Thread(target=go, args=(self.answer, "approve", pid)),
               threading.Thread(target=go, args=(self.app.api_prompt_answer, "t1",
                                                 {"prompt_id": pid, "option_index": 0}))]
        for t in ths:
            t.start()
        for t in ths:
            t.join(5)
        self.assertEqual(len(hc.keys_calls), 1, hc.keys_calls)
        self.assertEqual(sorted(r[0] for r in results if r[0] != "ok"), [409])
        self.assertIn((409, "prompt_changed"), results)

    def test_concurrent_free_text_is_locked(self):
        hc = self.hc
        hc.add("t2", st("blocked", "answer: 계절", "question"))
        hc.screens["t2"] = fixture("claude-question-single-2.1.329.txt")
        pid = self.app.api_prompt("t2")["prompt"]["id"]
        import time

        def slow_send(tid, text, force=False):
            time.sleep(0.3)
            hc.send_calls.append((tid, text, force))
            hc.screens[tid] = "⏺ 받았어.\n\n❯ \n"
            hc.set_status(tid, st("working"))
        hc.send = slow_send
        results = []
        start = threading.Barrier(2)

        def go():
            start.wait()
            try:
                results.append(self.app.api_prompt_answer("t2", {"prompt_id": pid, "text": "겨울"}))
            except hr.ApiError as e:
                results.append((e.code, e.body["error"]))
        ths = [threading.Thread(target=go) for _ in range(2)]
        for t in ths:
            t.start()
        for t in ths:
            t.join(5)
        self.assertEqual(len(hc.send_calls), 1)
        self.assertEqual(len(hc.keys_calls), 1)
        self.assertIn((409, "prompt_changed"), results)


class HiddenSymlinkTests(unittest.TestCase):
    def test_symlink_into_hidden_folder_rejected(self):
        home = os.path.realpath(tempfile.mkdtemp())
        os.makedirs(os.path.join(home, ".ssh", "keys"))
        os.makedirs(os.path.join(home, "proj", ".git"))
        os.symlink(os.path.join(home, ".ssh"), os.path.join(home, "vis"))
        os.symlink(os.path.join(home, "proj", ".git"), os.path.join(home, "gitlink"))
        for rel in ("vis", "vis/keys", "gitlink"):
            with self.subTest(rel=rel):
                with self.assertRaises(hr.ApiError):
                    hr.resolve_home_path(rel, home)
        self.assertEqual(hr.resolve_home_path("proj", home), os.path.join(home, "proj"))
        app, hc, _ = make_app(home=home)
        self.assertEqual(app.api_folders("")["dirs"], ["proj"])


class DefaultRelayTests(unittest.TestCase):
    """키 없는 설치(App Store판)는 기본 중계, 키 있는 기존 설치는 APNs 직접, "off"는 끔."""

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
        cfg = hr.load_config(self.path)
        return cfg, hr.build_app(cfg)

    def test_no_key_empty_setting_uses_default_relay(self):
        for extra in ({}, {"push_relay_url": ""}):
            cfg, app = self.load(extra)
            self.assertEqual(cfg["push_relay_url"], hr.DEFAULT_RELAY_URL)
            self.assertEqual(app.push_mode(), "relay")
            self.assertIsInstance(app.pusher, hr.RelayPusher)

    def test_key_without_setting_stays_apns(self):
        cfg, app = self.load({}, key=True)
        if os.name == "nt":  # Windows는 APNs 직접 모드를 지원하지 않는다: 키 파일이 있어도 중계
            self.assertEqual(cfg["push_relay_url"], hr.DEFAULT_RELAY_URL)
            self.assertEqual(app.push_mode(), "relay")
            return
        self.assertEqual(cfg["push_relay_url"], "")
        self.assertEqual(app.push_mode(), "apns")
        self.assertNotIsInstance(app.pusher, hr.RelayPusher)

    def test_explicit_url_wins(self):
        for key in (False, True):
            cfg, app = self.load({"push_relay_url": "https://relay.example.com/"}, key=key)
            self.assertEqual(cfg["push_relay_url"], "https://relay.example.com")
            self.assertEqual(app.push_mode(), "relay")

    def test_off_disables_relay(self):
        cfg, app = self.load({"push_relay_url": "off"})
        self.assertEqual(cfg["push_relay_url"], "")
        self.assertEqual(app.push_mode(), "none")
        cfg, app = self.load({"push_relay_url": "off"}, key=True)
        self.assertEqual(app.push_mode(), "apns")

    def test_default_relay_url_is_valid(self):
        self.assertEqual(hr.check_relay_url(hr.DEFAULT_RELAY_URL), hr.DEFAULT_RELAY_URL)


if __name__ == "__main__":
    unittest.main()
