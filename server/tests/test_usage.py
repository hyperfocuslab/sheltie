import datetime
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
import herdr_remote as hr  # noqa: E402
from test_herdr_remote import TOKEN, make_app  # noqa: E402

hr.log.addHandler(logging.NullHandler())
hr.log.propagate = False

NOW = datetime.datetime(2026, 11, 20, 15, 0, 0).timestamp()  # 로컬 시각


def iso(days_ago, hour=12):
    d = datetime.datetime.fromtimestamp(NOW).date() - datetime.timedelta(days=days_ago)
    local = datetime.datetime.combine(d, datetime.time(hour))
    return local.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def line(model, ts, mid, rid, i=0, o=0, cr=0, cw=0, typ="assistant"):
    return json.dumps({"type": typ, "timestamp": ts, "requestId": rid, "message": {
        "id": mid, "model": model, "usage": {
            "input_tokens": i, "output_tokens": o,
            "cache_read_input_tokens": cr, "cache_creation_input_tokens": cw}}})


class HomeCase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.env = mock.patch.dict(os.environ, {"HOME": self.home})
        self.env.start()
        hr._usage_file_cache.clear()
        hr._usage_limits_cache.update(at=0.0, next=0.0, value=None)
        os.makedirs(os.path.join(self.home, ".claude", "projects", "p1"))

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.home, ignore_errors=True)

    def write_log(self, name, lines, mtime=None):
        path = os.path.join(self.home, ".claude", "projects", "p1", name)
        with open(path, "w") as f:
            f.write("\n".join(lines) + "\n")
        if mtime:
            os.utime(path, (mtime, mtime))
        return path


class PriceTests(unittest.TestCase):
    def test_families(self):
        k = hr.usage_price_key
        self.assertEqual(k("claude-opus-4-5-20251101"), "opus_new")
        self.assertEqual(k("claude-opus-4-7"), "opus_new")
        self.assertEqual(k("claude-opus-4-1-20250805"), "opus_old")
        self.assertEqual(k("claude-opus-4-20250514"), "opus_old")
        self.assertEqual(k("claude-3-opus-20240229"), "opus_old")
        self.assertEqual(k("claude-sonnet-4-5-20250929"), "sonnet")
        self.assertEqual(k("claude-haiku-4-5-20251001"), "haiku_45")
        self.assertEqual(k("claude-3-5-haiku-20241022"), "haiku_35")
        self.assertEqual(k("claude-3-haiku-20240307"), "haiku_3")
        self.assertIsNone(k("gpt-5"))
        self.assertEqual(k("claude-opus-5-5"), "opus_55")
        self.assertEqual(k("claude-opus-5"), "opus_new")
        self.assertEqual(k("claude-sonnet-5-5"), "sonnet_55")
        self.assertEqual(k("claude-sonnet-5"), "sonnet_5")
        self.assertEqual(k("claude-sonnet-4-20250514"), "sonnet")
        self.assertEqual(k("claude-haiku-5-5"), "haiku_55")
        self.assertEqual(k("claude-fable-5-2"), "fable_52")
        self.assertEqual(k("claude-fable-5-1"), "fable_5")
        self.assertEqual(k("claude-mythos-5-2"), "fable_52")

    def test_cost(self):
        self.assertAlmostEqual(hr.usage_cost("claude-sonnet-4-5", 1_000_000, 1_000_000, 1_000_000, 1_000_000),
                               3 + 15 + 0.30 + 3.75)
        self.assertEqual(hr.usage_cost("mystery", 10, 10, 10, 10), 0.0)


class TotalsTests(HomeCase):
    def test_aggregate_dedupe_range_and_models(self):
        opus, son = "claude-opus-4-5-20251101", "claude-sonnet-4-5"
        self.write_log("a.jsonl", [
            line(opus, iso(0), "m1", "r1", i=1000, o=2000, cr=3000, cw=4000),
            line(opus, iso(0), "m1", "r1", i=1000, o=2000, cr=3000, cw=4000),   # 같은 줄: 한 번만
            line(son, iso(1), "m2", "r2", i=100, o=200),
            line("<synthetic>", iso(1), "m3", "r3", i=999, o=999),
            line(son, iso(0), "m4", "r4", i=5, typ="user"),                      # assistant 아님
            line(son, iso(10), "m5", "r5", i=7_000_000),                          # 7일 밖
            "not json {",
        ])
        # 다른 파일에 같은 message.id+requestId(세션 이어받기): 중복
        self.write_log("b.jsonl", [line(opus, iso(0), "m1", "r1", i=1000, o=2000, cr=3000, cw=4000)])
        totals, models, days = hr.usage_totals("7d", now=NOW)
        self.assertEqual([m["model"] for m in models], [opus, son])
        self.assertEqual(totals["input"], 1100)
        self.assertEqual(totals["output"], 2200)
        self.assertEqual(totals["cache_read"], 3000)
        self.assertEqual(totals["cache_write"], 4000)
        expect_opus = (1000 * 5 + 2000 * 25 + 3000 * 0.5 + 4000 * 6.25) / 1e6
        expect_son = (100 * 3 + 200 * 15) / 1e6
        self.assertAlmostEqual(models[0]["cost_usd"], expect_opus, places=6)
        self.assertAlmostEqual(totals["cost_usd"], expect_opus + expect_son, places=6)
        self.assertEqual(len(days), 7)
        self.assertEqual(days[-1]["date"], "2026-11-20")
        self.assertEqual(days[0]["date"], "2026-11-14")
        self.assertAlmostEqual(days[-1]["cost_usd"], expect_opus, places=6)
        self.assertEqual(days[-1]["tokens"], 10000)
        self.assertAlmostEqual(days[-2]["cost_usd"], expect_son, places=6)
        self.assertEqual(days[0]["tokens"], 0)

    def test_today_and_30d(self):
        self.write_log("a.jsonl", [line("claude-sonnet-4-5", iso(0), "m1", "r1", i=10),
                                   line("claude-sonnet-4-5", iso(1), "m2", "r2", i=20),
                                   line("claude-sonnet-4-5", iso(25), "m3", "r3", i=40)])
        t, _m, d = hr.usage_totals("today", now=NOW)
        self.assertEqual((t["input"], len(d)), (10, 1))
        t, _m, d = hr.usage_totals("30d", now=NOW)
        self.assertEqual((t["input"], len(d)), (70, 30))

    def test_old_file_skipped_by_mtime(self):
        old = NOW - 20 * 86400
        path = self.write_log("old.jsonl", [line("claude-sonnet-4-5", iso(0), "m1", "r1", i=10)], mtime=old)
        with mock.patch.object(hr, "usage_parse_file", wraps=hr.usage_parse_file) as pf:
            t, _m, _d = hr.usage_totals("7d", now=NOW)
        self.assertEqual(t["input"], 0)
        pf.assert_not_called()
        self.assertTrue(os.path.exists(path))

    def test_file_cache_reuses_until_changed(self):
        path = self.write_log("a.jsonl", [line("claude-sonnet-4-5", iso(0), "m1", "r1", i=10)])
        with mock.patch.object(hr, "usage_parse_file", wraps=hr.usage_parse_file) as pf:
            hr.usage_totals("7d", now=NOW)
            hr.usage_totals("30d", now=NOW)
            self.assertEqual(pf.call_count, 1)
            with open(path, "a") as f:
                f.write(line("claude-sonnet-4-5", iso(0), "m2", "r2", i=5) + "\n")
            t, _m, _d = hr.usage_totals("7d", now=NOW)
            self.assertEqual(pf.call_count, 2)
        self.assertEqual(t["input"], 15)

    def test_missing_dir(self):
        shutil.rmtree(os.path.join(self.home, ".claude"))
        t, m, d = hr.usage_totals("7d", now=NOW)
        self.assertEqual((t["cost_usd"], m, len(d)), (0.0, [], 7))


class LimitsTests(HomeCase):
    def creds(self, token="sk-ant-SECRET", expires=None, plan="max"):
        oauth = {"accessToken": token, "subscriptionType": plan}
        if expires is not None:
            oauth["expiresAt"] = expires
        with open(os.path.join(self.home, ".claude", ".credentials.json"), "w") as f:
            json.dump({"claudeAiOauth": oauth}, f)

    def keychain(self, status="missing", value=None):
        return mock.patch.object(hr, "_usage_keychain_read", return_value=(status, value))

    def test_sign_in_parsing(self):
        si = hr._usage_sign_in(json.dumps({"claudeAiOauth": {"accessToken": " t ", "expiresAt": 5, "subscriptionType": "max"}}))
        self.assertEqual(si, {"token": "t", "expires_at": 5, "plan": "max"})
        self.assertIsNone(hr._usage_sign_in("garbage"))
        self.assertIsNone(hr._usage_sign_in(json.dumps({"claudeAiOauth": {}})))

    def test_credentials_file(self):
        self.creds(expires=123)
        with self.keychain():
            si, err = hr.usage_find_credentials()
        self.assertEqual((si["token"], si["plan"], err), ("sk-ant-SECRET", "max", None))

    def test_no_credentials_and_locked(self):
        with self.keychain("missing"):
            self.assertEqual(hr.usage_find_credentials(), (None, "no_credentials"))
        with self.keychain("locked"):
            self.assertEqual(hr.usage_find_credentials(), (None, "keychain_locked"))

    def test_keychain_vs_file_later_wins(self):
        self.creds(token="file-tok", expires=2000)
        kc = json.dumps({"claudeAiOauth": {"accessToken": "kc-tok", "expiresAt": 1000}})
        with self.keychain("found", kc):
            self.assertEqual(hr.usage_find_credentials()[0]["token"], "file-tok")
        kc = json.dumps({"claudeAiOauth": {"accessToken": "kc-tok", "expiresAt": 3000}})
        with self.keychain("found", kc):
            self.assertEqual(hr.usage_find_credentials()[0]["token"], "kc-tok")
        kc = json.dumps({"claudeAiOauth": {"accessToken": "kc-tok"}})  # 만료 모름: 키체인 유지
        with self.keychain("found", kc):
            self.assertEqual(hr.usage_find_credentials()[0]["token"], "kc-tok")

    def test_parse_oauth(self):
        body = {"five_hour": {"utilization": 42.04, "resets_at": "2026-11-20T18:00:00.123Z"},
                "seven_day": {"utilization": 130, "resets_at": 1795000000},
                "seven_day_opus": {"utilization": 5, "resets_at": None},
                "seven_day_sonnet": None, "unknown": {"utilization": 1}}
        w = hr.usage_parse_oauth(body)
        self.assertEqual([(x["kind"], x["scope"]) for x in w],
                         [("session", None), ("week", None), ("week", "Opus")])
        self.assertEqual(w[0]["used_percent"], 42.0)
        self.assertEqual(w[0]["resets_at"], "2026-11-20T18:00:00Z")
        self.assertEqual(w[1]["used_percent"], 100.0)
        self.assertTrue(w[1]["resets_at"].startswith("2026-"))
        self.assertIsNone(w[2]["resets_at"])

    def test_limits_success_cache_and_no_token_leak(self):
        self.creds()
        body = {"five_hour": {"utilization": 10, "resets_at": "2026-11-20T18:00:00Z"}}
        with self.keychain(), mock.patch.object(hr, "usage_fetch_oauth", return_value=(body, None)) as f:
            a = hr.usage_limits(now=NOW)
            b = hr.usage_limits(now=NOW + 100)
            self.assertEqual(f.call_count, 1)
            self.assertEqual(f.call_args[0][0], "sk-ant-SECRET")
            hr.usage_limits(refresh=True, now=NOW + 10)   # 30초 안: 재요청 안 함
            self.assertEqual(f.call_count, 1)
            hr.usage_limits(refresh=True, now=NOW + 40)
            self.assertEqual(f.call_count, 2)
            hr.usage_limits(now=NOW + 40 + 301)
            self.assertEqual(f.call_count, 3)
        self.assertEqual(a, b)
        self.assertEqual(a["plan"], "max")
        self.assertIsNone(a["error"])
        self.assertNotIn("SECRET", json.dumps(a))

    def test_limits_failure_retries_after_60s(self):
        self.creds()
        with self.keychain(), mock.patch.object(hr, "usage_fetch_oauth", return_value=(None, "http_429")) as f:
            r = hr.usage_limits(now=NOW)
            self.assertEqual((r["error"], r["windows"]), ("http_429", []))
            hr.usage_limits(now=NOW + 30)
            self.assertEqual(f.call_count, 1)
            hr.usage_limits(now=NOW + 61)
            self.assertEqual(f.call_count, 2)

    def test_limits_expired_and_missing(self):
        self.creds(expires=1000)
        with self.keychain(), mock.patch.object(hr, "usage_fetch_oauth") as f:
            r = hr.usage_limits(now=NOW)
            f.assert_not_called()
        self.assertEqual(r["error"], "expired")
        hr._usage_limits_cache.update(value=None, next=0.0)
        os.remove(os.path.join(self.home, ".claude", ".credentials.json"))
        with self.keychain():
            self.assertEqual(hr.usage_limits(now=NOW)["error"], "no_credentials")

    def test_fetch_errors(self):
        err = urllib.error.HTTPError("u", 403, "x", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            self.assertEqual(hr.usage_fetch_oauth("t"), (None, "http_403"))
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
            self.assertEqual(hr.usage_fetch_oauth("t"), (None, "network"))


class RouteTests(HomeCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp()
        self.app, _hc, _p = make_app(self.tmp)
        self.srv = hr.make_server(self.app, "127.0.0.1", 0)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)
        super().tearDown()

    def get(self, path, token=TOKEN):
        r = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path))
        if token:
            r.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"{}")

    def test_401(self):
        self.assertEqual(self.get("/api/usage", token=None)[0], 401)
        self.assertEqual(self.get("/api/usage", token="bad")[0], 401)

    def test_200_shape_and_bad_range(self):
        self.write_log("a.jsonl", [line("claude-sonnet-4-5", iso(0), "m1", "r1", i=10, o=10)])
        with mock.patch.object(hr, "_usage_keychain_read", return_value=("missing", None)):
            code, body = self.get("/api/usage")
        self.assertEqual(code, 200)
        self.assertEqual(body["range"], "7d")
        self.assertEqual(body["limits"]["error"], "no_credentials")
        self.assertEqual(body["limits"]["windows"], [])
        self.assertEqual(len(body["by_day"]), 7)
        self.assertEqual(body["by_model"][0]["model"], "claude-sonnet-4-5")
        self.assertEqual(set(body["totals"]), {"input", "output", "cache_read", "cache_write", "cost_usd"})
        self.assertEqual(self.get("/api/usage?range=today")[1]["range"], "today")
        self.assertEqual(self.get("/api/usage?range=1y")[0], 400)


if __name__ == "__main__":
    unittest.main()
