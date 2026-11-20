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

환경변수
  HERDR_REMOTE_CONFIG  설정 파일(기본 ~/.config/herdr-remote/config.json). devices.json은 같은 폴더.
  HERDR_REMOTE_LOG     로그 파일(기본 ~/Library/Logs/herdr-remote.log, 1MB 회전)
"""
import base64
import hashlib
import hmac
import http.server
import ipaddress
import json
import logging
import logging.handlers
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.parse

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
}

HC_TIMEOUT = 10
MAX_BODY = 64 * 1024
POLL_INTERVAL = 15
BIND_RETRY = 10
EVENTS_MAX_BACKOFF = 30
JWT_TTL = 50 * 60

ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
DEVICE_TOKEN_RE = re.compile(r"^[0-9a-fA-F]{32,200}$")
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
    cfg["port"] = int(cfg["port"])
    cfg["_path"] = path
    cfg["_dir"] = os.path.dirname(path)
    return cfg


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

    def events_argv(self):
        return [self.path, "events"]


def status_of(info):
    s = (info or {}).get("status")
    return s if isinstance(s, dict) else {}


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


def _tool_summary(name, inp):
    if not isinstance(inp, dict):
        return ""
    for k in ("command", "file_path", "path", "pattern", "url", "query", "description", "prompt"):
        v = inp.get(k)
        if isinstance(v, str) and v.strip():
            return _cap(v.strip().split("\n", 1)[0], 200)
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
                            t["output"] = _cap(_result_text(c.get("content")))
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
    cwd = sess.get("cwd") if isinstance(sess.get("cwd"), str) else info.get("cwd")
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


# ---------------------------------------------------------------- 폴더

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


SUBTITLES = {"HR_PERMISSION": "허락 필요", "HR_PERMISSION_ALWAYS": "허락 필요", "HR_BLOCKED": "답 필요",
             "HR_DONE": "끝남", "HR_EXITED": "종료"}


def build_payload(tid, name, state, status, prompt=None, exit_code=None):
    """푸시 본문. 카드 내용은 넣지 않는다(앱이 열 때 /prompt로 가져온다): prompt_id·kind·category만."""
    status = status or {}
    msg = status.get("msg")
    cat = push_category(state, status, prompt)
    if state == "done":
        body = msg or "작업이 끝났어"
    elif state == "exited":
        msg = None
        body = "터미널이 끝났어" + ("" if exit_code is None else " (exit %s)" % exit_code)
    else:
        body = msg or "답이 필요해"
    body = body[:200]
    payload = {
        "aps": {
            "alert": {"title": name or tid, "subtitle": SUBTITLES[cat], "body": body},
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

    def add(self, token, env):
        with self.lock:
            devs = [d for d in self._load() if d["token"] != token]
            devs.append({"token": token, "env": env, "added": int(time.time())})
            write_private(self.path, devs)

    def remove(self, token):
        with self.lock:
            devs = self._load()
            keep = [d for d in devs if d["token"] != token]
            if len(keep) != len(devs):
                write_private(self.path, keep)
                return True
            return False


class ApnsPusher:

    def __init__(self, cfg, devices, runner=None, curl="/usr/bin/curl", openssl=None, clock=time.time):
        self.cfg = cfg
        self.devices = devices
        self.runner = runner or subprocess.run
        self.curl = curl
        self.openssl = openssl
        self.clock = clock
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
            try:
                code, reason = self.send_one(d, payload)
            except Exception as e:  # noqa: BLE001 - 한 기기 실패가 다른 기기를 막지 않게
                log.warning("푸시 실패 %s: %s", short_token(d.get("token")), type(e).__name__)
                continue
            if code == 200:
                sent += 1
                log.info("푸시 %s → %s 200", payload.get("terminal"), short_token(d["token"]))
            elif code == 410 or reason in ("BadDeviceToken", "Unregistered"):
                self.devices.remove(d["token"])
                log.info("푸시 %s: %d %s, 기기 삭제", short_token(d["token"]), code, reason)
            else:
                log.warning("푸시 %s: %d %s", short_token(d["token"]), code, reason)
        return sent


# ---------------------------------------------------------------- 전이 판정

class Tracker:
    """터미널마다 마지막 관측 (state, msg)를 기억하고, 바뀌었고 새 state가 blocked/done이면 알린다.

    - 같은 관측의 반복은 무시한다. blocked→working→blocked(같은 msg)는 새 막힘이라 다시 알린다.
    - 시작 직후 첫 폴링(initial): blocked는 알린다(재시작 중 막힌 걸 놓치지 않게), done은 기준선만.
    - done은 hc의 turn 번호당 한 번만 알린다(이벤트의 done과 폴링의 done이 겹쳐도 1건).
    """

    def __init__(self, notify, prompt_for=None):
        self.notify = notify
        self.prompt_for = prompt_for
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

    def known(self):
        with self.lock:
            return set(self.last)

    def known_claude(self):
        with self.lock:
            return set(self.claude_seen)


# ---------------------------------------------------------------- 앱

PUSH_PROMPT_BUDGET = 0.3   # 푸시용 카드 읽기 상한(초). 넘으면 prompt_id 없이 보낸다(알림 속도 우선).


class App:
    def __init__(self, cfg, hc, devices, pusher, runner=None, clock=time.monotonic):
        self.cfg = cfg
        self.hc = hc
        self.devices = devices
        self.pusher = pusher
        self.runner = runner or subprocess.run
        self.clock = clock
        self.home = os.path.realpath(os.path.expanduser(cfg.get("home") or "~"))
        self.plan_reader = make_plan_reader(self.home)
        self.tracker = Tracker(self._notify, prompt_for=self._prompt_for_push)
        self._refresh_lock = threading.Lock()
        self._term_locks = {}
        self._term_locks_guard = threading.Lock()
        self._pending = []
        self._detected = None
        self.stop = threading.Event()

    def _notify(self, payload):
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
            item = {"id": tid, "name": t.get("name"), "cwd": t.get("cwd"), "agent": t.get("agent"),
                    "state": t.get("state"), "status": None, "status_ms": None, "title": None,
                    "claude": is_claude(t)}
            try:
                info = self.hc.get(tid)
            except HcError:
                info = None
            if isinstance(info, dict):
                for k in ("name", "cwd", "agent", "state"):
                    if info.get(k) is not None:
                        item[k] = info.get(k)
                item["status"] = info.get("status") if isinstance(info.get("status"), dict) else None
                item["status_ms"] = info.get("status_ms")
                item["title"] = info.get("title")
                item["claude"] = is_claude(info)
            out.append(item)
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
                    if e.name.startswith("."):
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
            cwd = t.get("cwd")
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
        try:
            r = self.hc.run_claude(p, prompt)
        except HcError as e:
            raise ApiError(502, "hc_failed", detail=str(e)[:200])
        addr = (r or {}).get("address")
        if not isinstance(addr, str) or not ID_RE.match(addr):
            raise ApiError(502, "hc_failed")
        log.info("새 에이전트 %s", addr)
        return {"id": addr}

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
        self.devices.add(tok.lower(), env)
        log.info("기기 등록 %s %s", short_token(tok), env)
        return {"ok": True}

    def api_delete_device(self, body):
        tok = body.get("token")
        if not isinstance(tok, str) or not DEVICE_TOKEN_RE.match(tok):
            raise ApiError(400, "bad_token")
        removed = self.devices.remove(tok.lower())
        return {"ok": True, "removed": removed}


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
    ("POST", re.compile(r"^/api/agents$"), "agents"),
    ("POST", re.compile(r"^/api/terminals/([^/]+)/keys$"), "keys"),
    ("POST", re.compile(r"^/api/terminals/([^/]+)/send$"), "send"),
    ("POST", re.compile(r"^/api/devices$"), "add_device"),
    ("DELETE", re.compile(r"^/api/devices$"), "delete_device"),
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
                    res = {"ok": True}
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
                elif name == "agents":
                    res = app.api_agents(body)
                elif name == "keys":
                    res = app.api_keys(tid, body)
                elif name == "send":
                    res = app.api_send(tid, body)
                elif name == "add_device":
                    res = app.api_add_device(body)
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
    pusher = ApnsPusher(cfg, devices)
    return App(cfg, Hc(cfg["hc_path"]), devices, pusher)


def cmd_serve():
    cfg = load_config()
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
    log.info("시작 %s:%d", cfg["bind"], cfg["port"])
    app.poll(initial=True)
    threading.Thread(target=app.events_loop, name="events", daemon=True).start()
    threading.Thread(target=app.poll_loop, name="poll", daemon=True).start()
    try:
        srv.serve_forever()
    finally:
        app.stop.set()
    return 0


def cmd_test_push():
    cfg = load_config()
    app = build_app(cfg)
    if not app.pusher.available():
        print("APNs 키 파일이 없다: %s" % cfg["apns_key_path"], file=sys.stderr)
        return 1
    devs = app.devices.list()
    payload = build_payload("test", "herdr-remote", "done", {"msg": "시험 알림"})
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
