"""서버 자동 업데이트: 버전 비교, 매니페스트 검사, 해시·문법 확인, 다운그레이드·실패 버전 거부, 대기 세션 미룸,
auto_update false, 설치기 결과 파일 처리, 설치기를 서버와 분리해 띄우기."""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import sheltie as hr  # noqa: E402
import test_sheltie as base  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))


def server_text(version, extra=""):
    return 'import sys\nSERVER_VERSION = "%s"\n%s\n' % (version, extra)


def sha(b):
    return hashlib.sha256(b).hexdigest()


# RFC 8032 7.1 TEST 1 키(시험 전용). 실제 릴리스 키는 hr.UPDATE_PUBLIC_KEYS.
TEST_SECRET = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
TEST_PUB = "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"


def sign(data, secret=TEST_SECRET):
    import base64
    return base64.b64encode(hr.ed25519_sign(secret, data)) + b"\n"


class FakeFetch:
    """위치(종류, 값) → bytes. 없는 위치는 OSError."""

    def __init__(self, files):
        self.files = files
        self.calls = []

    def __call__(self, src, limit):
        self.calls.append(src[1])
        name = os.path.basename(src[1])
        if name not in self.files:
            raise OSError("404 " + src[1])
        data = self.files[name]
        if len(data) > limit:
            raise ValueError("too big")
        return data


def core(st):
    return {k: st[k] for k in ("failed", "attempted", "attempted_at")}


class UpdaterTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg_dir = os.path.join(self.tmp, "cfg")
        self.inst = os.path.join(self.tmp, "share", "sheltie")
        os.makedirs(self.cfg_dir)
        os.makedirs(self.inst)
        self.server_file = os.path.join(self.inst, "sheltie.py")
        open(self.server_file, "w").close()
        self.spawned = []
        self.busy = False
        self.fetch = FakeFetch({})
        self.now = 1000000.0

    def release(self, version="1.0.1", server=None, installer=b"#!/bin/sh\necho update\n", manifest=None,
                ps1=b"# ps1\n"):
        server = server_text(version).encode() if server is None else server
        files = {"sheltie.py": server, "install.sh": installer, "install.ps1": ps1}
        if manifest is None:
            manifest = {"version": version, "files": {k: sha(v) for k, v in files.items()}}
        files["manifest.json"] = manifest if isinstance(manifest, bytes) else json.dumps(manifest).encode()
        files["manifest.json.sig"] = sign(files["manifest.json"])
        self.fetch = FakeFetch(files)
        return files

    def resign(self, files):
        files["manifest.json.sig"] = sign(files["manifest.json"])

    def updater(self, version="1.0.0", windows=False, **cfg):
        c = {"_dir": self.cfg_dir, "update_url": "https://example.invalid/rel/manifest.json"}
        c.update(cfg)
        return hr.Updater(c, busy=lambda: self.busy, fetch=self.fetch,
                          spawn=lambda *a: self.spawned.append(a), server_file=self.server_file,
                          install_dir=self.inst, log_path=os.path.join(self.tmp, "update.log"),
                          windows=windows, version=version, rand=lambda: 0.5, wall=lambda: self.now,
                          public_keys=[TEST_PUB])

    def state(self):
        with open(os.path.join(self.cfg_dir, hr.UPDATE_STATE_FILE)) as f:
            return json.load(f)

    def write_status(self, **st):
        with open(os.path.join(self.cfg_dir, hr.UPDATE_STATUS_FILE), "w") as f:
            json.dump(st, f)


class VersionTests(unittest.TestCase):
    def test_parse_and_compare(self):
        self.assertEqual(hr.parse_version("1.2.3"), (1, 2, 3))
        self.assertLess(hr.parse_version("1.9.9"), hr.parse_version("1.10.0"))
        self.assertLess(hr.parse_version("0.9.0"), hr.parse_version("1.0.0"))
        for bad in ("1.0", "1.0.0.0", "v1.0.0", "01.0.0", "1.0.0-beta", "", None, 1, "1.0.x", " 1.0.0"):
            self.assertIsNone(hr.parse_version(bad), bad)

    def test_server_version_valid(self):
        self.assertIsNotNone(hr.parse_version(hr.SERVER_VERSION))
        with open(hr.__file__, encoding="utf-8") as f:
            m = hr.SERVER_VERSION_LINE_RE.findall(f.read())
        self.assertEqual(m, [hr.SERVER_VERSION])  # 릴리스 스크립트·설치기가 읽는 줄이 하나뿐

    def test_version_line_with_crlf(self):
        # Windows에서 저장소째 깐 서버 파일(.prev)은 CRLF일 수 있다
        self.assertEqual(hr.SERVER_VERSION_LINE_RE.search('x = 1\r\nSERVER_VERSION = "1.2.3"\r\ny = 2\r\n').group(1),
                         "1.2.3")

    def test_check_update_url(self):
        self.assertEqual(hr.check_update_url("https://github.com/a/b/manifest.json")[0], "url")
        self.assertEqual(hr.check_update_url("http://127.0.0.1:9/m.json")[0], "url")
        self.assertEqual(hr.check_update_url("http://localhost/m.json")[0], "url")
        local = os.path.join(tempfile.gettempdir(), "rel", "manifest.json")  # 이 OS의 절대 경로
        self.assertEqual(hr.check_update_url(local), ("file", local))
        for bad in ("http://example.com/m.json", "ftp://x/m.json", "rel/manifest.json", "", None,
                    "https://u:p@x/m.json", "https://x/m.json?a=1", "https://x:abc/m.json", "file:///tmp/m.json"):
            with self.assertRaises(ValueError, msg=bad):
                hr.check_update_url(bad)

    def test_sibling(self):
        self.assertEqual(hr.update_sibling(("url", "https://x/releases/latest/download/manifest.json"), "sheltie.py"),
                         ("url", "https://x/releases/latest/download/sheltie.py"))
        self.assertEqual(hr.update_sibling(("file", os.path.join("r", "manifest.json")), "install.sh"),
                         ("file", os.path.join("r", "install.sh")))


class ManifestTests(unittest.TestCase):
    def ok(self, **over):
        m = {"version": "1.2.3", "files": {"sheltie.py": "a" * 64, "install.sh": "b" * 64}}
        m.update(over)
        return json.dumps(m).encode()

    def test_good(self):
        v, files = hr.parse_manifest(self.ok())
        self.assertEqual(v, "1.2.3")
        self.assertEqual(files, {"sheltie.py": "a" * 64, "install.sh": "b" * 64})

    def test_unknown_files_dropped(self):
        _, files = hr.parse_manifest(self.ok(files={"sheltie.py": "a" * 64, "evil.sh": "c" * 64}))
        self.assertEqual(list(files), ["sheltie.py"])

    def test_bad_shapes(self):
        for bad in (b"not json", b"[]", b"\xff\xfe", self.ok(version="1.2"), self.ok(version=None),
                    self.ok(files=[]), self.ok(files={"sheltie.py": "A" * 64}), self.ok(files={"sheltie.py": "a" * 63}),
                    self.ok(files={"sheltie.py": 5}), json.dumps({"files": {}}).encode()):
            with self.assertRaises(ValueError, msg=bad):
                hr.parse_manifest(bad)


class CheckOnceTests(UpdaterTestBase):
    def test_newer_version_downloads_verifies_and_spawns(self):
        files = self.release("1.0.1")
        r = self.updater().check_once()
        self.assertEqual(r, "started")
        self.assertEqual(len(self.spawned), 1)
        installer, server, log_path, windows = self.spawned[0]
        self.assertEqual(os.path.basename(installer), "install.sh")
        self.assertFalse(windows)
        with open(server, "rb") as f:
            self.assertEqual(f.read(), files["sheltie.py"])
        with open(installer, "rb") as f:
            self.assertEqual(f.read(), files["install.sh"])
        self.assertEqual(os.path.dirname(server), os.path.join(self.inst, "update", "1.0.1"))
        self.assertEqual(self.state()["attempted"], "1.0.1")
        self.assertEqual(self.state()["attempted_at"], self.now)
        self.assertEqual(self.fetch.calls, ["https://example.invalid/rel/manifest.json",
                                            "https://example.invalid/rel/manifest.json.sig",
                                            "https://example.invalid/rel/sheltie.py",
                                            "https://example.invalid/rel/install.sh"])

    def test_windows_uses_ps1(self):
        self.release("1.0.1")
        self.assertEqual(self.updater(windows=True).check_once(), "started")
        self.assertEqual(os.path.basename(self.spawned[0][0]), "install.ps1")
        self.assertTrue(self.spawned[0][3])
        self.assertNotIn("https://example.invalid/rel/install.sh", self.fetch.calls)

    def test_same_version_not_downloaded(self):
        self.release("1.0.0")
        self.assertEqual(self.updater("1.0.0").check_once(), "up_to_date")
        self.assertEqual(self.spawned, [])
        self.assertEqual(len(self.fetch.calls), 2)  # 매니페스트와 서명만

    def test_downgrade_refused(self):
        self.release("1.0.9")
        self.assertEqual(self.updater("1.1.0").check_once(), "up_to_date")
        self.assertEqual(self.spawned, [])
        self.assertEqual(len(self.fetch.calls), 2)  # 매니페스트와 서명만

    def test_hash_mismatch_refused_not_marked_failed(self):
        files = self.release("1.0.1")
        files["install.sh"] = b"#!/bin/sh\nrm -rf ~\n"  # 매니페스트와 다른 파일
        self.assertEqual(self.updater().check_once(), "hash_mismatch")
        self.assertEqual(self.spawned, [])
        self.assertFalse(os.path.exists(os.path.join(self.inst, "update")))
        self.assertFalse(os.path.exists(os.path.join(self.cfg_dir, hr.UPDATE_STATE_FILE)))

    def test_server_hash_mismatch_refused(self):
        files = self.release("1.0.1")
        files["sheltie.py"] = server_text("1.0.1", "x = 1").encode()
        self.assertEqual(self.updater().check_once(), "hash_mismatch")
        self.assertEqual(self.spawned, [])

    def test_syntax_error_marked_failed(self):
        self.release("1.0.1", server=server_text("1.0.1", "def (:").encode())
        u = self.updater()
        self.assertEqual(u.check_once(), "bad_server_file")
        self.assertEqual(self.spawned, [])
        self.assertEqual(self.state()["failed"], ["1.0.1"])
        self.assertEqual(u.check_once(), "failed_version")
        self.assertEqual(self.spawned, [])

    def test_version_line_must_match_manifest(self):
        self.release("1.0.1", server=server_text("1.0.0").encode())
        self.assertEqual(self.updater().check_once(), "bad_server_file")
        self.assertEqual(self.spawned, [])
        self.assertEqual(self.state()["failed"], ["1.0.1"])

    def test_failed_version_skipped_but_newer_taken(self):
        self.release("1.0.1")
        with open(os.path.join(self.cfg_dir, hr.UPDATE_STATE_FILE), "w") as f:
            json.dump({"failed": ["1.0.1"]}, f)
        self.assertEqual(self.updater().check_once(), "failed_version")
        self.assertEqual(self.spawned, [])
        self.release("1.0.2")
        self.assertEqual(self.updater().check_once(), "started")

    def test_busy_defers_without_download(self):
        self.release("1.0.1")
        self.busy = True
        u = self.updater()
        self.assertEqual(u.check_once(), "busy")
        self.assertEqual(self.fetch.calls, [])
        self.assertEqual(self.spawned, [])
        self.assertEqual(u.next_delay("busy"), hr.UPDATE_BUSY_RETRY)

    def test_busy_after_download_does_not_spawn(self):
        self.release("1.0.1")
        calls = []

        def busy():
            calls.append(1)
            return len(calls) > 1  # 받는 동안 승인 대기가 생겼다
        u = self.updater()
        u.busy = busy
        self.assertEqual(u.check_once(), "busy")
        self.assertEqual(self.spawned, [])

    def test_auto_update_false_does_nothing(self):
        self.release("1.0.1")
        u = self.updater(auto_update=False)
        self.assertEqual(u.check_once(), "disabled")
        stop = threading.Event()
        stop.set()
        u.run(stop)
        self.assertEqual(self.fetch.calls, [])
        self.assertEqual(self.spawned, [])
        self.assertFalse(os.path.exists(os.path.join(self.inst, "update")))

    def test_not_installed_location_does_nothing(self):
        self.release("1.0.1")
        u = self.updater()
        u.install_dir = os.path.join(self.tmp, "elsewhere")
        self.assertEqual(u.check_once(), "not_installed")
        self.assertEqual(self.fetch.calls, [])

    def test_missing_installer_in_manifest(self):
        files = self.release("1.0.1")
        m = {"version": "1.0.1", "files": {"sheltie.py": sha(files["sheltie.py"])}}
        files["manifest.json"] = json.dumps(m).encode()
        self.resign(files)
        self.assertEqual(self.updater().check_once(), "bad_manifest")
        self.assertEqual(self.spawned, [])

    def test_bad_manifests(self):
        for bad in (b"<html>", json.dumps({"version": "2", "files": {}}).encode(),
                    json.dumps({"version": "1.0.1", "files": {"sheltie.py": "zz"}}).encode()):
            self.release("1.0.1", manifest=bad)
            self.assertEqual(self.updater().check_once(), "bad_manifest", bad)
        self.assertEqual(self.spawned, [])

    def test_download_errors(self):
        self.release("1.0.1")
        del self.fetch.files["manifest.json"]
        self.assertEqual(self.updater().check_once(), "download_error")
        self.release("1.0.1")
        del self.fetch.files["install.sh"]
        self.assertEqual(self.updater().check_once(), "download_error")
        self.assertEqual(self.spawned, [])

    def test_bad_url_refused(self):
        self.release("1.0.1")
        self.assertEqual(self.updater(update_url="http://example.com/manifest.json").check_once(), "bad_url")
        self.assertEqual(self.fetch.calls, [])

    def test_spawn_error_clears_attempted(self):
        self.release("1.0.1")
        u = self.updater()

        def boom(*a):
            raise OSError("no sh")
        u.spawn = boom
        self.assertEqual(u.check_once(), "spawn_error")
        self.assertIsNone(self.state()["attempted"])
        self.assertEqual(self.state()["failed"], [])

    def test_delays(self):
        u = self.updater(update_interval_sec=100, update_first_check_sec=5)
        self.assertEqual(u.first_delay, 5)
        self.assertEqual(u.next_delay("up_to_date"), 102.5)
        self.assertEqual(u.next_delay("busy"), 100)
        d = self.updater()
        self.assertEqual(d.first_delay, hr.UPDATE_FIRST_DELAY)
        self.assertAlmostEqual(d.next_delay("started"), hr.UPDATE_INTERVAL * 1.025)
        self.assertEqual(self.updater(update_interval_sec="x", update_first_check_sec=True).first_delay,
                         hr.UPDATE_FIRST_DELAY)

    def test_real_fetch_reads_local_files(self):
        rel = os.path.join(self.tmp, "rel")
        os.makedirs(rel)
        srv = server_text("1.0.1").encode()
        inst = b"#!/bin/sh\n"
        for n, d in (("sheltie.py", srv), ("install.sh", inst)):
            with open(os.path.join(rel, n), "wb") as f:
                f.write(d)
        m = json.dumps({"version": "1.0.1", "files": {"sheltie.py": sha(srv), "install.sh": sha(inst)}}).encode()
        with open(os.path.join(rel, "manifest.json"), "wb") as f:
            f.write(m)
        with open(os.path.join(rel, "manifest.json.sig"), "wb") as f:
            f.write(sign(m))
        self.fetch = hr.update_fetch
        u = self.updater(update_url=os.path.join(rel, "manifest.json"))
        self.assertEqual(u.check_once(), "started")
        with self.assertRaises(ValueError):
            hr.update_fetch(("file", os.path.join(rel, "sheltie.py")), 10)


class StatusTests(UpdaterTestBase):
    def test_rolled_back_status_marks_failed_and_is_removed(self):
        self.write_status(result="rolled_back", version="1.0.1", **{"from": "1.0.0", "reason": "health"})
        with open(os.path.join(self.cfg_dir, hr.UPDATE_STATE_FILE), "w") as f:
            json.dump({"failed": [], "attempted": "1.0.1", "attempted_at": self.now}, f)
        u = self.updater()
        with self.assertLogs("sheltie", "WARNING") as cm:
            u.process_status()
        self.assertIn("rolled_back", "\n".join(cm.output))
        self.assertEqual(core(self.state()), {"failed": ["1.0.1"], "attempted": None, "attempted_at": None})
        self.assertFalse(os.path.exists(os.path.join(self.cfg_dir, hr.UPDATE_STATUS_FILE)))
        self.release("1.0.1")
        u.fetch = self.fetch
        self.assertEqual(u.check_once(), "failed_version")
        self.assertEqual(self.spawned, [])

    def test_ok_status_not_failed(self):
        self.write_status(result="ok", version="1.0.1", **{"from": "1.0.0"})
        u = self.updater("1.0.1")
        u.process_status()
        self.assertEqual(core(u.load_state()), {"failed": [], "attempted": None, "attempted_at": None})
        self.assertEqual(u.load_state()["updated"], {"to": "1.0.1", "from": "1.0.0", "at": self.now})
        self.assertFalse(os.path.exists(os.path.join(self.cfg_dir, hr.UPDATE_STATUS_FILE)))

    def test_attempted_without_status_and_old_version_counts_then_fails(self):
        # 설치기가 결과 없이 죽고 옛 서버가 다시 떴다(재부팅일 수도 있다): 일시적 실패로 세고 3번째에 다시 받지 않는다
        for n in range(1, hr.UPDATE_TRANSIENT_TRIES + 1):
            st = self.updater("1.0.0").load_state()
            st.update(attempted="1.0.1", attempted_at=self.now)
            with open(os.path.join(self.cfg_dir, hr.UPDATE_STATE_FILE), "w") as f:
                json.dump(st, f)
            self.updater("1.0.0").process_status(startup=True)
            if n < hr.UPDATE_TRANSIENT_TRIES:
                self.assertEqual(core(self.state()), {"failed": [], "attempted": None, "attempted_at": None})
                self.assertEqual(self.state()["retries"], {"1.0.1": n})
        self.assertEqual(core(self.state()), {"failed": ["1.0.1"], "attempted": None, "attempted_at": None})
        self.assertEqual(self.state()["retries"], {})

    def test_running_installer_not_judged_until_grace(self):
        # 서버가 아직 안 내려갔는데 다음 확인이 먼저 오면(짧은 시험 간격) 도는 설치기를 실패로 치지 않는다
        with open(os.path.join(self.cfg_dir, hr.UPDATE_STATE_FILE), "w") as f:
            json.dump({"failed": [], "attempted": "1.0.1", "attempted_at": self.now - 60}, f)
        u = self.updater("1.0.0")
        u.process_status()
        self.assertEqual(self.state()["attempted"], "1.0.1")
        self.assertEqual(self.state()["failed"], [])
        self.now += hr.UPDATE_ATTEMPT_GRACE
        u.process_status()
        self.assertEqual(self.state()["retries"], {"1.0.1": 1})  # 일시적 실패 한 번
        self.assertIsNone(self.state()["attempted"])

    def test_attempted_equal_to_running_version_is_success(self):
        with open(os.path.join(self.cfg_dir, hr.UPDATE_STATE_FILE), "w") as f:
            json.dump({"failed": [], "attempted": "1.0.1", "attempted_at": self.now}, f)
        self.updater("1.0.1").process_status()
        self.assertEqual(core(self.state()), {"failed": [], "attempted": None, "attempted_at": None})
        self.assertEqual(self.state()["updated"]["to"], "1.0.1")

    def test_broken_status_and_state_files_tolerated(self):
        with open(os.path.join(self.cfg_dir, hr.UPDATE_STATUS_FILE), "w") as f:
            f.write("{oops")
        with open(os.path.join(self.cfg_dir, hr.UPDATE_STATE_FILE), "w") as f:
            f.write("[1, 2]")
        u = self.updater()
        u.process_status()
        self.assertFalse(os.path.exists(os.path.join(self.cfg_dir, hr.UPDATE_STATUS_FILE)))
        self.release("1.0.1")
        u.fetch = self.fetch
        self.assertEqual(u.check_once(), "started")

    def test_status_written_with_bom_by_powershell(self):
        with open(os.path.join(self.cfg_dir, hr.UPDATE_STATUS_FILE), "w", encoding="utf-8-sig") as f:
            json.dump({"result": "rolled_back", "version": "1.0.1", "from": "1.0.0"}, f)
        self.updater().process_status()
        self.assertEqual(self.state()["failed"], ["1.0.1"])

    def test_failure_log_tail_goes_to_server_log(self):
        with open(os.path.join(self.tmp, "update.log"), "w", encoding="utf-8") as f:
            f.write("line one\n새 서버가 응답하지 않는다\n")
        self.write_status(result="rolled_back", version="1.0.1", **{"from": "1.0.0"})
        with self.assertLogs("sheltie", "WARNING") as cm:
            self.updater().process_status()
        self.assertIn("새 서버가 응답하지 않는다", "\n".join(cm.output))

    def test_run_processes_status_then_checks(self):
        self.write_status(result="failed", version="1.0.1", **{"from": "1.0.0"})
        self.release("1.0.1")
        u = self.updater(update_first_check_sec=0.01, update_interval_sec=60)
        stop = threading.Event()
        t = threading.Thread(target=u.run, args=(stop,))
        t.start()
        deadline = time.time() + 5
        while len(self.fetch.calls) < 1 and time.time() < deadline:
            time.sleep(0.01)
        stop.set()
        t.join(5)
        self.assertEqual(self.fetch.calls[:1], ["https://example.invalid/rel/manifest.json"])
        self.assertEqual(self.spawned, [])  # 1.0.1은 실패로 기록됐다
        self.assertEqual(self.state()["failed"], ["1.0.1"])


class BusyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        cfg = dict(hr.DEFAULTS, token=base.TOKEN, _dir=self.tmp, _path=os.path.join(self.tmp, "config.json"))
        self.app = hr.App(cfg, base.FakeHc(), hr.DeviceStore(os.path.join(self.tmp, "devices.json")),
                          base.FakePusher())

    def test_idle_not_busy(self):
        self.assertFalse(self.app.update_busy())

    def test_blocked_terminal_is_busy(self):
        self.app.tracker.last["t1"] = ("blocked", "approve Bash")
        self.assertTrue(self.app.update_busy())

    def test_waiting_hook_hold_is_busy(self):
        h = hr.HookHold("claude", "PermissionRequest", "permission", "Bash", {}, {}, [{}])
        self.app.hooks.sessions["h1"] = {"holds": [h]}
        self.assertTrue(self.app.update_busy())
        h.state = "decided"
        self.assertFalse(self.app.update_busy())


class Ed25519Tests(unittest.TestCase):
    """RFC 8032 7.1 시험 벡터(TEST 1·2·3)와 잘못된 입력."""
    VECTORS = [
        ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
         "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
         "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe"
         "24655141438e7a100b"),
        ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
         "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
         "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302a"
         "eeb00d291612bb0c00"),
        ("c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
         "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025", "af82",
         "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f760984dc6594a7c15e9716ed2"
         "8dc027beceea1ec40a"),
    ]

    def test_rfc8032_vectors(self):
        for sk, pk, msg, sig in self.VECTORS:
            sk, pk, msg, sig = (bytes.fromhex(x) for x in (sk, pk, msg, sig))
            self.assertEqual(hr.ed25519_public_key(sk), pk)
            self.assertEqual(hr.ed25519_sign(sk, msg), sig)
            self.assertTrue(hr.ed25519_verify(pk, msg, sig))

    def test_rejects_tampering_and_bad_input(self):
        sk, pk, _, sig = (bytes.fromhex(x) for x in self.VECTORS[2])
        msg = bytes.fromhex("af82")
        self.assertFalse(hr.ed25519_verify(pk, msg + b"\x00", sig))
        self.assertFalse(hr.ed25519_verify(pk, b"\xaf\x83", sig))
        for i in (0, 31, 32, 63):
            bad = bytearray(sig)
            bad[i] ^= 1
            self.assertFalse(hr.ed25519_verify(pk, msg, bytes(bad)), i)
        other = bytes.fromhex(self.VECTORS[0][1])
        self.assertFalse(hr.ed25519_verify(other, msg, sig))
        # s >= q(가단성)
        s = int.from_bytes(sig[32:], "little") + hr._ED_Q
        self.assertFalse(hr.ed25519_verify(pk, msg, sig[:32] + s.to_bytes(32, "little")))
        for p_, s_ in ((pk[:31], sig), (pk, sig[:63]), ("x" * 32, sig), (pk, None), (b"\xff" * 32, sig)):
            self.assertFalse(hr.ed25519_verify(p_, msg, s_))

    def test_manifest_sig_helper(self):
        m = b'{"version": "1.0.1"}'
        s = sign(m).decode()
        self.assertTrue(hr.verify_manifest_sig(m, s, [TEST_PUB]))
        self.assertTrue(hr.verify_manifest_sig(m, s, ["zz", "00" * 31, TEST_PUB]))  # 망가진 키는 건너뛴다
        self.assertFalse(hr.verify_manifest_sig(m + b" ", s, [TEST_PUB]))  # 원문 바이트 그대로
        self.assertFalse(hr.verify_manifest_sig(m, s, [hr.UPDATE_PUBLIC_KEYS[0]]))
        self.assertFalse(hr.verify_manifest_sig(m, "not base64!", [TEST_PUB]))
        self.assertFalse(hr.verify_manifest_sig(m, "", [TEST_PUB]))
        self.assertFalse(hr.verify_manifest_sig(m, s, []))

    def test_release_public_keys_are_valid_points(self):
        self.assertGreaterEqual(len(hr.UPDATE_PUBLIC_KEYS), 1)
        for k in hr.UPDATE_PUBLIC_KEYS:
            self.assertRegex(k, r"^[0-9a-f]{64}$")
            self.assertIsNotNone(hr._ed_decompress(bytes.fromhex(k)))
            self.assertNotEqual(k, TEST_PUB)


class SignatureCheckTests(UpdaterTestBase):
    def test_missing_signature_refused_not_failed(self):
        files = self.release("1.0.1")
        del files["manifest.json.sig"]
        with self.assertLogs("sheltie", "WARNING") as cm:
            self.assertEqual(self.updater().check_once(), "bad_signature")
        self.assertIn("서명", "\n".join(cm.output))
        self.assertEqual(self.spawned, [])
        self.assertFalse(os.path.exists(os.path.join(self.cfg_dir, hr.UPDATE_STATE_FILE)))
        self.assertEqual(len(self.fetch.calls), 2)  # 파일은 받지 않았다

    def test_tampered_manifest_refused_then_fixed_release_taken(self):
        files = self.release("1.0.1")
        evil = b"#!/bin/sh\necho evil\n"
        files["install.sh"] = evil
        files["manifest.json"] = json.dumps({"version": "1.0.1", "files": {
            "sheltie.py": sha(files["sheltie.py"]), "install.sh": sha(evil)}}).encode()  # 해시는 맞지만 서명이 옛것
        with self.assertLogs("sheltie", "WARNING"):
            self.assertEqual(self.updater().check_once(), "bad_signature")
        self.assertEqual(self.spawned, [])
        self.release("1.0.1")  # 다음 날 제대로 서명된 릴리스: 영구 실패로 치지 않았으니 받는다
        self.assertEqual(self.updater().check_once(), "started")

    def test_signature_by_other_key_refused(self):
        files = self.release("1.0.1")
        files["manifest.json.sig"] = sign(files["manifest.json"], bytes.fromhex(Ed25519Tests.VECTORS[1][0]))
        with self.assertLogs("sheltie", "WARNING"):
            self.assertEqual(self.updater().check_once(), "bad_signature")
        self.assertEqual(self.spawned, [])

    def test_default_keys_are_release_keys(self):
        # 시험 키로 서명한 릴리스는 기본(실제 릴리스 키) 설정의 서버가 받지 않는다
        self.release("1.0.1")
        c = {"_dir": self.cfg_dir, "update_url": "https://example.invalid/rel/manifest.json"}
        u = hr.Updater(c, fetch=self.fetch, spawn=lambda *a: self.spawned.append(a), server_file=self.server_file,
                       install_dir=self.inst, log_path=os.path.join(self.tmp, "update.log"), windows=False,
                       version="1.0.0")
        self.assertEqual(u.public_keys, hr.UPDATE_PUBLIC_KEYS)
        with self.assertLogs("sheltie", "WARNING"):
            self.assertEqual(u.check_once(), "bad_signature")
        self.assertEqual(self.spawned, [])

    def test_signature_too_big_refused(self):
        files = self.release("1.0.1")
        files["manifest.json.sig"] = b"A" * (hr.UPDATE_MAX_SIG + 1)
        with self.assertLogs("sheltie", "WARNING"):
            self.assertEqual(self.updater().check_once(), "bad_signature")


class TransientFailureTests(UpdaterTestBase):
    def test_transient_installer_failure_retried_then_failed(self):
        self.release("1.0.1")
        for n in range(1, hr.UPDATE_TRANSIENT_TRIES + 1):
            self.write_status(result="rolled_back", version="1.0.1", transient=True, reason="health",
                              **{"from": "1.0.0"})
            with self.assertLogs("sheltie", "WARNING"):
                r = self.updater().check_once()
            if n < hr.UPDATE_TRANSIENT_TRIES:
                self.assertEqual(r, "started", n)  # 다시 받는다
                self.assertEqual(self.state()["retries"], {"1.0.1": n})
                self.assertEqual(self.state()["failed"], [])
            else:
                self.assertEqual(r, "failed_version")
        self.assertEqual(self.state()["failed"], ["1.0.1"])
        self.assertEqual(self.state()["retries"], {})
        self.assertEqual(len(self.spawned), hr.UPDATE_TRANSIENT_TRIES - 1)

    def test_explicit_installer_failure_is_permanent(self):
        self.release("1.0.1")
        self.write_status(result="failed", version="1.0.1", transient=False, reason="버전이 올라가지 않는다",
                          **{"from": "1.0.0"})
        with self.assertLogs("sheltie", "WARNING"):
            self.assertEqual(self.updater().check_once(), "failed_version")
        self.assertEqual(self.state()["failed"], ["1.0.1"])

    def test_success_clears_retries(self):
        with open(os.path.join(self.cfg_dir, hr.UPDATE_STATE_FILE), "w") as f:
            json.dump({"failed": [], "retries": {"1.0.1": 2, "1.0.5": 1}}, f)
        self.write_status(result="ok", version="1.0.1", **{"from": "1.0.0"})
        self.updater("1.0.1").process_status(startup=True)
        self.assertEqual(self.state()["retries"], {"1.0.5": 1})

    def test_bad_state_fields_tolerated(self):
        with open(os.path.join(self.cfg_dir, hr.UPDATE_STATE_FILE), "w") as f:
            json.dump({"failed": "x", "retries": {"1.0.1": "2", "bad": 1, "1.0.2": True, "1.0.3": 2},
                       "updated": {"to": "1.0.1"}, "fetch_errors": [1]}, f)
        st = self.updater().load_state()
        self.assertEqual(st["retries"], {"1.0.3": 2})
        self.assertIsNone(st["updated"])
        self.assertIsNone(st["fetch_errors"])


class FetchErrorLogTests(UpdaterTestBase):
    def boom(self, exc):
        def f(src, limit):
            raise exc
        return f

    def test_ssl_error_is_warning(self):
        import ssl
        u = self.updater()
        u.fetch = self.boom(urllib.error.URLError(ssl.SSLCertVerificationError(1, "certificate verify failed")))
        with self.assertLogs("sheltie", "WARNING") as cm:
            self.assertEqual(u.check_once(), "download_error")
        self.assertIn("인증서", "\n".join(cm.output))

    def test_other_errors_warn_once_on_second_same_day(self):
        u = self.updater()
        u.fetch = self.boom(urllib.error.URLError(OSError("connection refused")))
        with self.assertNoLogs("sheltie", "WARNING"):
            self.assertEqual(u.check_once(), "download_error")
        with self.assertLogs("sheltie", "WARNING") as cm:
            u.check_once()
        self.assertIn("오늘 2번째", "\n".join(cm.output))
        with self.assertNoLogs("sheltie", "WARNING"):
            u.check_once()
        self.now += 86400  # 다음 날은 다시 센다
        with self.assertNoLogs("sheltie", "WARNING"):
            u.check_once()
        self.assertEqual(self.state()["fetch_errors"]["n"], 1)
        self.assertEqual(self.state()["failed"], [])

    def test_file_download_error_counts_too(self):
        files = self.release("1.0.1")
        del files["install.sh"]
        u = self.updater()
        u.check_once()
        with self.assertLogs("sheltie", "WARNING"):
            self.assertEqual(u.check_once(), "download_error")


class CrashGuardTests(UpdaterTestBase):
    """업데이트 뒤 뜨자마자 죽는 릴리스: 10분 안에 3번 시작하면 .prev로 되돌리고 그 버전을 실패로 적는다."""

    def setUp(self):
        super().setUp()
        with open(self.server_file, "w") as f:
            f.write(server_text("1.0.1"))
        with open(self.server_file + ".prev", "w") as f:
            f.write(server_text("1.0.0"))

    def updated(self, to="1.0.1", at=None):
        st = self.updater(to).load_state()
        st["updated"] = {"to": to, "from": "1.0.0", "at": self.now if at is None else at}
        with open(os.path.join(self.cfg_dir, hr.UPDATE_STATE_FILE), "w") as f:
            json.dump(st, f)

    def starts_path(self):
        return os.path.join(self.cfg_dir, hr.UPDATE_STARTS_FILE)

    def test_three_starts_in_ten_minutes_roll_back(self):
        self.updated()
        self.assertFalse(self.updater("1.0.1").startup_guard())
        self.now += 60
        self.assertFalse(self.updater("1.0.1").startup_guard())
        with open(self.starts_path()) as f:
            self.assertEqual(len(json.load(f)), 2)
        self.now += 60
        with self.assertLogs("sheltie", "WARNING") as cm:
            self.assertTrue(self.updater("1.0.1").startup_guard())
        self.assertIn("이전 서버", "\n".join(cm.output))
        with open(self.server_file) as f:
            self.assertEqual(f.read(), server_text("1.0.0"))
        st = self.state()
        self.assertEqual(st["failed"], ["1.0.1"])
        self.assertIsNone(st["updated"])
        self.assertFalse(os.path.exists(self.starts_path()))
        # 되돌린 이전 서버가 다시 뜨면 아무것도 하지 않는다
        self.now += 10
        self.assertFalse(self.updater("1.0.0").startup_guard())
        with open(self.server_file) as f:
            self.assertEqual(f.read(), server_text("1.0.0"))

    def test_next_update_attempt_ends_watch(self):
        # 실제 launchd 재현에서 나온 오탐: 1.0.1로 업데이트한 뒤 1.0.2를 시도하다 되돌아오며 1.0.1이 다시 뜬 것을
        # 뜨자마자 죽는 것으로 셌다. 다음 업데이트를 띄우면 지난 감시는 끝난다.
        self.updated()
        self.assertFalse(self.updater("1.0.1").startup_guard())
        self.release("1.0.2")
        self.assertEqual(self.updater("1.0.1").check_once(), "started")
        self.assertIsNone(self.state()["updated"])
        self.assertFalse(os.path.exists(self.starts_path()))
        for _ in range(4):
            self.now += 30
            self.assertFalse(self.updater("1.0.1").startup_guard())
        self.assertEqual(self.state()["failed"], [])

    def test_prev_with_same_version_not_rolled_back(self):
        with open(self.server_file + ".prev", "w") as f:
            f.write(server_text("1.0.1"))  # 다음 업데이트 시도가 남긴 .prev(지금과 같은 버전)
        self.updated()
        for _ in range(4):
            self.assertFalse(self.updater("1.0.1").startup_guard())
        self.assertEqual(self.state()["failed"], [])

    def test_installer_ok_status_at_start_starts_watch(self):
        # 설치기가 ok를 쓰고 곧 서버가 죽었다: 시작 때 결과 파일을 읽어 지켜본다
        self.write_status(result="ok", version="1.0.1", time=self.now, **{"from": "1.0.0"})
        for _ in range(2):
            self.assertFalse(self.updater("1.0.1").startup_guard())
            self.now += 30
        with self.assertLogs("sheltie", "WARNING"):
            self.assertTrue(self.updater("1.0.1").startup_guard())

    def test_slow_restarts_not_rolled_back(self):
        self.updated()
        for _ in range(5):
            self.assertFalse(self.updater("1.0.1").startup_guard())
            self.now += hr.UPDATE_CRASH_WINDOW / 2 + 1
        with open(self.server_file) as f:
            self.assertEqual(f.read(), server_text("1.0.1"))

    def test_after_watch_period_nothing_and_record_cleared(self):
        self.updated(at=self.now - hr.UPDATE_WATCH - 1)
        for _ in range(4):
            self.assertFalse(self.updater("1.0.1").startup_guard())
        self.assertIsNone(self.state()["updated"])
        self.assertFalse(os.path.exists(self.starts_path()))

    def test_no_prev_file_no_rollback(self):
        os.unlink(self.server_file + ".prev")
        self.updated()
        for _ in range(4):
            self.assertFalse(self.updater("1.0.1").startup_guard())
        with open(self.server_file) as f:
            self.assertEqual(f.read(), server_text("1.0.1"))

    def test_not_installed_location_untouched(self):
        self.updated()
        for _ in range(4):
            u = self.updater("1.0.1")
            u.install_dir = os.path.join(self.tmp, "elsewhere")
            self.assertFalse(u.startup_guard())
        self.assertFalse(os.path.exists(self.starts_path()))

    def test_run_settles_after_watch_period(self):
        self.updated(at=self.now - hr.UPDATE_WATCH + 0.05)
        u = self.updater("1.0.1", auto_update=False)
        self.assertFalse(u.startup_guard())
        self.assertTrue(os.path.exists(self.starts_path()))
        stop = threading.Event()
        t = threading.Thread(target=u.run, args=(stop,))
        t.start()
        t.join(5)
        self.assertFalse(t.is_alive())  # 꺼져 있으면 정리 뒤 끝난다
        self.assertFalse(os.path.exists(self.starts_path()))
        self.assertIsNone(self.state()["updated"])

    def serve_with(self, guard, running=False):
        """cmd_serve를 가짜 앱·임시 포트로 돌린다. guard: startup_guard 대신, running: 같은 설정 서버가 이미 답하는가."""
        cfg = dict(hr.DEFAULTS, token=base.TOKEN, bind="127.0.0.1", port=0, _dir=self.cfg_dir,
                   _path=os.path.join(self.cfg_dir, "c.json"), push_relay_url="", fcm_relay_url="")
        calls = []
        saved = {n: getattr(hr, n) for n in ("load_config", "build_app", "hc_version_check", "make_server_retry",
                                             "sheltie_running")}
        saved_guard = hr.Updater.startup_guard
        app = hr.App(cfg, base.FakeHc(), hr.DeviceStore(os.path.join(self.tmp, "devices.json")), base.FakePusher())
        hr.load_config = lambda: cfg
        hr.build_app = lambda c: app
        hr.hc_version_check = lambda p: {}

        def msr(app_, bind, port, taken=None, **kw):
            calls.append("bind")
            if running:
                if taken():
                    raise hr.PortTaken("%s:%d" % (bind, port))
            return hr.make_server(app_, bind, 0)
        hr.make_server_retry = msr
        hr.sheltie_running = lambda c: running
        hr.Updater.startup_guard = lambda s: calls.append("guard") or guard
        try:
            return hr.cmd_serve(), calls
        finally:
            for n, v in saved.items():
                setattr(hr, n, v)
            hr.Updater.startup_guard = saved_guard

    def test_cmd_serve_exits_after_rollback(self):
        rc, calls = self.serve_with(True)
        self.assertEqual(rc, hr.UPDATE_CRASH_EXIT)
        self.assertEqual(calls, ["bind", "guard"])  # 포트를 잡은 시작만 센다
        self.assertNotEqual(hr.UPDATE_CRASH_EXIT, 0)

    def test_second_instance_exits_without_counting_start(self):
        # Windows 작업 반복 트리거가 띄운 두 번째 실행: 같은 설정의 서버가 이미 답하면 기다리지 않고 끝나고 시작으로 세지 않는다
        rc, calls = self.serve_with(True, running=True)
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [])  # 앱을 만들거나 포트를 기다리지도 않는다


class PortTakenTests(unittest.TestCase):
    def test_retry_stops_when_same_server_answers(self):
        sleeps = []
        s = socket_listen()
        self.addCleanup(s.close)
        with self.assertRaises(hr.PortTaken):
            hr.make_server_retry(None, "127.0.0.1", s.getsockname()[1], sleep=sleeps.append, taken=lambda: True)
        self.assertEqual(sleeps, [])

    def test_retry_continues_when_other_program_holds_port(self):
        sleeps = []
        s = socket_listen()
        self.addCleanup(s.close)
        with self.assertRaises(OSError):
            hr.make_server_retry(None, "127.0.0.1", s.getsockname()[1], sleep=sleeps.append, attempts=3,
                                 taken=lambda: False)
        self.assertEqual(len(sleeps), 2)

    def test_sheltie_running(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        cfg = dict(hr.DEFAULTS, token=base.TOKEN, bind="127.0.0.1", port=0, _dir=tmp,
                   _path=os.path.join(tmp, "config.json"), push_relay_url="", fcm_relay_url="")
        app = hr.App(cfg, base.FakeHc(), hr.DeviceStore(os.path.join(tmp, "devices.json")), base.FakePusher())
        srv = hr.make_server(app, "127.0.0.1", 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        cfg["port"] = srv.server_address[1]
        self.assertTrue(hr.sheltie_running(cfg))
        self.assertFalse(hr.sheltie_running(dict(cfg, token="x" * 40)))  # 다른 설치(토큰)
        s = socket_listen()
        self.addCleanup(s.close)
        self.assertFalse(hr.sheltie_running(dict(cfg, port=s.getsockname()[1]), timeout=0.5))


def socket_listen():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    return s


class HealthTests(unittest.TestCase):
    def test_health_has_server_version(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        cfg = dict(hr.DEFAULTS, token=base.TOKEN, bind="127.0.0.1", port=0, _dir=tmp,
                   _path=os.path.join(tmp, "config.json"), push_relay_url="", fcm_relay_url="")
        app = hr.App(cfg, base.FakeHc(), hr.DeviceStore(os.path.join(tmp, "devices.json")), base.FakePusher())
        srv = hr.make_server(app, "127.0.0.1", 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        req = urllib.request.Request("http://127.0.0.1:%d/api/health" % srv.server_address[1],
                                     headers={"Authorization": "Bearer " + base.TOKEN})
        with urllib.request.urlopen(req, timeout=5) as r:
            body = json.load(r)
        self.assertEqual(body["server_version"], hr.SERVER_VERSION)


class RedirectTests(unittest.TestCase):
    def redirect(self, old, new):
        h = hr._UpdateRedirect()
        req = urllib.request.Request(old)
        return h.redirect_request(req, None, 302, "Found", {}, new)

    def test_https_redirect_allowed(self):
        self.assertEqual(self.redirect("https://github.com/x", "https://objects.example/y").full_url,
                         "https://objects.example/y")

    def test_http_redirect_refused(self):
        with self.assertRaises(urllib.error.HTTPError):
            self.redirect("https://github.com/x", "http://evil.example/y")
        with self.assertRaises(urllib.error.HTTPError):
            self.redirect("http://127.0.0.1:9/x", "http://evil.example/y")


@unittest.skipIf(os.name == "nt", "맥: 새 세션(launchd가 서버 프로세스 그룹을 끝내도 살아남는다)")
class SpawnTests(unittest.TestCase):
    def test_installer_runs_in_new_session_with_src_and_log(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        inst = os.path.join(tmp, "install.sh")
        out = os.path.join(tmp, "out.txt")
        with open(inst, "w") as f:
            f.write('#!/bin/sh\necho "args=$* src=$SHELTIE_SRC"\n'
                    'python3 -c "import os; print(os.getsid(0))" > "%s"\n' % out)
        log_path = os.path.join(tmp, "logs", "update.log")
        p = hr.spawn_installer(inst, "/x/sheltie.py", log_path, windows=False)
        p.wait(10)
        with open(log_path) as f:
            self.assertEqual(f.read().strip(), "args=--update src=/x/sheltie.py")
        with open(out) as f:
            self.assertNotEqual(int(f.read()), os.getsid(0))


class WindowsSpawnTests(unittest.TestCase):
    """Windows 인자: powershell -File install.ps1 -Update, 숨은 콘솔(DETACHED_PROCESS 아님), 작업 개체 밖이 막히면 다시."""

    def test_flags_and_breakaway_fallback(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        calls = []

        def fake_popen(argv, creationflags=0, **kw):
            calls.append((argv, creationflags, kw["env"]["SHELTIE_SRC"]))
            if creationflags & hr.CREATE_BREAKAWAY_FROM_JOB:
                raise PermissionError(13, "access denied")
            return "proc"
        orig = hr.subprocess.Popen
        hr.subprocess.Popen = fake_popen
        try:
            r = hr.spawn_installer(os.path.join(tmp, "install.ps1"), "C:/x/sheltie.py",
                                   os.path.join(tmp, "update.log"), windows=True)
        finally:
            hr.subprocess.Popen = orig
        self.assertEqual(r, "proc")
        self.assertEqual(len(calls), 2)
        argv, flags, src = calls[1]
        self.assertTrue(argv[0].lower().endswith("powershell.exe"))
        self.assertEqual(argv[-3:], ["-File", os.path.join(tmp, "install.ps1"), "-Update"])
        self.assertIn("Bypass", argv)
        self.assertEqual(flags, hr.CREATE_NO_WINDOW | hr.CREATE_NEW_PROCESS_GROUP)
        self.assertFalse(flags & 0x00000008)  # DETACHED_PROCESS면 powershell 5.1이 아무것도 안 하고 끝난다
        self.assertEqual(src, "C:/x/sheltie.py")


def _load_sign_tool():
    import importlib.util
    spec = importlib.util.spec_from_file_location("sign_manifest", os.path.join(ROOT, "ops", "sign_manifest.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class SignToolTests(unittest.TestCase):
    def test_sign_bytes_verifies_with_given_keys(self):
        tool = _load_sign_tool()
        data = b'{"version": "1.0.1", "files": {}}\n'
        sig = tool.sign_bytes(data, TEST_SECRET, [TEST_PUB])
        self.assertTrue(hr.verify_manifest_sig(data, sig, [TEST_PUB]))
        self.assertEqual(sig, sign(data).decode().strip())  # 서버 시험과 같은 서명(결정적)

    def test_key_not_matching_server_keys_refused(self):
        tool = _load_sign_tool()
        with self.assertRaises(SystemExit):
            tool.sign_bytes(b"x", TEST_SECRET)  # 기본은 실제 릴리스 공개키

    def test_keychain_service_name(self):
        self.assertEqual(_load_sign_tool().KEYCHAIN_SERVICE, "sheltie-release-signing")


@unittest.skipIf(os.name == "nt", "맥 전용: sh·git·키체인")
class ReleaseScriptTests(unittest.TestCase):
    """ops/release-server.sh --dry-run: 임시 저장소(원격 포함), 가짜 gh·security(시험 키)."""

    def git(self, *a, cwd=None):
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@example.invalid", GIT_CONFIG_GLOBAL="/dev/null")
        subprocess.run(["git"] + list(a), cwd=cwd or self.repo, env=env, check=True, capture_output=True)

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "repo")
        bare = os.path.join(self.tmp, "origin.git")
        self.git("init", "-q", "--bare", bare, cwd=self.tmp)
        self.git("init", "-q", "-b", "main", self.repo, cwd=self.tmp)
        for rel in ("ops/release-server.sh", "ops/sign_manifest.py", "install.sh", "install.ps1"):
            os.makedirs(os.path.join(self.repo, os.path.dirname(rel)), exist_ok=True)
            shutil.copy(os.path.join(ROOT, rel), os.path.join(self.repo, rel))
        os.makedirs(os.path.join(self.repo, "server"))
        with open(os.path.join(ROOT, "server", "sheltie.py"), encoding="utf-8") as f:
            src = f.read()
        src = src.replace(hr.UPDATE_PUBLIC_KEYS[0], TEST_PUB)
        self.server = os.path.join(self.repo, "server", "sheltie.py")
        self.write_server(src.replace('SERVER_VERSION = "%s"' % hr.SERVER_VERSION, 'SERVER_VERSION = "1.0.0"'))
        self.git("add", ".")
        self.git("commit", "-q", "-m", "v1.0.0")
        self.git("remote", "add", "origin", bare)
        self.git("push", "-q", "origin", "main")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        os.symlink(sys.executable, os.path.join(self.bin, "python3"))
        self.fake("gh", 'echo "gh $*" >> "%s/gh.log"; exit 0' % self.tmp)
        self.fake("security", 'case "$*" in *sheltie-release-signing*-w*) echo "${KEY_HEX}";; *) exit 44;; esac')

    def write_server(self, text):
        with open(self.server, "w", encoding="utf-8") as f:
            f.write(text)

    def fake(self, name, body):
        p = os.path.join(self.bin, name)
        with open(p, "w") as f:
            f.write("#!/bin/sh\n" + body + "\n")
        os.chmod(p, 0o755)

    def release(self, ver="1.0.0", key=TEST_SECRET):
        env = {"HOME": self.tmp, "PATH": self.bin + ":/usr/bin:/bin", "KEY_HEX": key.hex(),
               "GIT_CONFIG_GLOBAL": "/dev/null", "TMPDIR": self.tmp}
        return subprocess.run(["sh", os.path.join(self.repo, "ops", "release-server.sh"), ver, "--dry-run",
                               "--skip-tests"], env=env, capture_output=True, text=True, timeout=120)

    def test_dry_run_signs_manifest_and_uploads_sig(self):
        r = self.release()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("manifest.json.sig: 서명·검증 완료", r.stdout)
        self.assertRegex(r.stdout, r"\+ gh release create server-v1\.0\.0 .*manifest\.json .*manifest\.json\.sig")
        self.assertNotIn(TEST_SECRET.hex(), r.stdout + r.stderr)

    def test_head_must_be_version_bump_commit(self):
        with open(os.path.join(self.repo, "notes.txt"), "w") as f:
            f.write("later\n")
        self.git("add", "notes.txt")
        self.git("commit", "-q", "-m", "later")
        self.git("push", "-q", "origin", "main")
        r = self.release()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("SERVER_VERSION을 올린 커밋", r.stderr)

    def test_key_not_matching_server_refused(self):
        r = self.release(key=bytes.fromhex(Ed25519Tests.VECTORS[1][0]))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("검증되지 않는다", r.stderr)
        self.assertNotIn("gh release create", r.stdout)


class InstallerContractTests(unittest.TestCase):
    """서버·두 설치기가 같은 이름과 버전 줄을 쓴다."""

    def read(self, name):
        with open(os.path.join(ROOT, name), encoding="utf-8") as f:
            return f.read()

    def test_status_file_name_in_both_installers(self):
        for name in ("install.sh", "install.ps1"):
            self.assertIn(hr.UPDATE_STATUS_FILE, self.read(name), name)

    def test_installers_read_version_line_like_server(self):
        # 설치기는 SERVER_VERSION 줄로 새 버전을 알아낸다: 같은 정규식 문자열을 쓴다
        sh = self.read("install.sh")
        ps = self.read("install.ps1")
        for t in (sh, ps):
            self.assertIn("SERVER_VERSION = ", t)

    def test_manifest_url_matches_release_script(self):
        rel = self.read(os.path.join("ops", "release-server.sh"))
        self.assertIn("hyperfocuslab/sheltie", rel)
        self.assertTrue(hr.UPDATE_MANIFEST_URL.startswith("https://github.com/hyperfocuslab/sheltie/releases/latest/"))
        self.assertIn("manifest.json", rel)
        self.assertTrue(re.search(r"server-v", rel))


if __name__ == "__main__":
    unittest.main()
