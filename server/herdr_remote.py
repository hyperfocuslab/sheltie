#!/usr/bin/env python3
"""herdr-remote: 맥미니의 herdr-core(`hc`) 위 Claude Code 에이전트를 아이폰에서 지켜보고 답하는 개인용 중계 서버.

Tailscale 주소에만 열리고, 모든 요청에 Bearer 토큰이 필요하다. 표준 라이브러리만 쓴다.
hc는 항상 argv 리스트(shell=False)로 부르며, 임의 명령을 실행하는 경로는 없다.

명령
  python3 herdr_remote.py init       설정이 없으면 토큰을 만들어 저장하고 한 번 출력한다(있으면 그대로 둔다).
  python3 herdr_remote.py serve      서버 실행(기본 동작).
  python3 herdr_remote.py test-push  등록된 모든 기기에 시험 푸시 1건.
  python3 herdr_remote.py pair       앱 연결용 QR과 sheltie:// 주소 출력(설정 public_url, 없으면 Tailscale 이름).

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

환경변수
  HERDR_REMOTE_CONFIG  설정 파일(기본 ~/.config/herdr-remote/config.json). devices.json은 같은 폴더.
  HERDR_REMOTE_LOG     로그 파일(기본 ~/Library/Logs/herdr-remote.log, 1MB 회전)
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
import os
import re
import secrets
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

DEFAULT_CONFIG_PATH = "~/.config/herdr-remote/config.json"
DEFAULT_LOG_PATH = "~/Library/Logs/herdr-remote.log"
DEFAULTS = {
    "bind": "127.0.0.1",
    "port": 8790,
    "token": "",
    "hc_path": "~/.local/bin/hc",
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
    os.makedirs(d, mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)


def write_private(path, data):
    """data(dict/list)를 JSON으로 0600 파일에 원자적으로 쓴다."""
    ensure_private_dir(os.path.dirname(path) or ".")
    tmp = "%s.tmp.%d" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
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
    with open(path) as f:
        raw = json.load(f)
    cfg = dict(DEFAULTS)
    cfg.update(raw)
    hosts = dict(DEFAULTS["apns_hosts"])
    hosts.update(raw.get("apns_hosts") or {})
    cfg["apns_hosts"] = hosts
    for k in ("hc_path", "apns_key_path"):
        cfg[k] = os.path.expanduser(cfg[k])
    cfg["push_relay_url"] = resolve_relay_url(cfg.get("push_relay_url"), cfg["apns_key_path"])
    cfg["port"] = int(cfg["port"])
    cfg["_path"] = path
    cfg["_dir"] = os.path.dirname(path)
    return cfg


def resolve_relay_url(url, apns_key_path):
    """실제로 쓸 중계 주소. "off"면 "", 비었고 APNs 키 파일이 없으면 기본 중계, 아니면 check_relay_url."""
    if url == RELAY_OFF:
        return ""
    if (url is None or url == "") and not os.path.isfile(apns_key_path or ""):
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
    path = os.path.expanduser(path or os.environ.get("HERDR_REMOTE_LOG") or DEFAULT_LOG_PATH)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    h = logging.handlers.RotatingFileHandler(path, maxBytes=1024 * 1024, backupCount=2)
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
            p = self.runner(argv, capture_output=True, text=True, timeout=HC_TIMEOUT, shell=False)
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

    def run_claude(self, cwd, prompt=None):
        """새 Claude 터미널. 실행 프로그램은 claude로 고정, prompt는 `--` 뒤라 옵션으로 읽히지 않는다(실측 2.1.329)."""
        argv = ["run", "--bg", "--json", "--cwd", cwd, "claude"]
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
            if os.path.basename(argv[0]) == "claude":
                return True
    return False


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
            free_text_index=None, tabs=None, always_index=None, reject_index=None):
    """options: (label, description, 화면 번호 또는 None). 번호는 숨긴 줄(Chat about this 등)을 건너뛰어도
    화면에 찍힌 그대로라 키 계산은 index가 아니라 number로 한다."""
    p = {"kind": kind, "title": title, "question": question, "body": body,
         "options": [{"index": k, "label": o[0], "description": o[1], "number": o[2] if len(o) > 2 else None}
                     for k, o in enumerate(options)],
         "selected": selected, "multi_select": bool(multi), "checked": list(checked or []),
         "free_text_index": free_text_index, "tabs": tabs,
         "always_index": always_index, "reject_index": reject_index}
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
    always = next((k for k, lb in enumerate(labels)
                   if lb.startswith("Yes, and") and "auto mode" not in lb), None)
    return _finish("approval", title, lines[q].strip(), body,
                   [(lb, None, r["num"]) for lb, r in zip(labels, rows)],
                   selected=next(k for k, r in enumerate(rows) if r["selected"]),
                   always_index=always, reject_index=reject_index(labels))


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


def parse_prompt(screen_text, plan_reader=None):
    """화면 글자 → 활성 메뉴 카드(dict) 또는 None. plan_reader(path)는 계획 파일 본문을 준다."""
    lines = screen_lines(screen_text)
    for fn in (_parse_question, _parse_submit, _parse_approval, _parse_confirm):
        p = fn(lines)
        if p:
            return p
    return _parse_plan(lines, plan_reader)


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


def read_regular_file(path, max_bytes, tail=False):
    """O_NOFOLLOW로 열고 일반 파일만. tail이면 끝 max_bytes. 반환 bytes(잘렸으면 tail 모드에서 첫 줄 버림)."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
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
        if num(fi) > 9:
            raise ApiError(400, "bad_option")
        return [("keys", [str(num(fi))]), ("text", text)]
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
    for h in {os.path.expanduser("~"), home_real()}:
        h = h.rstrip(os.sep)
        if h and (s == h or s.startswith(h + os.sep)):
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


def parse_transcript(lines, limit=50):
    """Claude Code 세션 jsonl 줄들 → (turns, 잘렸는지)."""
    turns = []
    tools = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or rec.get("isMeta") or rec.get("isSidechain"):
            continue
        typ = rec.get("type")
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


def find_transcript(info, home, runner=None):
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
    if not isinstance(proc_start, str) or _norm(proc_start) != ps_lstart(pid, runner):
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
            sid, path = find_transcript(info, home, runner=runner)
            if isinstance(pid, int):
                if len(_preview_by_pid) > 200:
                    _preview_by_pid.clear()
                _preview_by_pid[pid] = (now + PREVIEW_SESSION_TTL, (sid, path))
        return preview_for_path(path) if path else None
    except Exception:
        return None


# ---------------------------------------------------------------- 폴더

# 홈 바로 아래에서 폴더 목록에 안 보이는 것(Finder도 숨긴다. 에이전트를 여기서 시작할 일이 없다)
HIDDEN_HOME_DIRS = frozenset({"Library"})


def home_real():
    return os.path.realpath(os.path.expanduser("~"))


def resolve_home_path(rel, home):
    """홈 기준 상대 경로 → 홈 아래 실제 폴더 절대 경로. 숨김 요소, 절대 경로, 홈 밖(심볼릭 링크 포함)은 400."""
    if rel is None:
        rel = ""
    if not isinstance(rel, str) or len(rel) > 1024 or "\x00" in rel or rel.startswith("/"):
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
    if any(x.startswith(".") for x in r.split(os.sep)):
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


TAILSCALE_PATHS = ("/opt/homebrew/bin/tailscale", "/Applications/Tailscale.app/Contents/MacOS/Tailscale")


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
        p = runner([ts, "status", "--json"], capture_output=True, text=True, timeout=HC_TIMEOUT, shell=False)
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
               input=signing_input, capture_output=True, timeout=HC_TIMEOUT, shell=False)
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


# 알림 문구는 앱의 String Catalog 키(loc-key)로 보내 기기 언어로 보이게 한다.
# 평문 subtitle/body(영어)는 키를 모르는 구버전 앱과 로그용이다.
SUBTITLES = {"HR_PERMISSION": "Needs permission", "HR_PERMISSION_ALWAYS": "Needs permission",
             "HR_BLOCKED": "Needs reply", "HR_DONE": "Done", "HR_EXITED": "Exited"}
SUBTITLE_KEYS = {"HR_PERMISSION": "push.subtitle.permission", "HR_PERMISSION_ALWAYS": "push.subtitle.permission",
                 "HR_BLOCKED": "push.subtitle.blocked", "HR_DONE": "push.subtitle.done",
                 "HR_EXITED": "push.subtitle.exited"}
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
    alert = {"title": name or tid, "subtitle": SUBTITLES[cat], "body": body,
             "subtitle-loc-key": SUBTITLE_KEYS[cat]}
    if loc_key:
        alert["loc-key"] = loc_key
        if loc_args:
            alert["loc-args"] = loc_args
    payload = {
        "aps": {
            "alert": alert,
            "sound": "default",
            "category": cat,
            "thread-id": tid,
        },
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
            with open(self.path) as f:
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
            with open(self.path) as f:
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

    def send_one(self, device, payload):
        """반환: (http_code, reason). 토큰·JWT는 argv가 아니라 stdin의 curl 설정으로 넘긴다."""
        hosts = self.cfg.get("apns_hosts") or DEFAULTS["apns_hosts"]
        env = device.get("env") if device.get("env") in ("sandbox", "production") else "sandbox"
        base = (hosts.get(env) or DEFAULTS["apns_hosts"][env]).rstrip("/")
        if not base.startswith("https://"):
            raise ValueError("apns host must be https")
        tok = device["token"]
        body = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        conf = "\n".join([
            "url = " + _curl_quote("%s/3/device/%s" % (base, tok)),
            "header = " + _curl_quote("apns-topic: %s" % self.cfg["topic"]),
            "header = " + _curl_quote("apns-push-type: alert"),
            "header = " + _curl_quote("apns-priority: 10"),
            "header = " + _curl_quote("authorization: bearer %s" % self.jwt()),
            "data = " + _curl_quote(body),
        ]) + "\n"
        argv = [self.curl, "--http2", "-s", "--max-time", "15", "-o", "-",
                "-w", "\n%{http_code}", "--config", "-"]
        p = self.runner(argv, input=conf, capture_output=True, text=True, timeout=30, shell=False)
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

    def push(self, payload):
        if not self.available():
            return 0
        sent = 0
        for d in self.devices.list():
            code, _ = self._deliver(d, payload)
            if code == 200:
                sent += 1
        return sent

    def push_device(self, device, payload):
        """기기 하나에만 보낸다(시험 알림). 반환: (http_code, reason). 예외는 (0, 예외 이름)."""
        return self._deliver(device, payload)

    def _deliver(self, d, payload):
        """한 기기 발송 + 결과 처리(기기 삭제·서명 표시) + PushLog 기록. 반환: (http_code, reason)."""
        try:
            code, reason = self.send_one(d, payload)
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

    def __init__(self, cfg, devices, timeout=RELAY_TIMEOUT, clock=time.monotonic, sleep=time.sleep, push_log=None):
        super().__init__(cfg, devices, clock=clock, push_log=push_log)
        self.sleep = sleep
        u = urllib.parse.urlsplit(cfg["push_relay_url"])
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
        return bool(self.cfg.get("push_relay_url"))

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

    def push(self, payload):
        # 알림 하나에 collapse_id 하나: 중계가 apns-collapse-id로 넘겨 재시도 중복을 하나로 합친다.
        self._push_ctx.collapse_id = uuid.uuid4().hex
        try:
            return super().push(payload)
        finally:
            self._push_ctx.collapse_id = None

    def push_device(self, device, payload):
        self._push_ctx.collapse_id = uuid.uuid4().hex
        try:
            return super().push_device(device, payload)
        finally:
            self._push_ctx.collapse_id = None

    def send_one(self, device, payload):
        """반환: (http_code, reason). 중계 자신의 거절은 reason "relay:<error>"."""
        tok = device["token"]
        sig = device.get("relay_sig")
        if not sig:
            if tok not in self._no_sig_warned:
                self._no_sig_warned.add(tok)
                log.warning("푸시 %s: relay_sig가 없어 건너뛴다(앱에서 다시 등록 필요)", short_token(tok))
            return 0, "no_relay_sig"
        env = device.get("env") if device.get("env") in ("sandbox", "production") else "sandbox"
        cid = getattr(self._push_ctx, "collapse_id", None) or uuid.uuid4().hex
        body = json.dumps({"device_token": tok, "env": env, "relay_sig": sig, "collapse_id": cid, "payload": payload},
                          ensure_ascii=True, separators=(",", ":")).encode("ascii")
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
    if not isinstance(cwd, str) or not cwd.startswith("/"):
        return None

    def read_regular(path):
        # O_NONBLOCK: FIFO를 열 때 막히지 않는다. 연 뒤 fstat으로 일반 파일인지 본다(확인과 열기 사이 바꿔치기 방지).
        if not os.path.isfile(path):
            return None
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
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


# ---------------------------------------------------------------- 앱

PUSH_PROMPT_BUDGET = 0.3   # 푸시용 카드 읽기 상한(초). 넘으면 prompt_id 없이 보낸다(알림 속도 우선).


class App:
    def __init__(self, cfg, hc, devices, pusher, runner=None, clock=time.monotonic, push_log=None):
        self.cfg = cfg
        self.hc = hc
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

    def parse_screen(self, text):
        return parse_prompt(text, plan_reader=self.plan_reader)

    def _prompt_for_push(self, tid):
        """blocked 알림용 카드: hc read 한 번 + 파싱, PUSH_PROMPT_BUDGET 안에 못 끝나면 None."""
        box = {}

        def work():
            try:
                box["p"] = self.parse_screen((self.hc.read(tid, 0) or {}).get("text", ""))
            except Exception:  # noqa: BLE001 - 카드 실패가 알림을 막지 않게
                box["p"] = None
        th = threading.Thread(target=work, name="push-prompt", daemon=True)
        th.start()
        th.join(PUSH_PROMPT_BUDGET)
        if th.is_alive():
            log.info("카드 읽기 %dms 초과 %s, prompt_id 없이 보낸다", int(PUSH_PROMPT_BUDGET * 1000), tid)
        return box.get("p")

    def refresh(self, tid, event_state=None, initial=False, name=None, detected=None):
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
                turn = info.get("turn")
                st = status_of(info)
                if event_state == "done" and st.get("state") != "done":
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
            self.refresh(tid, initial=initial, name=t.get("name"))
        for tid in self.tracker.known() - live:
            self.tracker.forget(tid)
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
                st = status_of(info)
                if (st.get("state"), st.get("msg")) != key:
                    continue  # 그 사이 풀렸거나 바뀌었다: 다음 폴링이 판정한다
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
        if typ in ("status", "turn"):
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
            started = time.time()
            try:
                proc = popen(self.hc.events_argv(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             stdin=subprocess.DEVNULL, text=True, bufsize=1, shell=False)
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
        self.require_terminal(tid)
        info = self.get_info(tid)
        if not is_claude(info):
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
                    "claude": is_claude(t), "preview": None, "blocked_since": None}
            try:
                info = self.hc.get(tid)
            except HcError:
                info = None
            if isinstance(info, dict):
                for k in ("name", "agent", "state"):
                    if info.get(k) is not None:
                        item[k] = info.get(k)
                item["cwd"] = live_cwd(info) or item["cwd"]
                item["status"] = info.get("status") if isinstance(info.get("status"), dict) else None
                item["status_ms"] = info.get("status_ms")
                item["title"] = info.get("title")
                item["claude"] = is_claude(info)
                if item["claude"]:
                    item["preview"] = terminal_preview(info, self.home, self.runner)
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
                    idx = approve_index(labels)
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
            keys = decide_answer(info, action, expect)
            self._keys(tid, keys)
        log.info("answer %s %s", tid, action)
        return {"ok": True, "keys": keys}

    # --- 메뉴 카드
    def current_prompt(self, tid, info):
        if status_of(info).get("state") != "blocked":
            return None
        try:
            r = self.hc.read(tid, 0)
        except HcError:
            raise ApiError(502, "hc_failed")
        return self.parse_screen((r or {}).get("text", ""))

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
        info = self.require_claude(tid)
        sid, path = find_transcript(info, self.home, runner=self.runner)
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
                    if e.name.startswith(".") or (p == self.home and e.name in HIDDEN_HOME_DIRS):
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
            if r is not None and r not in recent and os.path.isdir(os.path.join(self.home, r)):
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
                r = self.hc.run_claude(p, prompt)
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
            self.require_claude(tid)
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
        if not isinstance(tok, str) or not DEVICE_TOKEN_RE.match(tok):
            raise ApiError(400, "bad_token")
        if env not in ("sandbox", "production"):
            raise ApiError(400, "bad_env")
        sig = body.get("relay_sig")
        if sig is not None and (not isinstance(sig, str) or not RELAY_SIG_RE.match(sig)):
            raise ApiError(400, "bad_relay_sig")
        self.devices.add(tok.lower(), env, relay_sig=sig)
        log.info("기기 등록 %s %s%s", short_token(tok), env, " (중계 서명 있음)" if sig else "")
        return {"ok": True}

    def api_delete_device(self, body):
        tok = body.get("token")
        if not isinstance(tok, str) or not DEVICE_TOKEN_RE.match(tok):
            raise ApiError(400, "bad_token")
        removed = self.devices.remove(tok.lower())
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
        if self.push_mode() == "none":
            raise ApiError(409, "push_unavailable")
        dev = next((d for d in self.devices.list() if device_id(d["token"]) == did), None)
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
AGENT_EXTRA_DIRS = ("~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin", "~/.bun/bin", "~/.npm-global/bin",
                    "~/.volta/bin", "~/.cargo/bin", "~/.opencode/bin", "~/.grok/bin", "~/.cursor/bin")
AGENT_CATALOG_TTL = 30.0


def find_executable(name, dirs):
    for d in dirs:
        if not d or not os.path.isabs(d):  # 상대 경로·빈 항목은 서버 작업 폴더의 파일을 실행하게 되므로 제외
            continue
        p = os.path.join(d, name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def agent_search_dirs(home, path_env=None, extra=AGENT_EXTRA_DIRS):
    path_env = os.environ.get("PATH", "") if path_env is None else path_env
    dirs = [d for d in path_env.split(os.pathsep) if d]
    for d in extra:
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
        self._at = 0.0
        self._lock = threading.Lock()

    def installed(self):
        with self._lock:
            now = self.clock()
            if self._cache is None or now - self._at >= self.ttl:
                dirs = agent_search_dirs(self.home, self.path_env, self.extra_dirs)
                self._cache = {a[0]: find_executable(a[2], dirs) is not None for a in AGENT_CATALOG}
                self._at = now
            return dict(self._cache)

    def entries(self):
        inst = self.installed()
        return [{"id": a[0], "name": a[1], "installed": inst[a[0]], "prompt": a[3]} for a in AGENT_CATALOG]

    def launch_argv(self, agent_id, prompt=None):
        if not isinstance(agent_id, str) or agent_id not in AGENT_BY_ID:
            raise ApiError(400, "unknown_agent")
        if not self.installed()[agent_id]:
            raise ApiError(400, "not_installed")
        return agent_argv(agent_id, prompt)


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

    def _body(self):
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
        if n > MAX_BODY:
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
                body = self._body() if method in ("POST", "DELETE") else {}
                tid = urllib.parse.unquote(mt.group(1)) if mt.groups() else None
                if name == "health":
                    res = {"ok": True, "name": machine_name()}
                    if app.cfg.get("push_relay_url"):
                        res["push_relay_url"] = app.cfg["push_relay_url"]
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
    allow_reuse_address = True


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
    sent = app.pusher.push(payload)
    print("시험 푸시: 기기 %d대 중 %d대 성공" % (len(devs), sent))
    return 0 if sent == len(devs) and devs else 1


def cmd_pair(out=None, cfg=None, runner=None):
    out = out or sys.stdout
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
    print(qr_halfblocks(qr_encode(link)), file=out)
    print("", file=out)
    print("아이폰 카메라로 QR을 찍거나 이 주소를 열어라(토큰이 들어 있다):", file=out)
    print(link, file=out)
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "serve"
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
    print("사용법: herdr_remote.py [init|serve|test-push|pair]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
