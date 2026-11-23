import json
import logging
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
import sheltie as hr  # noqa: E402
from test_sheltie import make_app, st  # noqa: E402

hr.log.addHandler(logging.NullHandler())
hr.log.propagate = False


def asst(*parts):
    return json.dumps({"type": "assistant", "message": {"content": list(parts)}})


def text(t):
    return {"type": "text", "text": t}


class PreviewCleanTest(unittest.TestCase):
    def test_markdown_stripped(self):
        src = "# 제목\n\n**굵게** 와 `코드`\n- 항목 하나\n1. 둘\n```py\nprint(1)\n```\n[링크](http://x)"
        self.assertEqual(hr.preview_clean(src), "제목 굵게 와 코드 항목 하나 둘 print(1) 링크")

    def test_truncate(self):
        out = hr.preview_clean("가" * 500)
        self.assertEqual(len(out), hr.PREVIEW_MAX + 1)
        self.assertTrue(out.endswith("…"))
        self.assertEqual(hr.preview_clean("짧다"), "짧다")

    def test_empty_is_none(self):
        self.assertIsNone(hr.preview_clean("  \n ``` \n"))

    def test_last_assistant_text_only(self):
        lines = [asst(text("첫 응답")),
                 asst({"type": "thinking", "thinking": "생각"}, text("마지막 응답")),
                 asst({"type": "tool_use", "id": "a", "name": "Bash", "input": {}}),
                 asst({"type": "thinking", "thinking": "또 생각"})]
        self.assertEqual(hr.preview_from_lines(lines), "마지막 응답")
        self.assertIsNone(hr.preview_from_lines([asst({"type": "thinking", "thinking": "x"})]))


class PreviewCacheTest(unittest.TestCase):
    def setUp(self):
        hr._preview_by_file.clear()
        hr._preview_by_pid.clear()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "s.jsonl")

    def write(self, t):
        with open(self.path, "w") as f:
            f.write(asst(text(t)) + "\n")

    def test_cache_by_mtime_size(self):
        self.write("하나")
        with mock.patch.object(hr, "read_regular_file", wraps=hr.read_regular_file) as rd:
            self.assertEqual(hr.preview_for_path(self.path), "하나")
            self.assertEqual(hr.preview_for_path(self.path), "하나")
            self.assertEqual(rd.call_count, 1)
            self.write("둘둘")
            self.assertEqual(hr.preview_for_path(self.path), "둘둘")
            self.assertEqual(rd.call_count, 2)

    def test_session_lookup_cached_per_pid(self):
        self.write("본문")
        with mock.patch.object(hr, "find_transcript", return_value=("s", self.path)) as ft:
            self.assertEqual(hr.terminal_preview({"pid": 7}, self.tmp, now=100.0), "본문")
            hr.terminal_preview({"pid": 7}, self.tmp, now=110.0)
            self.assertEqual(ft.call_count, 1)
            hr.terminal_preview({"pid": 7}, self.tmp, now=100.0 + hr.PREVIEW_SESSION_TTL + 1)
            self.assertEqual(ft.call_count, 2)

    def test_failure_is_none(self):
        with mock.patch.object(hr, "find_transcript", side_effect=RuntimeError("x")):
            self.assertIsNone(hr.terminal_preview({"pid": 8}, self.tmp))
        self.assertIsNone(hr.preview_for_path(os.path.join(self.tmp, "none.jsonl")))


class PreviewApiTest(unittest.TestCase):
    def setUp(self):
        hr._preview_by_file.clear()
        hr._preview_by_pid.clear()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_terminals_preview_fields(self):
        app, hc, _ = make_app(self.tmp)
        hc.add("t1", st("done"))
        hc.add("t2", st("done"))
        hc.add("t3", None, agent="bash")
        path = os.path.join(self.tmp, "a.jsonl")
        with open(path, "w") as f:
            f.write(asst(text("**완료** 했어요")) + "\n")

        def fake(info, home, runner=None):
            if info["address"] == "t2":
                raise RuntimeError("boom")
            return "s", path

        with mock.patch.object(hr, "find_transcript", side_effect=fake):
            res = app.api_terminals()["terminals"]
        by = {t["id"]: t for t in res}
        self.assertEqual(by["t1"]["preview"], "완료 했어요")
        self.assertIsNone(by["t2"]["preview"])  # 실패 격리
        self.assertIsNone(by["t3"]["preview"])  # non-claude
        self.assertEqual(len(res), 3)


if __name__ == "__main__":
    unittest.main()
