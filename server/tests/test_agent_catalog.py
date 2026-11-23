import json
import logging
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
import sheltie as hr  # noqa: E402
from test_sheltie import TOKEN, make_app  # noqa: E402

hr.log.addHandler(logging.NullHandler())
hr.log.propagate = False


def touch_exe(d, name, mode=0o755):
    # Windows는 실행 비트가 없고 확장자로 찾는다(.exe 우선): 같은 이름의 .exe를 만든다
    p = os.path.join(d, name + (".exe" if os.name == "nt" else ""))
    with open(p, "w") as f:
        f.write("#!/bin/sh\n")
    os.chmod(p, mode)
    return p


def codex_cmd(catalog):
    """hc에 넘기는 첫 인자: 맥은 이름, Windows는 찾은 실행 파일의 절대 경로."""
    return catalog.executable("codex") if hr.IS_WINDOWS else "codex"


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.bin = tempfile.mkdtemp()

    def catalog(self, path=None, **kw):
        return hr.AgentCatalog(self.home, path_env=self.bin if path is None else path, extra_dirs=kw.pop("extra_dirs", ()), **kw)

    def test_whitelist_ids_and_binaries(self):
        ids = [a[0] for a in hr.AGENT_CATALOG]
        self.assertEqual(ids[0], "claude")
        self.assertEqual(len(ids), len(set(ids)))
        for _id, _name, binary, _p in hr.AGENT_CATALOG:
            self.assertRegex(binary, r"^[a-z][a-z0-9-]*$")  # 경로·공백·옵션 모양이 들어올 수 없다

    def test_installed_detection(self):
        touch_exe(self.bin, "claude")
        touch_exe(self.bin, "codex")
        touch_exe(self.bin, "gemini", 0o644)  # 실행 권한 없음 → 미설치
        nonexistent = os.path.join(tempfile.gettempdir(), "no-such-dir-xyz")
        inst = self.catalog(path=nonexistent + os.pathsep + self.bin).installed()
        self.assertTrue(inst["claude"])
        self.assertTrue(inst["codex"])
        if os.name != "nt":  # Windows는 실행 비트가 없어 권한으로 미설치를 가를 수 없다
            self.assertFalse(inst["gemini"])
        self.assertFalse(inst["opencode"])

    def test_extra_dirs_under_home(self):
        os.makedirs(os.path.join(self.home, ".bun", "bin"))
        touch_exe(os.path.join(self.home, ".bun", "bin"), "opencode")
        self.assertTrue(self.catalog(path="", extra_dirs=("~/.bun/bin",)).installed()["opencode"])

    def test_relative_path_entries_ignored(self):
        cwd = os.getcwd()
        os.chdir(self.bin)
        try:
            touch_exe(self.bin, "amp")
            self.assertFalse(self.catalog(path="." + os.pathsep).installed()["amp"])
        finally:
            os.chdir(cwd)

    def test_entries_shape(self):
        touch_exe(self.bin, "claude")
        es = self.catalog().entries()
        self.assertEqual(es[0], {"id": "claude", "name": "Claude Code", "installed": True, "prompt": True})
        self.assertEqual({e["id"] for e in es}, {a[0] for a in hr.AGENT_CATALOG})
        self.assertTrue(all(set(e) == {"id", "name", "installed", "prompt"} for e in es))

    def test_cache_ttl(self):
        clock = Clock()
        c = self.catalog(clock=clock, ttl=30.0)
        self.assertFalse(c.installed()["codex"])
        touch_exe(self.bin, "codex")
        clock.t = 10.0
        self.assertFalse(c.installed()["codex"])  # 캐시
        clock.t = 31.0
        self.assertTrue(c.installed()["codex"])

    def test_argv_shapes(self):
        self.assertEqual(hr.agent_argv("codex"), ["codex"])
        self.assertEqual(hr.agent_argv("codex", "-rf 해"), ["codex", "--", "-rf 해"])
        self.assertEqual(hr.agent_argv("gemini", "a b"), ["gemini", "--", "a b"])
        self.assertEqual(hr.agent_argv("grok", "x"), ["grok", "--", "x"])
        self.assertEqual(hr.agent_argv("opencode"), ["opencode"])
        with self.assertRaises(hr.ApiError) as cm:
            hr.agent_argv("opencode", "x")
        self.assertEqual((cm.exception.code, cm.exception.body["error"]), (400, "prompt_unsupported"))

    def test_launch_argv_errors(self):
        touch_exe(self.bin, "codex")
        c = self.catalog()
        self.assertEqual(c.launch_argv("codex", "hi"), [codex_cmd(c), "--", "hi"])
        for bad, err in (("nope", "unknown_agent"), ("", "unknown_agent"), ("../codex", "unknown_agent"),
                         ("codex --yolo", "unknown_agent"), (3, "unknown_agent"), (["codex"], "unknown_agent"),
                         ("gemini", "not_installed")):
            with self.subTest(agent=bad):
                with self.assertRaises(hr.ApiError) as cm:
                    c.launch_argv(bad)
                self.assertEqual((cm.exception.code, cm.exception.body["error"]), (400, err))

    def test_hc_run_agent_argv(self):
        calls = []

        def runner(argv, **kw):
            calls.append((argv, kw))
            return subprocess.CompletedProcess(argv, 0, '{"address":"t7"}', "")
        h = hr.Hc("/x/hc", runner=runner)
        self.assertEqual(h.run_agent("/h/p", ["codex", "--", "hi"]), {"address": "t7"})
        self.assertEqual(calls[0][0], ["/x/hc", "run", "--bg", "--json", "--cwd", "/h/p", "codex", "--", "hi"])
        self.assertIs(calls[0][1]["shell"], False)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.realpath(tempfile.mkdtemp())
        os.makedirs(os.path.join(self.home, "proj"))
        self.bin = tempfile.mkdtemp()
        touch_exe(self.bin, "claude")
        touch_exe(self.bin, "codex")
        self.app, self.hc, _ = make_app(self.tmp, home=self.home)
        self.hc.agent_calls = []
        hc = self.hc

        def run_agent(cwd, argv):
            hc.agent_calls.append((cwd, list(argv)))
            return {"address": "t55"}
        hc.run_agent = run_agent
        self.app.agent_catalog = hr.AgentCatalog(self.app.home, path_env=self.bin, extra_dirs=())
        self.srv = hr.make_server(self.app, "127.0.0.1", 0)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), data=data, method=method,
                                   headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_catalog_route(self):
        code, body = self.req("GET", "/api/agents/catalog")
        self.assertEqual(code, 200)
        by = {a["id"]: a for a in body["agents"]}
        self.assertTrue(by["claude"]["installed"])
        self.assertTrue(by["codex"]["installed"])
        self.assertFalse(by["gemini"]["installed"])

    def test_old_request_still_claude(self):
        code, body = self.req("POST", "/api/agents", {"path": "proj", "prompt": "안녕"})
        self.assertEqual((code, body), (200, {"id": "t99"}))
        self.assertEqual(self.hc.run_calls, [(os.path.join(self.home, "proj"), "안녕")])
        self.assertEqual(self.hc.agent_calls, [])
        code, _ = self.req("POST", "/api/agents", {"path": "proj", "agent": "claude"})
        self.assertEqual(code, 200)
        self.assertEqual(len(self.hc.run_calls), 2)

    def test_codex_launch(self):
        code, body = self.req("POST", "/api/agents", {"path": "proj", "agent": "codex", "prompt": "-x 해줘"})
        self.assertEqual((code, body), (200, {"id": "t55"}))
        self.assertEqual(self.hc.agent_calls, [(os.path.join(self.home, "proj"),
                                                [codex_cmd(self.app.agent_catalog), "--", "-x 해줘"])])
        self.assertEqual(self.hc.run_calls, [])

    def test_bad_agent_400(self):
        for agent, err in (("evil", "unknown_agent"), ("sh -c id", "unknown_agent"), ("gemini", "not_installed"),
                           ("opencode", "not_installed")):
            with self.subTest(agent=agent):
                code, body = self.req("POST", "/api/agents", {"path": "proj", "agent": agent})
                self.assertEqual((code, body["error"]), (400, err))
        self.assertEqual(self.hc.agent_calls, [])
        self.assertEqual(self.hc.run_calls, [])


if __name__ == "__main__":
    unittest.main()
