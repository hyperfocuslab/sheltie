"""파일 보기(GET /api/files, /api/files/raw)와 첨부(POST /api/uploads)."""
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_sheltie import TOKEN, hr, make_app, st  # noqa: E402


class FilesHttpTests(unittest.TestCase):
    def setUp(self):
        self.home = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)
        for d in ("proj/sub", "proj/.git", ".ssh", "Library/Keychains", "Docs"):
            os.makedirs(os.path.join(self.home, d))
        self.write("proj/a.txt", b"hello")
        self.write("proj/B.md", b"# b")
        self.write("proj/.env", b"SECRET=1")
        self.write("proj/.git/config", b"[core]")
        self.write(".ssh/id_ed25519", b"KEY")
        self.write("Library/Keychains/login.keychain-db", b"KC")
        self.outside = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.outside, True)
        with open(os.path.join(self.outside, "passwd"), "wb") as f:
            f.write(b"root")
        os.symlink(self.outside, os.path.join(self.home, "escape"))
        os.symlink(os.path.join(self.outside, "passwd"), os.path.join(self.home, "proj", "outlink"))
        os.symlink(os.path.join(self.home, ".ssh"), os.path.join(self.home, "sshlink"))
        os.symlink(os.path.join(self.home, ".ssh", "id_ed25519"), os.path.join(self.home, "proj", "keylink"))
        os.symlink(os.path.join(self.home, "Library"), os.path.join(self.home, "lib"))
        os.symlink(os.path.join(self.home, "proj", "a.txt"), os.path.join(self.home, "Docs", "alink"))
        os.mkfifo(os.path.join(self.home, "proj", "fifo"))
        self.app, self.hc, _ = make_app(home=self.home)
        self.hc.add("t1", st("working"))
        self.hc.infos["t1"]["cwd"] = os.path.join(self.home, "proj")
        self.hc.add("t2", st("working"))
        self.hc.infos["t2"]["cwd"] = self.outside
        self.srv = hr.make_server(self.app, "127.0.0.1", 0)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def write(self, rel, data):
        with open(os.path.join(self.home, rel), "wb") as f:
            f.write(data)

    def req(self, method, path, data=None, token=TOKEN, headers=None):
        r = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), data=data, method=method)
        if token is not None:
            r.add_header("Authorization", "Bearer " + token)
        for k, v in (headers or {}).items():
            r.add_header(k, v)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.read(), e.headers

    def jreq(self, method, path, **kw):
        code, body, _ = self.req(method, path, **kw)
        return code, json.loads(body or b"{}")

    def files(self, rel):
        return self.jreq("GET", "/api/files?path=" + urllib.parse.quote(rel))

    def raw(self, rel):
        return self.req("GET", "/api/files/raw?path=" + urllib.parse.quote(rel))

    # --- 목록
    def test_list_home(self):
        code, body = self.files("")
        self.assertEqual(code, 200)
        self.assertEqual(body["path"], "")
        names = [e["name"] for e in body["entries"]]
        # 숨김, 홈의 Library, 홈 밖·숨김·Library로 가는 링크 제외. 폴더 먼저
        self.assertEqual(names, ["Docs", "proj"])
        self.assertFalse(body["truncated"])

    def test_list_folder(self):
        code, body = self.files("proj")
        self.assertEqual(code, 200)
        self.assertEqual(body["path"], "proj")
        by = {e["name"]: e for e in body["entries"]}
        self.assertEqual([e["name"] for e in body["entries"]], ["sub", "a.txt", "B.md"])  # fifo·링크(밖·숨김) 제외
        self.assertEqual((by["sub"]["dir"], by["sub"]["size"]), (True, None))
        self.assertEqual((by["a.txt"]["dir"], by["a.txt"]["size"]), (False, 5))
        self.assertIsInstance(by["a.txt"]["mtime"], int)
        # 홈 안을 가리키는 링크는 보인다
        self.assertEqual([e["name"] for e in self.files("Docs")[1]["entries"]], ["alink"])

    def test_list_file_is_not_dir(self):
        self.assertEqual(self.files("proj/a.txt"), (400, {"error": "not_dir"}))

    def test_bad_paths_rejected(self):
        bad = ("..", "../", "proj/../..", "proj/../../etc", "/etc", "/", self.outside, ".ssh", "proj/.git",
               "proj/.env", "escape", "escape/passwd", "sshlink", "Library", "library", "LIBRARY/Keychains",
               "Library/Keychains", "lib", "lib/Keychains", "nope", "proj/\x00")
        for rel in bad:
            with self.subTest(rel=rel):
                code, body = self.files(rel)
                self.assertEqual(code, 400)
                self.assertIn(body["error"], ("bad_path", "not_dir"))
        for rel in bad + ("proj/outlink", "proj/keylink", "sshlink/id_ed25519", ".ssh/id_ed25519", "proj/.env",
                          "Library/Keychains/login.keychain-db", "lib/Keychains/login.keychain-db", "",
                          "proj", "proj/fifo"):
            with self.subTest(raw=rel):
                code, body, _ = self.raw(rel)
                self.assertEqual(code, 400, rel)
                self.assertNotIn(b"KEY", body)
                self.assertNotIn(b"root", body)

    def test_raw(self):
        code, body, h = self.raw("proj/a.txt")
        self.assertEqual((code, body), (200, b"hello"))
        self.assertEqual(h["Content-Type"], "text/plain")
        self.assertEqual(h["Content-Length"], "5")
        self.assertEqual(h["X-Content-Type-Options"], "nosniff")
        code, body, _ = self.raw("Docs/alink")  # 홈 안 링크는 따라간다
        self.assertEqual((code, body), (200, b"hello"))
        self.write("proj/x.bin", b"\x00\x01")
        self.assertEqual(self.raw("proj/x.bin")[2]["Content-Type"], "application/octet-stream")

    def test_raw_too_large(self):
        with mock.patch.object(hr, "FILE_MAX", 4):
            code, body, _ = self.raw("proj/a.txt")
        self.assertEqual(code, 413)
        self.assertEqual(json.loads(body)["error"], "too_large")
        self.assertEqual(self.raw("proj/B.md")[0], 200)  # 상한 이하

    def test_raw_limit_is_32mb(self):
        self.assertEqual(hr.FILE_MAX, 32 * 1024 * 1024)
        with open(os.path.join(self.home, "proj", "big.bin"), "wb") as f:
            f.truncate(hr.FILE_MAX + 1)
        self.assertEqual(self.raw("proj/big.bin")[0], 413)

    def test_auth_required(self):
        for path in ("/api/files", "/api/files?path=proj", "/api/files/raw?path=proj/a.txt"):
            for tok in (None, "wrong"):
                with self.subTest(path=path, tok=tok):
                    code, body, _ = self.req("GET", path, token=tok)
                    self.assertEqual(code, 401)
                    self.assertNotIn(b"hello", body)
        code, _, _ = self.req("POST", "/api/uploads?name=a.txt", data=b"x", token=None)
        self.assertEqual(code, 401)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".sheltie")))

    def test_terminals_cwd_rel(self):
        code, body = self.jreq("GET", "/api/terminals")
        by = {t["id"]: t for t in body["terminals"]}
        self.assertEqual(by["t1"]["cwd_rel"], "proj")
        self.assertIsNone(by["t2"]["cwd_rel"])  # 홈 밖

    # --- 첨부
    def upload(self, name, data, **kw):
        return self.jreq("POST", "/api/uploads?name=" + urllib.parse.quote(name), data=data,
                         headers={"Content-Type": "application/octet-stream"}, **kw)

    def test_upload_saves_private_file(self):
        code, body = self.upload("스크린샷 1.png", b"PNGDATA")
        self.assertEqual(code, 200)
        path = body["path"]
        day = time.strftime("%Y-%m-%d")
        d = os.path.join(self.home, ".sheltie", "uploads", day)
        self.assertEqual(os.path.dirname(path), d)
        self.assertRegex(os.path.basename(path), r"^[0-9a-f]{8}-스크린샷_1\.png$")
        self.assertEqual(body["size"], 7)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"PNGDATA")
        for p in (os.path.join(self.home, ".sheltie"), os.path.dirname(d), d):
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o700, p)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        # 같은 이름을 다시 올려도 덮어쓰지 않는다
        self.assertNotEqual(self.upload("스크린샷 1.png", b"2")[1]["path"], path)

    def test_upload_name_sanitized(self):
        for name in ("../../.ssh/authorized_keys", "/etc/passwd", "..\\..\\evil.bat", ".bashrc", "a\x00b"):
            with self.subTest(name=name):
                code, body = self.upload(name, b"x")
                self.assertEqual(code, 200)
                p = body["path"]
                self.assertEqual(os.path.dirname(p), os.path.join(self.home, ".sheltie", "uploads",
                                                                  time.strftime("%Y-%m-%d")))
                self.assertFalse(os.path.basename(p).split("-", 1)[1].startswith("."))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".ssh", "authorized_keys")))

    def test_sanitize_upload_name(self):
        s = hr.sanitize_upload_name
        self.assertEqual(s("../../.ssh/authorized_keys"), "authorized_keys")
        self.assertEqual(s("..\\..\\evil.bat"), "evil.bat")
        self.assertEqual(s(".bashrc"), "bashrc")
        self.assertEqual(s("a b;rm -rf $(x).txt"), "a_b_rm_-rf_x_.txt")
        self.assertEqual(s("보고서 (최종).pdf"), "보고서_최종_.pdf")
        self.assertEqual(s(""), "file")
        self.assertEqual(s("..."), "file")
        self.assertEqual(s("$$$.png"), "_.png")
        self.assertEqual(s("???"), "file")
        self.assertEqual(s(None), "file")
        self.assertEqual(s("a‮gnp.exe"), "a_gnp.exe")  # 오른쪽→왼쪽 표시 문자
        long = s("x" * 300 + ".jpeg")
        self.assertEqual((len(long), long[-5:]), (hr.UPLOAD_NAME_MAX, ".jpeg"))

    def test_upload_limits(self):
        with mock.patch.object(hr, "FILE_MAX", 4):
            code, body = self.upload("a.bin", b"12345")
        self.assertEqual((code, body["error"]), (413, "too_large"))
        self.assertEqual(self.upload("a.bin", b"")[0], 400)  # 빈 파일
        d = os.path.join(self.home, ".sheltie", "uploads", time.strftime("%Y-%m-%d"))
        self.assertEqual(os.listdir(d) if os.path.isdir(d) else [], [])

    def test_upload_requires_content_length(self):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.putrequest("POST", "/api/uploads?name=a.txt")
        c.putheader("Authorization", "Bearer " + TOKEN)
        c.endheaders()
        r = c.getresponse()
        self.assertEqual((r.status, json.loads(r.read())["error"]), (411, "length_required"))
        c.close()
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.putrequest("POST", "/api/uploads?name=a.txt")
        c.putheader("Authorization", "Bearer " + TOKEN)
        c.putheader("Transfer-Encoding", "chunked")
        c.endheaders()
        c.send(b"1\r\nx\r\n0\r\n\r\n")
        r = c.getresponse()
        self.assertEqual(r.status, 400)
        c.close()

    def test_short_body_removed(self):
        with self.assertRaises(hr.ApiError) as cm:
            hr.save_upload(self.home, "a.txt", 10, io.BytesIO(b"abc"))
        self.assertEqual(cm.exception.body["error"], "short_body")
        d = os.path.join(self.home, ".sheltie", "uploads", time.strftime("%Y-%m-%d"))
        self.assertEqual(os.listdir(d), [])

    def test_upload_dir_symlink_rejected(self):
        os.symlink(self.outside, os.path.join(self.home, ".sheltie"))
        with self.assertRaises(hr.ApiError):
            hr.save_upload(self.home, "a.txt", 1, io.BytesIO(b"a"))
        self.assertEqual(sorted(os.listdir(self.outside)), ["passwd"])


    # --- 보안 리뷰 6~10
    def _raw_conn(self):
        import socket
        c = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        self.addCleanup(c.close)
        return c

    def _read_all(self, c, limit=5.0):
        import socket
        c.settimeout(limit)
        out = b""
        try:
            while True:
                b = c.recv(65536)
                if not b:
                    break
                out += b
        except socket.timeout:
            pass
        return out

    def _upload_dir(self):
        return os.path.join(self.home, ".sheltie", "uploads", time.strftime("%Y-%m-%d"))

    def test_stalled_upload_times_out_and_removes_partial(self):
        """6: 본문이 멈춘 업로드는 timeout 뒤 부분 파일을 지우고 연결을 닫는다."""
        with mock.patch.object(hr, "FILE_IO_TIMEOUT", 0.5):
            c = self._raw_conn()
            c.sendall(b"POST /api/uploads?name=a.bin HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer " + TOKEN.encode()
                      + b"\r\nContent-Length: 100\r\n\r\n" + b"x" * 10)
            deadline = time.time() + 3
            d = self._upload_dir()
            while time.time() < deadline:
                if os.path.isdir(d) and not os.listdir(d):
                    break
                time.sleep(0.05)
            self.assertTrue(os.path.isdir(d))
            self.assertEqual(os.listdir(d), [])
            data = self._read_all(c, 3)
            self.assertLessEqual(data.count(b"HTTP/1.1 "), 1)

    def test_stalled_download_times_out(self):
        """6: 받지 않는 다운로드는 timeout 뒤 연결을 끊는다(스레드가 영원히 붙잡히지 않게)."""
        import socket
        size = 24 * 1024 * 1024
        with open(os.path.join(self.home, "proj", "big.bin"), "wb") as f:
            f.write(b"\0" * size)
        with mock.patch.object(hr, "FILE_IO_TIMEOUT", 0.3):
            c = socket.socket()
            self.addCleanup(c.close)
            c.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            c.connect(("127.0.0.1", self.port))
            c.sendall(b"GET /api/files/raw?path=proj/big.bin HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer "
                      + TOKEN.encode() + b"\r\n\r\n")
            time.sleep(2)
            data = self._read_all(c, 3)
        head, _, body = data.partition(b"\r\n\r\n")
        self.assertIn(b"200", head.split(b"\r\n")[0])
        self.assertLess(len(body), size)

    def test_upload_os_error_closes_connection(self):
        """8: 업로드 중 OSError면 남은 본문이 다음 요청으로 읽히지 않게 연결을 닫는다."""
        def boom(*a, **k):
            raise OSError("disk")
        with mock.patch.object(hr, "upload_dir", boom):
            c = self._raw_conn()
            c.sendall(b"POST /api/uploads?name=a.bin HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer " + TOKEN.encode()
                      + b"\r\nContent-Length: 20\r\n\r\n" + b"GARBAGE-GARBAGE-12\r\n")
            data = self._read_all(c, 3)
        self.assertEqual(data.count(b"HTTP/1.1 "), 1, data)
        self.assertIn(b"500", data.split(b"\r\n")[0])

    def test_raw_error_after_headers_just_closes(self):
        """9: 헤더를 보낸 뒤 읽기 오류면 JSON 500을 덧붙이지 않고 연결만 닫는다."""
        self.write("proj/mid.bin", b"A" * (3 << 20))
        real = os.fdopen

        class Bad:
            def __init__(self, f):
                self.f, self.n = f, 0

            def __enter__(self):
                return self

            def __exit__(self, *a):
                self.f.close()

            def read(self, k):
                self.n += 1
                if self.n > 1:
                    raise OSError("io")
                return self.f.read(k)

        def fake_fdopen(fd, *a, **k):
            return Bad(real(fd, *a, **k))
        with mock.patch.object(hr.os, "fdopen", fake_fdopen):
            c = self._raw_conn()
            c.sendall(b"GET /api/files/raw?path=proj/mid.bin HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer "
                      + TOKEN.encode() + b"\r\n\r\n")
            data = self._read_all(c, 3)
        self.assertEqual(data.count(b"HTTP/1.1 "), 1)
        self.assertNotIn(b"internal", data)
        self.assertLess(len(data.partition(b"\r\n\r\n")[2]), 3 << 20)

    def test_prune_uploads_removes_old_day_dirs_only(self):
        """7: 7일 지난 날짜 폴더만 지운다. 링크는 따라가지 않는다."""
        base = os.path.join(self.home, ".sheltie", "uploads")
        for day in ("2026-11-01", "2026-11-13", "2026-11-14", "2026-11-21", "notes"):
            os.makedirs(os.path.join(base, day))
            with open(os.path.join(base, day, "f"), "wb") as f:
                f.write(b"x")
        # 오래된 날짜 이름의 링크: 링크만 처리하고 대상은 건드리지 않는다
        os.symlink(self.outside, os.path.join(base, "2026-10-01"))
        # 오래된 폴더 안 링크도 따라가지 않는다
        os.symlink(self.outside, os.path.join(base, "2026-11-01", "out"))
        now = time.mktime((2026, 11, 21, 12, 0, 0, 0, 0, -1))
        hr.prune_uploads(self.home, now=now)
        self.assertEqual(sorted(os.listdir(base)), ["2026-10-01", "2026-11-14", "2026-11-21", "notes"]
                         if os.path.islink(os.path.join(base, "2026-10-01")) else
                         ["2026-11-14", "2026-11-21", "notes"])
        self.assertEqual(sorted(os.listdir(self.outside)), ["passwd"])

    def test_prune_uploads_skips_symlinked_base(self):
        os.makedirs(os.path.join(self.outside, "uploads", "2020-01-01"))
        os.makedirs(os.path.join(self.home, ".sheltie"))
        os.symlink(os.path.join(self.outside, "uploads"), os.path.join(self.home, ".sheltie", "uploads"))
        hr.prune_uploads(self.home)
        self.assertTrue(os.path.isdir(os.path.join(self.outside, "uploads", "2020-01-01")))

    def test_upload_prunes_old_days(self):
        old = os.path.join(self.home, ".sheltie", "uploads", "2000-01-01")
        os.makedirs(old)
        hr._last_prune_day = None
        self.assertEqual(self.upload("a.txt", b"x")[0], 200)
        self.assertFalse(os.path.exists(old))

    def test_hidden_home_dirs_per_platform(self):
        """10: Library는 macOS에서만, AppData는 Windows에서만 막는다."""
        cases = [(False, "darwin", {"Library"}), (True, "win32", {"AppData"}), (False, "linux", set())]
        for win, plat, want in cases:
            with self.subTest(plat=plat), mock.patch.object(hr, "IS_WINDOWS", win), \
                    mock.patch.object(hr.sys, "platform", plat):
                self.assertEqual(set(hr.hidden_home_dirs()), want)


if __name__ == "__main__":
    unittest.main()
