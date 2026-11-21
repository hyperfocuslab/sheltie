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
from test_herdr_remote import TOKEN, hr, make_app, st  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
