#!/bin/sh
# Sheltie 설치기(macOS). 사용: sh install.sh [--dry-run] [--uninstall] [--no-hooks] [--no-hc] [--no-auto-update] [--no-wrap-claude] [--update]
#   --no-wrap-claude: 셸 설정(~/.zshrc, 있으면 ~/.bashrc·~/.bash_profile)에 `claude`를 `sheltie run claude`로 감싸는 셸 함수를 넣지 않는다.
#   기본은 터미널에서 묻고(Y), 터미널이 없으면 넣는다. 끄면 설정 wrap_claude: false로 남아 다시 설치해도 묻지도 넣지도 않는다(설정에서 지우면 다시 묻는다).
#   함수는 `# >>> sheltie: claude >>>` ~ `# <<< sheltie: claude <<<` 사이에만 있고 해제(--uninstall) 때 지운다. 심볼릭 링크인 rc 파일은 건드리지 않는다.
#   원래 claude로 띄우려면 `command claude` 또는 `SHELTIE_NO_WRAP=1 claude`. --update(자동 업데이트)는 셸 설정을 건드리지 않는다.
#   --no-auto-update: 서버 자동 업데이트를 끈다(설정 auto_update: false). 한 번 끄면 다시 설치해도 그대로다(설정에서 지우면 켜진다).
#   --update: 서버 자동 업데이트가 부른다(사람이 쓰지 않는다). SHELTIE_SRC의 새 서버로 바꾸고, 설정·토큰·기기는 그대로 두고,
#   tailscale·QR·hc 설치를 건너뛰고, 훅을 다시 등록하고, 서비스를 다시 시작한다. SHELTIE_UPDATE_WAIT초(기본 120) 안에
#   새 버전이 /api/health에 답하지 않으면 sheltie.py.prev로 되돌린다. 결과는 설정 폴더 update_status.json에 남긴다.
#   SHELTIE_LABEL(launchd 레이블, 기본 com.hyperfocuslab.sheltie): 시험용. 실제 서비스와 겹치지 않게 다른 이름으로 돌린다.
#   --no-hooks: Claude Code·Codex·Qwen Code 훅과 OpenCode 플러그인 등록(해제)을 건너뜀. hc(herdr-core)가 없어도 설치는 계속한다(공식 훅으로만 동작).
#   --no-hc: hc(herdr-core)가 없어도 자동 설치하지 않는다(SHELTIE_NO_HC=1과 같다). 없으면 고정 버전(HC_PIN_VERSION) 파일을 받아
#   이 파일에 박힌 SHA256으로 검증한 뒤 ~/.local/bin에 설치한다. 실패해도 설치는 계속한다. 해제는 herdr-core를 지우지 않는다.
#   원래 주소(HC_BASE_URL)에서 못 받거나 해시가 다르면 우리 미러(HC_MIRROR_URL)에서 같은 파일을 같은 해시로 받는다.
#   시험용 환경변수 SHELTIE_HC_URL(받을 파일: https URL 또는 로컬 경로, http 거부. 주면 미러로 넘어가지 않는다), SHELTIE_HC_SHA256(기대 해시 덮어쓰기)
#   `sheltie` 명령: ~/.local/bin/sheltie 를 만든다(예: sheltie run gemini). 표시 없는 같은 이름 파일은 건드리지 않는다.
#   환경변수: SHELTIE_SRC(서버 파일 https URL 또는 로컬 경로. 기본: 옆의 server/ 파일, 없으면 최신 서버 릴리스), SHELTIE_HC(hc 경로),
#   SHELTIE_PORT(기본 8790), SHELTIE_TAILSCALE(tailscale CLI 경로),
#   SHELTIE_SKIP_TAILSCALE=1(tailscale 점검·serve·pair 건너뜀, 시험용),
#   SHELTIE_SYS_AGENTS_DIR(시스템 LaunchAgents 폴더, 기본 /Library/LaunchAgents, 시험용)
#   이전 설치가 있으면 서비스를 내리고 설정 폴더를 새 위치로 옮긴다.
set -eu

# herdr-core 고정 버전. 바꾸면 server/sheltie.py HC_TESTED_VERSIONS도 같이, 실측 근거를 docs/plans/standalone-runner.md에 적는다.
HC_PIN_VERSION="0.1.21"
HC_SHA_MACOS_AARCH64="1ab38dd9270f63a8f794d6d60e8702eb0d1c85a72bda85d735e5eea43f05ffed"
HC_SHA_MACOS_X86_64="e377e1d342cddce0346da5b2f3c281b9701e4731c00024c7c5028265e77a217f"
HC_BASE_URL="https://releases.herdr.dev/core/$HC_PIN_VERSION"
# 원래 주소가 실패할 때 쓰는 미러(파일 이름·해시가 같다). install.ps1의 HC_MIRROR_URL과 같아야 한다(테스트가 비교).
HC_MIRROR_URL="https://github.com/hyperfocuslab/sheltie/releases/download/hc-$HC_PIN_VERSION"
# 서버 파일 기본 출처: 최신 서버 릴리스(ops/release-server.sh)의 자산. main이 아니라 릴리스된 코드만 깔린다.
# 같은 곳의 manifest.json 해시와 맞아야 설치한다. 저장소 이름이 바뀌면 이 한 줄만 고친다.
DEFAULT_SRC="https://github.com/hyperfocuslab/sheltie/releases/latest/download/sheltie.py"
LABEL="${SHELTIE_LABEL:-com.hyperfocuslab.sheltie}"
case "$LABEL" in *[!A-Za-z0-9.-]*|"") printf '%s\n' "SHELTIE_LABEL은 영문·숫자·점·하이픈만: $LABEL" >&2; exit 2 ;; esac
# 서버 자동 업데이트(--update) 결과 파일(설정 폴더). server/sheltie.py UPDATE_STATUS_FILE과 같다(테스트가 비교).
UPDATE_STATUS_NAME="update_status.json"
DATA_DIR="$HOME/.local/share/sheltie"
SERVER="$DATA_DIR/sheltie.py"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST="$PLIST_DIR/$LABEL.plist"
PLIST_COPY="$DATA_DIR/$LABEL.plist"
SYS_PLIST="${SHELTIE_SYS_AGENTS_DIR:-/Library/LaunchAgents}/$LABEL.plist"
LOG="$HOME/Library/Logs/sheltie.log"
CONFIG_DIR="$HOME/.config/sheltie"
CONFIG="$CONFIG_DIR/config.json"
# 이전 설치 경로. 설치할 때 옮기고 정리한다.
OLD_LABEL="com.hyperfocuslab.herdr-remote"
OLD_DATA_DIR="$HOME/.local/share/herdr-remote"
OLD_CONFIG_DIR="$HOME/.config/herdr-remote"
OLD_LOG="$HOME/Library/Logs/herdr-remote.log"
OLD_SERVER_NAME="herdr_remote.py"
OLD_PLIST="$PLIST_DIR/$OLD_LABEL.plist"
OLD_SYS_PLIST="${SHELTIE_SYS_AGENTS_DIR:-/Library/LaunchAgents}/$OLD_LABEL.plist"
# OpenCode 전역 설정 폴더(XDG_CONFIG_HOME 우선, OpenCode 1.19.6 로더와 같다). 플러그인은 그 안 plugins/
OPENCODE_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/opencode"
UID_N=$(id -u)
DRY=0
MODE=install
NOHOOKS=0
NOHC=0
NOAUTO=0
NOWRAP=0
PYTHON=""
CMD_DIR="$HOME/.local/bin"
CMD_PATH="$CMD_DIR/sheltie"
CMD_MARK="# sheltie-command:"
unset SHELTIE_CONFIG || true

for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    --uninstall) MODE=uninstall ;;
    --no-hooks) NOHOOKS=1 ;;
    --no-hc) NOHC=1 ;;
    --no-auto-update) NOAUTO=1 ;;
    --no-wrap-claude) NOWRAP=1 ;;
    --update) MODE=update ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "알 수 없는 옵션: $a (--dry-run, --uninstall, --no-hooks, --no-hc, --no-auto-update, --no-wrap-claude, --update)" >&2; exit 2 ;;
  esac
done

say() { printf '%s\n' "$*"; }
die() { code=$1; shift; printf '%s\n' "$*" >&2; exit "$code"; }
# 변경 명령은 여기로만 실행한다. dry-run이면 출력만 한다. 인자는 쉘을 거치지 않는다.
run() {
  if [ "$DRY" = 1 ]; then printf '+ %s\n' "$*"; return 0; fi
  "$@"
}

PORT=${SHELTIE_PORT:-}
case "$PORT" in
  "") ;;
  *[!0-9]*) die 2 "SHELTIE_PORT는 숫자여야 한다: $PORT" ;;
  *) if [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then die 2 "포트 범위는 1~65535: $PORT"; fi ;;
esac

find_tailscale() {
  if [ -n "${SHELTIE_TAILSCALE:-}" ]; then
    [ -x "$SHELTIE_TAILSCALE" ] && { printf '%s' "$SHELTIE_TAILSCALE"; return 0; }
    return 1
  fi
  if p=$(command -v tailscale 2>/dev/null) && [ -x "$p" ]; then printf '%s' "$p"; return 0; fi
  for p in /opt/homebrew/bin/tailscale /Applications/Tailscale.app/Contents/MacOS/Tailscale /usr/local/bin/tailscale; do
    [ -x "$p" ] && { printf '%s' "$p"; return 0; }
  done
  return 1
}

config_port() { # 설정 파일의 port(새 설정이 없으면 이전 설치의 설정). 없으면 8790
  _c=$CONFIG; [ -f "$_c" ] || _c="$OLD_CONFIG_DIR/config.json"
  _cp=${PYTHON:-python3}  # 업데이트 때는 서비스의 파이썬
  if [ -f "$_c" ] && command -v "$_cp" >/dev/null 2>&1; then
    "$_cp" -c 'import json,sys; print(int(json.load(open(sys.argv[1])).get("port",8790)))' "$_c" 2>/dev/null && return 0
  fi
  echo 8790
}

old_installed() { [ -e "$OLD_PLIST" ] || [ -e "$OLD_SYS_PLIST" ] || [ -d "$OLD_DATA_DIR" ]; }

# 이전 설치의 서비스를 내리고 plist를 지운다. 시스템 위치 plist는 sudo가 필요해 안내만 한다.
old_service_down() {
  run launchctl bootout "gui/$UID_N/$OLD_LABEL" 2>/dev/null || true
  run rm -f "$OLD_PLIST" "$OLD_DATA_DIR/$OLD_LABEL.plist"
  [ ! -e "$OLD_SYS_PLIST" ] || say "이전 설치의 시스템 plist가 남아 있다. 지우려면: sudo rm '$OLD_SYS_PLIST'"
}

# 설정 폴더 안을 가리키는 절대·~ 경로(apns_key_path 등)를 새 폴더로 고친다.
FIXPATHS_PY='
import json, os, sys
p, old, new = sys.argv[1:4]
home = os.path.expanduser("~")
nc = os.path.normcase
def fix(v):
    if isinstance(v, dict):
        return {k: fix(x) for k, x in v.items()}
    if isinstance(v, list):
        return [fix(x) for x in v]
    if not isinstance(v, str):
        return v
    e = os.path.expanduser(v)
    if not (nc(e) == nc(old) or nc(e).startswith(nc(old) + os.sep) or nc(e).startswith(nc(old) + "/")):
        return v
    rest = e[len(old):]
    if v.startswith("~") and nc(new).startswith(nc(home)):
        rel = new[len(home):]
        if v[1:2] == "/":
            rel = rel.replace("\\", "/")
        return "~" + rel + rest
    return new + rest
c = json.load(open(p, encoding="utf-8-sig"))
n = fix(c)
if n != c:
    tmp = p + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(n, f, indent=2)
    os.replace(tmp, p)
'

# 이전 설정 폴더가 링크면 옮길 수 없다. 새 위치에 새 토큰을 만들면 연결이 끊기므로 아무것도 바꾸기 전에 멈춘다.
old_check_link() {
  if [ -L "$OLD_CONFIG_DIR" ] && [ ! -e "$CONFIG_DIR" ] && [ ! -L "$CONFIG_DIR" ]; then
    die 1 "$OLD_CONFIG_DIR 가 링크다. 링크 대상 폴더를 $CONFIG_DIR 로 옮기거나 $CONFIG_DIR 링크를 새로 만든 뒤 다시 실행해라."
  fi
}

# 이전 서비스를 되살린다(설정 이동 실패 때). 사용자·시스템 위치 plist 중 있는 것으로.
old_service_restore() {
  for _p in "$OLD_PLIST" "$OLD_SYS_PLIST"; do
    [ ! -f "$_p" ] || launchctl bootstrap "gui/$UID_N" "$_p" 2>/dev/null || true
  done
}

# 새 서버 파일을 둔 뒤 부른다: 이전 서비스 중지(같은 포트) → 설정 폴더 이동(새 폴더가 없을 때만).
# 못 옮기면 이전 서비스를 되살리고 멈춘다. 이전 파일 삭제는 health 확인 뒤(old_cleanup).
old_stop_and_move() {
  if old_installed; then
    say "이전 설치를 찾았다. 서비스를 내리고 새 위치로 옮긴다."
    run launchctl bootout "gui/$UID_N/$OLD_LABEL" 2>/dev/null || true
  fi
  if [ -d "$OLD_CONFIG_DIR" ] && [ ! -L "$OLD_CONFIG_DIR" ]; then
    if [ -e "$CONFIG_DIR" ] || [ -L "$CONFIG_DIR" ]; then
      say "경고: $CONFIG_DIR 이 이미 있어 이전 설정($OLD_CONFIG_DIR)은 옮기지 않고 그대로 둔다."
    elif [ "$DRY" = 1 ]; then
      say "+ mv $OLD_CONFIG_DIR $CONFIG_DIR (안의 경로도 새 폴더로)"
    else
      if ! { mkdir -p "$(dirname "$CONFIG_DIR")" && mv "$OLD_CONFIG_DIR" "$CONFIG_DIR"; }; then
        old_service_restore
        die 1 "이전 설정 폴더를 옮기지 못했다: $OLD_CONFIG_DIR -> $CONFIG_DIR. 아무것도 지우지 않았다. 권한을 확인하고 다시 실행해라."
      fi
      [ ! -f "$CONFIG" ] || "$PYTHON" -c "$FIXPATHS_PY" "$CONFIG" "$OLD_CONFIG_DIR" "$CONFIG_DIR" \
        || die 1 "설정 폴더는 옮겼지만 안의 경로를 고치지 못했다: $CONFIG (apns_key_path 등을 $CONFIG_DIR 로 직접 고쳐라)"
      say "설정을 옮겼다: $OLD_CONFIG_DIR -> $CONFIG_DIR"
    fi
  fi
}

# 새 서버 health 확인 뒤 부른다: 이전 plist·로그 정리. 훅이 아직 이전 서버 파일을 부르면 설치 폴더는 남긴다.
old_cleanup() {
  if old_installed; then
    old_service_down
    _left=""
    # dry-run은 훅 파일을 안 바꾸므로 훅을 고칠 예정이면(--no-hooks가 아니면) 고쳐졌다고 본다.
    [ "$DRY" = 1 ] && [ "$NOHOOKS" != 1 ] || for _f in "$HOME/.claude/settings.json" "$HOME/.codex/hooks.json" \
        "$HOME/.qwen/settings.json" "$OPENCODE_DIR/plugins/sheltie.js" "$HOME/.gemini/config/hooks.json"; do
      [ ! -f "$_f" ] || ! grep -F "$OLD_SERVER_NAME" "$_f" >/dev/null 2>&1 || _left="${_left:+$_left, }$_f"
    done
    if [ -n "$_left" ] && [ -d "$OLD_DATA_DIR" ]; then
      say "경고: 훅이 아직 이전 서버 파일을 부른다($_left). $OLD_DATA_DIR 은 지우지 않았다. 훅과 함께 'sh install.sh'를 다시 실행하거나 그 파일에서 '$OLD_SERVER_NAME' hook 항목을 지워라." >&2
    else
      run rm -rf "$OLD_DATA_DIR"
    fi
  fi
  if [ -f "$OLD_LOG" ]; then
    if [ "$DRY" != 1 ] && [ ! -e "$LOG" ]; then mv -f "$OLD_LOG" "$LOG" 2>/dev/null || true; fi
    run rm -f "$OLD_LOG" "$OLD_LOG.1" "$OLD_LOG.2"
  fi
}

# Claude Code·Codex·Qwen Code·agy 훅 병합과 OpenCode 플러그인 쓰기 스크립트(이 한 곳만 고친다. install.ps1에 같은 글자로 들어 있고 테스트가 비교한다).
HOOKS_PY=$(cat <<'HOOKS_PY_EOF'
# ---- hooks_merge.py begin (install.sh and install.ps1 embed this identical text; a test compares them)
# usage: hooks_merge.py <install|uninstall> <claude|codex|qwen|opencode|agy> <settings-file> <python-exe> <server-file> <posix|windows> [dry]
# For opencode <settings-file> is the plugin file (plugins/sheltie.js): written or removed whole, only when it carries
# our mark on its first line (another file with that name is left alone: broken).
# For agy <settings-file> is ~/.gemini/config/hooks.json: its top-level keys are hook names (no "hooks" wrapper);
# only the key "sheltie" is written or removed, other names are kept.
# Prints exactly one status word: added | updated | unchanged | removed | absent | broken | error
import json, os, shutil, sys

action, target, path, py, server, flavor = sys.argv[1:7]
dry = len(sys.argv) > 7 and sys.argv[7] == "dry"
MARK = "sheltie.py"
OLD_MARKS = ("herdr_remote.py",)  # previous install path: replaced on install, removed on uninstall


def ours(cmd):
    if not isinstance(cmd, str):
        return False
    for m in (MARK,) + OLD_MARKS:
        i = cmd.find(m)
        if i >= 0 and cmd[i + len(m):].startswith(('" hook ', "' hook ", '" hook-wait ', "' hook-wait ")):
            return True
    return False


def spec():
    # (event, matcher or None, timeout seconds)
    if target == "claude":
        return [("PermissionRequest", "*", 600), ("PreToolUse", "AskUserQuestion|ExitPlanMode", 600),
                ("SessionStart", None, 10), ("SessionEnd", None, 10), ("UserPromptSubmit", None, 10), ("Stop", None, 10),
                # Tool-finished signals: Claude Code keeps the PermissionRequest hook alive after the PC answers,
                # so the server frees the held request when the same tool ends (short timeout, no output).
                ("PostToolUse", "*", 5), ("PostToolUseFailure", "*", 5)]
    if target == "qwen":
        # Qwen Code reads hook timeouts in seconds (default 60): PermissionRequest must outlast the server hold.
        return [("PermissionRequest", "*", 600), ("SessionStart", None, 10), ("SessionEnd", None, 10),
                ("UserPromptSubmit", None, 10), ("Stop", None, 10)]
    return [("PermissionRequest", "*", 600), ("SessionStart", None, 10), ("SessionEnd", None, 3),
            ("UserPromptSubmit", None, 10), ("Stop", None, 10)]


def handler(timeout):
    h = {"type": "command"}
    if flavor == "windows":
        if target in ("claude", "qwen"):
            h["command"] = '& "%s" "%s" hook %s' % (py, server, target)
            h["shell"] = "powershell"
        else:
            # Codex does not document its Windows shell: go through powershell.exe explicitly.
            inner = "& '%s' '%s' hook codex" % (py.replace("'", "''"), server.replace("'", "''"))
            h["command"] = 'powershell.exe -NoProfile -Command "%s"' % inner
    else:
        h["command"] = '"%s" "%s" hook %s' % (py, server, target)
    h["timeout"] = timeout
    return h


def wait_handler():
    # Claude Stop, async: waits for a message sent from the phone; exit 2 + stderr wakes the idle session.
    h = {"type": "command"}
    if flavor == "windows":
        # PowerShell -Command would turn a native exit code 2 into 1: pass it through.
        h["command"] = '& "%s" "%s" hook-wait claude; exit $LASTEXITCODE' % (py, server)
        h["shell"] = "powershell"
    else:
        h["command"] = '"%s" "%s" hook-wait claude' % (py, server)
    h["asyncRewake"] = True
    h["timeout"] = 86400
    return h


def strip(groups):
    """Remove our handlers from an event's group list. Returns (new groups, index where ours was, found)."""
    out, pos, found = [], None, False
    for g in groups:
        hs = g.get("hooks") if isinstance(g, dict) else None
        if not isinstance(hs, list):
            out.append(g)
            continue
        keep = [h for h in hs if not (isinstance(h, dict) and ours(h.get("command")))]
        if len(keep) != len(hs):
            found = True
            if pos is None:
                pos = len(out)
            if keep:
                g = dict(g)
                g["hooks"] = keep
                out.append(g)
        else:
            out.append(g)
    return out, pos, found


PLUGIN_MARK = "// sheltie-opencode-plugin"
PLUGIN = r'''// sheltie-opencode-plugin
// Written by the Sheltie installer and removed by its uninstaller. Hands OpenCode permission prompts and turn
// events to the local Sheltie server ("sheltie.py hook opencode") so they can be answered from the phone.
// Any failure is ignored: the OpenCode dialog stays as it is.
import { spawn, spawnSync } from "child_process"
import os from "os"
import path from "path"

const PY = __SHELTIE_PY__
const SERVER = __SHELTIE_SERVER__
const DB = path.join(process.env.XDG_DATA_HOME || path.join(os.homedir(), ".local", "share"), "opencode", "opencode.db")

export const SheltiePlugin = async (input) => {
  let dir = process.cwd()
  let client = null
  if (input) {
    if (typeof input.directory === "string") dir = input.directory
    client = input.client
  }
  const parents = {}
  const busy = {}
  const ran = {}
  const turns = {}
  const live = {}
  const pending = {}
  // A subagent session asks under its own id: show it as its top session, answer with its own id.
  const root = (sid) => {
    let s = sid
    for (let i = 0; i < 20; i++) {
      if (!parents[s]) break
      s = parents[s]
    }
    return s
  }
  const base = (sid, ev) => {
    const r = root(sid)
    live[r] = true
    return { hook_event_name: ev, session_id: r, cwd: dir, opencode_db: DB }
  }
  const send = (body, done) => {
    try {
      const child = spawn(PY, [SERVER, "hook", "opencode"], { stdio: ["pipe", "pipe", "ignore"], windowsHide: true })
      let out = ""
      child.on("error", () => {})
      child.stdout.on("data", (d) => { out += d })
      child.on("close", () => {
        if (done) {
          try { done(out) } catch (e) {}
        }
      })
      child.stdin.on("error", () => {})
      child.stdin.end(JSON.stringify(body))
      return child
    } catch (e) {
      return null
    }
  }
  // Server answer (Claude hook shape) -> OpenCode reply: allow + updatedPermissions = always, allow = once, deny = reject.
  const answer = async (sid, id, out) => {
    delete pending[id]
    let dec = null
    try { dec = JSON.parse(out).hookSpecificOutput.decision } catch (e) { return }
    if (!dec) return
    let response = null
    if (dec.behavior === "allow") {
      response = "once"
      if (Array.isArray(dec.updatedPermissions)) {
        if (dec.updatedPermissions.length > 0) response = "always"
      }
    } else if (dec.behavior === "deny") {
      response = "reject"
    }
    if (!response) return
    if (!client) return
    try {
      await client.postSessionIdPermissionsPermissionId({ path: { id: sid, permissionID: id }, body: { response: response } })
    } catch (e) {}
  }
  process.on("exit", () => {
    for (const sid of Object.keys(live)) {
      try {
        const body = JSON.stringify({ hook_event_name: "SessionEnd", session_id: sid, cwd: dir, reason: "exit" })
        spawnSync(PY, [SERVER, "hook", "opencode"], { input: body, stdio: ["pipe", "ignore", "ignore"], timeout: 3000, windowsHide: true })
      } catch (e) {}
    }
  })
  return {
    event: async ({ event }) => {
      try {
        if (!event) return
        const p = event.properties || {}
        const sid = p.sessionID
        if (typeof sid !== "string") return
        if (event.type === "session.created") {
          const info = p.info || {}
          if (typeof info.parentID === "string") {
            parents[sid] = info.parentID
            return
          }
          send(base(sid, "SessionStart"))
        } else if (event.type === "session.status") {
          if (parents[sid]) return
          let t = null
          if (p.status) t = p.status.type
          if (t === "busy") {
            if (!busy[sid]) {
              busy[sid] = true
              ran[sid] = true
              send(base(sid, "UserPromptSubmit"))
            }
          } else {
            busy[sid] = false
          }
        } else if (event.type === "session.idle") {
          if (parents[sid]) return
          busy[sid] = false
          if (!ran[sid]) return
          ran[sid] = false
          turns[sid] = (turns[sid] || 0) + 1
          const b = base(sid, "Stop")
          b.turn_id = sid + "#" + turns[sid]
          send(b)
        } else if (event.type === "permission.asked") {
          const id = p.id
          if (typeof id !== "string") return
          const b = base(sid, "PermissionRequest")
          b.tool_name = "tool"
          if (typeof p.permission === "string") b.tool_name = p.permission
          let meta = {}
          if (p.metadata) {
            if (typeof p.metadata === "object") meta = p.metadata
          }
          let pats = []
          if (Array.isArray(p.patterns)) pats = p.patterns
          b.tool_input = { patterns: pats, metadata: meta }
          b.always = []
          if (Array.isArray(p.always)) b.always = p.always.filter((x) => typeof x === "string")
          b.request_id = id
          const child = send(b, (out) => answer(sid, id, out))
          if (child) pending[id] = child
        } else if (event.type === "permission.replied") {
          // Answered on the PC (or by us): drop the held request so the server sees the hook go away.
          const c = pending[p.requestID]
          if (c) {
            delete pending[p.requestID]
            try { c.kill() } catch (e) {}
          }
        }
      } catch (e) {}
    },
  }
}
'''


def plugin_main():
    existed = os.path.exists(path)
    cur = None
    if existed:
        try:
            with open(path, encoding="utf-8") as f:
                cur = f.read()
        except (ValueError, OSError):
            return "broken"
        if not cur.startswith(PLUGIN_MARK):
            return "broken"
    if action == "uninstall":
        if not existed:
            return "absent"
        if not dry:
            os.remove(path)
        return "removed"
    text = PLUGIN.replace("__SHELTIE_PY__", json.dumps(py)).replace("__SHELTIE_SERVER__", json.dumps(server))
    if cur == text:
        return "unchanged"
    if not dry:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".sheltie-tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    return "updated" if existed else "added"


AGY_NAME = "sheltie"


def agy_entry():
    def h(timeout):
        if flavor == "windows":
            inner = "& '%s' '%s' hook agy" % (py.replace("'", "''"), server.replace("'", "''"))
            cmd = 'powershell.exe -NoProfile -Command "%s"' % inner
        else:
            cmd = '"%s" "%s" hook agy' % (py, server)
        return {"type": "command", "command": cmd, "timeout": timeout}
    # No PreToolUse: agy has no "only when it would ask" event, so holding every run/write would stall agy on the PC
    # until the phone answers. Approval stays in agy's own flow (the server still handles PreToolUse if added by hand).
    return {"SessionStart": [h(10)], "PreInvocation": [h(10)], "Stop": [h(10)]}


def agy_ours(entry):
    if not isinstance(entry, dict):
        return False
    for groups in entry.values():
        for g in groups if isinstance(groups, list) else []:
            hs = g.get("hooks") if isinstance(g, dict) and isinstance(g.get("hooks"), list) else [g]
            if any(isinstance(h, dict) and ours(h.get("command")) for h in hs):
                return True
    return False


def agy_main():
    doc = {}
    existed = os.path.exists(path)
    if existed:
        try:
            with open(path, encoding="utf-8-sig") as f:
                text = f.read()
            doc = json.loads(text) if text.strip() else {}
        except (ValueError, OSError):
            return "broken"
        if not isinstance(doc, dict):
            return "broken"
    elif action == "uninstall":
        return "absent"
    cur = doc.get(AGY_NAME)
    if cur is not None and not agy_ours(cur):
        return "error" if action == "install" else "absent"  # a hook of that name that is not ours: leave it alone
    if action == "install":
        new = agy_entry()
        if cur == new:
            return "unchanged"
        doc[AGY_NAME] = new
    else:
        if cur is None:
            return "absent"
        del doc[AGY_NAME]
    if not dry:
        if action == "uninstall" and not doc:
            os.remove(path)
            return "removed"
        if existed and not os.path.exists(path + ".sheltie-bak"):
            shutil.copy2(path, path + ".sheltie-bak")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".sheltie-tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(doc, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
    if action == "uninstall":
        return "removed"
    return "updated" if cur is not None else "added"


def main():
    if target == "opencode":
        return plugin_main()
    if target == "agy":
        return agy_main()
    doc = {}
    existed = os.path.exists(path)
    if existed:
        try:
            with open(path, encoding="utf-8-sig") as f:
                text = f.read()
            doc = json.loads(text) if text.strip() else {}
        except (ValueError, OSError):
            return "broken"
        if not isinstance(doc, dict):
            return "broken"
    elif action == "uninstall":
        return "absent"
    before = json.dumps(doc, sort_keys=True)
    hooks = doc.get("hooks")
    if hooks is None:
        hooks = {}
    if not isinstance(hooks, dict) or any(not isinstance(v, list) for v in hooks.values()):
        return "broken"
    had = False
    if action == "install":
        for ev, matcher, timeout in spec():
            groups, pos, found = strip(hooks.get(ev, []))
            had = had or found
            grp = {}
            if matcher is not None:
                grp["matcher"] = matcher
            grp["hooks"] = [handler(timeout)]
            if target == "claude" and ev == "Stop":
                grp["hooks"].append(wait_handler())
            groups.insert(len(groups) if pos is None else pos, grp)
            hooks[ev] = groups
        doc["hooks"] = hooks
    else:
        for ev in list(hooks):
            groups, pos, found = strip(hooks[ev])
            had = had or found
            if groups:
                hooks[ev] = groups
            else:
                del hooks[ev]
        if not had:
            return "absent"
        if hooks:
            doc["hooks"] = hooks
        else:
            doc.pop("hooks", None)
    if json.dumps(doc, sort_keys=True) == before and existed:
        return "unchanged"
    if not dry:
        if existed and not os.path.exists(path + ".sheltie-bak"):
            shutil.copy2(path, path + ".sheltie-bak")
        tmp = path + ".sheltie-tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(doc, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
    if action == "uninstall":
        return "removed"
    return "updated" if had else "added"


try:
    print(main())
except Exception:
    print("error")
# ---- hooks_merge.py end
HOOKS_PY_EOF
)

# hooks_apply <install|uninstall> <python>: 있는 에이전트(~/.claude, ~/.codex, ~/.qwen, ~/.gemini)의 훅 설정 파일에 우리 항목을
# 넣거나 빼고, OpenCode(설정 폴더가 있을 때)는 전역 플러그인 파일(plugins/sheltie.js)을 쓰거나 지운다.
hooks_apply() {
  _act=$1; _py=$2
  _dryarg=""; [ "$DRY" != 1 ] || _dryarg=dry
  _cxhooks=0
  for _t in claude codex qwen opencode agy; do
    _dir="$HOME/.$_t"; _file="$_dir/settings.json"
    case "$_t" in
      codex) _file="$_dir/hooks.json" ;;
      opencode) _dir=$OPENCODE_DIR; _file="$_dir/plugins/sheltie.js" ;;
      agy) _dir="$HOME/.gemini"; _file="$_dir/config/hooks.json" ;;
    esac
    # agy는 첫 실행 전엔 ~/.gemini가 없다: agy 명령이 있으면 등록한다(설치 순서와 상관없이 붙게).
    if [ ! -d "$_dir" ] && ! { [ "$_t" = agy ] && [ "$_act" = install ] && { command -v agy >/dev/null 2>&1 || [ -x "$HOME/.local/bin/agy" ]; }; }; then
      say "($_t: $_dir 없음, 훅 건너뜀)"; continue
    fi
    _res=$("$_py" -c "$HOOKS_PY" "$_act" "$_t" "$_file" "$_py" "$SERVER" posix $_dryarg 2>/dev/null) || _res=error
    _pre=""; [ "$DRY" != 1 ] || _pre="(dry-run) "
    case "$_res" in
      added) say "${_pre}$_t 훅 등록: $_file" ;;
      updated) say "${_pre}$_t 훅 갱신: $_file" ;;
      unchanged) say "$_t 훅이 이미 등록돼 있다: $_file" ;;
      removed) say "${_pre}$_t 훅 해제: $_file (백업 $_file.sheltie-bak 은 남긴다)" ;;
      absent) say "$_t: 우리 훅이 없다($_file)" ;;
      broken) say "경고: $_file 이 올바른 JSON이 아니라 건드리지 않았다. 고친 뒤 다시 실행해라." >&2 ;;
      *) say "경고: $_t 훅 처리에 실패했다($_file). 설치는 계속한다." >&2 ;;
    esac
    case "$_t:$_act:$_res" in codex:install:added|codex:install:updated|codex:install:unchanged) _cxhooks=1 ;; esac
  done
  [ "$_cxhooks" = 1 ] && codex_trust "$_py"
  return 0
}

# codex_trust <python>: Codex는 config.toml에 신뢰가 기록된 훅만 돌린다. 설치(동의) = 신뢰로 보고, 서버의
# codex-trust가 codex app-server에 물어 우리 훅만 신뢰로 기록한다(다른 훅은 건드리지 않는다). 실패해도 설치는 계속한다.
codex_trust() {
  if [ "$DRY" = 1 ]; then say "(dry-run) Codex에 Sheltie 훅 신뢰 등록(codex app-server)"; return 0; fi
  if _ctout=$("$1" "$SERVER" codex-trust 2>&1); then
    if [ -n "$_ctout" ]; then say "$(printf '%s' "$_ctout" | tail -n 1)"; fi
  else
    say "경고: Codex에 Sheltie 훅 신뢰를 등록하지 못했다($(printf '%s' "$_ctout" | tail -n 1)). Codex CLI에서 /hooks, Codex 앱은 설정의 훅 화면에서 Sheltie 훅을 승인해라." >&2
  fi
  return 0
}

# sheltie 명령(~/.local/bin/sheltie)
sq() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }
install_command() {
  if [ -e "$CMD_PATH" ] || [ -L "$CMD_PATH" ]; then
    if ! { [ -f "$CMD_PATH" ] && grep -F "$CMD_MARK" "$CMD_PATH" >/dev/null 2>&1; }; then
      say "$CMD_PATH 가 이미 있고 Sheltie가 만든 파일이 아니라 건드리지 않았다. sheltie 명령은 만들지 않았다."
      return 0
    fi
  fi
  if [ "$DRY" = 1 ]; then say "+ $CMD_PATH 작성(sheltie run <에이전트>)"; return 0; fi
  mkdir -p "$CMD_DIR" || { say "$CMD_DIR 를 만들지 못해 sheltie 명령은 건너뛴다."; return 0; }
  _ctmp=$(mktemp "$CMD_DIR/.sheltie.XXXXXX") || { say "sheltie 명령 파일을 만들지 못했다."; return 0; }
  {
    printf '#!/bin/sh\n%s Sheltie 설치기가 만들고 해제 때 지운다\n' "$CMD_MARK"
    printf 'exec %s %s "$@"\n' "$(sq "$PYTHON")" "$(sq "$SERVER")"
  } > "$_ctmp" && chmod 0755 "$_ctmp" && mv -f "$_ctmp" "$CMD_PATH" \
    || { rm -f "$_ctmp"; say "sheltie 명령을 만들지 못했다."; return 0; }
  case ":$PATH:" in
    *":$CMD_DIR:"*) ;;
    *) say "$CMD_DIR 가 PATH에 없다. 터미널에서 sheltie 명령을 쓰려면 한 번 실행해라: echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc" ;;
  esac
}
# claude 셸 함수(~/.zshrc 등). 터미널에서 평소처럼 `claude`만 쳐도 `sheltie run claude`로 떠서 폰에서 글 보내기·화면 보기까지 된다.
WRAP_BEGIN="# >>> sheltie: claude >>>"
WRAP_END="# <<< sheltie: claude <<<"
wrap_block() { # 표시 줄 포함 블록 전문. 따옴표 here-doc이라 내용은 그대로 나간다(zsh·bash 공통, POSIX에 가깝게)
  cat <<'WRAPEOF'
# >>> sheltie: claude >>>
# Sheltie 설치기가 넣고 해제(--uninstall) 때 지운다. 이 줄과 끝 표시 줄 사이를 고치면 다음 설치 때 덮어쓴다.
# 끄기: 설치기에 --no-wrap-claude. 한 번만 원래대로: `command claude` 또는 `SHELTIE_NO_WRAP=1 claude`
claude() {
  if [ -n "${SHELTIE_NO_WRAP:-}" ] || [ -n "${HC_TERMINAL_ID:-}" ] || ! [ -t 0 ] || ! [ -t 1 ] \
    || ! command -v sheltie >/dev/null 2>&1 \
    || ! { [ -x "$HOME/.local/bin/hc" ] || command -v hc >/dev/null 2>&1; }; then
    command claude "$@"
    return
  fi
  case "${1:-}" in
    agents|attach|auth|auto-mode|config|doctor|gateway|import|install|logs|mcp|migrate-installer|plugin|plugins|purge|respawn|rm|setup-token|stop|kill|ultrareview|update|upgrade)
      command claude "$@"
      return ;;
  esac
  for _sheltie_a in "$@"; do
    case "$_sheltie_a" in
      --) break ;;
      -p|--print|-v|--version|-h|--help|--bg|--background)
        command claude "$@"
        return ;;
    esac
  done
  sheltie run claude "$@"
}
# <<< sheltie: claude <<<
WRAPEOF
}
# wrap_rc <install|remove> <python>: 대상 rc 파일의 블록을 넣거나(있으면 교체) 지운다. 블록 밖 내용·권한은 그대로,
# 같은 폴더 임시 파일에 쓰고 rename으로 바꾼다. 심볼릭 링크인 rc 파일은 건드리지 않고 안내만 한다
# (링크 대상은 dotfiles 저장소일 수 있어 몰래 고치는 것보다 사용자가 직접 붙이는 쪽이 안전하다).
# 대상: ~/.zshrc(install은 없으면 만든다), ~/.bashrc·~/.bash_profile(있을 때만).
wrap_rc() {
  _wb=$(wrap_block)
  HR_BLOCK=$_wb HR_BEGIN=$WRAP_BEGIN HR_END=$WRAP_END HR_DRY=$DRY "$2" -c '
import os, shutil, sys, tempfile
action = sys.argv[1]
home = os.environ["HOME"]
block = os.environ["HR_BLOCK"].encode("utf-8")
begin, end = os.environ["HR_BEGIN"].encode("utf-8"), os.environ["HR_END"].encode("utf-8")
dry = os.environ["HR_DRY"] == "1"
rc = 0
for name in (".zshrc", ".bashrc", ".bash_profile"):
    p = os.path.join(home, name)
    if os.path.islink(p):
        try:
            has = begin in open(p, "rb").read()
        except OSError:
            has = False
        if action == "install" or has:
            print("%s 는 심볼릭 링크라 건드리지 않았다. claude 셸 함수는 직접 넣어라(내용은 install.sh의 wrap_block)." % p)
        continue
    exists = os.path.isfile(p)
    if not exists and not (action == "install" and name == ".zshrc"):
        continue
    try:
        data = open(p, "rb").read() if exists else b""
    except OSError as e:
        print("경고: %s 를 읽지 못했다(%s)." % (p, e), file=sys.stderr); rc = 1; continue
    lines = data.split(b"\n")
    out, i, placed, bad = [], 0, False, False
    while i < len(lines):
        ln = lines[i]
        if ln.rstrip(b"\r") == begin:
            j = i + 1
            while j < len(lines) and lines[j].rstrip(b"\r") != end:
                j += 1
            if j >= len(lines):  # 끝 표시가 없다: 어디까지가 우리 것인지 모르니 파일을 그대로 둔다
                bad = True
                break
            if action == "install":
                if not placed:
                    out.extend(block.split(b"\n"))
                    placed = True
            elif out and out[-1] == b"" and len(out) > 1:  # 넣을 때 앞에 둔 빈 줄도 같이 거둔다
                out.pop()
            i = j + 1
            continue
        out.append(ln)
        i += 1
    if bad:
        print("경고: %s 에 시작 표시만 있고 끝 표시가 없어 건드리지 않았다." % p, file=sys.stderr); rc = 1; continue
    if action == "install" and not placed:
        if out and out[-1] == b"":
            out.pop()
        if out:
            out.append(b"")
        out.extend(block.split(b"\n"))
        out.append(b"")
    new = b"\n".join(out)
    if new == data:
        continue
    if dry:
        print("+ %s %s claude 셸 함수 블록" % (p, "에 넣기:" if action == "install" else "에서 지우기:"))
        continue
    try:
        fd, tmp = tempfile.mkstemp(prefix="." + name + ".", dir=home)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(new)
            if exists:
                shutil.copymode(p, tmp)
            else:
                os.chmod(tmp, 0o644)
            os.replace(tmp, p)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError as e:
        print("경고: %s 를 고치지 못했다(%s)." % (p, e), file=sys.stderr); rc = 1; continue
    print("%s 에 claude 셸 함수를 %s." % (p, "넣었다" if action == "install" else "지웠다"))
sys.exit(rc)
' "$1" || say "경고: 셸 설정(claude 함수) 처리에 실패했다. 설치는 계속한다." >&2
}
# 감쌀지 정한다: --no-wrap-claude → 아니오(설정에 남김), 설정 wrap_claude가 false → 아니오, 터미널이 있으면 묻고(기본 Y), 없으면 예.
# curl | sh에서는 stdin이 파이프라 /dev/tty에서 읽는다. dry-run은 묻지 않고 예로 본다.
wrap_decide() { # 결과는 WRAP(1/0)과 WRAP_SAVE(1이면 설정에 false를 남긴다)
  WRAP=1; WRAP_SAVE=0
  if [ "$NOWRAP" = 1 ]; then WRAP=0; WRAP_SAVE=1; return 0; fi
  if [ "$(config_value wrap_claude)" = false ]; then WRAP=0; return 0; fi
  [ "$DRY" != 1 ] || return 0
  if ( : </dev/tty ) 2>/dev/null; then
    printf 'claude를 Sheltie로 감쌀까요? 폰에서 글 보내기·화면 보기까지 됩니다 [Y/n] ' >/dev/tty
    _wa=""
    read -r _wa </dev/tty || _wa=""
    case "$_wa" in
      [nN]*) WRAP=0; WRAP_SAVE=1 ;;
    esac
  fi
}
wrap_save_false() { # 설정에 wrap_claude: false를 남긴다(다른 키는 그대로)
  HR_CONFIG=$CONFIG "$PYTHON" -c '
import json, os
p = os.environ["HR_CONFIG"]
c = json.load(open(p))
c["wrap_claude"] = False
tmp = p + ".tmp"
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump(c, f, indent=2)
os.replace(tmp, p)
' || say "경고: 설정에 wrap_claude를 남기지 못했다($CONFIG)." >&2
}
wrap_main() { # 설치 단계: 정하고, 넣거나 지운다
  wrap_decide
  if [ "$WRAP" = 1 ]; then
    wrap_rc install "$PYTHON"
    WRAP_DONE=1
  else
    [ "$WRAP_SAVE" != 1 ] || [ "$DRY" = 1 ] || wrap_save_false
    wrap_rc remove "$PYTHON"
  fi
}
# LaunchAgent plist. deploy/user-launchagent.plist.in과 같아야 한다(테스트가 비교). 템플릿 파일이 옆에 없으면(curl | sh) 이 사본.
PLIST_BUILTIN='<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!-- install.sh가 @…@ 자리를 채워 ~/Library/LaunchAgents에 둔다. install.sh 안의 내장 사본과 같아야 한다(테스트가 비교). -->
<plist version="1.0"><dict>
  <key>Label</key><string>@LABEL@</string>
  <key>ProgramArguments</key><array><string>@PYTHON@</string><string>@SERVER@</string><string>serve</string></array>
  <key>EnvironmentVariables</key><dict>
    <key>HOME</key><string>@HOME@</string>
    <key>PATH</key><string>@PATH@</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>@LOG@</string>
  <key>StandardErrorPath</key><string>@LOG@</string>
</dict></plist>'
# write_plist <out>: $TEMPLATE(없으면 내장 사본)의 @…@ 자리를 채운다. PATH에는 hc 폴더와 python 폴더를 앞에 둔다.
# 시험용 레이블(SHELTIE_LABEL)이면 서버가 띄우는 업데이트 설치기도 그 레이블을 쓰게 환경변수로 넘긴다.
write_plist() {
  _svc=""; [ -z "${HC:-}" ] || _svc="$(dirname "$HC"):"
  _svc="$_svc$(dirname "$PYTHON"):/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
  HR_TEMPLATE=${TEMPLATE:-} HR_BUILTIN=$PLIST_BUILTIN HR_OUT=$1 HR_LABEL=$LABEL HR_PYTHON=$PYTHON \
  HR_SERVER=$SERVER HR_PATH=$_svc HR_LOG=$LOG HR_HOME=$HOME HR_TEST_LABEL=${SHELTIE_LABEL:-} "$PYTHON" -c '
import os
from xml.sax.saxutils import escape
t = open(os.environ["HR_TEMPLATE"]).read() if os.environ["HR_TEMPLATE"] else os.environ["HR_BUILTIN"] + "\n"
for k in ("LABEL", "PYTHON", "SERVER", "PATH", "LOG", "HOME"):
    t = t.replace("@%s@" % k, escape(os.environ["HR_" + k]))
if os.environ["HR_TEST_LABEL"]:
    t = t.replace("<key>PATH</key>", "<key>SHELTIE_LABEL</key><string>%s</string>\n    <key>PATH</key>" % escape(os.environ["HR_TEST_LABEL"]), 1)
open(os.environ["HR_OUT"], "w").write(t)
'
}

# launchd: bootout은 비동기라 바로 bootstrap하면 "5: Input/output error"로 실패할 수 있다. 내려간 걸 확인하고 올린다.
launchd_loaded() { launchctl print "gui/$UID_N/$LABEL" >/dev/null 2>&1; }
launchd_down() { # bootout하고 서비스가 사라질 때까지(최대 10초) 기다린다
  launchctl bootout "gui/$UID_N/$LABEL" 2>/dev/null || true
  _lw=0
  while launchd_loaded && [ "$_lw" -lt 10 ]; do sleep 1; _lw=$((_lw + 1)); done
}
launchd_up() { # <plist>: bootstrap. 실패하면 1초 간격으로 모두 3번
  _lt=0
  while :; do
    launchctl bootstrap "gui/$UID_N" "$1" && return 0
    _lt=$((_lt + 1)); [ "$_lt" -lt 3 ] || return 1
    sleep 1
  done
}

if [ "$MODE" = uninstall ]; then
  [ -n "$PORT" ] || PORT=$(config_port)
  run launchctl bootout "gui/$UID_N/$LABEL" 2>/dev/null || say "(실행 중인 서비스가 없다)"
  run rm -f "$PLIST" "$PLIST_COPY"
  ! old_installed || old_service_down
  if [ "${SHELTIE_SKIP_TAILSCALE:-}" != 1 ] && TS=$(find_tailscale); then
    run "$TS" serve "--https=$PORT" off || say "tailscale serve 해제 실패(이미 없을 수 있다)"
  fi
  if [ -f "$CMD_PATH" ] && grep -F "$CMD_MARK" "$CMD_PATH" >/dev/null 2>&1; then run rm -f "$CMD_PATH"; fi
  if UPY2=$(command -v python3 2>/dev/null) && [ -n "$UPY2" ]; then wrap_rc remove "$UPY2"
  else say "python3가 없어 셸 설정의 claude 함수는 그대로다. ~/.zshrc 등에서 '$WRAP_BEGIN' ~ '$WRAP_END' 줄 사이를 직접 지워라."; fi
  [ ! -e "$SYS_PLIST" ] || say "/Library/LaunchAgents에도 설치돼 있다. 지우려면: sudo rm $SYS_PLIST"
  if [ "$NOHOOKS" != 1 ]; then
    if UPY=$(command -v python3 2>/dev/null) && [ -n "$UPY" ]; then hooks_apply uninstall "$UPY"
    else say "python3가 없어 에이전트 훅은 그대로다. ~/.claude/settings.json, ~/.codex/hooks.json, ~/.qwen/settings.json 에서 'sheltie.py' hook 항목을, ~/.gemini/config/hooks.json 에서 \"sheltie\" 항목을, $OPENCODE_DIR/plugins/sheltie.js 를 직접 지워라."; fi
  fi
  _kept=$CONFIG_DIR; [ -d "$_kept" ] || [ ! -d "$OLD_CONFIG_DIR" ] || _kept=$OLD_CONFIG_DIR
  say "해제 완료. 설정과 토큰은 남겼다: $_kept (지우려면 직접 삭제)"
  exit 0
fi

# ---- 1. 점검
[ "$(uname -s)" = Darwin ] || die 1 "macOS에서만 설치할 수 있다."

# 서비스 plist의 파이썬(ProgramArguments 첫 칸). 업데이트는 지금 서비스가 쓰는 파이썬을 그대로 쓴다.
service_python() {
  _sp=$PLIST; [ ! -f "$SYS_PLIST" ] || _sp=$SYS_PLIST
  [ -f "$_sp" ] || return 0
  _py=$(/usr/libexec/PlistBuddy -c "Print :ProgramArguments:0" "$_sp" 2>/dev/null) \
    || _py=$(sed -n 's|.*<key>ProgramArguments</key><array><string>\([^<]*\)</string>.*|\1|p' "$_sp" | head -n 1)
  [ -n "$_py" ] && [ -x "$_py" ] && printf '%s' "$_py"
  return 0
}
PYTHON=""
[ "$MODE" != update ] || PYTHON=$(service_python)
[ -n "$PYTHON" ] || PYTHON=$(command -v python3 2>/dev/null || true)
[ -n "$PYTHON" ] || die 1 "python3가 없다. 터미널에서 'xcode-select --install'로 개발자 도구를 설치한 뒤 다시 실행해라."
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
  || die 1 "python3가 3.9보다 낮다($PYTHON). 'xcode-select --install' 또는 'brew install python'으로 새 버전을 설치해라."

# ---- 서버 자동 업데이트(--update). 서버가 받은 새 서버 파일(SHELTIE_SRC)로 바꾸고 다시 시작한다.
# 설정·토큰·기기 파일은 읽기만 한다. tailscale·hc 설치·QR은 건너뛴다. 새 버전이 정해진 시간 안에 답하지 않으면 되돌린다.
# update_status <result> <version> <from> <reason> <transient 0|1>: 서버가 다음 시작 때 읽는 결과 파일.
# transient=1(응답 시간 초과·쓰기 실패 등)이면 서버가 다음 확인 때 다시 해 보고 3번째에야 그 버전을 버린다.
update_status() {
  HR_OUT="$CONFIG_DIR/$UPDATE_STATUS_NAME" HR_RESULT=$1 HR_VER=$2 HR_FROM=$3 HR_REASON=$4 HR_TRANSIENT=${5:-0} "$PYTHON" -c '
import json, os, time
p = os.environ["HR_OUT"]
tmp = p + ".tmp"
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump({"result": os.environ["HR_RESULT"], "version": os.environ["HR_VER"], "from": os.environ["HR_FROM"],
               "reason": os.environ["HR_REASON"], "transient": os.environ["HR_TRANSIENT"] == "1",
               "time": int(time.time())}, f)
os.replace(tmp, p)
' || say "경고: 업데이트 결과를 남기지 못했다: $CONFIG_DIR/$UPDATE_STATUS_NAME" >&2
}
update_fail() { # <이유> [permanent]: 서버를 건드리기 전 실패. permanent(릴리스가 잘못됨)가 아니면 일시적 실패다
  say "업데이트 중단: $1" >&2
  _tr=1; [ "${2:-}" != permanent ] || _tr=0
  [ ! -d "$CONFIG_DIR" ] || update_status failed "${NEWV:-}" "${OLDV:-}" "$1" "$_tr"
  exit 1
}
server_version_of() { # 서버 파일의 SERVER_VERSION 줄(server/sheltie.py SERVER_VERSION_LINE_RE와 같은 모양)
  "$PYTHON" -c '
import re, sys
m = re.search(r"^SERVER_VERSION = \"([^\"\n]*)\"[ \t]*$", open(sys.argv[1], encoding="utf-8-sig").read(), re.M)
print(m.group(1) if m else "")
' "$1" 2>/dev/null || true
}
config_value() { # <키>: 설정 값(없으면 빈 문자열, false는 "false")
  "$PYTHON" -c '
import json, sys
v = json.load(open(sys.argv[1])).get(sys.argv[2])
print("false" if v is False else ("" if v is None else v))
' "$CONFIG" "$1" 2>/dev/null || true
}
# wait_version <버전> <초>: 그 버전을 말하고 이 단계 뒤(T0)에 뜬 서버가 /api/health에 답하면 0
wait_version() {
  _deadline=$(( $(date +%s) + $2 ))
  while :; do
    if printf 'header = "Authorization: Bearer %s"\nurl = "http://127.0.0.1:%s/api/health"\n' "$TOKEN" "$PORT" \
       | curl -fsS -m 3 -K - 2>/dev/null | HR_WANT=$1 HR_T0=$T0 "$PYTHON" -c '
import json, os, sys
d = json.load(sys.stdin)
ok = (not os.environ["HR_WANT"] or d.get("server_version") == os.environ["HR_WANT"]) \
    and float(d.get("started") or 0) >= float(os.environ["HR_T0"])
sys.exit(0 if ok else 1)
' 2>/dev/null; then return 0; fi
    [ "$(date +%s)" -lt "$_deadline" ] || return 1
    sleep 1
  done
}
# update_restart <kick|reload> [이전 plist]: 서비스 다시 시작. plist가 바뀌었으면(reload) bootout → 내려갈 때까지 기다림 →
# bootstrap(다시 시도). 새 plist가 끝내 안 올라가면 bootout이 안 끝난 경우 kickstart, 아니면 이전 plist를 되돌려 놓고 올린다.
# 이 설치기는 서버가 새 세션으로 띄워서 서버 job이 내려가도 살아남는다.
update_restart() {
  if [ "$SYS_INSTALLED" = 1 ]; then
    launchctl kickstart -k "gui/$UID_N/$LABEL"
    return
  fi
  if [ "$1" = reload ]; then
    launchd_down
    launchd_up "$PLIST" && return 0
    if launchd_loaded; then launchctl kickstart -k "gui/$UID_N/$LABEL"; return; fi
    if [ -n "${2:-}" ] && [ -f "$2" ]; then
      say "경고: 새 plist로 서비스를 올리지 못해 이전 plist로 올린다." >&2
      mv -f "$2" "$PLIST" && cp "$PLIST" "$PLIST_COPY"
      launchd_up "$PLIST"
      return
    fi
    return 1
  fi
  launchctl kickstart -k "gui/$UID_N/$LABEL" 2>/dev/null || launchd_up "$PLIST"
}
update_main() {
  T0=$(date +%s)
  NEWV=""; OLDV=""
  [ -f "$CONFIG" ] || update_fail "설정이 없다: $CONFIG"
  [ -f "$SERVER" ] || update_fail "설치된 서버 파일이 없다: $SERVER"
  SRC=${SHELTIE_SRC:-}
  [ -n "$SRC" ] && [ -f "$SRC" ] || update_fail "새 서버 파일이 없다: ${SRC:-(SHELTIE_SRC 없음)}"
  "$PYTHON" -c 'import sys; compile(open(sys.argv[1], encoding="utf-8-sig").read(), sys.argv[1], "exec")' "$SRC" \
    || update_fail "새 서버 파일이 올바른 파이썬이 아니다: $SRC" permanent
  NEWV=$(server_version_of "$SRC")
  [ -n "$NEWV" ] || update_fail "새 서버 파일에 SERVER_VERSION이 없다: $SRC" permanent
  OLDV=$(server_version_of "$SERVER")
  # 버전은 올라가기만 한다(서버도 확인하지만 설치기도 거꾸로 가지 않는다).
  HR_NEW=$NEWV HR_OLD=$OLDV "$PYTHON" -c '
import os, re, sys
rx = re.compile(r"^(0|[1-9][0-9]{0,4})\.(0|[1-9][0-9]{0,4})\.(0|[1-9][0-9]{0,4})$")
n, o = rx.match(os.environ["HR_NEW"]), rx.match(os.environ["HR_OLD"])
sys.exit(0 if n and (not os.environ["HR_OLD"] or (o and tuple(map(int, n.groups())) > tuple(map(int, o.groups()))))
         else 1)
' || update_fail "버전이 올라가지 않는다(${OLDV:-?} -> $NEWV)" permanent
  PORT=$(config_port)
  TOKEN=$("$PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1]))["token"])' "$CONFIG" 2>/dev/null) \
    || update_fail "설정에서 토큰을 읽지 못했다: $CONFIG"
  HC=$(config_value hc_path)
  WAIT=${SHELTIE_UPDATE_WAIT:-120}
  case "$WAIT" in ""|*[!0-9]*) WAIT=120 ;; esac
  SYS_INSTALLED=0
  [ ! -e "$SYS_PLIST" ] || SYS_INSTALLED=1
  say "Sheltie 서버 업데이트: ${OLDV:-?} -> $NEWV"
  if [ "$DRY" = 1 ]; then
    say "+ $SERVER -> $SERVER.prev, $SRC -> $SERVER, 훅 다시 등록, 서비스 다시 시작, ${WAIT}초 안에 $NEWV 확인(아니면 되돌림)"
    exit 0
  fi
  # 교체: 지금 서버는 .prev로 남기고, 새 파일은 같은 폴더 임시 파일에서 rename(반쯤 쓴 파일로 뜨지 않게)
  cp -p "$SERVER" "$SERVER.prev" || update_fail "지금 서버를 보관하지 못했다: $SERVER.prev"
  _utmp=$(mktemp "$DATA_DIR/sheltie.XXXXXX") || update_fail "임시 파일을 만들지 못했다: $DATA_DIR"
  if ! { cp "$SRC" "$_utmp" && chmod 0755 "$_utmp" && mv -f "$_utmp" "$SERVER"; }; then
    rm -f "$_utmp"
    update_fail "새 서버 파일을 두지 못했다: $SERVER"
  fi
  # 여기부터는 서버 파일이 바뀌었다. 어떤 명령이 실패해도(쓰기·디스크 오류) 중간에 끝나지 않고
  # 재시작·확인·되돌리기·결과 기록까지 간다.
  set +e
  if [ "$NOHOOKS" = 1 ] || [ "$(config_value hooks)" = false ]; then say "(훅 등록 건너뜀)"; else hooks_apply install "$PYTHON"; fi
  install_command
  _mode=kick
  if [ "$SYS_INSTALLED" != 1 ]; then
    if write_plist "$PLIST.new"; then
      if [ -f "$PLIST" ] && cmp -s "$PLIST.new" "$PLIST"; then
        rm -f "$PLIST.new"
      else
        [ ! -f "$PLIST" ] || cp -p "$PLIST" "$PLIST.prev"
        mv -f "$PLIST.new" "$PLIST" && cp "$PLIST" "$PLIST_COPY"
        _mode=reload
      fi
    else
      rm -f "$PLIST.new"
      say "경고: 새 plist를 만들지 못해 지금 plist로 다시 시작한다." >&2
    fi
  fi
  update_restart "$_mode" "$PLIST.prev" || say "경고: 서비스를 다시 시작하지 못했다($LABEL)." >&2
  if wait_version "$NEWV" "$WAIT"; then
    update_status ok "$NEWV" "$OLDV" "" 0
    rm -f "$PLIST.prev"
    say "업데이트 완료: $NEWV"
    exit 0
  fi
  # 되돌리기: 이전 서버(와 plist)로. 결과를 먼저 남겨야 다시 뜬 이전 서버가 실패한 버전을 읽는다.
  say "새 서버($NEWV)가 ${WAIT}초 안에 답하지 않아 이전 서버(${OLDV:-?})로 되돌린다." >&2
  _rtmp=$(mktemp "$DATA_DIR/sheltie.XXXXXX") && cp -p "$SERVER.prev" "$_rtmp" && mv -f "$_rtmp" "$SERVER" \
    || say "경고: 이전 서버 파일을 되돌리지 못했다. 직접 복사해라: cp '$SERVER.prev' '$SERVER'" >&2
  if [ "$_mode" = reload ] && [ -f "$PLIST.prev" ]; then
    mv -f "$PLIST.prev" "$PLIST" && cp "$PLIST" "$PLIST_COPY"
  fi
  update_status rolled_back "$NEWV" "$OLDV" "새 서버가 ${WAIT}초 안에 /api/health에 ${NEWV}로 답하지 않았다" 1
  T0=$(date +%s)
  update_restart "$_mode" || say "경고: 서비스를 다시 시작하지 못했다($LABEL)." >&2
  if wait_version "$OLDV" "$WAIT"; then say "이전 서버로 되돌렸다(${OLDV:-?})."
  else
    # 마지막 수단: 서비스가 내려가 있으면 지금(되돌린) plist로 다시 올린다
    if [ "$SYS_INSTALLED" != 1 ] && ! launchd_loaded; then
      say "경고: 서비스가 내려가 있어 다시 올린다($PLIST)." >&2
      T0=$(date +%s)
      if launchd_up "$PLIST" && wait_version "$OLDV" "$WAIT"; then say "이전 서버로 되돌렸다(${OLDV:-?})."; exit 3; fi
    fi
    say "경고: 되돌린 서버도 답하지 않는다. 로그를 확인해라: tail -n 30 $LOG" >&2
  fi
  exit 3
}
[ "$MODE" != update ] || update_main

find_hc() {
  HC=
  if [ -n "${SHELTIE_HC:-}" ]; then
    [ -x "$SHELTIE_HC" ] && HC=$SHELTIE_HC
  elif [ -x "$HOME/.local/bin/hc" ]; then
    HC="$HOME/.local/bin/hc"
  elif p=$(command -v hc 2>/dev/null) && [ -x "$p" ]; then
    HC=$p
  elif [ -x "$HOME/.local/bin/herdr-core" ]; then
    HC="$HOME/.local/bin/herdr-core"
  fi
  return 0
}
find_hc

# hc가 없으면 고정 버전 herdr-core를 받아 SHA256을 확인하고 직접 깐다. 실패해도 설치는 계속한다(훅은 hc 없이 된다). 코어는 띄우지 않는다.
hc_fail() { say "herdr-core 자동 설치를 못 했다($1)."; }
hc_sha256() { shasum -a 256 "$1" 2>/dev/null | cut -d' ' -f1; }
# 임시 폴더 $1 안에 받고 해시를 확인한다. 실패하면 이유를 _hreason에 두고 1로 돌아온다(알리기는 부른 쪽이 한다).
hc_fetch() {
  _hd=$1; _hsrc=$2; _hwant=$3
  rm -f "$_hd/hc.tar.gz"
  case "$_hsrc" in
    https://*) curl --proto '=https' --tlsv1.2 -fsSL -o "$_hd/hc.tar.gz" "$_hsrc" || { _hreason="받지 못했다: $_hsrc"; return 1; } ;;
    *) if [ -f "$_hsrc" ]; then cp "$_hsrc" "$_hd/hc.tar.gz" || { _hreason="파일을 못 읽었다: $_hsrc"; return 1; }; else _hreason="파일이 없다: $_hsrc"; return 1; fi ;;
  esac
  _hgot=$(hc_sha256 "$_hd/hc.tar.gz")
  if [ -z "$_hgot" ] || [ "$_hgot" != "$_hwant" ]; then _hreason="체크섬 불일치: 받은 파일이 예상과 다르다"; return 1; fi
  return 0
}
# 임시 폴더 $1 안의 확인된 hc.tar.gz를 풀어 설치한다. 실패는 hc_fail로 알리고 1로 돌아온다.
hc_unpack_install() {
  _hd=$1
  mkdir "$_hd/x" && tar -xzf "$_hd/hc.tar.gz" -C "$_hd/x" || { hc_fail "압축을 풀지 못했다"; return 1; }
  _hbin="$HOME/.local/bin"
  _hbundled="$_hd/x/Herdr Core.app/Contents/MacOS/herdr-core"
  _hcore="$_hd/x/herdr-core"
  if [ -x "$_hbundled" ] && [ "$(id -u)" != 0 ]; then
    /usr/bin/codesign --verify --strict -R '=identifier "dev.herdr.core" and anchor apple generic and certificate leaf[subject.OU] = "2W9652Q7QR"' "$_hd/x/Herdr Core.app" || { hc_fail "Herdr 서명 확인 실패"; return 1; }
    "$_hbundled" --version >/dev/null 2>&1 || { hc_fail "받은 herdr-core가 이 기기에서 실행되지 않는다"; return 1; }
    mkdir -p "$_hbin" || { hc_fail "$_hbin 을 만들지 못했다"; return 1; }
    "$_hbundled" --hc-install-bundle "$_hbundled" "$_hbin/herdr-core" || { hc_fail "Herdr Core.app 설치 실패"; return 1; }
  else
    [ -f "$_hcore" ] || { hc_fail "받은 묶음에 herdr-core가 없다"; return 1; }
    chmod 755 "$_hcore"
    "$_hcore" --version >/dev/null 2>&1 || { hc_fail "받은 herdr-core가 이 기기에서 실행되지 않는다"; return 1; }
    mkdir -p "$_hbin" || { hc_fail "$_hbin 을 만들지 못했다"; return 1; }
    cp "$_hcore" "$_hbin/herdr-core.new" && chmod 755 "$_hbin/herdr-core.new" && mv -f "$_hbin/herdr-core.new" "$_hbin/herdr-core" \
      || { rm -f "$_hbin/herdr-core.new"; hc_fail "$_hbin/herdr-core 를 쓰지 못했다"; return 1; }
  fi
  if ! command -v hc >/dev/null 2>&1 && [ ! -e "$_hbin/hc" ] && [ ! -L "$_hbin/hc" ]; then
    ln -s herdr-core "$_hbin/hc" || hc_fail "hc 링크를 못 만들었다"
  fi
  return 0
}
install_hc() {
  if [ "$NOHC" = 1 ] || [ "${SHELTIE_NO_HC:-}" = 1 ]; then say "(herdr-core 자동 설치 건너뜀)"; return 0; fi
  [ -z "${SHELTIE_HC:-}" ] || return 0
  case "$(uname -m)" in
    arm64|aarch64) _htarget=macos-aarch64; _hpin=$HC_SHA_MACOS_AARCH64 ;;
    x86_64) _htarget=macos-x86_64; _hpin=$HC_SHA_MACOS_X86_64 ;;
    *) _htarget=""; _hpin="" ;;
  esac
  _hsrc=${SHELTIE_HC_URL:-}
  _hmirror=""
  if [ -z "$_hsrc" ]; then
    if [ -z "$_htarget" ]; then say "(herdr-core 자동 설치 건너뜀: 이 CPU용 빌드가 없다)"; return 0; fi
    _hsrc="$HC_BASE_URL/herdr-core-$_htarget.tar.gz"
    _hmirror="$HC_MIRROR_URL/herdr-core-$_htarget.tar.gz"
  fi
  _hwant=${SHELTIE_HC_SHA256:-$_hpin}
  case "$_hsrc" in
    http://*) hc_fail "herdr-core는 https로만 받는다: $_hsrc"; return 0 ;;
  esac
  if [ "$DRY" = 1 ]; then
    say "+ herdr-core $HC_PIN_VERSION 설치($_hsrc)"
    [ -z "$_hmirror" ] || say "+   실패하면 미러에서 받기($_hmirror)"
    return 0
  fi
  [ -n "$_hwant" ] || { hc_fail "검증할 체크섬이 없다"; return 0; }
  _hdir=$(mktemp -d "${TMPDIR:-/tmp}/sheltie-hc.XXXXXX") || { hc_fail "임시 폴더를 못 만들었다"; return 0; }
  say "herdr-core $HC_PIN_VERSION 을 설치한다($_hsrc)"
  if ! hc_fetch "$_hdir" "$_hsrc" "$_hwant"; then
    if [ -z "$_hmirror" ]; then hc_fail "$_hreason"; rm -rf "$_hdir"; return 0; fi
    say "herdr-core를 원래 주소에서 못 받았다($_hreason). 미러에서 받는다($_hmirror)"
    hc_fetch "$_hdir" "$_hmirror" "$_hwant" || { hc_fail "$_hreason"; rm -rf "$_hdir"; return 0; }
  fi
  hc_unpack_install "$_hdir" || { rm -rf "$_hdir"; return 0; }
  rm -rf "$_hdir"
  find_hc
  [ -n "$HC" ] || hc_fail "설치 뒤에도 hc를 찾지 못했다"
  return 0
}

HC_TRY=0
[ -n "$HC" ] || HC_TRY=1

TS=
if [ "${SHELTIE_SKIP_TAILSCALE:-}" != 1 ]; then
  TS=$(find_tailscale) || die 1 "tailscale이 없다. https://tailscale.com/download 에서 설치하고 로그인한 뒤 다시 실행해라."
  if ! "$TS" status --json 2>/dev/null | "$PYTHON" -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("BackendState")=="Running" else 1)' 2>/dev/null; then
    die 1 "tailscale이 로그인돼 있지 않다. Tailscale 앱을 열어 로그인한 뒤 다시 실행해라."
  fi
fi

old_check_link

# tailscale 점검을 통과한 뒤에만 hc를 깐다(점검에서 죽을 설치에 hc만 남기지 않는다).
if [ "$HC_TRY" = 1 ]; then install_hc; fi
[ -n "$HC" ] || say "herdr-core 없이 설치한다: 공식 훅으로만 동작, herdr 터미널 기능은 꺼짐 (hc를 쓰려면 SHELTIE_HC=<경로>)"

# ---- 2. 서버 파일
SRC=${SHELTIE_SRC:-}
# curl | sh로 돌면 $0은 "sh"라 dirname이 현재 폴더가 된다. 파일로 실행했을 때만 옆 파일을 쓴다.
HERE=""
case "$0" in
  *install.sh) [ -f "$0" ] && HERE=$(cd "$(dirname "$0")" 2>/dev/null && pwd || true) ;;
esac
if [ -z "$SRC" ] && [ -n "$HERE" ] && [ -f "$HERE/server/sheltie.py" ]; then
  SRC="$HERE/server/sheltie.py"
fi
[ -n "$SRC" ] || SRC=$DEFAULT_SRC
case "$SRC" in
  https://*) ;;
  http://*) die 1 "서버 파일은 https로만 받는다: $SRC" ;;
esac

run mkdir -p "$DATA_DIR" "$PLIST_DIR" "$HOME/Library/Logs"
if [ "$DRY" = 1 ]; then
  printf '+ install -m 0755 %s %s\n' "$SRC" "$SERVER"
else
  TMP=$(mktemp "$DATA_DIR/sheltie.XXXXXX")
  trap 'rm -f "$TMP"' EXIT
  case "$SRC" in
    https://*) curl --proto '=https' --tlsv1.2 -fsSL -o "$TMP" "$SRC" || die 1 "서버 파일을 받지 못했다: $SRC" ;;
    *) [ -f "$SRC" ] || die 1 "서버 파일이 없다: $SRC"; cp "$SRC" "$TMP" ;;
  esac
  [ -s "$TMP" ] || die 1 "받은 서버 파일이 비어 있다: $SRC"
  "$PYTHON" -c 'import sys; compile(open(sys.argv[1]).read(), sys.argv[1], "exec")' "$TMP" \
    || die 1 "받은 서버 파일이 올바른 파이썬이 아니다: $SRC"
  if [ "$SRC" = "$DEFAULT_SRC" ]; then
    # 릴리스 매니페스트의 해시와 맞는지(서명 검증은 설치된 서버가 업데이트 때 한다)
    _man="${DEFAULT_SRC%/*}/manifest.json"
    TMPM=$(mktemp "$DATA_DIR/manifest.XXXXXX")
    trap 'rm -f "$TMP" "$TMPM"' EXIT
    curl --proto '=https' --tlsv1.2 -fsSL -o "$TMPM" "$_man" || die 1 "릴리스 manifest.json을 받지 못했다: $_man"
    "$PYTHON" -c '
import hashlib, json, sys
want = json.load(open(sys.argv[1]))["files"]["sheltie.py"]
sys.exit(0 if hashlib.sha256(open(sys.argv[2], "rb").read()).hexdigest() == want else 1)
' "$TMPM" "$TMP" 2>/dev/null || die 1 "받은 서버 파일이 릴리스 manifest.json의 해시와 다르다: $SRC"
    rm -f "$TMPM"
  fi
  chmod 0755 "$TMP"
  mv -f "$TMP" "$SERVER"
  trap - EXIT
fi
# 이전 설치: 새 서버 파일이 제자리에 있을 때만 내리고 설정을 옮긴다(init이 새 토큰을 만들기 전에).
old_stop_and_move
run "$PYTHON" "$SERVER" init
if [ "$DRY" != 1 ]; then
  # 설정에 hc 경로와(지정했으면) 포트, --no-hooks(자동 업데이트도 훅을 건드리지 않게), --no-auto-update를 반영한다.
  HR_CONFIG=$CONFIG HR_HC=$HC HR_PORT=$PORT HR_NOHOOKS=$NOHOOKS HR_NOAUTO=$NOAUTO "$PYTHON" -c '
import json, os
p = os.environ["HR_CONFIG"]
c = json.load(open(p))
c["hc_path"] = os.environ["HR_HC"]
if os.environ["HR_PORT"]:
    c["port"] = int(os.environ["HR_PORT"])
if os.environ["HR_NOHOOKS"] == "1":
    c["hooks"] = False
else:
    c.pop("hooks", None)
if os.environ["HR_NOAUTO"] == "1":
    c["auto_update"] = False
tmp = p + ".tmp"
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump(c, f, indent=2)
os.replace(tmp, p)
' || die 1 "설정 파일을 고치지 못했다: $CONFIG"
  PORT=$(config_port)
else
  [ -n "$PORT" ] || PORT=8790
fi

# ---- 2b. Claude Code·Codex·Qwen Code 훅, OpenCode 플러그인
if [ "$NOHOOKS" = 1 ]; then say "(--no-hooks: 훅 등록 건너뜀)"; else hooks_apply install "$PYTHON"; fi

# ---- 2c. sheltie 명령(~/.local/bin/sheltie)
install_command

# ---- 2d. claude 셸 함수(~/.zshrc 등). --update는 여기까지 오지 않는다(셸 설정을 건드리지 않는다).
WRAP_DONE=0
wrap_main

# ---- 3. LaunchAgent
TEMPLATE=
SYS_INSTALLED=0
[ ! -e "$SYS_PLIST" ] || SYS_INSTALLED=1
[ -z "$HERE" ] || [ ! -f "$HERE/deploy/user-launchagent.plist.in" ] || TEMPLATE="$HERE/deploy/user-launchagent.plist.in"
if [ "$SYS_INSTALLED" = 1 ]; then
  # 외장 홈 맥에서 sudo로 시스템 위치에 설치한 경우: 사용자 plist를 만들거나 bootout하지 않고 다시 띄우기만 한다.
  say "시스템 위치에 설치돼 있다($SYS_PLIST). 서비스를 다시 시작한다."
  if ! run launchctl kickstart -k "gui/$UID_N/$LABEL"; then
    die 5 "서비스를 다시 시작하지 못했다. 터미널에서 실행해라: launchctl bootstrap gui/$UID_N '$SYS_PLIST'"
  fi
elif [ "$DRY" = 1 ]; then
  say "+ $PLIST 작성(python=$PYTHON, server=$SERVER, log=$LOG)"
else
  write_plist "$PLIST" || die 1 "plist를 만들지 못했다."
  cp "$PLIST" "$PLIST_COPY"
fi
if [ "$SYS_INSTALLED" != 1 ]; then
  if [ "$DRY" = 1 ]; then
    run launchctl bootout "gui/$UID_N/$LABEL"
    run launchctl bootstrap "gui/$UID_N" "$PLIST"
  elif launchd_down; ! launchd_up "$PLIST"; then
    rm -f "$PLIST"
    {
      echo "launchctl bootstrap이 실패했다. 홈이 외장 볼륨이면 ~/Library/LaunchAgents를 못 쓴다."
      echo "아래 한 줄을 터미널에서 실행하면(관리자 비밀번호 필요) 시스템 위치에 설치된다. 끝나면 'sh install.sh'를 다시 실행해라."
      echo "sudo install -o root -g wheel -m 644 '$PLIST_COPY' /Library/LaunchAgents/ && launchctl bootstrap gui/$UID_N '$SYS_PLIST'"
    } >&2
    exit 5
  fi
fi

# ---- 4. tailscale serve (tailnet 전용, Funnel 아님)
funnel_on() { # 이 포트에 Funnel(인터넷 공개)이 켜져 있으면 0. 판정 못 하면 1.
  "$TS" serve status --json 2>/dev/null | HR_PORT=$PORT "$PYTHON" -c '
import json, os, sys
try:
    d = json.load(sys.stdin) or {}
except ValueError:
    sys.exit(1)
port = ":" + os.environ["HR_PORT"]
sys.exit(0 if any(v and k.endswith(port) for k, v in (d.get("AllowFunnel") or {}).items()) else 1)
' 2>/dev/null
}
if [ -n "$TS" ]; then
  if [ "$DRY" != 1 ] && funnel_on; then
    {
      echo "tailscale Funnel이 포트 ${PORT}에 켜져 있다. 이대로면 서버가 인터넷에 공개된다."
      echo "Funnel을 끈 뒤 다시 실행해라: $TS funnel --https=$PORT off"
      echo "(지금 설정 보기: $TS funnel status)"
    } >&2
    exit 8
  elif [ "$DRY" != 1 ] && "$TS" serve status 2>/dev/null | grep -F ":$PORT " >/dev/null \
     && "$TS" serve status 2>/dev/null | grep -F "proxy http://127.0.0.1:$PORT" >/dev/null; then
    say "tailscale serve가 이미 설정돼 있다(:$PORT)."
  elif ! run "$TS" serve --bg "--https=$PORT" "http://127.0.0.1:$PORT"; then
    die 6 "tailscale serve 설정에 실패했다. Tailscale 관리 콘솔(DNS 탭)에서 HTTPS 인증서를 켰는지 확인하고, 직접 실행해 봐라: $TS serve --bg --https=$PORT http://127.0.0.1:$PORT"
  fi
else
  say "(tailscale 건너뜀)"
fi

# ---- 5. health
if [ "$DRY" = 1 ]; then
  say "+ GET http://127.0.0.1:$PORT/api/health (토큰 인증)"
else
  TOKEN=$("$PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1]))["token"])' "$CONFIG")
  TRIES=${SHELTIE_HEALTH_TRIES:-10}
  i=0; ok=0
  while [ "$i" -lt "$TRIES" ]; do
    if printf 'header = "Authorization: Bearer %s"\nurl = "http://127.0.0.1:%s/api/health"\n' "$TOKEN" "$PORT" \
       | curl -fsS -m 3 -o /dev/null -K - 2>/dev/null; then ok=1; break; fi
    i=$((i + 1)); sleep 1
  done
  [ "$ok" = 1 ] || die 7 "서버가 응답하지 않는다. 로그를 확인해라: tail -n 30 $LOG"
  say "서버 동작 확인(포트 $PORT)."
fi
old_cleanup

# ---- 6. 페어링
if [ -n "$TS" ]; then
  if [ "$DRY" = 1 ]; then
    say "+ $PYTHON $SERVER pair"
  elif ! "$PYTHON" "$SERVER" pair; then
    die 4 "설치는 끝났지만 pair 명령이 없거나 실패했다. 서버를 최신 버전으로 받아 다시 실행하거나, 앱에 주소와 토큰($CONFIG)을 직접 입력해라."
  fi
else
  say "(tailscale 없이 설치해서 pair는 건너뜀)"
fi
if [ "$NOHOOKS" != 1 ]; then
  say "Claude Code·Codex·Qwen Code·OpenCode를 새로 시작해야 훅이 적용된다."
fi
if [ "$WRAP_DONE" = 1 ] && [ "$DRY" != 1 ]; then
  say "claude 셸 함수는 새 터미널부터 적용된다(지금 터미널은 source ~/.zshrc). 원래 claude로 띄우려면 command claude 또는 SHELTIE_NO_WRAP=1 claude."
fi
