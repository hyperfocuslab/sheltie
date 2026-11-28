"""데스크톱 앱(Code 탭) 세션 제목 → 훅 세션 이름. 파일 모양은 맥 ~/Library/Application Support/Claude/
claude-code-sessions/<계정>/<조직>/local_*.json 실측(필요한 키만)."""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_sheltie as base  # noqa: E402

hr = base.hr
CLI = "e37da3e5-96c7-4edd-b143-424626c2c906"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class DesktopTitlesTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.org = os.path.join(self.root, "acct", "org")
        os.makedirs(self.org)
        self.clock = Clock()
        self.titles = hr.DesktopTitles(self.root, ttl=15, clock=self.clock)

    def write(self, local, **meta):
        p = os.path.join(self.org, "local_%s.json" % local)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)
        st = os.stat(p)
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000 * len(os.listdir(self.org))))
        return p

    def test_title_by_cli_session_id(self):
        self.write("a", sessionId="local_a", cliSessionId=CLI, lastAssistantCliSessionId=CLI,
                   title="7950 프로젝트 작업실 확인", cwd="/Users/x/Documents/github")
        self.assertEqual(self.titles.title(CLI), "7950 프로젝트 작업실 확인")
        self.assertIsNone(self.titles.title("other"))
        self.assertIsNone(self.titles.title(None))

    def test_last_assistant_id_also_maps(self):
        self.write("a", cliSessionId="old-id", lastAssistantCliSessionId="new-id", title="이어 쓴 세션")
        self.assertEqual(self.titles.title("new-id"), "이어 쓴 세션")
        self.assertEqual(self.titles.title("old-id"), "이어 쓴 세션")

    def test_rename_followed_after_ttl(self):
        p = self.write("a", cliSessionId=CLI, title="처음")
        self.assertEqual(self.titles.title(CLI), "처음")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"cliSessionId": CLI, "title": "바뀐 제목"}, f, ensure_ascii=False)
        st = os.stat(p)
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
        self.assertEqual(self.titles.title(CLI), "처음")  # ttl 안에서는 다시 읽지 않는다
        self.clock.t += 16
        self.assertEqual(self.titles.title(CLI), "바뀐 제목")

    def test_bad_files_and_missing_root(self):
        with open(os.path.join(self.org, "local_bad.json"), "w") as f:
            f.write("{not json")
        self.write("list", cliSessionId=CLI)  # 제목 없음
        with open(os.path.join(self.org, "other.json"), "w") as f:
            json.dump({"cliSessionId": CLI, "title": "local_ 아닌 파일"}, f)
        self.assertIsNone(self.titles.title(CLI))
        self.assertIsNone(hr.DesktopTitles(os.path.join(self.root, "none")).title(CLI))

    def test_whitespace_collapsed(self):
        self.write("a", cliSessionId=CLI, title="  줄\n바꿈   제목 ")
        self.assertEqual(self.titles.title(CLI), "줄 바꿈 제목")

    def test_hook_backend_uses_title_for_claude_only(self):
        self.write("a", cliSessionId=CLI, title="사이드바 제목")
        hb = hr.HookBackend(titles=self.titles)
        tid = hr.hook_session_tid(CLI)
        hb.touch(tid, "claude", {"session_id": CLI, "cwd": "/Users/x/Documents/github"}, self.root)
        self.assertEqual(hb.get(tid)["name"], "사이드바 제목")
        self.assertEqual([r["name"] for r in hb.ls()], ["사이드바 제목"])
        tid2 = hr.hook_session_tid("zzzz-codex-1")
        hb.touch(tid2, "codex", {"session_id": CLI[:-1] + "x", "cwd": "/w/proj"}, self.root)
        self.assertEqual(hb.get(tid2)["name"], "proj")

    def test_hook_backend_falls_back_to_folder(self):
        hb = hr.HookBackend(titles=self.titles)
        tid = hr.hook_session_tid(CLI)
        hb.touch(tid, "claude", {"session_id": CLI, "cwd": "/Users/x/Documents/github"}, self.root)
        self.assertEqual(hb.get(tid)["name"], "github")

    def test_sessions_dir_per_platform(self):
        from unittest import mock
        with mock.patch.object(hr.sys, "platform", "darwin"):
            self.assertEqual(hr.desktop_sessions_dirs("/h", {}),
                             ["/h/Library/Application Support/Claude/claude-code-sessions"])
        pk = os.path.join(self.root, "local", "Packages")
        os.makedirs(os.path.join(pk, "Claude_pzs8sxrjxfjjc"))
        os.makedirs(os.path.join(pk, "Other_123"))
        with mock.patch.object(hr.sys, "platform", "win32"):
            self.assertEqual(hr.desktop_sessions_dirs("/h", {"APPDATA": "/ad", "LOCALAPPDATA": os.path.join(self.root, "local")}),
                             [os.path.join("/ad", "Claude", "claude-code-sessions"),
                              os.path.join(pk, "Claude_pzs8sxrjxfjjc", "LocalCache", "Roaming", "Claude",
                                           "claude-code-sessions")])

    def test_several_roots(self):
        other = os.path.join(self.root, "msix")
        os.makedirs(os.path.join(other, "a", "o"))
        with open(os.path.join(other, "a", "o", "local_z.json"), "w", encoding="utf-8") as f:
            json.dump({"cliSessionId": "win-id", "title": "윈도우 세션"}, f, ensure_ascii=False)
        t = hr.DesktopTitles([os.path.join(self.root, "missing"), other], clock=self.clock)
        self.assertEqual(t.title("win-id"), "윈도우 세션")

if __name__ == "__main__":
    unittest.main()
