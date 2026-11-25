"""훅 세션 글 보내기(깨우기): POST /hook/claude/wait 대기자, /api/terminals/<훅 id>/send 줄, hook-wait 하위 명령,
대화 보기의 폰 글 표시. 계획: docs/plans/desktop-send.md(위험한 입력 5개)."""
import io
import json
import os
import socket
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_hook_backend as hb  # noqa: E402

hr = hb.hr
TOKEN = hb.TOKEN
load = hb.load
wait_for = hb.wait_for
SID = "d43c1295-83b6-4d1a-aec5-887cb1b3a97b"
TID = "h887cb1b3a97b"


class WakeBase(hb.HookBase):
    def setUp(self):
        super().setUp()
        self.app.hook_send_wait = 0.5

    def tearDown(self):
        for s in list(self.app.hooks.sessions.values()):
            self.app.hooks._end_waiter(s, "test_end")
        super().tearDown()

    def stop_body(self, **over):
        return load("claude-stop-2.1.329.json", **over)

    def wait_bg(self, body=None):
        """hook-wait 요청을 스레드로. box["r"]에 (code, body), 응답 없이 끊기면 box["err"]."""
        box = {}

        def run():
            try:
                box["r"] = self.req("POST", "/hook/claude/wait", body or self.stop_body())
            except Exception as e:  # noqa: BLE001
                box["err"] = e
        th = threading.Thread(target=run, daemon=True)
        th.start()
        box["th"] = th
        return box

    def attached(self, tid=TID):
        return wait_for(lambda: self.app.hooks.has(tid) and self.app.hooks.sessions[tid]["waiter"])

    def send(self, text, tid=TID):
        return self.req("POST", "/api/terminals/%s/send" % tid, {"text": text})

    def done(self, box, timeout=5.0):
        box["th"].join(timeout)
        self.assertFalse(box["th"].is_alive(), "wait still held")
        return box.get("r")

    def caps(self, tid=TID):
        return {t["id"]: t for t in self.req("GET", "/api/terminals")[1]["terminals"]}[tid]["caps"]


class WakeServerTests(WakeBase):
    def test_no_waiter_ever_is_unsupported_and_caps_empty(self):
        self.hook("claude", load("claude-session-start-2.1.329.json"))
        self.assertEqual(self.caps(), [])
        self.assertEqual(self.send("hi"), (409, {"error": "unsupported"}))
        self.assertEqual(self.app.hooks.pending(TID), [])

    def test_caps_send_after_first_waiter(self):
        self.hook("claude", load("claude-session-start-2.1.329.json"))
        self.hook("claude", self.stop_body())
        box = self.wait_bg()
        self.attached()
        self.assertEqual(self.caps(), ["send"])
        self.assertEqual(self.caps("t1"), ["read", "keys", "send"])  # hc 터미널은 그대로
        self.app.hooks.end_waiter(TID, "x")
        self.done(box)
        self.assertEqual(self.caps(), ["send"])  # 한 번 붙었으면 대기자가 없어도 보낼 수 있다(줄에 둔다)

    def test_idle_waiter_gets_text_immediately(self):
        self.hook("claude", self.stop_body())
        box = self.wait_bg()
        self.attached()
        self.assertEqual(self.send("이어서 해"), (200, {"ok": True, "queued": False}))
        self.assertEqual(self.done(box), (200, {"text": "이어서 해"}))
        self.assertEqual(self.status(TID)["state"], "working")  # 깨어나 턴을 시작한다
        self.assertEqual(self.app.hooks.pending(TID), [])

    def test_risky1_send_without_waiter_queued_then_delivered_after_stop(self):
        # 대기자가 붙은 적은 있지만 지금은 없다(시간 초과·턴 진행 중) → 줄에 두고 다음 Stop 뒤 대기자에게
        self.hook("claude", self.stop_body())
        box = self.wait_bg()
        self.attached()
        self.hook("claude", load("claude-user-prompt-submit-2.1.329.json"))  # PC에서 새 프롬프트: 대기자 끝
        self.assertEqual(self.done(box), (200, {"end": "new_turn"}))
        self.assertEqual(self.send("하나"), (200, {"ok": True, "queued": True}))
        self.assertEqual(self.send("둘"), (200, {"ok": True, "queued": True}))
        self.assertEqual(self.app.hooks.pending(TID), ["하나", "둘"])
        # Stop의 비동기 hook-wait가 동기 Stop 이벤트보다 먼저 와도(working) Stop 전에는 주지 않는다
        box = self.wait_bg()
        self.attached()
        time.sleep(0.5)
        self.assertTrue(box["th"].is_alive())
        self.hook("claude", self.stop_body())
        self.assertEqual(self.done(box), (200, {"text": "하나"}))
        self.assertEqual(self.app.hooks.pending(TID), ["둘"])
        # 깨어난 턴이 끝나면(Stop) 다음 대기자가 남은 글을 받는다
        self.hook("claude", self.stop_body(prompt_id="p2"))
        self.assertEqual(self.done(self.wait_bg()), (200, {"text": "둘"}))
        self.assertEqual(self.app.hooks.pending(TID), [])

    def test_working_holds_delivery_until_stop(self):
        self.hook("claude", self.stop_body())
        box = self.wait_bg()
        self.attached()
        self.app.hooks.set_base(TID, "working")  # 다른 경로로 작업 중이 됐다
        self.assertEqual(self.send("기다려"), (200, {"ok": True, "queued": True}))
        time.sleep(0.4)
        self.assertTrue(box["th"].is_alive())
        self.hook("claude", self.stop_body(prompt_id="p9"))
        self.assertEqual(self.done(box), (200, {"text": "기다려"}))

    def test_held_permission_blocks_delivery(self):
        self.hook("claude", self.stop_body())
        box = self.wait_bg()
        self.attached()
        perm = hb.load("claude-permission-bash-2.1.329.json", session_id=SID)
        pbox = self.hook_bg("claude", perm)
        p = self.blocked(TID)
        self.assertEqual(self.send("x"), (200, {"ok": True, "queued": True}))
        self.assertTrue(box["th"].is_alive())
        self.answer(TID, {"action": "reject", "prompt_id": p["id"]})
        self.result(pbox)
        self.app.hooks.set_base(TID, "done")
        self.assertEqual(self.done(box), (200, {"text": "x"}))

    def test_risky2_two_waiters_old_ends_text_once(self):
        self.hook("claude", self.stop_body())
        a = self.wait_bg()
        wa = self.attached()
        b = self.wait_bg()
        wait_for(lambda: self.app.hooks.sessions[TID]["waiter"] not in (None, wa))
        self.assertEqual(self.done(a), (200, {"end": "superseded"}))
        self.assertEqual(self.send("한 번만"), (200, {"ok": True, "queued": False}))
        self.assertEqual(self.done(b), (200, {"text": "한 번만"}))
        self.assertEqual(self.app.hooks.pending(TID), [])

    def stamped(self, started, pid):
        return self.stop_body(sheltie_wait={"started": started, "pid": pid})

    def test_old_process_reconnect_cannot_push_out_newer(self):
        self.hook("claude", self.stop_body())
        old = self.wait_bg(self.stamped(1000.0, 11))
        w_old = self.attached()
        new = self.wait_bg(self.stamped(2000.0, 22))  # 다음 Stop에서 뜬 프로세스
        wait_for(lambda: self.app.hooks.sessions[TID]["waiter"] not in (None, w_old))
        self.assertEqual(self.done(old), (200, {"end": "superseded"}))
        w_new = self.app.hooks.sessions[TID]["waiter"]
        # 옛 프로세스가 재시도로 다시 붙으려 하면 바로 돌려보내고 새 대기자는 그대로
        self.assertEqual(self.req("POST", "/hook/claude/wait", self.stamped(1000.0, 11)), (200, {"end": "superseded"}))
        self.assertIs(self.app.hooks.sessions[TID]["waiter"], w_new)
        self.assertIsNone(w_new.end)
        self.assertEqual(self.send("새 것에게만"), (200, {"ok": True, "queued": False}))
        self.assertEqual(self.done(new), (200, {"text": "새 것에게만"}))
        self.assertEqual(self.app.hooks.pending(TID), [])
        # 대기자가 없는 동안에도 옛 프로세스는 받지 않는다(글은 줄에 남아 새 프로세스를 기다린다)
        self.app.hooks.set_base(TID, "done")
        self.app.hook_send_wait = 0.2
        self.assertEqual(self.send("다음")[1], {"ok": True, "queued": True})
        self.assertEqual(self.req("POST", "/hook/claude/wait", self.stamped(1000.0, 11)), (200, {"end": "superseded"}))
        self.assertEqual(self.app.hooks.pending(TID), ["다음"])

    def test_same_process_reconnect_allowed(self):
        now = [100.0]
        self.app.hook_clock = lambda: now[0]
        self.hook("claude", self.stop_body())
        a = self.wait_bg(self.stamped(5000.0, 7))
        self.attached()
        now[0] += hr.HOOK_WAIT_HOLD_SEC + 1
        self.assertEqual(self.done(a), (200, {"again": True}))
        b = self.wait_bg(self.stamped(5000.0, 7))  # 같은 started·pid: 재접속
        self.attached()
        self.send("재접속")
        self.assertEqual(self.done(b), (200, {"text": "재접속"}))
        # 같은 시각이라도 pid가 다르면 다른 프로세스: 더 늦은 것으로 보지 않는다
        self.assertEqual(self.req("POST", "/hook/claude/wait", self.stamped(5000.0, 8)), (200, {"end": "superseded"}))

    def test_bad_or_missing_stamp_is_accepted(self):
        self.assertIsNone(hr.hook_wait_stamp({}))
        for bad in ({"started": "x", "pid": 1}, {"started": True, "pid": 1}, {"started": float("nan"), "pid": 1},
                    {"started": 1.0, "pid": "1"}, {"started": 1.0}, "x"):
            self.assertIsNone(hr.hook_wait_stamp({"sheltie_wait": bad}), bad)
        self.assertEqual(hr.hook_wait_stamp({"sheltie_wait": {"started": 3, "pid": 4}}), (3.0, 4))

    def raw_wait(self, body=None):
        data = json.dumps(body or self.stop_body()).encode()
        s = socket.create_connection(("127.0.0.1", self.port))
        s.sendall(b"POST /hook/claude/wait HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer %s\r\n"
                  b"Content-Type: application/json\r\nContent-Length: %d\r\n\r\n" % (TOKEN.encode(), len(data)) + data)
        return s

    def test_risky3_client_gone_at_delivery_requeued_and_delivered_once(self):
        self.hook("claude", self.stop_body())
        s = self.raw_wait()
        self.attached()
        self.app.hooks.set_base(TID, "working")  # 전달을 잠시 막아 둔다
        self.assertEqual(self.send("잃지 마"), (200, {"ok": True, "queued": True}))
        s.close()  # 훅 프로세스가 죽었다
        self.app.hooks.set_base(TID, "done")
        wait_for(lambda: self.app.hooks.sessions[TID]["waiter"] is None, timeout=3)
        self.assertEqual(self.app.hooks.pending(TID), ["잃지 마"])
        self.assertEqual(self.done(self.wait_bg()), (200, {"text": "잃지 마"}))
        self.assertEqual(self.app.hooks.pending(TID), [])

    def test_risky3_write_failure_requeues(self):
        self.hook("claude", self.stop_body())
        hooks = self.app.hooks  # 직접 부른다: write가 실패하면 줄 맨 앞으로
        self.app.hooks.sessions[TID]["wake_ready"] = True
        ev = self.app.hooks.enqueue(TID, "첫째")
        self.app.hooks.enqueue(TID, "둘째")

        def boom(obj):
            raise BrokenPipeError()
        res = self.app.hook_wait("claude", self.stop_body(), lambda: True, boom)
        self.assertIs(res, hr.HOOK_NO_REPLY)
        self.assertFalse(ev.is_set())
        self.assertEqual(hooks.pending(TID), ["첫째", "둘째"])
        self.assertEqual(self.status(TID)["state"], "done")  # 전달 안 됐으니 작업 중으로 바꾸지 않는다
        got = []
        self.assertIs(self.app.hook_wait("claude", self.stop_body(), lambda: True, got.append), hr.HOOK_WRITTEN)
        self.assertEqual(got, [{"text": "첫째"}])
        self.assertTrue(ev.is_set())

    def test_risky3_gone_between_take_and_write_requeues(self):
        self.hook("claude", self.stop_body())
        self.app.hooks.sessions[TID]["wake_ready"] = True
        self.app.hooks.enqueue(TID, "하나")
        alive = iter([True, False])  # 루프 확인 때는 붙어 있었고, 쓰기 직전에 끊겼다
        wrote = []
        res = self.app.hook_wait("claude", self.stop_body(), lambda: next(alive), wrote.append)
        self.assertIs(res, hr.HOOK_NO_REPLY)
        self.assertEqual((wrote, self.app.hooks.pending(TID)), ([], ["하나"]))

    def test_risky4_long_and_control_text_rejected(self):
        self.hook("claude", self.stop_body())
        box = self.wait_bg()
        self.attached()
        for bad in ("x" * (hr.MAX_TEXT + 1), "a\x1bb", "a\x00", "", 5):
            with self.subTest(bad=repr(bad)[:20]):
                self.assertEqual(self.send(bad), (400, {"error": "bad_text"}))
        self.assertEqual(self.app.hooks.pending(TID), [])
        self.assertTrue(box["th"].is_alive())
        self.assertEqual(self.send("줄\n바꿈\t탭"), (200, {"ok": True, "queued": False}))
        self.assertEqual(self.done(box), (200, {"text": "줄\n바꿈\t탭"}))

    def test_queue_cap(self):
        self.hook("claude", load("claude-user-prompt-submit-2.1.329.json"))
        self.app.hooks.sessions[TID]["wake_ready"] = True
        self.app.hook_send_wait = 0
        for i in range(hr.HOOK_INBOX_MAX):
            self.assertEqual(self.send("m%d" % i)[0], 200)
        self.assertEqual(self.send("넘침"), (409, {"error": "queue_full"}))

    def test_session_end_ends_waiter_and_drops_queue(self):
        self.hook("claude", self.stop_body())
        box = self.wait_bg()
        self.attached()
        self.app.hooks.set_base(TID, "working")
        self.send("버려짐")
        self.hook("claude", load("claude-session-end-2.1.329.json"))
        self.assertEqual(self.done(box), (200, {"end": "session_end"}))
        self.assertFalse(self.app.hooks.has(TID))

    def test_prune_ends_waiter(self):
        self.hook("claude", self.stop_body())
        box = self.wait_bg()
        self.attached()
        self.app.hooks.wall = lambda: time.time() + hr.HOOK_STALE_SEC + 10
        self.app.hooks.prune()
        self.assertEqual(self.done(box), (200, {"end": "session_end"}))

    def test_hold_limit_returns_again(self):
        now = [100.0]
        self.app.hook_clock = lambda: now[0]
        self.hook("claude", self.stop_body())
        box = self.wait_bg()
        self.attached()
        now[0] += hr.HOOK_WAIT_HOLD_SEC + 1
        self.assertEqual(self.done(box), (200, {"again": True}))
        self.assertIsNone(self.app.hooks.sessions[TID]["waiter"])

    def test_other_agents_and_guards(self):
        self.assertEqual(self.req("POST", "/hook/codex/wait", load("codex-stop-doc.json")), (200, {"end": "unsupported"}))
        self.assertEqual(self.req("POST", "/hook/gemini/wait", self.stop_body())[0], 404)
        self.assertEqual(self.req("POST", "/hook/claude/wait", self.stop_body(), token="bad")[0], 401)
        self.assertEqual(self.req("POST", "/hook/claude/wait", self.stop_body(), headers={"X-Forwarded-For": "1"}),
                         (403, {"error": "forbidden"}))
        self.assertEqual(self.req("POST", "/hook/claude/wait", {"hook_event_name": "Stop"})[0], 400)
        self.assertEqual(self.req("POST", "/hook/claude/wait", dict(self.stop_body(), workspaceRoot="/w")),
                         (200, {"end": "foreign"}))
        self.assertEqual(self.app.hooks.sessions, {})
        # Codex 훅 세션에는 보낼 수 없다
        self.hook("codex", load("codex-user-prompt-submit-0.171.0.json"))
        ctid = self.tid_of(load("codex-user-prompt-submit-0.171.0.json"))
        self.assertEqual(self.send("hi", ctid), (409, {"error": "unsupported"}))

    def test_hc_send_unchanged(self):
        self.assertEqual(self.send("hello", "t1"), (200, {"ok": True}))
        self.assertEqual(self.hc.send_calls, [("t1", "hello")])


class TranscriptWakeTests(unittest.TestCase):
    def user(self, content):
        return json.dumps({"type": "user", "message": {"role": "user", "content": content}})

    def test_wake_block_shown_as_user_text(self):
        real = ("<task-notification>\n<summary>Stop hook feedback</summary>\n</task-notification>\n<system-reminder>\n"
                "Stop hook blocking error from command \"Stop\": %s\n이어서 해\n둘째 줄\n\n</system-reminder>"
                % hr.HOOK_WAKE_MARK)
        other = ("<task-notification>\n<summary>Stop hook feedback</summary>\n</task-notification>\n<system-reminder>\n"
                 "Stop hook blocking error from command \"lint.sh\": lint failed\n</system-reminder>")
        mixed = "<system-reminder>\nsomething internal\n</system-reminder>\n안녕"
        turns, _ = hr.parse_transcript([self.user(real), self.user(other), self.user(mixed)], 50)
        self.assertEqual([t["parts"] for t in turns],
                         [[{"kind": "text", "text": "이어서 해\n둘째 줄"}], [{"kind": "text", "text": "안녕"}]])
        # 내용이 리스트인 줄도 같다
        turns, _ = hr.parse_transcript([self.user([{"type": "text", "text": real}])], 50)
        self.assertEqual(turns[0]["parts"], [{"kind": "text", "text": "이어서 해\n둘째 줄"}])

    def test_full_command_string_in_header(self):
        t = ('<system-reminder>\nStop hook blocking error from command "\\"/usr/bin/python3\\" \\"/x/sheltie.py\\" '
             'hook-wait claude": %s\nhi\n</system-reminder>' % hr.HOOK_WAKE_MARK)
        self.assertEqual(hr._user_text(t), "hi")


class HookWaitCommandTests(WakeBase):
    def setUp(self):
        super().setUp()
        self.cfg = os.path.join(self.tmp, "config.json")
        self.write_cfg(self.port)

    def write_cfg(self, port):
        with open(self.cfg, "w") as f:
            json.dump({"token": TOKEN, "port": port, "bind": "127.0.0.1", "push_relay_url": "off",
                       "hc_path": ""}, f)

    def run_cmd(self, body=None, env=None, args=("claude",), **kw):
        err = io.BytesIO()
        with mock.patch.dict(os.environ, {"SHELTIE_CONFIG": self.cfg}), \
                mock.patch.object(sys, "stdout", io.StringIO()) as out:
            rc = hr.cmd_hook_wait(list(args), stdin=io.BytesIO(json.dumps(body or self.stop_body()).encode()),
                                  stderr=err, env=env if env is not None else {}, **kw)
        self.assertEqual(out.getvalue(), "")  # stdout에는 아무것도 쓰지 않는다
        return rc, err.getvalue()

    def cmd_bg(self, **kw):
        box = {}
        th = threading.Thread(target=lambda: box.update(r=self.run_cmd(**kw)), daemon=True)
        th.start()
        box["th"] = th
        return box

    def test_risky5_inside_herdr_exits_0_without_attaching(self):
        rc, err = self.run_cmd(env={"HC_TERMINAL_ID": "t3"})
        self.assertEqual((rc, err), (0, b""))
        self.assertEqual(self.app.hooks.sessions, {})
        rc, err = self.run_cmd(env={"GROK_SESSION_ID": "x"})
        self.assertEqual((rc, err), (0, b""))
        self.assertEqual(self.run_cmd(args=("codex",)), (0, b""))
        self.assertEqual(self.app.hooks.sessions, {})

    def test_text_writes_marker_and_exits_2(self):
        self.hook("claude", self.stop_body())
        box = self.cmd_bg()
        self.attached()
        self.assertEqual(self.send("폰에서 온 글\n두 줄")[1], {"ok": True, "queued": False})
        box["th"].join(5)
        self.assertEqual(box["r"], (2, ("%s\n폰에서 온 글\n두 줄\n" % hr.HOOK_WAKE_MARK).encode("utf-8")))

    def test_client_sends_stamp_once_per_process(self):
        self.app.hook_wait_sec = 0.3
        seen = []
        real = self.app.hook_wait

        def spy(agent, body, *a, **kw):
            seen.append(body.get("sheltie_wait"))
            return real(agent, body, *a, **kw)
        self.app.hook_wait = spy
        self.hook("claude", self.stop_body())
        box = self.cmd_bg(started=1234.5)
        wait_for(lambda: len(seen) >= 2)  # again 뒤 재접속
        self.send("끝")
        box["th"].join(5)
        self.assertEqual(box["r"][0], 2)
        self.assertEqual({json.dumps(x, sort_keys=True) for x in seen},
                         {json.dumps({"pid": os.getpid(), "started": 1234.5}, sort_keys=True)})

    def test_old_client_process_exits_0_when_newer_exists(self):
        self.hook("claude", self.stop_body())
        new = self.cmd_bg(started=2000.0)
        self.attached()
        self.assertEqual(self.run_cmd(started=1000.0), (0, b""))
        self.assertTrue(new["th"].is_alive())
        self.send("새 것")
        new["th"].join(5)
        self.assertEqual(new["r"][0], 2)

    def test_end_exits_0_quietly(self):
        self.hook("claude", self.stop_body())
        box = self.cmd_bg()
        self.attached()
        self.hook("claude", load("claude-session-end-2.1.329.json"))
        box["th"].join(5)
        self.assertEqual(box["r"], (0, b""))

    def test_again_repolls(self):
        self.app.hook_wait_sec = 0.3
        self.hook("claude", self.stop_body())
        box = self.cmd_bg()
        self.attached()
        time.sleep(0.8)  # 상한이 두 번 넘게 지났다: 다시 붙어 있어야 한다
        self.assertTrue(box["th"].is_alive())
        self.attached()
        self.send("늦게")
        box["th"].join(5)
        self.assertEqual(box["r"][0], 2)

    def test_auth_error_exits_0(self):
        with open(self.cfg, "w") as f:
            json.dump({"token": "wrong-token-0000000000", "port": self.port, "bind": "127.0.0.1"}, f)
        self.assertEqual(self.run_cmd(), (0, b""))

    def test_server_down_retries_then_exits_0(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        self.write_cfg(port)
        now = [0.0]
        sleeps = []

        def sleep(d):
            sleeps.append(d)
            now[0] += d
        rc, err = self.run_cmd(sleep=sleep, clock=lambda: now[0], down_sec=10)
        self.assertEqual((rc, err), (0, b""))
        self.assertEqual(sleeps[:4], [0.5, 1, 2, 4])
        self.assertGreaterEqual(now[0], 10)

    def test_server_comes_back_after_drop(self):
        # 첫 시도는 연결 거부(서버 재시작 중), 다음 시도에 붙어 글을 받는다
        real = hr.http.client.HTTPConnection
        calls = []

        class conn(real):  # urllib(테스트 요청)도 이 클래스를 쓰니 hook-wait의 /wait 요청만 센다
            def request(self, method, url, *a, **kw):
                if url.endswith("/wait"):
                    calls.append(1)
                    if len(calls) == 1:
                        raise ConnectionRefusedError()
                return real.request(self, method, url, *a, **kw)
        self.hook("claude", self.stop_body())
        with mock.patch.object(hr.http.client, "HTTPConnection", conn):
            box = self.cmd_bg(sleep=lambda d: None)
            self.attached()
            self.send("돌아옴")
            box["th"].join(5)
        self.assertEqual(box["r"][0], 2)
        self.assertEqual(len(calls), 2)

    def test_main_dispatch(self):
        with mock.patch.object(hr, "cmd_hook_wait", return_value=2) as m:
            self.assertEqual(hr.main(["hook-wait", "claude"]), 2)
        m.assert_called_once_with(["claude"])


if __name__ == "__main__":
    unittest.main()
