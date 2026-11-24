"""hc(herdr-core) 계약 테스트: 서버(Hc 클래스)가 읽는 JSON 키와 형태가 새 hc에서도 그대로인지 실제 바이너리로 본다.

SHELTIE_HC_CONTRACT_BIN(hc 또는 herdr-core 실행 파일 경로)이 없으면 전부 건너뛴다.
있으면 격리된 코어를 따로 띄운다. 사용자의 실제 코어·소켓·저장 터미널은 건드리지 않는다:
HC_SESSION(코어 이름), HC_SOCKET(짧은 소켓 경로), XDG_STATE_HOME·XDG_CONFIG_HOME, HOME을 모두 임시 폴더로 바꾼다.
끝나면 그 코어를 `hc server stop`으로 내리고 임시 폴더를 지운다.
"""
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import sheltie  # noqa: E402

BIN = os.environ.get("SHELTIE_HC_CONTRACT_BIN", "")
OSC_END = "\\033\\\\"  # sh printf가 ESC \ (ST)로 바꾼다


def osc7501(fields):
    return "printf '\\033]7501;%s%s'" % (fields, OSC_END)


@unittest.skipUnless(BIN and os.path.isfile(BIN), "SHELTIE_HC_CONTRACT_BIN 없음")
class HcContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="shc", dir="/tmp")  # 소켓 경로 길이 제한 때문에 짧게
        env = {k: v for k, v in os.environ.items() if not k.startswith("HC_")}
        home = os.path.join(cls.tmp, "home")
        os.makedirs(home)
        env.update({
            "HOME": home,
            "XDG_STATE_HOME": os.path.join(cls.tmp, "state"),
            "XDG_CONFIG_HOME": os.path.join(cls.tmp, "config"),
            "HC_SESSION": "contract",
            "HC_SOCKET": os.path.join(cls.tmp, "s.sock"),
        })
        cls.env = env
        cls.work = os.path.join(cls.tmp, "work")
        os.makedirs(cls.work)
        cls.work = os.path.realpath(cls.work)

        def runner(argv, **kw):
            kw.setdefault("env", env)
            return subprocess.run(argv, **kw)

        cls.runner = staticmethod(runner)
        cls.hc = sheltie.Hc(BIN, runner=runner)

    @classmethod
    def tearDownClass(cls):
        try:
            subprocess.run([BIN, "server", "stop"], env=cls.env, capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            pass
        # launchd 아래에서 돌면 코어가 `--hc-launchd`로 따로 떠서 server stop으로 안 내려간다(맥미니 감시 실측).
        # 이 테스트가 준 바이너리 경로나 임시 폴더를 명령줄에 가진 프로세스만 PID로 끈다.
        try:
            ps = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            ps = ""
        for line in ps.splitlines():
            pid, _, cmd = line.strip().partition(" ")
            if pid.isdigit() and int(pid) != os.getpid() and (cmd.startswith(BIN + " ") or cls.tmp in cmd):
                try:
                    os.kill(int(pid), signal.SIGKILL)
                except OSError:
                    pass
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # --- 도우미
    def run_sh(self, script):
        return self.hc.run_agent(self.work, ["sh", "-c", script])

    def wait_for(self, fn, timeout=8.0, what="조건"):
        end = time.time() + timeout
        while time.time() < end:
            try:
                last = fn()
            except sheltie.HcError:
                last = None
            if last:
                return last
            time.sleep(0.1)
        self.fail("%s을(를) %s초 안에 못 봤다" % (what, timeout))

    def kill(self, tid):
        try:
            self.hc._run(["kill", tid])
        except sheltie.HcError:
            pass

    # --- 테스트
    def test_version(self):
        out = subprocess.run([BIN, "--version"], env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(out.returncode, 0)
        ver = sheltie.parse_hc_version(out.stdout)
        self.assertRegex(ver or "", r"^\d+\.\d+\.\d+$")
        info = sheltie.hc_version_check(BIN, runner=self.runner)
        self.assertEqual(info["hc_version"], ver)

    def test_run_agent_and_ls(self):
        r = self.run_sh("printf ready; sleep 30")
        tid = r.get("address")
        try:
            self.assertTrue(sheltie.ID_RE.match(tid or ""), r)
            ls = self.wait_for(lambda: [t for t in self.hc.ls() if t.get("address") == tid], what="ls에 터미널")
            t = ls[0]
            self.assertEqual(t.get("state"), "live")
            self.assertIsInstance(t.get("cwd"), str)
            self.assertIn("agent", t)
        finally:
            self.kill(tid)

    def test_get_keys(self):
        tid = self.run_sh("printf ready; sleep 30")["address"]
        try:
            info = self.wait_for(lambda: self.hc.get(tid), what="get")
            self.assertEqual(info.get("state"), "live")
            self.assertEqual(info.get("address"), tid)
            fg = self.wait_for(lambda: (self.hc.get(tid).get("foreground") or None), what="foreground")
            self.assertIsInstance(fg, dict)
            self.assertIsInstance(fg.get("argv"), list)
            self.assertIsInstance(fg.get("cwd"), str)
            self.assertIsInstance(info.get("cwd"), str)
            self.assertEqual(os.path.realpath(sheltie.live_cwd(self.hc.get(tid))), self.work)
            self.assertIn("exit_code", info)
            self.assertIn("turn", info)
        finally:
            self.kill(tid)

    def test_status_osc7501(self):
        tid = self.run_sh(osc7501("state=blocked:kind=permission:app=claude:msg=aGVsbG8=") + "; sleep 30")["address"]
        try:
            info = self.wait_for(lambda: self.hc.get(tid).get("status") and self.hc.get(tid), what="status")
            st = sheltie.status_of(info)
            self.assertEqual(st.get("state"), "blocked")
            self.assertEqual(st.get("kind"), "permission")
            self.assertEqual(st.get("app"), "claude")
            self.assertEqual(st.get("msg"), "hello")  # base64를 풀어서 준다
        finally:
            self.kill(tid)

    def test_read_send_keys(self):
        tid = self.run_sh("cat")["address"]
        try:
            self.wait_for(lambda: self.hc.get(tid), what="get")
            self.hc.send(tid, "-hello-contract")  # '-'로 시작해도 옵션으로 읽히지 않는다
            self.hc.keys(tid, ["enter"])
            out = self.wait_for(lambda: "-hello-contract" in self.hc.read(tid)["text"] and self.hc.read(tid),
                                what="read에 보낸 글자")
            self.assertIsInstance(out["text"], str)
            self.assertIsInstance(out.get("complete"), bool)
            lim = self.hc.read(tid, lines=1)
            self.assertIsInstance(lim["text"], str)
            self.hc.keys(tid, ["shift-tab"])  # HC_KEY_NAMES 이름이 받아들여진다
            self.hc.keys(tid, ["ctrl-c"])
        finally:
            self.kill(tid)

    def test_keys_ctrl_d_ends_cat(self):
        tid = self.run_sh("cat")["address"]
        self.wait_for(lambda: self.hc.get(tid), what="get")
        self.hc.keys(tid, ["ctrl-d"])
        info = self.wait_for(lambda: self.hc.get(tid).get("state") == "finished" and self.hc.get(tid),
                             what="ctrl-d로 끝남")
        self.assertEqual(info.get("exit_code"), 0)

    def test_send_force_on_blocked(self):
        tid = self.run_sh(osc7501("state=blocked:kind=question:app=x:msg=cT8=") + "; cat")["address"]
        try:
            self.wait_for(lambda: sheltie.status_of(self.hc.get(tid)).get("state") == "blocked", what="blocked")
            with self.assertRaises(sheltie.HcError):
                self.hc.send(tid, "nope")  # blocked 터미널은 기본 거부
            self.hc.send(tid, "yes-forced", force=True)
            self.hc.keys(tid, ["enter"])
            self.wait_for(lambda: "yes-forced" in self.hc.read(tid)["text"], what="강제로 보낸 글자")
        finally:
            self.kill(tid)

    def test_read_styled_attach_observe(self):
        tid = self.run_sh("printf '\\033[31mredtext\\033[0m'; sleep 30")["address"]
        try:
            self.wait_for(lambda: "redtext" in self.hc.read(tid)["text"], what="화면에 글자")
            out = self.hc.read_styled(tid)
            self.assertIn("redtext", out)
            self.assertIn("\x1b[", out)  # SGR 등 제어열이 남아 있다
        finally:
            self.kill(tid)

    def test_events_stream(self):
        proc = subprocess.Popen(self.hc.events_argv(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL, text=True, env=self.env)
        tid = None
        try:
            time.sleep(0.5)
            tid = self.run_sh("sleep 1; " + osc7501("state=working:app=x") + "; sleep 1; exit 3")["address"]
            seen = {}
            end = time.time() + 15
            while time.time() < end and "finished" not in seen:
                rd, _, _ = select.select([proc.stdout], [], [], 0.5)
                if not rd:
                    continue
                line = proc.stdout.readline()
                if not line:
                    break
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(ev, dict) or ev.get("method") != "event":
                    continue
                p = ev.get("params") or {}
                if p.get("terminal") == tid:
                    seen[p.get("type")] = p.get("data")
            self.assertIn("status", seen)
            self.assertIn("finished", seen)
            self.assertIsInstance(seen["status"], dict)
            self.assertEqual(seen["finished"].get("exit_code"), 3)
        finally:
            proc.kill()
            proc.wait(timeout=5)
            proc.stdout.close()
            if tid:
                self.kill(tid)


if __name__ == "__main__":
    unittest.main()
