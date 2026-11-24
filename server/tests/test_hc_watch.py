"""ops/hc-watch.sh: 받은 herdr-core가 ~/Documents(저장소) 경로를 보지 않고, 시간 상한을 넘으면 끊기는지 본다.

가짜 태그 목록·tar.gz를 file:// 주소로 주고 --dry-run으로 돌린다(네트워크·텔레그램·상태 파일 없음).
가짜 herdr-core는 부를 때마다 작업 폴더·PWD·HOME을 FAKE_LOG에 적는다. --version이 아니면 프로세스 그룹을 벗어난
자식을 하나 남기고 오래 잔다(권한 창에 막힌 코어 흉내).
"""
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
SCRIPT = os.path.join(ROOT, "ops", "hc-watch.sh")

FAKE_HC = r'''#!/bin/sh
echo "call=$1 cwd=$(pwd -P) pwd=$PWD home=$HOME" >> "$FAKE_LOG"
if [ "$1" = "--version" ]; then echo "herdr-core 9.9.9"; exit 0; fi
if [ "$1" = "linger" ]; then while :; do sleep 1; done; fi
# 프로세스 그룹을 벗어난 자식(launchd로 따로 뜬 코어 흉내). 명령줄에 이 파일 경로가 있다.
perl -e 'setpgrp(0, 0); exec @ARGV' "$0" linger </dev/null >/dev/null 2>&1 &
echo "linger=$!" >> "$FAKE_LOG"
exec sleep 600
'''


def plat():
    m = os.uname().machine
    return {"arm64": "macos-aarch64", "x86_64": "macos-x86_64"}[m] if os.uname().sysname == "Darwin" else \
        {"x86_64": "linux-x86_64", "aarch64": "linux-aarch64"}[m]


class HcWatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hcw")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # 가짜 저장소(HERDR_REMOTE_DIR): install.sh의 고정 버전과 실제 테스트 소스
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.repo, "server", "tests"))
        with open(os.path.join(self.repo, "install.sh"), "w") as f:
            f.write('HC_PIN_VERSION="0.1.0"\n')
        shutil.copy(os.path.join(ROOT, "server", "sheltie.py"), os.path.join(self.repo, "server"))
        shutil.copy(os.path.join(ROOT, "server", "tests", "test_hc_contract.py"),
                    os.path.join(self.repo, "server", "tests"))
        # 가짜 배포처
        dl = os.path.join(self.tmp, "dl", "9.9.9")
        os.makedirs(dl)
        fake = os.path.join(self.tmp, "herdr-core")
        with open(fake, "w") as f:
            f.write(FAKE_HC)
        os.chmod(fake, 0o755)
        with tarfile.open(os.path.join(dl, "herdr-core-%s.tar.gz" % plat()), "w:gz") as t:
            t.add(fake, arcname="herdr-core")
        tags = os.path.join(self.tmp, "tags.json")
        with open(tags, "w") as f:
            f.write('[{"name": "v9.9.9"}, {"name": "v0.1.0"}, {"name": "nightly"}]')
        self.log = os.path.join(self.tmp, "fake.log")
        self.home = os.path.join(self.tmp, "realhome")
        os.makedirs(self.home)
        self.env = dict(os.environ, HOME=self.home, HERDR_REMOTE_DIR=self.repo, FAKE_LOG=self.log,
                        HC_WATCH_TAGS_URL="file://" + tags,
                        HC_WATCH_DOWNLOAD_BASE="file://" + os.path.join(self.tmp, "dl"),
                        HC_WATCH_CHANGELOG_BASE="file://" + os.path.join(self.tmp, "nochangelog"),
                        HC_WATCH_TEST_LIMIT="4")

    def run_watch(self):
        start = time.time()
        out = subprocess.run(["/bin/bash", SCRIPT, "--dry-run"], cwd=os.path.join(self.repo, "server"),
                             env=self.env, capture_output=True, text=True, timeout=60)
        return out, time.time() - start

    def calls(self):
        with open(self.log) as f:
            return [dict(kv.split("=", 1) for kv in line.split()) for line in f if line.startswith("call=")]

    def test_runs_outside_repo_and_home(self):
        out, _ = self.run_watch()
        self.assertEqual(out.returncode, 0, out.stderr)
        calls = self.calls()
        self.assertTrue(any(c["call"] == "--version" for c in calls), calls)
        self.assertTrue(any(c["call"] != "--version" for c in calls), calls)  # 계약 테스트도 바이너리를 불렀다
        repo = os.path.realpath(self.repo)
        for c in calls:
            for key in ("cwd", "pwd", "home"):
                self.assertFalse(os.path.realpath(c[key]).startswith(repo), (key, c))
                self.assertFalse(os.path.realpath(c[key]).startswith(os.path.realpath(self.home)), (key, c))
            self.assertIn("/hc-watch.", c["cwd"])
            self.assertNotEqual(c["home"], self.home)

    def test_time_limit_kills_stuck_core(self):
        out, took = self.run_watch()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("hc 9.9.9 나왔다", out.stdout)
        self.assertIn("4초 안에 안 끝나 강제로 끊었다", out.stdout)
        self.assertLess(took, 30)
        with open(self.log) as f:
            lingers = [line.split("=", 1)[1].strip() for line in f if line.startswith("linger=")]
        self.assertTrue(lingers)
        time.sleep(0.3)
        for pid in lingers:  # 그룹을 벗어난 자식까지 꺼졌다
            alive = subprocess.run(["ps", "-p", pid], capture_output=True).returncode == 0
            self.assertFalse(alive, "pid %s가 남았다" % pid)
        left = subprocess.run(["pgrep", "-f", "/hc-watch\\..*/bin/herdr-core linger"], capture_output=True, text=True)
        self.assertEqual(left.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
