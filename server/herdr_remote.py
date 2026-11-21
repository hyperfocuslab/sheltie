#!/usr/bin/env python3
"""herdr-remote: 맥미니의 herdr-core(`hc`) 위 Claude Code 에이전트를 아이폰에서 지켜보고 답하는 개인용 중계 서버.

Tailscale 주소에만 열리고, 모든 요청에 Bearer 토큰이 필요하다. 표준 라이브러리만 쓴다.
hc는 항상 argv 리스트(shell=False)로 부르며, 임의 명령을 실행하는 경로는 없다.

명령
  python3 herdr_remote.py init       설정이 없으면 토큰을 만들어 저장하고 한 번 출력한다(있으면 그대로 둔다).
  python3 herdr_remote.py serve      서버 실행(기본 동작).
  python3 herdr_remote.py test-push  등록된 모든 기기에 시험 푸시 1건.
  python3 herdr_remote.py pair       앱 연결용 QR과 sheltie:// 주소 출력(설정 public_url, 없으면 Tailscale 이름).
  python3 herdr_remote.py hook claude|codex  에이전트 훅이 부른다(stdin 훅 JSON → 로컬 서버 → stdout 결정 JSON).

폰 접속: 맥미니 방화벽이 Homebrew Python의 들어오는 연결을 막으므로 127.0.0.1에만 열고
tailscale serve로 tailnet 안에만 HTTPS로 내보낸다(다른 맥미니 서비스와 같은 방식, Funnel 아님):
ssh my-mac '/opt/homebrew/bin/tailscale serve --bg --https=8790 http://127.0.0.1:8790'
→ 앱 주소 https://my-mac.tailnet.ts.net:8790

상시 실행 설치(홈이 외장 볼륨이면 ~/Library/LaunchAgents는 bootstrap이 실패한다.
/Library/LaunchAgents에 root 소유로 두고 사용자 gui 도메인에 올린다. sudo 비밀번호가 필요하다):
ssh -t my-mac 'python3 ~/Documents/github/herdr-remote/server/herdr_remote.py init && sudo install -o root -g wheel -m 644 ~/Documents/github/herdr-remote/deploy/com.hyperfocuslab.herdr-remote.plist /Library/LaunchAgents/ && launchctl bootstrap gui/$(id -u) /Library/LaunchAgents/com.hyperfocuslab.herdr-remote.plist'
재시작: launchctl kickstart -k gui/$(id -u)/com.hyperfocuslab.herdr-remote; tail ~/Library/Logs/herdr-remote.log
해제: launchctl bootout gui/$(id -u)/com.hyperfocuslab.herdr-remote && sudo rm /Library/LaunchAgents/com.hyperfocuslab.herdr-remote.plist
APNs 키: Apple Developer에서 받은 .p8을 ~/.config/herdr-remote/AuthKey.p8 (0600)에 둔다. 없으면 푸시만 꺼진다.
중계 모드: APNs 키 파일이 없고 설정의 push_relay_url이 비어 있으면 기본 중계(DEFAULT_RELAY_URL)로 키 없이 보낸다
(App Store판 앱, 기기마다 relay_sig 필요). push_relay_url에 https 주소를 넣으면 그 중계를 쓰고, "off"면 중계를 끈다.
키 파일이 있고 push_relay_url이 비어 있으면 지금처럼 APNs 직접 모드.
Android(env "fcm") 기기는 APNs 키가 있어도 항상 중계로 보낸다: push_relay_url이 비면 기본 중계, 주소면 그 중계,
"off"면 보내지 않고 발송 상태에 실패(fcm_relay_off)로 남긴다.

환경변수
  HERDR_REMOTE_CONFIG  설정 파일(기본 ~/.config/herdr-remote/config.json). devices.json은 같은 폴더.
  HERDR_REMOTE_LOG     로그 파일(기본 ~/Library/Logs/herdr-remote.log, 1MB 회전)

Windows(Python 3.9+, 표준 라이브러리만): 같은 파일 하나로 돈다. 맥과 다른 점만 적는다.
  설정·devices.json은 맥과 같은 ~/.config/herdr-remote/ (토큰이 든 파일이라 로밍 %APPDATA%에 두지 않는다).
  로그 기본 %LOCALAPPDATA%\\herdr-remote\\herdr-remote.log. hc 기본 %LOCALAPPDATA%\\Programs\\herdr-core\\hc.exe.
  상시 실행은 작업 스케줄러(로그온 시)로 `pythonw.exe herdr_remote.py serve`(콘솔 없음 → 자식 프로세스는 CREATE_NO_WINDOW).
  에이전트는 PATH·흔한 설치 폴더에서 .exe 우선으로 찾아 절대 경로로 hc에 넘긴다(hc 코어는 PATH를 띄운 환경에서 물려받는다).
  APNs 직접 모드(curl·openssl)는 지원하지 않는다: 푸시는 중계 모드만 쓴다.
"""
import base64
import hashlib
import hmac
import http.client
import http.server
import ipaddress
import json
import logging
import logging.handlers
import ntpath
import os
import re
import secrets
import select
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.parse
import uuid

# 플랫폼 분기는 이 상수 하나로만 한다(테스트는 monkeypatch로 Windows 경로를 맥에서 돌린다).
IS_WINDOWS = os.name == "nt"
CREATE_NO_WINDOW = 0x08000000

DEFAULT_CONFIG_PATH = "~/.config/herdr-remote/config.json"
DEFAULT_LOG_PATH = "~/Library/Logs/herdr-remote.log"


def _local_appdata():
    return os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")


def default_log_path():
    if IS_WINDOWS:
        return os.path.join(_local_appdata(), "herdr-remote", "herdr-remote.log")
    return DEFAULT_LOG_PATH


def default_hc_path():
    if IS_WINDOWS:
        return os.path.join(_local_appdata(), "Programs", "herdr-core", "hc.exe")
    return "~/.local/bin/hc"


def subprocess_kwargs(text=False):
    """자식 프로세스 공용 인자. 맥은 빈 dict(주입된 runner의 kwargs가 그대로다).
    Windows: pythonw에는 콘솔이 없어 콘솔 앱을 부를 때마다 창이 깜빡이므로 CREATE_NO_WINDOW,
    텍스트 출력은 로케일(cp949)이 아니라 UTF-8로 읽는다."""
    if not IS_WINDOWS:
        return {}
    kw = {"creationflags": CREATE_NO_WINDOW}
    if text:
        kw.update(encoding="utf-8", errors="replace")
    return kw


def is_abs_path(p):
    """cwd가 절대 경로인가. Windows 규칙은 맥에서도 ntpath로 검사할 수 있다."""
    return ntpath.isabs(p) or os.path.isabs(p) if IS_WINDOWS else os.path.isabs(p)
DEFAULTS = {
    "bind": "127.0.0.1",
    "port": 8790,
    "token": "",
    "hc_path": default_hc_path(),
    "apns_key_path": "~/.config/herdr-remote/AuthKey.p8",
    "apns_key_id": "5BN2SS5X65",
    "team_id": "2CU9248L85",
    "topic": "com.hyperfocuslab.sheltie",
    "apns_hosts": {"sandbox": "https://api.sandbox.push.apple.com",
                   "production": "https://api.push.apple.com"},
    "push_relay_url": "",
}

DEFAULT_RELAY_URL = "https://sheltie-push-relay.cpubench.workers.dev"
RELAY_OFF = "off"  # push_relay_url에 이 값을 넣으면 중계를 쓰지 않는다

HC_TIMEOUT = 10
MAX_BODY = 64 * 1024
POLL_INTERVAL = 15
BIND_RETRY = 10
EVENTS_MAX_BACKOFF = 30
JWT_TTL = 50 * 60
RELAY_TIMEOUT = 10
RELAY_IDLE_MAX = 60  # 이보다 오래 쉰 연결은 NAT가 조용히 버렸을 수 있어 새로 맺는다
RELAY_RETRY_WAIT = 1.0  # 중계 5xx 뒤 한 번 다시 보내기 전 쉬는 시간(초)
REMINDER_AFTER_SEC = (300, 1800)  # 막힘 첫 알림 뒤 다시 알릴 시점(초). config reminder_after_sec로 바꾼다
PUSH_STATUS_FILE = "push_status.json"
PUSH_FAILING_WINDOW_MS = 24 * 3600 * 1000
PUSH_LOG_MAX_DEVICES = 64
PUSH_TEST_MIN_GAP = 10.0  # 같은 기기 시험 알림 최소 간격(초)

ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
DEVICE_TOKEN_RE = re.compile(r"^[0-9a-fA-F]{32,200}$")
# FCM 등록 토큰: 대소문자를 가리므로 바꾸지 않고 저장한다. 중계(relay.ts FCM_TOKEN_RE)와 같은 범위.
FCM_TOKEN_RE = re.compile(r"^[A-Za-z0-9_:\-]{20,1024}$")
APNS_ENVS = ("sandbox", "production")
FCM_ENV = "fcm"
FCM_RELAY_OFF_REASON = "fcm_relay_off"
RELAY_SIG_RE = re.compile(r"^v1\.[A-Za-z0-9_-]{43}$")
DEVICE_ID_RE = re.compile(r"^[0-9a-f]{16}$")
ALLOWED_KEYS = frozenset(["enter", "esc", "up", "down", "left", "right", "tab", "space"]
                         + [str(i) for i in range(1, 10)])
MAX_KEYS = 10
MAX_TEXT = 4000
TRUST_MSG = "trust this folder"
TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")

log = logging.getLogger("herdr-remote")


# ---------------------------------------------------------------- 설정·파일

def config_path():
    return os.path.expanduser(os.environ.get("HERDR_REMOTE_CONFIG") or DEFAULT_CONFIG_PATH)


def ensure_private_dir(d):
    if IS_WINDOWS:
        # Windows는 사용자 프로필(홈) ACL을 그대로 물려받는다. Python 3.13+는 mode=0o700이면 "소유자만" ACL을 거는데,
        # 관리자 PowerShell에서 설치하면 소유자가 Administrators가 되어 일반 권한 서비스가 설정을 못 읽는다(7950 실측).
        os.makedirs(d, exist_ok=True)
        return
    os.makedirs(d, mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)


def write_private(path, data):
    """data(dict/list)를 JSON으로 0600 파일에 원자적으로 쓴다."""
    ensure_private_dir(os.path.dirname(path) or ".")
    tmp = "%s.tmp.%d" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_config(path=None):
    path = path or config_path()
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    cfg = dict(DEFAULTS)
    cfg.update(raw)
    hosts = dict(DEFAULTS["apns_hosts"])
    hosts.update(raw.get("apns_hosts") or {})
    cfg["apns_hosts"] = hosts
    for k in ("hc_path", "apns_key_path"):
        cfg[k] = os.path.expanduser(cfg[k])
    raw_relay = cfg.get("push_relay_url")
    cfg["push_relay_url"] = resolve_relay_url(raw_relay, cfg["apns_key_path"])
    cfg["fcm_relay_url"] = resolve_fcm_relay_url(raw_relay)
    cfg["port"] = int(cfg["port"])
    cfg["_path"] = path
    cfg["_dir"] = os.path.dirname(path)
    return cfg


def resolve_relay_url(url, apns_key_path):
    """실제로 쓸 중계 주소. "off"면 "", 비었고 APNs 키 파일이 없으면 기본 중계, 아니면 check_relay_url."""
    if url == RELAY_OFF:
        return ""
    # Windows는 APNs 직접 모드(curl·openssl)를 지원하지 않으므로 키 파일이 있어도 중계를 쓴다.
    if (url is None or url == "") and (IS_WINDOWS or not os.path.isfile(apns_key_path or "")):
        return DEFAULT_RELAY_URL
    return check_relay_url(url)


def resolve_fcm_relay_url(url):
    """Android(fcm) 기기에 쓸 중계 주소. APNs 키와 상관없다: "off"면 "", 비었으면 기본 중계, 아니면 check_relay_url."""
    if url == RELAY_OFF:
        return ""
    if url is None or url == "":
        return DEFAULT_RELAY_URL
    return check_relay_url(url)


def check_relay_url(url):
    """push_relay_url 검사. 비었으면 "", https만 허용(로컬 시험용 http://127.0.0.1·localhost 예외). 끝 슬래시는 뗀다."""
    if url is None or url == "":
        return ""
    if not isinstance(url, str):
        raise ValueError("push_relay_url은 문자열이어야 한다")
    u = urllib.parse.urlsplit(url)
    u.port  # 포트가 숫자가 아니면 ValueError
    ok = u.hostname and (u.scheme == "https" or (u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost")))
    if not ok or u.username or u.password or u.query or u.fragment:
        raise ValueError("push_relay_url 거부: https://만 허용(로컬 시험은 http://127.0.0.1·http://localhost): %r" % url)
    return url.rstrip("/")


def init_config(path=None, out=None):
    """설정이 없을 때만 만든다. 만들었으면 토큰을 한 번 출력한다. 반환: 만들었는지."""
    out = out or sys.stdout
    path = path or config_path()
    ensure_private_dir(os.path.dirname(path))
    if os.path.exists(path):
        print("설정이 이미 있어 그대로 둔다: %s" % path, file=out)
        return False
    cfg = json.loads(json.dumps(DEFAULTS))
    cfg["token"] = secrets.token_urlsafe(32)
    write_private(path, cfg)
    print("설정을 만들었다: %s" % path, file=out)
    print("토큰(앱에 한 번 입력): %s" % cfg["token"], file=out)
    return True


def setup_logging(path=None):
    path = os.path.expanduser(path or os.environ.get("HERDR_REMOTE_LOG") or default_log_path())
    os.makedirs(os.path.dirname(path), exist_ok=True)
    h = logging.handlers.RotatingFileHandler(path, maxBytes=1024 * 1024, backupCount=2, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(h)
    log.setLevel(logging.INFO)


def short_token(tok):
    return (tok or "")[:6] + "…"


# ---------------------------------------------------------------- hc

class HcError(Exception):
    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code


class Hc:
    """hc CLI 래퍼. 항상 argv 리스트, shell=False, 타임아웃 10초."""

    def __init__(self, path, runner=None):
        self.path = path
        self.runner = runner or subprocess.run

    def _run(self, args):
        argv = [self.path] + list(args)
        try:
            p = self.runner(argv, capture_output=True, text=True, timeout=HC_TIMEOUT, shell=False,
                            **subprocess_kwargs(text=True))
        except subprocess.TimeoutExpired:
            raise HcError("hc timeout: %s" % args[0])
        except OSError as e:
            raise HcError("hc not runnable: %s" % e)
        if p.returncode != 0:
            raise HcError((p.stderr or p.stdout or "").strip()[:300] or "hc exit %d" % p.returncode,
                          p.returncode)
        return p.stdout

    def _json(self, args):
        out = self._run(args)
        try:
            return json.loads(out)
        except ValueError:
            raise HcError("hc returned non-JSON for %s" % args[0])

    def ls(self):
        return self._json(["ls", "--json"]).get("terminals") or []

    def get(self, tid):
        return self._json(["get", tid, "--json"])

    def read(self, tid, lines=0):
        args = ["read", tid, "--json"]
        if lines:
            args += ["--lines", str(int(lines))]
        return self._json(args)

    def keys(self, tid, keys):
        self._run(["keys", tid] + list(keys))

    def send(self, tid, text, force=False):
        # `--` 로 끝맺어 '-'로 시작하는 문장이 옵션(--force 등)으로 읽히지 않게 한다.
        # force: 메뉴의 자유 입력 줄에 쓸 때만(hc는 blocked 터미널에 쓰기를 기본으로 거부한다).
        self._run(["send"] + (["--force"] if force else []) + [tid, "--", text])

    def run_claude(self, cwd, prompt=None, exe=None):
        """새 Claude 터미널. 실행 프로그램은 claude로 고정, prompt는 `--` 뒤라 옵션으로 읽히지 않는다(실측 2.1.329).
        exe: Windows에서 찾아 둔 claude 절대 경로(hc 코어가 PATH를 못 찾는다). 맥은 None이라 이름 그대로."""
        argv = ["run", "--bg", "--json", "--cwd", cwd, exe or "claude"]
        if prompt:
            argv += ["--", prompt]
        return self._json(argv)

    def run_agent(self, cwd, argv):
        """허용 목록 에이전트 터미널. argv는 agent_argv()가 만든 리스트만 온다(프롬프트는 `--` 뒤)."""
        return self._json(["run", "--bg", "--json", "--cwd", cwd] + list(argv))

    def events_argv(self):
        return [self.path, "events"]


def status_of(info):
    s = (info or {}).get("status")
    return s if isinstance(s, dict) else {}


def live_cwd(info):
    """지금 폴더: 앞에서 도는 프로그램의 cwd(셸에서 cd한 뒤의 폴더), 없으면 터미널을 연 폴더."""
    info = info or {}
    fg = info.get("foreground")
    if isinstance(fg, dict) and isinstance(fg.get("cwd"), str) and fg.get("cwd"):
        return fg["cwd"]
    cwd = info.get("cwd")
    return cwd if isinstance(cwd, str) else None


def _is_claude_binary(arg0):
    if IS_WINDOWS:  # C:\\...\\claude.exe: 구분자 두 가지, 확장자, 대소문자
        base = ntpath.basename(arg0).lower()
        if base.endswith(".exe"):
            base = base[:-4]
        return base == "claude"
    return os.path.basename(arg0) == "claude"


def is_claude(info):
    info = info or {}
    if info.get("agent") in ("claude", "claude-code"):
        return True
    if status_of(info).get("app") == "claude-code":
        return True
    fg = info.get("foreground")
    if isinstance(fg, dict):
        argv = fg.get("argv")
        if isinstance(argv, list) and argv and isinstance(argv[0], str):
            if _is_claude_binary(argv[0]):
                return True
    return False


def _binary_base(arg0):
    if IS_WINDOWS:
        base = ntpath.basename(arg0).lower()
        for ext in (".exe", ".cmd"):
            if base.endswith(ext):
                return base[:-len(ext)]
        return base
    return os.path.basename(arg0)


def is_codex(info):
    """OpenAI Codex CLI 터미널. hc가 agent·status.app을 codex로 알려 주거나 앞 프로그램이 codex다."""
    info = info or {}
    if info.get("agent") == "codex" or status_of(info).get("app") == "codex":
        return True
    fg = info.get("foreground")
    if isinstance(fg, dict):
        argv = fg.get("argv")
        if isinstance(argv, list) and argv and isinstance(argv[0], str) and _binary_base(argv[0]) == "codex":
            return True
    return False


SCRIPT_HOSTS = ("node", "bun", "deno")


def _agent_names(info):
    """hc agent, status.app, 앞 프로그램 이름(node·bun으로 도는 CLI는 그 스크립트 이름)."""
    info = info or {}
    names = set()
    for v in (info.get("agent"), status_of(info).get("app")):
        if isinstance(v, str) and v:
            names.add(v)
    fg = info.get("foreground")
    argv = fg.get("argv") if isinstance(fg, dict) else None
    if isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv):
        base = _binary_base(argv[0])
        names.add(base)
        if base in SCRIPT_HOSTS:
            script = next((a for a in argv[1:] if not a.startswith("-")), None)
            if script:
                names.add(_binary_base(script))
    return names


def _named(*names):
    want = set(names)
    return lambda info: bool(_agent_names(info) & want)


def agent_kind(info):
    """잠금화면 답·대화 보기·메시지 보내기를 지원하는 에이전트 종류(AGENTS 키), 아니면 None."""
    for kind in AGENT_ORDER:
        if AGENTS[kind]["match"](info):
            return kind
    return None


def agent_spec(kind):
    """에이전트 표 항목. 모르는 종류는 Claude 항목(옛 동작)."""
    return AGENTS.get(kind) or AGENTS["claude"]


def is_permission(status):
    msg = status.get("msg") or ""
    if msg == TRUST_MSG:
        return True
    return status.get("kind") == "permission" and msg.startswith("approve ")


class ApiError(Exception):
    def __init__(self, code, error, **extra):
        super().__init__(error)
        self.code = code
        self.body = dict(error=error, **extra)


def decide_answer(info, action, expect_msg=None):
    """현재 hc get 결과를 보고 보낼 키를 정한다. 오래된 알림에서 허락을 눌러도
    작업 중인 입력창에 '1'이 들어가지 않게 하는 guard."""
    if action not in ("approve", "reject"):
        raise ApiError(400, "bad_action")
    st = status_of(info)
    if st.get("state") != "blocked":
        raise ApiError(409, "not_blocked")
    msg = st.get("msg")
    if expect_msg is not None and expect_msg != msg:
        raise ApiError(409, "changed", msg=msg)
    if action == "reject":
        return ["esc"]
    if msg == TRUST_MSG:
        return ["down", "enter"]
    if st.get("kind") == "permission" and isinstance(msg, str) and msg.startswith("approve "):
        return ["1"]
    raise ApiError(409, "needs_manual", msg=msg)


def validate_text(text):
    if not isinstance(text, str) or not (1 <= len(text) <= MAX_TEXT):
        raise ApiError(400, "bad_text")
    for ch in text:
        if ch in "\n\t":
            continue
        o = ord(ch)
        if o < 0x20 or o == 0x7F or 0x80 <= o < 0xA0:
            raise ApiError(400, "bad_text")
    return text


def validate_keys(keys):
    if not isinstance(keys, list) or not (1 <= len(keys) <= MAX_KEYS):
        raise ApiError(400, "bad_keys")
    for k in keys:
        if not isinstance(k, str) or k not in ALLOWED_KEYS:
            raise ApiError(400, "bad_keys")
    return keys


# ---------------------------------------------------------------- 메뉴 카드(화면 파싱)
#
# Claude Code 화면에서 활성 메뉴(권한 승인, 질문, 계획, 리뷰, 확인)를 읽는다.
# 방식은 Ovis(MIT, devswha/ovis, server/agents/claude.ts·shared.ts)를 따라 줄였다:
# 힌트 줄로 종류를 정하고, 힌트가 화면 끝에 있을 때만 활성으로 본다.
# 실측: Claude Code 2.1.329, 맥미니 200열(tests/fixtures/claude-*-2.1.329.txt).

ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
ASK_HINT_RE = re.compile(r"enter to select.*(?:↑/↓|tab/arrow keys) to navigate.*esc to cancel", re.I)
CONFIRM_HINT_RE = re.compile(r"enter to confirm.*esc to (?:cancel|exit|go back)", re.I)
ESC_HINT_RE = re.compile(r"esc to cancel", re.I)
PLAN_Q_RE = re.compile(r"Claude has written up a plan and is ready to execute\. Would you like to proceed\?")
APPROVAL_Q_RE = re.compile(r"^Do you want to .+\?$")
SOLID_RULE_RE = re.compile(r"^[─━]{8,}$")
LABELED_RULE_RE = re.compile(r"^─{3,}\s.*─$")
DASH_RULE_RE = re.compile(r"^[╌┄┈╍]{8,}$")
NUM_ROW_RE = re.compile(r"^(\s*)([›>❯])?\s*(\d+)\.\s+(.+)$")
CHECKBOX_RE = re.compile(r"^\[([ xX✓✔])\]\s*")
SUBMIT_ROW_RE = re.compile(r"^(?:([›>❯])\s*)?Submit$")
TABS_RE = re.compile(r"^←\s+[☐☒☑✔]")
CHIP_RE = re.compile(r"^([☐☒☑✔])\s+(\S.*)$")
TAB_ITEM_RE = re.compile(r"([☐☒☑✔])\s+(.+?)(?=\s{2,}|\s*$)")
CALL_RE = re.compile(r"^[●⏺]\s+[\w.:-]+(?:\s[\w.:-]+)*(?:\s\(MCP\))?\(")
PLAN_FILE_RE = re.compile(r"(~/\.claude/plans/[A-Za-z0-9._-]+\.md)")
FREE_TEXT_RE = re.compile(r"^(?:Type something\.?|Tell Claude what to change)$", re.I)
SELECTED_MARK_RE = re.compile(r"^[❯›>]\s*")
PROMPT_ID_RE = re.compile(r"^[0-9a-f]{12}$")
SIDE_PANEL_MIN_COL = 40
SIDE_PANEL_MIN_ROWS = 8
MAX_PROMPT_BODY = 12000
PROMPT_KINDS = ("approval", "question", "plan", "confirm", "submit")


def char_width(ch):
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def strip_side_panel(lines):
    """오른쪽에 붙은 플러그인 사이드 패널(같은 표시 열의 `│`가 8행 이상, 열 40 이상)을 잘라낸다.
    폭 계산이 터미널과 한 칸 어긋나는 글자가 있어도 잘리게 ±2열 안의 `│`를 본다."""
    cols = []
    counts = {}
    for line in lines:
        found = []
        w = 0
        for i, ch in enumerate(line):
            if ch == "│" and w >= SIDE_PANEL_MIN_COL - 2:
                found.append((w, i))
            w += char_width(ch)
        cols.append(found)
        for c in set(w for w, _ in found):
            counts[c] = counts.get(c, 0) + 1
    if not counts:
        return lines
    best = max(counts.items(), key=lambda kv: (kv[1], -kv[0]))
    col, n = best
    if n < SIDE_PANEL_MIN_ROWS or col < SIDE_PANEL_MIN_COL:
        return lines
    out = []
    for line, found in zip(lines, cols):
        near = [(abs(w - col), i) for w, i in found if abs(w - col) <= 2]
        if near:
            out.append(line[:min(near)[1]].rstrip())
        else:
            out.append(line)
    return out


def screen_lines(text):
    lines = [ln.rstrip() for ln in ANSI_RE.sub("", text or "").replace("\r", "").split("\n")]
    return [ln.rstrip() for ln in strip_side_panel(lines)]


def _is_rule(s):
    return bool(SOLID_RULE_RE.match(s) or LABELED_RULE_RE.match(s) or DASH_RULE_RE.match(s))


def _meaningful_tail(lines, n):
    """빈 줄·가로줄을 뺀 마지막 n줄의 index(위→아래 순)."""
    out = []
    for i in range(len(lines) - 1, -1, -1):
        s = lines[i].strip()
        if not s or _is_rule(s):
            continue
        out.append(i)
        if len(out) >= n:
            break
    return list(reversed(out))


def _hint_index(lines, rx, tail=3):
    for i in reversed(_meaningful_tail(lines, tail)):
        s = lines[i].strip()
        joined = s + " " + lines[i + 1].strip() if i + 1 < len(lines) else s
        if rx.search(s) or rx.search(joined):
            return i
    return -1


def _indent(line):
    return len(line) - len(line.lstrip(" "))


def _menu_rows(lines, start, end):
    """번호 줄 메뉴. 커서(❯) 줄과 같은 열의 번호만, 마지막 '1.'부터. 이어지는 들여쓴 줄은 extra."""
    rows = []
    for i in range(max(0, start), min(end, len(lines))):
        m = NUM_ROW_RE.match(lines[i])
        if not m:
            continue
        rows.append({"line": i, "num": int(m.group(3)), "selected": bool(m.group(2)),
                     "col": lines[i].index(m.group(3), len(m.group(1))), "label": m.group(4).strip()})
    if not rows:
        return []
    anchor = ([r for r in rows if r["selected"]] or rows)[-1]
    rows = [r for r in rows if r["col"] == anchor["col"]]
    ones = [k for k, r in enumerate(rows) if r["num"] == 1]
    rows = rows[ones[-1]:] if ones else []
    if not rows or any(r["num"] != k + 1 for k, r in enumerate(rows)):
        return []
    for k, r in enumerate(rows):
        stop = rows[k + 1]["line"] if k + 1 < len(rows) else end
        extra = []
        r["submit"] = None
        for j in range(r["line"] + 1, min(stop, len(lines))):
            s = lines[j].strip()
            if not s or _is_rule(s):
                if extra:
                    break
                continue
            sm = SUBMIT_ROW_RE.match(s)
            if sm:
                r["submit"] = {"line": j, "selected": bool(sm.group(1))}
                break
            if _indent(lines[j]) <= r["col"]:
                break
            extra.append(s)
        r["extra"] = extra
        r["end"] = (r["submit"] or {}).get("line", r["line"] + len(extra))
        cb = CHECKBOX_RE.match(r["label"])
        r["checkbox"] = bool(cb)
        r["checked"] = bool(cb) and cb.group(1) != " "
        if cb:
            r["label"] = r["label"][cb.end():].strip()
    return rows


def _norm(s):
    return " ".join(s.split())


def _dedent_block(lines):
    lines = list(lines)
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return None
    ind = min(_indent(ln) for ln in lines if ln.strip())
    return "\n".join(ln[ind:] if ln.strip() else "" for ln in lines)[:MAX_PROMPT_BODY]


def _tabs_above(lines, first_row):
    """질문 위의 탭 막대(`←  ☒ 색  ☐ 과일  ✔ Submit  →`)나 단일 질문 칩(`☐ 계절`)."""
    for i in range(first_row - 1, max(-1, first_row - 40), -1):
        s = lines[i].strip()
        if SOLID_RULE_RE.match(s):
            return None, None, -1
        if TABS_RE.match(s):
            inner = s[1:].rstrip("→").strip()
            tabs = [{"label": m.group(2).strip(), "answered": m.group(1) != "☐"}
                    for m in TAB_ITEM_RE.finditer(inner) if m.group(1) != "✔"]
            return tabs, None, i
        m = CHIP_RE.match(s)
        if m and not NUM_ROW_RE.match(lines[i]):
            return None, m.group(2).strip(), i
    return None, None, -1


def _question_text(lines, stop, first_row):
    text = []
    for i in range(first_row - 1, max(stop, first_row - 30), -1):
        s = lines[i].strip()
        if not s:
            if text:
                break
            continue
        if _is_rule(s) or TABS_RE.match(s) or CHIP_RE.match(s):
            break
        text.insert(0, s)
    return _norm(" ".join(text)) or None


def prompt_id(p):
    key = [p["kind"], p["title"], p["question"], p["body"],
           [[o["label"], o["description"], o.get("number")] for o in p["options"]],
           bool(p["multi_select"]), [t["label"] for t in (p.get("tabs") or [])]]
    raw = json.dumps(key, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:12]


def _finish(kind, title, question, body, options, selected=None, multi=False, checked=None,
            free_text_index=None, tabs=None, always_index=None, reject_index=None, persist_index=None):
    """options: (label, description, 화면 번호 또는 None). 번호는 숨긴 줄(Chat about this 등)을 건너뛰어도
    화면에 찍힌 그대로라 키 계산은 index가 아니라 number로 한다."""
    p = {"kind": kind, "title": title, "question": question, "body": body,
         "options": [{"index": k, "label": o[0], "description": o[1], "number": o[2] if len(o) > 2 else None}
                     for k, o in enumerate(options)],
         "selected": selected, "multi_select": bool(multi), "checked": list(checked or []),
         "free_text_index": free_text_index, "tabs": tabs,
         "always_index": always_index, "reject_index": reject_index, "persist_index": persist_index}
    p["id"] = prompt_id(p)
    return p


def _parse_question(lines):
    h = _hint_index(lines, ASK_HINT_RE)
    if h < 0:
        return None
    rows = _menu_rows(lines, h - 64, h)
    if len(rows) < 2:
        return None
    submit = next((r["submit"] for r in rows if r["submit"]), None)
    on_submit = bool(submit and submit["selected"])
    nsel = sum(1 for r in rows if r["selected"])
    multi = any(r["checkbox"] for r in rows)
    if nsel != (0 if on_submit else 1) or (on_submit and not multi):
        return None
    # 복수 선택의 "Type something"은 체크박스 줄이라 자유 입력으로 다루지 않는다(카드에서 뺀다).
    opts = [r for r in rows if r["label"] != "Chat about this"
            and not (multi and FREE_TEXT_RE.match(r["label"]))]
    if not opts:
        return None
    tabs, chip, tabs_line = _tabs_above(lines, rows[0]["line"])
    question = _question_text(lines, tabs_line, rows[0]["line"])
    if not question:
        return None
    if tabs is not None:
        title = tabs[0]["label"] if len(tabs) == 1 else None
    else:
        title = chip
    free = next((k for k, r in enumerate(opts) if FREE_TEXT_RE.match(r["label"])), None)
    sel = next((k for k, r in enumerate(opts) if r["selected"]), None)
    return _finish("question", title, question, None,
                   [(r["label"], _norm(" ".join(r["extra"])) or None, r["num"]) for r in opts],
                   selected=sel, multi=multi, checked=[k for k, r in enumerate(opts) if r["checked"]],
                   free_text_index=free, tabs=tabs)


def _parse_submit(lines):
    tail = _meaningful_tail(lines, 4)
    head = -1
    for i in range(len(lines) - 1, max(-1, len(lines) - 80), -1):
        s = lines[i].strip()
        if s in ("Review your answers", "Ready to submit your answers?"):
            head = i
            break
    if head < 0:
        return None
    rows = _menu_rows(lines, head + 1, len(lines))
    if [r["label"] for r in rows] != ["Submit answers", "Cancel"]:
        return None
    if sum(1 for r in rows if r["selected"]) != 1:
        return None
    after = [i for i in tail if i > rows[-1]["end"]]
    if any(not ESC_HINT_RE.search(lines[i]) for i in after):
        return None
    tabs, _, _ = _tabs_above(lines, head + 1)
    body = "\n".join(ln.strip() for ln in lines[head + 1:rows[0]["line"]] if ln.strip()) or None
    return _finish("submit", None, lines[head].strip(), body, [(r["label"], None, r["num"]) for r in rows],
                   selected=next(k for k, r in enumerate(rows) if r["selected"]), tabs=tabs)


def _parse_plan(lines, plan_reader=None):
    q = -1
    for i in range(len(lines) - 1, -1, -1):
        if PLAN_Q_RE.search(lines[i]):
            q = i
            break
    if q < 0:
        return None
    rows = _menu_rows(lines, q + 1, len(lines))
    if len(rows) < 2 or sum(1 for r in rows if r["selected"]) != 1:
        return None
    covered = set()
    for r in rows:
        covered.update(range(r["line"], r["end"] + 1))
    plan_path = None
    for i in range(q + 1, len(lines)):
        s = lines[i].strip()
        if not s or _is_rule(s) or i in covered:
            continue
        m = PLAN_FILE_RE.search(s) if s.lower().startswith("ctrl+g") else None
        if not m:
            return None  # 메뉴 아래에 다른 글이 있으면 이미 지나간 메뉴다
        plan_path = m.group(1)
    title = None
    start = 0
    for i in range(q - 1, -1, -1):
        s = lines[i].strip()
        if s == "Ready to code?":
            title, start = s, i + 1
            break
    body = None
    if plan_path and plan_reader:
        body = plan_reader(plan_path)
    if not body:
        seg = lines[start:q]
        for k, ln in enumerate(seg):
            if ln.strip() == "Here is Claude's plan:":
                seg = seg[k + 1:]
                break
        body = _dedent_block([ln for ln in seg if not _is_rule(ln.strip())])
    free = next((k for k, r in enumerate(rows) if FREE_TEXT_RE.match(r["label"])), None)
    return _finish("plan", title, lines[q].strip(), body,
                   [(r["label"], _norm(" ".join(r["extra"])) or None, r["num"]) for r in rows],
                   selected=next(k for k, r in enumerate(rows) if r["selected"]), free_text_index=free)


def _approval_panel(lines, q):
    call = -1
    for i in range(q - 1, -1, -1):
        if CALL_RE.match(lines[i].strip()):
            call = i
            break
    rules = [i for i in range(call + 1, q) if SOLID_RULE_RE.match(lines[i].strip())]
    if not rules:
        return None, None
    rule = rules[0] if call >= 0 else rules[-1]
    if q - rule > 60:
        return None, None
    panel = [ln for ln in lines[rule + 1:q]
             if not ln.strip().startswith("Tip:") and not DASH_RULE_RE.match(ln.strip())]
    while panel and not panel[0].strip():
        panel.pop(0)
    if not panel:
        return None, None
    return panel[0].strip(), _dedent_block(panel[1:])


APPROVE_UNSAFE = ("switch to", "auto mode", "don't ask", "always")
REJECT_UNSAFE = ("don't ask", "never", "always")


def _safe_index(labels, word, unsafe):
    """정확히 word("Yes"/"No")인 항목, 없으면 word로 시작하되 영구 설정 문구가 없는 첫 항목, 없으면 None."""
    low = [_norm(lb).lower().replace("\u2019", "'") for lb in labels]
    for k, lb in enumerate(low):
        if lb == word:
            return k
    rx = re.compile(r"^%s\b" % word)
    for k, lb in enumerate(low):
        if rx.match(lb) and not any(u in lb for u in unsafe):
            return k
    return None


def approve_index(labels):
    return _safe_index(labels, "yes", APPROVE_UNSAFE)


def reject_index(labels):
    return _safe_index(labels, "no", REJECT_UNSAFE)


PERSIST_MARKS = ("don't ask again", "always allow", "this project", "from this project")


def allow_more_indices(labels):
    """(세션 한정 허용, 프로젝트 영구 허용) 선택지 index. auto mode 전환은 둘 다 아니다.
    세션: "Yes, and switch to accept edits … for this session", "Yes, allow all edits during this session".
    영구: "Yes, and don't ask again for … in <dir>", "Yes, and always allow access to <dir> from this project"."""
    session = persist = None
    for k, lb in enumerate(labels):
        low = _norm(lb).lower().replace("\u2019", "'")
        if not re.match(r"^yes\b", low) or low == "yes" or "auto mode" in low:
            continue
        if "this session" in low:
            if session is None:
                session = k
        elif persist is None and (low.startswith("yes, and") or any(m in low for m in PERSIST_MARKS)):
            persist = k
    return session, persist


def _parse_approval(lines):
    h = _hint_index(lines, ESC_HINT_RE)
    if h < 0:
        return None
    q = -1
    for i in range(h - 1, max(-1, h - 40), -1):
        if APPROVAL_Q_RE.match(lines[i].strip()):
            q = i
            break
    if q < 0:
        return None
    rows = _menu_rows(lines, q + 1, h)
    if len(rows) < 2 or sum(1 for r in rows if r["selected"]) != 1:
        return None
    labels = [_norm(" ".join([r["label"]] + r["extra"])) for r in rows]
    title, body = _approval_panel(lines, q)
    always, persist = allow_more_indices(labels)
    return _finish("approval", title, lines[q].strip(), body,
                   [(lb, None, r["num"]) for lb, r in zip(labels, rows)],
                   selected=next(k for k, r in enumerate(rows) if r["selected"]),
                   always_index=always, reject_index=reject_index(labels), persist_index=persist)


def _parse_confirm(lines):
    h = _hint_index(lines, CONFIRM_HINT_RE)
    if h < 0:
        return None
    end = h - 1
    while end >= 0 and not lines[end].strip():
        end -= 1
    start = end
    while start > 0 and lines[start - 1].strip() and not _is_rule(lines[start - 1].strip()):
        start -= 1
    rows = [(i, lines[i].strip()) for i in range(start, end + 1)]
    if not 2 <= len(rows) <= 9 or any(NUM_ROW_RE.match(lines[i]) for i, _ in rows):
        return None
    sel = [k for k, (_, s) in enumerate(rows) if SELECTED_MARK_RE.match(s)]
    if len(sel) != 1:
        return None
    labels = [SELECTED_MARK_RE.sub("", s).strip() for _, s in rows]
    top = start - 1
    while top >= 0 and not _is_rule(lines[top].strip()) and start - top <= 30:
        top -= 1
    panel = [ln.strip() for ln in lines[top + 1:start] if ln.strip()]
    title = panel[0].rstrip(":") if panel else None
    prose = _norm(" ".join(panel[1:]))
    m = re.search(r"(?:^|[.:!]\s+)([^.:!?]*\?)", prose)
    question = m.group(1).strip() if m else (title or "")
    body = "\n".join(panel[1:]) or None
    return _finish("confirm", title, question, body, [(lb, None) for lb in labels], selected=sel[0],
                   reject_index=reject_index(labels))


# ---- Codex CLI 메뉴. 실측: codex-cli 0.171.0, 200열(tests/fixtures/codex-*-0.171.0.txt).
# 커서는 `›`, 숫자 키가 그 항목을 바로 고르고 제출한다(승인·질문 모두). 모르는 화면은 None(카드·버튼 없음).
CODEX_APPROVAL_HINT_RE = re.compile(r"^Press enter to confirm or esc to cancel$")
CODEX_APPROVAL_Q_RE = re.compile(r"^Would you like to (.+)\?$")
CODEX_QUESTION_HINT_RE = re.compile(r"^tab to add notes \| enter to submit (?:answer|all)\b")
CODEX_QUESTION_HEAD_RE = re.compile(r"^Question (\d+)/(\d+)\b")
CODEX_SHORTCUT_RE = re.compile(r"\s+\(([a-z]|esc)\)$")
CODEX_FREE_TEXT = "None of the above"
CODEX_APPROVAL_TITLES = {"run the following command": "Run command", "make the following edits": "Edit files"}


def _codex_hint(lines, rx):
    tail = _meaningful_tail(lines, 1)
    if not tail or not rx.match(lines[tail[0]].strip()):
        return -1
    return tail[0]


def _find_above(lines, h, rx, span=40):
    for i in range(h - 1, max(-1, h - span), -1):
        if rx.match(lines[i].strip()):
            return i
    return -1


def _parse_codex_approval(lines):
    h = _codex_hint(lines, CODEX_APPROVAL_HINT_RE)
    if h < 0:
        return None
    q = _find_above(lines, h, CODEX_APPROVAL_Q_RE)
    if q < 0:
        return None
    rows = _menu_rows(lines, q + 1, h)
    if len(rows) < 2 or sum(1 for r in rows if r["selected"]) != 1:
        return None
    labels, shortcuts = [], []
    for r in rows:
        lb = _norm(" ".join([r["label"]] + r["extra"]))
        m = CODEX_SHORTCUT_RE.search(lb)
        labels.append(lb[:m.start()] if m else lb)
        shortcuts.append(m.group(1) if m else None)
    body = _dedent_block(lines[q + 1:rows[0]["line"]])
    question = lines[q].strip()
    title = CODEX_APPROVAL_TITLES.get(CODEX_APPROVAL_Q_RE.match(question).group(1).lower(), question)

    def yes_with(key):  # (a)=이 세션 동안 허용, (p)=규칙 파일(~/.codex/rules)에 영구 허용. 실측 0.171.0
        return next((k for k, (lb, s) in enumerate(zip(labels, shortcuts))
                     if s == key and re.match(r"^yes\b", lb.lower())), None)
    p = _finish("approval", title, question, body, [(lb, None, r["num"]) for lb, r in zip(labels, rows)],
                selected=next(k for k, r in enumerate(rows) if r["selected"]),
                always_index=yes_with("a"), reject_index=reject_index(labels), persist_index=yes_with("p"))
    p["agent"] = "codex"
    return p


def _parse_codex_question(lines):
    h = _codex_hint(lines, CODEX_QUESTION_HINT_RE)
    if h < 0:
        return None
    head = _find_above(lines, h, CODEX_QUESTION_HEAD_RE, span=64)
    if head < 0:
        return None
    rows = _menu_rows(lines, head + 1, h)
    if len(rows) < 2 or sum(1 for r in rows if r["selected"]) != 1:
        return None
    question = _norm(" ".join(ln.strip() for ln in lines[head + 1:rows[0]["line"]] if ln.strip()))
    if not question:
        return None
    opts = []
    for r in rows:
        parts = re.split(r"\s{2,}", r["label"], maxsplit=1)
        desc = _norm(" ".join(parts[1:] + r["extra"])) or None
        opts.append((parts[0].strip(), desc, r["num"]))
    m = CODEX_QUESTION_HEAD_RE.match(lines[head].strip())
    title = "Question %s/%s" % (m.group(1), m.group(2)) if m.group(2) != "1" else None
    free = next((k for k, o in enumerate(opts) if o[0] == CODEX_FREE_TEXT), None)
    p = _finish("question", title, question, None, opts,
                selected=next(k for k, r in enumerate(rows) if r["selected"]), free_text_index=free)
    p["agent"] = "codex"
    return p


CODEX_PLAN_HINT_RE = re.compile(r"^enter select · esc back$")
CODEX_PLAN_Q = "Implement this plan?"
CODEX_PLAN_HEAD = "• Proposed Plan"
CODEX_PLAN_WORKED_RE = re.compile(r"^Worked for .+")
CODEX_PLAN_MODE_RE = re.compile(r"\bPlan mode \(")  # 입력창 아래 줄: "Plan mode (⇧tab to cycle)"
CODEX_PLAN_SETTLE = 1.5  # 플랜 모드 턴이 끝난 뒤 메뉴를 다시 볼 때까지(초)


def _parse_codex_plan(lines):
    """플랜 모드 끝 "Implement this plan?" 메뉴(실측 0.171.0). hc는 이 화면을 blocked로 보지 않는다."""
    h = _codex_hint(lines, CODEX_PLAN_HINT_RE)
    if h < 0:
        return None
    q = _find_above(lines, h, re.compile(r"^%s$" % re.escape(CODEX_PLAN_Q)), span=8)
    if q < 0:
        return None
    rows = _menu_rows(lines, q + 1, h)
    if len(rows) != 3 or sum(1 for r in rows if r["selected"]) != 1:
        return None
    opts = []
    for r in rows:
        parts = re.split(r"\s{2,}", r["label"], maxsplit=1)
        opts.append((parts[0].strip(), _norm(" ".join(parts[1:] + r["extra"])) or None, r["num"]))
    if not (opts[0][0].lower().startswith("yes") and opts[2][0].lower().startswith("no")):
        return None
    body = None
    for i in range(q - 1, max(-1, q - 400), -1):
        if lines[i].strip() == CODEX_PLAN_HEAD:
            seg = [ln for ln in lines[i + 1:q] if not CODEX_PLAN_WORKED_RE.match(ln.strip())]
            body = _dedent_block(seg)
            break
        if lines[i].strip().startswith("› ") and not NUM_ROW_RE.match(lines[i]):
            break  # 사용자 입력 줄을 넘어가면 이번 계획이 아니다
    p = _finish("plan", None, CODEX_PLAN_Q, body, opts,
                selected=next(k for k, r in enumerate(rows) if r["selected"]), reject_index=2)
    p["agent"] = "codex"
    return p


# ---- Gemini CLI 대화 상자. 실측: gemini-cli 0.70.0, 200열(tests/fixtures/gemini-*-0.70.0.txt).
# 메뉴는 화면 맨 끝의 ╭…╰ 상자 안에 있고 커서는 `●`, 숫자 키가 그 항목을 바로 고르고 제출한다.
# 직접 입력 줄("Enter a custom value", "Type your feedback...")은 숫자로 커서만 옮기고 입력 칸이 열린다.
GEMINI_APPROVE_FIRST = "Allow once"
GEMINI_ALWAYS = "Allow for this session"
GEMINI_EDITOR_RE = re.compile(r"external editor", re.I)
GEMINI_HEAD_RE = re.compile(r"^\?\s+(\S.*)$")
GEMINI_Q_HEAD = "Answer Questions"
GEMINI_Q_HINT_RE = re.compile(r"^Enter to select · ↑/↓ to navigate · Esc to cancel$")
GEMINI_TEXT_HINT_RE = re.compile(r"^Enter to submit · Esc to cancel$")
GEMINI_TEXT_PLACEHOLDER = "> Enter your response"
GEMINI_FREE_LABELS = ("Enter a custom value", "Type your feedback...")
GEMINI_PLAN_Q = "Ready to start implementation?"
GEMINI_PLAN_HINT_RE = re.compile(r"^Enter to select · ↑/↓ to navigate · Ctrl\+G to edit plan · Esc to cancel$")
GEMINI_TITLE_BLOCKED_RE = re.compile(r"^\s*✋")  # 터미널 제목: ✋ Action Required / ✦ Working… / ◇ Ready
GEMINI_TITLE_WORKING_RE = re.compile(r"^\s*✦")


def _box_unwrap(line):
    """`│ 글자 │` → 글자(안쪽 한 칸 여백 제거). 상자 줄이 아니면 None. 오른쪽 테두리는 screen_lines의
    사이드 패널 자르기가 이미 지웠을 수 있어 없어도 된다."""
    s = line.rstrip()
    if not s.startswith("│"):
        return None
    s = s[1:-1] if s.endswith("│") and len(s) >= 2 else s[1:]
    return (s[1:] if s.startswith(" ") else s).rstrip()


def _last_box(lines):
    """화면 맨 끝(마지막 글자 줄)이 상자 아래 테두리면 그 상자의 안쪽 줄들, 아니면 None."""
    last = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].strip()), -1)
    if last < 0:
        return None
    col = lines[last].find("╰")
    if col < 0 or lines[last][:col].strip():
        return None
    top = next((i for i in range(last - 1, max(-1, last - 400), -1)
                if len(lines[i]) > col and lines[i][col] == "╭" and not lines[i][:col].strip()), -1)
    if top < 0:
        return None
    inner = []
    for ln in lines[top + 1:last]:
        u = _box_unwrap(ln[col:])
        if u is None:
            return None
        inner.append(u)
    return inner


def _gemini_rows(inner, start=0):
    conv = [("›" + ln[1:]) if ln.startswith("●") else ln for ln in inner]
    rows = _menu_rows(conv, start, len(conv))
    if not rows or sum(1 for r in rows if r["selected"]) != 1:
        return []
    return rows


def _gemini_body(lines):
    """본문 줄: 안쪽 상자(명령·차이)는 테두리를 벗긴다."""
    out = []
    for ln in lines:
        s = ln.strip()
        if s.startswith(("╭", "╰")):
            continue
        u = _box_unwrap(ln.strip())
        out.append(u if u is not None else ln)
    return _dedent_block(out)


def _parse_gemini_approval(inner):
    rows = _gemini_rows(inner)
    if len(rows) < 2 or rows[0]["label"] != GEMINI_APPROVE_FIRST or rows[0]["num"] != 1:
        return None
    if not (rows[-1]["label"].startswith("No") and rows[-1]["label"].endswith("(esc)")):
        return None
    first = rows[0]["line"]
    q = next((i for i in range(first - 1, max(-1, first - 6), -1) if inner[i].strip().endswith("?")), -1)
    head = next((i for i in range(0, q) if GEMINI_HEAD_RE.match(inner[i].strip())), -1) if q > 0 else -1
    if q < 0 or head < 0:
        return None
    rest = GEMINI_HEAD_RE.match(inner[head].strip()).group(1)
    title = re.split(r"\s{2,}", rest, maxsplit=1)[0]
    body = _gemini_body(inner[head + 1:q]) or rest
    opts, sel = [], None
    for r in rows:
        if GEMINI_EDITOR_RE.search(r["label"]):
            continue  # 외부 편집기는 PC에서만 열린다
        if r["selected"]:
            sel = len(opts)
        lb = _norm(r["label"])
        opts.append((lb[:-len("(esc)")].rstrip() if lb.endswith("(esc)") else lb, None, r["num"]))
    lbs = [o[0] for o in opts]
    p = _finish("approval", title, inner[q].strip(), body, opts, selected=sel,
                always_index=lbs.index(GEMINI_ALWAYS) if GEMINI_ALWAYS in lbs else None,
                reject_index=next((k for k, lb in enumerate(lbs) if lb.startswith("No")), None))
    p["approve_index"] = 0  # "Allow once"(rows[0]로 확인)
    p["agent"] = "gemini"
    return p


def _gemini_meaningful(inner):
    return [i for i, ln in enumerate(inner) if ln.strip()]


def _parse_gemini_question(inner):
    idx = _gemini_meaningful(inner)
    if len(idx) < 3 or inner[idx[0]].strip() != GEMINI_Q_HEAD:
        return None
    if any(inner[i].strip() in ("▲", "▼") or inner[i].strip().startswith("←") for i in idx):
        return None  # 여러 질문(탭)·스크롤되는 목록: 일부만 보여 카드로 만들지 않는다
    hint = inner[idx[-1]].strip()
    rows = _gemini_rows(inner, idx[0] + 1)
    if rows and GEMINI_Q_HINT_RE.match(hint):
        question = _norm(" ".join(inner[i].strip() for i in idx if idx[0] < i < rows[0]["line"]))
        opts = []
        for r in rows:
            opts.append((_norm(r["label"]), _norm(" ".join(r["extra"])) or None, r["num"]))
        free = next((k for k, o in enumerate(opts) if o[0] in GEMINI_FREE_LABELS), None)
        sel = next(k for k, r in enumerate(rows) if r["selected"])
    elif not rows and GEMINI_TEXT_HINT_RE.match(hint):
        ph = [i for i in idx if inner[i].strip() == GEMINI_TEXT_PLACEHOLDER]
        if len(ph) != 1:
            return None  # 이미 PC에서 입력 중
        question = _norm(" ".join(inner[i].strip() for i in idx if idx[0] < i < ph[0]))
        opts, free, sel = [(GEMINI_TEXT_PLACEHOLDER[2:], None, None)], 0, None
    else:
        return None
    if not question:
        return None
    p = _finish("question", None, question, None, opts, selected=sel, free_text_index=free)
    p["agent"] = "gemini"
    return p


def _parse_gemini_plan(inner):
    idx = _gemini_meaningful(inner)
    if len(idx) < 4 or inner[idx[0]].strip() != GEMINI_PLAN_Q or not GEMINI_PLAN_HINT_RE.match(inner[idx[-1]].strip()):
        return None
    rows = _gemini_rows(inner, idx[0] + 1)
    if len(rows) != 3 or not rows[0]["label"].startswith("Yes"):
        return None
    opts = [(_norm(r["label"]), _norm(" ".join(r["extra"])) or None, r["num"]) for r in rows]
    free = next((k for k, o in enumerate(opts) if o[0] in GEMINI_FREE_LABELS), None)
    p = _finish("plan", None, GEMINI_PLAN_Q, _gemini_body(inner[idx[0] + 1:rows[0]["line"]]), opts,
                selected=next(k for k, r in enumerate(rows) if r["selected"]), free_text_index=free)
    p["agent"] = "gemini"
    return p


def _parse_gemini_lines(lines, plan_reader=None):
    inner = _last_box(lines)
    if inner is None:
        return None
    return _parse_gemini_approval(inner) or _parse_gemini_question(inner) or _parse_gemini_plan(inner)


def _parse_codex_lines(lines, plan_reader=None):
    return _parse_codex_approval(lines) or _parse_codex_question(lines) or _parse_codex_plan(lines)


def parse_codex_prompt(screen_text):
    return _parse_codex_lines(screen_lines(screen_text))


def _parse_claude_lines(lines, plan_reader=None):
    for fn in (_parse_question, _parse_submit, _parse_approval, _parse_confirm):
        p = fn(lines)
        if p:
            return p
    return _parse_plan(lines, plan_reader)


def parse_prompt(screen_text, plan_reader=None, agent=None):
    """화면 글자 → 활성 메뉴 카드(dict) 또는 None. plan_reader(path)는 계획 파일 본문을 준다.
    agent(AGENTS 키)의 파서로 읽는다. 없거나 모르면 Claude Code 화면으로 읽는다."""
    return agent_spec(agent)["parse"](screen_lines(screen_text), plan_reader)


def make_plan_reader(home):
    """`~/.claude/plans/<이름>.md`만 읽는다(심볼릭 링크·일반 파일 아님은 거부, 12000자 상한)."""
    base = os.path.join(home, ".claude", "plans")

    def read(path):
        if not isinstance(path, str) or not PLAN_FILE_RE.fullmatch(path):
            return None
        name = path.rsplit("/", 1)[-1]
        try:
            data = read_regular_file(os.path.join(base, name), MAX_PROMPT_BODY * 4)
        except OSError:
            return None
        text = data.decode("utf-8", "replace").strip()
        return text[:MAX_PROMPT_BODY] or None
    return read


def is_reparse_point(path):
    """Windows 심볼릭 링크·정션 등(FILE_ATTRIBUTE_REPARSE_POINT). 없는 경로는 False."""
    try:
        return bool(getattr(os.lstat(path), "st_file_attributes", 0) & 0x400)
    except OSError:
        return False


def read_regular_file(path, max_bytes, tail=False):
    """O_NOFOLLOW로 열고 일반 파일만. tail이면 끝 max_bytes. 반환 bytes(잘렸으면 tail 모드에서 첫 줄 버림)."""
    if IS_WINDOWS and is_reparse_point(path):  # Windows에는 O_NOFOLLOW가 없다: 심볼릭 링크·정션은 미리 거른다
        raise OSError("reparse point")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                 | getattr(os, "O_BINARY", 0))
    try:
        st_ = os.fstat(fd)
        if not stat.S_ISREG(st_.st_mode):
            raise OSError("not a regular file")
        size = st_.st_size
        cut = False
        if tail and size > max_bytes:
            os.lseek(fd, size - max_bytes, os.SEEK_SET)
            cut = True
        chunks = []
        left = max_bytes
        while left > 0:
            b = os.read(fd, min(left, 1 << 20))
            if not b:
                break
            chunks.append(b)
            left -= len(b)
        data = b"".join(chunks)
    finally:
        os.close(fd)
    if cut:
        nl = data.find(b"\n")
        data = data[nl + 1:] if nl >= 0 else b""
    return data


def nav_keys(cur, target):
    if cur is None:
        return None
    d = target - cur
    return (["down"] * d if d > 0 else ["up"] * (-d))


def _free_text_digit(p, num, text):
    """자유 입력 줄 번호를 눌러 커서를 옮긴 뒤 붙여넣기+enter(Claude 실측 2.1.329)."""
    fi = p["free_text_index"]
    if num(fi) > 9:
        raise ApiError(400, "bad_option")
    return [("keys", [str(num(fi))]), ("text", text)]


def _free_text_codex(p, num, text):
    """Codex: 숫자는 그 항목으로 바로 제출한다. 커서를 "None of the above"로 옮기고 tab으로 메모 칸을 연 뒤
    붙여넣기+enter(실측 0.171.0: 답 "None of the above" + 메모가 간다)."""
    cur = p.get("selected")
    keys = nav_keys(None if cur is None else num(cur), num(p["free_text_index"]))
    if keys is None:
        raise ApiError(409, "no_cursor")
    return [("keys", keys + ["tab"]), ("text", text)]


def _free_text_gemini(p, num, text):
    """Gemini: 입력만 받는 질문(번호 없음)은 바로 붙여넣기+enter, 그 밖에는 Claude처럼 번호 → 붙여넣기."""
    if p["options"][p["free_text_index"]].get("number") is None:
        return [("text", text)]
    return _free_text_digit(p, num, text)


def answer_steps(p, option_index=None, option_indices=None, text=None):
    """카드에 대한 답 → [("keys",[...]) | ("text", s)].
    실측(2.1.329): 숫자 키는 승인·계획·단일 선택·리뷰에서 바로 고른다. 자유 입력 줄은 숫자로 커서만 옮기고,
    붙여넣기+enter(hc send --force)로 보낸다. 복수 선택은 숫자가 토글, right가 다음 탭/리뷰로 간다."""
    n = len(p["options"])
    kind = p["kind"]

    def num(i):  # 화면 번호(번호 없는 confirm은 순서)
        v = p["options"][i].get("number")
        return v if v is not None else i + 1
    if text is not None:
        fi = p.get("free_text_index")
        if fi is None or p["multi_select"]:
            raise ApiError(400, "no_free_text")
        if "\n" in text or "\t" in text:
            raise ApiError(400, "bad_text")
        return agent_spec(p.get("agent"))["free_text"](p, num, text)
    if option_indices is not None:
        if not p["multi_select"]:
            raise ApiError(400, "not_multi_select")
        want = set(option_indices)
        if any(not 0 <= i < n for i in want):
            raise ApiError(400, "bad_option")
        if p.get("free_text_index") in want:
            raise ApiError(400, "no_free_text")
        if any(num(i) > 9 for i in want):
            raise ApiError(400, "bad_option")
        toggle = sorted(want.symmetric_difference(set(p["checked"])) - {p.get("free_text_index")})
        return [("keys", [str(num(i)) for i in toggle] + ["right"])]
    if option_index is None:
        raise ApiError(400, "bad_answer")
    if not 0 <= option_index < n:
        raise ApiError(400, "bad_option")
    if p["multi_select"]:
        raise ApiError(400, "use_option_indices")
    if option_index == p.get("free_text_index"):
        raise ApiError(400, "needs_text")
    if kind == "confirm" or num(option_index) > 9:
        cur = p.get("selected")
        keys = nav_keys(None if cur is None else num(cur), num(option_index))
        if keys is None:
            raise ApiError(409, "no_cursor")
        return [("keys", keys + ["enter"])]
    return [("keys", [str(num(option_index))])]


# ---------------------------------------------------------------- 대화 기록(jsonl)

HIDDEN_USER_PREFIXES = ("<command-", "<local-command", "<task-")
MAX_PART = 8000
TRANSCRIPT_TAIL = 4 * 1024 * 1024
SESSION_ID_RE = re.compile(r"^[0-9A-Za-z-]{8,64}$")


def _cap(s, n=MAX_PART):
    return s if len(s) <= n else s[:n]


def _cap_obj(v):
    if isinstance(v, str):
        return _cap(v)
    if isinstance(v, list):
        return [_cap_obj(x) for x in v[:200]]
    if isinstance(v, dict):
        return {str(k): _cap_obj(x) for k, x in list(v.items())[:100]}
    return v


def _tilde(s):
    """홈 아래 절대 경로를 ~/...로 줄인다(폰 화면 폭이 좁아 /Users/<이름>/ 부분이 경로 끝을 밀어낸다)."""
    seps = "\\/" if IS_WINDOWS else os.sep  # Windows는 도구가 "/"로 보낸 경로도 있다. 드라이브 문자 대소문자는 무시
    fold = str.lower if IS_WINDOWS else (lambda x: x)
    for h in {os.path.expanduser("~"), home_real()}:
        h = h.rstrip(seps)
        if h and (fold(s) == fold(h) or (len(s) > len(h) and fold(s[:len(h)]) == fold(h) and s[len(h)] in seps)):
            return "~" + s[len(h):]
    return s


def _tool_summary(name, inp):
    if not isinstance(inp, dict):
        return ""
    for k in ("command", "file_path", "path", "pattern", "url", "query", "description", "prompt"):
        v = inp.get(k)
        if isinstance(v, str) and v.strip():
            v = v.strip().split("\n", 1)[0]
            if k in ("file_path", "path"):
                v = _tilde(v)
            return _cap(v, 200)
    for v in inp.values():
        if isinstance(v, str) and v.strip():
            return _cap(v.strip().split("\n", 1)[0], 200)
    qs = inp.get("questions")  # AskUserQuestion
    if isinstance(qs, list) and qs and isinstance(qs[0], dict) and isinstance(qs[0].get("question"), str):
        return _cap(qs[0]["question"], 200)
    return ""


def _result_text(c):
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        out = []
        for x in c:
            if isinstance(x, dict) and isinstance(x.get("text"), str):
                out.append(x["text"])
            elif isinstance(x, dict) and x.get("type"):
                out.append("[%s]" % x.get("type"))
        return "\n".join(out)
    return ""


def _strip_ansi(text):
    """도구 출력(Bash 결과 등)의 터미널 제어 문자를 지운다. 화면 읽기와 같은 ANSI_RE + 남은 ESC·\r."""
    text = re.sub(r"\x1b[()*+][0-9A-Za-z]", "", ANSI_RE.sub("", text))  # 문자 집합 지정(ESC ( B)
    return text.replace("\x1b", "").replace("\r\n", "\n").replace("\r", "\n")


def _hidden(text):
    return text.lstrip().startswith(HIDDEN_USER_PREFIXES)


CODEX_HIDDEN_USER_PREFIXES = ("<", "# AGENTS.md instructions")


def _codex_add(turns, role, parts):
    if not parts:
        return
    if role == "assistant" and turns and turns[-1]["role"] == "assistant":
        turns[-1]["parts"].extend(parts)
    else:
        turns.append({"role": role, "parts": parts})


def _codex_record(p, turns, tools):
    """Codex rollout의 response_item 하나를 Claude 대화와 같은 turns 모양으로 더한다.
    message(user/assistant, developer는 숨김), reasoning 요약, function_call·custom_tool_call과 그 output."""
    if not isinstance(p, dict):
        return
    pt = p.get("type")
    if pt == "message":
        role = p.get("role")
        if role not in ("user", "assistant"):
            return
        parts = []
        for c in p.get("content") if isinstance(p.get("content"), list) else []:
            t = c.get("text") if isinstance(c, dict) else None
            if not isinstance(t, str) or not t.strip():
                continue
            if role == "user" and t.lstrip().startswith(CODEX_HIDDEN_USER_PREFIXES):
                continue  # 환경·지침 주입(<environment_context>, AGENTS.md 등)
            parts.append({"kind": "text", "text": _cap(t)})
        _codex_add(turns, role, parts)
    elif pt == "reasoning":
        summ = p.get("summary") if isinstance(p.get("summary"), list) else []
        text = "\n\n".join(s["text"] for s in summ if isinstance(s, dict) and isinstance(s.get("text"), str)).strip()
        if text:
            _codex_add(turns, "assistant", [{"kind": "thinking", "text": _cap(text)}])
    elif pt in ("function_call", "custom_tool_call"):
        name = p.get("name") if isinstance(p.get("name"), str) else "?"
        if pt == "function_call":
            try:
                inp = json.loads(p.get("arguments") or "{}")
            except (TypeError, ValueError):
                inp = {"arguments": p.get("arguments")}
            if not isinstance(inp, dict):
                inp = {"arguments": inp}
        else:
            inp = {"input": p.get("input") if isinstance(p.get("input"), str) else ""}
        summary = _tool_summary(name, {"command": inp["cmd"]} if isinstance(inp.get("cmd"), str) else inp)
        t = {"kind": "tool", "name": name, "summary": summary, "input": _cap_obj(inp), "output": None,
             "state": "pending"}
        if isinstance(p.get("call_id"), str):
            tools[p["call_id"]] = t
        _codex_add(turns, "assistant", [t])
    elif pt in ("function_call_output", "custom_tool_call_output"):
        t = tools.get(p.get("call_id"))
        if t is not None:
            out = p.get("output")
            if isinstance(out, dict):
                out = out.get("content")
            t["output"] = _cap(_strip_ansi(_result_text(out)))
            t["state"] = "ok"


GEMINI_HIDDEN_USER_PREFIXES = ("<session_context>",)


def _gemini_tool_result(tc):
    res = tc.get("result")
    for part in res if isinstance(res, list) else []:
        fr = part.get("functionResponse") if isinstance(part, dict) else None
        resp = fr.get("response") if isinstance(fr, dict) else None
        if isinstance(resp, dict):
            v = resp.get("output", resp.get("error"))
            if isinstance(v, str):
                return v
            if v is not None:
                return json.dumps(v, ensure_ascii=False)
    return None


def _parse_gemini_transcript(recs, limit):
    """Gemini chats jsonl(실측 0.70.0): 같은 id 줄이 다시 오면 갱신, $patch는 content 갱신, $set은 메타.
    user(content 부분 목록, functionResponse는 도구 결과라 건너뜀), gemini(content·thoughts·toolCalls)."""
    by_id, order = {}, []
    for rec in recs:
        patch = rec.get("$patch")
        if isinstance(patch, dict):
            for u in patch.get("updates") if isinstance(patch.get("updates"), list) else []:
                if isinstance(u, dict) and u.get("id") in by_id:
                    by_id[u["id"]] = dict(by_id[u["id"]], **{k: v for k, v in u.items() if k != "id"})
            continue
        rid = rec.get("id")
        if not isinstance(rid, str) or rec.get("type") not in ("user", "gemini"):
            continue
        if rid not in by_id:
            order.append(rid)
        by_id[rid] = rec
    turns = []
    for rid in order:
        rec = by_id[rid]
        content = rec.get("content")
        if rec["type"] == "user":
            parts = []
            for c in content if isinstance(content, list) else [{"text": content}]:
                t = c.get("text") if isinstance(c, dict) else None
                if isinstance(t, str) and t.strip() and not t.lstrip().startswith(GEMINI_HIDDEN_USER_PREFIXES):
                    parts.append({"kind": "text", "text": _cap(t)})
            if parts:
                turns.append({"role": "user", "parts": parts})
            continue
        parts = []
        for th in rec.get("thoughts") if isinstance(rec.get("thoughts"), list) else []:
            if isinstance(th, dict):
                t = "\n".join(x for x in (th.get("subject"), th.get("description")) if isinstance(x, str) and x.strip())
                if t.strip():
                    parts.append({"kind": "thinking", "text": _cap(t)})
        if isinstance(content, str):
            texts = [content]
        else:
            texts = [c.get("text") for c in content if isinstance(c, dict)] if isinstance(content, list) else []
        for t in texts:
            if isinstance(t, str) and t.strip():
                parts.append({"kind": "text", "text": _cap(t)})
        for tc in rec.get("toolCalls") if isinstance(rec.get("toolCalls"), list) else []:
            if not isinstance(tc, dict):
                continue
            name = tc.get("name") if isinstance(tc.get("name"), str) else "?"
            inp = tc.get("args") if isinstance(tc.get("args"), dict) else {}
            out = _gemini_tool_result(tc)
            status = tc.get("status")
            state = "ok" if status == "success" else ("error" if status in ("error", "cancelled") else "pending")
            parts.append({"kind": "tool", "name": name, "summary": _tool_summary(name, inp), "input": _cap_obj(inp),
                          "output": _cap(_strip_ansi(out)) if out is not None else None, "state": state})
        if not parts:
            continue
        if turns and turns[-1]["role"] == "assistant":
            turns[-1]["parts"].extend(parts)
        else:
            turns.append({"role": "assistant", "parts": parts})
    cut = len(turns) > limit
    return turns[-limit:] if limit else turns, cut


def _is_gemini_record(rec):
    return rec.get("type") == "gemini" or "$set" in rec or "$patch" in rec or "projectHash" in rec


def parse_transcript(lines, limit=50):
    """Claude Code 세션 jsonl(또는 Codex rollout jsonl, Gemini chats jsonl) 줄들 → (turns, 잘렸는지)."""
    recs = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            recs.append(rec)
    if any(_is_gemini_record(r) for r in recs[:50]):
        return _parse_gemini_transcript(recs, limit)
    turns = []
    tools = {}
    for rec in recs:
        if rec.get("isMeta") or rec.get("isSidechain"):
            continue
        typ = rec.get("type")
        if typ == "response_item":  # Codex rollout 줄
            _codex_record(rec.get("payload"), turns, tools)
            continue
        if typ not in ("user", "assistant"):
            continue
        msg = rec.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if typ == "user":
            parts = []
            if isinstance(content, str):
                if content.strip() and not _hidden(content):
                    parts.append({"kind": "text", "text": _cap(content)})
            elif isinstance(content, list):
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    if c.get("type") == "text" and isinstance(c.get("text"), str):
                        if c["text"].strip() and not _hidden(c["text"]):
                            parts.append({"kind": "text", "text": _cap(c["text"])})
                    elif c.get("type") == "tool_result":
                        t = tools.get(c.get("tool_use_id"))
                        if t is not None:
                            t["output"] = _cap(_strip_ansi(_result_text(c.get("content"))))
                            t["state"] = "error" if c.get("is_error") else "ok"
            if parts:
                turns.append({"role": "user", "parts": parts})
            continue
        parts = []
        for c in content if isinstance(content, list) else []:
            if not isinstance(c, dict):
                continue
            ct = c.get("type")
            if ct == "text" and isinstance(c.get("text"), str) and c["text"].strip():
                parts.append({"kind": "text", "text": _cap(c["text"])})
            elif ct == "thinking" and isinstance(c.get("thinking"), str) and c["thinking"].strip():
                parts.append({"kind": "thinking", "text": _cap(c["thinking"])})
            elif ct == "tool_use":
                name = c.get("name") if isinstance(c.get("name"), str) else "?"
                inp = c.get("input") if isinstance(c.get("input"), dict) else {}
                t = {"kind": "tool", "name": name, "summary": _tool_summary(name, inp),
                     "input": _cap_obj(inp), "output": None, "state": "pending"}
                if isinstance(c.get("id"), str):
                    tools[c["id"]] = t
                parts.append(t)
        if not parts:
            continue
        if turns and turns[-1]["role"] == "assistant":
            turns[-1]["parts"].extend(parts)
        else:
            turns.append({"role": "assistant", "parts": parts})
    cut = len(turns) > limit
    return turns[-limit:] if limit else turns, cut


def ps_lstart(pid, runner=None):
    runner = runner or subprocess.run
    env = dict(os.environ, LC_ALL="C", TZ="UTC")
    try:
        p = runner(["/bin/ps", "-o", "lstart=", "-p", str(int(pid))], capture_output=True, text=True,
                   timeout=HC_TIMEOUT, shell=False, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        return None
    return _norm(p.stdout or "") or None


def proc_create_filetime(pid):
    """Windows: 프로세스 생성 시각 FILETIME(1601년부터 100ns, 정수). 못 얻으면 None. ~/.claude/sessions의 procStart와 같은 값."""
    if not IS_WINDOWS:
        return None
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        k32.GetProcessTimes.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        h = k32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return None
        try:
            c, e, k, u = (wintypes.FILETIME() for _ in range(4))
            if not k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
                return None
            return (c.dwHighDateTime << 32) | c.dwLowDateTime
        finally:
            k32.CloseHandle(h)
    except (OSError, ValueError, AttributeError, ImportError):
        return None


def _proc_start_matches(proc_start, pid, runner, filetime):
    if IS_WINDOWS:
        ft = (filetime or proc_create_filetime)(pid)
        return ft is not None and proc_start.strip() == str(ft)
    return _norm(proc_start) == ps_lstart(pid, runner)


def find_transcript(info, home, runner=None, filetime=None):
    """hc get 결과 → (session_id, jsonl 경로) 또는 (None, None).
    pid → ~/.claude/sessions/<pid>.json(interactive, procStart가 ps lstart와 같아야 함) → projects/<cwd 변환>/<id>.jsonl."""
    info = info or {}
    pid = info.get("pid")
    if not isinstance(pid, int):
        pid = (info.get("foreground") or {}).get("pid") if isinstance(info.get("foreground"), dict) else None
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None, None
    try:
        raw = read_regular_file(os.path.join(home, ".claude", "sessions", "%d.json" % pid), 256 * 1024)
        sess = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None, None
    if not isinstance(sess, dict) or sess.get("kind") != "interactive":
        return None, None
    sid = sess.get("sessionId")
    if not isinstance(sid, str) or not SESSION_ID_RE.match(sid):
        return None, None
    proc_start = sess.get("procStart")
    if not isinstance(proc_start, str) or not _proc_start_matches(proc_start, pid, runner, filetime):
        return None, None  # pid가 다른 프로세스로 재사용됐다
    cwd = sess.get("cwd") if isinstance(sess.get("cwd"), str) else live_cwd(info)
    projects = os.path.join(home, ".claude", "projects")
    if isinstance(cwd, str):
        path = os.path.join(projects, re.sub(r"[^A-Za-z0-9]", "-", cwd), sid + ".jsonl")
        if os.path.isfile(path) and not os.path.islink(path):
            return sid, path
    try:
        for d in sorted(os.listdir(projects)):
            path = os.path.join(projects, d, sid + ".jsonl")
            if os.path.isfile(path) and not os.path.islink(path):
                return sid, path
    except OSError:
        pass
    return sid, None


CODEX_ROLLOUT_RE = re.compile(r"^rollout-[0-9T:-]+-([0-9a-fA-F-]{8,64})\.jsonl$")
CODEX_SCAN_DAYS = 3


def codex_sessions_dir(home):
    env = os.environ.get("CODEX_HOME")
    return os.path.join(os.path.expanduser(env) if env else os.path.join(home, ".codex"), "sessions")


def _codex_rollout(path, base):
    """base(sessions) 아래 일반 파일인 rollout-*.jsonl이면 (session_id, path)."""
    m = CODEX_ROLLOUT_RE.match(os.path.basename(path))
    if not m:
        return None
    real = os.path.realpath(path)
    root = os.path.realpath(base)
    if not real.startswith(root + os.sep) or os.path.islink(path) or not os.path.isfile(path):
        return None
    return m.group(1), path


def _lsof_rollouts(pid, base, runner):
    """그 프로세스가 열어 둔 rollout 파일(맥·리눅스). Codex는 세션 동안 rollout을 열어 둔다(실측 0.171.0)."""
    exe = next((p for p in ("/usr/sbin/lsof", "/usr/bin/lsof") if os.path.exists(p)), None)
    if not exe:
        return None
    try:
        r = runner([exe, "-a", "-p", str(int(pid)), "-Fn"], capture_output=True, text=True,
                   timeout=HC_TIMEOUT, shell=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    for ln in (r.stdout or "").splitlines():
        if ln.startswith("n"):
            hit = _codex_rollout(ln[1:], base)
            if hit:
                return hit
    return None


def _rollout_cwd(path):
    try:
        data = read_regular_file(path, 512 * 1024)
    except OSError:
        return None
    first = data.split(b"\n", 1)[0]
    try:
        rec = json.loads(first.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    p = rec.get("payload") if isinstance(rec, dict) and rec.get("type") == "session_meta" else None
    return p.get("cwd") if isinstance(p, dict) and isinstance(p.get("cwd"), str) else None


def _latest_rollout_in(cwd, base):
    """열린 파일을 못 볼 때(Windows 등): 최근 며칠 rollout 중 같은 폴더에서 시작한 가장 최근 수정 파일."""
    if not isinstance(cwd, str):
        return None
    want = os.path.normcase(os.path.realpath(cwd))
    cands = []
    try:
        for y in sorted(os.listdir(base), reverse=True)[:2]:
            for mo in sorted(os.listdir(os.path.join(base, y)), reverse=True)[:2]:
                for d in sorted(os.listdir(os.path.join(base, y, mo)), reverse=True)[:CODEX_SCAN_DAYS]:
                    folder = os.path.join(base, y, mo, d)
                    for name in os.listdir(folder):
                        hit = _codex_rollout(os.path.join(folder, name), base)
                        if hit:
                            cands.append((os.stat(hit[1]).st_mtime, hit))
    except OSError:
        pass
    for _, hit in sorted(cands, reverse=True)[:30]:
        c = _rollout_cwd(hit[1])
        if c and os.path.normcase(os.path.realpath(c)) == want:
            return hit
    return None


def find_codex_transcript(info, home, runner=None):
    """Codex 터미널 → (session_id, rollout 경로) 또는 (None, None)."""
    info = info or {}
    runner = runner or subprocess.run
    base = codex_sessions_dir(home)
    pid = info.get("pid")
    if not isinstance(pid, int):
        fg = info.get("foreground")
        pid = fg.get("pid") if isinstance(fg, dict) else None
    hit = None
    if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 and not IS_WINDOWS:
        hit = _lsof_rollouts(pid, base, runner)
    if hit is None:
        hit = _latest_rollout_in(live_cwd(info), base)
    return hit if hit else (None, None)


GEMINI_SESSION_RE = re.compile(r"^session-[0-9T-]+-[0-9a-fA-F]{4,64}\.jsonl$")


def gemini_dir(home):
    env = os.environ.get("GEMINI_CLI_HOME")
    return os.path.join(os.path.expanduser(env) if env else home, ".gemini")


def _gemini_project_dirs(base, cwd):
    """그 폴더의 기록 폴더 후보: projects.json의 짧은 이름, 없으면 tmp/*/.project_root가 같은 폴더."""
    want = {cwd, os.path.realpath(cwd)}
    out = []
    try:
        data = json.loads(read_regular_file(os.path.join(base, "projects.json"), 1 << 20).decode("utf-8", "replace"))
        projects = data.get("projects") if isinstance(data, dict) else None
        for k, v in (projects or {}).items():
            if isinstance(k, str) and isinstance(v, str) and v and k in want:
                out.append(v)
    except (OSError, ValueError):
        pass
    if not out:
        try:
            for name in os.listdir(os.path.join(base, "tmp")):
                try:
                    root = read_regular_file(os.path.join(base, "tmp", name, ".project_root"), 4096)
                except OSError:
                    continue
                if root.decode("utf-8", "replace").strip() in want:
                    out.append(name)
        except OSError:
            pass
    return [n for n in out if n not in (".", "..") and "/" not in n and "\\" not in n]


def _jsonl_first(path):
    try:
        head = read_regular_file(path, 64 * 1024).decode("utf-8", "replace").split("\n", 1)[0]
        rec = json.loads(head)
    except (OSError, ValueError):
        return {}
    return rec if isinstance(rec, dict) else {}


def find_gemini_transcript(info, home, runner=None):
    """Gemini 터미널 → (session_id, 기록 경로). 실측 0.70.0: ~/.gemini/tmp/<이름>/chats/session-*.jsonl,
    이름은 ~/.gemini/projects.json이 폴더마다 정한다. 세션 동안 파일을 열어 두지 않아 그 폴더의 가장 최근 파일을 본다."""
    cwd = live_cwd(info)
    if not cwd:
        return None, None
    base = gemini_dir(home)
    best = None
    for name in _gemini_project_dirs(base, cwd):
        chats = os.path.join(base, "tmp", name, "chats")
        try:
            names = os.listdir(chats)
        except OSError:
            continue
        for f in names:
            if not GEMINI_SESSION_RE.match(f):
                continue
            path = os.path.join(chats, f)
            try:
                st_ = os.stat(path, follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISREG(st_.st_mode) and (best is None or st_.st_mtime > best[0]):
                best = (st_.st_mtime, path)
    if best is None:
        return None, None
    sid = _jsonl_first(best[1]).get("sessionId")
    return (sid if isinstance(sid, str) and SESSION_ID_RE.match(sid) else None), best[1]


def find_agent_transcript(info, home, runner=None):
    return agent_spec(agent_kind(info))["transcript"](info, home, runner=runner)


def read_transcript(path, limit=50):
    data = read_regular_file(path, TRANSCRIPT_TAIL, tail=True)
    try:
        size = os.stat(path, follow_symlinks=False).st_size
    except OSError:
        size = 0
    turns, cut = parse_transcript(data.decode("utf-8", "replace").split("\n"), limit)
    return turns, cut or size > TRANSCRIPT_TAIL


# ---------------------------------------------------------------- 목록 미리보기(마지막 응답)

PREVIEW_MAX = 160
PREVIEW_TAIL = 256 * 1024
PREVIEW_SESSION_TTL = 30.0
_preview_by_file = {}  # jsonl 경로 → ((mtime_ns, size), preview)
_preview_by_pid = {}   # pid → (만료 시각, (sid, path))


def preview_clean(text, limit=PREVIEW_MAX):
    """마크다운 기호를 대충 걷고 공백을 한 칸으로 합쳐 limit자 안팎으로 자른다."""
    t = re.sub(r"```[^\n]*", " ", text)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"(?m)^\s{0,3}(#{1,6}|>|[-*+]|\d+[.)])\s+", "", t)
    t = re.sub(r"(\*\*|__|~~)", "", t)
    t = re.sub(r"(?m)^\s*([-*_]\s*){3,}$", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > limit:
        t = t[:limit].rstrip() + "…"
    return t or None


def preview_from_lines(lines):
    """jsonl 줄들에서 마지막 assistant 텍스트(thinking·tool 제외)."""
    turns, _ = parse_transcript(lines, 0)
    for turn in reversed(turns):
        if turn["role"] != "assistant":
            continue
        texts = [p["text"] for p in turn["parts"] if p.get("kind") == "text"]
        if texts:
            return preview_clean(texts[-1])
    return None


def preview_for_path(path):
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        return None
    key = (st.st_mtime_ns, st.st_size)
    hit = _preview_by_file.get(path)
    if hit and hit[0] == key:
        return hit[1]
    try:
        data = read_regular_file(path, PREVIEW_TAIL, tail=True)
    except OSError:
        return None
    pv = preview_from_lines(data.decode("utf-8", "replace").split("\n"))
    if len(_preview_by_file) > 200:
        _preview_by_file.clear()
    _preview_by_file[path] = (key, pv)
    return pv


def terminal_preview(info, home, runner=None, now=None):
    """Claude 터미널 한 개의 미리보기. 실패하면 None(목록 전체에 영향 없음)."""
    try:
        pid = (info or {}).get("pid")
        if not isinstance(pid, int):
            fg = (info or {}).get("foreground")
            pid = fg.get("pid") if isinstance(fg, dict) else None
        now = time.monotonic() if now is None else now
        hit = _preview_by_pid.get(pid) if isinstance(pid, int) else None
        if hit and hit[0] > now:
            sid, path = hit[1]
        else:
            sid, path = find_agent_transcript(info, home, runner=runner)
            if isinstance(pid, int):
                if len(_preview_by_pid) > 200:
                    _preview_by_pid.clear()
                _preview_by_pid[pid] = (now + PREVIEW_SESSION_TTL, (sid, path))
        return preview_for_path(path) if path else None
    except Exception:
        return None


# ---------------------------------------------------------------- 폴더

# 홈 바로 아래에서 폴더 목록에 안 보이는 것(Finder도 숨긴다. 에이전트를 여기서 시작할 일이 없다)
def hidden_home_dirs():
    return frozenset({"Library", "AppData"}) if IS_WINDOWS else frozenset({"Library"})


HIDDEN_HOME_DIRS = hidden_home_dirs()


def home_real():
    return os.path.realpath(os.path.expanduser("~"))


def resolve_home_path(rel, home):
    """홈 기준 상대 경로 → 홈 아래 실제 폴더 절대 경로. 숨김 요소, 절대 경로, 홈 밖(심볼릭 링크 포함)은 400."""
    if rel is None:
        rel = ""
    if not isinstance(rel, str) or len(rel) > 1024 or "\x00" in rel or rel.startswith("/"):
        raise ApiError(400, "bad_path")
    if IS_WINDOWS and ("\\" in rel or ":" in rel):  # 앱은 "/" 구분만 보낸다. 역슬래시·드라이브 문자·대체 스트림 거부
        raise ApiError(400, "bad_path")
    parts = [x for x in rel.split("/") if x]
    if any(x.startswith(".") for x in parts):
        raise ApiError(400, "bad_path")
    p = os.path.realpath(os.path.join(home, *parts)) if parts else home
    if p != home and not p.startswith(home.rstrip(os.sep) + os.sep):
        raise ApiError(400, "bad_path")
    if p != home and rel_to_home(p, home) is None:  # 심볼릭 링크로 숨김 폴더에 들어간 경우
        raise ApiError(400, "bad_path")
    if not os.path.isdir(p):
        raise ApiError(400, "bad_path")
    return p


def rel_to_home(p, home):
    if p == home:
        return ""
    if not p.startswith(home.rstrip(os.sep) + os.sep):
        return None
    r = os.path.relpath(p, home)
    if IS_WINDOWS:  # 앱과는 항상 "/" 구분
        r = r.replace("\\", "/")
    if any(x.startswith(".") for x in r.split("/" if IS_WINDOWS else os.sep)):
        return None
    return r


# ---------------------------------------------------------------- QR(페어링)
#
# 표준 라이브러리만 쓰는 QR 인코더: 바이트 모드, 오류 정정 M, 버전 1~10, 마스크 8개 중 벌점 최소.
# 알고리즘은 Project Nayuki "QR Code generator library"(MIT, https://www.nayuki.io/page/qr-code-generator-library)를 따랐다.

QR_ECC_M_PER_BLOCK = [None, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26]
QR_ECC_M_BLOCKS = [None, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5]
QR_MAX_VERSION = 10


def _qr_raw_modules(ver):
    r = (16 * ver + 128) * ver + 64
    if ver >= 2:
        na = ver // 7 + 2
        r -= (25 * na - 10) * na - 55
        if ver >= 7:
            r -= 36
    return r


def _qr_data_codewords(ver):
    return _qr_raw_modules(ver) // 8 - QR_ECC_M_PER_BLOCK[ver] * QR_ECC_M_BLOCKS[ver]


def _gf_mul(x, y):
    z = 0
    for i in range(7, -1, -1):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree):
    res = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            res[j] = _gf_mul(res[j], root)
            if j + 1 < degree:
                res[j] ^= res[j + 1]
        root = _gf_mul(root, 0x02)
    return res


def _rs_remainder(data, div):
    res = [0] * len(div)
    for b in data:
        factor = b ^ res.pop(0)
        res.append(0)
        for i, coef in enumerate(div):
            res[i] ^= _gf_mul(coef, factor)
    return res


def _qr_align_positions(ver, size):
    if ver == 1:
        return []
    na = ver // 7 + 2
    step = (ver * 8 + na * 3 + 5) // (na * 4 - 4) * 2
    return list(reversed([size - 7 - i * step for i in range(na - 1)] + [6]))


def qr_encode(data):
    """bytes → 모듈 행렬(list[list[bool]], True=어두움)."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    ver = None
    for v in range(1, QR_MAX_VERSION + 1):
        ccbits = 8 if v <= 9 else 16
        if 4 + ccbits + 8 * len(data) <= _qr_data_codewords(v) * 8:
            ver = v
            break
    if ver is None:
        raise ValueError("data too long for QR version %d" % QR_MAX_VERSION)
    bits = []

    def put(val, n):
        for i in range(n - 1, -1, -1):
            bits.append((val >> i) & 1)
    put(0x4, 4)
    put(len(data), 8 if ver <= 9 else 16)
    for b in data:
        put(b, 8)
    cap = _qr_data_codewords(ver) * 8
    put(0, min(4, cap - len(bits)))
    put(0, (-len(bits)) % 8)
    pad = 0xEC
    while len(bits) < cap:
        put(pad, 8)
        pad ^= 0xEC ^ 0x11
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    # 블록 나누기·오류 정정·섞기
    nblocks = QR_ECC_M_BLOCKS[ver]
    ecclen = QR_ECC_M_PER_BLOCK[ver]
    raw = _qr_raw_modules(ver) // 8
    nshort = nblocks - raw % nblocks
    shortlen = raw // nblocks
    div = _rs_divisor(ecclen)
    blocks = []
    k = 0
    for i in range(nblocks):
        dat = words[k:k + shortlen - ecclen + (0 if i < nshort else 1)]
        k += len(dat)
        ecc = _rs_remainder(dat, div)
        if i < nshort:
            dat = dat + [0]
        blocks.append(dat + ecc)
    final = []
    for i in range(len(blocks[0])):
        for j, blk in enumerate(blocks):
            if i != shortlen - ecclen or j >= nshort:
                final.append(blk[i])
    size = ver * 4 + 17
    mod = [[False] * size for _ in range(size)]
    fn = [[False] * size for _ in range(size)]

    def setf(x, y, dark):
        mod[y][x] = dark
        fn[y][x] = True
    for i in range(size):
        setf(6, i, i % 2 == 0)
        setf(i, 6, i % 2 == 0)
    for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                x, y = cx + dx, cy + dy
                if 0 <= x < size and 0 <= y < size:
                    setf(x, y, max(abs(dx), abs(dy)) not in (2, 4))
    al = _qr_align_positions(ver, size)
    for i, ax in enumerate(al):
        for j, ay in enumerate(al):
            if (i == 0 and j == 0) or (i == 0 and j == len(al) - 1) or (i == len(al) - 1 and j == 0):
                continue
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    setf(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)

    def draw_format(mask):
        d = (0 << 3) | mask  # ECC M의 format bits = 0
        rem = d
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        b = ((d << 10) | rem) ^ 0x5412
        bit = lambda i: ((b >> i) & 1) != 0  # noqa: E731
        for i in range(6):
            setf(8, i, bit(i))
        setf(8, 7, bit(6))
        setf(8, 8, bit(7))
        setf(7, 8, bit(8))
        for i in range(9, 15):
            setf(14 - i, 8, bit(i))
        for i in range(8):
            setf(size - 1 - i, 8, bit(i))
        for i in range(8, 15):
            setf(8, size - 15 + i, bit(i))
        setf(8, size - 8, True)
    draw_format(0)
    if ver >= 7:
        rem = ver
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        vb = (ver << 12) | rem
        for i in range(18):
            dark = ((vb >> i) & 1) != 0
            a, b2 = size - 11 + i % 3, i // 3
            setf(a, b2, dark)
            setf(b2, a, dark)
    i = 0
    right = size - 1
    while right >= 1:
        if right == 6:
            right = 5
        for vert in range(size):
            for j in range(2):
                x = right - j
                upward = ((right + 1) & 2) == 0
                y = size - 1 - vert if upward else vert
                if not fn[y][x] and i < len(final) * 8:
                    mod[y][x] = ((final[i >> 3] >> (7 - (i & 7))) & 1) != 0
                    i += 1
        right -= 2
    masks = [
        lambda x, y: (x + y) % 2 == 0, lambda x, y: y % 2 == 0, lambda x, y: x % 3 == 0,
        lambda x, y: (x + y) % 3 == 0, lambda x, y: (x // 3 + y // 2) % 2 == 0,
        lambda x, y: x * y % 2 + x * y % 3 == 0, lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
        lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
    ]
    base = [row[:] for row in mod]
    best = None
    for m in range(8):
        for y in range(size):
            for x in range(size):
                mod[y][x] = base[y][x] ^ (not fn[y][x] and masks[m](x, y))
        draw_format(m)
        pen = _qr_penalty(mod)
        if best is None or pen < best[0]:
            best = (pen, m, [row[:] for row in mod])
    return best[2]


def _qr_penalty(mod):
    size = len(mod)
    pen = 0
    lines = [list(r) for r in mod] + [[mod[y][x] for y in range(size)] for x in range(size)]
    pat1 = [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0]
    pat2 = pat1[::-1]
    for ln in lines:
        run = 1
        for k in range(1, size + 1):
            if k < size and ln[k] == ln[k - 1]:
                run += 1
            else:
                if run >= 5:
                    pen += 3 + run - 5
                run = 1
        padded = [0] * 4 + [int(v) for v in ln] + [0] * 4
        for k in range(len(padded) - 10):
            w = padded[k:k + 11]
            if w == pat1 or w == pat2:
                pen += 40
    for y in range(size - 1):
        for x in range(size - 1):
            c = mod[y][x]
            if c == mod[y][x + 1] == mod[y + 1][x] == mod[y + 1][x + 1]:
                pen += 3
    dark = sum(sum(1 for v in r if v) for r in mod)
    total = size * size
    k = (abs(dark * 20 - total * 10) + total - 1) // total - 1
    pen += max(0, k) * 10
    return pen


def qr_halfblocks(mod, quiet=2, ansi=True):
    """유니코드 반블록 두 줄씩. ansi면 흰 배경·검은 글자를 강제해 터미널 테마와 상관없이 읽히게 한다."""
    size = len(mod)
    n = size + 2 * quiet

    def dark(x, y):
        x -= quiet
        y -= quiet
        return 0 <= x < size and 0 <= y < size and mod[y][x]
    out = []
    for y in range(0, n, 2):
        row = []
        for x in range(n):
            top, bot = dark(x, y), (dark(x, y + 1) if y + 1 < n else False)
            row.append("█" if top and bot else "▀" if top else "▄" if bot else " ")
        s = "".join(row)
        out.append("\x1b[30;47m" + s + "\x1b[0m" if ansi else s)
    return "\n".join(out)


TAILSCALE_PATHS = ("/opt/homebrew/bin/tailscale", "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
                   "C:\\Program Files\\Tailscale\\tailscale.exe")


def find_tailscale():
    for p in TAILSCALE_PATHS:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return shutil.which("tailscale")


def server_public_url(cfg, runner=None, tailscale=None):
    url = cfg.get("public_url")
    if isinstance(url, str) and url.strip():
        return url.strip().rstrip("/")
    runner = runner or subprocess.run
    ts = tailscale or find_tailscale()
    if not ts:
        return None
    try:
        p = runner([ts, "status", "--json"], capture_output=True, text=True, timeout=HC_TIMEOUT, shell=False,
                    **subprocess_kwargs(text=True))
        name = json.loads(p.stdout or "{}").get("Self", {}).get("DNSName")
    except (OSError, ValueError, subprocess.TimeoutExpired, AttributeError):
        return None
    if not isinstance(name, str) or not name.strip(".") or not re.match(r"^[A-Za-z0-9.-]+$", name):
        return None
    return "https://%s:%d" % (name.rstrip("."), int(cfg["port"]))


def pair_url(server_url, token):
    return "sheltie://pair?u=%s&t=%s" % (urllib.parse.quote(server_url, safe=""),
                                             urllib.parse.quote(token, safe=""))


# ---------------------------------------------------------------- APNs

def b64url(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _der_len(buf, i):
    n = buf[i]
    i += 1
    if n < 0x80:
        return n, i
    k = n & 0x7F
    if k == 0 or k > 2:
        raise ValueError("bad DER length")
    n = int.from_bytes(buf[i:i + k], "big")
    return n, i + k


def der_to_raw(der, size=32):
    """ECDSA DER(SEQUENCE{INTEGER r, INTEGER s}) → r||s (각 size 바이트)."""
    der = bytes(der)
    if not der or der[0] != 0x30:
        raise ValueError("not a DER sequence")
    seq_len, i = _der_len(der, 1)
    if i + seq_len != len(der):
        raise ValueError("bad DER sequence length")
    out = b""
    for _ in range(2):
        if der[i] != 0x02:
            raise ValueError("expected INTEGER")
        n, i = _der_len(der, i + 1)
        v = der[i:i + n].lstrip(b"\x00")
        i += n
        if len(v) > size:
            raise ValueError("integer too large")
        out += v.rjust(size, b"\x00")
    if i != len(der):
        raise ValueError("trailing bytes")
    return out


def raw_to_der(raw):
    """r||s → DER. 검증(openssl dgst -verify)용."""
    half = len(raw) // 2

    def enc_int(b):
        b = b.lstrip(b"\x00") or b"\x00"
        if b[0] & 0x80:
            b = b"\x00" + b
        return b"\x02" + bytes([len(b)]) + b

    body = enc_int(raw[:half]) + enc_int(raw[half:])
    return b"\x30" + bytes([len(body)]) + body


def find_openssl():
    return shutil.which("openssl") or "/usr/bin/openssl"


def make_jwt(key_path, key_id, team_id, now=None, openssl=None, runner=None):
    runner = runner or subprocess.run
    now = int(now if now is not None else time.time())
    header = b64url(json.dumps({"alg": "ES256", "kid": key_id}, separators=(",", ":")).encode())
    claims = b64url(json.dumps({"iss": team_id, "iat": now}, separators=(",", ":")).encode())
    signing_input = ("%s.%s" % (header, claims)).encode("ascii")
    p = runner([openssl or find_openssl(), "dgst", "-sha256", "-sign", key_path],
               input=signing_input, capture_output=True, timeout=HC_TIMEOUT, shell=False,
               **subprocess_kwargs())
    if p.returncode != 0:
        raise RuntimeError("openssl sign failed (exit %d)" % p.returncode)
    sig = der_to_raw(p.stdout)
    return "%s.%s" % (signing_input.decode("ascii"), b64url(sig))


def push_category(state, status, prompt=None):
    if state == "done":
        return "HR_DONE"
    if state == "exited":
        return "HR_EXITED"
    if prompt:
        if prompt["kind"] == "approval" and prompt.get("body") is None:
            return "HR_BLOCKED"  # 패널을 못 읽어 prompt_id가 명령 내용을 담지 못한다: 버튼 없이 앱에서 직접
        if prompt["kind"] == "approval":
            return "HR_PERMISSION_ALWAYS" if prompt.get("always_index") is not None else "HR_PERMISSION"
        if prompt["kind"] == "confirm":
            return "HR_PERMISSION"
        return "HR_BLOCKED"
    if is_permission(status or {}):
        return "HR_PERMISSION"
    return "HR_BLOCKED"


TIME_SENSITIVE_CATEGORIES = {"HR_PERMISSION", "HR_PERMISSION_ALWAYS", "HR_BLOCKED"}

# 알림 문구는 앱의 String Catalog 키(loc-key)로 보내 기기 언어로 보이게 한다.
# 평문 subtitle/body(영어)는 키를 모르는 구버전 앱과 로그용이다.
SUBTITLES = {"HR_PERMISSION": "Needs permission", "HR_PERMISSION_ALWAYS": "Needs permission",
             "HR_BLOCKED": "Needs reply", "HR_DONE": "Done", "HR_EXITED": "Exited"}
SUBTITLE_KEYS = {"HR_PERMISSION": "push.subtitle.permission", "HR_PERMISSION_ALWAYS": "push.subtitle.permission",
                 "HR_BLOCKED": "push.subtitle.blocked", "HR_DONE": "push.subtitle.done",
                 "HR_EXITED": "push.subtitle.exited"}
# 알림 유효기간(초). 이 시간 안에 기기에 못 닿으면 APNs·FCM이 버린다(오래 꺼진 폰에 지난 알림이 늦게 오지 않게).
PUSH_TTL_BLOCKED = 3600  # 답을 기다리는 막힘·권한 요청(미답 재알림 포함)
PUSH_TTL_DONE = 6 * 3600  # 작업 끝남·터미널 종료
PUSH_TTL_TEST = 300  # 시험 알림
PUSH_TTLS = {"HR_PERMISSION": PUSH_TTL_BLOCKED, "HR_PERMISSION_ALWAYS": PUSH_TTL_BLOCKED,
             "HR_BLOCKED": PUSH_TTL_BLOCKED, "HR_DONE": PUSH_TTL_DONE, "HR_EXITED": PUSH_TTL_DONE,
             "HR_TEST": PUSH_TTL_TEST}


def push_ttl(payload):
    """알림 종류(category)별 유효기간(초). 모르는 종류는 막힘과 같이 1시간."""
    aps = payload.get("aps") if isinstance(payload, dict) else None
    cat = aps.get("category") if isinstance(aps, dict) else None
    if cat is None and isinstance(payload, dict):
        cat = payload.get("category")
    return PUSH_TTLS.get(cat, PUSH_TTL_BLOCKED)


REMINDER_SUBTITLE_KEYS = {"HR_PERMISSION": "push.subtitle.reminder.permission",
                          "HR_PERMISSION_ALWAYS": "push.subtitle.reminder.permission",
                          "HR_BLOCKED": "push.subtitle.reminder.blocked"}


def test_alert():
    return {"title": "Sheltie", "subtitle": "Test notification", "body": "Notifications are working",
            "subtitle-loc-key": "push.subtitle.test", "loc-key": "push.body.test"}


def build_payload(tid, name, state, status, prompt=None, exit_code=None):
    """푸시 본문. 카드 내용은 넣지 않는다(앱이 열 때 /prompt로 가져온다): prompt_id·kind·category만."""
    status = status or {}
    msg = status.get("msg")
    cat = push_category(state, status, prompt)
    loc_key, loc_args = None, None
    if state == "done":
        body = msg or "Task finished"
        loc_key = None if msg else "push.body.done"
    elif state == "exited":
        msg = None
        if exit_code is None:
            body, loc_key = "Terminal exited", "push.body.exited"
        else:
            body, loc_key = "Terminal exited (exit %s)" % exit_code, "push.body.exited.code"
            loc_args = [str(exit_code)]
    else:
        body = msg or "Needs your reply"
        loc_key = None if msg else "push.body.blocked"
    body = body[:200]
    alert = {"title": name or "Untitled terminal", "subtitle": SUBTITLES[cat], "body": body,
             "subtitle-loc-key": SUBTITLE_KEYS[cat]}
    if not name:
        alert["title-loc-key"] = "push.title.untitled"  # id를 제목으로 보이지 않는다
    if loc_key:
        alert["loc-key"] = loc_key
        if loc_args:
            alert["loc-args"] = loc_args
    aps = {"alert": alert, "sound": "default", "category": cat, "thread-id": tid}
    if cat in TIME_SENSITIVE_CATEGORIES:
        # 에이전트가 답을 기다리는 알림: 집중 모드에서도 배너·소리가 나게(앱에 Time Sensitive 엔타이틀먼트).
        aps["interruption-level"] = "time-sensitive"
    payload = {
        "aps": aps,
        "terminal": tid,
        "name": name,
        "state": state,
        "msg": msg,
        "category": cat,
        "prompt_id": prompt["id"] if prompt else None,
        "kind": prompt["kind"] if prompt else None,
    }
    if state == "exited":
        payload["exit_code"] = exit_code
    return payload


def _curl_quote(s):
    return '"%s"' % s.replace("\\", "\\\\").replace('"', '\\"')


class DeviceStore:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            return [d for d in data if isinstance(d, dict) and d.get("token")]
        except FileNotFoundError:
            return []
        except ValueError:
            log.warning("devices.json 읽기 실패, 빈 목록으로 본다")
            return []

    def list(self):
        with self.lock:
            return self._load()

    def add(self, token, env, relay_sig=None):
        with self.lock:
            devs = [d for d in self._load() if d["token"] != token]
            dev = {"token": token, "env": env, "added": int(time.time())}
            if relay_sig:
                dev["relay_sig"] = relay_sig
            devs.append(dev)
            write_private(self.path, devs)

    def mark_relay_sig_bad(self, token, sig):
        """중계가 서명을 거절했다: 서명을 지우고 relay_sig_bad를 남긴다(기기는 유지).
        그 사이 새 서명으로 다시 등록됐으면(sig가 다르면) 건드리지 않는다."""
        with self.lock:
            devs = self._load()
            for d in devs:
                if d["token"] == token and d.get("relay_sig") == sig:
                    d.pop("relay_sig", None)
                    d["relay_sig_bad"] = True
                    write_private(self.path, devs)
                    return True
            return False

    def relay_resign(self):
        """다시 등록이 필요한 기기들의 sha256(소문자 토큰) hex 앞 16자."""
        return [hashlib.sha256(d["token"].lower().encode("ascii")).hexdigest()[:16]
                for d in self.list() if d.get("relay_sig_bad")]

    def remove(self, token):
        with self.lock:
            devs = self._load()
            keep = [d for d in devs if d["token"] != token]
            if len(keep) != len(devs):
                write_private(self.path, keep)
                return True
            return False


def device_id(token):
    """기기 ID: sha256(소문자 토큰) hex 앞 16자. 앱과 relay_resign이 같은 값을 쓴다."""
    return hashlib.sha256(token.lower().encode("ascii")).hexdigest()[:16]


def _ms_or_none(v):
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


class PushLog:
    """발송 결과 기록(시각은 epoch ms). 토큰은 남기지 않고 기기 ID만 쓴다.
    path가 있으면 발송마다 0600 파일로 저장해 재시작해도 남긴다. 저장 실패는 경고만 하고 발송을 막지 않는다."""

    def __init__(self, path=None, wall=time.time):
        self.path = path
        self.wall = wall
        self.lock = threading.Lock()
        self.data = self._load()

    @staticmethod
    def _empty():
        return {"last_success_at": None, "last_error_at": None, "relay_limited_at": None, "devices": {}}

    def _load(self):
        d = self._empty()
        if not self.path:
            return d
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = json.load(f)
        except FileNotFoundError:
            return d
        except (OSError, ValueError):
            log.warning("%s 읽기 실패, 새로 시작한다", PUSH_STATUS_FILE)
            return d
        if not isinstance(raw, dict):
            return d
        for k in ("last_success_at", "last_error_at", "relay_limited_at"):
            d[k] = _ms_or_none(raw.get(k))
        devs = raw.get("devices")
        if isinstance(devs, dict):
            for did, e in devs.items():
                if not (isinstance(did, str) and DEVICE_ID_RE.match(did) and isinstance(e, dict)):
                    continue
                err = e.get("last_error")
                if isinstance(err, dict) and _ms_or_none(err.get("at")) is not None:
                    code = err.get("code")
                    err = {"at": err["at"], "code": code if isinstance(code, int) and not isinstance(code, bool) else 0,
                           "reason": str(err.get("reason") or "")[:100]}
                else:
                    err = None
                d["devices"][did] = {"last_success_at": _ms_or_none(e.get("last_success_at")),
                                     "last_attempt_at": _ms_or_none(e.get("last_attempt_at")), "last_error": err}
        return d

    def now_ms(self):
        return int(self.wall() * 1000)

    def _save(self):  # self.lock 안에서
        if not self.path:
            return
        try:
            write_private(self.path, self.data)
        except OSError as e:
            log.warning("%s 저장 실패: %s", PUSH_STATUS_FILE, e.strerror or type(e).__name__)

    def record(self, token, code, reason=""):
        """한 기기 발송 결과. code 200이면 성공, 아니면 last_error를 바꾼다(성공해도 last_error는 남긴다)."""
        now = self.now_ms()
        did = device_id(token)
        with self.lock:
            devs = self.data["devices"]
            e = devs.setdefault(did, {"last_success_at": None, "last_attempt_at": None, "last_error": None})
            e["last_attempt_at"] = now
            if code == 200:
                e["last_success_at"] = now
                self.data["last_success_at"] = now
            else:
                e["last_error"] = {"at": now, "code": code if isinstance(code, int) else 0,
                                   "reason": str(reason or "")[:100]}
                self.data["last_error_at"] = now
            if len(devs) > PUSH_LOG_MAX_DEVICES:
                for old in sorted(devs, key=lambda k: devs[k]["last_attempt_at"] or 0)[:len(devs) - PUSH_LOG_MAX_DEVICES]:
                    del devs[old]
            self._save()

    def relay_limited(self):
        with self.lock:
            self.data["relay_limited_at"] = self.now_ms()
            self._save()

    def device(self, did):
        with self.lock:
            e = self.data["devices"].get(did)
            return json.loads(json.dumps(e)) if e else None

    def summary(self):
        with self.lock:
            return {k: self.data[k] for k in ("last_success_at", "last_error_at", "relay_limited_at")}


class ApnsPusher:

    def __init__(self, cfg, devices, runner=None, curl="/usr/bin/curl", openssl=None, clock=time.time, push_log=None):
        self.cfg = cfg
        self.devices = devices
        self.runner = runner or subprocess.run
        self.curl = curl
        self.openssl = openssl
        self.clock = clock
        self.push_log = push_log if push_log is not None else PushLog()
        self._jwt = None
        self._jwt_at = 0
        self._lock = threading.Lock()
        self._warned = False
        self._fcm = None
        self._fcm_lock = threading.Lock()

    def available(self):
        ok = os.path.isfile(self.cfg["apns_key_path"])
        if not ok and not self._warned:
            log.warning("APNs 키 파일이 없어 푸시를 보내지 않는다")
            self._warned = True
        return ok

    def jwt(self):
        with self._lock:
            now = self.clock()
            if self._jwt is None or now - self._jwt_at >= JWT_TTL:
                self._jwt = make_jwt(self.cfg["apns_key_path"], self.cfg["apns_key_id"],
                                     self.cfg["team_id"], now=now, openssl=self.openssl)
                self._jwt_at = now
            return self._jwt

    def send_one(self, device, payload, ttl=None):
        """반환: (http_code, reason). 토큰·JWT는 argv가 아니라 stdin의 curl 설정으로 넘긴다.
        ttl(초)을 안 주면 알림 종류로 정한다(push_ttl)."""
        hosts = self.cfg.get("apns_hosts") or DEFAULTS["apns_hosts"]
        env = device.get("env") if device.get("env") in ("sandbox", "production") else "sandbox"
        base = (hosts.get(env) or DEFAULTS["apns_hosts"][env]).rstrip("/")
        if not base.startswith("https://"):
            raise ValueError("apns host must be https")
        tok = device["token"]
        if ttl is None:
            ttl = push_ttl(payload)
        body = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        conf = "\n".join([
            "url = " + _curl_quote("%s/3/device/%s" % (base, tok)),
            "header = " + _curl_quote("apns-topic: %s" % self.cfg["topic"]),
            "header = " + _curl_quote("apns-push-type: alert"),
            "header = " + _curl_quote("apns-priority: 10"),
            "header = " + _curl_quote("apns-expiration: %d" % (int(self.clock()) + ttl)),
            "header = " + _curl_quote("authorization: bearer %s" % self.jwt()),
            "data = " + _curl_quote(body),
        ]) + "\n"
        argv = [self.curl, "--http2", "-s", "--max-time", "15", "-o", "-",
                "-w", "\n%{http_code}", "--config", "-"]
        p = self.runner(argv, input=conf, capture_output=True, text=True, timeout=30, shell=False,
                         **subprocess_kwargs(text=True))
        out = (p.stdout or "").rsplit("\n", 1)
        code = out[-1].strip() if out else ""
        reason = ""
        if len(out) == 2 and out[0].strip():
            try:
                reason = json.loads(out[0]).get("reason", "")
            except ValueError:
                reason = out[0][:100]
        try:
            return int(code), reason
        except ValueError:
            return 0, "curl exit %d" % p.returncode

    def push(self, payload, ttl=None):
        sent = 0
        apns_ok = None  # APNs 기기가 있을 때만 확인한다(키 없음 경고도 그때 한 번)
        for d in self.devices.list():
            if d.get("env") == FCM_ENV:
                code, _ = self._deliver_fcm(d, payload, ttl)
            else:
                if apns_ok is None:
                    apns_ok = self.available()
                if not apns_ok:
                    continue
                code, _ = self._deliver(d, payload, ttl)
            if code == 200:
                sent += 1
        return sent

    def push_device(self, device, payload, ttl=None):
        """기기 하나에만 보낸다(시험 알림). 반환: (http_code, reason). 예외는 (0, 예외 이름)."""
        if device.get("env") == FCM_ENV:
            return self._deliver_fcm(device, payload, ttl)
        return self._deliver(device, payload, ttl)

    def fcm_relay_url(self):
        return self.cfg.get("fcm_relay_url") or ""

    def _fcm_pusher(self):
        """fcm 기기를 보낼 중계 발송기. 없으면(off) None. 이 발송기가 같은 중계를 쓰면 자기 자신."""
        url = self.fcm_relay_url()
        if not url:
            return None
        if isinstance(self, RelayPusher) and self._url == url:
            return self
        with self._fcm_lock:
            if self._fcm is None:
                self._fcm = RelayPusher(self.cfg, self.devices, url=url, push_log=self.push_log)
            return self._fcm

    def _deliver_fcm(self, d, payload, ttl=None):
        """Android 기기: APNs 키가 있어도 항상 중계로. 중계가 꺼져 있으면 보내지 않고 실패로 남긴다."""
        p = self._fcm_pusher()
        if p is None:
            self._record(d, 0, FCM_RELAY_OFF_REASON)
            log.warning("푸시 %s: Android 기기인데 중계가 꺼져 있어(push_relay_url off) 보내지 않는다",
                        short_token(d.get("token")))
            return 0, FCM_RELAY_OFF_REASON
        if p is self:
            return self._deliver(d, payload, ttl)
        return p.push_device(d, payload, ttl)

    def _deliver(self, d, payload, ttl=None):
        """한 기기 발송 + 결과 처리(기기 삭제·서명 표시) + PushLog 기록. 반환: (http_code, reason)."""
        try:
            code, reason = self.send_one(d, payload, ttl)
        except Exception as e:  # noqa: BLE001 - 한 기기 실패가 다른 기기를 막지 않게
            log.warning("푸시 실패 %s: %s", short_token(d.get("token")), type(e).__name__)
            self._record(d, 0, type(e).__name__)
            return 0, type(e).__name__
        self._record(d, code, reason)
        if code == 200:
            log.info("푸시 %s → %s 200", payload.get("terminal"), short_token(d["token"]))
        elif code == 410 or reason in ("BadDeviceToken", "Unregistered"):
            self.devices.remove(d["token"])
            log.info("푸시 %s: %d %s, 기기 삭제", short_token(d["token"]), code, reason)
        elif reason == "no_relay_sig":
            pass  # send_one이 기기마다 한 번 경고했다
        elif reason == "relay:bad_sig":
            self.devices.mark_relay_sig_bad(d["token"], d.get("relay_sig"))
            log.warning("푸시 %s: 중계가 서명을 거절, 서명을 지웠다(앱에서 다시 등록 필요)", short_token(d["token"]))
        else:
            log.warning("푸시 %s: %d %s", short_token(d["token"]), code, reason)
        return code, reason

    def _record(self, d, code, reason):
        try:
            self.push_log.record(d["token"], code, reason)
        except Exception:  # noqa: BLE001 - 기록 실패가 발송을 막지 않게
            log.exception("발송 기록 오류")


class RelayPusher(ApnsPusher):
    """키 없이 Sheltie 푸시 중계(POST <push_relay_url>/v1/push)로 보낸다. 연결은 keep-alive로 재사용한다."""

    def __init__(self, cfg, devices, timeout=RELAY_TIMEOUT, clock=time.monotonic, sleep=time.sleep, push_log=None,
                 url=None):
        super().__init__(cfg, devices, clock=clock, push_log=push_log)
        self.sleep = sleep
        self._url = url or cfg["push_relay_url"]
        u = urllib.parse.urlsplit(self._url)
        self._https = u.scheme == "https"
        self._host = u.hostname
        self._port = u.port
        self._path = u.path.rstrip("/") + "/v1/push"
        self._timeout = timeout
        self._conn = None
        self._conn_used = 0.0
        self._conn_lock = threading.Lock()
        self._no_sig_warned = set()
        self._push_ctx = threading.local()

    def available(self):
        return bool(self._url)

    def _new_conn(self):
        cls = http.client.HTTPSConnection if self._https else http.client.HTTPConnection
        return cls(self._host, self._port, timeout=self._timeout)

    def _drop_conn(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass
            self._conn = None

    def _post(self, body):
        """반환: (status, 본문 bytes). 연결 오류·끊긴 연결이면 새 연결로 한 번만 다시 보낸다.
        시간 초과는 중계가 이미 받았을 수 있어(중복 알림) 다시 보내지 않는다."""
        headers = {"Content-Type": "application/json", "Content-Length": str(len(body))}
        with self._conn_lock:
            if self._conn is not None and self.clock() - self._conn_used > RELAY_IDLE_MAX:
                self._drop_conn()
            for attempt in (0, 1):
                if self._conn is None:
                    self._conn = self._new_conn()
                try:
                    self._conn.request("POST", self._path, body=body, headers=headers)
                    resp = self._conn.getresponse()
                    data = resp.read()
                    self._conn_used = self.clock()
                    if resp.will_close:
                        self._drop_conn()
                    return resp.status, data
                except socket.timeout:
                    self._drop_conn()
                    raise
                except (http.client.HTTPException, OSError):
                    self._drop_conn()
                    if attempt:
                        raise

    def push(self, payload, ttl=None):
        # 알림 하나에 collapse_id 하나: 중계가 apns-collapse-id로 넘겨 재시도 중복을 하나로 합친다.
        self._push_ctx.collapse_id = uuid.uuid4().hex
        try:
            return super().push(payload, ttl)
        finally:
            self._push_ctx.collapse_id = None

    def push_device(self, device, payload, ttl=None):
        self._push_ctx.collapse_id = uuid.uuid4().hex
        try:
            return super().push_device(device, payload, ttl)
        finally:
            self._push_ctx.collapse_id = None

    def send_one(self, device, payload, ttl=None):
        """반환: (http_code, reason). 중계 자신의 거절은 reason "relay:<error>".
        ttl(초)은 중계가 APNs apns-expiration·FCM android.ttl로 바꾼다. 안 주면 알림 종류로 정한다."""
        tok = device["token"]
        sig = device.get("relay_sig")
        if not sig:
            if tok not in self._no_sig_warned:
                self._no_sig_warned.add(tok)
                log.warning("푸시 %s: relay_sig가 없어 건너뛴다(앱에서 다시 등록 필요)", short_token(tok))
            return 0, "no_relay_sig"
        env = device.get("env") if device.get("env") in APNS_ENVS + (FCM_ENV,) else "sandbox"
        cid = getattr(self._push_ctx, "collapse_id", None) or uuid.uuid4().hex
        if ttl is None:
            ttl = push_ttl(payload)
        body = json.dumps({"device_token": tok, "env": env, "relay_sig": sig, "collapse_id": cid, "ttl": int(ttl),
                           "payload": payload}, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        status, data = self._post(body)
        if 500 <= status < 600:
            # 중계·APNs 쪽 일시 오류: 같은 collapse_id로 한 번만 다시(중계가 apns-collapse-id로 중복을 합친다).
            log.info("푸시 %s: 중계 %d, %g초 뒤 한 번 다시 보낸다", short_token(tok), status, RELAY_RETRY_WAIT)
            self.sleep(RELAY_RETRY_WAIT)
            status, data = self._post(body)
        if status == 429:
            self.push_log.relay_limited()  # 다시 보내지 않는다(한도에 더 부딪힌다)
        reason = ""
        try:
            obj = json.loads(data.decode("utf-8")) if data else {}
        except (ValueError, UnicodeDecodeError):
            obj = None
        if isinstance(obj, dict):
            if isinstance(obj.get("error"), str):
                reason = "relay:" + obj["error"][:50]
            elif isinstance(obj.get("reason"), str):
                reason = obj["reason"][:100]
        elif status != 200:
            reason = "relay:bad_response"
        return status, reason


# ---------------------------------------------------------------- 전이 판정

class Tracker:
    """터미널마다 마지막 관측 (state, msg)를 기억하고, 바뀌었고 새 state가 blocked/done이면 알린다.

    - 같은 관측의 반복은 무시한다. blocked→working→blocked(같은 msg)는 새 막힘이라 다시 알린다.
    - 시작 직후 첫 폴링(initial): blocked는 알린다(재시작 중 막힌 걸 놓치지 않게), done은 기준선만.
    - done은 hc의 turn 번호당 한 번만 알린다(이벤트의 done과 폴링의 done이 겹쳐도 1건).
    """

    def __init__(self, notify, prompt_for=None, reminders=None):
        self.notify = notify
        self.prompt_for = prompt_for
        self.reminders = reminders
        self.lock = threading.Lock()
        self.last = {}
        self.done_turn = {}
        self.claude_seen = {}

    def observe(self, tid, status, turn=None, name=None, initial=False):
        status = status or {}
        state = status.get("state")
        key = (state, status.get("msg") if state == "blocked" else None)
        with self.lock:
            prev = self.last.get(tid)
            self.last[tid] = key
            if prev == key:
                return None
            if self.reminders is not None:
                # 막힘이 풀렸거나 바뀌었다: 이전 막힘의 재알림 취소(새 막힘이면 아래에서 다시 잡는다)
                self.reminders.cancel(tid)
            if state == "blocked":
                pass
            elif state == "done":
                if turn is not None:
                    if self.done_turn.get(tid) == turn:
                        return None
                    self.done_turn[tid] = turn
                if initial and prev is None:
                    return None
            else:
                return None
        prompt = None
        if state == "blocked" and self.prompt_for:
            prompt = self.prompt_for(tid)
        payload = build_payload(tid, name, state, status, prompt=prompt)
        if state == "blocked" and self.reminders is not None:
            self.reminders.start(tid, key, name)  # 첫 알림 발송 성공 여부와 상관없이 일정을 잡는다
        self.notify(payload)
        return payload

    def mark_claude(self, tid, name):
        with self.lock:
            self.claude_seen[tid] = name

    def exited(self, tid, exit_code=None, name=None):
        """본 적 있는 Claude 터미널이 끝나면 HR_EXITED 1회. 두 번째 호출부터는 None."""
        with self.lock:
            if tid not in self.claude_seen:
                return None
            seen_name = self.claude_seen.pop(tid)
        payload = build_payload(tid, name or seen_name, "exited", None, exit_code=exit_code)
        self.notify(payload)
        return payload

    def forget(self, tid):
        with self.lock:
            self.last.pop(tid, None)
            self.done_turn.pop(tid, None)
        if self.reminders is not None:
            self.reminders.cancel(tid)

    def known(self):
        with self.lock:
            return set(self.last)

    def known_claude(self):
        with self.lock:
            return set(self.claude_seen)


# ---------------------------------------------------------------- 미답 재알림

def reminder_intervals(cfg):
    """config reminder_after_sec: 양수 초 목록(빈 목록이면 재알림 끔). 형식이 틀리면 기본값."""
    v = cfg.get("reminder_after_sec", list(REMINDER_AFTER_SEC))
    if isinstance(v, list) and all(isinstance(x, (int, float)) and not isinstance(x, bool) and x > 0 for x in v):
        return sorted(v)
    log.warning("reminder_after_sec 형식이 틀려 기본값 %s를 쓴다", list(REMINDER_AFTER_SEC))
    return list(REMINDER_AFTER_SEC)


class Reminders:
    """같은 막힘(터미널, (state, msg))이 계속되면 첫 알림 뒤 intervals초마다 한 번씩 다시 알릴 일정.
    막힘이 풀리거나 바뀌거나 터미널이 사라지면(Tracker가 cancel) 지운다. 확인은 poll_loop 주기에서 due()로."""

    def __init__(self, intervals, clock=time.monotonic):
        self.intervals = list(intervals)
        self.clock = clock
        self.lock = threading.Lock()
        self.items = {}

    def start(self, tid, key, name):
        if not self.intervals:
            return
        with self.lock:
            self.items[tid] = {"key": key, "name": name, "t0": self.clock(), "sent": 0}

    def cancel(self, tid):
        with self.lock:
            self.items.pop(tid, None)

    def due(self):
        """지금 보낼 재알림 [(tid, key, n, name)]. n은 1부터. 여러 시점을 한꺼번에 넘겼으면 마지막 것 하나만."""
        now = self.clock()
        out = []
        with self.lock:
            for tid, it in list(self.items.items()):
                passed = sum(1 for s in self.intervals if now - it["t0"] >= s)
                if passed > it["sent"]:
                    it["sent"] = passed
                    out.append((tid, it["key"], passed, it["name"]))
                if it["sent"] >= len(self.intervals):
                    del self.items[tid]
        return out


# ---------------------------------------------------------------- 막힘 시작 시각

class BlockedSince:
    """터미널별로 blocked가 시작된 시각(epoch ms)을 기억한다. hc의 status_ms는 의미가 확인되지 않아 쓰지 않는다.

    blocked가 아닌 관측이 한 번이라도 있으면 지우고, 사라진 터미널도 지운다.
    서버를 다시 켠 시점에 이미 막혀 있던 터미널은 그때부터 센다.
    """

    def __init__(self, wall=time.time):
        self.wall = wall
        self.lock = threading.Lock()
        self.since = {}

    def update(self, observed):
        """observed: {tid: blocked 여부}. {tid: blocked_since(ms)|None}를 돌려준다."""
        now_ms = int(self.wall() * 1000)
        out = {}
        with self.lock:
            for tid in list(self.since):
                if tid not in observed:
                    del self.since[tid]
            for tid, blocked in observed.items():
                if blocked:
                    out[tid] = self.since.setdefault(tid, now_ms)
                else:
                    self.since.pop(tid, None)
                    out[tid] = None
        return out


# ---------------------------------------------------------------- 여러 PC(앱 목록·알림 구분)

def server_tag(token):
    """알림에 넣는 PC 표시. 앱은 저장한 토큰으로 같은 값을 계산해 알림을 보낸 PC를 고른다.
    sha256("sheltie-server:" + 토큰) hex 앞 12자(토큰 자체는 드러나지 않는다)."""
    return hashlib.sha256(("sheltie-server:" + (token or "")).encode()).hexdigest()[:12]


def machine_name():
    """/api/health가 주는 PC 이름(앱이 처음 추가할 때 기본 이름). 호스트 이름에서 .local을 뗀다."""
    try:
        name = socket.gethostname() or ""
    except OSError:
        return None
    if name.lower().endswith(".local"):
        name = name[:-6]
    return name or None


def git_branch(cwd, max_up=40):
    """cwd가 속한 git 저장소의 현재 브랜치. 저장소가 아니면 None, 브랜치가 없으면(detached) 커밋 앞 7자.
    git을 부르지 않고 .git/HEAD를 직접 읽는다(worktree의 `gitdir:` 파일도 따라간다).
    일반 파일만 읽고(FIFO 등에서 멈추지 않게), 결과는 브랜치 이름 형식이나 커밋 hex일 때만 돌려준다
    (심볼릭 링크가 저장소 밖 파일을 가리켜도 그 내용이 나가지 않게)."""
    if not isinstance(cwd, str) or not is_abs_path(cwd):
        return None

    def read_regular(path):
        # O_NONBLOCK: FIFO를 열 때 막히지 않는다. 연 뒤 fstat으로 일반 파일인지 본다(확인과 열기 사이 바꿔치기 방지).
        if not os.path.isfile(path):
            return None
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return None
            return os.read(fd, 4096).decode("utf-8", "replace")
        finally:
            os.close(fd)

    def safe_branch(name):
        if re.fullmatch(r"[A-Za-z0-9._/-]{1,100}", name) and ".." not in name:
            return name
        return None
    d = os.path.normpath(cwd)
    for _ in range(max_up):
        dot = os.path.join(d, ".git")
        head = None
        try:
            if os.path.isdir(dot):
                head = os.path.join(dot, "HEAD")
            elif os.path.isfile(dot):
                line = (read_regular(dot) or "").strip()
                if line.startswith("gitdir:"):
                    gd = line[len("gitdir:"):].strip()
                    if not os.path.isabs(gd):
                        gd = os.path.normpath(os.path.join(d, gd))
                    head = os.path.join(gd, "HEAD")
            if head:
                ref = read_regular(head)
                if ref is None:
                    return None
                ref = ref.strip()
                if ref.startswith("ref:"):
                    ref = ref[4:].strip()
                    return safe_branch(ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref)
                if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", ref):
                    return ref[:7]
                return None
        except OSError:
            return None
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent
    return None


class BranchCache:
    """cwd별 브랜치를 잠깐 기억한다(목록은 5초마다 불린다)."""

    def __init__(self, ttl=10.0, clock=time.monotonic, reader=git_branch):
        self.ttl = ttl
        self.clock = clock
        self.reader = reader
        self.lock = threading.Lock()
        self.cache = {}

    def get(self, cwd):
        if not isinstance(cwd, str) or not cwd:
            return None
        now = self.clock()
        with self.lock:
            hit = self.cache.get(cwd)
            if hit and now - hit[0] < self.ttl:
                return hit[1]
        try:
            b = self.reader(cwd)
        except Exception:  # noqa: BLE001 - 브랜치 실패가 목록을 막지 않게
            b = None
        with self.lock:
            self.cache[cwd] = (now, b)
            if len(self.cache) > 512:
                self.cache.clear()
        return b


# ---------------------------------------------------------------- 훅 세션(herdr 없이)
#
# Claude Code·Codex 공식 훅이 `herdr_remote.py hook <claude|codex>`로 이벤트 JSON을 POST /hook/<agent>에 보낸다.
# 막힘(PermissionRequest, PreToolUse AskUserQuestion·ExitPlanMode)은 폰 답이 올 때까지 그 요청을 붙잡았다가
# 결정을 돌려준다. 세션 하나가 터미널 하나(id "h" + session_id 영숫자 끝 12자)이고, App은 MultiBackend로
# hc 터미널과 같은 자리에서 다룬다. 화면·키·글 보내기·새 작업은 없다(caps []).

HOOK_AGENTS = ("claude", "codex")
HOOK_APPS = {"claude": "claude-code", "codex": "codex"}
HOOK_HOLD_SEC = 540           # 훅 타임아웃(600초)보다 짧게. 지나면 결정 없이 {} → 터미널 대화상자로
HOOK_CLIENT_TIMEOUT = 590     # hook 하위 명령이 서버 답을 기다리는 상한(초)
HOOK_POLL = 0.2               # 붙잡은 동안 폰 답·연결 끊김을 보는 간격(초)
HOOK_MAX_BODY = 4 * 1024 * 1024  # Write의 tool_input에는 파일 내용 전체가 들어 있다
HOOK_MAX_OUT = 1024 * 1024
HOOK_STALE_SEC = 24 * 3600    # 이 시간 동안 이벤트가 없던 세션은 목록에서 뺀다(SessionEnd 없이 죽은 세션)
HOOK_REJECT_MSG = "Rejected from phone (Sheltie)"
HOOK_PROXY_HEADERS = ("Tailscale-User-Login", "Tailscale-User-Name", "X-Forwarded-For")
HOOK_HOLD_TOOLS = ("AskUserQuestion", "ExitPlanMode")
HOOK_CAPS = ["read", "keys", "send"]  # hc 터미널이 지원하는 것. 훅 세션은 []
HOOK_FREE_TEXT = "Type something."
HOOK_PLAN_FREE_TEXT = "Tell Claude what to change"
HOOK_NO_REPLY = object()      # 클라이언트가 이미 끊겼다: 아무것도 쓰지 않는다


def hook_session_tid(session_id):
    """session_id → 훅 세션 id("h" + 영숫자 끝 12자). 못 만들면 None.
    끝을 쓴다: Codex는 UUIDv7이라 앞 12자가 밀리초 시각이어서 같은 순간에 시작한 세션끼리 겹친다."""
    if not isinstance(session_id, str):
        return None
    s = re.sub(r"[^A-Za-z0-9]", "", session_id)[-12:]
    return "h" + s if s else None


def hook_transcript_path(path, home):
    """훅이 준 transcript_path가 ~/.claude/projects 또는 Codex sessions 아래 .jsonl이면 실제 경로, 아니면 None."""
    if not isinstance(path, str) or not path.endswith(".jsonl") or "\x00" in path or len(path) > 4096:
        return None
    try:
        real = os.path.realpath(path)
        for base in (os.path.join(home, ".claude", "projects"), codex_sessions_dir(home)):
            rb = os.path.realpath(base)
            if real.startswith(rb.rstrip(os.sep) + os.sep):
                return real
    except (OSError, ValueError):
        pass
    return None


def _hook_str(v, n=MAX_PROMPT_BODY):
    return v[:n] if isinstance(v, str) and v else None


def hook_tool_body(tool, inp):
    """카드 본문: 무엇을 승인하는지(명령·파일 경로·바꿀 내용). 비지 않는다."""
    inp = inp if isinstance(inp, dict) else {}
    parts = []
    for k in ("command", "file_path", "notebook_path", "path", "url"):
        v = _hook_str(inp.get(k))
        if v:
            parts.append(v)
    if tool == "Write":
        v = _hook_str(inp.get("content"), 4000)
        if v:
            parts.append(v)
    elif tool == "Edit":
        old, new = _hook_str(inp.get("old_string"), 2000), _hook_str(inp.get("new_string"), 2000)
        if old or new:
            parts.append("- %s\n+ %s" % (old or "", new or ""))
    desc = _hook_str(inp.get("description"), 1000)
    if desc:
        parts.append(desc)
    if not parts:
        parts.append(json.dumps(inp, ensure_ascii=False, indent=2)[:MAX_PROMPT_BODY] if inp else (tool or "tool"))
    return "\n\n".join(parts)[:MAX_PROMPT_BODY]


def _hook_rule_text(rule):
    if not isinstance(rule, dict) or not isinstance(rule.get("toolName"), str):
        return None
    rc = rule.get("ruleContent")
    return "%s(%s)" % (rule["toolName"], rc) if isinstance(rc, str) and rc else rule["toolName"]


def hook_always_rules(suggestions):
    """permission_suggestions 중 계속 허용에 쓸 것: type addRules, behavior allow만(addDirectories·setMode는 쓰지 않는다)."""
    out = []
    if isinstance(suggestions, list):
        for s in suggestions:
            if (isinstance(s, dict) and s.get("type") == "addRules" and s.get("behavior") == "allow"
                    and isinstance(s.get("rules"), list) and s["rules"]
                    and all(_hook_rule_text(r) for r in s["rules"])):
                out.append(s)
    return out


class HookHold:
    """붙잡힌 훅 요청 하나. state: waiting → decided(폰 답) | gone(PC가 먼저 답해 연결 끊김) | expired(시간 초과)."""

    def __init__(self, agent, event_name, kind, tool_name, tool_input, status, prompts, always_perms=None,
                 questions=None):
        self.agent = agent
        self.event_name = event_name
        self.kind = kind  # permission | question | plan
        self.tool_name = tool_name
        self.tool_input = tool_input if isinstance(tool_input, dict) else {}
        self.status = status
        self.prompts = prompts
        self.always_perms = always_perms or None
        self.questions = questions or []
        self.qi = 0
        self.answers = {}
        self.state = "waiting"
        self.result = None
        self.event = threading.Event()

    def prompt(self):
        if self.state != "waiting":
            return None
        return self.prompts[min(self.qi, len(self.prompts) - 1)]

    def allow(self, updated_input=None, perms=None):
        if self.event_name == "PreToolUse":
            out = {"hookEventName": "PreToolUse", "permissionDecision": "allow"}
            if updated_input is not None:
                out["updatedInput"] = updated_input
        else:
            d = {"behavior": "allow"}
            if perms:
                d["updatedPermissions"] = perms
            out = {"hookEventName": "PermissionRequest", "decision": d}
        return {"hookSpecificOutput": out}

    def deny(self, message=HOOK_REJECT_MSG):
        if self.event_name == "PreToolUse":
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                           "permissionDecisionReason": message}}
        return {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                       "decision": {"behavior": "deny", "message": message}}}


def _hook_card_id(p, tool_input, nonce):
    """카드 id: 화면 카드와 같은 prompt_id에 원본 tool_input 전체와 요청마다 다른 nonce를 섞는다.
    잘린 본문이 같아도, 같은 명령을 다시 물어도 이전 알림의 답이 새 요청에 들어가지 않게."""
    raw = json.dumps([prompt_id(p), tool_input, nonce], ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), default=str).encode("utf-8")
    p["id"] = hashlib.sha256(raw).hexdigest()[:12]
    return p


def hook_permission_hold(agent, body):
    tool = body.get("tool_name") if isinstance(body.get("tool_name"), str) else "tool"
    inp = body.get("tool_input") if isinstance(body.get("tool_input"), dict) else {}
    nonce = secrets.token_hex(8)
    perms = hook_always_rules(body.get("permission_suggestions")) if agent == "claude" else []
    if agent == "codex":
        title = CODEX_APPROVAL_TITLES.get("make the following edits" if tool == "apply_patch"
                                          else "run the following command") if tool in ("Bash", "apply_patch") else tool
        question = ("Would you like to make the following edits?" if tool == "apply_patch"
                    else "Would you like to run the following command?" if tool == "Bash"
                    else "Allow %s?" % tool)
    else:
        title = {"Bash": "Bash command", "Write": "Create file", "Edit": "Edit file"}.get(tool, tool)
        question = "Do you want to proceed?"
    options = [("Yes", None)]
    always = None
    if perms:
        rules = [_hook_rule_text(r) for s in perms for r in s["rules"]]
        always = len(options)
        options.append(("Yes, and don't ask again for %s" % ", ".join(rules)[:300], None))
    reject = len(options)
    options.append(("No", None))
    p = _finish("approval", title, question, hook_tool_body(tool, inp), options, selected=0,
                always_index=always, reject_index=reject)
    status = {"state": "blocked", "kind": "permission", "msg": "approve %s" % tool, "app": HOOK_APPS[agent]}
    return HookHold(agent, "PermissionRequest", "permission", tool, inp, status,
                    [_hook_card_id(p, inp, nonce)], always_perms=perms)


def hook_question_hold(agent, body):
    """AskUserQuestion: 질문마다 카드 하나(탭처럼 차례로). 질문이 이상하면 None(붙잡지 않는다)."""
    inp = body.get("tool_input") if isinstance(body.get("tool_input"), dict) else {}
    qs = inp.get("questions")
    if not isinstance(qs, list) or not 1 <= len(qs) <= 8:
        return None
    clean = []
    for q in qs:
        if not isinstance(q, dict) or not isinstance(q.get("question"), str) or not q["question"]:
            return None
        opts = q.get("options")
        if not isinstance(opts, list) or not opts or len(opts) > 20:
            return None
        labels = []
        for o in opts:
            if not isinstance(o, dict) or not isinstance(o.get("label"), str) or not o["label"]:
                return None
            labels.append((o["label"], o.get("description") if isinstance(o.get("description"), str) else None))
        clean.append({"question": q["question"], "header": q.get("header") if isinstance(q.get("header"), str) else None,
                      "options": labels, "multi": bool(q.get("multiSelect"))})
    nonce = secrets.token_hex(8)
    prompts = []
    for i, q in enumerate(clean):
        options = list(q["options"])
        free = None
        if not q["multi"]:  # 화면 카드와 같게: 복수 선택에는 자유 입력을 두지 않는다
            free = len(options)
            options.append((HOOK_FREE_TEXT, None))
        tabs = ([{"label": c["header"] or c["question"][:40], "answered": k < i} for k, c in enumerate(clean)]
                if len(clean) > 1 else None)
        p = _finish("question", q["header"], q["question"], None, options, selected=0, multi=q["multi"],
                    free_text_index=free, tabs=tabs)
        prompts.append(_hook_card_id(p, [inp, i], nonce))
    status = {"state": "blocked", "kind": "question", "msg": clean[0]["question"][:200], "app": HOOK_APPS[agent]}
    return HookHold(agent, "PreToolUse", "question", "AskUserQuestion", inp, status, prompts, questions=clean)


def hook_plan_hold(agent, body):
    inp = body.get("tool_input") if isinstance(body.get("tool_input"), dict) else {}
    plan = _hook_str(inp.get("plan")) or _hook_str(inp.get("planFilePath")) or "(plan)"
    p = _finish("plan", "Ready to code?",
                "Claude has written up a plan and is ready to execute. Would you like to proceed?", plan,
                [("Yes", None), ("No, keep planning", None), (HOOK_PLAN_FREE_TEXT, None)],
                selected=0, free_text_index=2, reject_index=1)
    status = {"state": "blocked", "kind": "plan", "msg": None, "app": HOOK_APPS[agent]}
    return HookHold(agent, "PreToolUse", "plan", "ExitPlanMode", inp, status,
                    [_hook_card_id(p, inp, secrets.token_hex(8))])


class HookBackend:
    """훅 세션 저장소. Hc와 같은 ls/get 모양을 준다(read·keys·send는 MultiBackend가 거절한다)."""

    def __init__(self, wall=time.time):
        self.wall = wall
        self.lock = threading.Lock()
        self.sessions = {}

    def has(self, tid):
        with self.lock:
            return isinstance(tid, str) and tid in self.sessions

    def touch(self, tid, agent, body, home):
        """세션 등록·갱신. 끝난(시간 초과) 붙잡기는 PC에서 풀렸을 것이라 지운다. 반환: 새로 만들었는지."""
        now = self.wall()
        cwd = body.get("cwd") if isinstance(body.get("cwd"), str) else None
        tp = hook_transcript_path(body.get("transcript_path"), home)
        with self.lock:
            s = self.sessions.get(tid)
            new = s is None
            if new:
                s = self.sessions[tid] = {
                    "tid": tid, "session_id": body.get("session_id"), "agent": agent, "cwd": cwd,
                    "name": (os.path.basename(cwd.rstrip("/\\")) or cwd) if cwd else None,
                    "transcript": tp, "base": {"state": "idle", "app": HOOK_APPS[agent]}, "status_ms": int(now * 1000),
                    "turn": 0, "holds": [], "seen": now}
            s["seen"] = now
            if cwd and not s["cwd"]:
                s["cwd"] = cwd
                s["name"] = os.path.basename(cwd.rstrip("/\\")) or cwd
            if tp:
                s["transcript"] = tp
            s["holds"] = [h for h in s["holds"] if h.state != "expired"]
            return new

    def set_base(self, tid, state, new_turn=False):
        with self.lock:
            s = self.sessions.get(tid)
            if s is None:
                return
            s["base"] = {"state": state, "app": HOOK_APPS[s["agent"]]}
            s["status_ms"] = int(self.wall() * 1000)
            if new_turn:
                s["turn"] += 1

    def new_stop(self, tid, key):
        """Stop의 턴 id가 지난 Stop과 다르면 True(턴 id가 없으면 False)."""
        if not isinstance(key, str) or not key:
            return False
        with self.lock:
            s = self.sessions.get(tid)
            if s is None or s.get("stop_key") == key:
                return False
            s["stop_key"] = key
            return True

    def add_hold(self, tid, hold):
        """붙잡기 추가. 맨 앞(지금 카드)이 되었으면 True."""
        with self.lock:
            s = self.sessions[tid]
            s["holds"].append(hold)
            s["status_ms"] = int(self.wall() * 1000)
            return s["holds"][0] is hold

    def finish(self, hold, state, result=None):
        """waiting인 붙잡기만 state로 바꾼다(폰 답과 연결 끊김·시간 초과의 경쟁). 바꿨으면 True."""
        with self.lock:
            if hold.state != "waiting":
                return False
            hold.state = state
            hold.result = result
            hold.event.set()
            return True

    def pop_hold(self, tid, hold, base="working"):
        with self.lock:
            s = self.sessions.get(tid)
            if s is None or hold not in s["holds"]:
                return
            s["holds"].remove(hold)
            if not s["holds"]:
                s["base"] = {"state": base, "app": HOOK_APPS[s["agent"]]}
            s["status_ms"] = int(self.wall() * 1000)

    def queued(self, tid):
        with self.lock:
            s = self.sessions.get(tid)
            return len(s["holds"]) if s else 0

    def active(self, tid):
        """지금 카드의 붙잡기(맨 앞) 또는 None."""
        with self.lock:
            s = self.sessions.get(tid)
            return s["holds"][0] if s and s["holds"] else None

    def current_prompt(self, tid):
        h = self.active(tid)
        return h.prompt() if h else None

    def advance_question(self, hold, question, value):
        """질문 하나 답. 남은 질문이 있으면 False, 다 답했으면 True. 그사이 풀렸으면 ApiError."""
        with self.lock:
            if hold.state != "waiting":
                raise ApiError(409, "not_blocked")
            hold.answers[question] = value
            hold.qi += 1
            return hold.qi >= len(hold.prompts)

    def remove(self, tid):
        with self.lock:
            s = self.sessions.pop(tid, None)
        for h in (s or {}).get("holds", []):
            self.finish(h, "gone")
        return s

    def prune(self, max_idle=HOOK_STALE_SEC):
        cut = self.wall() - max_idle
        with self.lock:
            old = [t for t, s in self.sessions.items() if s["seen"] < cut and not s["holds"]]
            for t in old:
                del self.sessions[t]
        return old

    def transcript(self, tid):
        with self.lock:
            s = self.sessions.get(tid)
            return (s["session_id"], s["transcript"]) if s else (None, None)

    def _status(self, s):
        if s["holds"]:
            return dict(s["holds"][0].status)
        return dict(s["base"])

    def ls(self):
        with self.lock:
            return [{"address": s["tid"], "name": s["name"], "cwd": s["cwd"], "agent": s["agent"], "state": "live",
                     "turn": s["turn"]} for s in self.sessions.values()]

    def get(self, tid):
        with self.lock:
            s = self.sessions.get(tid)
            if s is None:
                raise HcError("no hook session")
            return {"address": tid, "name": s["name"], "cwd": s["cwd"], "agent": s["agent"], "state": "live",
                    "status": self._status(s), "status_ms": s["status_ms"], "title": None, "turn": s["turn"]}


class MultiBackend:
    """App의 self.hc 자리. id가 훅 세션이면 HookBackend, 아니면 hc(동작 그대로).
    hc가 없는 설치(hc_path가 ""이거나 실행 파일이 없음)는 hc를 부르지 않고 훅 세션만 다룬다(경고는 한 번)."""

    def __init__(self, hc, hooks):
        self.hc = hc
        self.hooks = hooks
        self._warned = False

    def hc_enabled(self):
        if self.hc is None:
            ok = False
        else:
            path = getattr(self.hc, "path", None)
            if path is None:  # 경로가 없는 hc(테스트 가짜): 있다고 본다
                ok = True
            else:
                ok = bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)
        if not ok and not self._warned:
            self._warned = True
            log.warning("hc가 없다(%s): herdr 터미널 없이 훅 세션만 다룬다",
                        getattr(self.hc, "path", None) or "hc_path 비어 있음")
        elif ok:
            self._warned = False
        return ok

    def _hc(self):
        if not self.hc_enabled():
            raise HcError("hc not available")
        return self.hc

    def ls(self):
        hooks = self.hooks.ls()
        if not self.hc_enabled():
            return hooks
        return list(self.hc.ls()) + hooks

    def get(self, tid):
        if self.hooks.has(tid):
            return self.hooks.get(tid)
        return self._hc().get(tid)

    def read(self, tid, lines=0):
        if self.hooks.has(tid):
            raise ApiError(409, "unsupported")
        return self._hc().read(tid, lines)

    def keys(self, tid, keys):
        if self.hooks.has(tid):
            raise ApiError(409, "unsupported")
        return self._hc().keys(tid, keys)

    def send(self, tid, text, force=False):
        if self.hooks.has(tid):
            raise ApiError(409, "unsupported")
        return self._hc().send(tid, text, force=force)

    def run_claude(self, cwd, prompt=None, exe=None):
        if not self.hc_enabled():
            raise ApiError(409, "unsupported")
        return self.hc.run_claude(cwd, prompt, exe=exe) if exe is not None else self.hc.run_claude(cwd, prompt)

    def run_agent(self, cwd, argv):
        if not self.hc_enabled():
            raise ApiError(409, "unsupported")
        return self.hc.run_agent(cwd, argv)

    def events_argv(self):
        return self.hc.events_argv()


# ---------------------------------------------------------------- 앱

PUSH_PROMPT_BUDGET = 0.3   # 푸시용 카드 읽기 상한(초). 넘으면 prompt_id 없이 보낸다(알림 속도 우선).
# Codex는 hc가 blocked로 알린 뒤 0.9초쯤 지나서야 승인 메뉴가 화면에 그려진다(맥미니 실측, 0.171.0).
# 그래서 Codex만 메뉴가 보일 때까지 이 시간 안에서 다시 읽는다. 끝내 없으면 prompt_id 없이 보낸다.
CODEX_PUSH_PROMPT_WAIT = 2.0
CODEX_PUSH_PROMPT_RETRY = 0.15


# ---------------------------------------------------------------- 에이전트 표
# 에이전트마다 다른 것을 한 곳에 모은다. 새 에이전트는 항목 하나와 그 파서·기록 함수만 더한다.
#   match(info)            이 터미널이 그 에이전트인가(hc agent·status.app·앞 프로그램)
#   parse(lines, reader)   화면 줄 → 활성 메뉴 카드 또는 None(모르는 화면은 None: 카드·버튼 없음)
#   free_text(p, num, s)   카드의 직접 입력 답 → answer_steps 단계
#   transcript(info, home, runner=) → (session_id, 기록 경로) 또는 (None, None). 테스트가 모듈 함수를 바꿔
#                          끼우므로 호출 때 이름으로 찾는다
#   push_wait()            blocked 알림 카드를 읽을 시간(초). 메뉴가 늦게 그려지는 에이전트는 그 안에서 다시 읽는다
#   screen                 hc가 막힘으로 안 보는 화면을 서버가 화면으로 판정하는 방식:
#                          None, "codex_plan"(hc status + 계획 메뉴만 덧씌움),
#                          "full"(OSC 7501 상태를 안 보내 hc status가 없다: 화면 메뉴·터미널 제목으로 전부 판정)
#   title_blocked/title_working  "full"에서 터미널 제목으로 막힘·작업 중을 아는 정규식(없으면 None)
#   busy_re                "full"에서 제목이 없을 때 화면 끝 줄로 작업 중을 아는 정규식(없으면 None)
AGENTS = {
    "claude": {"match": is_claude, "parse": _parse_claude_lines, "free_text": _free_text_digit,
               "transcript": lambda info, home, runner=None: find_transcript(info, home, runner=runner),
               "push_wait": lambda: PUSH_PROMPT_BUDGET, "retry": False,
               "screen": None},
    "codex": {"match": is_codex, "parse": _parse_codex_lines, "free_text": _free_text_codex,
              "transcript": lambda info, home, runner=None: find_codex_transcript(info, home, runner=runner),
              "push_wait": lambda: CODEX_PUSH_PROMPT_WAIT, "retry": True,
              "screen": "codex_plan"},
    "gemini": {"match": _named("gemini"), "parse": _parse_gemini_lines, "free_text": _free_text_gemini,
               "transcript": lambda info, home, runner=None: find_gemini_transcript(info, home, runner=runner),
               "push_wait": lambda: PUSH_PROMPT_BUDGET, "retry": False, "screen": "full",
               "title_blocked": GEMINI_TITLE_BLOCKED_RE, "title_working": GEMINI_TITLE_WORKING_RE, "busy_re": None},
}
AGENT_ORDER = ("claude", "codex", "gemini")
SCREEN_POLL_INTERVAL = 3.0  # "full" 에이전트 화면 판정 주기(초). 제목 이벤트가 먼저 오면 그때 바로 본다
SCREEN_SETTLE = 0.6  # 제목은 막힘인데 메뉴를 못 읽었을 때 다시 볼 때까지(초)
PROMPT_STATUS_KINDS = {"approval": "permission", "confirm": "permission", "question": "question", "plan": "plan"}


class App:
    def __init__(self, cfg, hc, devices, pusher, runner=None, clock=time.monotonic, push_log=None):
        self.cfg = cfg
        if not isinstance(hc, MultiBackend):
            hc = MultiBackend(hc, HookBackend())
        self.hc = hc  # hc 터미널과 훅 세션을 id로 나눠 준다
        self.hooks = hc.hooks
        self.hook_hold_sec = HOOK_HOLD_SEC
        self.hook_clock = time.monotonic  # 테스트가 바꾼다(붙잡기 시간 초과)
        self.devices = devices
        self.pusher = pusher
        self.runner = runner or subprocess.run
        self.clock = clock
        if push_log is None:
            push_log = getattr(pusher, "push_log", None)
        if push_log is None:
            push_log = PushLog(os.path.join(cfg["_dir"], PUSH_STATUS_FILE) if cfg.get("_dir") else None)
        self.push_log = push_log
        self._test_push_at = {}
        self._test_push_lock = threading.Lock()
        self.home = os.path.realpath(os.path.expanduser(cfg.get("home") or "~"))
        self.plan_reader = make_plan_reader(self.home)
        self.reminders = Reminders(reminder_intervals(cfg))
        self._later = lambda sec, fn: threading.Timer(sec, fn).start()  # 테스트가 바꾼다
        self._agent_kinds = {}  # tid → agent_kind(마지막 hc get). 알림 카드를 어느 화면으로 읽을지 정한다
        self._screen_state = {}  # "full" 에이전트: tid → 마지막으로 판정한 status
        self._screen_turns = {}  # "full" 에이전트: tid → 끝남 횟수(Tracker의 turn 대신)
        self.tracker = Tracker(self._notify, prompt_for=self._prompt_for_push, reminders=self.reminders)
        self.blocked_since = BlockedSince()
        self.branches = BranchCache()
        self.agent_catalog = AgentCatalog(self.home)
        self._refresh_lock = threading.Lock()
        self._term_locks = {}
        self._term_locks_guard = threading.Lock()
        self._pending = []
        self._detected = None
        self.stop = threading.Event()

    def _notify(self, payload):
        payload["server"] = server_tag(self.cfg.get("token"))
        # refresh 잠금 안에서 불리므로 모았다가 잠금 밖에서 보낸다. 감지 시각을 같이 둔다.
        self._pending.append((payload, self._detected if self._detected is not None else self.clock()))

    def _flush(self):
        pending, self._pending = self._pending, []
        for p, t0 in pending:
            try:
                self.pusher.push(p)
            except Exception:  # noqa: BLE001
                log.exception("푸시 오류")
            log.info("알림 %s %s %s 감지→발송 %dms", p["terminal"], p["state"], p["aps"]["category"],
                     int((self.clock() - t0) * 1000))

    def parse_screen(self, text, agent=None):
        return parse_prompt(text, plan_reader=self.plan_reader, agent=agent)

    def _prompt_for_push(self, tid):
        """blocked 알림용 카드: hc read 한 번 + 파싱, PUSH_PROMPT_BUDGET 안에 못 끝나면 None."""
        if self.hooks.has(tid):
            return self.hooks.current_prompt(tid)  # 훅 세션: 붙잡은 요청의 카드(시간 초과면 None)
        box = {}
        agent = self._agent_kinds.get(tid)
        spec = agent_spec(agent)
        budget = spec["push_wait"]()
        end = time.monotonic() + budget
        stop = threading.Event()

        def work():
            try:
                while True:
                    box["p"] = self.parse_screen((self.hc.read(tid, 0) or {}).get("text", ""), agent)
                    if box["p"] or not spec["retry"] or stop.is_set() \
                            or time.monotonic() + CODEX_PUSH_PROMPT_RETRY >= end:
                        return
                    time.sleep(CODEX_PUSH_PROMPT_RETRY)
            except Exception:  # noqa: BLE001 - 카드 실패가 알림을 막지 않게
                box["p"] = None
        th = threading.Thread(target=work, name="push-prompt", daemon=True)
        th.start()
        th.join(budget)
        stop.set()
        if th.is_alive():
            log.info("카드 읽기 %dms 초과 %s, prompt_id 없이 보낸다", int(budget * 1000), tid)
            return None
        return box.get("p")

    def _codex_plan_status(self, tid, info):
        """hc가 blocked로 보지 않는 Codex "Implement this plan?" 메뉴가 화면 끝에 살아 있으면
        (막힘 status, True). 메뉴는 없고 플랜 모드면 (None, True). 그 밖에는 (None, False)."""
        if self.hooks.has(tid) or agent_spec(agent_kind(info))["screen"] != "codex_plan":
            return None, False
        st = status_of(info)
        if st.get("state") not in ("done", "idle"):
            return None, False
        try:
            text = (self.hc.read(tid, 0) or {}).get("text", "")
        except HcError:
            return None, False
        p = parse_prompt(text, agent="codex")
        if p and p["kind"] == "plan":
            return dict(st, state="blocked", kind="plan", msg=None), True
        tail = [ln for ln in screen_lines(text) if ln.strip()][-1:]
        return None, bool(tail and CODEX_PLAN_MODE_RE.search(tail[0]))

    def _screen_status(self, tid, info, commit=False, settle=False):
        """hc status가 없는 에이전트("full")의 status를 화면·제목으로 만든다.
        메뉴 카드가 읽히면 막힘(kind·msg는 카드에서), 제목이 막힘이면 카드 없는 막힘, 작업 중이면 working,
        작업·막힘 뒤에 멈추면 done(다음 작업까지 유지), 아니면 idle. commit이면 판정을 기억한다(refresh만).
        제목은 막힘인데 메뉴를 아직 못 읽었고 settle이 아니면 None(잠깐 뒤 다시 본다)."""
        kind = agent_kind(info)
        spec = agent_spec(kind)
        title = info.get("title") if isinstance(info.get("title"), str) else ""
        try:
            text = (self.hc.read(tid, 0) or {}).get("text", "")
        except HcError:
            text = ""
        p = self.parse_screen(text, kind) if text else None
        prev_st = self._screen_state.get(tid) or {}
        prev = prev_st.get("state")
        if p:
            msg = ("approve " + (p["title"] or "")).strip() if p["kind"] in ("approval", "confirm") \
                else (p["question"] or None if p["kind"] == "question" else None)
            st = {"app": kind, "state": "blocked", "kind": PROMPT_STATUS_KINDS.get(p["kind"], "question"),
                  "msg": msg[:200] if msg else None}
        elif spec.get("title_blocked") and spec["title_blocked"].search(title):
            if prev == "blocked":
                st = prev_st  # 같은 상자에서 PC나 앱이 고르는 중(직접 입력 칸 등): 새 막힘이 아니다
            elif not settle:
                return None
            else:
                st = {"app": kind, "state": "blocked", "kind": "dialog", "msg": None}
        else:
            busy = bool(spec.get("title_working") and spec["title_working"].search(title))
            if not busy and spec.get("busy_re") and text:
                tail = [ln for ln in screen_lines(text) if ln.strip()][-8:]
                busy = any(spec["busy_re"].search(ln) for ln in tail)
            if busy:
                st = {"app": kind, "state": "working"}
            elif prev in ("working", "blocked", "done"):
                st = {"app": kind, "state": "done"}
            else:
                st = {"app": kind, "state": "idle"}
        if commit:
            if st["state"] == "done" and prev != "done":
                self._screen_turns[tid] = self._screen_turns.get(tid, 0) + 1
            self._screen_state[tid] = st
        return st

    def effective_info(self, tid, info):
        """화면으로 판정한 막힘이 있으면 status를 바꾼 사본(카드·답·목록이 막힘으로 다룬다).
        Codex는 계획 실행 메뉴만, hc status가 없는 에이전트("full")는 status 전체."""
        if agent_spec(agent_kind(info))["screen"] == "full":
            st = self._screen_status(tid, info)
            info = dict(info)
            info["status"] = st or self._screen_state.get(tid) or {"app": agent_kind(info), "state": "idle"}
            return info
        plan_st, _ = self._codex_plan_status(tid, info)
        if plan_st:
            info = dict(info)
            info["status"] = plan_st
        return info

    def refresh(self, tid, event_state=None, initial=False, name=None, detected=None, settle=False):
        """hc get으로 현재 상태를 다시 읽어 판정한다. event_state가 done이면 그것도 먼저 넣는다."""
        with self._refresh_lock:
            self._detected = detected if detected is not None else self.clock()
            try:
                info = self.hc.get(tid)
            except HcError as e:
                log.warning("hc get %s 실패: %s", tid, e)
                info = None
            if info is not None:
                name = info.get("name") or name
                if is_claude(info) and info.get("state") in (None, "live"):
                    self.tracker.mark_claude(tid, name)
                self._agent_kinds[tid] = agent_kind(info)
                turn = info.get("turn")
                st = status_of(info)
                if agent_spec(self._agent_kinds[tid])["screen"] == "full":
                    st = self._screen_status(tid, info, commit=True, settle=settle)
                    if st is None:  # 제목은 막힘인데 메뉴가 아직 안 그려졌다: 잠깐 뒤 한 번 더
                        self._later(SCREEN_SETTLE, lambda: self.refresh(tid, name=name, settle=True))
                        self._detected = None
                        return
                    turn = self._screen_turns.get(tid)
                    plan_st, plan_mode = None, False
                else:
                    plan_st, plan_mode = self._codex_plan_status(tid, info)
                if plan_st:
                    st = plan_st  # 계획 실행 메뉴: 끝남 대신 막힘으로(끝남 알림을 따로 보내지 않는다)
                elif plan_mode and st.get("state") == "done" and not settle:
                    # 플랜 모드 턴이 끝났는데 메뉴가 아직 안 그려졌다(맥미니는 늦게 그린다): 끝남 판정을 잠깐 미룬다
                    self._later(CODEX_PLAN_SETTLE, lambda: self.refresh(tid, name=name, settle=True))
                    self._detected = None
                    return
                if event_state == "done" and st.get("state") not in ("done", "blocked"):
                    self.tracker.observe(tid, {"state": "done"}, turn=turn, name=name)
                self.tracker.observe(tid, st, turn=turn, name=name, initial=initial)
            elif event_state == "done":
                self.tracker.observe(tid, {"state": "done"}, name=name)
            self._detected = None
        self._flush()

    def exited(self, tid, exit_code=None, detected=None):
        with self._refresh_lock:
            self._detected = detected if detected is not None else self.clock()
            if tid in self.tracker.known_claude():
                if exit_code is None:
                    try:
                        info = self.hc.get(tid)
                        if isinstance(info.get("exit_code"), int):
                            exit_code = info["exit_code"]
                    except (HcError, AttributeError):
                        pass
                self.tracker.exited(tid, exit_code=exit_code)
            self.tracker.forget(tid)
            self._detected = None
        self._flush()

    def poll(self, initial=False):
        self.hooks.prune()
        try:
            terms = self.hc.ls()
        except HcError as e:
            log.warning("hc ls 실패: %s", e)
            return
        live = set()
        for t in terms:
            tid = t.get("address")
            if not isinstance(tid, str) or not ID_RE.match(tid):
                continue
            if t.get("state") not in (None, "live"):
                continue
            live.add(tid)
            if self.hooks.has(tid):
                continue  # 훅 세션은 이벤트로만 판정한다
            self.refresh(tid, initial=initial, name=t.get("name"))
        for tid in self.tracker.known() - live:
            self.tracker.forget(tid)
        for tid in set(self._agent_kinds) - live:
            self._agent_kinds.pop(tid, None)
            self._screen_state.pop(tid, None)
            self._screen_turns.pop(tid, None)
        for tid in self.tracker.known_claude() - live:
            self.exited(tid)

    def check_reminders(self):
        """막힘이 계속된 터미널에 재알림. 카드는 그때 화면을 다시 읽어 새로 만든다."""
        due = self.reminders.due()
        if not due:
            return
        with self._refresh_lock:
            for tid, key, n, name in due:
                self._detected = self.clock()
                try:
                    info = self.hc.get(tid)
                except HcError as e:
                    log.warning("다시 알림 %s: hc get 실패: %s", tid, e)
                    continue
                info = self.effective_info(tid, info)
                st = status_of(info)
                if (st.get("state"), st.get("msg")) != key:
                    continue  # 그 사이 풀렸거나 바뀌었다: 다음 폴링이 판정한다
                self._agent_kinds[tid] = agent_kind(info)
                payload = build_payload(tid, info.get("name") or name, "blocked", st,
                                        prompt=self._prompt_for_push(tid))
                payload["reminder"] = n
                alert = payload["aps"]["alert"]
                alert["subtitle"] = "Reminder · " + alert["subtitle"]
                alert["subtitle-loc-key"] = REMINDER_SUBTITLE_KEYS[payload["aps"]["category"]]
                log.info("다시 알림 %s %d번째", tid, n)
                self._notify(payload)
            self._detected = None
        self._flush()

    def handle_event_line(self, line):
        t0 = self.clock()
        try:
            ev = json.loads(line)
        except ValueError:
            return
        if not isinstance(ev, dict) or ev.get("method") != "event":
            return
        p = ev.get("params") or {}
        tid = p.get("terminal")
        if not isinstance(tid, str) or not ID_RE.match(tid):
            return
        typ = p.get("type")
        data = p.get("data") if isinstance(p.get("data"), dict) else {}
        if typ == "title":
            # hc status가 없는 에이전트는 제목(✋/✦/◇)이 바뀔 때 화면을 본다. 처음 보는 터미널은 한 번 판별한다.
            kind = self._agent_kinds.get(tid, "?")
            if kind == "?" or agent_spec(kind)["screen"] == "full":
                self.refresh(tid, detected=t0)
        elif typ in ("status", "turn"):
            st = data.get("status") if isinstance(data.get("status"), dict) else {}
            ev_state = st.get("state") or data.get("state")
            self.refresh(tid, event_state=ev_state, detected=t0)
        elif typ in ("finished", "removed"):
            code = data.get("exit_code")
            self.exited(tid, exit_code=code if isinstance(code, int) and not isinstance(code, bool) else None,
                        detected=t0)

    # --- 감시 스레드
    def events_loop(self, popen=None):
        popen = popen or subprocess.Popen
        backoff = 1
        while not self.stop.is_set():
            if not self.hc.hc_enabled():  # hc 없는 설치: 경고는 hc_enabled가 한 번만 남긴다
                self.stop.wait(EVENTS_MAX_BACKOFF)
                continue
            started = time.time()
            try:
                proc = popen(self.hc.events_argv(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             stdin=subprocess.DEVNULL, text=True, bufsize=1, shell=False,
                             **subprocess_kwargs(text=True))
                log.info("hc events 연결")
                for line in proc.stdout:
                    if self.stop.is_set():
                        break
                    try:
                        self.handle_event_line(line)
                    except Exception:  # noqa: BLE001
                        log.exception("이벤트 처리 오류")
                proc.kill()
                proc.wait(timeout=5)
            except Exception as e:  # noqa: BLE001
                log.warning("hc events 오류: %s", e)
            if time.time() - started > 60:
                backoff = 1
            log.info("hc events 끊김, %d초 뒤 재연결", backoff)
            self.stop.wait(backoff)
            backoff = min(backoff * 2, EVENTS_MAX_BACKOFF)

    def screen_poll(self):
        """hc status가 없는 에이전트("full") 터미널만 화면으로 다시 판정한다(제목 이벤트를 놓쳐도 잡히게)."""
        for tid, kind in list(self._agent_kinds.items()):
            if kind and agent_spec(kind)["screen"] == "full":
                self.refresh(tid)

    def screen_poll_loop(self):
        while not self.stop.wait(SCREEN_POLL_INTERVAL):
            try:
                self.screen_poll()
            except Exception:  # noqa: BLE001
                log.exception("화면 판정 오류")

    def poll_loop(self):
        while not self.stop.wait(POLL_INTERVAL):
            try:
                self.poll()
            except Exception:  # noqa: BLE001
                log.exception("폴링 오류")
            try:
                self.check_reminders()
            except Exception:  # noqa: BLE001
                log.exception("다시 알림 오류")

    # --- 훅 세션
    def hook_event(self, agent, body, alive=lambda: True):
        """POST /hook/<agent> 한 건. 반환: 응답 JSON(dict), None(빈 본문), HOOK_NO_REPLY(이미 끊김)."""
        if agent not in HOOK_AGENTS:
            raise ApiError(404, "no_route")
        ev = body.get("hook_event_name")
        tid = hook_session_tid(body.get("session_id"))
        if not isinstance(ev, str) or tid is None or not ID_RE.match(tid):
            raise ApiError(400, "bad_hook")
        if self.hooks.has(tid) and self.hooks.get(tid)["agent"] != agent:
            raise ApiError(409, "agent_mismatch")
        if ev == "SessionEnd":
            if self.hooks.has(tid):
                self._hook_end(tid, body.get("reason"))
            return None
        new = self.hooks.touch(tid, agent, body, self.home)
        if new:
            self._hook_observe(tid)  # 기준선(idle): 알림 없음
        if ev == "UserPromptSubmit":
            self.hooks.set_base(tid, "working")
            self._hook_observe(tid)
            return None
        if ev == "Stop":
            # 턴 하나가 끝났다. UserPromptSubmit 없이 Stop만 연달아 와도(Codex) 턴 id(turn_id, Claude는 prompt_id)가
            # 바뀌었으면 새 턴으로 보고 working을 먼저 넣어 끝남을 다시 알린다. 같은 턴의 Stop이 또 오면
            # (다른 Stop 훅이 막아 이어 간 경우) Tracker가 같은 관측으로 무시한다.
            if self.hooks.new_stop(tid, body.get("turn_id") or body.get("prompt_id")):
                self.hooks.set_base(tid, "working")
                self._hook_observe(tid)
            self.hooks.set_base(tid, "done", new_turn=True)
            self._hook_observe(tid)
            return None
        tool = body.get("tool_name")
        if ev == "PermissionRequest":
            if tool in HOOK_HOLD_TOOLS:
                return {}  # 질문·계획은 PreToolUse가 붙잡는다
            return self._hook_hold(tid, hook_permission_hold(agent, body), alive)
        if ev == "PreToolUse":
            hold = None
            if agent == "claude" and tool == "AskUserQuestion":
                hold = hook_question_hold(agent, body)
            elif agent == "claude" and tool == "ExitPlanMode":
                hold = hook_plan_hold(agent, body)
            if hold is None:
                return {}
            return self._hook_hold(tid, hold, alive)
        return None

    def _hook_observe(self, tid):
        """훅 세션의 지금 상태를 Tracker에 넣는다(막힘·끝남 알림, 재알림 일정)."""
        with self._refresh_lock:
            self._detected = self.clock()
            try:
                info = self.hooks.get(tid)
            except HcError:
                info = None
            if info is not None:
                if info["agent"] == "claude":
                    self.tracker.mark_claude(tid, info["name"])
                self._agent_kinds[tid] = info["agent"]
                self.tracker.observe(tid, info["status"], turn=info["turn"], name=info["name"])
            self._detected = None
        self._flush()

    def _hook_end(self, tid, reason):
        """SessionEnd: 세션을 지우고, 붙잡힌 요청은 풀고, Claude가 끝난 것이면(/clear·/resume 제외) 종료 알림."""
        s = self.hooks.remove(tid)
        with self._refresh_lock:
            self._detected = self.clock()
            if reason not in ("clear", "resume") and tid in self.tracker.known_claude():
                self.tracker.exited(tid, name=(s or {}).get("name"))
            self.tracker.forget(tid)
            with self.tracker.lock:
                self.tracker.claude_seen.pop(tid, None)
            self._agent_kinds.pop(tid, None)
            self._detected = None
        self._flush()
        log.info("훅 세션 끝 %s (%s)", tid, reason)

    def _hook_hold(self, tid, hold, alive):
        """폰 답·연결 끊김·시간 초과 중 먼저 오는 것까지 이 요청(스레드)을 붙잡는다."""
        if self.hooks.add_hold(tid, hold):
            self._hook_observe(tid)
        log.info("훅 붙잡기 %s %s %s", tid, hold.event_name, hold.tool_name)
        end = self.hook_clock() + self.hook_hold_sec
        while not hold.event.wait(HOOK_POLL):
            if not alive():
                if self.hooks.finish(hold, "gone"):
                    break
            elif self.hook_clock() >= end:
                if self.hooks.finish(hold, "expired"):
                    break
        if hold.state == "expired":
            # 결정 없이 돌려준다 → 터미널 대화상자. 막힘은 그대로 두고(PC에서 답해야 한다) 폰 답은 needs_pc.
            log.info("훅 붙잡기 시간 초과 %s", tid)
            if self.hooks.queued(tid) > 1:  # 뒤에 기다리는 요청이 있으면 그것을 지금 카드로
                self.hooks.pop_hold(tid, hold)
                self._hook_observe(tid)
            return {}
        self.hooks.pop_hold(tid, hold)
        self._hook_observe(tid)
        if hold.state == "gone":
            log.info("훅 연결 끊김 %s (PC에서 답함)", tid)
            return HOOK_NO_REPLY
        return hold.result

    def _hook_active(self, tid, pid=None):
        """답할 붙잡기와 카드. guard: 없으면 not_blocked/prompt_changed, 시간 초과면 needs_pc, 카드 id가 다르면 prompt_changed."""
        hold = self.hooks.active(tid)
        if hold is None or hold.state == "gone":
            if pid is not None:
                raise ApiError(409, "prompt_changed", prompt=None)
            raise ApiError(409, "not_blocked")
        if hold.state == "expired":
            raise ApiError(409, "needs_pc")
        p = hold.prompt()
        if pid is not None and (p is None or p["id"] != pid):
            raise ApiError(409, "prompt_changed", prompt=p)
        return hold, p

    def _hook_decide(self, tid, hold, result):
        if not self.hooks.finish(hold, "decided", result):
            raise ApiError(409, "not_blocked")  # 그사이 PC에서 답했거나 시간이 지났다
        log.info("훅 답 %s %s", tid, result["hookSpecificOutput"].get("permissionDecision")
                 or result["hookSpecificOutput"]["decision"]["behavior"])

    def hook_api_answer(self, tid, body):
        """잠금화면 버튼(허락·계속 허용·거절). 키 대신 훅 결정으로 답한다."""
        action = body.get("action")
        pid = body.get("prompt_id")
        if pid is not None and (not isinstance(pid, str) or not PROMPT_ID_RE.match(pid)):
            raise ApiError(400, "bad_prompt_id")
        if pid is None and action == "always":
            raise ApiError(400, "needs_prompt_id")
        expect = body.get("expect_msg")
        if expect is not None and not isinstance(expect, str):
            raise ApiError(400, "bad_expect_msg")
        with self.term_lock(tid):
            hold, p = self._hook_active(tid, pid)
            msg = hold.status.get("msg")
            if expect is not None and expect != msg:
                raise ApiError(409, "changed", msg=msg)
            if action == "reject":
                result = hold.deny()
            elif hold.kind == "question":
                raise ApiError(409, "needs_manual", prompt=p)
            elif action == "always":
                if not hold.always_perms or hold.agent != "claude":
                    raise ApiError(409, "no_always", prompt=p)
                result = hold.allow(perms=hold.always_perms)
            elif hold.kind == "plan":
                result = hold.allow(updated_input=hold.tool_input)
            else:
                result = hold.allow()
            self._hook_decide(tid, hold, result)
        return {"ok": True, "keys": [], "decision": "deny" if action == "reject" else "allow"}

    def hook_prompt_answer(self, tid, pid, oi, ois, text):
        """카드 답(선택지·복수 선택·자유 입력). 검사는 api_prompt_answer가 이미 했다."""
        with self.term_lock(tid):
            hold, p = self._hook_active(tid, pid)
            n = len(p["options"])
            free = p.get("free_text_index")
            if text is not None:
                if free is None or p["multi_select"]:
                    raise ApiError(400, "no_free_text")
                if "\n" in text or "\t" in text:
                    raise ApiError(400, "bad_text")
            elif ois is not None:
                if not p["multi_select"]:
                    raise ApiError(400, "not_multi_select")
                if not ois or any(not 0 <= i < n for i in ois):
                    raise ApiError(400, "bad_option")
            else:
                if not 0 <= oi < n:
                    raise ApiError(400, "bad_option")
                if p["multi_select"]:
                    raise ApiError(400, "use_option_indices")
                if oi == free:
                    raise ApiError(400, "needs_text")
            if hold.kind == "question":
                if text is not None:
                    value = text
                elif ois is not None:
                    value = ", ".join(p["options"][i]["label"] for i in sorted(set(ois)))
                else:
                    value = p["options"][oi]["label"]
                if not self.hooks.advance_question(hold, p["question"], value):
                    log.info("훅 질문 %s %d/%d", tid, hold.qi, len(hold.prompts))
                    return {"ok": True}
                result = hold.allow(updated_input=dict(hold.tool_input, answers=dict(hold.answers)))
            elif hold.kind == "plan":
                if text is not None:
                    result = hold.deny(text)  # "Tell Claude what to change": 계획을 고치라는 말로 돌려준다
                elif oi == 0:
                    result = hold.allow(updated_input=hold.tool_input)
                else:
                    result = hold.deny()
            else:
                if text is not None:
                    raise ApiError(400, "no_free_text")
                if oi == p.get("reject_index"):
                    result = hold.deny()
                elif oi == p.get("always_index"):
                    if not hold.always_perms or hold.agent != "claude":
                        raise ApiError(409, "no_always", prompt=p)
                    result = hold.allow(perms=hold.always_perms)
                else:
                    result = hold.allow()
            self._hook_decide(tid, hold, result)
        return {"ok": True}

    def _hook_preview(self, tid):
        _, path = self.hooks.transcript(tid)
        path = hook_transcript_path(path, self.home)
        return preview_for_path(path) if path else None

    # --- API
    def term_lock(self, tid):
        """터미널별 잠금: 화면 재확인부터 키 전송 끝까지 한 요청만."""
        with self._term_locks_guard:
            lk = self._term_locks.get(tid)
            if lk is None:
                lk = self._term_locks[tid] = threading.Lock()
            return lk

    def require_terminal(self, tid):
        if not isinstance(tid, str) or not ID_RE.match(tid):
            raise ApiError(400, "bad_id")
        if self.hooks.has(tid):
            for t in self.hooks.ls():
                if t.get("address") == tid:
                    return t
        try:
            terms = self.hc.ls()
        except HcError:
            raise ApiError(502, "hc_failed")
        for t in terms:
            if t.get("address") == tid:
                return t
        raise ApiError(404, "not_found")

    def get_info(self, tid):
        try:
            return self.hc.get(tid)
        except HcError:
            raise ApiError(502, "hc_failed")

    def require_claude(self, tid):
        """답·카드·대화 보기·키·메시지를 지원하는 에이전트(Claude Code, Codex)만. 오류 이름은 옛 앱 호환으로 not_claude."""
        self.require_terminal(tid)
        info = self.get_info(tid)
        if agent_kind(info) is None:
            raise ApiError(403, "not_claude")
        return info

    def api_terminals(self):
        try:
            terms = self.hc.ls()
        except HcError:
            raise ApiError(502, "hc_failed")
        out = []
        for t in terms:
            tid = t.get("address")
            if not isinstance(tid, str) or not ID_RE.match(tid):
                continue
            item = {"id": tid, "name": t.get("name"), "cwd": live_cwd(t), "agent": t.get("agent"),
                    "state": t.get("state"), "status": None, "status_ms": None, "title": None,
                    "claude": agent_kind(t) is not None, "agent_kind": agent_kind(t), "preview": None,
                    "blocked_since": None}
            try:
                info = self.hc.get(tid)
            except HcError:
                info = None
            if isinstance(info, dict):
                info = self.effective_info(tid, info)
                for k in ("name", "agent", "state"):
                    if info.get(k) is not None:
                        item[k] = info.get(k)
                item["cwd"] = live_cwd(info) or item["cwd"]
                item["status"] = info.get("status") if isinstance(info.get("status"), dict) else None
                item["status_ms"] = info.get("status_ms")
                item["title"] = info.get("title")
                # "claude"는 앱이 대화 보기·막힘 구역을 켜는 기준이라 Codex도 true(옛 앱이 그대로 Codex를 지원한다).
                item["agent_kind"] = agent_kind(info)
                item["claude"] = item["agent_kind"] is not None
                if item["claude"]:
                    item["preview"] = (self._hook_preview(tid) if self.hooks.has(tid)
                                       else terminal_preview(info, self.home, self.runner))
            item["caps"] = [] if self.hooks.has(tid) else list(HOOK_CAPS)
            out.append(item)
        since = self.blocked_since.update(
            {i["id"]: (i["status"] or {}).get("state") == "blocked" for i in out})
        for i in out:
            i["blocked_since"] = since.get(i["id"])
            i["branch"] = self.branches.get(i.get("cwd"))
        return {"terminals": out}

    def api_screen(self, tid, lines):
        self.require_terminal(tid)
        try:
            n = int(lines) if lines not in (None, "") else 0
        except ValueError:
            raise ApiError(400, "bad_lines")
        if not 0 <= n <= 300:
            raise ApiError(400, "bad_lines")
        try:
            r = self.hc.read(tid, n)
        except HcError:
            raise ApiError(502, "hc_failed")
        return {"text": r.get("text", ""), "complete": r.get("complete")}

    def api_answer(self, tid, body):
        action = body.get("action")
        if action not in ("approve", "always", "reject"):
            raise ApiError(400, "bad_action")
        if self.hooks.has(tid):
            return self.hook_api_answer(tid, body)
        pid = body.get("prompt_id")
        if pid is not None:
            if not isinstance(pid, str) or not PROMPT_ID_RE.match(pid):
                raise ApiError(400, "bad_prompt_id")
            with self.term_lock(tid):
                info = self.require_claude(tid)
                p = self._same_prompt(tid, info, pid)
                self._check_answerable(p)
                if p["kind"] not in ("approval", "plan", "confirm"):
                    raise ApiError(409, "needs_manual", prompt=p)
                if p["kind"] == "plan" and action != "reject":
                    raise ApiError(409, "needs_manual", prompt=p)  # 계획 승인은 auto mode 등 갈림길이라 앱에서
                labels = [o["label"] for o in p["options"]]
                if action == "approve":
                    # 승인 글자가 Yes가 아닌 에이전트(Gemini "Allow once")는 파서가 approve_index를 정해 둔다
                    idx = p["approve_index"] if "approve_index" in p else approve_index(labels)
                    if idx is None:
                        raise ApiError(409, "needs_manual", prompt=p)
                elif action == "always":
                    idx = p.get("always_index")
                    if idx is None:
                        raise ApiError(409, "no_always", prompt=p)
                else:
                    idx = p.get("reject_index")
                if idx is None:
                    keys = ["esc"]
                    self._keys(tid, keys)
                else:
                    keys = self._do_steps(tid, answer_steps(p, option_index=idx))
            log.info("answer %s %s (카드)", tid, action)
            return {"ok": True, "keys": keys}
        if action == "always":
            raise ApiError(400, "needs_prompt_id")
        expect = body.get("expect_msg")
        if expect is not None and not isinstance(expect, str):
            raise ApiError(400, "bad_expect_msg")
        with self.term_lock(tid):
            info = self.require_claude(tid)
            keys = decide_answer(self.effective_info(tid, info), action, expect)
            self._keys(tid, keys)
        log.info("answer %s %s", tid, action)
        return {"ok": True, "keys": keys}

    # --- 메뉴 카드
    def current_prompt(self, tid, info):
        info = self.effective_info(tid, info)
        if status_of(info).get("state") != "blocked":
            return None
        try:
            r = self.hc.read(tid, 0)
        except HcError:
            raise ApiError(502, "hc_failed")
        return self.parse_screen((r or {}).get("text", ""), agent_kind(info))

    def _same_prompt(self, tid, info, pid):
        """키를 보내기 직전에 화면을 다시 읽어 같은 카드인지(힌트가 화면 끝에 살아 있는지) 확인."""
        p = self.current_prompt(tid, info)
        if p is None or p["id"] != pid:
            raise ApiError(409, "prompt_changed", prompt=p)
        return p

    @staticmethod
    def _check_answerable(p):
        if p["kind"] == "approval" and p.get("body") is None:
            # 패널(명령 내용)을 못 읽었으면 prompt_id가 무엇을 승인하는지 보장하지 못한다
            raise ApiError(409, "needs_manual", prompt=p)

    def _do_steps(self, tid, steps):
        sent = []
        for kind, val in steps:
            if kind == "keys":
                self._keys(tid, val)
                sent += val
            else:
                try:
                    self.hc.send(tid, val, force=True)
                except HcError as e:
                    raise ApiError(409, "hc_refused", detail=str(e)[:200])
                sent.append("<text>")
        return sent

    def api_prompt(self, tid):
        if self.hooks.has(tid):
            return {"prompt": self.hooks.current_prompt(tid)}
        info = self.require_claude(tid)
        return {"prompt": self.current_prompt(tid, info)}

    def api_prompt_answer(self, tid, body):
        pid = body.get("prompt_id")
        if not isinstance(pid, str) or not PROMPT_ID_RE.match(pid):
            raise ApiError(400, "bad_prompt_id")
        given = [k for k in ("option_index", "option_indices", "text") if body.get(k) is not None]
        if len(given) != 1:
            raise ApiError(400, "bad_answer")
        oi = body.get("option_index")
        ois = body.get("option_indices")
        text = body.get("text")
        if oi is not None and (not isinstance(oi, int) or isinstance(oi, bool)):
            raise ApiError(400, "bad_option")
        if ois is not None:
            if (not isinstance(ois, list) or len(ois) > 20
                    or any(not isinstance(i, int) or isinstance(i, bool) for i in ois)):
                raise ApiError(400, "bad_option")
        if text is not None:
            validate_text(text)
        if self.hooks.has(tid):
            return self.hook_prompt_answer(tid, pid, oi, ois, text)
        with self.term_lock(tid):
            info = self.require_claude(tid)
            p = self._same_prompt(tid, info, pid)
            if ois is not None and any(not 0 <= i < len(p["options"]) for i in ois):
                raise ApiError(400, "bad_option")
            self._check_answerable(p)
            steps = answer_steps(p, option_index=oi, option_indices=ois, text=text)
            self._do_steps(tid, steps)
        log.info("prompt answer %s %s %s", tid, p["kind"], given[0])
        return {"ok": True}

    # --- 대화 기록
    def api_transcript(self, tid, limit):
        try:
            n = int(limit) if limit not in (None, "") else 50
        except ValueError:
            raise ApiError(400, "bad_limit")
        if not 1 <= n <= 200:
            raise ApiError(400, "bad_limit")
        if self.hooks.has(tid):
            sid, path = self.hooks.transcript(tid)
            path = hook_transcript_path(path, self.home)
        else:
            info = self.require_claude(tid)
            sid, path = find_agent_transcript(info, self.home, runner=self.runner)
        if not path:
            return {"session_id": sid, "turns": [], "truncated": False}
        try:
            turns, cut = read_transcript(path, n)
        except OSError:
            return {"session_id": sid, "turns": [], "truncated": False}
        return {"session_id": sid, "turns": turns, "truncated": cut}

    # --- 폴더·새 에이전트
    def api_folders(self, rel):
        p = resolve_home_path(rel or "", self.home)
        dirs = []
        try:
            with os.scandir(p) as it:
                for e in it:
                    if e.name.startswith(".") or (p == self.home and e.name in hidden_home_dirs()):
                        continue
                    try:
                        if not e.is_dir(follow_symlinks=True):
                            continue
                        if e.is_symlink() and rel_to_home(os.path.realpath(e.path), self.home) is None:
                            continue
                    except OSError:
                        continue
                    dirs.append(e.name)
        except OSError:
            raise ApiError(400, "bad_path")
        dirs.sort(key=lambda x: x.lower())
        recent = []
        try:
            terms = self.hc.ls()
        except HcError:
            terms = []
        for t in terms:
            cwd = live_cwd(t)
            if not isinstance(cwd, str):
                continue
            r = rel_to_home(os.path.realpath(cwd), self.home)
            # 홈 자체(빈 경로)는 "최근 폴더"로 쓸모가 없다(앱에 이름 없는 줄이 생긴다).
            if r and r not in recent and os.path.isdir(os.path.join(self.home, r)):
                recent.append(r)
        return {"path": rel_to_home(p, self.home) or "", "dirs": dirs[:500], "recent": recent[:20]}

    def api_agents(self, body):
        p = resolve_home_path(body.get("path", ""), self.home)
        prompt = body.get("prompt")
        if prompt is not None:
            if prompt == "":
                prompt = None
            else:
                validate_text(prompt)
        agent = body.get("agent")
        if agent is None or agent == "claude":
            agent_id = "claude"
        else:
            agent_id = agent
            argv = self.agent_catalog.launch_argv(agent_id, prompt)  # 400: unknown_agent·not_installed·prompt_unsupported
        try:
            if agent_id == "claude":
                r = (self.hc.run_claude(p, prompt, exe=self.agent_catalog.executable("claude"))
                     if IS_WINDOWS else self.hc.run_claude(p, prompt))
            else:
                r = self.hc.run_agent(p, argv)
        except HcError as e:
            raise ApiError(502, "hc_failed", detail=str(e)[:200])
        addr = (r or {}).get("address")
        if not isinstance(addr, str) or not ID_RE.match(addr):
            raise ApiError(502, "hc_failed")
        log.info("새 에이전트 %s (%s)", addr, agent_id)
        return {"id": addr}

    def api_agent_catalog(self):
        return {"agents": self.agent_catalog.entries()}

    def api_keys(self, tid, body):
        keys = validate_keys(body.get("keys"))
        with self.term_lock(tid):
            self.require_claude(tid)
            self._keys(tid, keys)
        log.info("keys %s %s", tid, " ".join(keys))
        return {"ok": True}

    def api_send(self, tid, body):
        text = validate_text(body.get("text"))
        with self.term_lock(tid):
            info = self.require_claude(tid)
            if agent_spec(agent_kind(info))["screen"] == "full" \
                    and status_of(self.effective_info(tid, info)).get("state") == "blocked":
                # hc는 이 에이전트의 대화 상자를 모른다: 그대로 보내면 메뉴에 글자가 들어간다
                raise ApiError(409, "hc_refused", detail="blocked")
            try:
                self.hc.send(tid, text)
            except HcError as e:
                raise ApiError(409, "hc_refused", detail=str(e)[:200])
        log.info("send %s (%d자)", tid, len(text))
        return {"ok": True}

    def _keys(self, tid, keys):
        try:
            self.hc.keys(tid, keys)
        except HcError as e:
            raise ApiError(502, "hc_failed", detail=str(e)[:200])

    def api_add_device(self, body):
        tok = body.get("token")
        env = body.get("env")
        if env == FCM_ENV:
            # FCM 토큰은 hex가 아니고 대소문자를 가린다: 그대로 저장한다.
            if not isinstance(tok, str) or not FCM_TOKEN_RE.match(tok):
                raise ApiError(400, "bad_token")
        else:
            if not isinstance(tok, str) or not DEVICE_TOKEN_RE.match(tok):
                raise ApiError(400, "bad_token")
            if env not in APNS_ENVS:
                raise ApiError(400, "bad_env")
            tok = tok.lower()
        sig = body.get("relay_sig")
        if sig is not None and (not isinstance(sig, str) or not RELAY_SIG_RE.match(sig)):
            raise ApiError(400, "bad_relay_sig")
        self.devices.add(tok, env, relay_sig=sig)
        log.info("기기 등록 %s %s%s", short_token(tok), env, " (중계 서명 있음)" if sig else "")
        return {"ok": True}

    def api_delete_device(self, body):
        tok = body.get("token")
        if not isinstance(tok, str) or not (DEVICE_TOKEN_RE.match(tok) or FCM_TOKEN_RE.match(tok)):
            raise ApiError(400, "bad_token")
        # APNs 토큰은 소문자로, FCM 토큰은 그대로 저장돼 있다.
        removed = self.devices.remove(tok) or self.devices.remove(tok.lower())
        return {"ok": True, "removed": removed}

    # --- 알림 상태(무음 실패 방지)
    def push_mode(self):
        if self.cfg.get("push_relay_url"):
            return "relay"
        if os.path.isfile(self.cfg.get("apns_key_path") or ""):
            return "apns"
        return "none"

    def push_health(self, did=None):
        """/api/health의 push. failing: 등록 기기가 있고 마지막 결과가 실패이며 그 실패가 24시간 안.
        did(기기 ID)를 주면 그 기기 기록으로만 판정한다(다른 기기의 실패로 경고가 뜨지 않게)."""
        s = self.push_log.summary()
        err, ok = s["last_error_at"], s["last_success_at"]
        if isinstance(did, str) and re.fullmatch(r"[0-9a-f]{16}", did):
            e = self.push_log.device(did) or {}
            err = (e.get("last_error") or {}).get("at")
            ok = e.get("last_success_at")
        failing = (err is not None and (ok is None or err > ok)
                   and self.push_log.now_ms() - err <= PUSH_FAILING_WINDOW_MS and bool(self.devices.list()))
        return {"last_success_at": ok, "relay_limited_at": s["relay_limited_at"], "failing": failing}

    @staticmethod
    def _device_id_arg(did):
        if not isinstance(did, str) or not DEVICE_ID_RE.match(did.lower()):
            raise ApiError(400, "bad_device")
        return did.lower()

    def api_push_status(self, did=None):
        devs = self.devices.list()
        s = self.push_log.summary()
        res = {"mode": self.push_mode(), "devices": len(devs), "last_success_at": s["last_success_at"],
               "relay_limited_at": s["relay_limited_at"], "reminder_after_sec": list(self.reminders.intervals),
               "device": None}
        if did is not None:
            did = self._device_id_arg(did)
            e = self.push_log.device(did) or {}
            res["device"] = {"id": did, "registered": any(device_id(d["token"]) == did for d in devs),
                             "last_success_at": e.get("last_success_at"), "last_attempt_at": e.get("last_attempt_at"),
                             "last_error": e.get("last_error")}
        return res

    def test_payload(self):
        return {
            "aps": {
                "alert": test_alert(),
                "sound": "default",
                "category": "HR_TEST",
                "thread-id": "sheltie-test",
            },
            "terminal": None,
            "state": "test",
            "category": "HR_TEST",
            "server": server_tag(self.cfg.get("token")),
        }

    def api_push_test(self, body):
        """기기 하나에 시험 알림을 동기로 보낸다. 결과는 PushLog에도 남는다."""
        did = self._device_id_arg(body.get("device"))
        dev = next((d for d in self.devices.list() if device_id(d["token"]) == did), None)
        # Android 기기는 APNs 모드와 상관없이 중계로 간다(중계가 꺼져 있으면 아래에서 실패로 기록·응답).
        if (dev is None or dev.get("env") != FCM_ENV) and self.push_mode() == "none":
            raise ApiError(409, "push_unavailable")
        if dev is None:
            raise ApiError(404, "device_not_registered")
        now = self.clock()
        with self._test_push_lock:
            last = self._test_push_at.get(did)
            if last is not None and now - last < PUSH_TEST_MIN_GAP:
                raise ApiError(429, "too_soon")
            self._test_push_at[did] = now
        t0 = self.clock()
        code, reason = self.pusher.push_device(dev, self.test_payload())
        elapsed = int((self.clock() - t0) * 1000)
        log.info("시험 알림 %s: %d %s %dms", did, code, reason, elapsed)
        return {"ok": code == 200, "code": code, "reason": reason, "elapsed_ms": elapsed}


# ---------------------------------------------------------------- 에이전트 목록 (새 작업에서 고르기)
# 새 작업에 쓸 수 있는 에이전트는 이 허용 목록(AGENT_CATALOG)에 있는 것뿐이다. 설치 감지 방식은 Ovis(MIT)
# server/agent-catalog-detect.ts를 따랐다: PATH에서 실행 파일을 찾고, 없으면 설치 프로그램이 흔히 쓰는 폴더도 본다.
# 임의 명령 실행 경로는 없다: 요청의 id는 허용 목록 키로만 쓰고, 실행 프로그램 이름은 상수에서 온다.
# prompt: 첫 지시를 `-- <지시>`로 넘기는 방식이 실제 --help로 확인된 것만 True(codex, gemini, grok).
# 확인 못 한 에이전트는 지시 없이 시작만 허용한다(지시를 보내면 400 prompt_unsupported).

AGENT_CATALOG = (
    # (id, 표시 이름, 실행 파일, 첫 지시 전달)
    ("claude", "Claude Code", "claude", True),
    ("codex", "Codex", "codex", True),
    ("gemini", "Gemini CLI", "gemini", True),
    ("grok", "Grok Build", "grok", True),
    ("cursor", "Cursor Agent", "cursor-agent", False),
    ("opencode", "OpenCode", "opencode", False),
    ("copilot", "GitHub Copilot", "copilot", False),
    ("amp", "Amp", "amp", False),
    ("qwen", "Qwen Code", "qwen", False),
)
AGENT_BY_ID = {a[0]: a for a in AGENT_CATALOG}
AGENT_EXTRA_DIRS_MAC = ("~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin", "~/.bun/bin", "~/.npm-global/bin",
                        "~/.volta/bin", "~/.cargo/bin", "~/.opencode/bin", "~/.grok/bin", "~/.cursor/bin")
# Windows: ~/는 홈, %APPDATA%·%LOCALAPPDATA%는 환경변수(없으면 건너뛴다)
AGENT_EXTRA_DIRS_WIN = ("~/.local/bin", "%APPDATA%/npm", "~/.bun/bin", "~/.cargo/bin", "~/.volta/bin",
                        "~/.npm-global", "~/.opencode/bin", "~/.grok/bin", "~/.cursor/bin",
                        "%LOCALAPPDATA%/Microsoft/WinGet/Links", "%LOCALAPPDATA%/Programs/cursor-agent")
AGENT_EXTRA_DIRS = AGENT_EXTRA_DIRS_WIN if IS_WINDOWS else AGENT_EXTRA_DIRS_MAC
WIN_EXEC_EXTS = (".exe", ".com", ".cmd", ".bat")  # .exe 우선(.cmd·.bat은 hc가 직접 못 띄울 수 있다: 미확인)
AGENT_CATALOG_TTL = 30.0


def find_executable(name, dirs):
    if IS_WINDOWS:
        # 확장자별로 모든 폴더를 훑는다: PATH 앞쪽의 .cmd보다 뒤쪽의 .exe를 고른다.
        exts = [""] if name.lower().endswith(WIN_EXEC_EXTS) else list(WIN_EXEC_EXTS)
    else:
        exts = [""]
    for ext in exts:
        for d in dirs:
            if not d or not is_abs_path(d):  # 상대 경로·빈 항목은 서버 작업 폴더의 파일을 실행하게 되므로 제외
                continue
            p = os.path.join(d, name + ext)
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p
    return None


def agent_search_dirs(home, path_env=None, extra=AGENT_EXTRA_DIRS):
    path_env = os.environ.get("PATH", "") if path_env is None else path_env
    dirs = [d for d in path_env.split(os.pathsep) if d]
    for d in extra:
        if d.startswith("%"):
            var, _, rest = d[1:].partition("%")
            base = os.environ.get(var)
            if not base:
                continue
            d = os.path.join(base, rest.lstrip("/"))
        d = os.path.join(home, d[2:]) if d.startswith("~/") else d
        if d not in dirs:
            dirs.append(d)
    return dirs


def agent_argv(agent_id, prompt=None):
    """hc run 뒤에 붙일 argv. 프로그램 이름은 허용 목록 상수, 지시는 `--` 뒤라 옵션으로 읽히지 않는다."""
    _id, _name, binary, takes_prompt = AGENT_BY_ID[agent_id]
    argv = [binary]
    if prompt:
        if not takes_prompt:
            raise ApiError(400, "prompt_unsupported")
        argv += ["--", prompt]
    return argv


class AgentCatalog:
    def __init__(self, home, path_env=None, clock=time.monotonic, ttl=AGENT_CATALOG_TTL, extra_dirs=AGENT_EXTRA_DIRS):
        self.home = home
        self.extra_dirs = extra_dirs
        self.path_env = path_env
        self.clock = clock
        self.ttl = ttl
        self._cache = None
        self._paths = {}
        self._at = 0.0
        self._lock = threading.Lock()

    def _scan(self):
        now = self.clock()
        if self._cache is None or now - self._at >= self.ttl:
            dirs = agent_search_dirs(self.home, self.path_env, self.extra_dirs)
            self._paths = {a[0]: find_executable(a[2], dirs) for a in AGENT_CATALOG}
            self._cache = {k: v is not None for k, v in self._paths.items()}
            self._at = now

    def installed(self):
        with self._lock:
            self._scan()
            return dict(self._cache)

    def executable(self, agent_id):
        """찾은 실행 파일 절대 경로(없으면 None)."""
        with self._lock:
            self._scan()
            return self._paths.get(agent_id)

    def entries(self):
        inst = self.installed()
        return [{"id": a[0], "name": a[1], "installed": inst[a[0]], "prompt": a[3]} for a in AGENT_CATALOG]

    def launch_argv(self, agent_id, prompt=None):
        if not isinstance(agent_id, str) or agent_id not in AGENT_BY_ID:
            raise ApiError(400, "unknown_agent")
        if not self.installed()[agent_id]:
            raise ApiError(400, "not_installed")
        argv = agent_argv(agent_id, prompt)
        if IS_WINDOWS:  # hc 코어는 PATH를 띄운 환경에서 물려받아 이름만으로는 못 찾는다: 절대 경로로 넘긴다
            argv[0] = self.executable(agent_id) or argv[0]
        return argv


# ---------------------------------------------------------------- 사용량 (Usage)
# 한도(Claude OAuth usage API)와 토큰·비용(~/.claude/projects/**/*.jsonl 집계).
# 한도 쪽 로직은 Ovis(MIT) server/usage.ts의 Claude 부분을 옮겼다. 토큰은 로그·응답에 넣지 않는다.

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
USAGE_USER_AGENT = "sheltie"
USAGE_FRESH = 300.0         # 성공한 결과를 다시 쓰는 시간(초)
USAGE_MIN_REFRESH = 30.0    # refresh=1이어도 이 안에서는 다시 묻지 않는다
USAGE_RETRY = 60.0          # 실패한 뒤 다시 묻기까지
USAGE_CMD_TIMEOUT = 5.0
USAGE_MAX_READ = 1024 * 1024
USAGE_RANGES = {"today": 1, "7d": 7, "30d": 30}

# 가격 USD/MTok: (입력, 출력, 캐시 읽기, 캐시 쓰기(5분) = 입력의 1.25배). Anthropic 공식 API 가격
# (claude-api 스킬 모델 표, 2026-11-03 기준). 모르는 모델은 비용 0.
USAGE_PRICES = {
    "fable_52": (8.0, 40.0, 0.20, 10.0),     # Fable/Mythos 5.2 이상
    "fable_5": (10.0, 50.0, 1.0, 12.5),      # Fable/Mythos 5, 5.1
    "opus_55": (4.0, 20.0, 0.20, 5.0),       # Opus 5.5 이상
    "opus_new": (5.0, 25.0, 0.50, 6.25),     # Opus 4.5 ~ 5
    "opus_old": (15.0, 75.0, 1.50, 18.75),   # Opus 3, 4, 4.1
    "sonnet_55": (2.0, 10.0, 0.10, 2.5),     # Sonnet 5.5 이상
    "sonnet_5": (2.0, 10.0, 0.20, 2.5),      # Sonnet 5
    "sonnet": (3.0, 15.0, 0.30, 3.75),       # Sonnet 3.x ~ 4.6
    "haiku_55": (0.10, 0.50, 0.01, 0.125),   # Haiku 5.5 이상(10만 토큰 이하 프롬프트 가격)
    "haiku_45": (1.0, 5.0, 0.10, 1.25),      # Haiku 4.5
    "haiku_35": (0.80, 4.0, 0.08, 1.0),
    "haiku_3": (0.25, 1.25, 0.03, 0.30),
}
# 계열별 (이 버전 이상이면, 가격 키). 위에서부터 처음 맞는 것.
_USAGE_TIERS = {
    "fable": [((5, 2), "fable_52"), ((0, 0), "fable_5")],
    "mythos": [((5, 2), "fable_52"), ((0, 0), "fable_5")],
    "opus": [((5, 5), "opus_55"), ((4, 5), "opus_new"), ((0, 0), "opus_old")],
    "sonnet": [((5, 5), "sonnet_55"), ((5, 0), "sonnet_5"), ((0, 0), "sonnet")],
    "haiku": [((5, 5), "haiku_55"), ((4, 5), "haiku_45"), ((3, 5), "haiku_35"), ((0, 0), "haiku_3")],
}
_USAGE_NEW_RE = re.compile(r"(fable|mythos|opus|sonnet|haiku)-(\d{1,2})(?:-(\d{1,2}))?(?!\d)")
_USAGE_OLD_RE = re.compile(r"claude-(\d)(?:-(\d))?-(opus|sonnet|haiku)")


def usage_price_key(model):
    m = (model or "").lower()
    v = _USAGE_NEW_RE.search(m)
    if v:
        fam, ver = v.group(1), (int(v.group(2)), int(v.group(3) or 0))
    else:
        o = _USAGE_OLD_RE.search(m)
        if not o:
            return None
        fam, ver = o.group(3), (int(o.group(1)), int(o.group(2) or 0))
    for least, key in _USAGE_TIERS[fam]:
        if ver >= least:
            return key
    return None


def usage_cost(model, inp, out, cread, cwrite):
    key = usage_price_key(model)
    if key is None:
        return 0.0
    pi, po, pr, pw = USAGE_PRICES[key]
    return (inp * pi + out * po + cread * pr + cwrite * pw) / 1e6


def _usage_num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return v


def _usage_iso(v):
    n = _usage_num(v)
    if n is not None and n > 0:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(n / 1000 if n >= 1e12 else n))
    if isinstance(v, str) and v.strip():
        ts = _usage_parse_ts(v.strip())
        if ts is not None:
            return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
    return None


def _usage_parse_ts(s):
    """ISO 8601 문자열 -> epoch 초. 못 읽으면 None."""
    import datetime
    try:
        t = s[:-1] + "+00:00" if s.endswith("Z") else s
        d = datetime.datetime.fromisoformat(t)
        if d.tzinfo is None:
            d = d.replace(tzinfo=datetime.timezone.utc)
        return d.timestamp()
    except (ValueError, TypeError):
        return None


# ---- 한도: Claude OAuth

def _usage_sign_in(source):
    """키체인 값 또는 .credentials.json 본문 -> {token, expires_at(ms), plan} 또는 None."""
    try:
        oauth = (json.loads(source) or {}).get("claudeAiOauth") or {}
    except (ValueError, TypeError, AttributeError):
        return None
    if not isinstance(oauth, dict):
        return None
    tok = oauth.get("accessToken")
    if not isinstance(tok, str) or not tok.strip():
        return None
    plan = oauth.get("subscriptionType")
    return {"token": tok.strip(), "expires_at": _usage_num(oauth.get("expiresAt")),
            "plan": plan.strip() if isinstance(plan, str) and plan.strip() else None}


def _usage_keychain_read(service, account=None):
    """('found', 값) | ('missing', None) | ('locked', None). launchd 로그인 세션 우회는 옮기지 않았다."""
    argv = ["security", "find-generic-password", "-s", service] + (["-a", account] if account else []) + ["-w"]
    try:
        r = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=USAGE_CMD_TIMEOUT)
    except subprocess.TimeoutExpired:
        return ("locked", None)  # 접근 허용 창이 떠서 멈춘 경우
    except (OSError, ValueError):
        return ("missing", None)
    if r.returncode == 0:
        return ("found", r.stdout.decode("utf-8", "replace").strip())
    if r.returncode == 44:  # errSecItemNotFound
        return ("missing", None)
    return ("locked", None)


def usage_find_credentials():
    """(sign_in 또는 None, 에러 코드 또는 None). 키체인과 파일 중 만료가 더 늦은 쪽."""
    home = os.path.expanduser("~")
    item, locked = None, False
    if sys.platform == "darwin":
        user = os.environ.get("USER")
        for acct in ([user, None] if user else [None]):
            st_, val = _usage_keychain_read("Claude Code-credentials", acct)
            if st_ == "locked":
                locked = True
                break
            if st_ == "found":
                item = _usage_sign_in(val)
                if item:
                    break
    file_si = None
    try:
        with open(os.path.join(home, ".claude", ".credentials.json"), "rb") as f:
            raw = f.read(USAGE_MAX_READ + 1)
        if len(raw) <= USAGE_MAX_READ:
            file_si = _usage_sign_in(raw.decode("utf-8", "replace"))
    except OSError:
        pass
    file_later = bool(file_si and item and file_si["expires_at"] is not None
                      and item["expires_at"] is not None and file_si["expires_at"] > item["expires_at"])
    if item and not file_later:
        return item, None
    if file_si:
        return file_si, None
    return None, ("keychain_locked" if locked else "no_credentials")


def _usage_window(value, kind, scope):
    if not isinstance(value, dict):
        return None
    used = _usage_num(value.get("utilization"))
    if used is None:
        return None
    return {"kind": kind, "scope": scope, "used_percent": round(min(100.0, max(0.0, float(used))), 1),
            "resets_at": _usage_iso(value.get("resets_at"))}


def usage_parse_oauth(body):
    wins = [_usage_window(body.get("five_hour"), "session", None),
            _usage_window(body.get("seven_day"), "week", None),
            _usage_window(body.get("seven_day_opus"), "week", "Opus"),
            _usage_window(body.get("seven_day_sonnet"), "week", "Sonnet")]
    return [w for w in wins if w]


def usage_fetch_oauth(token):
    """OAuth usage API 호출 -> body(dict). 실패는 ApiError 대신 (None, 에러 코드)."""
    import urllib.request
    import urllib.error
    req = urllib.request.Request(USAGE_URL, headers={
        "authorization": "Bearer " + token, "accept": "application/json",
        "anthropic-beta": "oauth-2025-04-20", "user-agent": USAGE_USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read(USAGE_MAX_READ + 1)
    except urllib.error.HTTPError as e:
        e.close()
        return None, "http_%d" % e.code
    except (urllib.error.URLError, OSError, ValueError):
        return None, "network"
    if len(raw) > USAGE_MAX_READ:
        return None, "network"
    try:
        body = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return None, "http_200"
    return (body, None) if isinstance(body, dict) else (None, "http_200")


_usage_limits_cache = {"at": 0.0, "next": 0.0, "value": None}
_usage_limits_lock = threading.Lock()


def usage_limits(refresh=False, now=None):
    now = time.time() if now is None else now
    with _usage_limits_lock:
        c = _usage_limits_cache
        if c["value"] is not None:
            if refresh and now - c["at"] >= USAGE_MIN_REFRESH:
                pass
            elif now < c["next"]:
                return c["value"]
        si, err = usage_find_credentials()
        result = {"plan": None, "windows": [], "error": None}
        ok = False
        if si is None:
            result["error"] = err
        else:
            result["plan"] = si["plan"]
            if si["expires_at"] is not None and si["expires_at"] <= now * 1000:
                result["error"] = "expired"
            else:
                body, e = usage_fetch_oauth(si["token"])
                if body is None:
                    result["error"] = e
                else:
                    result["windows"] = usage_parse_oauth(body)
                    ok = True
        c["at"], c["value"] = now, result
        c["next"] = now + (USAGE_FRESH if ok else USAGE_RETRY)
        return result


# ---- 토큰·비용: ~/.claude/projects/**/*.jsonl

_usage_file_cache = {}  # path -> ((mtime_ns, size), [레코드])
_usage_file_lock = threading.Lock()


def _usage_int(v):
    n = _usage_num(v)
    return int(n) if n is not None and n > 0 else 0


def usage_parse_file(path):
    """assistant 줄 -> [(dedupe키 또는 None, epoch, model, in, out, cache_read, cache_write)]"""
    recs = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"assistant"' not in line or '"usage"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(d, dict) or d.get("type") != "assistant":
                    continue
                msg = d.get("message")
                if not isinstance(msg, dict):
                    continue
                usage = msg.get("usage")
                model = msg.get("model")
                if not isinstance(usage, dict) or not isinstance(model, str) or model.startswith("<"):
                    continue
                ts = _usage_parse_ts(d["timestamp"]) if isinstance(d.get("timestamp"), str) else None
                if ts is None:
                    continue
                mid, rid = msg.get("id"), d.get("requestId")
                key = (mid, rid) if isinstance(mid, str) and isinstance(rid, str) else None
                recs.append((key, ts, model, _usage_int(usage.get("input_tokens")),
                             _usage_int(usage.get("output_tokens")),
                             _usage_int(usage.get("cache_read_input_tokens")),
                             _usage_int(usage.get("cache_creation_input_tokens"))))
    except OSError:
        return None
    return recs


def _usage_files(root):
    for dp, _dn, fns in os.walk(root):
        for fn in fns:
            if fn.endswith(".jsonl"):
                yield os.path.join(dp, fn)


def usage_totals(range_name, now=None):
    import datetime
    days = USAGE_RANGES[range_name]
    now = time.time() if now is None else now
    today = datetime.datetime.fromtimestamp(now).date()
    first = today - datetime.timedelta(days=days - 1)
    start_ts = datetime.datetime.combine(first, datetime.time.min).timestamp()
    root = os.path.join(os.path.expanduser("~"), ".claude", "projects")
    seen = set()
    by_model = {}
    by_day = {(first + datetime.timedelta(days=i)).isoformat(): [0.0, 0] for i in range(days)}
    live = set()
    for path in sorted(_usage_files(root)):
        try:
            stt = os.stat(path)
        except OSError:
            continue
        live.add(path)
        if stt.st_mtime < start_ts:
            continue
        sig = (stt.st_mtime_ns, stt.st_size)
        with _usage_file_lock:
            hit = _usage_file_cache.get(path)
        if hit and hit[0] == sig:
            recs = hit[1]
        else:
            recs = usage_parse_file(path)
            if recs is None:
                continue
            with _usage_file_lock:
                _usage_file_cache[path] = (sig, recs)
        for key, ts, model, i, o, cr, cw in recs:
            if ts < start_ts:
                continue
            date = datetime.datetime.fromtimestamp(ts).date().isoformat()
            if date not in by_day:  # 미래 시각 등
                continue
            if key is not None:
                if key in seen:
                    continue
                seen.add(key)
            cost = usage_cost(model, i, o, cr, cw)
            m = by_model.setdefault(model, [0, 0, 0, 0, 0.0])
            m[0] += i; m[1] += o; m[2] += cr; m[3] += cw; m[4] += cost
            by_day[date][0] += cost
            by_day[date][1] += i + o + cr + cw
    with _usage_file_lock:  # 지워진 파일 캐시 정리
        for p in [p for p in _usage_file_cache if p not in live]:
            del _usage_file_cache[p]
    models = [{"model": k, "input": v[0], "output": v[1], "cache_read": v[2], "cache_write": v[3],
               "cost_usd": round(v[4], 6)} for k, v in by_model.items()]
    models.sort(key=lambda r: (-r["cost_usd"], r["model"]))
    totals = {"input": sum(r["input"] for r in models), "output": sum(r["output"] for r in models),
              "cache_read": sum(r["cache_read"] for r in models), "cache_write": sum(r["cache_write"] for r in models),
              "cost_usd": round(sum(r["cost_usd"] for r in models), 6)}
    days_out = [{"date": d, "cost_usd": round(v[0], 6), "tokens": v[1]} for d, v in sorted(by_day.items())]
    return totals, models, days_out


def api_usage(query):
    """GET /api/usage?range=today|7d|30d&refresh=1"""
    rng = (query.get("range") or ["7d"])[0]
    if rng not in USAGE_RANGES:
        raise ApiError(400, "bad_range")
    refresh = (query.get("refresh") or [""])[0] == "1"
    totals, models, days = usage_totals(rng)
    return {"limits": usage_limits(refresh), "range": rng, "totals": totals,
            "by_model": models, "by_day": days}


# ---------------------------------------------------------------- HTTP

ROUTES = [
    ("GET", re.compile(r"^/api/health$"), "health"),
    ("GET", re.compile(r"^/api/terminals$"), "terminals"),
    ("GET", re.compile(r"^/api/terminals/([^/]+)/screen$"), "screen"),
    ("POST", re.compile(r"^/api/terminals/([^/]+)/answer$"), "answer"),
    ("GET", re.compile(r"^/api/terminals/([^/]+)/prompt$"), "prompt"),
    ("POST", re.compile(r"^/api/terminals/([^/]+)/prompt/answer$"), "prompt_answer"),
    ("GET", re.compile(r"^/api/terminals/([^/]+)/transcript$"), "transcript"),
    ("GET", re.compile(r"^/api/folders$"), "folders"),
    ("GET", re.compile(r"^/api/agents/catalog$"), "agent_catalog"),
    ("POST", re.compile(r"^/api/agents$"), "agents"),
    ("POST", re.compile(r"^/api/terminals/([^/]+)/keys$"), "keys"),
    ("POST", re.compile(r"^/api/terminals/([^/]+)/send$"), "send"),
    ("POST", re.compile(r"^/api/devices$"), "add_device"),
    ("DELETE", re.compile(r"^/api/devices$"), "delete_device"),
    ("GET", re.compile(r"^/api/usage$"), "usage"),
    ("GET", re.compile(r"^/api/push/status$"), "push_status"),
    ("POST", re.compile(r"^/api/push/test$"), "push_test"),
    ("POST", re.compile(r"^/hook/(claude|codex)$"), "hook"),
]


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "herdr-remote"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 요청 줄만(헤더·본문 없음)
        log.info("%s %s", self.client_address[0], fmt % args)

    def _reply(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self):
        expected = self.server.app.cfg.get("token") or ""
        h = self.headers.get("Authorization") or ""
        if not expected or not h.startswith("Bearer "):
            return False
        return hmac.compare_digest(h[7:].strip().encode("utf-8"), expected.encode("utf-8"))

    def _reply_empty(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _client_alive(self):
        """붙잡은 훅 요청의 클라이언트가 아직 붙어 있나. 훅 프로세스가 죽으면(PC에서 먼저 답함) 소켓이 EOF로 읽힌다."""
        sock = self.connection
        try:
            r, _, _ = select.select([sock], [], [], 0)
            if not r:
                return True
            return bool(sock.recv(1, socket.MSG_PEEK))
        except (OSError, ValueError):
            return False

    def _body(self, limit=MAX_BODY):
        cl = self.headers.get("Content-Length")
        if self.headers.get("Transfer-Encoding"):
            raise ApiError(400, "chunked_not_supported")
        if not cl:
            return {}
        try:
            n = int(cl)
        except ValueError:
            raise ApiError(400, "bad_length")
        if n < 0:
            raise ApiError(400, "bad_length")
        if n > limit:
            raise ApiError(413, "too_large")
        raw = self.rfile.read(n)
        if not raw:
            return {}
        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "bad_json")
        if not isinstance(body, dict):
            raise ApiError(400, "bad_json")
        return body

    def _handle(self, method):
        app = self.server.app
        try:
            if not self._authorized():
                self.close_connection = True
                return self._reply(401, {"error": "unauthorized"})
            parsed = urllib.parse.urlsplit(self.path)
            path = parsed.path
            query = urllib.parse.parse_qs(parsed.query)
            for m, rx, name in ROUTES:
                mt = rx.match(path)
                if not mt:
                    continue
                if m != method:
                    continue
                if name == "hook":
                    # 훅은 이 PC의 에이전트만 보낸다: tailscale serve(폰 쪽 프록시)를 거쳐 온 요청은 거절
                    if any(self.headers.get(h) is not None for h in HOOK_PROXY_HEADERS):
                        self.close_connection = True
                        return self._reply(403, {"error": "forbidden"})
                    res = app.hook_event(mt.group(1), self._body(HOOK_MAX_BODY), self._client_alive)
                    if res is HOOK_NO_REPLY:
                        self.close_connection = True
                        return None
                    if res is None:
                        return self._reply_empty()
                    return self._reply(200, res)
                body = self._body() if method in ("POST", "DELETE") else {}
                tid = urllib.parse.unquote(mt.group(1)) if mt.groups() else None
                if name == "health":
                    res = {"ok": True, "name": machine_name()}
                    if app.cfg.get("push_relay_url"):
                        res["push_relay_url"] = app.cfg["push_relay_url"]
                    if app.cfg.get("fcm_relay_url"):
                        # Android 앱은 이 주소로 등록한다(APNs 직접 모드여도 fcm은 중계를 쓴다).
                        res["fcm_relay_url"] = app.cfg["fcm_relay_url"]
                    if app.cfg.get("push_relay_url") or app.cfg.get("fcm_relay_url"):
                        res["relay_resign"] = app.devices.relay_resign()
                    res["push"] = app.push_health((query.get("device") or [None])[0])
                elif name == "terminals":
                    res = app.api_terminals()
                elif name == "screen":
                    res = app.api_screen(tid, (query.get("lines") or [None])[0])
                elif name == "answer":
                    res = app.api_answer(tid, body)
                elif name == "prompt":
                    res = app.api_prompt(tid)
                elif name == "prompt_answer":
                    res = app.api_prompt_answer(tid, body)
                elif name == "transcript":
                    res = app.api_transcript(tid, (query.get("limit") or [None])[0])
                elif name == "folders":
                    res = app.api_folders((query.get("path") or [""])[0])
                elif name == "agent_catalog":
                    res = app.api_agent_catalog()
                elif name == "agents":
                    res = app.api_agents(body)
                elif name == "keys":
                    res = app.api_keys(tid, body)
                elif name == "send":
                    res = app.api_send(tid, body)
                elif name == "add_device":
                    res = app.api_add_device(body)
                elif name == "usage":
                    res = api_usage(query)
                elif name == "push_status":
                    res = app.api_push_status((query.get("device") or [None])[0])
                elif name == "push_test":
                    res = app.api_push_test(body)
                else:
                    res = app.api_delete_device(body)
                return self._reply(200, res)
            # 경로 안에 슬래시가 더 있는 id(원격 machine/t7 등)는 여기서 걸린다.
            if path.startswith("/api/terminals/"):
                return self._reply(400, {"error": "bad_id"})
            return self._reply(404, {"error": "no_route"})
        except ApiError as e:
            if e.code == 413:
                self.close_connection = True
            return self._reply(e.code, e.body)
        except Exception:  # noqa: BLE001
            log.exception("요청 처리 오류")
            return self._reply(500, {"error": "internal"})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_DELETE(self):
        self._handle("DELETE")


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    # Windows의 SO_REUSEADDR는 다른 프로세스가 같은 포트를 같이 잡게 한다(포트 가로채기).
    allow_reuse_address = not IS_WINDOWS


def check_bind(bind):
    try:
        ip = ipaddress.ip_address(bind)
    except ValueError:
        raise ValueError("bind는 IP 주소여야 한다: %r" % bind)
    if ip == ipaddress.ip_address("127.0.0.1") or (ip.version == 4 and ip in TAILSCALE_NET):
        return ip
    raise ValueError("bind %s 거부: Tailscale(100.64.0.0/10) 또는 127.0.0.1만 허용" % bind)


def make_server(app, bind, port):
    check_bind(bind)
    srv = Server((bind, port), Handler)
    srv.app = app
    return srv


def make_server_retry(app, bind, port, sleep=time.sleep, attempts=None):
    """부팅 직후 Tailscale IP가 아직 없으면 bind가 실패하므로 10초마다 다시 시도한다."""
    check_bind(bind)
    n = 0
    while True:
        try:
            return make_server(app, bind, port)
        except OSError as e:
            n += 1
            if attempts is not None and n >= attempts:
                raise
            log.warning("bind %s:%d 실패(%s), %d초 뒤 재시도", bind, port, e.strerror or e, BIND_RETRY)
            sleep(BIND_RETRY)


# ---------------------------------------------------------------- 진입점

def build_app(cfg):
    devices = DeviceStore(os.path.join(cfg["_dir"], "devices.json"))
    push_log = PushLog(os.path.join(cfg["_dir"], PUSH_STATUS_FILE))
    if cfg.get("push_relay_url"):
        pusher = RelayPusher(cfg, devices, push_log=push_log)
    else:
        pusher = ApnsPusher(cfg, devices, push_log=push_log)
    return App(cfg, Hc(cfg["hc_path"]), devices, pusher, push_log=push_log)


def cmd_serve():
    try:
        cfg = load_config()
    except ValueError as e:
        log.error("설정 오류: %s", e)
        print("설정 오류: %s" % e, file=sys.stderr)
        return 2
    if not cfg.get("token") or len(cfg["token"]) < 20:
        log.error("토큰이 없거나 짧다. init을 먼저 돌려라")
        print("토큰이 없거나 짧다. init을 먼저 돌려라", file=sys.stderr)
        return 2
    try:
        check_bind(cfg["bind"])
    except ValueError as e:
        log.error("%s", e)
        print(e, file=sys.stderr)
        return 2
    app = build_app(cfg)
    app.pusher.available()
    srv = make_server_retry(app, cfg["bind"], cfg["port"])
    log.info("시작 %s:%d (푸시: %s)", cfg["bind"], cfg["port"], "중계" if cfg.get("push_relay_url") else "직접")
    app.poll(initial=True)
    threading.Thread(target=app.events_loop, name="events", daemon=True).start()
    threading.Thread(target=app.poll_loop, name="poll", daemon=True).start()
    threading.Thread(target=app.screen_poll_loop, name="screen-poll", daemon=True).start()
    try:
        srv.serve_forever()
    finally:
        app.stop.set()
    return 0


def cmd_test_push():
    try:
        cfg = load_config()
    except ValueError as e:
        print("설정 오류: %s" % e, file=sys.stderr)
        return 2
    app = build_app(cfg)
    if not app.pusher.available():
        if cfg.get("push_relay_url"):
            print("푸시 중계를 쓸 수 없다: %s" % cfg["push_relay_url"], file=sys.stderr)
        else:
            print("APNs 키 파일이 없다: %s (또는 설정에 push_relay_url)" % cfg["apns_key_path"], file=sys.stderr)
        return 1
    devs = app.devices.list()
    payload = build_payload("test", "herdr-remote", "done", {})
    payload["aps"]["alert"] = test_alert()
    sent = app.pusher.push(payload, ttl=PUSH_TTL_TEST)
    print("시험 푸시: 기기 %d대 중 %d대 성공" % (len(devs), sent))
    return 0 if sent == len(devs) and devs else 1


def enable_windows_vt():
    """Windows 콘솔의 ANSI 처리(ENABLE_VIRTUAL_TERMINAL_PROCESSING 0x4)를 켠다. 켜졌으면 True."""
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetStdHandle.restype = wintypes.HANDLE
        h = k32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = wintypes.DWORD()
        if not h or not k32.GetConsoleMode(h, ctypes.byref(mode)):  # 콘솔이 아니면(리다이렉트) 실패
            return False
        return bool(k32.SetConsoleMode(h, mode.value | 0x4))
    except (OSError, ValueError, AttributeError, ImportError):
        return False


def _ensure_utf8(stream):
    """리다이렉트·cp949 파이프에서 █▀▄가 UnicodeEncodeError로 죽지 않게."""
    enc = (getattr(stream, "encoding", None) or "").lower().replace("_", "-")
    if enc not in ("utf-8", "utf8") and hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            pass


def cmd_pair(out=None, cfg=None, runner=None, enable_vt=None):
    out = out or sys.stdout
    ansi = True
    if IS_WINDOWS:
        _ensure_utf8(out)
        ansi = (enable_vt or enable_windows_vt)()
    cfg = cfg or load_config()
    tok = cfg.get("token") or ""
    if len(tok) < 20:
        print("토큰이 없거나 짧다. init을 먼저 돌려라", file=sys.stderr)
        return 2
    url = server_public_url(cfg, runner=runner)
    if not url:
        print("서버 주소를 못 정했다. 설정에 public_url을 넣거나 Tailscale을 켜라", file=sys.stderr)
        return 1
    link = pair_url(url, tok)
    print(qr_halfblocks(qr_encode(link), ansi=ansi), file=out)
    print("", file=out)
    print("아이폰 카메라로 QR을 찍거나 이 주소를 열어라(토큰이 들어 있다):", file=out)
    print(link, file=out)
    return 0


def cmd_hook(args, stdin=None, stdout=None, env=None, timeout=HOOK_CLIENT_TIMEOUT):
    """`herdr_remote.py hook <claude|codex>`: 에이전트 훅이 부른다. stdin의 훅 JSON을 로컬 서버에 넘기고
    서버 응답(JSON)을 그대로 stdout에 쓴다. herdr 안(HC_TERMINAL_ID)이거나 서버가 꺼져 있거나 어떤 오류든
    아무것도 쓰지 않고 0으로 끝난다 → 에이전트가 평소 터미널 대화상자를 띄운다. 로그 파일은 열지 않는다."""
    env = os.environ if env is None else env
    try:
        if env.get("HC_TERMINAL_ID"):
            return 0
        agent = args[0] if args else None
        if agent not in HOOK_AGENTS:
            return 0
        raw = (stdin if stdin is not None else sys.stdin.buffer).read(HOOK_MAX_BODY + 1)
        if not raw or len(raw) > HOOK_MAX_BODY:
            return 0
        cfg = load_config()
        tok = cfg.get("token") or ""
        if not tok:
            return 0
        conn = http.client.HTTPConnection(cfg.get("bind") or "127.0.0.1", cfg["port"], timeout=timeout)
        try:
            conn.request("POST", "/hook/" + agent, body=raw,
                         headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
            resp = conn.getresponse()
            data = resp.read(HOOK_MAX_OUT)
        finally:
            conn.close()
        if resp.status != 200 or not data.strip():
            return 0
        if not isinstance(json.loads(data.decode("utf-8")), dict):
            return 0
        out = stdout if stdout is not None else sys.stdout.buffer
        out.write(data)
        out.flush()
    except Exception:  # noqa: BLE001 - 어떤 실패든 에이전트의 평소 흐름으로
        pass
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "serve"
    if cmd == "hook":  # 에이전트가 부른다: 로그 설정(파일 열기) 없이 빠르게
        return cmd_hook(argv[1:])
    setup_logging()
    if cmd == "init":
        init_config()
        return 0
    if cmd == "serve":
        return cmd_serve()
    if cmd == "test-push":
        return cmd_test_push()
    if cmd == "pair":
        return cmd_pair()
    print("사용법: herdr_remote.py [init|serve|test-push|pair|hook <claude|codex>]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
