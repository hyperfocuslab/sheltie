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
import mimetypes
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
HC_STYLED_WAIT = 0.4  # hc attach --observe 첫 그림을 받는 시간(초). 실측 hc 0.1.21: 붙고 6ms 안에 다 온다
MAX_BODY = 64 * 1024
# 이 프로세스가 뜬 시각(/api/health의 started). 설치기가 재설치 뒤 응답한 서버가 새 서버인지 대조한다.
PROCESS_STARTED = time.time()
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
LIVE_ACTIVITY_FILE = "live_activity.json"
LIVE_ACTIVITY_MIN_GAP = 5.0  # 같은 기기 Live Activity 푸시 최소 간격(초). 그 안의 변화는 한 번으로 합친다
LIVE_ACTIVITY_START_WAIT = 60.0  # 예전 재시도 간격(초). 지금은 막힘 한 번에 start 한 번이라 쓰지 않는다
LIVE_ACTIVITY_ATTRIBUTES_TYPE = "SheltieActivityAttributes"  # 앱 확장의 ActivityAttributes 타입 이름
LIVE_ACTIVITY_TTL = 3600  # 갱신 푸시 유효기간(초)
LIVE_ACTIVITY_END_LINGER = 0  # 끝(end) 뒤 잠금화면에 남기는 시간(초). 0이면 바로 내린다
LIVE_ACTIVITY_STALE = 900  # start·update에 넣는 stale-date(초 뒤). end를 못 보내도 낡은 것으로 표시된다
LIVE_ACTIVITY_REFRESH = 600  # 막힘이 그대로여도 이만큼 지나면 update로 stale-date를 늦춘다(초)

ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
DEVICE_TOKEN_RE = re.compile(r"^[0-9a-fA-F]{32,200}$")
# FCM 등록 토큰: 대소문자를 가리므로 바꾸지 않고 저장한다. 중계(relay.ts FCM_TOKEN_RE)와 같은 범위.
FCM_TOKEN_RE = re.compile(r"^[A-Za-z0-9_:\-]{20,1024}$")
APNS_ENVS = ("sandbox", "production")
FCM_ENV = "fcm"
FCM_RELAY_OFF_REASON = "fcm_relay_off"
RELAY_SIG_RE = re.compile(r"^v1\.[A-Za-z0-9_-]{43}$")
DEVICE_ID_RE = re.compile(r"^[0-9a-f]{16}$")
ALLOWED_KEYS = frozenset(["enter", "esc", "up", "down", "left", "right", "tab", "space",
                          "shift-tab", "ctrl-c", "ctrl-d"] + [str(i) for i in range(1, 10)])
# API 키 이름 → hc 키 이름(hc는 `ctrl+c`처럼 + 로 잇는다). 여기 없는 이름은 그대로 넘긴다.
HC_KEY_NAMES = {"shift-tab": "shift+tab", "ctrl-c": "ctrl+c", "ctrl-d": "ctrl+d"}
SCREEN_WAIT_MAX = 20          # screen long-poll 최대 대기(초)
SCREEN_WAIT_INTERVAL = 0.3    # 서버 안에서 화면이 바뀌었는지 보는 간격
SCREEN_WAIT_SLOTS = 8         # 동시에 기다릴 수 있는 long-poll 수(넘으면 기다리지 않고 바로 돌려준다)
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

    def read_styled(self, tid):
        """색(SGR)이 남은 지금 화면: `hc attach --observe`의 첫 그림(실측 hc 0.1.21: 붙자마자 화면 전체를 줄마다
        \\r\\n으로 다시 그린다). 보기만 하고 입력·크기 변경은 안 한다. 그 뒤 흐름은 HC_STYLED_WAIT에서 끊는다."""
        argv = [self.path, "attach", "--observe", tid]
        try:
            out = self.runner(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=HC_STYLED_WAIT,
                              shell=False, **subprocess_kwargs()).stdout
        except subprocess.TimeoutExpired as e:
            out = e.stdout if e.stdout is not None else e.output  # 그림을 받은 뒤 끊었다(정상)
        except OSError as e:
            raise HcError("hc not runnable: %s" % e)
        if not out:
            raise HcError("hc attach: no paint")
        return out.decode("utf-8", "replace") if isinstance(out, bytes) else out

    def keys(self, tid, keys):
        self._run(["keys", tid] + [HC_KEY_NAMES.get(k, k) for k in keys])

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
# ask_user 여러 질문은 상자 안 탭 막대(`← □ Features │ ✓ DB │ ≡ Review →`, ✓ = 답한 질문. 지금 탭은 색으로만 보인다)가 있고,
# 목록은 늘 3항목(6줄) 창으로 스크롤된다(▲/▼, 질문 하나여도 4항목 이상이면). 그래서 항목 전체는 화면이 아니라 Gemini
# 기록(jsonl)에 먼저 쓰이는 ask_user 호출 인자(status "scheduled")에서 읽고, 보이는 질문 문장·탭 이름·번호 줄이 그것과
# 맞을 때만 카드로 만든다(지금 질문 = 문장이 같은 질문). 키(실측 0.70.0): 숫자 = 그 항목으로 커서 + 고르기(단일 선택은
# 고르고 다음 탭, 마지막이면 제출. 복수 선택은 토글). 복수 선택 항목은 옵션들, "All of the above"(옵션 2개 이상),
# 직접 입력, "Done"(번호 표시 없지만 숫자 키는 된다: 다음 탭/제출). 안 보이는 항목의 체크는 모르니 All → 1 → All → All
# (어느 상태에서든 전부 해제)로 비운 뒤 고를 것만 토글하고 Done. 직접 입력은 그 번호 → 붙여넣기+enter(다음 탭). 글 질문은
# 붙여넣기+enter. Review 탭은 enter 제출(안 답한 질문은 빠진 채로), esc는 질문 전체 취소(턴이 멈춘다).
# 커서가 직접 입력 줄에 있으면(숫자가 글자로 들어간다) 카드 없음. 안 보이는 직접 입력 줄에 PC에서 글을 넣었으면 그 글도
# 같이 간다(화면으로 못 본다).
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
GEMINI_ASK_KEY = "gemini:ask_user"  # parse의 reader에 이 키를 주면 답을 기다리는 ask_user 질문들(list)을 준다
GEMINI_TABS_HINT = "Enter to select · ←/→ to switch questions · Esc to cancel"
GEMINI_TABS_TEXT_HINT = "Enter to submit · Tab/Shift+Tab to switch questions · Esc to cancel"
GEMINI_REVIEW_HINT = "Enter to submit · Tab/Shift+Tab to edit answers · Esc to cancel"
GEMINI_REVIEW_HEAD = "Review your answers:"
GEMINI_REVIEW_WARN_RE = re.compile(r"^⚠ You have \d+ unanswered questions?$")
GEMINI_MULTI_NOTE = "(Select all that apply)"
GEMINI_ALL = "All of the above"
GEMINI_DONE = "Done"
GEMINI_ASK_ROW_RE = re.compile(r"^(●)?\s*(\d+)\.\s+(?:\[([ x])\]\s+)?(\S.*)$")
GEMINI_ASK_DONE_RE = re.compile(r"^(●)?\s+Done$")
GEMINI_SUBMIT_LABELS = ("Submit", "Cancel")


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


def _parse_gemini_question(inner, plan_reader=None):
    idx = _gemini_meaningful(inner)
    if len(idx) < 3 or inner[idx[0]].strip() != GEMINI_Q_HEAD:
        return None
    if any(inner[i].strip() in ("▲", "▼") or inner[i].strip().startswith("←") for i in idx):
        # 여러 질문(탭)·스크롤되는 목록: 화면엔 일부만 보여 기록의 ask_user 인자와 맞춰 본다(없으면 카드 없음)
        return _parse_gemini_ask(inner, idx, plan_reader(GEMINI_ASK_KEY) if plan_reader else None)
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


def _gemini_tab_bar(s):
    """`← □ Features │ ✓ DB │ ≡ Review →` → [(답했는가, 이름)](Review 뺌) 또는 None."""
    if not (s.startswith("← ") and s.endswith(" →")):
        return None
    items = s[2:-2].split(" │ ")
    if len(items) < 3 or items[-1] != "≡ Review":
        return None
    out = []
    for it in items[:-1]:
        if len(it) < 3 or it[0] not in "□✓" or it[1] != " ":
            return None
        out.append((it[0] == "✓", it[2:]))
    return out


def _gemini_ask_questions(qs):
    """기록의 ask_user questions → [(type, 질문, 머리말, [(라벨, 설명)], 복수 선택, placeholder)] 또는 None(모르는 모양)."""
    if not isinstance(qs, list) or not qs:
        return None
    out = []
    for q in qs:
        if not isinstance(q, dict) or not isinstance(q.get("question"), str) or not isinstance(q.get("header"), str):
            return None
        t = q.get("type")
        ph = q.get("placeholder") if isinstance(q.get("placeholder"), str) and q.get("placeholder") else None
        if t == "yesno":
            opts = [("Yes", None), ("No", None)]
        elif t == "choice":
            raw = q.get("options")
            if not isinstance(raw, list) or not raw or any(
                    not isinstance(o, dict) or not isinstance(o.get("label"), str) or not o["label"].strip() for o in raw):
                return None
            opts = [(_norm(o["label"]), _norm(o["description"]) if isinstance(o.get("description"), str)
                     and o["description"].strip() else None) for o in raw]
        elif t == "text":
            opts = []
        else:
            return None
        out.append((t, _norm(q["question"]), q["header"], opts, t == "choice" and q.get("multiSelect") is True, ph))
    return out


def _parse_gemini_review(body, tabs):
    if not body or body[0] != GEMINI_REVIEW_HEAD:
        return None
    rest = body[1:]
    if rest and GEMINI_REVIEW_WARN_RE.match(rest[0]):
        rest = rest[1:]
    entries = []
    for ln in rest:
        k = len(entries)
        if k < len(tabs) and ln.startswith(tabs[k]["label"] + " → "):
            entries.append(ln)
        elif entries:
            entries[-1] += " " + ln  # 긴 답이 줄바꿈됐다
        else:
            return None
    if len(entries) != len(tabs):
        return None
    return _finish("submit", None, GEMINI_REVIEW_HEAD[:-1], "\n".join(entries),
                   [(lb, None, None) for lb in GEMINI_SUBMIT_LABELS], reject_index=1, tabs=tabs)


def _parse_gemini_ask(inner, idx, qs):
    """탭·스크롤 목록이 있는 ask_user 상자. qs: 기록의 ask_user questions(답을 기다리는 호출)."""
    qs = _gemini_ask_questions(qs)
    if not qs or len(idx) < 4:
        return None
    bar = _gemini_tab_bar(inner[idx[1]].strip())
    if bar is not None:
        if [h for _, h in bar] != [q[2] for q in qs]:
            return None
        tabs = [{"label": h, "answered": a} for a, h in bar]
        pos = 2
    elif len(qs) == 1 and not inner[idx[1]].strip().startswith("←"):
        tabs, pos = None, 1
    else:
        return None
    hint = inner[idx[-1]].strip()
    body = [inner[i] for i in idx[pos:-1]]
    if hint == GEMINI_REVIEW_HINT:
        p = _parse_gemini_review([b.strip() for b in body], tabs) if tabs else None
        if p:
            p["agent"] = "gemini"
        return p
    stop = next((k for k, b in enumerate(body) if b.strip() in ("▲", "▼", GEMINI_MULTI_NOTE)
                 or b.strip().startswith("> ") or GEMINI_ASK_ROW_RE.match(b.strip())
                 or GEMINI_ASK_DONE_RE.match(b)), len(body))
    text = _norm(" ".join(b.strip() for b in body[:stop]))
    hit = [k for k, q in enumerate(qs) if q[1] == text]
    if not text or len(hit) != 1:
        return None
    t, question, header, src, multi, ph = qs[hit[0]]
    title = header if tabs else None
    rest = body[stop:]
    if t == "text":
        want_hint = GEMINI_TABS_TEXT_HINT if tabs else "Enter to submit · Esc to cancel"
        label = ph or GEMINI_TEXT_PLACEHOLDER[2:]
        if hint != want_hint or [b.strip() for b in rest] != ["> " + label]:
            return None  # PC에서 글을 넣었다(placeholder 대신 글이 보인다)
        p = _finish("question", title, question, None, [(label, None, None)], free_text_index=0, tabs=tabs)
        p["agent"] = "gemini"
        return p
    if hint != (GEMINI_TABS_HINT if tabs else "Enter to select · ↑/↓ to navigate · Esc to cancel"):
        return None  # 직접 입력 줄에 커서(힌트가 Enter to submit으로 바뀐다)
    n = len(src)
    other = ph or GEMINI_FREE_LABELS[0]
    items = [lb for lb, _ in src] + ([GEMINI_ALL] if multi and n > 1 else []) + [other]
    total = len(items) + (1 if multi else 0)
    if total > 9:
        return None  # 두 자리 번호는 숫자 키가 1초 기다린다. ask_user가 options를 4개로 막아(0.70.0 검증) 실제로는 최대 7
    if (GEMINI_MULTI_NOTE in [b.strip() for b in rest]) != multi or (rest and rest[0].strip() == GEMINI_MULTI_NOTE) != multi:
        return None
    cursor, checked, seen, prev_row = [], [], 0, False
    for b in rest[1:] if multi else rest:
        s = b.strip()
        if s in ("▲", "▼"):
            prev_row = False
            continue
        m = GEMINI_ASK_ROW_RE.match(s)
        d = GEMINI_ASK_DONE_RE.match(b) if not m else None
        if m:
            k = int(m.group(2))
            if not 1 <= k <= len(items) or m.group(4) != items[k - 1] or (m.group(3) is not None) != multi:
                return None  # 기록과 다른 항목, PC에서 직접 입력 줄에 글을 넣었다
            if m.group(1):
                cursor.append(k)
            if multi and m.group(3) == "x" and k <= n:
                checked.append(k - 1)
            seen += 1
            prev_row = True
        elif d and multi:
            if d.group(1):
                cursor.append(total)
            prev_row = False
        elif prev_row and b.startswith(" "):
            continue  # 항목 설명 줄
        else:
            return None
    if not seen or len(cursor) != 1 or cursor[0] == len(items):
        return None  # 커서가 직접 입력 줄이면 숫자가 글자로 들어간다
    sel = cursor[0] - 1 if cursor[0] <= n else None
    opts = [(lb, desc, k + 1) for k, (lb, desc) in enumerate(src)]
    free = None
    if not multi:
        opts.append((other, None, n + 1))
        free = n
    p = _finish("question", title, question, None, opts, selected=sel, multi=multi, checked=checked,
                free_text_index=free, tabs=tabs)
    p["agent"] = "gemini"
    return p


def _pick_gemini(p, option_index):
    """Review 탭 확인 카드: Submit = enter, Cancel = esc(질문 전체 취소, 실측). 그 밖에는 기본(숫자 키)."""
    if p["kind"] != "submit":
        return None
    return [("keys", ["enter" if option_index == 0 else "esc"])]


def _multi_gemini(p, num, want):
    """복수 선택(실측 0.70.0): 숫자 = 토글. 안 보이는 항목의 체크를 모르니 옵션이 둘 이상이면 All → 1 → All(어느 상태든
    전부 체크) → All(전부 해제)로 비우고 고를 것만 숫자로 토글한 뒤 Done 번호(다음 탭, 마지막·질문 하나면 제출).
    옵션 하나면 All이 없고 항목이 다 보여 체크를 안다."""
    n = len(p["options"])
    if n > 1:
        keys = [str(n + 1), "1", str(n + 1), str(n + 1)] + [str(i + 1) for i in sorted(want)]
        done = n + 3
    else:
        keys = ["1"] if (0 in want) != (0 in p["checked"]) else []
        done = n + 2
    if done > 9:
        raise ApiError(400, "bad_option")
    return [("keys", keys + [str(done)])]


def _gemini_dialog_group(text):
    """여러 질문 상자(탭 막대)면 묶음 키(탭 이름들), 아니면 None."""
    inner = _last_box(screen_lines(text))
    idx = _gemini_meaningful(inner or [])
    if len(idx) < 2 or inner[idx[0]].strip() != GEMINI_Q_HEAD:
        return None
    bar = _gemini_tab_bar(inner[idx[1]].strip())
    return ("gemini-questions", tuple(h for _, h in bar)) if bar else None


GEMINI_ASK_TAIL = 2 * 1024 * 1024
GEMINI_TOOL_DONE = ("success", "error", "cancelled")
_gemini_ask_cache = {}  # 기록 경로 → ((크기, 시각), questions)


def read_gemini_ask(path):
    """Gemini 기록(jsonl)에서 답을 기다리는 마지막 ask_user 호출의 questions 또는 None. 실측 0.70.0: 호출은 상자가 뜰 때
    status "scheduled" 줄로 쓰이고, 답하면 같은 호출 id가 "success"(esc면 "cancelled") 줄로 다시 쓰인다."""
    try:
        st_ = os.stat(path, follow_symlinks=False)
    except OSError:
        return None
    key = (st_.st_size, st_.st_mtime_ns)
    hit = _gemini_ask_cache.get(path)
    if hit and hit[0] == key:
        return hit[1]
    try:
        data = read_regular_file(path, GEMINI_ASK_TAIL, tail=True)
    except OSError:
        return None
    calls = {}
    for ln in data.split(b"\n"):
        if b'"ask_user"' not in ln:
            continue
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        tcs = rec.get("toolCalls") if isinstance(rec, dict) else None
        for tc in tcs if isinstance(tcs, list) else []:
            if isinstance(tc, dict) and tc.get("name") == "ask_user" and isinstance(tc.get("id"), str):
                calls.pop(tc["id"], None)
                calls[tc["id"]] = (tc.get("status"), tc.get("args"))
    pending = [a for s, a in calls.values() if s not in GEMINI_TOOL_DONE]
    qs = pending[-1].get("questions") if pending and isinstance(pending[-1], dict) else None
    if len(_gemini_ask_cache) > 50:
        _gemini_ask_cache.clear()
    _gemini_ask_cache[path] = (key, qs)
    return qs


def gemini_pending_ask(info, home, runner=None):
    _, path = find_gemini_transcript(info, home, runner=runner)
    return read_gemini_ask(path) if path else None


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
    return _parse_gemini_approval(inner) or _parse_gemini_question(inner, plan_reader) or _parse_gemini_plan(inner)


# ---- GitHub Copilot CLI 대화 상자. 실측: copilot 1.0.114, 200열(tests/fixtures/copilot-*-1.0.114.txt).
# 승인·폴더 신뢰·계획·autopilot 권한은 화면 맨 끝 ╭…╰ 상자(제목, 가로줄, 본문, 번호 메뉴, 힌트)다. 숫자 키가 그 항목을
# 바로 고르고 제출한다. "No, and tell Copilot…"·"Suggest changes"는 입력 항목이라 숫자는 커서만 옮기고 글자를 치면
# 라벨이 입력값으로 바뀐다(빈 enter는 무시): 숫자 → 붙여넣기+enter. esc는 거절과 함께 턴 전체를 멈춘다: 앱의 거절은
# 3번 + 정해진 문구(COPILOT_DENY_TEXT)로 보내 Copilot이 다른 방법으로 이어 가게 한다.
# ask_user 질문은 가로줄 두 개 사이 대화 상자이고 번호가 없다(숫자 키 무시): ↑/↓ + enter. "Other"로 커서를 옮기면
# 상자 아래 입력 칸이 열린다.
# 여러 칸 폼은 메시지 아래 탭 막대(`  Language   Features   Project name   Public repo?`, 지금 탭·답한 탭 표시는 글자로
# 안 보인다)가 있고 칸 제목으로 지금 탭을 안다. 카드는 지금 칸 하나. 키(실측 1.0.114): enter가 그 칸을 확정하고 다음
# 탭(마지막 탭이면 폼 전체 제출). 단일 선택은 ↑/↓ + enter, 답한 항목은 커서가 없을 때 `✓ TypeScript`. 복수 선택
# (`❯ • lint`, 체크 `✓ ci`)은 ↑/↓ + space 토글 후 enter(안 골라도 넘어가고, 질문 하나면 빈 답으로 제출된다).
# 복수 선택의 "Other"는 커서가 오면 입력 칸이 열리고 space가 글자로 들어가 카드에서 뺀다. 글 입력 칸은 붙여넣기+enter
# (빈 enter도 다음 탭). PC에서 글을 넣었거나(`❯ demo`, `Other: Rust`) 복수 선택 커서가 Other에 있으면 카드 없음.
COPILOT_HINT_RE = re.compile(r"^↑/↓ to navigate · enter to select(?: · ctrl\+e to show full plan)? · esc to cancel$")
COPILOT_APPROVAL_Q_RE = re.compile(r"^Do you (?:want to .+|trust the files in this folder)\?$")
COPILOT_ESC_SUFFIX_RE = re.compile(r"\s*\(Esc(?: to stop)?\)$")
COPILOT_REJECT_FREE = "No, and tell Copilot what to do differently"
COPILOT_REJECT_LABEL = "No"  # 서버가 승인 카드 끝에 더하는 거절 버튼(화면에는 없다, 키는 3번 + COPILOT_DENY_TEXT)
COPILOT_DENY_TEXT = "Denied from phone. Don't do this; suggest another way."
COPILOT_TRUST_TITLE = "Confirm folder trust"
COPILOT_PLAN_TITLE = "Plan Ready for Review"
COPILOT_PLAN_FREE = "Suggest changes"
COPILOT_AUTOPILOT_TITLE = "Enable autopilot mode"
COPILOT_Q_HEAD = "Copilot needs information."
COPILOT_Q_HINT_RE = re.compile(r"^(↑/↓ select · )?(space toggle · )?enter accept · (tab next · )?(shift\+tab prev · )?"
                               r"ctrl\+d decline · esc cancel$")
COPILOT_OTHER = "Other (type your answer)"
COPILOT_OTHER_RE = re.compile(r"^Other \(type your answer(?: below)?\)$")
COPILOT_OTHER_INPUT = "┃ Type your answer..."
COPILOT_CHOICE_RE = re.compile(r"^(\s*?)(❯|✓| ) (\S.*)$")  # ✓: 여러 칸 폼에서 답한 단일 선택 항목(커서가 없을 때)
COPILOT_MULTI_ROW_RE = re.compile(r"^([•✓]) (\S.*)$")


def _copilot_box(lines):
    """화면 맨 끝 메뉴 상자 → (제목, 안쪽 줄, 메뉴 줄, 라벨) 또는 None."""
    inner = _last_box(lines)
    if not inner or len(inner) < 4 or not inner[0].strip() or not SOLID_RULE_RE.match(inner[1].strip()):
        return None
    idx = [i for i, ln in enumerate(inner) if ln.strip()]
    if not COPILOT_HINT_RE.match(inner[idx[-1]].strip()):
        return None
    rows = _menu_rows(inner, 2, idx[-1])
    if len(rows) < 2 or sum(1 for r in rows if r["selected"]) != 1:
        return None
    labels = [COPILOT_ESC_SUFFIX_RE.sub("", _norm(" ".join([r["label"]] + r["extra"]))) for r in rows]
    return inner[0].strip(), inner, rows, labels


def _parse_copilot_box(lines):
    box = _copilot_box(lines)
    if box is None:
        return None
    title, inner, rows, labels = box
    sel = next(k for k, r in enumerate(rows) if r["selected"])
    opts = [(lb, None, r["num"]) for lb, r in zip(labels, rows)]
    first = rows[0]["line"]
    if title == COPILOT_PLAN_TITLE:
        if len(rows) != 4 or labels[-1] != COPILOT_PLAN_FREE \
                or not all(lb.startswith(("Accept plan", "Exit")) for lb in labels[:3]):
            return None  # 4번에 PC에서 글자를 치는 중이면 라벨이 입력값으로 바뀐다
        p = _finish("plan", None, COPILOT_PLAN_TITLE, _dedent_block(inner[2:first]), opts, selected=sel,
                    free_text_index=3)
    elif title == COPILOT_AUTOPILOT_TITLE:
        if len(rows) != 4 or labels[-1] != "Cancel" or not all(lb.startswith("Use ") for lb in labels[:3]):
            return None
        p = _finish("question", None, COPILOT_AUTOPILOT_TITLE, _dedent_block(inner[2:first]), opts, selected=sel)
    else:
        # 질문 줄: 메뉴 바로 위 빈 줄 사이 덩어리(긴 경로는 접힐 수 있다)
        q_end = first - 1
        while q_end > 1 and not inner[q_end].strip():
            q_end -= 1
        q_start = q_end
        while q_start > 2 and inner[q_start - 1].strip() and q_end - q_start < 4:
            q_start -= 1
        question = _norm(" ".join(ln.strip() for ln in inner[q_start:q_end + 1]))
        if not COPILOT_APPROVAL_Q_RE.match(question) or labels[0] != "Yes":
            return None
        body = _gemini_body(inner[2:q_start])
        if title == COPILOT_TRUST_TITLE:
            if labels[-1] != "No" or len(rows) != 3:
                return None
            free, reject = None, len(rows) - 1
        elif labels[-1] == COPILOT_REJECT_FREE:
            # 거절 항목은 입력 항목이라 숫자만으로는 제출되지 않는다: 이 줄은 직접 입력(지시와 함께 거절)으로 두고,
            # 앱이 입력 항목을 버튼에서 빼므로 번호 없는 거절 버튼 "No"를 끝에 더한다. 키는 3번 + 정해진 문구
            # (_pick_copilot. 라벨의 "Esc to stop"은 턴 전체를 멈춘다)
            free, reject = len(rows) - 1, len(rows)
            opts.append((COPILOT_REJECT_LABEL, None, None))
        else:
            return None  # 3번에 PC에서 글자를 치는 중(라벨이 입력값으로 바뀐다) 등
        _, persist = allow_more_indices(labels)  # "Yes, and don't ask again …"은 permissions-config.json에 영구 저장
        p = _finish("approval", title, question, body, opts, selected=sel, free_text_index=free,
                    reject_index=reject, persist_index=persist)
    p["agent"] = "copilot"
    return p


def _copilot_question_dialog(lines):
    """화면 맨 끝 가로줄 두 개 사이 질문 상자의 안쪽 줄과 "Other" 입력 칸이 열려 있는지. 아니면 None."""
    nz = [i for i, ln in enumerate(lines) if ln.strip()]
    if not nz:
        return None
    k = len(nz) - 1
    other_open = False
    if lines[nz[k]].strip().startswith("╹"):
        # "Other" 입력 칸(╻▄▄ / ┃ Type your answer... / ╹▀▀). 이미 글자를 쳤으면 카드 없음
        if k < 3 or lines[nz[k - 1]].strip() != COPILOT_OTHER_INPUT or not lines[nz[k - 2]].strip().startswith("╻"):
            return None
        k -= 3
        other_open = True
    if k < 1 or not SOLID_RULE_RE.match(lines[nz[k]].strip()):
        return None
    bottom = nz[k]
    top = next((i for i in range(bottom - 1, max(-1, bottom - 120), -1) if SOLID_RULE_RE.match(lines[i].strip())), -1)
    if top < 0:
        return None
    return lines[top + 1:bottom], other_open


def _copilot_tab_bar(line):
    """여러 칸 폼의 탭 막대(`  Language   Features   Project name`, 두 칸 들여쓰기·칸 사이 공백 여럿) → 라벨 목록 또는 None."""
    if not line.startswith("  ") or line.startswith("   "):
        return None
    items = re.split(r"\s{2,}", line.strip())
    return items if len(items) >= 2 and len(set(items)) == len(items) else None


def _parse_copilot_question(lines):
    got = _copilot_question_dialog(lines)
    if got is None:
        return None
    inner, other_open = got
    idx = [i for i, ln in enumerate(inner) if ln.strip()]
    if len(idx) < 4 or inner[idx[0]].strip() != COPILOT_Q_HEAD:
        return None
    hint = COPILOT_Q_HINT_RE.match(inner[idx[-1]].strip())
    if not hint:
        return None
    choice, multi, tab_next, tab_prev = (bool(hint.group(k)) for k in (1, 2, 3, 4))
    msg = []
    j = idx[0] + 1
    while j < len(inner) and inner[j].strip():
        msg.append(inner[j].strip())
        j += 1
    # 여러 칸 폼: 메시지 아래 탭 막대(빈 줄 사이). 힌트의 tab next / shift+tab prev와 함께 있어야 한다
    tab_labels = None
    k = j
    while k < idx[-1] and not inner[k].strip():
        k += 1
    if k < idx[-1] and k + 1 < len(inner) and not inner[k + 1].strip():
        tab_labels = _copilot_tab_bar(inner[k])
        if tab_labels is not None:
            j = k + 1
    if (tab_labels is not None) != (tab_next or tab_prev):
        return None
    field = [ln for ln in inner[j:idx[-1]]]
    ms = [COPILOT_CHOICE_RE.match(ln) for ln in field]
    cur = [k for k, m in enumerate(ms) if m and m.group(2) == "❯"]
    text_in = [k for k, ln in enumerate(field) if ln.strip() == "❯"]
    checked = []
    if choice and len(cur) == 1 and not text_in:
        col = len(ms[cur[0]].group(1))
        a = b = cur[0]
        while a > 0 and ms[a - 1] and len(ms[a - 1].group(1)) == col:
            a -= 1
        while b + 1 < len(field) and ms[b + 1] and len(ms[b + 1].group(1)) == col:
            b += 1
        labels = [m.group(3).strip() for m in ms[a:b + 1]]
        sel = cur[0] - a
        if multi:
            # 복수 선택 `❯ • lint` / `  ✓ ci`, 끝 줄은 "Other". 커서가 Other에 오면 입력 칸이 열려 카드 없음
            rows = [COPILOT_MULTI_ROW_RE.match(lb) for lb in labels]
            if not all(rows) or any(m.group(2) == "✓" for m in ms[a:b + 1]) or other_open \
                    or rows[-1].group(1) != "•" or rows[-1].group(2) != COPILOT_OTHER or sel == len(rows) - 1:
                return None
            labels = [r.group(2) for r in rows[:-1]]
            checked = [k for k, r in enumerate(rows[:-1]) if r.group(1) == "✓"]
            free = None
        else:
            if any(lb.startswith(("•", "✓")) for lb in labels) or labels[-1].startswith("Other: "):
                return None  # 모르는 줄 모양, PC에서 Other에 답을 넣었다
            labels = [COPILOT_OTHER if COPILOT_OTHER_RE.match(lb) else lb for lb in labels]
            free = labels.index(COPILOT_OTHER) if COPILOT_OTHER in labels else None
            if other_open != (free is not None and sel == free):
                return None  # 입력 칸은 커서가 "Other"에 있을 때만 열린다
        if len(labels) < 2 - multi:
            return None
        head = [ln.strip() for ln in field[:a] if ln.strip()]
        t = next((k for k in range(len(head) - 1, -1, -1) if head[k].endswith(":")), None)
        if t is None:
            return None
        msg += head[:t]
        title, desc = head[t][:-1].strip(), _norm(" ".join(head[t + 1:])) or None
        opts = [(lb, None, None) for lb in labels]
    elif not choice and not multi and len(text_in) == 1 and not cur and not other_open:
        k = text_in[0]
        if k < 1 or not field[k - 1].strip().startswith("▄") or k + 1 >= len(field) \
                or not field[k + 1].strip().startswith("▀"):
            return None
        head = [ln.strip() for ln in field[:k - 1]]
        while head and not head[-1]:
            head.pop()
        para = []
        while head and head[-1]:
            para.insert(0, head.pop())
        msg += [h for h in head if h]
        if not para:
            return None
        title, desc = para[0], _norm(" ".join(para[1:])) or None
        opts, free, sel = [(title, None, None)], 0, None
    else:
        return None
    if tab_labels is not None:
        # 지금 탭은 칸 제목으로 안다. 첫 탭엔 shift+tab prev, 마지막 탭엔 tab next가 없다
        if tab_labels.count(title) != 1:
            return None
        n = tab_labels.index(title)
        if tab_prev != (n > 0) or tab_next != (n < len(tab_labels) - 1):
            return None
    question = _norm(" ".join(msg))
    if not question:
        return None
    # 지금 탭이 아닌 칸에 답했는지는 화면 글자로 안 보여 tabs는 채우지 않는다(제목이 지금 칸)
    p = _finish("question", title or None, question, desc, opts, selected=sel, multi=multi, checked=checked,
                free_text_index=free)
    p["agent"] = "copilot"
    return p


def _parse_copilot_lines(lines, plan_reader=None):
    return _parse_copilot_box(lines) or _parse_copilot_question(lines)


def _pick_copilot(p, option_index):
    """승인 카드에 서버가 더한 거절 버튼(번호 없음): 입력 항목 3번을 고르고 정해진 문구를 붙여넣기+enter(직접 입력과
    같은 키). esc는 거절과 함께 턴 전체를 멈춘다(실측 1.0.114). 그 밖에는 기본."""
    o = p["options"][option_index]
    if p["kind"] == "approval" and option_index == p.get("reject_index") and o.get("number") is None:
        return _free_text_digit(p, lambda i: p["options"][i]["number"], COPILOT_DENY_TEXT)
    return None


def _copilot_status_reject(p):
    """카드 없이(prompt_id 없이) 거절: 승인 카드에 서버가 더한 거절 버튼이 있으면 그 키 단계(3번 + 문구).
    폴더 신뢰·계획 등 다른 상자나 카드를 못 읽으면 None(예전대로 esc)."""
    if not p or p.get("agent") != "copilot" or p["kind"] != "approval":
        return None
    ri = p.get("reject_index")
    if ri is None or p["options"][ri].get("number") is not None:
        return None
    return _pick_copilot(p, ri)


def _multi_copilot(p, num, want):
    """복수 선택: 숫자 키를 무시한다. ↑/↓로 항목마다 가서 space 토글, enter 확정(다음 탭, 마지막 탭·질문 하나면 제출).
    안 골라도 enter가 넘어간다(실측 1.0.114). 커서가 Other에 가면 space가 글자로 들어가 Other는 건너뛴다(카드에 없다)."""
    cur = p.get("selected")
    if cur is None:
        raise ApiError(409, "no_cursor")
    keys = []
    for i in sorted(want.symmetric_difference(set(p["checked"]))):
        keys += nav_keys(cur, i) + ["space"]
        cur = i
    return [("keys", keys + ["enter"])]


def _copilot_overlay_card(p):
    """hc가 막힘으로 보지 않는 Copilot 상자(폴더 신뢰: idle, autopilot 권한: working). 실측 1.0.114."""
    return (p["kind"], p["title"] or p["question"]) in (("approval", COPILOT_TRUST_TITLE),
                                                        ("question", COPILOT_AUTOPILOT_TITLE))


# ---- Qwen Code 대화 상자. 실측: qwen-code 0.28.3, 200열(tests/fixtures/qwen-*-0.28.3.txt).
# 승인·계획·질문은 상자 없이 `? <도구 표시명> <요약> ←` 줄 아래에 본문, 질문 줄, 번호 메뉴(커서 승인 `›`, 질문 `❯`)가
# 그려지고 화면 끝은 `⠏ Waiting for user confirmation...`이다. 숫자 키가 그 항목을 바로 고르고 제출한다.
# "No, suggest changes (esc)"·"No, keep planning (esc)"는 esc와 같다(입력 칸 없이 턴이 끝난다).
# 질문 하나·단일 선택은 숫자 키가 고르고 제출. "Type something..."은 숫자 → 붙여넣기+enter.
# 여러 질문은 맨 위 탭 막대(`▸ Features   Deploy  Submit`, 답한 탭 뒤 ✓)가 있고 카드는 지금 탭 하나만 담는다.
# 단일 선택 탭은 숫자 키 = 고르고 다음 탭, 직접 입력도 숫자 → 붙여넣기+enter(실측). 복수 선택(`❯ [✓] 1. Auth`)은
# 숫자 키가 커서만 옮기고 space가 토글, enter가 확정(질문 하나면 제출, 여럿이면 다음 탭). 아무것도 안 고르면 enter가
# 무시된다. Submit 탭(`Ready to submit your answers?` 1 Submit answers / 2 Cancel)도 숫자는 커서만 옮겨 숫자+enter.
# Cancel은 도구를 취소하고 턴이 끝난다. 폴더 신뢰는 화면 끝 ╭…╰ 상자(Qwen이 permission "Approval needed"를 보낸다).
QWEN_WAIT_RE = re.compile(r"^\S Waiting for user confirmation\.\.\.$")
QWEN_HEAD_RE = re.compile(r"^\? (\S+)(?: (.*?))? ←$")
QWEN_ESC_SUFFIX = " (esc)"
QWEN_APPROVE_FIRST = "Yes, allow once"
QWEN_REJECT = "No, suggest changes"
QWEN_SHELL_PERSIST_RE = re.compile(r"^Always allow run .+ commands (?:in this project|for this user)$")
QWEN_EDIT_TOOLS = ("WriteFile", "Edit")
QWEN_EDIT_ALWAYS = "Yes, allow always"  # 파일 저장 없이 이 세션만 auto-accept edits로 바뀐다(실측)
QWEN_PLAN_TOOL = "ExitPlanMode"
QWEN_PLAN_Q = "Would you like to proceed?"
QWEN_PLAN_EDITOR = "o open full plan in editor"
QWEN_PLAN_LABELS = ("Yes, and auto-accept edits", "Yes, and manually approve edits", "No, keep planning")
QWEN_PLAN_RESTORE = "Yes, restore previous mode ("
QWEN_Q_TOOL = "AskUserQuestion"
QWEN_Q_SUMMARY = "Ask user 1 question"
QWEN_Q_SUMMARY_RE = re.compile(r"^Ask user (\d+) questions$")
QWEN_Q_HINT = "↑/↓: Navigate | Enter: Select | Esc: Cancel"
QWEN_Q_HINT_MULTI = "↑/↓: Navigate | Space: Toggle | Enter: Confirm | Esc: Cancel"
QWEN_Q_HINT_TAB = "↑/↓: Navigate | ←/→: Switch tabs | Enter: Select | Esc: Cancel"
QWEN_Q_HINT_TAB_MULTI = "↑/↓: Navigate | ←/→: Switch tabs | Space: Toggle | Enter: Confirm | Esc: Cancel"
QWEN_SUBMIT_HINT = "↑/↓: Navigate | ←/→: Switch tabs | Enter: Select"
QWEN_SUBMIT_Q = "Ready to submit your answers?"
QWEN_SUBMIT_HEAD = "Your answers:"
QWEN_SUBMIT_LABELS = ["Submit answers", "Cancel"]
QWEN_SUBMIT_TAB = "Submit"
QWEN_TAB_CUR = "▸ "
QWEN_TAB_DONE = " ✓"
QWEN_CHECK_ROW_RE = re.compile(r"^(\s*(?:❯\s*)?)\[([ ✓])\]\s+(\d+\.\s+)")
QWEN_FREE = "Type something..."
QWEN_TRUST_Q = "Do you trust this folder?"


def _qwen_block(lines):
    """화면 끝 대기 줄 위의 `? 도구 … ←` 블록 → (도구, 요약, 안쪽 줄) 또는 None."""
    nz = [i for i, ln in enumerate(lines) if ln.strip()]
    if not nz or not QWEN_WAIT_RE.match(lines[nz[-1]].strip()):
        return None
    end = nz[-1]
    head = next((i for i in range(end - 1, max(-1, end - 400), -1) if QWEN_HEAD_RE.match(lines[i].strip())), -1)
    if head < 0:
        return None
    m = QWEN_HEAD_RE.match(lines[head].strip())
    return m.group(1), (m.group(2) or "").strip(), lines[head + 1:end]


def _qwen_menu(inner, stop=None):
    """번호 메뉴(커서 하나) → (rows, 라벨, 질문 줄 index) 또는 None. 라벨 끝 " (esc)"는 뗀다."""
    rows = _menu_rows(inner, 0, len(inner) if stop is None else stop)
    if len(rows) < 2 or sum(1 for r in rows if r["selected"]) != 1:
        return None
    q = next((i for i in range(rows[0]["line"] - 1, -1, -1) if inner[i].strip()), -1)
    if q < 0:
        return None
    labels = []
    for r in rows:
        lb = _norm(r["label"])
        labels.append(lb[:-len(QWEN_ESC_SUFFIX)] if lb.endswith(QWEN_ESC_SUFFIX) else lb)
    return rows, labels, q


def _qwen_finish(p, approve=None):
    if approve is not None:
        p["approve_index"] = approve
    p["agent"] = "qwen"
    return p


def _parse_qwen_approval(tool, summary, inner):
    menu = _qwen_menu(inner)
    if menu is None:
        return None
    rows, labels, q = menu
    question = inner[q].strip()
    if labels[0] != QWEN_APPROVE_FIRST or labels[-1] != QWEN_REJECT or not question.endswith("?"):
        return None
    always = persist = None
    if tool == "Shell":
        # 2번 <cwd>/.qwen/settings.json, 3번 $QWEN_HOME/settings.json의 permissions.allow에 영구 저장(실측)
        if len(rows) != 4 or not all(QWEN_SHELL_PERSIST_RE.match(lb) for lb in labels[1:3]):
            return None
        persist = 1
    elif tool in QWEN_EDIT_TOOLS:
        if len(rows) != 3 or labels[1] != QWEN_EDIT_ALWAYS:
            return None
        always = 1
    else:
        return None  # 캡처하지 않은 도구
    block = _dedent_block(inner[:q])
    body = "\n\n".join(x for x in (summary, block) if x) or None
    opts = [(lb, None, r["num"]) for lb, r in zip(labels, rows)]
    return _qwen_finish(_finish("approval", tool, question, body, opts,
                                selected=next(k for k, r in enumerate(rows) if r["selected"]),
                                always_index=always, reject_index=len(rows) - 1, persist_index=persist), approve=0)


def _parse_qwen_plan(inner):
    menu = _qwen_menu(inner)
    if menu is None:
        return None
    rows, labels, q = menu
    if inner[q].strip() != QWEN_PLAN_Q or len(rows) != 4 or not labels[0].startswith(QWEN_PLAN_RESTORE) \
            or tuple(labels[1:]) != QWEN_PLAN_LABELS:
        return None
    body = _dedent_block([ln for ln in inner[:q] if ln.strip() != QWEN_PLAN_EDITOR])
    opts = [(lb, None, r["num"]) for lb, r in zip(labels, rows)]
    return _qwen_finish(_finish("plan", QWEN_PLAN_TOOL, QWEN_PLAN_Q, body, opts,
                                selected=next(k for k, r in enumerate(rows) if r["selected"]), reject_index=3))


def _qwen_tabs(line, n):
    """탭 막대 `▸ Features   Deploy  Submit` / `Features ✓ ▸ Deploy  Submit` → ([{label, answered}], 지금 탭 index)
    또는 None. 질문 탭 n개 + 맨 끝 Submit이어야 한다."""
    s = line.strip().replace(" " + QWEN_TAB_CUR, "  " + QWEN_TAB_CUR)
    items = [x for x in re.split(r"\s{2,}", s) if x]
    if len(items) != n + 1:
        return None
    tabs, cur = [], None
    for k, it in enumerate(items):
        if it.startswith(QWEN_TAB_CUR):
            if cur is not None:
                return None
            cur, it = k, it[len(QWEN_TAB_CUR):]
        done = it.endswith(QWEN_TAB_DONE)
        if done:
            it = it[:-len(QWEN_TAB_DONE)]
        if not it or "▸" in it:
            return None
        tabs.append({"label": it, "answered": done})
    if cur is None or tabs[-1] != {"label": QWEN_SUBMIT_TAB, "answered": False}:
        return None
    return tabs[:-1], cur


def _parse_qwen_question(summary, inner):
    nz = [i for i, ln in enumerate(inner) if ln.strip()]
    if len(nz) < 4:
        return None
    hint = inner[nz[-1]].strip()
    m = QWEN_Q_SUMMARY_RE.match(summary)
    tabs = cur = None
    if m:
        t = _qwen_tabs(inner[nz[0]], int(m.group(1)))
        if t is None:
            return None
        tabs, cur = t
        if cur == len(tabs):
            return _parse_qwen_submit(inner, nz, tabs)
        if hint not in (QWEN_Q_HINT_TAB, QWEN_Q_HINT_TAB_MULTI):
            return None
        multi = hint == QWEN_Q_HINT_TAB_MULTI
    elif summary == QWEN_Q_SUMMARY and hint in (QWEN_Q_HINT, QWEN_Q_HINT_MULTI):
        multi = hint == QWEN_Q_HINT_MULTI
    else:
        return None
    # 복수 선택 줄 `❯ [✓] 1. Auth`를 `❯ 1. [✓] Auth`로 바꿔 공통 메뉴 읽기(체크박스는 번호 뒤)를 쓴다
    body = [QWEN_CHECK_ROW_RE.sub(r"\1\3[\2] ", ln) for ln in inner] if multi else inner
    menu = _qwen_menu(body, nz[-1])
    if menu is None:
        return None
    rows, labels, _ = menu
    if any(r["checkbox"] for r in rows) != multi or (multi and not all(r["checkbox"] for r in rows)) \
            or any(lb.startswith(">") for lb in labels):
        return None  # 힌트와 줄 모양이 다르거나, PC에서 직접 입력 칸을 연 상태(`❯ 4. > Type something...`)
    head = [body[i].strip() for i in nz if i < rows[0]["line"]]
    if tabs is not None:
        head = head[1:]  # 탭 막대
        if not head or labels[-1] != QWEN_FREE:
            return None
        title, question = None, _norm(" ".join(head))
    else:
        if len(head) < 2:
            return None
        title, question = head[0], _norm(" ".join(head[1:]))
    keep = list(range(len(rows)))
    if multi:
        # 복수 선택의 "Type something..."은 체크박스 줄이라 카드에서 뺀다(Claude와 같다). PC에서 체크했으면 모르는 상태
        if labels[-1] != QWEN_FREE or rows[-1]["checked"]:
            return None
        keep = keep[:-1]
    opts = [(labels[k], _norm(" ".join(rows[k]["extra"])) or None, rows[k]["num"]) for k in keep]
    kl = [labels[k] for k in keep]
    free = kl.index(QWEN_FREE) if QWEN_FREE in kl and not multi else None
    sel = next((j for j, k in enumerate(keep) if rows[k]["selected"]), None)
    return _qwen_finish(_finish("question", title, question, None, opts, selected=sel, multi=multi,
                                checked=[j for j, k in enumerate(keep) if rows[k]["checked"]],
                                free_text_index=free, tabs=tabs))


def _parse_qwen_submit(inner, nz, tabs):
    """여러 질문 끝 Submit 탭: "Your answers:" 요약 + "Ready to submit your answers?" 1 Submit answers / 2 Cancel."""
    if inner[nz[-1]].strip() != QWEN_SUBMIT_HINT:
        return None
    menu = _qwen_menu(inner, nz[-1])
    if menu is None:
        return None
    rows, labels, q = menu
    if labels != QWEN_SUBMIT_LABELS or inner[q].strip() != QWEN_SUBMIT_Q:
        return None
    ans = [i for i in nz if nz[0] < i < q]
    if not ans or inner[ans[0]].strip() != QWEN_SUBMIT_HEAD:
        return None
    body = "\n".join(inner[i].strip() for i in ans)
    return _qwen_finish(_finish("submit", None, QWEN_SUBMIT_Q, body, [(lb, None, r["num"]) for lb, r in zip(labels, rows)],
                                selected=next(k for k, r in enumerate(rows) if r["selected"]), tabs=tabs))


def _pick_qwen(p, option_index):
    """Submit 탭은 숫자 키가 커서만 옮긴다: 숫자 + enter(실측 0.28.3). 그 밖에는 기본(숫자 키 = 고르고 제출)."""
    if p["kind"] == "submit":
        return [("keys", [str(p["options"][option_index]["number"]), "enter"])]
    return None


def _multi_qwen(p, num, want):
    """복수 선택: 숫자 키는 커서만 옮기고 space가 토글, enter가 확정(실측 0.28.3). 하나도 안 고르면 enter가 무시된다."""
    if not want:
        raise ApiError(400, "bad_option")
    if any(num(i) > 9 for i in want.symmetric_difference(set(p["checked"]))):  # 누를 숫자 키는 한 자리만
        raise ApiError(400, "bad_option")
    keys = []
    for i in sorted(want.symmetric_difference(set(p["checked"]))):
        keys += [str(num(i)), "space"]
    return [("keys", keys + ["enter"])]


def _parse_qwen_trust(lines):
    inner = _last_box(lines)
    if inner is None:
        return None
    menu = _qwen_menu(inner)
    if menu is None:
        return None
    rows, labels, _ = menu
    nz = [i for i, ln in enumerate(inner) if ln.strip() and i < rows[0]["line"]]
    if not nz or inner[nz[0]].strip() != QWEN_TRUST_Q or len(rows) != 3 \
            or not labels[0].startswith("Trust folder") or not labels[1].startswith("Trust parent folder") \
            or labels[2] != "Don't trust":
        return None
    body = _dedent_block(inner[nz[0] + 1:rows[0]["line"]])
    opts = [(lb, None, r["num"]) for lb, r in zip(labels, rows)]
    # 1번도 $QWEN_HOME/trustedFolders.json에 저장되지만 신뢰 화면의 기본 답이다. 2번(상위 폴더)은 더 넓은 영구 허용
    return _qwen_finish(_finish("approval", None, QWEN_TRUST_Q, body, opts,
                                selected=next(k for k, r in enumerate(rows) if r["selected"]),
                                reject_index=2, persist_index=1), approve=0)


def _parse_qwen_lines(lines, plan_reader=None):
    blk = _qwen_block(lines)
    if blk is None:
        return _parse_qwen_trust(lines)
    tool, summary, inner = blk
    if tool == QWEN_PLAN_TOOL:
        return _parse_qwen_plan(inner)
    if tool == QWEN_Q_TOOL:
        return _parse_qwen_question(summary, inner)
    return _parse_qwen_approval(tool, summary, inner)


def is_qwen(info):
    """Qwen Code 터미널. hc agent·status.app("qwen-code")·`node …/qwen`, 또는 Qwen이 자기 자신을 다시 띄운
    `node --expose-gc …/@qwen-code/qwen-code/cli.js`(실측 0.28.3)."""
    if _agent_names(info) & {"qwen", "qwen-code"}:
        return True
    fg = (info or {}).get("foreground")
    argv = fg.get("argv") if isinstance(fg, dict) else None
    if isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv) \
            and _binary_base(argv[0]) in SCRIPT_HOSTS:
        script = next((a for a in argv[1:] if not a.startswith("-")), "")
        return script.replace("\\", "/").endswith("/@qwen-code/qwen-code/cli.js")
    return False


# ---- OpenCode 대화 상자. 실측: opencode 1.19.6, 194열(tests/fixtures/opencode-*-1.19.6.txt).
# 승인·질문·plan_exit는 화면 맨 아래 왼쪽 막대(`┃`) 블록이다. 오른쪽 사이드바(제목·Context·경로·"• OpenCode <버전>")가
# 테두리 없이 같은 줄에 붙어 있어 마지막 줄의 "• OpenCode" 열에서 자른다(사이드바가 없는 좁은 화면은 자르지 않는다).
# OSC 7501을 안 보내고 제목은 "OC | <세션 제목>"으로 고정이라 막힘·작업 중은 화면으로만 안다.
# 승인 메뉴 `Allow once   Allow always   Reject`는 번호가 없고 숫자 키를 무시한다. ←/→(h/l)는 끝에서 반대편으로 돌고
# 지금 고른 버튼은 색으로만 보여 화면 글자로 알 수 없다. 메뉴가 뜰 때 커서는 늘 Allow once라(실측) 그 자리를 기준으로
# 한 번 허용 = enter, 계속 허용 = right enter + 2차 확인(Confirm) enter, 거절 = esc(커서 위치와 상관없다).
# "Allow always"는 메모리에만 있다("until OpenCode is restarted", 재시작 뒤 다시 물음 실측): 세션 한정.
# 2차 확인 화면(△ Always allow)의 esc는 Cancel(원래 메뉴로, 커서는 다시 Allow once), esc 두 번이면 거절(실측).
# 질문(질문 1개·단일 선택)은 번호를 누르면 바로 제출, "Type your own answer"는 번호 → 붙여넣기+enter.
# 여러 질문이거나 복수 선택이면 위에 탭 막대(`Features   License   Confirm`, 질문 하나 복수 선택도 `Langs   Confirm`)가 있고
# 카드는 지금 탭 하나만 담는다. 지금 탭·답한 탭은 색으로만 보여 화면 글자로 모른다: 질문 탭이 하나일 때만 tabs를 채우고
# 여럿이면 tabs 없음. Confirm 탭(Review)은 `<탭>: <답>`/`(not answered)` 요약이라 거기서는 답한 탭을 안다.
# 키(실측 1.19.6): 복수 선택(`1. [✓] Logging`, `enter toggle`)은 숫자 키 = 토글(커서와 상관없이), tab = 다음 탭(안 골라도
# 넘어가고 모델은 "Unanswered"를 받는다). 단일 선택 탭(`enter confirm`)은 숫자 = 고르고 다음 탭, 직접 입력은 숫자 → 붙여넣기+enter
# (입력 뒤 다음 탭). Review(`enter submit`)는 enter = 제출, esc = 질문 dismiss(턴 끝).
# 직접 입력 칸이 열렸거나 글이 들어간 상태(항목 아래 줄)는 숫자가 칸에 써지므로 카드 없음. 복수 선택 카드엔 직접 입력 없음.
# plan_exit(실험 플랜 모드)는 질문 모양 Yes/No(No는 턴 끝).
OPENCODE_SIDEBAR_RE = re.compile(r"• OpenCode \d")
OPENCODE_PERM_HEAD = "△ Permission required"
OPENCODE_ALWAYS_HEAD = "△ Always allow"
OPENCODE_PERM_LABELS = ("Allow once", "Allow always", "Reject")
OPENCODE_BUTTONS_RE = re.compile(r"^Allow once {2,}Allow always {2,}Reject {2,}(.+)$")
OPENCODE_CONFIRM_RE = re.compile(r"^Confirm {2,}Cancel {2,}(.+)$")
OPENCODE_PERM_HINT_RE = re.compile(r"^(?:ctrl\+f (?:fullscreen|minimize) {2})?⇆ select {2}enter confirm$")
OPENCODE_ALWAYS_Q_RE = re.compile(r"^This will allow .+ until OpenCode is restarted\.?$")
OPENCODE_SHELL_HEAD = "# Shell command"
OPENCODE_EDIT_HEAD_RE = re.compile(r"^→ (Edit \S.*)$")
OPENCODE_Q_HINT = "↑↓ select  enter submit  esc dismiss"
OPENCODE_FREE = "Type your own answer"
OPENCODE_Q_HINT_MULTI = "⇆ tab  ↑↓ select  enter toggle  esc dismiss"
OPENCODE_Q_HINT_TAB = "⇆ tab  ↑↓ select  enter confirm  esc dismiss"
OPENCODE_REVIEW_HINT = "⇆ tab  enter submit  esc dismiss"
OPENCODE_REVIEW_HEAD = "Review"
OPENCODE_CONFIRM_TAB = "Confirm"
OPENCODE_MULTI_SUFFIX = " (select all that apply)"
OPENCODE_NOT_ANSWERED = "(not answered)"
OPENCODE_SUBMIT_LABELS = ("Submit", "Dismiss")
OPENCODE_PLAN_Q_RE = re.compile(
    r"^Plan at \S+ is complete\. Would you like to switch to the build agent and start implementing\?$")
OPENCODE_BUSY_RE = re.compile(r"^\s*\S+\s+esc interrupt\b")  # 작업 중 맨 아래 줄 "⬝⬝⬝⬝⬝⬝⬝⬝  esc interrupt"
# 카드를 못 읽어도 막힘인 대화 상자 힌트(승인·2차 확인 "⇆ select  enter confirm", 질문·여러 질문 "… esc dismiss")
OPENCODE_DIALOG_RE = re.compile(r"⇆ select {2}enter confirm|\besc dismiss\b")
OPENCODE_ANSWER_KEYS = {"Allow once": ["enter"], "Allow always": ["right", "enter", "enter"], "Reject": ["esc"],
                        "Confirm": ["enter"], "Cancel": ["esc"], "Submit": ["enter"], "Dismiss": ["esc"]}


def _cut_cols(line, col):
    """표시 열 col부터 잘라 낸 앞부분."""
    w = 0
    for i, ch in enumerate(line):
        if w >= col:
            return line[:i]
        w += char_width(ch)
    return line


def _opencode_block(lines):
    """화면 맨 아래 `┃` 블록의 안쪽 줄(막대 뒤 글자, 사이드바는 잘라 냄). 맨 아래가 막대 블록이 아니면 None."""
    nz = [i for i, ln in enumerate(lines) if ln.strip()]
    if not nz:
        return None
    last = nz[-1]
    bar = lines[last].find("┃")
    if bar < 0 or lines[last][:bar].strip():
        return None
    m = OPENCODE_SIDEBAR_RE.search(lines[last])
    cut = sum(char_width(c) for c in lines[last][:m.start()]) if m else None
    top = last
    while top > 0 and len(lines[top - 1]) > bar and lines[top - 1][bar] == "┃" and not lines[top - 1][:bar].strip():
        top -= 1
    return [(_cut_cols(ln, cut) if cut is not None else ln)[bar + 1:].rstrip() for ln in lines[top:last + 1]]


def _parse_opencode_approval(inner):
    idx = [i for i, ln in enumerate(inner) if ln.strip()]
    if len(idx) < 3:
        return None
    head, btn = inner[idx[0]].strip(), idx[-1]
    rx = {OPENCODE_PERM_HEAD: OPENCODE_BUTTONS_RE, OPENCODE_ALWAYS_HEAD: OPENCODE_CONFIRM_RE}.get(head)
    m = rx.match(inner[btn].strip()) if rx else None
    if not m or not OPENCODE_PERM_HINT_RE.match(m.group(1).strip()):
        return None
    mid = [i for i in idx if idx[0] < i < btn]
    if not mid:
        return None
    first = inner[mid[0]].strip()
    if head == OPENCODE_PERM_HEAD:
        if first == OPENCODE_SHELL_HEAD:
            title = first[2:]
        else:
            me = OPENCODE_EDIT_HEAD_RE.match(first)
            if not me:
                return None  # 캡처하지 않은 권한(웹 가져오기, 폴더 밖 접근 등)
            title = me.group(1)
        p = _finish("approval", title, OPENCODE_PERM_HEAD[2:], _dedent_block(inner[mid[0] + 1:btn]),
                    [(lb, None, None) for lb in OPENCODE_PERM_LABELS], selected=0, always_index=1, reject_index=2)
    else:
        if not OPENCODE_ALWAYS_Q_RE.match(first):
            return None
        # 거절(reject_index 없음)은 esc 두 번: 2차 확인의 esc는 Cancel(원래 메뉴)이라 한 번 더 눌러 거절한다
        p = _finish("approval", OPENCODE_ALWAYS_HEAD[2:], first, _dedent_block(inner[mid[0] + 1:btn]) or first,
                    [("Confirm", None, None), ("Cancel", None, None)], selected=0)
    p["approve_index"] = 0
    return p


def _opencode_tabs(line):
    """탭 막대 `Features   License   Confirm` → 질문 탭 라벨 목록(맨 끝 Confirm 뺌) 또는 None."""
    items = [x for x in re.split(r"\s{2,}", line.strip()) if x]
    if len(items) < 2 or items[-1] != OPENCODE_CONFIRM_TAB or OPENCODE_CONFIRM_TAB in items[:-1]:
        return None
    return items[:-1]


def _parse_opencode_review(inner, idx, labels):
    """Confirm 탭: `Review` + 탭마다 `<라벨>: <답>`(안 답하면 `(not answered)`) → 확인 카드(Submit = enter, Dismiss = esc)."""
    body = [inner[i].strip() for i in idx[1:-1]]
    if not body or body[0] != OPENCODE_REVIEW_HEAD:
        return None
    entries = []
    for ln in body[1:]:
        k = len(entries)
        if k < len(labels) and ln.startswith(labels[k] + ": "):
            entries.append(ln)
        elif entries:
            entries[-1] += " " + ln  # 긴 답이 줄바꿈됐다
        else:
            return None
    if len(entries) != len(labels):
        return None
    tabs = [{"label": lb, "answered": e[len(lb) + 2:].strip() != OPENCODE_NOT_ANSWERED} for lb, e in zip(labels, entries)]
    return _finish("submit", None, OPENCODE_REVIEW_HEAD, "\n".join(entries),
                   [(lb, None, None) for lb in OPENCODE_SUBMIT_LABELS], reject_index=1, tabs=tabs)


def _parse_opencode_question(inner):
    idx = [i for i, ln in enumerate(inner) if ln.strip()]
    if len(idx) < 3:
        return None
    hint = inner[idx[-1]].strip()
    labels_tab = None
    if hint in (OPENCODE_Q_HINT_MULTI, OPENCODE_Q_HINT_TAB, OPENCODE_REVIEW_HINT):
        labels_tab = _opencode_tabs(inner[idx[0]])
        if labels_tab is None:
            return None
        if hint == OPENCODE_REVIEW_HINT:
            return _parse_opencode_review(inner, idx, labels_tab)
        idx = idx[1:]
    elif hint != OPENCODE_Q_HINT:
        return None
    multi = hint == OPENCODE_Q_HINT_MULTI
    rows = _menu_rows(inner, idx[0], idx[-1])
    if not rows or any(r["selected"] for r in rows) or any(r["checkbox"] != multi for r in rows):
        return None
    head = [inner[i].strip() for i in idx if i < rows[0]["line"]]
    if not head:
        return None
    question = _norm(" ".join(head))
    if multi:
        if not question.endswith(OPENCODE_MULTI_SUFFIX):
            return None
        question = question[:-len(OPENCODE_MULTI_SUFFIX)].strip()
    labels = [_norm(r["label"]) for r in rows]
    # 이미 답한 단일 선택 탭에 다시 오면 고른 항목이 `MIT ✓`로 그려진다: 라벨에서 떼고 답했다는 것만 남긴다
    answered_here = not multi and any(lb.endswith(" ✓") for lb in labels)
    labels = [lb[:-2] if not multi and lb.endswith(" ✓") else lb for lb in labels]
    free = None
    if labels[-1] == OPENCODE_FREE:
        if rows[-1]["extra"] or rows[-1]["checked"]:
            return None  # PC에서 직접 입력 칸을 열었거나 글을 넣었다(칸·글이 항목 아래에 그려진다)
        free = len(rows) - 1
    elif labels_tab is not None:
        return None  # 탭 화면은 늘 직접 입력 항목이 끝에 있다(실측)
    keep = list(range(len(rows)))
    if multi:
        keep, free = keep[:-1], None  # 복수 선택의 직접 입력은 체크박스 줄이라 카드에서 뺀다(Claude와 같다)
    opts = [(labels[k], _norm(" ".join(rows[k]["extra"])) or None, rows[k]["num"]) for k in keep]
    tabs = title = None
    if labels_tab is not None and len(labels_tab) == 1:
        title, tabs = labels_tab[0], [{"label": labels_tab[0], "answered": False}]
    if labels_tab is None and OPENCODE_PLAN_Q_RE.match(question):
        if labels != ["Yes", "No"]:
            return None
        return _finish("plan", None, question, None, opts, reject_index=1)
    p = _finish("question", title, question, None, opts, multi=multi,
                checked=[j for j, k in enumerate(keep) if rows[k]["checked"]], free_text_index=free, tabs=tabs)
    if answered_here:
        p["answered_here"] = True  # prompt_id에는 안 들어간다
    return p


def _parse_opencode_lines(lines, plan_reader=None):
    inner = _opencode_block(lines)
    if inner is None:
        return None
    p = _parse_opencode_approval(inner) or _parse_opencode_question(inner)
    if p:
        p["agent"] = "opencode"
    return p


def _opencode_dialog_group(text):
    """여러 질문 상자(탭 막대 + 탭 힌트)면 탭 라벨 묶음, 아니면 None."""
    inner = _opencode_block(screen_lines(text))
    idx = [i for i, ln in enumerate(inner or []) if ln.strip()]
    if len(idx) < 2 or inner[idx[-1]].strip() not in (OPENCODE_Q_HINT_MULTI, OPENCODE_Q_HINT_TAB, OPENCODE_REVIEW_HINT):
        return None
    tabs = _opencode_tabs(inner[idx[0]])
    return tuple(tabs) if tabs else None


SGR_SEQ_RE = re.compile(r"\x1b\[([0-9;:]*)m|\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")


def _sgr_cells(line):
    """색 그림 한 줄 → (글자들, 글자마다 (글자색, 배경색, 반전)). SGR 말고 다른 제어열은 버린다."""
    fg = bg = None
    rev = False
    chars, styles = [], []
    pos = 0
    for m in list(SGR_SEQ_RE.finditer(line)) + [None]:
        end = m.start() if m else len(line)
        for ch in line[pos:end]:
            if ch >= " ":
                chars.append(ch)
                styles.append((fg, bg, rev))
        if m is None:
            break
        pos = m.end()
        if m.group(1) is None:
            continue
        ps = [int(x) if x.isdigit() else 0 for x in re.split(r"[;:]", m.group(1))]
        k = 0
        while k < len(ps):
            v = ps[k]
            if v in (38, 48) and k + 1 < len(ps):
                n = 3 if ps[k + 1] == 2 else 1
                col = tuple(ps[k + 2:k + 2 + n])
                if v == 38:
                    fg = col
                else:
                    bg = col
                k += 2 + n
                continue
            if v == 0:
                fg = bg = None
                rev = False
            elif v == 7:
                rev = True
            elif v == 27:
                rev = False
            elif v == 39:
                fg = None
            elif v == 49:
                bg = None
            elif 30 <= v <= 37 or 90 <= v <= 97:
                fg = (v,)
            elif 40 <= v <= 47 or 100 <= v <= 107:
                bg = (v,)
            k += 1
    return "".join(chars), styles


def _opencode_style_tabs(p, text, read):
    """여러 질문 탭 막대의 지금 탭·답한 탭은 색으로만 보인다(실측 1.19.6, 기본 테마): 지금 탭은 배경이 강조색,
    답한 탭은 글자색이 밝고 답 안 한 탭은 Confirm 탭과 같은 흐린 글자색. read()가 준 색 그림(hc attach)으로 카드의
    tabs·title을 채운다. id는 그대로(글자 화면으로 만든 재확인·알림 카드와 같아야 한다). 못 읽으면 그대로 둔다."""
    if not p or p["kind"] != "question" or p.get("tabs") is not None:
        return p
    labels = _opencode_dialog_group(text)
    if not labels or len(labels) < 2:
        return p
    raw = read() if callable(read) else read
    names = list(labels) + [OPENCODE_CONFIRM_TAB]
    bar = re.compile(r"(?:^|\s)" + r"\s{2,}".join("(%s)" % re.escape(x) for x in names) + r"(?=\s|$)")
    for line in (raw or "").split("\n"):
        plain, styles = _sgr_cells(line.rstrip("\r"))
        m = bar.search(plain)
        if not m:
            continue
        st = [styles[m.start(k + 1)] for k in range(len(names))]
        if any(x[2] for x in st):
            return p  # 반전으로 그린 테마: 모르는 모양
        bgs = [x[1] for x in st]
        cur = [k for k in range(len(st)) if bgs.count(bgs[k]) == 1]
        if len(cur) != 1 or cur[0] == len(labels) or st[-1][0] is None:
            return p
        muted = st[-1][0]
        c = cur[0]
        if p["multi_select"]:
            now = bool(p["checked"])
        else:
            now = bool(p.get("answered_here"))  # 답한 단일 선택 탭에 다시 오면 `MIT ✓`(파서가 라벨에서 뗀다)
        p["tabs"] = [{"label": lb, "answered": now if k == c else st[k][0] != muted} for k, lb in enumerate(labels)]
        p["title"] = labels[c]
        return p
    return p


def _pick_opencode(p, option_index):
    """승인·2차 확인 버튼(번호 없음, 커서가 돌아 화면으로 위치를 모른다): 뜰 때 자리(첫 버튼) 기준 키.
    Review 탭은 enter(제출)·esc(dismiss). 질문은 숫자 키."""
    if p["kind"] not in ("approval", "submit"):
        return None
    return [("keys", list(OPENCODE_ANSWER_KEYS[p["options"][option_index]["label"]]))]


def _multi_opencode(p, num, want):
    """복수 선택: 숫자 키 = 토글(커서와 상관없이), tab = 다음 탭(실측 1.19.6). 안 골라도 tab으로 넘어간다."""
    if any(num(i) > 9 for i in want.symmetric_difference(set(p["checked"]))):  # 누를 숫자 키는 한 자리만
        raise ApiError(400, "bad_option")
    return [("keys", [str(num(i)) for i in sorted(want.symmetric_difference(set(p["checked"])))] + ["tab"])]


def _reject_keys_opencode(p):
    return ["esc", "esc"] if p["kind"] == "approval" and p["title"] == OPENCODE_ALWAYS_HEAD[2:] else ["esc"]


# ---- Grok Build 대화 상자. 실측: grok 1.0.77, 200열(tests/fixtures/grok-*-1.0.77.txt).
# 승인·질문은 화면 끝 힌트 줄 바로 위 `┃` 줄들(왼쪽 막대)에 그려진다. 메뉴 줄은 `1 (●) Yes, proceed`(질문 직접 입력 `z`).
# 승인 힌트 `1/N:select │ … │ Esc:scrollback`의 N이 항목 수다. 항목 수·번호가 명령마다 달라(4개 또는 5개, Yes가 2번 또는 3번)
# 라벨로 맞춘다. 숫자 키가 그 항목을 바로 고르고 제출한다. Esc는 거절이 아니라 포커스를 스크롤백으로 옮긴다(그때 힌트가
# `Tab/Space:permission`으로 바뀌고 숫자 키를 무시한다: 카드 없음). always-approve 모드 항목은 $GROK_HOME/config.toml에
# 전역 영구 저장, "Always allow:"·"Never allow:"는 그 폴더 권한 파일에 영구 저장, "allow all edits during this session"만
# 세션 한정(파일 변화 없음). 계획 검토는 숫자 메뉴 없이 a 승인 / s 수정 요청(입력창으로) / q 계획 포기.
# 폴더 신뢰는 화면 가운데 y/n 글자 키.
# 여러 질문은 메뉴 아래 `[1/2] ↑/↓ navigate · ←/→ question · y copy  Enter:…`(질문 이름 탭 막대 없음): 카드는 지금 질문
# 하나(제목 "k/N"). 복수 선택은 `1 [x] Tests`(질문 하나여도). 키(실측 1.0.77): 단일 선택 질문은 숫자 = 고르고 다음 질문
# (마지막이면 전체 제출), 직접 입력은 z → 붙여넣기+enter. 복수 선택은 숫자 = 토글(커서와 상관없이), 고른 게 있으면
# enter = 다음 질문(마지막이면 제출, 힌트 Enter:next/submit · Esc:unselect), 하나도 없으면 enter가 커서 줄을 골라 버려
# → = 다음 질문(안 답한 질문은 모델에 안 간다), 마지막 질문이면 카드에서 빈 답 불가. 복수 선택의 z 줄은 카드에서 뺀다.
# hc msg는 지금 질문 문장이라 질문을 넘기면 바뀐다: dialog_group으로 같은 상자면 알림 한 번.
GROK_GUTTER = "┃"
GROK_ROW_RE = re.compile(r"^([1-9z]) (\([○●]\)|\[[ xX✓]\]) ?(.*)$")
GROK_APPROVAL_HINT_RE = re.compile(r"^1/(\d+):select │ .*│ Esc:scrollback$")
GROK_SCOPE_RE = re.compile(r"^← → narrow scope(?: · e edit pattern)?$")
GROK_EDIT_Q_RE = re.compile(r"^Allow Edit to (.+)\?$")
GROK_EDIT_STATUS_RE = re.compile(r"^◆ Running (?:Write|Edit) `")
GROK_EDIT_HEAD_RE = re.compile(r"^◆ (?:Creating|Edit) (\S.*)$")
GROK_ALWAYS_APPROVE = "Yes, and don't ask again for anything (always-approve mode)"
GROK_SESSION_EDITS = "Yes, allow all edits during this session"
GROK_REJECT = "No, reject"
GROK_REJECT_SUFFIX = " (type to add feedback)"
GROK_RULE_PREFIXES = ("Always allow: ", "Never allow: ")
GROK_EDIT_LABELS = (GROK_ALWAYS_APPROVE, GROK_SESSION_EDITS, "Yes", GROK_REJECT)
GROK_Q_HINT_RE = re.compile(r"^Tab:next answer │ Esc:(scrollback|unselect) │ Shift\+x:dismiss$")
GROK_Q_NAV_RE = re.compile(r"^(?:\[(\d+)/(\d+)\] )?↑/↓ navigate · (←/→ question · )?y copy Enter:(submit|edit|select|next)$")
GROK_FREE = "Type your answer here"
GROK_PLAN_HINT = "c:comment │ y:copy plan │ a:approve │ q:quit plan │ v:select │ Tab:prompt"
GROK_PLAN_MENU = "a approve | s request changes | c comment | y copy plan | q quit plan"
GROK_PLAN_WAIT = "◆ Waiting on plan approval"
GROK_PLAN_TOP = "╭─ plan.md"
GROK_PLAN_Q = "Review the plan"
GROK_PLAN_LINE_RE = re.compile(r"^\s*(\d+)(?: (.*))?$")
GROK_TRUST_Q = "Do you trust the contents of this directory?"
GROK_TRUST_ROW_RE = re.compile(r"^(Yes, proceed|No, quit)\s{2,}([yn])$")
GROK_FOOTER_RE = re.compile(r"^Grok Build\s+\d+\.\d+\.\d+\S*$")
# 번호 없는 항목의 글자 키(계획 검토 a/q, 폴더 신뢰 y/n). "Request changes"는 직접 입력(s → 붙여넣기)
GROK_LETTER_KEYS = {("plan", "Approve"): "a", ("plan", "Quit plan"): "q",
                    ("approval", "Yes, proceed"): "y", ("approval", "No, quit"): "n"}


def _grok_gutter(lines):
    """화면 끝 힌트 줄 바로 위의 `┃` 줄들 → (힌트(공백 정리), 안쪽 줄들, 첫 `┃` 줄 index) 또는 None.
    오른쪽 끝 스크롤 막대(█)는 뗀다."""
    nz = [i for i, ln in enumerate(lines) if ln.strip()]
    if not nz:
        return None
    end = nz[-1]
    i = end - 1
    while i >= 0 and not lines[i].strip():
        i -= 1
    inner = []
    while i >= 0 and lines[i].lstrip().startswith(GROK_GUTTER):
        s = lines[i].lstrip()[1:]
        s = s[2:] if s.startswith("  ") else s.lstrip()
        inner.append(s.rstrip().rstrip("█").rstrip())
        i -= 1
    if not inner:
        return None
    inner.reverse()
    return _norm(lines[end]), inner, i + 1


def _grok_rows(inner, stop=None):
    """메뉴 줄(이어진 줄만) → [{line, key, mark, text}] 또는 None."""
    rows = []
    for k, s in enumerate(inner[:stop]):
        m = GROK_ROW_RE.match(s)
        if m:
            rows.append({"line": k, "key": m.group(1), "mark": m.group(2), "text": m.group(3)})
    if not rows or rows[-1]["line"] - rows[0]["line"] != len(rows) - 1:
        return None
    return rows


def _grok_edit_preview(lines, top, path):
    """파일 승인 위 대화 영역의 미리보기(`◆ Creating <파일>`/`◆ Edit <파일>` 아래 줄들). 승인 상자 바로 위 상태 줄
    (`◆ Running Write|Edit …`) 위의 마지막 `◆` 줄이 같은 파일일 때만. 없으면 None."""
    st = next((i for i in range(top - 1, -1, -1) if lines[i].strip()), -1)
    if st < 0 or not GROK_EDIT_STATUS_RE.match(lines[st].strip()):
        return None
    head = next((i for i in range(st - 1, -1, -1) if lines[i].strip().startswith("◆ ")), -1)
    m = GROK_EDIT_HEAD_RE.match(lines[head].strip()) if head >= 0 else None
    if not m or m.group(1).rsplit("/", 1)[-1] != path.rsplit("/", 1)[-1]:
        return None
    return _dedent_block([ln.rstrip("█").rstrip() for ln in lines[head + 1:st]])


def _parse_grok_approval(hint, inner, lines, top):
    m = GROK_APPROVAL_HINT_RE.match(hint)
    rows = _grok_rows(inner) if m else None
    if not rows:
        return None
    n = int(m.group(1))
    if len(rows) != n or [r["key"] for r in rows] != [str(k) for k in range(1, n + 1)] \
            or any(r["mark"] not in ("(○)", "(●)") for r in rows) or sum(r["mark"] == "(●)" for r in rows) != 1 \
            or any(s.strip() for s in inner[rows[-1]["line"] + 1:]):
        return None
    head = [s for s in inner[:rows[0]["line"]] if s.strip()]
    if not head or not head[0].startswith("›"):
        return None
    question = head[0][1:].strip()
    cmd, rest = [], head[1:]
    while rest and rest[0].startswith("│"):
        s = rest.pop(0)
        cmd.append(s[2:] if s.startswith("│ ") else s[1:])
    if rest and GROK_SCOPE_RE.match(_norm(rest[0])):
        rest.pop(0)
    if rest or not question:
        return None
    labels = []
    for r in rows:
        lb = _norm(r["text"])
        labels.append(lb[:-len(GROK_REJECT_SUFFIX)] if lb.endswith(GROK_REJECT_SUFFIX) else lb)
    if cmd:
        # 셸 명령(run_terminal_command): 질문은 모델이 준 description(hc status msg와 같다)
        if "Yes, proceed" not in labels or GROK_REJECT not in labels or labels[0] != GROK_ALWAYS_APPROVE \
                or any(lb not in (GROK_ALWAYS_APPROVE, "Yes, proceed", GROK_REJECT)
                       and not lb.startswith(GROK_RULE_PREFIXES) for lb in labels):
            return None
        title, body, approve, always = "Shell", "\n".join(cmd), labels.index("Yes, proceed"), None
    else:
        em = GROK_EDIT_Q_RE.match(question)
        if not em or tuple(labels) != GROK_EDIT_LABELS:
            return None  # 캡처하지 않은 승인
        path = em.group(1)
        pv = _grok_edit_preview(lines, top, path)
        title, body = "Edit", path + ("\n\n" + pv if pv else "")
        approve, always = 2, 1  # 2번 "allow all edits during this session"은 메모리만(실측: 파일 변화 없음)
    opts = [(lb, None, int(r["key"])) for lb, r in zip(labels, rows)]
    # 거절 줄은 입력 항목이다(글자를 치면 그 줄이 입력 칸이 된다): 거절 버튼은 그 번호(reject_keys),
    # 이 줄을 고르면 직접 입력으로 메모와 함께 거절(턴이 이어지고 메모가 새 사용자 메시지로 간다, 실측)
    p = _finish("approval", title, question, body, opts,
                selected=next(k for k, r in enumerate(rows) if r["mark"] == "(●)"),
                always_index=always, free_text_index=labels.index(GROK_REJECT), persist_index=0)
    p["approve_index"] = approve
    return p


def _grok_q_nav(inner):
    """질문 메뉴 아래 안내 줄 → (match, 그 줄 index) 또는 None. 여러 질문이면 `[k/N] … ←/→ question` 둘 다 있어야 한다."""
    nz = [k for k, s in enumerate(inner) if s.strip()]
    nav = GROK_Q_NAV_RE.match(_norm(inner[nz[-1]])) if nz else None
    if not nav or (nav.group(1) is None) != (nav.group(3) is None):
        return None
    if nav.group(1) is not None and not 1 <= int(nav.group(1)) <= int(nav.group(2)):
        return None
    return nav, nz


def _parse_grok_question(hint, inner):
    hm = GROK_Q_HINT_RE.match(hint)
    nv = _grok_q_nav(inner) if hm else None
    if not nv:
        return None  # 직접 입력 중에는 힌트가 다르다
    nav, nz = nv
    rows = _grok_rows(inner, nz[-1])
    if not rows:
        return None
    multi = rows[0]["mark"].startswith("[")
    if any(r["mark"].startswith("[") != multi for r in rows):
        return None
    keys = [r["key"] for r in rows]
    if keys[-1] != "z" or keys[:-1] != [str(k) for k in range(1, len(rows))] or rows[-1]["text"] != GROK_FREE \
            or any(inner[k].strip() for k in range(rows[-1]["line"] + 1, nz[-1])):
        return None  # z 줄에 입력 칸이 열린 상태(`z (●) ❯`) 등
    checked = [k for k, r in enumerate(rows[:-1]) if multi and r["mark"] != "[ ]"]
    tab, ntab, enter = nav.group(1), nav.group(2), nav.group(4)
    last = tab is None or tab == ntab
    if multi:
        if rows[-1]["mark"] != "[ ]" or len(rows) < 2:
            return None  # PC에서 z 줄을 체크했다(모르는 상태)
        # 실측: 체크가 있으면 Esc:unselect, 마지막이 아니면 체크 여부에 따라 Enter:next / Enter:select
        if (hm.group(1) == "unselect") != bool(checked) \
                or enter != ("submit" if last else ("next" if checked else "select")):
            return None
    elif hm.group(1) != "scrollback" or enter != ("select" if not last else ("edit" if len(rows) == 1 else "submit")):
        return None  # 옵션 없는 질문만 Enter:edit(커서가 이미 z 줄). 탭 중간의 옵션 없는 질문·답한 탭으로 돌아온 화면은 안 봄
    if tab is not None and len(rows) == 1:
        return None
    q = [inner[k].strip() for k in nz if k < rows[0]["line"]]
    if not q:
        return None
    opts = []
    for r in rows[:-1]:
        parts = re.split(r"\s{2,}", r["text"].strip(), maxsplit=1)
        opts.append((parts[0], parts[1] if len(parts) > 1 else None, int(r["key"])))
    free = None
    if not multi:
        opts.append((GROK_FREE, None, None))
        free = len(opts) - 1
    sel = next((k for k, r in enumerate(rows) if r["mark"] == "(●)"), None)
    p = _finish("question", None if tab is None else "%s/%s" % (tab, ntab), _norm(" ".join(q)), None, opts,
                selected=sel, multi=multi, checked=checked, free_text_index=free)
    p["last_tab"] = last  # 복수 선택 답의 확정 키(마지막 질문이면 enter가 제출)
    return p


def _grok_dialog_group(text):
    """여러 질문 상자(`[k/N] … ←/→ question`)면 묶음 키, 아니면 None. 질문 문장(hc msg)이 탭마다 바뀌어도 같은 상자다."""
    g = _grok_gutter(screen_lines(text))
    nv = _grok_q_nav(g[1]) if g else None
    if not nv or nv[0].group(1) is None:
        return None
    return ("grok-questions", int(nv[0].group(2)))


def _parse_grok_plan(lines):
    nz = [i for i, ln in enumerate(lines) if ln.strip()]
    if len(nz) < 6 or _norm(lines[nz[-1]]) != GROK_PLAN_HINT:
        return None
    wait = next((i for i in reversed(nz[-20:-1]) if lines[i].strip() == GROK_PLAN_WAIT), -1)
    if wait < 0:
        return None
    above = [i for i in nz if i < wait]
    if len(above) < 4 or not lines[above[-1]].strip().startswith("╰"):
        return None
    menu, sep = above[-2], above[-3]
    if _norm(lines[menu].strip().lstrip("│").rstrip("│")) != GROK_PLAN_MENU \
            or not lines[sep].strip().startswith("│─"):
        return None
    top = next((i for i in range(sep - 1, -1, -1) if lines[i].strip().startswith(GROK_PLAN_TOP)), -1)
    if top < 0:
        return None
    out = []
    for ln in lines[top + 1:sep]:
        s = ln.strip()
        if not s.startswith("│"):
            return None
        s = s[1:].rstrip("│█ ")
        m = GROK_PLAN_LINE_RE.match(s)
        if m:
            out.append(m.group(2) or "")
        elif s.strip():
            out.append(s.strip())  # 줄 번호 없는 줄(긴 줄이 접힌 것)
    body = "\n".join(out).strip()
    if not body:
        return None
    opts = [("Approve", None, None), ("Request changes", None, None), ("Quit plan", None, None)]
    # q는 계획을 버리고 plan 모드를 끈다(실측). 실행은 안 된다. 계획을 이어 가려면 수정 요청(직접 입력)
    return _finish("plan", "plan.md", GROK_PLAN_Q, body[:MAX_PROMPT_BODY], opts, free_text_index=1, reject_index=2)


def _parse_grok_trust(lines):
    nz = [i for i, ln in enumerate(lines) if ln.strip()]
    if len(nz) < 4 or not GROK_FOOTER_RE.match(lines[nz[-1]].strip()):
        return None
    r1, r2 = (GROK_TRUST_ROW_RE.match(lines[i].strip()) for i in nz[-3:-1])
    if not r1 or not r2 or (r1.group(1), r1.group(2), r2.group(1), r2.group(2)) != ("Yes, proceed", "y", "No, quit", "n"):
        return None
    q = next((i for i in reversed(nz[:-3]) if lines[i].strip() == GROK_TRUST_Q), -1)
    if q < 0 or nz.index(q) < len(nz) - 10:
        return None
    mid = [lines[i].strip() for i in nz if q < i < nz[-3]]
    if not mid:
        return None
    body = mid[0] + ("\n\n" + " ".join(mid[1:]) if mid[1:] else "")
    p = _finish("approval", None, GROK_TRUST_Q, body, [("Yes, proceed", None, None), ("No, quit", None, None)],
                reject_index=1)
    p["approve_index"] = 0  # $GROK_HOME/trusted_folders.toml에 저장되지만 신뢰 화면의 기본 답이다. n은 grok을 끝낸다
    return p


def _parse_grok_lines(lines, plan_reader=None):
    p = None
    g = _grok_gutter(lines)
    if g:
        hint, inner, top = g
        p = _parse_grok_approval(hint, inner, lines, top) or _parse_grok_question(hint, inner)
    if p is None:
        p = _parse_grok_plan(lines) or _parse_grok_trust(lines)
    if p:
        p["agent"] = "grok"
    return p


def _pick_grok(p, option_index):
    """번호 없는 항목(계획 검토 a/q, 폴더 신뢰 y/n)은 글자 키. 번호 있는 항목은 기본(숫자 키 = 고르고 제출)."""
    o = p["options"][option_index]
    if o.get("number") is not None:
        return None
    k = GROK_LETTER_KEYS.get((p["kind"], o["label"]))
    return [("keys", [k])] if k else None


def _multi_grok(p, num, want):
    """복수 선택: 숫자 키 = 토글. 고른 게 있으면 enter(다음 질문, 마지막이면 제출), 없으면 →(다음 질문).
    마지막 질문에서 빈 답은 enter가 커서 줄을 골라 제출해 버린다(실측 1.0.77): 400."""
    if any(num(i) > 9 for i in want.symmetric_difference(set(p["checked"]))):  # 누를 숫자 키는 한 자리만
        raise ApiError(400, "bad_option")
    keys = [str(num(i)) for i in sorted(want.symmetric_difference(set(p["checked"])))]
    if want:
        return [("keys", keys + ["enter"])]
    if p.get("last_tab", True):
        raise ApiError(400, "bad_option")
    return [("keys", keys + ["right"])]


def _reject_keys_grok(p):
    """승인 카드의 거절 버튼: "No, reject" 번호(숫자 키 = 고르고 제출, 턴이 끝난다. 실측). Esc는 거절이 아니다."""
    row = next((o for o in p["options"] if o["label"] == GROK_REJECT and o.get("number")), None)
    if p["kind"] != "approval" or row is None:
        raise ApiError(409, "needs_manual", prompt=p)
    return [str(row["number"])]


def _free_text_grok(p, num, text):
    """Grok(실측 1.0.77): 승인 거절 메모는 커서를 "No, reject" 줄로 옮기고 글자 하나(x)로 입력 칸을 연 뒤 지우고
    붙여넣기+enter. 계획 수정 요청은 s(입력창으로) → 붙여넣기+enter. 옵션 있는 질문은 z(입력 칸으로, 글자로 안
    들어감) → 붙여넣기+enter. 옵션 없는 질문은 커서가 이미 z 줄이라 z가 글자로 들어간다: enter(편집 시작) → 붙여넣기+enter."""
    if p["kind"] == "approval":
        keys = nav_keys(None if p.get("selected") is None else num(p["selected"]), num(p["free_text_index"]))
        if keys is None:
            raise ApiError(409, "no_cursor")
        return [("keys", keys + ["x", "backspace"]), ("text", text)]
    if p["kind"] == "plan":
        return [("keys", ["s"]), ("text", text)]
    if p["kind"] == "question":
        return [("keys", ["enter" if len(p["options"]) == 1 else "z"]), ("text", text)]
    raise ApiError(400, "no_free_text")


def is_cursor(info):
    """Cursor Agent CLI 터미널. 앞 프로그램 `~/.local/bin/cursor-agent`(node 실행 파일, 실측 2026.11.19), hc agent
    "cursor-agent", 또는 `node …/cursor-agent/versions/<버전>/index.js`. 같은 것인 `agent` 이름은 다른 CLI(Grok 등)와
    겹쳐 보지 않는다."""
    if _agent_names(info) & {"cursor-agent"}:
        return True
    fg = (info or {}).get("foreground")
    argv = fg.get("argv") if isinstance(fg, dict) else None
    if isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv) \
            and _binary_base(argv[0]) in SCRIPT_HOSTS:
        script = next((a for a in argv[1:] if not a.startswith("-")), "")
        return "/cursor-agent/versions/" in script.replace("\\", "/")
    return False


# ---- Cursor Agent CLI. 실측: cursor-agent 2026.11.19-f005e9d, 200열(tests/fixtures/cursor-*-2026.11.19.txt).
# OSC 7501을 안 보내고 제목은 AI가 붙인 대화 제목으로 고정이라 막힘·작업 중은 화면으로만 안다(screen "full").
# 셸 승인(`Run this command?`)·작업 폴더 밖 파일 쓰기 승인(`Write to this file?`)·모드 전환 승인(`Switch to … mode?`)은
# 화면 맨 아래 `→ 라벨 (키)` 줄들이다. 숫자 표시가 없고 괄호 안 글자 키가 바로 고른다: y 실행, tab 허용 목록 추가
# (전역 ~/.cursor/cli-config.json permissions.allow에 영구 저장 → persist만), esc/n 거절 → "대신 할 말" 입력칸
# (붙여넣기+enter를 모델이 "Rejected: <글>"로 받는다). "Run Everything"(shift+tab)은 저장 방식을 모르고 키를 안 써 봐
# 카드에서 뺀다. 계획 승인(`Ready to build?`)은 박스 안 번호 줄이고 숫자 키가 바로 제출한다(3은 수정 입력칸, 빈 칸
# enter는 무시, esc는 메뉴로: 글 없는 거절이 없다).
# 폴더 신뢰는 `[a] Trust this workspace / [q] Quit` 박스(a 신뢰, q 종료).
# 질문 도구(AskQuestion, grok-4.7 모델에서만 뜬다)는 화면 맨 아래 `┌ … └` 박스: 제목, `Question N of M`(탭 막대 없음, 한 번에
# 한 질문), `N. 질문 (multi-select)`, `› [ ] 항목`(단일 선택도 체크칸, 커서 ›), 끝에 늘 `[ ] Other: (type to answer)`,
# 힌트 `↑/↓ option · ←/→ question · Space select · Enter next/submit · Esc to skip`. 키(실측): 숫자 무시. enter가 커서 항목을
# 고르고 다음 질문(마지막이면 제출): 단일 선택은 ↑/↓ + enter. Other는 커서 → 붙여넣기+enter. 복수 선택은 ↑/↓ + space 토글,
# tab = 다음 질문. 마지막 질문의 복수 선택은 enter 한 번에 제출이 안 된 적이 있어(원인 미확인) 카드 없음. esc 건너뛰기는
# 실측 못 해 거절 버튼 없음. PC에서 Other에 글을 넣었으면 카드 없음. 계정 한도로 카드 답은 실제로 확인 못 했다(fixture만).
CURSOR_ROW_RE = re.compile(r"^(→\s+)?(\S.*?) \(([^()]+)\)$")
CURSOR_SHELL_Q = "Run this command?"
CURSOR_WRITE_Q = "Write to this file?"
CURSOR_SWITCH_Q_RE = re.compile(r"^Switch to (\S.*) mode\?$")
CURSOR_MENU_HINTS = ("↑/↓ to navigate • Enter to select • auto-rejects when the bar runs out",
                     "ctrl+r to review changed files")
CURSOR_RUN_EVERYTHING = "Run Everything"
CURSOR_SHELL_LABELS = (re.compile(r"^Run \(once\)$"), re.compile(r"^Add Shell\(.+\) to allowlist\?$"),
                       re.compile(r"^Run Everything$"), re.compile(r"^Skip & tell the agent what to do instead$"))
CURSOR_WRITE_LABELS = (re.compile(r"^Proceed$"), re.compile(r"^Reject & propose changes$"),
                       re.compile(r"^Add Write\(.+\) to allowlist\?$"), re.compile(r"^Run Everything$"))
CURSOR_SWITCH_LABELS = (re.compile(r"^Approve mode switch$"), re.compile(r"^Reject$"))
CURSOR_WRITE_IN_RE = re.compile(r"^in (\S.*)$")
CURSOR_PLAN_Q = "Ready to build?"
CURSOR_PLAN_HEAD = "Suggested Plan"
CURSOR_PLAN_ROW_RE = re.compile(r"^(→\s+)?([1-9])\.\s+(\S.*?) \(([^()]+)\)$")
CURSOR_PLAN_SAVED_RE = re.compile(r"^Saved to (\S.*)$")
CURSOR_PLAN_REVISE = "No, propose changes"
CURSOR_TRUST_HEAD = "⚠ Workspace Trust Required"
CURSOR_TRUST_Q = "Do you trust the contents of this directory?"
CURSOR_TRUST_ROW_RE = re.compile(r"^(▶\s+)?\[([a-z])\] (\S.*)$")
CURSOR_TRUST_HINT = "Use arrow keys to navigate, Enter to select, or press the key shown"
CURSOR_TRUST_MSG_RE = re.compile(r"^approve Workspace Trust Required$")  # 신뢰 카드의 막힘 msg(_card_status)
CURSOR_Q_HINT = "↑/↓ option · ←/→ question · Space select · Enter next/submit · Esc to skip"
CURSOR_Q_COUNT_RE = re.compile(r"^Question (\d+) of (\d+)$")
CURSOR_Q_ROW_RE = re.compile(r"^(›\s+)?\[([ x])\] (\S.*)$")
CURSOR_Q_MULTI = " (multi-select)"
CURSOR_OTHER = "Other: (type to answer)"
# 작업 중: 입력줄 오른쪽 "ctrl+c to stop", 또는 그 위 스피너 줄 "⠀⠞ Working"
CURSOR_BUSY_RE = re.compile(r"ctrl\+c to stop$|^\s*[⠀-⣿]+\s+Working\b")
# 카드를 못 읽어도 막힘: 거절 뒤 "대신 할 말"·계획 수정 입력칸, 모드 전환·신뢰 힌트, 메뉴 줄의 거절·Run Everything 키
CURSOR_DIALOG_RE = re.compile(
    r"→ Tell the agent what to do instead \(|→ Describe how to revise the plan \("
    r"|auto-rejects when the bar runs out|Use arrow keys to navigate, Enter to select, or press the key shown"
    r"|Enter next/submit · Esc to skip"
    r"|\((?:esc or n(?: or p)?|n or esc|p or Esc|shift\+tab)\)")


def _cursor_key(spec):
    """괄호 안 키 설명("y", "tab", "esc or n", "n or esc") → 보낼 키 하나(글자 우선). 모르면 None."""
    toks = [t.strip() for t in spec.split(" or ")]
    for t in toks:
        if len(t) == 1 and t.isalpha():
            return t.lower()
    return "tab" if toks == ["tab"] else None


def _cursor_body_above(lines, qi):
    """질문 줄 위, 가장 가까운 가로줄 아래의 줄들(명령·파일 미리보기)과 그 가로줄 index."""
    for r in range(qi - 1, max(-1, qi - 80), -1):
        if SOLID_RULE_RE.match(lines[r].strip()):
            return lines[r + 1:qi], r
    return None, -1


def _parse_cursor_approval(lines):
    nz = [i for i, ln in enumerate(lines) if ln.strip()]
    if nz and lines[nz[-1]].strip() in CURSOR_MENU_HINTS:
        nz = nz[:-1]
    rows = []
    for i in reversed(nz):
        m = CURSOR_ROW_RE.match(lines[i].strip())
        if not m:
            break
        rows.insert(0, (i, m))
    if len(rows) < 2 or rows[-1][0] - rows[0][0] != len(rows) - 1:
        return None
    first = rows[0][0]
    qi = next((j for j in range(first - 1, max(-1, first - 6), -1)
               if lines[j].strip() in (CURSOR_SHELL_Q, CURSOR_WRITE_Q) or CURSOR_SWITCH_Q_RE.match(lines[j].strip())),
              -1)
    if qi < 0 or any(not lines[j].strip() for j in range(qi + 1, first)):
        return None
    question = lines[qi].strip()
    info = [lines[j].strip() for j in range(qi + 1, first)]
    labels = [_norm(m.group(2)) for _, m in rows]
    keys = [_cursor_key(m.group(3)) for _, m in rows]
    want = CURSOR_SHELL_LABELS if question == CURSOR_SHELL_Q else \
        CURSOR_WRITE_LABELS if question == CURSOR_WRITE_Q else CURSOR_SWITCH_LABELS
    if len(labels) != len(want) or any(not rx.match(lb) for rx, lb in zip(want, labels)):
        return None  # 캡처하지 않은 모양(항목이 다르다)
    keep = [k for k, lb in enumerate(labels) if lb != CURSOR_RUN_EVERYTHING]
    if any(keys[k] is None for k in keep):
        return None
    selected = next((n for n, k in enumerate(keep) if rows[k][1].group(1)), None)
    opts = [(labels[k], None, None) for k in keep]
    okeys = [keys[k] for k in keep]
    if question == CURSOR_SHELL_Q:
        above, _ = _cursor_body_above(lines, qi)
        cmd = _dedent_block(above or [])
        if not cmd or not cmd.startswith("$"):
            return None
        body = re.sub(r"^\$\s+", "", cmd) + ("\n\n" + "\n".join(info) if info else "")
        # 거절 줄은 입력 항목: 고르면 "대신 할 말" 입력칸이 열린다(직접 입력). 거절 버튼은 reject_keys
        p = _finish("approval", "Shell", question, body, opts, selected=selected, free_text_index=2, persist_index=1)
    elif question == CURSOR_WRITE_Q:
        m = CURSOR_WRITE_IN_RE.match(info[0]) if len(info) == 1 else None
        if not m:
            return None
        above, rule = _cursor_body_above(lines, qi)
        pv = _dedent_block(above or [])
        body = m.group(1) + ("\n\n" + pv if pv and rule >= 0 else "")
        p = _finish("approval", "Write", question, body[:MAX_PROMPT_BODY], opts, selected=selected,
                    free_text_index=1, persist_index=2)
    else:
        # 모드 전환(SwitchMode 도구): 줄어드는 바가 다 되면 자동 거절. y 승인, n 거절
        p = _finish("approval", "SwitchMode", question, " ".join(info) or question, opts, selected=selected,
                    reject_index=1)
    p["approve_index"] = 0
    p["option_keys"] = okeys
    return p


def _cursor_box(lines, bottom, side):
    """화면 맨 아래 박스(맨 끝 줄이 bottom 글자로 시작)의 안쪽 줄들. 맨 위 테두리가 화면 밖이면 보이는 데까지."""
    nz = [i for i, ln in enumerate(lines) if ln.strip()]
    if not nz or not lines[nz[-1]].strip().startswith(bottom):
        return None
    inner = []
    i = nz[-1] - 1
    while i >= 0 and lines[i].strip().startswith(side):
        s = lines[i].strip()[1:]
        inner.append((s[:-1] if s.endswith(side) else s).rstrip())
        i -= 1
    inner.reverse()
    return inner or None


def _parse_cursor_plan(lines):
    inner = _cursor_box(lines, "└", "│")
    if not inner:
        return None
    nz = [k for k, s in enumerate(inner) if s.strip()]
    qi = next((k for k in nz if inner[k].strip() == CURSOR_PLAN_Q), -1)
    if qi < 0:
        return None
    rows = [CURSOR_PLAN_ROW_RE.match(inner[k].strip()) for k in nz if k > qi]
    if not rows or any(m is None for m in rows) or [int(m.group(2)) for m in rows] != list(range(1, len(rows) + 1)):
        return None
    labels = [_norm(m.group(3)) for m in rows]
    if labels[-1] != CURSOR_PLAN_REVISE or not all(lb.startswith("Yes, ") for lb in labels[:-1]):
        return None
    before = [k for k in nz if k < qi]
    if len(before) < 2 or not SOLID_RULE_RE.match(inner[before[-1]].strip()):
        return None
    saved = CURSOR_PLAN_SAVED_RE.match(inner[before[-2]].strip())
    if not saved:
        return None
    head = next((k for k in nz if inner[k].strip() == CURSOR_PLAN_HEAD), -1)
    body = _dedent_block([s for s in inner[head + 1:before[-2]] if not SOLID_RULE_RE.match(s.strip())])
    if not body:
        return None
    # 숫자 키가 그 항목을 바로 제출한다(실측). 3은 "어떻게 고칠지" 입력칸을 연다(직접 입력)
    return _finish("plan", saved.group(1).rsplit("/", 1)[-1], CURSOR_PLAN_Q, body[:MAX_PROMPT_BODY],
                   [(lb, None, int(m.group(2))) for lb, m in zip(labels, rows)],
                   selected=next((k for k, m in enumerate(rows) if m.group(1)), None),
                   free_text_index=len(rows) - 1)


def _parse_cursor_trust(lines):
    inner = _cursor_box(lines, "╰", "│")
    if not inner:
        return None
    nz = [inner[k].strip() for k, s in enumerate(inner) if s.strip()]
    if len(nz) < 6 or nz[0] != CURSOR_TRUST_HEAD or nz[-1] != CURSOR_TRUST_HINT:
        return None
    r1, r2 = (CURSOR_TRUST_ROW_RE.match(s) for s in nz[-3:-1])
    if not r1 or not r2 or (r1.group(2), r1.group(3), r2.group(2), r2.group(3)) != (
            "a", "Trust this workspace", "q", "Quit"):
        return None
    if CURSOR_TRUST_Q not in nz:
        return None
    path = nz[nz.index(CURSOR_TRUST_Q) + 1:-3]
    if len(path) != 1:
        return None
    p = _finish("approval", CURSOR_TRUST_HEAD[2:], CURSOR_TRUST_Q, path[0],
                [("Trust this workspace", None, None), ("Quit", None, None)],
                selected=0 if r1.group(1) else 1 if r2.group(1) else None, reject_index=1)
    # 신뢰는 ~/.cursor/projects/<폴더>/.workspace-trusted에 저장되지만 이 화면의 기본 답이다. q는 cursor-agent를 끝낸다
    p["approve_index"] = 0
    p["option_keys"] = ["a", "q"]
    return p


def _cursor_question_box(lines):
    """질문 박스 → (제목, 질문 번호, 질문 수, 안쪽 줄들, 힌트 줄 index) 또는 None."""
    inner = _cursor_box(lines, "└", "│")
    nz = [k for k, s in enumerate(inner or []) if s.strip()]
    if len(nz) < 4 or inner[nz[-1]].strip() != CURSOR_Q_HINT:
        return None
    m = CURSOR_Q_COUNT_RE.match(inner[nz[1]].strip())
    if not m or not 1 <= int(m.group(1)) <= int(m.group(2)):
        return None
    return inner[nz[0]].strip(), int(m.group(1)), int(m.group(2)), inner, nz


def _parse_cursor_question(lines):
    box = _cursor_question_box(lines)
    if not box:
        return None
    title, n, total, inner, nz = box
    body = nz[2:-1]
    ri = next((j for j, k in enumerate(body) if CURSOR_Q_ROW_RE.match(inner[k].strip())), None)
    if ri is None or ri == 0:
        return None
    qlines, rlines = body[:ri], body[ri:]
    if rlines[-1] - rlines[0] != len(rlines) - 1:
        return None  # 항목 사이에 다른 줄(설명 등): 캡처하지 않은 모양
    rows = [CURSOR_Q_ROW_RE.match(inner[k].strip()) for k in rlines]
    if any(r is None for r in rows):
        return None
    q = _norm(" ".join(inner[k].strip() for k in qlines))
    prefix = "%d. " % n
    if not q.startswith(prefix):
        return None
    q = q[len(prefix):]
    multi = q.endswith(CURSOR_Q_MULTI)
    if multi:
        q = q[:-len(CURSOR_Q_MULTI)].strip()
    labels = [_norm(r.group(3)) for r in rows]
    if not q or len(labels) < 2 or labels[-1] != CURSOR_OTHER or rows[-1].group(2) != " ":
        return None  # PC에서 Other에 글을 넣었다(`[x] Other: duckdb`)
    cur = [k for k, r in enumerate(rows) if r.group(1)]
    if len(cur) != 1:
        return None
    checked = [k for k, r in enumerate(rows[:-1]) if r.group(2) == "x"]
    last = n == total
    if multi:
        if last:
            return None  # 마지막 질문의 복수 선택 enter는 한 번에 제출이 안 된 적이 있다(원인 미확인)
        opts, free = [(lb, None, None) for lb in labels[:-1]], None
    else:
        if len(checked) > 1:
            return None
        # 다른 항목에 [x]가 있을 때 Other에 글을 넣으면 어느 쪽이 가는지 모른다: 직접 입력(Other)은 빈 폼에서만
        opts = [(lb, None, None) for lb in labels[:-1]] + ([] if checked else [("Other", None, None)])
        free = None if checked else len(opts) - 1
        checked = []
    p = _finish("question", "%s %d/%d" % (title, n, total) if total > 1 else title, q, None, opts,
                selected=cur[0], multi=multi, checked=checked, free_text_index=free)
    p["last_tab"] = last
    return p


def _cursor_dialog_group(text):
    """여러 질문 박스(`Question N of M`, M > 1)면 묶음 키(제목, 질문 수), 아니면 None."""
    box = _cursor_question_box(screen_lines(text))
    if not box or box[2] < 2:
        return None
    return ("cursor-questions", box[0], box[2])


def _parse_cursor_lines(lines, plan_reader=None):
    p = _parse_cursor_approval(lines) or _parse_cursor_plan(lines) or _parse_cursor_trust(lines) \
        or _parse_cursor_question(lines)
    if p:
        p["agent"] = "cursor"
    return p


def _pick_cursor(p, option_index):
    """승인·신뢰 항목은 괄호 안 글자 키(바로 고른다). 계획은 기본(숫자 키 = 고르고 제출)."""
    keys = p.get("option_keys")
    if p["kind"] != "approval" or not keys or not keys[option_index]:
        return None
    return [("keys", [keys[option_index]])]


def _reject_keys_cursor(p):
    """reject_index가 없는 카드의 거절: 승인은 거절 줄 키(n) → 빈 "대신 할 말" 칸에 enter(그냥 거절, 실측).
    계획은 글 없는 거절이 없다(3 → 빈 칸 enter는 아무 일도 없고 esc는 메뉴로 돌아온다, 실측): 앱에서 수정 글로."""
    fi = p.get("free_text_index")
    if p["kind"] != "approval" or fi is None:
        raise ApiError(409, "needs_manual", prompt=p)
    return [p["option_keys"][fi], "enter"]


def _free_text_cursor(p, num, text):
    """승인 거절 메모: 거절 줄 키(n)로 "대신 할 말" 입력칸을 연 뒤 붙여넣기+enter(모델이 "Rejected: <글>"로 받는다, 실측).
    계획 수정: 3 → 붙여넣기+enter(실측)."""
    if p["kind"] == "approval":
        return [("keys", [p["option_keys"][p["free_text_index"]]]), ("text", text)]
    if p["kind"] == "question":
        # 질문: 커서를 "Other"로 옮기면 글자가 그 칸에 들어간다. 붙여넣기+enter가 다음 질문(마지막이면 제출, 실측)
        keys = nav_keys(p.get("selected"), p["free_text_index"])
        if keys is None:
            raise ApiError(409, "no_cursor")
        return ([("keys", keys)] if keys else []) + [("text", text)]
    return _free_text_digit(p, num, text)


def _multi_cursor(p, num, want):
    """복수 선택(마지막 질문이 아닐 때만 카드): 숫자 키 무시. ↑/↓로 항목마다 가서 space 토글, tab = 다음 질문(실측).
    enter는 커서 항목까지 골라 버려 쓰지 않는다."""
    cur = p.get("selected")
    if cur is None or p.get("last_tab", True):
        raise ApiError(409, "no_cursor")
    keys = []
    for i in sorted(want.symmetric_difference(set(p["checked"]))):
        keys += nav_keys(cur, i) + ["space"]
        cur = i
    return [("keys", keys + ["tab"])]


def _cursor_status_approve(p):
    """카드 없이(prompt_id 없이) 승인: 지금 화면 카드의 승인 키. 카드를 못 읽으면 None(앱에서 직접)."""
    if not p or p["kind"] != "approval" or p.get("approve_index") is None:
        return None
    return [p["option_keys"][p["approve_index"]]]


def _cursor_status_reject(p):
    if not p or p["kind"] != "approval":
        return None
    if p.get("reject_index") is not None:
        return [s for _, v in answer_steps(p, option_index=p["reject_index"]) for s in v]
    try:
        return _reject_keys_cursor(p)
    except ApiError:
        return None


# ---- Amp CLI. 실측: amp 0.0.1795233642-g42d75f, 200열(tests/fixtures/amp-*-0.0.1795233642.txt).
# OSC 7501로 막힘을 보내지만 kind가 없다: 승인·질문 모두 {"msg": "Input needed", "state": "blocked"}. 종류는 화면 상자
# 제목으로 가른다. 대화 상자는 입력 상자 위 `╭─ 제목 ─╮` 상자이고 아래 테두리에 힌트가 있다(그 아래 "Up next (N)" 요약
# 줄과 입력 상자만 올 수 있다). 메뉴에 번호가 없고 숫자 키는 아무것도 안 한다: 커서(‣, 늘 0에서 시작) ↓/↑ + enter.
# 승인: Allow Once / Reject with feedback(입력칸 "Tell Amp what to do differently") / Allow All for This Session(메모리만,
# 파일 변화 없음) / Allow All for Every Session(settings.json에 "amp.dangerouslyAllowAll": true를 써서 모든 도구가 영구
# 무승인이 된다: 카드에서 뺀다). Esc는 거절(도구 결과가 거절이 되고 모델이 이어 간다). 셸(`Bash:`)·패치(`Apply Patch:`)만
# 캡처했다. 질문(ask_user_choice `Choose an option`)은 단일 선택, 직접 입력 없음, Esc는 고르지 않고 닫기. 계획 모드 없음.
AMP_DIALOG_HINT_RE = re.compile(r"^╰─+ (↑/↓/j/k move · Enter select(?: · Ctrl\+E/Ctrl\+Y scroll)? · Esc cancel) ─╯$")
AMP_TOP_RE = re.compile(r"^╭─ (\S.*?) ─+╮?$")
AMP_UP_NEXT_RE = re.compile(r"^  (?:Up next \(\d+\)|\d+\. \S.*)$")
AMP_SCROLLBAR_RE = re.compile(r"\s+[▀-▟]$")
AMP_APPROVAL_HEAD = "Approval Required"
AMP_QUESTION_HEAD = "Choose an option"
AMP_APPROVAL_TOOLS = {"Bash:": "Bash", "Apply Patch:": "Apply Patch"}
AMP_RULE_RE = re.compile(r"^Matches (?:built-in|user) permissions rule \d+: ")
AMP_APPROVAL_LABELS = ("Allow Once", "Reject with feedback", "Allow All for This Session",
                       "Allow All for Every Session")
AMP_ROW_RE = re.compile(r"^(‣| ) (\S.*)$")
AMP_FEEDBACK_WAIT = 0.5  # 피드백 입력칸이 그려지는 시간(실측 0.1~0.2초)


def _amp_dialog(lines):
    """화면 끝의 활성 Amp 대화 상자 → (제목, 안쪽 줄들). 아래 테두리 힌트 뒤에 "Up next" 요약 줄과 입력 상자(╭│╰ 줄)만
    있어야 한다(대화가 그 아래로 이어지면 지난 상자다). 없으면 None."""
    nz = [i for i, ln in enumerate(lines) if ln.strip()]
    bottom = None
    for i in reversed(nz):
        s = lines[i]
        if AMP_DIALOG_HINT_RE.match(s):
            bottom = i
            break
        if not (s.startswith(("╭", "│", "╰")) or AMP_UP_NEXT_RE.match(s)):
            return None
    if bottom is None:
        return None
    after = [lines[i] for i in nz if i > bottom]
    if not after or not after[-1].startswith("╰") or AMP_DIALOG_HINT_RE.match(after[-1]):
        return None  # 입력 상자가 화면 맨 아래에 있어야 한다
    top = next((i for i in range(bottom - 1, max(-1, bottom - 400), -1) if lines[i].startswith("╭")), -1)
    m = AMP_TOP_RE.match(lines[top]) if top >= 0 else None
    if not m:
        return None
    inner = []
    for ln in lines[top + 1:bottom]:
        u = _box_unwrap(ln)
        if u is None:
            return None
        inner.append(AMP_SCROLLBAR_RE.sub("", u).rstrip())
    return m.group(1), inner


def _amp_rows(inner):
    """메뉴 줄(`‣ 라벨` / `  라벨`)은 상자 안 마지막 글자 줄들이다. → (첫 메뉴 줄 index, 라벨들, 커서 index)."""
    idx = [i for i, ln in enumerate(inner) if ln.strip()]
    rows = []
    for i in reversed(idx):
        m = AMP_ROW_RE.match(inner[i])
        if not m or (rows and i != rows[0][0] - 1):
            break
        rows.insert(0, (i, m.group(1) == "‣", m.group(2).strip()))
    if not rows or sum(1 for r in rows if r[1]) != 1:
        return None
    return rows[0][0], [r[2] for r in rows], next(k for k, r in enumerate(rows) if r[1])


def _parse_amp_approval(inner):
    got = _amp_rows(inner)
    if not got:
        return None
    first, labels, cur = got
    if tuple(labels) != AMP_APPROVAL_LABELS:
        return None
    head = [i for i in range(first) if inner[i].strip()]
    if not head or inner[head[0]].strip() not in AMP_APPROVAL_TOOLS:
        return None  # 캡처하지 않은 도구(create_file·edit_file 등)
    title = AMP_APPROVAL_TOOLS[inner[head[0]].strip()]
    body = [ln for ln in inner[head[0] + 1:first] if not AMP_RULE_RE.match(ln.strip())]
    lead = next((k for k, ln in enumerate(body) if ln.strip()), None)
    if lead is not None and body[lead].strip() == "Patchtext:":
        body = body[lead + 1:]  # 패치 원문 위 인자 이름 줄
    body = _dedent_block(body)
    if not body:
        return None
    # "Allow All for Every Session"은 전역 영구 무승인 스위치라 카드에서 뺀다(마지막 줄이라 앞 항목의 커서 이동은 같다)
    p = _finish("approval", title, AMP_APPROVAL_HEAD, body, [(lb, None, None) for lb in labels[:3]],
                selected=cur if cur < 3 else None, free_text_index=1, always_index=2)
    p["approve_index"] = 0
    return p


def _parse_amp_question(inner):
    got = _amp_rows(inner)
    if not got:
        return None
    first, labels, cur = got
    head = [inner[i].strip() for i in range(first) if inner[i].strip()]
    if not head:
        return None
    return _finish("question", None, _norm(" ".join(head)), None, [(lb, None, None) for lb in labels], selected=cur)


def _parse_amp_lines(lines, plan_reader=None):
    got = _amp_dialog(lines)
    if not got:
        return None
    title, inner = got
    p = (_parse_amp_approval(inner) if title == AMP_APPROVAL_HEAD
         else _parse_amp_question(inner) if title == AMP_QUESTION_HEAD else None)
    if p:
        p["agent"] = "amp"
    return p


def _free_text_amp(p, num, text):
    """승인 거절 메모: 커서를 "Reject with feedback"으로 옮겨 enter(입력칸이 열린다) → 칸이 그려질 때까지 잠깐 →
    붙여넣기+enter(실측: 도구 결과가 "Tool rejected by plugin: <글>". 바로 붙여넣으면 글이 사라지고 빈 거절이 된다)."""
    if p["kind"] != "approval":
        raise ApiError(400, "no_free_text")
    keys = nav_keys(None if p.get("selected") is None else num(p["selected"]), num(p["free_text_index"]))
    if keys is None:
        raise ApiError(409, "no_cursor")
    return [("keys", keys + ["enter"]), ("wait", AMP_FEEDBACK_WAIT), ("text", text)]


def _amp_status_approve(p):
    """카드 없이(prompt_id 없이) 승인: 지금 화면 승인 카드의 Allow Once로 커서 이동 + enter. 카드가 없으면 None."""
    if not p or p["kind"] != "approval":
        return None
    try:
        return [s for _, v in answer_steps(p, option_index=p["approve_index"]) for s in v]
    except ApiError:
        return None


def _amp_status_reject(p):
    """카드 없이 거절: 승인·질문 카드가 보이면 esc(승인은 거절, 질문은 고르지 않고 닫기). 피드백 입력칸 등 카드가
    없으면 None(그 칸의 esc는 입력 취소라 거절이 아니다)."""
    return ["esc"] if p and p["kind"] in ("approval", "question") else None


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


def _free_text_copilot(p, num, text):
    """Copilot: 승인 3·계획 4(번호 있는 입력 항목)는 숫자 → 붙여넣기+enter. 질문(번호 없음)은 커서를 "Other"로 옮겨
    입력 칸을 연 뒤 붙여넣기+enter, 입력만 받는 질문은 바로 붙여넣기+enter(실측 1.0.114)."""
    fi = p["free_text_index"]
    if p["options"][fi].get("number") is not None:
        return _free_text_digit(p, num, text)
    if p.get("selected") is None:
        return [("text", text)]
    keys = nav_keys(num(p["selected"]), num(fi))
    return ([("keys", keys)] if keys else []) + [("text", text)]


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
        multi = agent_spec(p.get("agent")).get("multi")  # 에이전트만의 토글·확정 키(Qwen: 숫자+space, enter)
        if multi:  # 숫자 키 상한(9)은 숫자로 답하는 에이전트 함수가 정한다(Copilot·Cursor는 화살표라 상한 없음)
            return multi(p, num, want)
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
    pick = agent_spec(p.get("agent")).get("pick")  # 에이전트만의 키(OpenCode 승인 버튼)
    steps = pick(p, option_index) if pick else None
    if steps is not None:
        return steps
    # 화면에 번호가 없는 메뉴(Claude confirm, Copilot 질문: 숫자 키를 무시한다)는 커서 이동 + enter
    if kind == "confirm" or num(option_index) > 9 or p["options"][option_index].get("number") is None:
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


def _is_copilot_record(rec):
    """Copilot 이벤트 줄: type이 "user.message"처럼 점으로 나뉘고 data가 있다(Claude·Codex·Gemini 줄 type에는 점이 없다)."""
    typ = rec.get("type")
    return isinstance(typ, str) and "." in typ and isinstance(rec.get("data"), dict)


def _parse_copilot_transcript(recs, limit):
    """Copilot events.jsonl(실측 1.0.114): user.message(content, 빈 것은 Copilot이 넣은 재촉이라 숨김),
    assistant.message(reasoningText·content·toolRequests), tool.execution_complete(success, result.content·error.message)."""
    turns, tools = [], {}
    for rec in recs:
        typ, d = rec.get("type"), rec.get("data")
        if not isinstance(d, dict):
            continue
        if typ == "user.message":
            c = d.get("content")
            if isinstance(c, str) and c.strip():
                turns.append({"role": "user", "parts": [{"kind": "text", "text": _cap(c)}]})
        elif typ == "assistant.message":
            parts = []
            r = d.get("reasoningText")
            if isinstance(r, str) and r.strip():
                parts.append({"kind": "thinking", "text": _cap(r)})
            c = d.get("content")
            if isinstance(c, str) and c.strip():
                parts.append({"kind": "text", "text": _cap(c)})
            for tr in d.get("toolRequests") if isinstance(d.get("toolRequests"), list) else []:
                if not isinstance(tr, dict):
                    continue
                name = tr.get("name") if isinstance(tr.get("name"), str) else "?"
                inp = tr.get("arguments") if isinstance(tr.get("arguments"), dict) else {}
                t = {"kind": "tool", "name": name, "summary": _tool_summary(name, inp), "input": _cap_obj(inp),
                     "output": None, "state": "pending"}
                if isinstance(tr.get("toolCallId"), str):
                    tools[tr["toolCallId"]] = t
                parts.append(t)
            _codex_add(turns, "assistant", parts)
        elif typ == "tool.execution_complete":
            t = tools.get(d.get("toolCallId"))
            if t is None:
                continue
            ok = d.get("success") is True
            src = d.get("result") if ok else d.get("error")
            out = src.get("content" if ok else "message") if isinstance(src, dict) else None
            t["output"] = _cap(_strip_ansi(out)) if isinstance(out, str) else None
            t["state"] = "ok" if ok else "error"
    cut = len(turns) > limit
    return turns[-limit:] if limit else turns, cut


QWEN_HIDDEN_USER_PREFIXES = ("<system-reminder>", "<session_context>")


def _is_qwen_record(rec):
    """Qwen 줄: message.parts(Gemini API 모양) 또는 systemPayload(system 줄). Claude message에는 content만 있다."""
    msg = rec.get("message")
    return (isinstance(msg, dict) and isinstance(msg.get("parts"), list)) or "systemPayload" in rec \
        or rec.get("type") == "tool_result"


def _parse_qwen_transcript(recs, limit):
    """Qwen chats jsonl(실측 0.28.3): user(message.parts text), assistant(text·thought·functionCall),
    tool_result(functionResponse + toolCallResult.status success|cancelled|error), system은 무시."""
    turns, tools = [], {}
    for rec in recs:
        typ, msg = rec.get("type"), rec.get("message")
        parts_in = msg.get("parts") if isinstance(msg, dict) and isinstance(msg.get("parts"), list) else []
        if typ == "user":
            parts = []
            for pt in parts_in:
                t = pt.get("text") if isinstance(pt, dict) else None
                if isinstance(t, str) and t.strip() and not t.lstrip().startswith(QWEN_HIDDEN_USER_PREFIXES):
                    parts.append({"kind": "text", "text": _cap(t)})
            if parts:
                turns.append({"role": "user", "parts": parts})
        elif typ == "assistant":
            parts = []
            for pt in parts_in:
                if not isinstance(pt, dict):
                    continue
                fc = pt.get("functionCall")
                if isinstance(fc, dict):
                    name = fc.get("name") if isinstance(fc.get("name"), str) else "?"
                    inp = fc.get("args") if isinstance(fc.get("args"), dict) else {}
                    t = {"kind": "tool", "name": name, "summary": _tool_summary(name, inp), "input": _cap_obj(inp),
                         "output": None, "state": "pending"}
                    if isinstance(fc.get("id"), str):
                        tools[fc["id"]] = t
                    parts.append(t)
                elif isinstance(pt.get("text"), str) and pt["text"].strip():
                    parts.append({"kind": "thinking" if pt.get("thought") else "text", "text": _cap(pt["text"])})
            _codex_add(turns, "assistant", parts)
        elif typ == "tool_result":
            res = rec.get("toolCallResult") if isinstance(rec.get("toolCallResult"), dict) else {}
            for pt in parts_in:
                fr = pt.get("functionResponse") if isinstance(pt, dict) else None
                if not isinstance(fr, dict):
                    continue
                t = tools.get(fr.get("id")) or tools.get(res.get("callId"))
                if t is None:
                    continue
                resp = fr.get("response") if isinstance(fr.get("response"), dict) else {}
                v = resp.get("output", resp.get("error"))
                if v is not None and not isinstance(v, str):
                    v = json.dumps(v, ensure_ascii=False)
                t["output"] = _cap(_strip_ansi(v)) if isinstance(v, str) else None
                status = res.get("status")
                ok = status == "success" if status else "output" in resp
                t["state"] = "ok" if ok else "error"
    cut = len(turns) > limit
    return turns[-limit:] if limit else turns, cut


def _is_grok_record(rec):
    """Grok updates.jsonl 줄: ACP `session/update`(또는 `_x.ai/session/update`) 알림 그대로. 다른 기록에는 method가 없다."""
    return rec.get("method") in ("session/update", "_x.ai/session/update") and isinstance(rec.get("params"), dict)


def _grok_tool_output(u):
    raw = u.get("rawOutput")
    if isinstance(raw, dict) and isinstance(raw.get("output_for_prompt"), str):
        return raw["output_for_prompt"]
    texts = []
    for c in u.get("content") if isinstance(u.get("content"), list) else []:
        inner = c.get("content") if isinstance(c, dict) and c.get("type") == "content" else None
        if isinstance(inner, dict) and isinstance(inner.get("text"), str) and inner["text"]:
            texts.append(inner["text"])
    if texts:
        return "\n".join(texts)
    if raw is not None and not isinstance(raw, str):
        return json.dumps(raw, ensure_ascii=False)
    return raw


def _parse_grok_transcript(recs, limit):
    """Grok updates.jsonl(실측 1.0.77): user_message_chunk·agent_message_chunk·agent_thought_chunk(조각을 이어 붙인다),
    tool_call(title=도구 이름, rawInput), tool_call_update(status completed|failed, rawOutput·content).
    turn_completed·current_mode_update 등은 글을 끊기만 한다."""
    turns, tools = [], {}
    open_ = None  # (role, kind, 묶음 키), 이어 붙일 part

    for rec in recs:
        u = rec["params"].get("update")
        if not isinstance(u, dict):
            continue
        su = u.get("sessionUpdate")
        if su in ("user_message_chunk", "agent_message_chunk", "agent_thought_chunk"):
            c = u.get("content")
            t = c.get("text") if isinstance(c, dict) and c.get("type") == "text" else None
            if not isinstance(t, str) or not t:
                continue
            role = "user" if su == "user_message_chunk" else "assistant"
            kind = "thinking" if su == "agent_thought_chunk" else "text"
            meta = u.get("_meta") if isinstance(u.get("_meta"), dict) else {}
            key = (role, kind, meta.get("promptIndex") if role == "user" else None)
            if open_ and open_[0] == key:
                open_[1]["text"] = _cap(open_[1]["text"] + t)
                continue
            part = {"kind": kind, "text": _cap(t)}
            _codex_add(turns, role, [part])
            open_ = (key, part)
            continue
        open_ = None
        tid = u.get("toolCallId")
        if su == "tool_call":
            name = u.get("title") if isinstance(u.get("title"), str) else "?"
            inp = u.get("rawInput") if isinstance(u.get("rawInput"), dict) else {}
            t = {"kind": "tool", "name": name, "summary": _tool_summary(name, inp), "input": _cap_obj(inp),
                 "output": None, "state": "pending"}
            if isinstance(tid, str):
                tools[tid] = t
            _codex_add(turns, "assistant", [t])
        elif su == "tool_call_update" and u.get("status") in ("completed", "failed"):
            t = tools.get(tid)
            if t is None:
                continue
            v = _grok_tool_output(u)
            t["output"] = _cap(_strip_ansi(v)) if isinstance(v, str) else None
            raw = u.get("rawOutput") if isinstance(u.get("rawOutput"), dict) else {}
            code = raw.get("exit_code")
            bad = u["status"] == "failed" or (isinstance(code, int) and not isinstance(code, bool) and code != 0)
            t["state"] = "error" if bad else "ok"
    for turn in turns:
        turn["parts"] = [p for p in turn["parts"] if p["kind"] == "tool" or p["text"].strip()]
    turns = [t for t in turns if t["parts"]]
    cut = len(turns) > limit
    return turns[-limit:] if limit else turns, cut


def parse_transcript(lines, limit=50):
    """Claude Code 세션 jsonl(또는 Codex rollout jsonl, Gemini chats jsonl, Copilot events.jsonl,
    Qwen chats jsonl, Grok updates.jsonl) 줄들 → (turns, 잘렸는지)."""
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
    if any(_is_grok_record(r) for r in recs[:50]):
        return _parse_grok_transcript([r for r in recs if _is_grok_record(r)], limit)
    if any(_is_gemini_record(r) for r in recs[:50]):
        return _parse_gemini_transcript(recs, limit)
    if any(_is_copilot_record(r) for r in recs[:50]):
        return _parse_copilot_transcript(recs, limit)
    if any(_is_qwen_record(r) for r in recs[:50]):
        return _parse_qwen_transcript(recs, limit)
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


COPILOT_SCAN_SESSIONS = 50  # 잠금 파일로 못 찾았을 때 폴더를 비교해 볼 최근 세션 수
COPILOT_CWD_RE = re.compile(r"^cwd:\s*(.+?)\s*$", re.M)
COPILOT_LOCK_RE = re.compile(r"^inuse\.(\d{1,10})\.lock$")


def _child_pids(pid, runner):
    """바로 아래 자식 pid들(맥·리눅스 pgrep). 못 구하면 []."""
    exe = next((p for p in ("/usr/bin/pgrep", "/bin/pgrep") if os.path.exists(p)), None)
    if IS_WINDOWS or not exe:
        return []
    try:
        r = runner([exe, "-P", str(int(pid))], capture_output=True, text=True, timeout=HC_TIMEOUT, shell=False)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [int(x) for x in (r.stdout or "").split() if x.isdigit()]


def copilot_dir(home):
    env = os.environ.get("COPILOT_HOME")
    return os.path.expanduser(env) if env else os.path.join(home, ".copilot")


def _copilot_workspace_cwd(sdir):
    try:
        m = COPILOT_CWD_RE.search(read_regular_file(os.path.join(sdir, "workspace.yaml"), 64 * 1024)
                                  .decode("utf-8", "replace"))
    except OSError:
        return None
    if not m:
        return None
    v = m.group(1)
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        v = v[1:-1]
    return v


def find_copilot_transcript(info, home, runner=None):
    """Copilot 터미널 → (session_id, events.jsonl). 실측 1.0.114: ~/.copilot/session-state/<id>/events.jsonl.
    쓰는 동안 파일을 열어 두지 않는다. 살아 있는 세션 폴더에는 inuse.<pid>.lock이 있어 그 pid(또는 그 자식)의 세션을
    먼저 찾고, 없으면 workspace.yaml의 cwd가 같은 가장 최근 세션."""
    info = info or {}
    base = os.path.join(copilot_dir(home), "session-state")
    try:
        names = os.listdir(base)
    except OSError:
        return None, None
    entries = []
    for name in names:
        if not SESSION_ID_RE.match(name):
            continue
        try:
            st_ = os.stat(os.path.join(base, name, "events.jsonl"), follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISREG(st_.st_mode):
            entries.append((st_.st_mtime, name))
    entries.sort(reverse=True)
    locks = {}  # 잠금 pid → 세션
    for _, name in entries:
        try:
            for f in os.listdir(os.path.join(base, name)):
                m = COPILOT_LOCK_RE.match(f)
                if m:
                    locks.setdefault(int(m.group(1)), name)
        except OSError:
            continue
    if locks:
        fg = info.get("foreground")
        pids = {v for v in (info.get("pid"), fg.get("pid") if isinstance(fg, dict) else None)
                if isinstance(v, int) and not isinstance(v, bool) and v > 0}
        hit = next((locks[p] for p in pids if p in locks), None)
        if hit is None:
            # npm으로 깐 copilot은 node 런처가 플랫폼 바이너리를 자식으로 띄우고, 잠금은 자식 pid다(실측 1.0.114)
            kids = [c for p in pids for c in _child_pids(p, runner or subprocess.run)]
            hit = next((locks[c] for c in kids if c in locks), None)
        if hit is not None:
            return hit, os.path.join(base, hit, "events.jsonl")
    cwd = live_cwd(info)
    if not cwd:
        return None, None
    want = {cwd, os.path.realpath(cwd)}
    for _, name in entries[:COPILOT_SCAN_SESSIONS]:
        if _copilot_workspace_cwd(os.path.join(base, name)) in want:
            return name, os.path.join(base, name, "events.jsonl")
    return None, None


QWEN_CHAT_RE = re.compile(r"^([0-9A-Za-z-]{8,64})\.jsonl$")


def qwen_dir(home):
    env = os.environ.get("QWEN_HOME")
    return os.path.expanduser(env) if env else os.path.join(home, ".qwen")


def _qwen_project(cwd):
    """Qwen sanitizeCwd: 영숫자가 아닌 글자를 모두 '-'로(윈도우는 소문자로 먼저). 실측 0.28.3."""
    return re.sub(r"[^a-zA-Z0-9]", "-", cwd.lower() if IS_WINDOWS else cwd)


def _qwen_chats(base, cwd):
    out = []
    for c in (cwd, os.path.realpath(cwd)):
        d = os.path.join(base, "projects", _qwen_project(c), "chats")
        if d not in out:
            out.append(d)
    return out


def find_qwen_transcript(info, home, runner=None):
    """Qwen 터미널 → (session_id, 기록 경로). 실측 0.28.3: $QWEN_HOME(기본 ~/.qwen)/projects/<cwd 변환>/chats/<id>.jsonl.
    실행 중에는 $QWEN_HOME/sessions/<pid>.json(sessionId, cwd)이 지금 세션을 알려 준다(/clear 뒤 새 세션도).
    파일을 열어 두지 않는다. 세션 파일이 없거나 그 기록이 아직 없으면 그 폴더의 가장 최근 기록."""
    info = info or {}
    base = qwen_dir(home)
    fg = info.get("foreground")
    pids = [v for v in (info.get("pid"), fg.get("pid") if isinstance(fg, dict) else None)
            if isinstance(v, int) and not isinstance(v, bool) and v > 0]
    cwd = live_cwd(info)

    def session_of(pid):
        try:
            rec = json.loads(read_regular_file(os.path.join(base, "sessions", "%d.json" % pid), 64 * 1024)
                             .decode("utf-8", "replace"))
        except (OSError, ValueError):
            return None
        return rec if isinstance(rec, dict) else None
    sess = next((s for s in (session_of(p) for p in dict.fromkeys(pids)) if s), None)
    if sess is None and pids and os.path.isdir(os.path.join(base, "sessions")):
        # node 런처가 자기 자신을 자식으로 다시 띄우면 세션 파일은 자식 pid다(실측 0.28.3)
        kids = [c for p in dict.fromkeys(pids) for c in _child_pids(p, runner or subprocess.run)]
        sess = next((s for s in (session_of(c) for c in kids) if s), None)
    if sess is not None:
        sid, scwd = sess.get("sessionId"), sess.get("cwd")
        if isinstance(sid, str) and SESSION_ID_RE.match(sid) and isinstance(scwd, str) and scwd:
            for d in _qwen_chats(base, scwd):
                path = os.path.join(d, sid + ".jsonl")
                try:
                    if stat.S_ISREG(os.stat(path, follow_symlinks=False).st_mode):
                        return sid, path
                except OSError:
                    continue
        if isinstance(scwd, str) and scwd:
            cwd = scwd
    if not cwd:
        return None, None
    best = None
    for d in _qwen_chats(base, cwd):
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for f in names:
            m = QWEN_CHAT_RE.match(f)
            if not m:
                continue
            path = os.path.join(d, f)
            try:
                st_ = os.stat(path, follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISREG(st_.st_mode) and (best is None or st_.st_mtime > best[0]):
                best = (st_.st_mtime, path, m.group(1))
    if best is None:
        return None, None
    return best[2], best[1]


# OpenCode 기록은 세션별 파일이 아니라 SQLite 하나다(실측 1.19.6): $XDG_DATA_HOME/opencode/opencode.db
# (기본 ~/.local/share/opencode/opencode.db, WAL). session(id ses_…, directory=cwd, parent_id=하위 에이전트),
# message(id, session_id, time_created, data JSON: role…), part(id, message_id, session_id, data JSON: type text·reasoning·
# tool·step-start…). 실행 중인 OpenCode가 계속 열어 두므로 읽기 전용(mode=ro)으로만 연다.
OPENCODE_SESSION_RE = re.compile(r"^ses_[0-9A-Za-z]{8,64}$")
OPENCODE_MAX_MESSAGES = 1000  # 대화 보기에 읽을 최근 메시지 수(넘으면 truncated)
OPENCODE_PREVIEW_MESSAGES = 40
OPENCODE_DB_TIMEOUT = 1.0
OPENCODE_HIDDEN_USER_PREFIXES = ("<system-reminder>",)
_opencode_preview_cache = {}  # (db 경로, 세션) → ((db·wal 크기·시각), 미리보기)


def opencode_db(home):
    env = os.environ.get("XDG_DATA_HOME")
    base = os.path.expanduser(env) if env else os.path.join(home, ".local", "share")
    return os.path.join(base, "opencode", "opencode.db")


def _opencode_query(path, sql, args=()):
    """읽기 전용으로 열어 한 번 묻고 닫는다. 못 열거나 실패하면 None."""
    try:
        import pathlib
        import sqlite3
    except ImportError:  # _sqlite3 없이 빌드된 Python
        return None
    try:
        if not stat.S_ISREG(os.stat(path, follow_symlinks=False).st_mode):
            return None
        con = sqlite3.connect(pathlib.Path(os.path.abspath(path)).as_uri() + "?mode=ro", uri=True,
                              timeout=OPENCODE_DB_TIMEOUT, check_same_thread=False)
    except (OSError, ValueError, sqlite3.Error):
        return None
    try:
        return con.execute(sql, args).fetchall()
    except sqlite3.Error:
        return None
    finally:
        con.close()


def find_opencode_transcript(info, home, runner=None):
    """OpenCode 터미널 → (session_id, opencode.db 경로). 그 폴더(session.directory)에서 가장 최근에 바뀐 최상위 세션."""
    cwd = live_cwd(info or {})
    if not cwd:
        return None, None
    path = opencode_db(home)
    rows = _opencode_query(path, "SELECT id FROM session WHERE directory IN (?, ?) AND parent_id IS NULL "
                                 "ORDER BY time_updated DESC LIMIT 1", (cwd, os.path.realpath(cwd)))
    if not rows or not isinstance(rows[0][0], str) or not OPENCODE_SESSION_RE.match(rows[0][0]):
        return None, None
    return rows[0][0], path


def _opencode_tool_summary(name, inp):
    fp = inp.get("filePath")
    if isinstance(fp, str) and fp.strip():
        return _cap(_tilde(fp.strip()), 200)
    return _tool_summary(name, inp)


def _parse_opencode_transcript(items, limit):
    """[(message data, [part data…])] → (turns, 잘렸는지). user는 text(OpenCode가 넣은 synthetic·system-reminder 숨김),
    assistant는 text·reasoning·tool(state.status completed → output, error → error, 그 밖은 대기). 실측 1.19.6."""
    turns = []
    for msg, parts in items:
        role = msg.get("role") if isinstance(msg, dict) else None
        out = []
        for pt in parts:
            if not isinstance(pt, dict):
                continue
            typ, text = pt.get("type"), pt.get("text")
            if role == "user":
                if typ == "text" and isinstance(text, str) and text.strip() and not pt.get("synthetic") \
                        and not text.lstrip().startswith(OPENCODE_HIDDEN_USER_PREFIXES):
                    out.append({"kind": "text", "text": _cap(text)})
            elif typ in ("text", "reasoning"):
                if isinstance(text, str) and text.strip():
                    out.append({"kind": "text" if typ == "text" else "thinking", "text": _cap(text)})
            elif typ == "tool":
                name = pt.get("tool") if isinstance(pt.get("tool"), str) else "?"
                s = pt.get("state") if isinstance(pt.get("state"), dict) else {}
                inp = s.get("input") if isinstance(s.get("input"), dict) else {}
                status = s.get("status")
                res = s.get("output") if status == "completed" else s.get("error") if status == "error" else None
                out.append({"kind": "tool", "name": name, "summary": _opencode_tool_summary(name, inp),
                            "input": _cap_obj(inp), "output": _cap(_strip_ansi(res)) if isinstance(res, str) else None,
                            "state": {"completed": "ok", "error": "error"}.get(status, "pending")})
        if role == "user" and out:
            turns.append({"role": "user", "parts": out})
        elif role == "assistant":
            _codex_add(turns, "assistant", out)
    cut = len(turns) > limit
    return turns[-limit:] if limit else turns, cut


def read_opencode_transcript(path, sid, limit=50, max_messages=OPENCODE_MAX_MESSAGES):
    if not isinstance(sid, str) or not OPENCODE_SESSION_RE.match(sid):
        return [], False
    msgs = _opencode_query(path, "SELECT id, data FROM message WHERE session_id = ? "
                                 "ORDER BY time_created DESC, id DESC LIMIT ?", (sid, max_messages + 1))
    if not msgs:
        return [], False
    more = len(msgs) > max_messages
    msgs = list(reversed(msgs[:max_messages]))
    by_msg = {m[0]: [] for m in msgs}
    ids = list(by_msg)
    for k in range(0, len(ids), 500):
        chunk = ids[k:k + 500]
        rows = _opencode_query(path, "SELECT message_id, data FROM part WHERE session_id = ? AND message_id IN (%s) "
                                     "ORDER BY message_id, id" % ",".join("?" * len(chunk)), [sid] + chunk)
        for mid, data in rows or []:
            try:
                by_msg[mid].append(json.loads(data))
            except (ValueError, TypeError, KeyError):
                continue
    items = []
    for mid, data in msgs:
        try:
            items.append((json.loads(data), by_msg[mid]))
        except (ValueError, TypeError):
            continue
    turns, cut = _parse_opencode_transcript(items, limit)
    return turns, cut or more


def opencode_preview(path, sid):
    """목록 미리보기: 최근 메시지 몇 개만 읽어 마지막 assistant 글. db·wal이 안 바뀌었으면 기억한 값."""
    key = []
    for f in (path, path + "-wal"):
        try:
            st_ = os.stat(f, follow_symlinks=False)
            key.append((st_.st_mtime_ns, st_.st_size))
        except OSError:
            key.append(None)
    hit = _opencode_preview_cache.get((path, sid))
    if hit and hit[0] == key:
        return hit[1]
    turns, _ = read_opencode_transcript(path, sid, 0, max_messages=OPENCODE_PREVIEW_MESSAGES)
    pv = None
    for turn in reversed(turns):
        texts = [p["text"] for p in turn["parts"] if p.get("kind") == "text"] if turn["role"] == "assistant" else []
        if texts:
            pv = preview_clean(texts[-1])
            break
    if len(_opencode_preview_cache) > 200:
        _opencode_preview_cache.clear()
    _opencode_preview_cache[(path, sid)] = (key, pv)
    return pv


def grok_dir(home):
    env = os.environ.get("GROK_HOME")
    return os.path.expanduser(env) if env else os.path.join(home, ".grok")


def _grok_cwd_dirs(base, cwd):
    """그 폴더의 세션 폴더들: $GROK_HOME/sessions/<URL 인코딩 cwd>(실측 1.0.77: `%2FUsers%2Fme%2Fwork%2Fdemo`).
    인코딩이 조금 달라도 맞게 이름을 풀어 비교하고, 255바이트가 넘는 이름(slug+hash)은 안의 `.cwd` 파일로 맞춘다."""
    want = {cwd, os.path.realpath(cwd)}
    root = os.path.join(base, "sessions")
    out = []
    for c in want:
        d = os.path.join(root, urllib.parse.quote(c, safe=""))
        if d not in out and os.path.isdir(d) and not os.path.islink(d):
            out.append(d)
    if out:
        return out
    try:
        names = os.listdir(root)
    except OSError:
        return out
    for name in names:
        d = os.path.join(root, name)
        if not os.path.isdir(d) or os.path.islink(d):
            continue
        if urllib.parse.unquote(name) in want:
            out.append(d)
            continue
        try:
            if read_regular_file(os.path.join(d, ".cwd"), 8192).decode("utf-8", "replace").strip() in want:
                out.append(d)
        except OSError:
            continue
    return out


def _grok_updates(d):
    """세션 폴더 → (session_id, updates.jsonl 경로) 또는 None."""
    sid = os.path.basename(d)
    path = os.path.join(d, "updates.jsonl")
    if not SESSION_ID_RE.match(sid):
        return None
    try:
        if not stat.S_ISREG(os.stat(path, follow_symlinks=False).st_mode):
            return None
    except OSError:
        return None
    return sid, path


def _lsof_grok(pid, base, runner):
    """grok이 세션 동안 열어 두는 `sessions/<cwd>/<id>/events.jsonl`(실측 1.0.77) → 그 세션의 updates.jsonl."""
    exe = next((p for p in ("/usr/sbin/lsof", "/usr/bin/lsof") if os.path.exists(p)), None)
    if not exe:
        return None
    try:
        r = runner([exe, "-a", "-p", str(int(pid)), "-Fn"], capture_output=True, text=True,
                   timeout=HC_TIMEOUT, shell=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    root = os.path.realpath(os.path.join(base, "sessions"))
    for ln in (r.stdout or "").splitlines():
        if not ln.startswith("n") or os.path.basename(ln[1:]) != "events.jsonl":
            continue
        d = os.path.dirname(ln[1:])
        if os.path.realpath(os.path.dirname(os.path.dirname(d))) != root:
            continue
        hit = _grok_updates(d)
        if hit:
            return hit
    return None


def find_grok_transcript(info, home, runner=None):
    """Grok 터미널 → (session_id, updates.jsonl 경로). 실측 1.0.77: $GROK_HOME(기본 ~/.grok)/sessions/<URL 인코딩 cwd>/
    <세션 id>/updates.jsonl(ACP 알림 기록). 지금 세션은 grok이 열어 둔 events.jsonl(lsof)로, 못 보면 그 폴더의 최근 기록."""
    info = info or {}
    runner = runner or subprocess.run
    base = grok_dir(home)
    fg = info.get("foreground")
    pids = [v for v in (info.get("pid"), fg.get("pid") if isinstance(fg, dict) else None)
            if isinstance(v, int) and not isinstance(v, bool) and v > 0]
    if not IS_WINDOWS:
        for pid in dict.fromkeys(pids):
            hit = _lsof_grok(pid, base, runner)
            if hit:
                return hit
    cwd = live_cwd(info)
    if not cwd:
        return None, None
    best = None
    for cd in _grok_cwd_dirs(base, cwd):
        try:
            names = os.listdir(cd)
        except OSError:
            continue
        for name in names:
            hit = _grok_updates(os.path.join(cd, name))
            if not hit:
                continue
            try:
                mt = os.stat(hit[1], follow_symlinks=False).st_mtime
            except OSError:
                continue
            if best is None or mt > best[0]:
                best = (mt, hit)
    return best[1] if best else (None, None)


# Cursor 기록(실측 2026.11.19): ~/.cursor/chats/<md5(cwd)>/<chatId>/store.db(SQLite, WAL, 세션 동안 열어 둔다).
# blobs(id = 내용 sha256 hex, data), meta(key '0' = hex로 적은 JSON {latestRootBlobId, name, mode, …}).
# root blob(protobuf)의 field 1 반복이 메시지 blob id(32바이트) 순서 목록이고 메시지 blob은 AI SDK 모양 JSON
# (role system/user/assistant/tool, content: text·reasoning·tool-call·tool-result). 읽기 전용으로만 연다.
# 같은 대화의 agent-transcripts/<id>.jsonl은 도구 결과·거절이 없어 쓰지 않는다.
CURSOR_CHAT_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
CURSOR_MAX_MESSAGES = 1000
CURSOR_PREVIEW_MESSAGES = 40
CURSOR_USER_QUERY_RE = re.compile(r"<user_query>\n?(.*?)\n?</user_query>", re.S)
_cursor_preview_cache = {}  # (db 경로, 대화) → ((db·wal 크기·시각), 미리보기)


def cursor_dir(home):
    return os.path.join(home, ".cursor")


def _cursor_chat_dirs(base, cwd):
    out = []
    for c in dict.fromkeys((cwd, os.path.realpath(cwd))):
        out.append(os.path.join(base, "chats", hashlib.md5(c.encode("utf-8")).hexdigest()))
    return out


def _cursor_store(d):
    """대화 폴더 → (chatId, store.db 경로) 또는 None."""
    cid = os.path.basename(d)
    path = os.path.join(d, "store.db")
    if not CURSOR_CHAT_RE.match(cid):
        return None
    try:
        if not stat.S_ISREG(os.stat(path, follow_symlinks=False).st_mode):
            return None
    except OSError:
        return None
    return cid, path


def _lsof_cursor(pid, base, runner):
    exe = next((p for p in ("/usr/sbin/lsof", "/usr/bin/lsof") if os.path.exists(p)), None)
    if not exe:
        return None
    try:
        r = runner([exe, "-a", "-p", str(int(pid)), "-Fn"], capture_output=True, text=True,
                   timeout=HC_TIMEOUT, shell=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    root = os.path.realpath(os.path.join(base, "chats"))
    for ln in (r.stdout or "").splitlines():
        if not ln.startswith("n") or os.path.basename(ln[1:]) != "store.db":
            continue
        d = os.path.dirname(ln[1:])
        if os.path.realpath(os.path.dirname(os.path.dirname(d))) != root:
            continue
        hit = _cursor_store(d)
        if hit:
            return hit
    return None


def _mtime_with_wal(path):
    best = None
    for f in (path, path + "-wal"):
        try:
            mt = os.stat(f, follow_symlinks=False).st_mtime
        except OSError:
            continue
        best = mt if best is None else max(best, mt)
    return best


def find_cursor_transcript(info, home, runner=None):
    """Cursor 터미널 → (chatId, store.db 경로). 지금 대화는 cursor-agent가 열어 둔 store.db(lsof)로,
    못 보면 그 폴더(md5(cwd)) 대화 중 가장 최근에 바뀐 것."""
    info = info or {}
    runner = runner or subprocess.run
    base = cursor_dir(home)
    fg = info.get("foreground")
    pids = [v for v in (info.get("pid"), fg.get("pid") if isinstance(fg, dict) else None)
            if isinstance(v, int) and not isinstance(v, bool) and v > 0]
    if not IS_WINDOWS:
        for pid in dict.fromkeys(pids):
            hit = _lsof_cursor(pid, base, runner)
            if hit:
                return hit
    cwd = live_cwd(info)
    if not cwd:
        return None, None
    best = None
    for cd in _cursor_chat_dirs(base, cwd):
        try:
            names = os.listdir(cd)
        except OSError:
            continue
        for name in names:
            hit = _cursor_store(os.path.join(cd, name))
            mt = _mtime_with_wal(hit[1]) if hit else None
            if mt is not None and (best is None or mt > best[0]):
                best = (mt, hit)
    return best[1] if best else (None, None)


def _pb_fields(buf):
    """protobuf 메시지를 얕게 훑는다: [(field, wire type, 값)]. 깨졌으면 거기까지."""
    out = []
    i, n = 0, len(buf)

    def varint(i):
        v = shift = 0
        while i < n:
            b = buf[i]
            i += 1
            v |= (b & 0x7F) << shift
            if not b & 0x80:
                return v, i
            shift += 7
            if shift > 63:
                break
        raise ValueError("varint")
    try:
        while i < n:
            key, i = varint(i)
            field, wt = key >> 3, key & 7
            if wt == 0:
                v, i = varint(i)
            elif wt == 1:
                v, i = buf[i:i + 8], i + 8
            elif wt == 2:
                ln, i = varint(i)
                if i + ln > n:
                    break
                v, i = buf[i:i + ln], i + ln
            elif wt == 5:
                v, i = buf[i:i + 4], i + 4
            else:
                break
            out.append((field, wt, v))
    except ValueError:
        pass
    return out


def _cursor_user_text(content):
    """사용자 메시지에서 <user_query> 안 글만(시스템이 넣은 user_info·system_reminder·첨부는 숨김)."""
    if isinstance(content, str):
        raw = content
    elif isinstance(content, list):
        raw = "\n".join(c.get("text") for c in content
                        if isinstance(c, dict) and c.get("type") == "text" and isinstance(c.get("text"), str))
    else:
        return None
    qs = [q.strip() for q in CURSOR_USER_QUERY_RE.findall(raw) if q.strip()]
    return "\n\n".join(qs) or None


def _parse_cursor_messages(msgs, limit):
    """AI SDK 모양 메시지 목록 → (turns, 잘렸는지). 도구 결과(role tool)는 같은 toolCallId 호출에 붙인다.
    거절은 결과 "Rejected: <사용자 글>"과 highLevelToolCallResult.output.rejected(실측)."""
    turns = []
    calls = {}
    for m in msgs:
        if not isinstance(m, dict):
            continue
        role, content = m.get("role"), m.get("content")
        if role == "user":
            text = _cursor_user_text(content)
            if text:
                turns.append({"role": "user", "parts": [{"kind": "text", "text": _cap(text)}]})
        elif role == "assistant" and isinstance(content, list):
            out = []
            for c in content:
                if not isinstance(c, dict):
                    continue
                typ = c.get("type")
                if typ in ("text", "reasoning") and isinstance(c.get("text"), str) and c["text"].strip():
                    out.append({"kind": "text" if typ == "text" else "thinking", "text": _cap(c["text"])})
                elif typ == "tool-call":
                    name = c.get("toolName") if isinstance(c.get("toolName"), str) else "?"
                    args = c.get("args") if isinstance(c.get("args"), dict) else {}
                    if name == "CallDynamicTool" and isinstance(args.get("toolName"), str):
                        name = args["toolName"]  # TodoWrite·SwitchMode 등은 CallDynamicTool로 불린다
                        args = args.get("arguments") if isinstance(args.get("arguments"), dict) else {}
                    part = {"kind": "tool", "name": name, "summary": _tool_summary(name, args),
                            "input": _cap_obj(args), "output": None, "state": "pending"}
                    if isinstance(c.get("toolCallId"), str):
                        calls[c["toolCallId"]] = part
                    out.append(part)
            _codex_add(turns, "assistant", out)
        elif role == "tool" and isinstance(content, list):
            hl = ((m.get("providerOptions") or {}).get("cursor") or {}).get("highLevelToolCallResult") \
                if isinstance(m.get("providerOptions"), dict) else None
            hl = hl if isinstance(hl, dict) else {}
            outp = hl.get("output") if isinstance(hl.get("output"), dict) else {}
            bad = bool(hl.get("isError")) or "rejected" in outp or "error" in outp
            for c in content:
                if not isinstance(c, dict) or c.get("type") != "tool-result":
                    continue
                part = calls.get(c.get("toolCallId"))
                if part is None:
                    continue
                res = c.get("result")
                if not isinstance(res, str):
                    res = json.dumps(res, ensure_ascii=False) if res is not None else None
                part["output"] = _cap(_strip_ansi(res)) if res is not None else None
                part["state"] = "error" if bad or (res or "").startswith("Rejected") else "ok"
    cut = len(turns) > limit
    return turns[-limit:] if limit else turns, cut


def read_cursor_transcript(path, sid, limit=50, max_messages=CURSOR_MAX_MESSAGES):
    if not isinstance(sid, str) or not CURSOR_CHAT_RE.match(sid):
        return [], False
    rows = _opencode_query(path, "SELECT value FROM meta WHERE key = '0'")
    try:
        raw = rows[0][0]
        raw = raw.decode("ascii") if isinstance(raw, bytes) else raw
        meta = json.loads(bytes.fromhex(raw).decode("utf-8"))
        root_id = meta["latestRootBlobId"]
        if not isinstance(root_id, str) or not re.match(r"^[0-9a-fA-F]{64}$", root_id):
            return [], False
    except (TypeError, IndexError, ValueError, KeyError, AttributeError):
        return [], False
    rows = _opencode_query(path, "SELECT data FROM blobs WHERE id = ?", (root_id,))
    if not rows or not isinstance(rows[0][0], (bytes, bytearray)):
        return [], False
    ids = [bytes(v).hex() for f, wt, v in _pb_fields(bytes(rows[0][0])) if f == 1 and wt == 2 and len(v) == 32]
    more = len(ids) > max_messages
    ids = ids[-max_messages:]
    data = {}
    uniq = list(dict.fromkeys(ids))
    for k in range(0, len(uniq), 500):
        chunk = uniq[k:k + 500]
        for bid, blob in _opencode_query(path, "SELECT id, data FROM blobs WHERE id IN (%s)"
                                         % ",".join("?" * len(chunk)), chunk) or []:
            data[bid] = blob
    msgs = []
    for bid in ids:
        blob = data.get(bid)
        try:
            msgs.append(json.loads(bytes(blob).decode("utf-8") if isinstance(blob, (bytes, bytearray)) else blob))
        except (TypeError, ValueError, UnicodeDecodeError):
            continue
    turns, cut = _parse_cursor_messages(msgs, limit)
    return turns, cut or more


def cursor_preview(path, sid):
    """목록 미리보기: 최근 메시지 몇 개만 읽어 마지막 assistant 글. db·wal이 안 바뀌었으면 기억한 값."""
    key = []
    for f in (path, path + "-wal"):
        try:
            st_ = os.stat(f, follow_symlinks=False)
            key.append((st_.st_mtime_ns, st_.st_size))
        except OSError:
            key.append(None)
    hit = _cursor_preview_cache.get((path, sid))
    if hit and hit[0] == key:
        return hit[1]
    turns, _ = read_cursor_transcript(path, sid, 0, max_messages=CURSOR_PREVIEW_MESSAGES)
    pv = None
    for turn in reversed(turns):
        texts = [p["text"] for p in turn["parts"] if p.get("kind") == "text"] if turn["role"] == "assistant" else []
        if texts:
            pv = preview_clean(texts[-1])
            break
    if len(_cursor_preview_cache) > 200:
        _cursor_preview_cache.clear()
    _cursor_preview_cache[(path, sid)] = (key, pv)
    return pv


# ---- Amp 대화 기록. 본문은 Amp 서버(ampcode.com)에만 있다(실측 0.0.1795233642). 로컬에는 스레드별 운영 로그
# $XDG_CACHE_HOME/amp/logs/threads/<T-id>.log만 있고 amp가 세션 동안 쓰기로 열어 둔다(lsof로 스레드 ID).
# lsof가 없거나(Windows) 못 찾으면 `amp threads list --json`(1.2초 안팎, [{id, title, updated, tree: "file:///<폴더>",
# messageCount}])에서 그 터미널 폴더의 가장 최근 스레드를 고른다.
# 본문은 `amp threads export <T-id>`(JSON, 0.6초 안팎)로 읽는다. 실행 비용 때문에 대화 보기 요청 때만 부르고
# 목록 미리보기에는 쓰지 않는다. 실행 파일은 허용 목록 이름(AGENT_BY_ID)을 find_executable로 찾고 인자는 검증한 id만.
AMP_THREAD_RE = re.compile(r"^T-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
AMP_EXPORT_TIMEOUT = 8.0
AMP_EXPORT_TTL = 5.0  # 같은 스레드 export를 다시 쓰는 시간(초)
AMP_EXPORT_MAX = 16 * 1024 * 1024
AMP_SUGGESTED_RE = re.compile(r"\s*<suggested-replies>.*?</suggested-replies>\s*", re.S)
AMP_LIST_LIMIT = 50
AMP_LIST_TTL = 5.0  # threads list를 다시 쓰는 시간(초)
_amp_export_cache = {}  # (exe, id) → (시각, dict)
_amp_list_cache = {}    # exe → (시각, list)
_amp_export_lock = threading.Lock()


def _lsof_exe():
    return next((p for p in ("/usr/sbin/lsof", "/usr/bin/lsof") if os.path.exists(p)), None)


def _lsof_amp(pid, runner):
    exe = _lsof_exe()
    if not exe:
        return None
    try:
        r = runner([exe, "-a", "-p", str(int(pid)), "-Fn"], capture_output=True, text=True,
                   timeout=HC_TIMEOUT, shell=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    for ln in (r.stdout or "").splitlines():
        if not ln.startswith("n"):
            continue
        path = ln[1:].replace("\\", "/")
        name = path.rsplit("/", 1)[-1]
        if not name.endswith(".log") or not path[:-len(name)].endswith("/amp/logs/threads/"):
            continue
        if AMP_THREAD_RE.match(name[:-4]):
            return name[:-4]
    return None


def amp_executable(home):
    return find_executable(AGENT_BY_ID["amp"][2], agent_search_dirs(home))


def _amp_tree_path(tree, win=IS_WINDOWS):
    """threads list의 tree("file:///Users/me/demo", Windows "file:///C:/…") → 폴더 경로 또는 None."""
    if not isinstance(tree, str) or not tree.startswith("file://"):
        return None
    p = urllib.parse.unquote(tree[len("file://"):])
    if win:
        if re.match(r"^/[A-Za-z]:", p):
            return p[1:]
        return "//" + p if p and not p.startswith("/") else None  # file://server/share → UNC
    return p if p.startswith("/") else None


def _amp_same_dir(a, b, win=IS_WINDOWS):
    """같은 폴더인가. Windows는 대소문자·구분자·끝 구분자를 무시한다."""
    if win:
        def f(x):
            return ntpath.normcase(ntpath.normpath(x)).rstrip("\\")
    else:
        def f(x):
            return os.path.normcase(os.path.realpath(x))
    return f(a) == f(b)


def amp_latest_thread_in(cwd, exe, runner=None, clock=time.monotonic):
    """`amp threads list --json`에서 cwd 폴더(tree)의 가장 최근(updated) 스레드 ID. 없거나 실패하면 None.
    목록은 AMP_LIST_TTL 동안 기억한 값."""
    if not isinstance(cwd, str) or not cwd or not is_abs_path(cwd):
        return None
    if not isinstance(exe, str) or not is_abs_path(exe) or not os.path.isfile(exe):
        return None
    now = clock()
    with _amp_export_lock:
        hit = _amp_list_cache.get(exe)
    if hit and now - hit[0] < AMP_LIST_TTL:
        rows = hit[1]
    else:
        runner = runner or subprocess.run
        try:
            r = runner([exe, "threads", "list", "--json", "--limit", str(AMP_LIST_LIMIT)], capture_output=True,
                       text=True, timeout=AMP_EXPORT_TIMEOUT, shell=False, **subprocess_kwargs(text=True))
        except (OSError, subprocess.TimeoutExpired):
            return None
        out = r.stdout if isinstance(r.stdout, str) else ""
        if getattr(r, "returncode", 0) != 0 or not out or len(out) > AMP_EXPORT_MAX:
            return None
        try:
            rows = json.loads(out)
        except ValueError:
            return None
        if not isinstance(rows, list):
            return None
        with _amp_export_lock:
            if len(_amp_list_cache) > 10:
                _amp_list_cache.clear()
            _amp_list_cache[exe] = (now, rows)
    best = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        tid, updated, path = row.get("id"), row.get("updated"), _amp_tree_path(row.get("tree"))
        if not isinstance(tid, str) or not AMP_THREAD_RE.match(tid) or not isinstance(updated, str) or not path:
            continue
        if _amp_same_dir(path, cwd) and (best is None or updated > best[0]):
            best = (updated, tid)
    return best[1] if best else None


def find_amp_transcript(info, home, runner=None):
    """Amp 터미널 → (스레드 ID, amp 실행 파일 경로). 스레드는 amp가 열어 둔 운영 로그(lsof)로 먼저 찾고, lsof가 없거나
    못 찾으면 threads list에서 그 터미널 폴더의 가장 최근 스레드(같은 폴더에 Amp 둘이면 최근 것). 실행 파일을 못 찾으면
    경로 None(대화 보기 빈 목록)."""
    info = info or {}
    runner = runner or subprocess.run
    fg = info.get("foreground")
    pids = [v for v in (info.get("pid"), fg.get("pid") if isinstance(fg, dict) else None)
            if isinstance(v, int) and not isinstance(v, bool) and v > 0]
    for pid in dict.fromkeys(pids):
        tid = _lsof_amp(pid, runner)
        if tid:
            return tid, amp_executable(home)
    exe = amp_executable(home)
    tid = amp_latest_thread_in(info.get("cwd"), exe, runner=runner) if exe else None
    return (tid, exe) if tid else (None, None)


def amp_thread_export(exe, thread_id, runner=None, clock=time.monotonic):
    """`amp threads export <id>` → dict 또는 None. 같은 스레드는 AMP_EXPORT_TTL 동안 기억한 값."""
    if not isinstance(thread_id, str) or not AMP_THREAD_RE.match(thread_id):
        return None
    if not isinstance(exe, str) or not is_abs_path(exe) or not os.path.isfile(exe):
        return None
    now = clock()
    with _amp_export_lock:
        hit = _amp_export_cache.get((exe, thread_id))
        if hit and now - hit[0] < AMP_EXPORT_TTL:
            return hit[1]
    runner = runner or subprocess.run
    try:
        r = runner([exe, "threads", "export", thread_id], capture_output=True, text=True,
                   timeout=AMP_EXPORT_TIMEOUT, shell=False, **subprocess_kwargs(text=True))
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = r.stdout if isinstance(r.stdout, str) else ""
    if getattr(r, "returncode", 0) != 0 or not out or len(out) > AMP_EXPORT_MAX:
        return None
    try:
        data = json.loads(out)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("id") != thread_id or not isinstance(data.get("messages"), list):
        return None
    with _amp_export_lock:
        if len(_amp_export_cache) > 50:
            _amp_export_cache.clear()
        _amp_export_cache[(exe, thread_id)] = (now, data)
    return data


def _amp_tool_output(run):
    res = run.get("result")
    if isinstance(res, str):
        return res
    if isinstance(res, dict):
        if isinstance(res.get("output"), str):
            return res["output"]
        if isinstance(res.get("summary"), str):
            return res["summary"]
    if res is None:
        return None
    return json.dumps(res, ensure_ascii=False)


def _parse_amp_thread(data, limit):
    """export JSON(실측): messages[] = {role: user|assistant, content: [text | thinking | tool_use{id,name,input} |
    tool_result{toolUseID, run{status, result}}]}. tool_result는 user 메시지에 오지만 도구 줄에 붙인다."""
    turns, tools = [], {}
    for m in data.get("messages") or []:
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant"):
            continue
        role = m["role"]
        parts = []
        for c in m.get("content") if isinstance(m.get("content"), list) else []:
            if not isinstance(c, dict):
                continue
            t = c.get("type")
            if t == "text" and isinstance(c.get("text"), str):
                text = AMP_SUGGESTED_RE.sub("\n", c["text"]).strip() if role == "assistant" else c["text"]
                if text.strip():
                    parts.append({"kind": "text", "text": _cap(text)})
            elif t == "thinking" and role == "assistant" and isinstance(c.get("thinking"), str) and c["thinking"].strip():
                parts.append({"kind": "thinking", "text": _cap(c["thinking"])})
            elif t == "tool_use" and role == "assistant":
                name = c.get("name") if isinstance(c.get("name"), str) else "?"
                inp = c.get("input") if isinstance(c.get("input"), dict) else {}
                tool = {"kind": "tool", "name": name, "summary": _tool_summary(name, inp), "input": _cap_obj(inp),
                        "output": None, "state": "pending"}
                if isinstance(c.get("id"), str):
                    tools[c["id"]] = tool
                parts.append(tool)
            elif t == "tool_result":
                tool = tools.get(c.get("toolUseID"))
                run = c.get("run") if isinstance(c.get("run"), dict) else {}
                if tool is None or run.get("status") in (None, "in-progress", "queued", "blocked-on-user"):
                    continue
                v = _amp_tool_output(run)
                tool["output"] = _cap(_strip_ansi(v)) if isinstance(v, str) else None
                res = run.get("result")
                code = res.get("exitCode") if isinstance(res, dict) else None
                bad = run.get("status") != "done" \
                    or (isinstance(code, int) and not isinstance(code, bool) and code != 0) \
                    or (isinstance(res, str) and res.startswith("Tool rejected"))
                tool["state"] = "error" if bad else "ok"
        if role == "user":
            if parts:
                turns.append({"role": "user", "parts": parts})
        else:
            _codex_add(turns, "assistant", parts)
    cut = len(turns) > limit
    return turns[-limit:] if limit else turns, cut


def read_amp_transcript(exe, thread_id, limit=50):
    data = amp_thread_export(exe, thread_id)
    if data is None:
        return [], False
    return _parse_amp_thread(data, limit)


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
        spec = agent_spec(agent_kind(info))
        if "preview" in spec and spec["preview"] is None:
            return None  # 미리보기가 없는 에이전트(Amp: 기록 찾기에 lsof·threads list를 부른다)
        hit = _preview_by_pid.get(pid) if isinstance(pid, int) else None
        if hit and hit[0] > now:
            sid, path = hit[1]
        else:
            sid, path = find_agent_transcript(info, home, runner=runner)
            if isinstance(pid, int):
                if len(_preview_by_pid) > 200:
                    _preview_by_pid.clear()
                _preview_by_pid[pid] = (now + PREVIEW_SESSION_TTL, (sid, path))
        if not path:
            return None
        pv = agent_spec(agent_kind(info)).get("preview")  # 기록이 jsonl이 아닌 에이전트(OpenCode SQLite)
        return pv(path, sid) if pv else preview_for_path(path)
    except Exception:
        return None


# ---------------------------------------------------------------- 폴더

# 홈 바로 아래에서 폴더 목록에 안 보이는 것(Finder도 숨긴다. 에이전트를 여기서 시작할 일이 없다).
# Library는 macOS, AppData는 Windows에만 있는 시스템 폴더다. 리눅스에서는 같은 이름의 사용자 폴더일 수 있다.
def hidden_home_dirs():
    if IS_WINDOWS:
        return frozenset({"AppData"})
    if sys.platform == "darwin":
        return frozenset({"Library"})
    return frozenset()


HIDDEN_HOME_DIRS = hidden_home_dirs()


def home_real():
    return os.path.realpath(os.path.expanduser("~"))


def resolve_home_path(rel, home):
    """홈 기준 상대 경로 → 홈 아래 실제 폴더 절대 경로. 숨김 요소, 절대 경로, 홈 밖(심볼릭 링크 포함)은 400."""
    p = _resolve_under_home(rel, home)
    if not os.path.isdir(p):
        raise ApiError(400, "bad_path")
    return p


def _resolve_under_home(rel, home):
    """resolve_home_path의 경로 검사(폴더인지는 안 본다). 파일 보기 API도 같은 검사를 쓴다."""
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


# ---------------------------------------------------------------- 파일 보기·첨부
#
# 파일 보기(GET /api/files, /api/files/raw)는 폴더 고르기(/api/folders)와 같은 경로 검사에 더해
# 홈 바로 아래 Library(macOS)·AppData(Windows)를 통째로 막는다. 키체인·메일·메시지·브라우저 프로필이 거기 있다.
# 맥 기본 파일 시스템은 대소문자를 가리지 않으므로 "library"도 막는다.
# 첨부(POST /api/uploads)는 ~/.sheltie/uploads/<날짜>/에만 쓴다(점 폴더라 파일 보기로는 안 보인다).

FILE_MAX = 32 * 1024 * 1024
FILE_IO_TIMEOUT = 60  # 업로드·다운로드 소켓: 이만큼 진전이 없으면 끊는다(초)
FILES_LIST_MAX = 1000
UPLOAD_NAME_MAX = 80


def _blocked_top(rel):
    """홈 기준 상대 경로의 첫 요소가 홈의 숨김 폴더(Library 등)인가. 대소문자 무시."""
    first = rel.replace("\\", "/").split("/", 1)[0].lower()
    return first in {d.lower() for d in hidden_home_dirs()}


def resolve_home_file(rel, home):
    """파일 보기용: 홈 아래 실제 절대 경로(파일·폴더). 폴더 API 규칙 + Library 금지 + 없는 경로 400."""
    p = _resolve_under_home(rel, home)
    if p != home:
        if _blocked_top(rel or "") or _blocked_top(rel_to_home(p, home) or ""):
            raise ApiError(400, "bad_path")
    if not os.path.exists(p):
        raise ApiError(400, "bad_path")
    return p


def open_home_file(rel, home):
    """GET /api/files/raw: (fd, 크기, 실제 경로). 일반 파일만, FILE_MAX 넘으면 413. fd는 호출한 쪽이 닫는다."""
    if not rel:
        raise ApiError(400, "bad_path")
    p = resolve_home_file(rel, home)
    if IS_WINDOWS and is_reparse_point(p):
        raise ApiError(400, "bad_path")
    try:
        # FIFO에서 멈추지 않게 O_NONBLOCK, 검사와 열기 사이에 링크로 바뀐 경우를 막으려고 O_NOFOLLOW
        fd = os.open(p, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                     | getattr(os, "O_BINARY", 0))
    except OSError:
        raise ApiError(400, "bad_path")
    try:
        st_ = os.fstat(fd)
        if not stat.S_ISREG(st_.st_mode):
            raise ApiError(400, "not_file")
        if st_.st_size > FILE_MAX:
            raise ApiError(413, "too_large", max=FILE_MAX)
    except BaseException:
        os.close(fd)
        raise
    return fd, st_.st_size, p


def list_home_dir(rel, home):
    """GET /api/files: 폴더 안 파일·폴더. 숨김, 홈의 Library, 홈 밖·숨김으로 가는 링크, 특수 파일은 뺀다."""
    p = resolve_home_file(rel or "", home)
    if not os.path.isdir(p):
        raise ApiError(400, "not_dir")
    entries = []
    try:
        with os.scandir(p) as it:
            for e in it:
                if e.name.startswith(".") or (p == home and _blocked_top(e.name)):
                    continue
                try:
                    if e.is_symlink():
                        r = rel_to_home(os.path.realpath(e.path), home)
                        if r is None or r == "" or _blocked_top(r):
                            continue
                    st_ = e.stat(follow_symlinks=True)
                except OSError:
                    continue
                is_dir = stat.S_ISDIR(st_.st_mode)
                if not is_dir and not stat.S_ISREG(st_.st_mode):
                    continue
                entries.append({"name": e.name, "dir": is_dir, "size": None if is_dir else st_.st_size,
                                "mtime": int(st_.st_mtime)})
    except OSError:
        raise ApiError(400, "bad_path")
    entries.sort(key=lambda x: (not x["dir"], x["name"].lower()))
    return {"path": rel_to_home(p, home) or "", "entries": entries[:FILES_LIST_MAX],
            "truncated": len(entries) > FILES_LIST_MAX}


def sanitize_upload_name(name):
    """첨부 파일 이름: 마지막 경로 요소만, 글자·숫자·._-만 남기고 나머지는 _, 앞의 점(숨김)은 뗀다."""
    if not isinstance(name, str):
        name = ""
    name = unicodedata.normalize("NFC", name)
    name = re.split(r"[/\\]", name)[-1]
    out = "".join(c if (c.isalnum() or c in "._-") else "_" for c in name)
    out = re.sub(r"_{2,}", "_", out).lstrip(".").rstrip(".")
    if len(out) > UPLOAD_NAME_MAX:
        stem, ext = os.path.splitext(out)
        ext = ext if len(ext) <= 16 else ""
        out = stem[:UPLOAD_NAME_MAX - len(ext)] + ext
    if not out or not any(c.isalnum() for c in out):
        out = "file" + (os.path.splitext(out)[1] if out else "")
    return out


def upload_dir(home, day):
    """~/.sheltie/uploads/<day>. 단계마다 0700, 링크면 거부(다른 곳에 쓰지 않게)."""
    d = home
    for part in (".sheltie", "uploads", day):
        d = os.path.join(d, part)
        try:
            os.mkdir(d) if IS_WINDOWS else os.mkdir(d, 0o700)
        except FileExistsError:
            pass
        st_ = os.lstat(d)
        if not stat.S_ISDIR(st_.st_mode) or (IS_WINDOWS and is_reparse_point(d)):
            raise ApiError(500, "upload_dir")
        if not IS_WINDOWS:
            os.chmod(d, 0o700, follow_symlinks=False) if os.chmod in os.supports_follow_symlinks else os.chmod(d, 0o700)
    return d


UPLOAD_KEEP_DAYS = 7
_UPLOAD_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_last_prune_day = None
_prune_lock = threading.Lock()


def prune_uploads(home, now=None, keep_days=UPLOAD_KEEP_DAYS):
    """~/.sheltie/uploads/ 아래 keep_days일 지난 날짜 폴더를 지운다. 링크는 따라가지도 지우지도 않는다."""
    now = time.time() if now is None else now
    cutoff = time.strftime("%Y-%m-%d", time.localtime(now - keep_days * 86400))
    d = home
    for part in (".sheltie", "uploads"):
        d = os.path.join(d, part)
        try:
            st_ = os.lstat(d)
        except OSError:
            return
        if not stat.S_ISDIR(st_.st_mode) or (IS_WINDOWS and is_reparse_point(d)):
            return
    try:
        names = os.listdir(d)
    except OSError:
        return
    for name in names:
        if not _UPLOAD_DAY_RE.match(name) or name >= cutoff:
            continue
        p = os.path.join(d, name)
        try:
            st_ = os.lstat(p)
        except OSError:
            continue
        if not stat.S_ISDIR(st_.st_mode) or (IS_WINDOWS and is_reparse_point(p)):
            continue
        # rmtree는 안쪽 링크를 따라가지 않고 링크만 지운다
        shutil.rmtree(p, ignore_errors=True)


def _prune_uploads_daily(home):
    """업로드 때 하루 한 번만 정리한다. 정리 실패는 업로드를 막지 않는다."""
    global _last_prune_day
    today = time.strftime("%Y-%m-%d")
    with _prune_lock:
        if _last_prune_day == today:
            return
        _last_prune_day = today
    try:
        prune_uploads(home)
    except Exception:  # noqa: BLE001
        log.exception("첨부 폴더 정리 실패")


def save_upload(home, name, length, rfile, day=None):
    """POST /api/uploads 본문 length 바이트를 새 파일에 쓴다. 덜 오면 지우고 400."""
    _prune_uploads_daily(home)
    safe = sanitize_upload_name(name)
    d = upload_dir(home, day or time.strftime("%Y-%m-%d"))
    path = os.path.join(d, "%s-%s" % (secrets.token_hex(4), safe))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_BINARY", 0), 0o600)
    left = length
    try:
        with os.fdopen(fd, "wb") as f:
            while left > 0:
                b = rfile.read(min(left, 1 << 20))
                if not b:
                    break
                f.write(b)
                left -= len(b)
    except BaseException:
        _unlink_quiet(path)
        raise
    if left:
        _unlink_quiet(path)
        raise ApiError(400, "short_body")
    return {"path": path, "name": os.path.basename(path), "size": length}


def _unlink_quiet(path):
    try:
        os.unlink(path)
    except OSError:
        pass


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
        if ttl is None:
            ttl = push_ttl(payload)
        return self._apns_post(device, device["token"], payload, ttl, self.cfg["topic"], "alert", 10)

    def send_live_activity(self, device, token, payload, priority=10, ttl=LIVE_ACTIVITY_TTL):
        """Live Activity 푸시. token은 Activity 푸시 토큰 또는 push-to-start 토큰, device는 앱의 일반 기기 항목.
        topic은 번들 ID + ".push-type.liveactivity"로 고정. 반환: (http_code, reason)."""
        return self._apns_post(device, token, payload, ttl, self.cfg["topic"] + ".push-type.liveactivity",
                               "liveactivity", priority)

    def _apns_post(self, device, tok, payload, ttl, topic, push_type, priority):
        hosts = self.cfg.get("apns_hosts") or DEFAULTS["apns_hosts"]
        env = device.get("env") if device.get("env") in ("sandbox", "production") else "sandbox"
        base = (hosts.get(env) or DEFAULTS["apns_hosts"][env]).rstrip("/")
        if not base.startswith("https://"):
            raise ValueError("apns host must be https")
        body = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        conf = "\n".join([
            "url = " + _curl_quote("%s/3/device/%s" % (base, tok)),
            "header = " + _curl_quote("apns-topic: %s" % topic),
            "header = " + _curl_quote("apns-push-type: %s" % push_type),
            "header = " + _curl_quote("apns-priority: %d" % priority),
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
        return self._post_with_retry(tok, body)

    def send_live_activity(self, device, token, payload, priority=10, ttl=LIVE_ACTIVITY_TTL):
        """Live Activity 푸시를 중계로. 서명은 token이 아니라 앱의 일반 기기 토큰(auth_token)용이다."""
        sig = device.get("relay_sig")
        if not sig:
            return 0, "no_relay_sig"
        env = device.get("env") if device.get("env") in APNS_ENVS else "sandbox"
        body = json.dumps({"push_type": "liveactivity", "device_token": token, "auth_token": device["token"],
                           "env": env, "relay_sig": sig, "priority": int(priority), "ttl": int(ttl),
                           "payload": payload}, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        return self._post_with_retry(token, body)

    def _post_with_retry(self, tok, body):
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
        self.names = {}

    def observe(self, tid, status, turn=None, name=None, initial=False):
        status = status or {}
        state = status.get("state")
        key = (state, status.get("msg") if state == "blocked" else None)
        with self.lock:
            prev = self.last.get(tid)
            self.last[tid] = key
            if name:
                self.names[tid] = name
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

    def blocked_items(self):
        """지금 막힌 터미널 [(tid, 이름, 막힘 msg)]."""
        with self.lock:
            return [(tid, self.names.get(tid), key[1]) for tid, key in self.last.items() if key[0] == "blocked"]

    def forget(self, tid):
        with self.lock:
            self.last.pop(tid, None)
            self.names.pop(tid, None)
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
# Claude·Codex가 아닌 에이전트가 우리 훅을 부를 때의 표시(Grok Build 1.0.77 실측: GROK_* 환경변수,
# camelCase 필드 hookEventName·sessionId·workspaceRoot를 snake_case와 같이 보낸다).
FOREIGN_HOOK_ENV = ("GROK_SESSION_ID", "GROK_HOOK_EVENT")
FOREIGN_HOOK_KEYS = ("workspaceRoot", "hookEventName")
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

    def read_styled(self, tid):
        if self.hooks.has(tid):
            raise ApiError(409, "unsupported")
        return self._hc().read_styled(tid)

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
# Copilot ask_user 질문 상자는 blocked 이벤트 직후 수십 ms 늦게 그려질 때가 있다(맥북 실측 1.0.114, 4번 중 2번 카드 없이 감).
COPILOT_PUSH_PROMPT_WAIT = 1.0
# Qwen 시작 폴더 신뢰 상자는 blocked 이벤트 뒤 30~50ms 지나 그려진다(맥북 실측 0.28.3, 5번 중 5번).
QWEN_PUSH_PROMPT_WAIT = 1.0


# ---------------------------------------------------------------- 에이전트 표
# 에이전트마다 다른 것을 한 곳에 모은다. 새 에이전트는 항목 하나와 그 파서·기록 함수만 더한다.
#   match(info)            이 터미널이 그 에이전트인가(hc agent·status.app·앞 프로그램)
#   parse(lines, reader)   화면 줄 → 활성 메뉴 카드 또는 None(모르는 화면은 None: 카드·버튼 없음)
#   free_text(p, num, s)   카드의 직접 입력 답 → answer_steps 단계
#   transcript(info, home, runner=) → (session_id, 기록 경로) 또는 (None, None). 테스트가 모듈 함수를 바꿔
#                          끼우므로 호출 때 이름으로 찾는다
#   push_wait()            blocked 알림 카드를 읽을 시간(초). 메뉴가 늦게 그려지는 에이전트는 그 안에서 다시 읽는다
#   screen                 hc가 막힘으로 안 보는 화면을 서버가 화면으로 판정하는 방식:
#                          None, "overlay"(hc status를 쓰고, hc가 막힘으로 안 보는 메뉴만 화면으로 덧씌움: 아래 overlay),
#                          "full"(OSC 7501 상태를 안 보내 hc status가 없다: 화면 메뉴·터미널 제목으로 전부 판정)
#   overlay                "overlay"일 때 {states: 화면을 볼 hc state들, card(p): 덧씌울 카드인가,
#                          plan_mode_re: 메뉴는 없고 이 줄이 화면 끝이면 끝남을 잠깐 미룸(없으면 None),
#                          poll: 상태 이벤트 없이 뜨는 상자라 3초 화면 판정(screen_poll)에 넣는다}
#   send_guard             화면으로 판정한 막힘이면 메시지 보내기를 409로 막는다(hc는 그 상자를 몰라 글자가 들어간다)
#   title_blocked/title_working  "full"에서 터미널 제목으로 막힘·작업 중을 아는 정규식(없으면 None)
#   busy_re                "full"에서 제목이 없을 때 화면 끝 줄로 작업 중을 아는 정규식(없으면 None)
#   done_settle            (선택) "full"에서 막힘 뒤 바로 끝남으로 보일 때 SCREEN_SETTLE 뒤 한 번 더 본다(답 직후 작업 표시 전 장면)
#   idle_after_re          (선택) "full"에서 이 msg의 막힘(시작 화면) 뒤에는 끝남이 아니라 idle
#   dialog_re              "full"에서 카드를 못 읽어도 화면 끝 줄이 이 힌트면 막힘(제목으로 막힘을 모르는 에이전트, 없으면 None)
#   dialog_group(text)     (선택) 여러 질문 상자의 묶음 키(탭 라벨 등, 아니면 None). 막힘 중 같은 묶음의
#                          다른 탭 카드로 바뀌면 새 막힘이 아니다(알림 한 번, Claude·Qwen의 hc status와 같다).
#                          hc status 에이전트(Grok: msg가 지금 질문 문장)도 같은 상자면 처음 msg로 둔다
#   style_tabs(p, text, read)  (선택) 지금 탭·답한 탭이 색으로만 보이는 여러 질문 상자(OpenCode): 앱 카드(/prompt)만
#                          read()(hc attach 색 그림)로 tabs·title을 채운다. id는 그대로
#   pick(p, i)             (선택) 그 항목을 고르는 키 단계. None을 주면 숫자·커서 이동 기본 방식
#   multi(p, num, want)    (선택) 복수 선택 답(want: 고를 index 집합) → 키 단계. 없으면 숫자 토글 + right(Claude)
#   reject_keys(p)         (선택) reject_index가 없는 카드의 거절 키(기본 esc)
#   approve_keys           (선택) 카드 없이(prompt_id 없이) 승인할 때 "1" 대신 보낼 키. 함수면 지금 화면 카드(또는 None)로
#                          키를 정한다(None이면 409 needs_manual)
#   reject_status_keys     (선택) 카드 없이 거절할 때 esc 대신 보낼 키. None이면 거절을 앱에서 직접(409 needs_manual).
#                          함수면 approve_keys처럼 지금 화면 카드로 정한다
#   reject_status_steps(p) (선택) 카드 없이 거절할 때 지금 화면 카드로 정하는 키 단계(글 붙여넣기 포함, Copilot 3번 + 문구).
#                          None이면 기본(esc 또는 reject_status_keys)
#   read(path, sid, n)     (선택) 기록이 jsonl이 아닐 때 (turns, 잘렸는지). preview(path, sid)는 목록 미리보기
#                          (preview None: 미리보기 없음, 목록에서 기록 찾기도 부르지 않는다)
#   ask_reader(info, home, runner=)  (선택) 화면에 일부만 보이는 질문 상자를 채울 기록 속 질문 인자(Gemini ask_user).
#                          parse의 reader에 GEMINI_ASK_KEY를 주면 이걸 부른다(필요할 때만 기록을 읽는다)
#   card_status            (선택) hc가 막힘을 알지만 kind가 없을 때(Amp): 막힘 중 화면 카드가 읽히면 API 판정(effective_info)
#                          의 status를 카드로 바꾼다. 알림 판정(refresh)은 hc status 그대로
AGENTS = {
    "claude": {"match": is_claude, "parse": _parse_claude_lines, "free_text": _free_text_digit,
               "transcript": lambda info, home, runner=None: find_transcript(info, home, runner=runner),
               "push_wait": lambda: PUSH_PROMPT_BUDGET, "retry": False,
               "screen": None},
    "codex": {"match": is_codex, "parse": _parse_codex_lines, "free_text": _free_text_codex,
              "transcript": lambda info, home, runner=None: find_codex_transcript(info, home, runner=runner),
              "push_wait": lambda: CODEX_PUSH_PROMPT_WAIT, "retry": True,
              "screen": "overlay",
              "overlay": {"states": ("done", "idle"), "card": lambda p: p["kind"] == "plan",
                          "plan_mode_re": CODEX_PLAN_MODE_RE, "poll": False}},
    "gemini": {"match": _named("gemini"), "parse": _parse_gemini_lines, "free_text": _free_text_gemini,
               "transcript": lambda info, home, runner=None: find_gemini_transcript(info, home, runner=runner),
               "push_wait": lambda: PUSH_PROMPT_BUDGET, "retry": False, "screen": "full",
               "title_blocked": GEMINI_TITLE_BLOCKED_RE, "title_working": GEMINI_TITLE_WORKING_RE, "busy_re": None,
               "pick": _pick_gemini, "multi": _multi_gemini, "dialog_group": _gemini_dialog_group,
               "ask_reader": lambda info, home, runner=None: gemini_pending_ask(info, home, runner=runner),
               "send_guard": True},
    # Copilot은 OSC 7501로 승인(permission)·질문·계획(question) 막힘을 보낸다. 폴더 신뢰(idle)·autopilot 권한(working)
    # 상자만 hc가 모른다(실측 1.0.114). 질문 상자는 blocked 직후 조금 늦게 그려질 때가 있어 다시 읽는다.
    "copilot": {"match": _named("copilot"), "parse": _parse_copilot_lines, "free_text": _free_text_copilot,
                "pick": _pick_copilot, "multi": _multi_copilot, "reject_status_steps": _copilot_status_reject,
                "transcript": lambda info, home, runner=None: find_copilot_transcript(info, home, runner=runner),
                "push_wait": lambda: COPILOT_PUSH_PROMPT_WAIT, "retry": True, "screen": "overlay",
                "overlay": {"states": ("idle", "working", "done"), "card": _copilot_overlay_card,
                            "plan_mode_re": None, "poll": True},
                "send_guard": True},
    # Qwen Code는 OSC 7501로 승인·계획(permission)·질문(question)·폴더 신뢰(permission "Approval needed")를 다 보낸다
    # (실측 0.28.3): 화면 덧씌우기가 필요 없다. 시작 신뢰 상자는 blocked 뒤 30~50ms 늦게 그려져 다시 읽는다.
    "qwen": {"match": is_qwen, "parse": _parse_qwen_lines, "free_text": _free_text_digit,
             "pick": _pick_qwen, "multi": _multi_qwen,
             "transcript": lambda info, home, runner=None: find_qwen_transcript(info, home, runner=runner),
             "push_wait": lambda: QWEN_PUSH_PROMPT_WAIT, "retry": True, "screen": None, "send_guard": True},
    # OpenCode는 OSC 7501을 안 보내고 제목도 "OC | <세션 제목>"으로 고정이다(실측 1.19.6): 막힘은 화면 대화 상자,
    # 작업 중은 맨 아래 "esc interrupt" 줄로만 안다. 기록은 SQLite(opencode.db).
    "opencode": {"match": _named("opencode"), "parse": _parse_opencode_lines, "free_text": _free_text_digit,
                 "transcript": lambda info, home, runner=None: find_opencode_transcript(info, home, runner=runner),
                 "read": lambda path, sid, n: read_opencode_transcript(path, sid, n),
                 "preview": lambda path, sid: opencode_preview(path, sid),
                 "push_wait": lambda: PUSH_PROMPT_BUDGET, "retry": False, "screen": "full",
                 "title_blocked": None, "title_working": None, "busy_re": OPENCODE_BUSY_RE,
                 "dialog_re": OPENCODE_DIALOG_RE, "dialog_group": _opencode_dialog_group,
                 "style_tabs": _opencode_style_tabs,
                 "pick": _pick_opencode, "multi": _multi_opencode,
                 "reject_keys": _reject_keys_opencode,
                 "approve_keys": ["enter"],
                 "send_guard": True},
    # Grok Build는 OSC 7501로 승인·계획·폴더 신뢰(permission)·질문(question)을 다 보낸다(실측 1.0.77): 덧씌우기 없음.
    # 메뉴는 blocked 감지 직후(≤0.25초) 이미 다 그려져 있다. Esc는 거절이 아니라 포커스 이동(그 뒤 숫자 키 무시)이라
    # 카드 없는 거절에 esc를 보내지 않는다.
    "grok": {"match": _named("grok"), "parse": _parse_grok_lines, "free_text": _free_text_grok,
             "transcript": lambda info, home, runner=None: find_grok_transcript(info, home, runner=runner),
             "push_wait": lambda: PUSH_PROMPT_BUDGET, "retry": False, "screen": None,
             "pick": _pick_grok, "multi": _multi_grok, "dialog_group": _grok_dialog_group,
             "reject_keys": _reject_keys_grok, "reject_status_keys": None, "send_guard": True},
    # Cursor Agent는 OSC 7501을 안 보내고 제목도 AI가 붙인 대화 제목으로 고정이다(실측 2026.11.19): 막힘은 화면 메뉴,
    # 작업 중은 입력줄 "ctrl+c to stop"·"Working" 줄로만 안다. 승인 메뉴는 글자 키라 카드 없는 답도 지금 화면 카드의
    # 키로 보낸다(esc는 거절이 아니라 "대신 할 말" 입력칸을 연다). 기록은 SQLite(store.db).
    "cursor": {"match": is_cursor, "parse": _parse_cursor_lines, "free_text": _free_text_cursor,
               "transcript": lambda info, home, runner=None: find_cursor_transcript(info, home, runner=runner),
               "read": lambda path, sid, n: read_cursor_transcript(path, sid, n),
               "preview": lambda path, sid: cursor_preview(path, sid),
               "push_wait": lambda: PUSH_PROMPT_BUDGET, "retry": False, "screen": "full",
               "title_blocked": None, "title_working": None, "busy_re": CURSOR_BUSY_RE,
               "dialog_re": CURSOR_DIALOG_RE, "dialog_group": _cursor_dialog_group,
               "pick": _pick_cursor, "multi": _multi_cursor, "reject_keys": _reject_keys_cursor,
               "approve_keys": _cursor_status_approve, "reject_status_keys": _cursor_status_reject,
               "done_settle": True, "idle_after_re": CURSOR_TRUST_MSG_RE, "send_guard": True},
    # Amp은 OSC 7501로 막힘·작업 중·끝남을 보내지만 막힘에 kind가 없다(msg "Input needed", 실측 0.0.1795233642):
    # hc status를 그대로 쓰고, 막힘일 때 화면 카드가 읽히면 API 판정용 status만 카드로 바꾼다(card_status. 알림 판정에는
    # 안 써서 PC에서 피드백 입력칸을 열어도 새 알림이 안 간다). 메뉴는 번호 없이 커서 이동 + enter. 대화 기록은 서버에만
    # 있어 대화 보기 때만 `amp threads export`를 부르고 목록 미리보기는 없다.
    "amp": {"match": _named("amp"), "parse": _parse_amp_lines, "free_text": _free_text_amp,
            "transcript": lambda info, home, runner=None: find_amp_transcript(info, home, runner=runner),
            "read": lambda path, sid, n: read_amp_transcript(path, sid, n),
            "preview": None,
            "push_wait": lambda: PUSH_PROMPT_BUDGET, "retry": False, "screen": None, "card_status": True,
            "approve_keys": _amp_status_approve, "reject_status_keys": _amp_status_reject, "send_guard": True},
}
AGENT_ORDER = ("claude", "codex", "gemini", "copilot", "qwen", "opencode", "grok", "cursor", "amp")
SCREEN_POLL_INTERVAL = 3.0  # "full" 에이전트 화면 판정 주기(초). 제목 이벤트가 먼저 오면 그때 바로 본다
# 제목으로 작업 중을 모르는 "full" 에이전트(OpenCode·Cursor)는 앱에서 보낸 순간을 작업 중으로 둔다(판정 주기보다 짧은
# 작업도 끝남 알림이 가게. hc events에는 화면 출력 이벤트가 없다, 실측 hc 0.1.21). 보낸 뒤 이 시간(초) 안에 본 화면은
# 아직 작업 표시가 그려지기 전일 수 있어 끝남으로 보지 않는다
SEND_SETTLE = 1.5
SCREEN_SETTLE = 0.6  # 제목은 막힘인데 메뉴를 못 읽었을 때 다시 볼 때까지(초)
PROMPT_STATUS_KINDS = {"approval": "permission", "confirm": "permission", "question": "question", "plan": "plan"}


def _card_status(p):
    """화면에서 읽은 카드로 만든 막힘 status(app 제외). 승인은 msg "approve <제목>", 질문은 질문 글."""
    msg = ("approve " + (p["title"] or "")).strip() if p["kind"] in ("approval", "confirm") \
        else (p["question"] or None if p["kind"] == "question" else None)
    return {"state": "blocked", "kind": PROMPT_STATUS_KINDS.get(p["kind"], "question"), "msg": msg[:200] if msg else None}


# ---------------------------------------------------------------- Live Activity

class LiveActivityStore:
    """기기별 Live Activity 토큰. 키는 앱의 일반 APNs 기기 토큰(DeviceStore와 같은 값).
    start_token: iOS 17.2+ push-to-start 토큰(앱이 꺼져 있어도 Activity를 시작시킨다).
    activity_token: 돌고 있는 Activity의 푸시 토큰(없으면 Activity가 없다)."""

    def __init__(self, path=None):
        self.path = path
        self.lock = threading.Lock()
        self.data = self._load()

    def _load(self):
        if not self.path:
            return {}
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = json.load(f)
            return {k: v for k, v in raw.items() if isinstance(v, dict)} if isinstance(raw, dict) else {}
        except (FileNotFoundError, ValueError):
            return {}

    def _save(self):
        if self.path:
            write_private(self.path, self.data)

    def get(self, device):
        with self.lock:
            e = self.data.get(device)
            return dict(e) if e else None

    def items(self):
        with self.lock:
            return [(k, dict(v)) for k, v in self.data.items()]

    def set(self, device, **fields):
        """start_token·activity_token을 정한다. 값이 None이면 지운다. 둘 다 없어지면 항목을 지운다."""
        with self.lock:
            e = self.data.setdefault(device, {})
            for k in ("start_token", "activity_token"):
                if k in fields:
                    if fields[k] is None:
                        e.pop(k, None)
                    elif e.get(k) != fields[k]:
                        e[k] = fields[k]
                        if k == "activity_token":
                            e.pop("started_at", None)
            if not e.get("start_token") and not e.get("activity_token"):
                self.data.pop(device, None)
            self._save()

    def mark_started(self, device, wall):
        with self.lock:
            if device in self.data:
                self.data[device]["started_at"] = wall
                self._save()

    def clear_started(self, device):
        """막힘이 다 풀렸다. 다음 막힘에 다시 start할 수 있게 한다."""
        with self.lock:
            e = self.data.get(device)
            if e is not None and e.pop("started_at", None) is not None:
                self._save()

    def remove_device(self, device):
        with self.lock:
            if self.data.pop(device, None) is not None:
                self._save()
                return True
            return False


def live_activity_state(blocked, now_s):
    """막힘 목록 [(tid, 이름, msg, since_ms)]에서 content-state. 가장 오래 막힌 것이 대표.
    키 이름은 앱 확장의 SheltieActivityAttributes.ContentState와 같아야 한다(Date가 아니라 정수 초·ms)."""
    state = {"blockedCount": len(blocked), "oldestTerminal": "", "oldestName": "", "oldestSummary": "",
             "oldestSinceMs": 0, "updatedAt": int(now_s)}
    if blocked:
        tid, name, msg, since = min(blocked, key=lambda b: b[3])
        state.update(oldestTerminal=tid, oldestName=(name or tid)[:60], oldestSummary=(msg or "")[:120],
                     oldestSinceMs=int(since))
    return state


def live_activity_signature(state):
    """바뀌었는지 판단하는 값. updatedAt은 뺀다."""
    return (state["blockedCount"], state["oldestTerminal"], state["oldestName"], state["oldestSummary"])


def live_activity_action(entry, state, last_sig, last_at, now, min_gap=LIVE_ACTIVITY_MIN_GAP,
                         start_wait=LIVE_ACTIVITY_START_WAIT, refresh=LIVE_ACTIVITY_REFRESH):
    """이 기기에 지금 무엇을 보낼지. "start" | "update" | "end" | "wait"(최소 간격 때문에 미룸) | None(할 일 없음).
    now·last_at·entry["started_at"]은 같은 시계(초). 상태가 그대로여도 refresh초가 지나면 update(stale-date 연장)."""
    n = state["blockedCount"]
    if entry.get("activity_token"):
        if n == 0:
            action = "end"
        elif live_activity_signature(state) != last_sig:
            action = "update"
        elif last_at is not None and now - last_at >= refresh:
            action = "update"
        else:
            return None
    elif n > 0 and entry.get("start_token") and "started_at" not in entry:
        action = "start"
    else:
        return None
    if last_at is not None and now - last_at < min_gap:
        return "wait"
    return action


def live_activity_payload(action, state, now_s, pc_name=None, server=None):
    aps = {"timestamp": int(now_s), "event": action, "content-state": state}
    if action in ("start", "update"):
        # 서버가 end를 못 보내는 경우(앱이 Activity 토큰을 못 올림, 서버 꺼짐)에도 오래된 막힘이 새것처럼 남지 않게.
        aps["stale-date"] = int(now_s) + LIVE_ACTIVITY_STALE
    if action == "start":
        aps["attributes-type"] = LIVE_ACTIVITY_ATTRIBUTES_TYPE
        aps["attributes"] = {"pcName": pc_name or "", "serverTag": server or ""}
        # 이게 없으면 push로 시작된 Activity에 갱신·종료용 push token이 안 나와 서버가 end를 못 보낸다(실기기 확인).
        aps["input-push-token"] = 1
        aps["alert"] = {"title": pc_name or "Sheltie", "body": state["oldestName"] or "Input needed"}
    elif action == "end":
        aps["dismissal-date"] = int(now_s) + LIVE_ACTIVITY_END_LINGER
    return {"aps": aps}


class LiveActivityManager:
    """막힘 수가 바뀌면 등록된 Live Activity에 start/update/end를 APNs(또는 중계)로 보낸다.
    기기마다 최소 간격(5초) 안의 변화는 하나로 합치고, 간격이 지나면 그때 상태로 한 번 더 확인한다."""

    def __init__(self, tracker, store, devices, pusher, pc_name=None, server=None, wall=time.time,
                 later=None, min_gap=LIVE_ACTIVITY_MIN_GAP):
        self.tracker = tracker
        self.store = store
        self.devices = devices
        self.pusher = pusher
        self.pc_name = pc_name
        self.server = server
        self.wall = wall
        self.later = later or (lambda sec, fn: threading.Timer(sec, fn).start())
        self.min_gap = min_gap
        self.lock = threading.RLock()
        self.since = {}  # tid → 막힘이 처음 보인 시각(ms)
        self.sent = {}  # 기기 → (signature, 보낸 시각(초))
        self.timer_set = False

    def _blocked(self, now):
        now_ms = int(now * 1000)
        items = self.tracker.blocked_items()
        live = {i[0] for i in items}
        for tid in list(self.since):
            if tid not in live:
                del self.since[tid]
        return [(tid, name, msg, self.since.setdefault(tid, now_ms)) for tid, name, msg in items]

    def forget_device(self, dev):
        with self.lock:
            self.sent.pop(dev, None)

    def sync(self):
        """보낸 [(기기 짧은 id, action)]를 돌려준다(시험용)."""
        sent = []
        wait = None
        with self.lock:
            entries = self.store.items()
            now = self.wall()
            if not entries:
                self.sent.clear()
                self.since.clear()
                return sent
            state = live_activity_state(self._blocked(now), now)
            known = {d["token"]: d for d in self.devices.list()}
            for dev, entry in entries:
                d = known.get(dev)
                if d is None or d.get("env") not in APNS_ENVS:
                    self.store.remove_device(dev)
                    self.sent.pop(dev, None)
                    continue
                if state["blockedCount"] == 0 and "started_at" in entry and not entry.get("activity_token"):
                    # 토큰 없이 띄운 Activity는 서버가 못 끈다(stale-date·앱이 정리). 다음 막힘에 새로 start하게만 한다.
                    self.store.clear_started(dev)
                prev = self.sent.get(dev)
                act = live_activity_action(entry, state, prev[0] if prev else None, prev[1] if prev else None,
                                           now, min_gap=self.min_gap)
                if act == "wait":
                    left = self.min_gap - (now - prev[1]) + 0.05
                    wait = left if wait is None else max(wait, left)
                    continue
                if act is None:
                    continue
                self._send(dev, d, entry, act, state, now)
                sent.append((short_token(dev), act))
            if wait is not None and not self.timer_set:
                self.timer_set = True
                self.later(wait, self._trailing)
        return sent

    def _trailing(self):
        with self.lock:
            self.timer_set = False
        try:
            self.sync()
        except Exception:  # noqa: BLE001
            log.exception("Live Activity 합치기 발송 오류")

    def _send(self, dev, d, entry, act, state, now):
        token = entry["start_token"] if act == "start" else entry["activity_token"]
        payload = live_activity_payload(act, state, now, self.pc_name, self.server)
        send = getattr(self.pusher, "send_live_activity", None)
        if send is None:
            return
        self.sent[dev] = (live_activity_signature(state), now)  # 실패해도 최소 간격은 지킨다
        try:
            code, reason = send(d, token, payload, 10)
        except Exception as e:  # noqa: BLE001 - 한 기기 실패가 다른 기기를 막지 않게
            log.warning("Live Activity %s %s 실패: %s", short_token(dev), act, type(e).__name__)
            return
        if code == 200:
            log.info("Live Activity %s %s 200", short_token(dev), act)
            if act == "start":
                self.store.mark_started(dev, now)
            elif act == "end":
                self.store.set(dev, activity_token=None)
        elif code == 410 or reason in ("BadDeviceToken", "Unregistered", "ExpiredToken"):
            self.store.set(dev, **({"start_token": None} if act == "start" else {"activity_token": None}))
            log.info("Live Activity %s: %d %s, 토큰 삭제", short_token(dev), code, reason)
        elif reason == "relay:bad_sig":
            self.devices.mark_relay_sig_bad(d["token"], d.get("relay_sig"))
        else:
            log.warning("Live Activity %s %s: %d %s", short_token(dev), act, code, reason)


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
        self._sleep = time.sleep  # 테스트가 바꾼다
        self._agent_kinds = {}  # tid → agent_kind(마지막 hc get). 알림 카드를 어느 화면으로 읽을지 정한다
        self._screen_state = {}  # "full" 에이전트: tid → 마지막으로 판정한 status
        self._screen_group = {}  # "full" 에이전트: tid → 막힘 중인 여러 질문 상자의 묶음 키(dialog_group)
        self._status_group = {}  # hc status 에이전트: tid → (묶음 키, 그 상자의 첫 msg)(dialog_group)
        self._screen_sent = {}  # 제목 규칙 없는 "full" 에이전트: tid → 앱에서 메시지를 보낸 시각(SEND_SETTLE)
        self._screen_turns = {}  # "full" 에이전트: tid → 끝남 횟수(Tracker의 turn 대신)
        self._card_states = {}  # card_status 에이전트: tid → 이번 막힘에서 마지막으로 읽은 카드 status
        self.tracker = Tracker(self._notify, prompt_for=self._prompt_for_push, reminders=self.reminders)
        self.blocked_since = BlockedSince()
        self.live_activity = LiveActivityStore(
            os.path.join(cfg["_dir"], LIVE_ACTIVITY_FILE) if cfg.get("_dir") else None)
        self.live_activity_mgr = LiveActivityManager(
            self.tracker, self.live_activity, devices, pusher, pc_name=machine_name(),
            server=server_tag(cfg.get("token")), later=lambda sec, fn: self._later(sec, fn))
        self.branches = BranchCache()
        self.agent_catalog = AgentCatalog(self.home)
        self._refresh_lock = threading.Lock()
        self._term_locks = {}
        self._term_locks_guard = threading.Lock()
        self._screen_wait_lock = threading.Lock()
        self._screen_waiters = 0
        self._pending = []
        self._detected = None
        self.stop = threading.Event()

    def _notify(self, payload):
        payload["server"] = server_tag(self.cfg.get("token"))
        # refresh 잠금 안에서 불리므로 모았다가 잠금 밖에서 보낸다. 감지 시각을 같이 둔다.
        self._pending.append((payload, self._detected if self._detected is not None else self.clock()))

    def sync_live_activity(self):
        try:
            self.live_activity_mgr.sync()
        except Exception:  # noqa: BLE001
            log.exception("Live Activity 오류")

    def _flush(self):
        pending, self._pending = self._pending, []
        for p, t0 in pending:
            try:
                self.pusher.push(p)
            except Exception:  # noqa: BLE001
                log.exception("푸시 오류")
            log.info("알림 %s %s %s 감지→발송 %dms", p["terminal"], p["state"], p["aps"]["category"],
                     int((self.clock() - t0) * 1000))
        self.sync_live_activity()

    def parse_screen(self, text, agent=None, tid=None, info=None):
        reader = self.plan_reader
        ask = agent_spec(agent).get("ask_reader")
        if ask and tid is not None:
            base = reader

            def reader(key):
                """GEMINI_ASK_KEY면 그 터미널 기록의 답을 기다리는 질문 인자(화면에 안 보이는 항목까지), 아니면 계획 파일."""
                if key != GEMINI_ASK_KEY:
                    return base(key)
                i = info
                if i is None:
                    try:
                        i = self.hc.get(tid)
                    except HcError:
                        return None
                try:
                    return ask(i, self.home, runner=self.runner)
                except Exception:  # noqa: BLE001 - 기록을 못 읽으면 카드 없음
                    log.exception("질문 인자 읽기 실패 %s", tid)
                    return None
        return parse_prompt(text, plan_reader=reader, agent=agent)

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
                    box["p"] = self.parse_screen((self.hc.read(tid, 0) or {}).get("text", ""), agent, tid=tid)
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

    def _overlay_status(self, tid, info):
        """hc가 막힘으로 보지 않는 메뉴(Codex "Implement this plan?", Copilot 폴더 신뢰·autopilot 권한)가 화면 끝에
        살아 있으면 (막힘 status, True). 메뉴는 없고 플랜 모드면 (None, True). 그 밖에는 (None, False).
        훅 세션은 화면이 없어 보지 않는다."""
        if self.hooks.has(tid):
            return None, False
        kind = agent_kind(info)
        over = agent_spec(kind).get("overlay") if agent_spec(kind)["screen"] == "overlay" else None
        if not over:
            return None, False
        st = status_of(info)
        if st.get("state") not in over["states"]:
            return None, False
        try:
            text = (self.hc.read(tid, 0) or {}).get("text", "")
        except HcError:
            return None, False
        p = parse_prompt(text, agent=kind)
        if p and over["card"](p):
            return dict(st, **_card_status(p)), True
        mode_re = over.get("plan_mode_re")
        if not mode_re:
            return None, False
        tail = [ln for ln in screen_lines(text) if ln.strip()][-1:]
        return None, bool(tail and mode_re.search(tail[0]))

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
        p = self.parse_screen(text, kind, tid=tid, info=info) if text else None
        prev_st = self._screen_state.get(tid) or {}
        prev = prev_st.get("state")
        group = spec["dialog_group"](text) if p and text and spec.get("dialog_group") else None
        if p and group is not None and prev == "blocked" and self._screen_group.get(tid) == group:
            st = prev_st  # 여러 질문 상자의 다른 탭(답해서 다음 탭으로 넘어감): 같은 막힘
        elif p:
            st = dict({"app": kind}, **_card_status(p))
        elif spec.get("title_blocked") and spec["title_blocked"].search(title):
            if prev == "blocked":
                st = prev_st  # 같은 상자에서 PC나 앱이 고르는 중(직접 입력 칸 등): 새 막힘이 아니다
            elif not settle:
                return None
            else:
                st = {"app": kind, "state": "blocked", "kind": "dialog", "msg": None}
        elif spec.get("dialog_re") and text and any(
                spec["dialog_re"].search(ln) for ln in [x for x in screen_lines(text) if x.strip()][-4:]):
            # 카드로 못 읽는 대화 상자(여러 질문 등)나 PC에서 직접 입력 칸을 연 상자: 같은 상자면 그대로
            st = prev_st if prev == "blocked" else {"app": kind, "state": "blocked", "kind": "dialog", "msg": None}
        else:
            busy = bool(spec.get("title_working") and spec["title_working"].search(title))
            if not busy and spec.get("busy_re") and text:
                tail = [ln for ln in screen_lines(text) if ln.strip()][-8:]
                busy = any(spec["busy_re"].search(ln) for ln in tail)
            sent = self._screen_sent.get(tid)
            if busy:
                st = {"app": kind, "state": "working"}
            elif prev == "working" and sent is not None and self.clock() - sent < SEND_SETTLE:
                st = prev_st  # 앱에서 막 보냈다: 작업 표시가 아직 안 그려졌을 수 있다
            elif prev == "blocked" and spec.get("idle_after_re") \
                    and spec["idle_after_re"].search(prev_st.get("msg") or ""):
                st = {"app": kind, "state": "idle"}  # 시작 화면(폴더 신뢰)에 답한 뒤: 끝난 작업이 없다
            elif prev in ("working", "blocked", "done"):
                if prev == "blocked" and spec.get("done_settle") and commit and not settle:
                    # 답 직후 작업 표시가 그려지기 전 한 장면(실측 Cursor): 알림 판정만 잠깐 뒤 한 번 더 보고 끝남으로
                    return None
                st = {"app": kind, "state": "done"}
            else:
                st = {"app": kind, "state": "idle"}
        if commit:
            if st["state"] == "done" and prev != "done":
                self._screen_turns[tid] = self._screen_turns.get(tid, 0) + 1
            self._screen_state[tid] = st
            if st["state"] != "blocked":
                self._screen_group.pop(tid, None)
            elif p:
                self._screen_group[tid] = group
        return st

    def _card_state(self, tid, info, commit=False):
        """card_status 에이전트(Amp): hc 막힘 중 화면 카드로 만든 status(kind·msg를 카드에서). 카드가 안 보이면(PC에서
        피드백 입력칸을 열었다 등) 이 막힘에서 마지막으로 본 카드 status를 그대로 둬 새 알림이 안 가게 한다. 처음부터
        카드가 없으면 None(hc status 그대로). commit이면 기억한다(refresh만)."""
        if self.hooks.has(tid) or status_of(info).get("state") != "blocked":
            if commit:
                self._card_states.pop(tid, None)
            return None
        try:
            text = (self.hc.read(tid, 0) or {}).get("text", "")
        except HcError:
            text = ""
        p = self.parse_screen(text, agent_kind(info), tid=tid, info=info) if text else None
        if not p:
            return self._card_states.get(tid)
        st = dict(status_of(info), **_card_status(p))
        if commit:
            self._card_states[tid] = st
        return st

    def _group_status(self, tid, st):
        """hc status 에이전트(dialog_group, Grok): 여러 질문 상자에서 질문을 넘기면 hc msg가 다음 질문 문장으로 바뀐다.
        같은 상자 안이면 그 상자의 첫 msg로 둬 Tracker가 새 막힘으로 보지 않게 한다(알림·재알림 한 번)."""
        spec = agent_spec(self._agent_kinds.get(tid))
        if spec["screen"] == "full" or not spec.get("dialog_group"):
            return st
        if st.get("state") != "blocked":
            self._status_group.pop(tid, None)
            return st
        try:
            text = (self.hc.read(tid, 0) or {}).get("text", "")
        except HcError:
            text = ""
        group = spec["dialog_group"](text) if text else None
        prev = self._status_group.get(tid)
        if group is None:
            self._status_group.pop(tid, None)
            return st
        if prev and prev[0] == group:
            return dict(st, msg=prev[1])
        self._status_group[tid] = (group, st.get("msg"))
        return st

    def effective_info(self, tid, info):
        """화면으로 판정한 막힘이 있으면 status를 바꾼 사본(카드·답·목록이 막힘으로 다룬다).
        "overlay"(Codex 계획 실행 메뉴, Copilot 폴더 신뢰·autopilot 권한)는 그 메뉴만, hc status가 없는 에이전트("full")는
        status 전체."""
        spec = agent_spec(agent_kind(info))
        if spec["screen"] == "full":
            st = self._screen_status(tid, info)
            info = dict(info)
            info["status"] = st or self._screen_state.get(tid) or {"app": agent_kind(info), "state": "idle"}
            return info
        if spec.get("card_status"):
            st = self._card_state(tid, info)
            if st:
                info = dict(info)
                info["status"] = st
            return info
        plan_st, _ = self._overlay_status(tid, info)
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
                elif agent_spec(self._agent_kinds[tid]).get("card_status"):
                    plan_st, plan_mode = self._card_state(tid, info, commit=True), False
                    if plan_st is None and st.get("state") == "blocked" and not settle:
                        # hc는 막힘인데 상자가 아직 안 그려졌다(실측 Amp: blocked 직후 한 장면): 잠깐 뒤 한 번 더 본다.
                        # 바로 알리면 "Input needed" 알림 뒤 카드 알림이 한 번 더 간다
                        self._later(SCREEN_SETTLE, lambda: self.refresh(tid, name=name, settle=True))
                        self._detected = None
                        return
                else:
                    plan_st, plan_mode = self._overlay_status(tid, info)
                if plan_st:
                    st = plan_st  # 계획 실행 메뉴: 끝남 대신 막힘으로(끝남 알림을 따로 보내지 않는다)
                elif plan_mode and st.get("state") == "done" and not settle:
                    # 플랜 모드 턴이 끝났는데 메뉴가 아직 안 그려졌다(맥미니는 늦게 그린다): 끝남 판정을 잠깐 미룬다
                    self._later(CODEX_PLAN_SETTLE, lambda: self.refresh(tid, name=name, settle=True))
                    self._detected = None
                    return
                st = self._group_status(tid, st)
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
            self._screen_group.pop(tid, None)
            self._status_group.pop(tid, None)
            self._screen_turns.pop(tid, None)
            self._card_states.pop(tid, None)
        for tid in self.tracker.known_claude() - live:
            self.exited(tid)
        self.sync_live_activity()  # 사라진 터미널로 막힘 수가 줄었을 수 있다

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
        """hc status가 없는 에이전트("full")와 상태 이벤트 없이 상자가 뜨는 에이전트(overlay poll) 터미널만
        화면으로 다시 판정한다(제목 이벤트를 놓치거나 working 중에 상자가 떠도 잡히게)."""
        for tid, kind in list(self._agent_kinds.items()):
            spec = agent_spec(kind) if kind else None
            if spec and (spec["screen"] == "full" or (spec.get("overlay") or {}).get("poll")):
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
        if any(k in body for k in FOREIGN_HOOK_KEYS):
            return None  # Claude 훅을 빌려 부르는 다른 에이전트(Grok 등): 등록·붙잡기 없이 넘긴다
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
            i["cwd_rel"] = self.cwd_rel(i.get("cwd"))
        return {"terminals": out}

    def _read_screen(self, tid, n):
        try:
            r = self.hc.read(tid, n)
        except HcError:
            raise ApiError(502, "hc_failed")
        text = r.get("text", "")
        return {"text": text, "complete": r.get("complete"),
                "hash": hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]}

    def api_screen(self, tid, lines, since=None, wait=None):
        self.require_terminal(tid)
        try:
            n = int(lines) if lines not in (None, "") else 0
        except ValueError:
            raise ApiError(400, "bad_lines")
        if not 0 <= n <= 300:
            raise ApiError(400, "bad_lines")
        try:
            w = float(wait) if wait not in (None, "") else 0.0
        except ValueError:
            raise ApiError(400, "bad_wait")
        if not 0 <= w <= SCREEN_WAIT_MAX:
            raise ApiError(400, "bad_wait")
        res = self._read_screen(tid, n)
        if not since or res["hash"] != since:
            return res
        if w <= 0:
            return {"unchanged": True, "hash": since}
        # since와 같다: 바뀔 때까지 기다린다. 스레드 하나를 잡으므로 동시 수를 제한한다.
        with self._screen_wait_lock:
            if self._screen_waiters >= SCREEN_WAIT_SLOTS:
                return {"unchanged": True, "hash": since, "busy": True}
            self._screen_waiters += 1
        try:
            deadline = time.monotonic() + w
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    return {"unchanged": True, "hash": since}
                time.sleep(min(SCREEN_WAIT_INTERVAL, left))
                res = self._read_screen(tid, n)
                if res["hash"] != since:
                    return res
        finally:
            with self._screen_wait_lock:
                self._screen_waiters -= 1

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
                    keys = agent_spec(p.get("agent")).get("reject_keys", lambda _p: ["esc"])(p)
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
            spec = agent_spec(agent_kind(info))
            if action == "reject" and "reject_status_steps" in spec:
                steps = spec["reject_status_steps"](self.current_prompt(tid, info))
                if steps is not None:  # esc가 턴 전체를 멈추는 에이전트(Copilot): 거절 항목 + 문구
                    keys = self._do_steps(tid, steps)
                    log.info("answer %s %s", tid, action)
                    return {"ok": True, "keys": keys}
            if action == "reject" and "reject_status_keys" in spec:
                keys = spec["reject_status_keys"]  # esc가 거절이 아닌 에이전트(Grok: 포커스만 옮긴다)
                if callable(keys):
                    keys = keys(self.current_prompt(tid, info))  # 글자 키 메뉴(Cursor): 지금 화면 카드의 거절 키
                if not keys:
                    raise ApiError(409, "needs_manual", msg=status_of(self.effective_info(tid, info)).get("msg"))
                keys = list(keys)
            if keys == ["1"]:
                ak = agent_spec(agent_kind(info)).get("approve_keys")
                if callable(ak):
                    ak = ak(self.current_prompt(tid, info))  # 글자 키 메뉴(Cursor): 지금 화면 카드의 승인 키
                    if not ak:
                        raise ApiError(409, "needs_manual", msg=status_of(self.effective_info(tid, info)).get("msg"))
                keys = list(ak or keys)  # 숫자 키를 무시하는 승인 메뉴
            self._keys(tid, keys)
        log.info("answer %s %s", tid, action)
        return {"ok": True, "keys": keys}

    # --- 메뉴 카드
    def current_prompt(self, tid, info, styled=False):
        """styled: 앱에 보낼 카드(/prompt)면 색으로만 보이는 탭 상태를 hc attach 색 그림으로 채운다(style_tabs)."""
        info = self.effective_info(tid, info)
        if status_of(info).get("state") != "blocked":
            return None
        try:
            r = self.hc.read(tid, 0)
        except HcError:
            raise ApiError(502, "hc_failed")
        text = (r or {}).get("text", "")
        kind = agent_kind(info)
        p = self.parse_screen(text, kind, tid=tid, info=info)
        style = agent_spec(kind).get("style_tabs") if styled and p else None
        if style:
            def read():
                try:
                    return self.hc.read_styled(tid)
                except (HcError, AttributeError) as e:
                    log.info("색 화면 못 읽음 %s: %s", tid, e)
                    return ""
            style(p, text, read)
        return p

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
            elif kind == "wait":
                self._sleep(val)  # 키로 연 입력칸이 그려질 때까지(Amp 피드백 칸)
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
        return {"prompt": self.current_prompt(tid, info, styled=True)}

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
            reader = agent_spec(agent_kind(info)).get("read")  # 기록이 jsonl이 아닌 에이전트(OpenCode SQLite)
            if reader and path:
                turns, cut = reader(path, sid, n)
                return {"session_id": sid, "turns": turns, "truncated": cut}
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

    # --- 파일 보기·첨부
    def api_files(self, rel):
        return list_home_dir(rel or "", self.home)

    def api_file_raw(self, rel):
        return open_home_file(rel or "", self.home)

    def api_upload(self, name, length, rfile):
        return save_upload(self.home, name, length, rfile)

    def cwd_rel(self, cwd):
        """터미널 cwd의 홈 기준 상대 경로(파일 보기 시작 폴더). 홈 밖·숨김이면 None, 홈이면 ""."""
        if not isinstance(cwd, str) or not cwd or not is_abs_path(cwd):
            return None
        return rel_to_home(os.path.realpath(cwd), self.home)

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
            if agent_spec(agent_kind(info)).get("send_guard") \
                    and status_of(self.effective_info(tid, info)).get("state") == "blocked":
                # hc는 이 에이전트의 대화 상자를 모른다: 그대로 보내면 메뉴에 글자가 들어간다
                raise ApiError(409, "hc_refused", detail="blocked")
            try:
                self.hc.send(tid, text)
            except HcError as e:
                raise ApiError(409, "hc_refused", detail=str(e)[:200])
        spec = agent_spec(agent_kind(info))
        if spec["screen"] == "full" and not spec.get("title_working"):
            self._mark_sent_working(tid, info)
        log.info("send %s (%d자)", tid, len(text))
        return {"ok": True}

    def _mark_sent_working(self, tid, info):
        """제목으로 작업 중을 모르는 "full" 에이전트: 보낸 순간을 작업 중으로 판정해 둔다. 3초 판정 사이에 끝난 작업도
        다음 판정이 작업 중 → 끝남으로 보고 알린다(SEND_SETTLE)."""
        st = {"app": agent_kind(info), "state": "working"}
        with self._refresh_lock:
            self._screen_state[tid] = st
            self._screen_group.pop(tid, None)
            self._screen_sent[tid] = self.clock()
            self.tracker.observe(tid, st, turn=self._screen_turns.get(tid), name=info.get("name"))
        self._flush()

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
        self.live_activity.remove_device(tok.lower())
        return {"ok": True, "removed": removed}

    def api_live_activity(self, body):
        """앱이 Live Activity 토큰을 올린다. device: 앱의 일반 APNs 기기 토큰(먼저 /api/devices로 등록돼 있어야 한다),
        start_token: push-to-start 토큰, activity_token: 돌고 있는 Activity의 푸시 토큰. 키가 있고 null이면 지운다."""
        dev = body.get("device")
        if not isinstance(dev, str) or not DEVICE_TOKEN_RE.match(dev):
            raise ApiError(400, "bad_token")
        dev = dev.lower()
        fields = {}
        for k in ("start_token", "activity_token"):
            if k not in body:
                continue
            v = body[k]
            if v is None:
                fields[k] = None
            elif isinstance(v, str) and DEVICE_TOKEN_RE.match(v):
                fields[k] = v.lower()
            else:
                raise ApiError(400, "bad_token")
        if not fields:
            raise ApiError(400, "bad_request")
        d = next((x for x in self.devices.list() if x["token"] == dev), None)
        if d is None or d.get("env") not in APNS_ENVS:
            raise ApiError(404, "unknown_device")
        self.live_activity.set(dev, **fields)
        log.info("Live Activity 등록 %s %s", short_token(dev), ",".join(sorted(fields)))
        if fields.get("activity_token"):
            # 새 Activity가 바로 현재 상태를 보이게 한다(최소 간격 안이면 합쳐진다).
            self.live_activity_mgr.forget_device(dev)
        self.sync_live_activity()
        return {"ok": True}

    def api_delete_live_activity(self, body):
        """device만 주면 그 기기의 Live Activity 토큰을 모두, activity_token도 주면 그 Activity 토큰만 지운다."""
        dev = body.get("device")
        if not isinstance(dev, str) or not DEVICE_TOKEN_RE.match(dev):
            raise ApiError(400, "bad_token")
        dev = dev.lower()
        act = body.get("activity_token")
        if act is None:
            return {"ok": True, "removed": self.live_activity.remove_device(dev)}
        if not isinstance(act, str) or not DEVICE_TOKEN_RE.match(act):
            raise ApiError(400, "bad_token")
        e = self.live_activity.get(dev)
        if e is None or e.get("activity_token") != act.lower():
            return {"ok": True, "removed": False}
        self.live_activity.set(dev, activity_token=None)
        return {"ok": True, "removed": True}

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
    ("GET", re.compile(r"^/api/files$"), "files"),
    ("GET", re.compile(r"^/api/files/raw$"), "file_raw"),
    ("POST", re.compile(r"^/api/uploads$"), "upload"),
    ("GET", re.compile(r"^/api/agents/catalog$"), "agent_catalog"),
    ("POST", re.compile(r"^/api/agents$"), "agents"),
    ("POST", re.compile(r"^/api/terminals/([^/]+)/keys$"), "keys"),
    ("POST", re.compile(r"^/api/terminals/([^/]+)/send$"), "send"),
    ("POST", re.compile(r"^/api/devices$"), "add_device"),
    ("DELETE", re.compile(r"^/api/devices$"), "delete_device"),
    ("POST", re.compile(r"^/api/live-activity$"), "live_activity"),
    ("DELETE", re.compile(r"^/api/live-activity$"), "delete_live_activity"),
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

    def _io_timeout(self):
        """업로드·다운로드 동안 소켓 timeout(진전 없는 시간). 이전 값을 돌려준다."""
        prev = self.connection.gettimeout()
        self.connection.settimeout(FILE_IO_TIMEOUT)
        return prev

    def _reply_file(self, fd, size, real):
        """파일 바이트를 그대로. 여는 사이 파일이 줄면 연결을 끊어 앱이 덜 받은 것을 알게 한다.
        헤더를 보낸 뒤의 오류(timeout·읽기 오류·앱이 닫음)는 JSON을 덧붙이지 않고 연결만 닫는다."""
        ctype = mimetypes.guess_type(os.path.basename(real))[0] or "application/octet-stream"
        prev = self._io_timeout()
        try:
            with os.fdopen(fd, "rb") as f:
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(size))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                left = size
                try:
                    while left > 0:
                        b = f.read(min(left, 1 << 20))
                        if not b:
                            self.close_connection = True
                            break
                        self.wfile.write(b)
                        left -= len(b)
                except (BrokenPipeError, ConnectionResetError):  # 앱이 받다가 닫음(미리보기 취소)
                    self.close_connection = True
                except Exception:  # noqa: BLE001  timeout·읽기 오류: 이미 200을 보냈으니 끊기만
                    log.warning("파일 전송 중단: %s", os.path.basename(real), exc_info=True)
                    self.close_connection = True
        finally:
            try:
                self.connection.settimeout(prev)
            except OSError:
                pass

    def _upload(self, app, query):
        """POST /api/uploads?name=: 본문이 파일 바이트. Content-Length 필수, FILE_MAX까지."""
        try:
            if self.headers.get("Transfer-Encoding"):
                raise ApiError(400, "chunked_not_supported")
            cl = self.headers.get("Content-Length")
            if not cl:
                raise ApiError(411, "length_required")
            try:
                n = int(cl)
            except ValueError:
                raise ApiError(400, "bad_length")
            if n <= 0:
                raise ApiError(400, "empty")
            if n > FILE_MAX:
                raise ApiError(413, "too_large", max=FILE_MAX)
            prev = self._io_timeout()
            try:
                res = app.api_upload((query.get("name") or [""])[0], n, self.rfile)
            finally:
                try:
                    self.connection.settimeout(prev)
                except OSError:
                    pass
        except BaseException:
            # 안 읽은 본문이 다음 요청으로 읽히지 않게(ApiError·timeout·OSError 모두). 부분 파일은 save_upload가 지운다.
            self.close_connection = True
            raise
        return self._reply(200, res)

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
                if name == "upload":
                    return self._upload(app, query)
                if name == "file_raw":
                    fd, size, real = app.api_file_raw((query.get("path") or [""])[0])
                    return self._reply_file(fd, size, real)
                body = self._body() if method in ("POST", "DELETE") else {}
                tid = urllib.parse.unquote(mt.group(1)) if mt.groups() else None
                if name == "health":
                    res = {"ok": True, "name": machine_name(), "started": round(PROCESS_STARTED, 3)}
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
                    res = app.api_screen(tid, (query.get("lines") or [None])[0],
                                         (query.get("since") or [None])[0], (query.get("wait") or [None])[0])
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
                elif name == "files":
                    res = app.api_files((query.get("path") or [""])[0])
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
                elif name == "live_activity":
                    res = app.api_live_activity(body)
                elif name == "delete_live_activity":
                    res = app.api_delete_live_activity(body)
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
        # Grok Build는 ~/.claude의 훅을 같이 읽어 부른다(1.0.77 실측). Claude 세션이 아니니 넘긴다.
        if any(env.get(k) for k in FOREIGN_HOOK_ENV):
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
