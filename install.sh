#!/bin/sh
# Sheltie 설치기(macOS). 사용: sh install.sh [--dry-run] [--uninstall] [--no-hooks]
#   --no-hooks: Claude Code·Codex·Qwen Code 훅과 OpenCode 플러그인 등록(해제)을 건너뜀. hc(herdr-core)가 없어도 설치는 계속한다(공식 훅으로만 동작).
#   환경변수: SHELTIE_SRC(서버 파일 https URL 또는 로컬 경로. 기본: 옆의 server/ 파일, 없으면 GitHub raw), SHELTIE_HC(hc 경로),
#   SHELTIE_PORT(기본 8790), SHELTIE_TAILSCALE(tailscale CLI 경로),
#   SHELTIE_SKIP_TAILSCALE=1(tailscale 점검·serve·pair 건너뜀, 시험용),
#   SHELTIE_SYS_AGENTS_DIR(시스템 LaunchAgents 폴더, 기본 /Library/LaunchAgents, 시험용)
#   이전 설치가 있으면 서비스를 내리고 설정 폴더를 새 위치로 옮긴다.
set -eu

# 서버 파일 기본 출처. 저장소 이름이 바뀌면 이 한 줄만 고친다.
DEFAULT_SRC="https://raw.githubusercontent.com/hyperfocuslab/sheltie/main/server/sheltie.py"
LABEL="com.hyperfocuslab.sheltie"
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
unset SHELTIE_CONFIG || true

for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    --uninstall) MODE=uninstall ;;
    --no-hooks) NOHOOKS=1 ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) echo "알 수 없는 옵션: $a (--dry-run, --uninstall, --no-hooks)" >&2; exit 2 ;;
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
  if [ -f "$_c" ] && command -v python3 >/dev/null 2>&1; then
    python3 -c 'import json,sys; print(int(json.load(open(sys.argv[1])).get("port",8790)))' "$_c" 2>/dev/null && return 0
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
        "$HOME/.qwen/settings.json" "$OPENCODE_DIR/plugins/sheltie.js"; do
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

# Claude Code·Codex·Qwen Code 훅 병합과 OpenCode 플러그인 쓰기 스크립트(이 한 곳만 고친다. install.ps1에 같은 글자로 들어 있고 테스트가 비교한다).
HOOKS_PY=$(cat <<'HOOKS_PY_EOF'
# ---- hooks_merge.py begin (install.sh and install.ps1 embed this identical text; a test compares them)
# usage: hooks_merge.py <install|uninstall> <claude|codex|qwen|opencode> <settings-file> <python-exe> <server-file> <posix|windows> [dry]
# For opencode <settings-file> is the plugin file (plugins/sheltie.js): written or removed whole, only when it carries
# our mark on its first line (another file with that name is left alone: broken).
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
        if i >= 0 and cmd[i + len(m):i + len(m) + 7] in ('" hook ', "' hook "):
            return True
    return False


def spec():
    # (event, matcher or None, timeout seconds)
    if target == "claude":
        return [("PermissionRequest", "*", 600), ("PreToolUse", "AskUserQuestion|ExitPlanMode", 600),
                ("SessionStart", None, 10), ("SessionEnd", None, 10), ("UserPromptSubmit", None, 10), ("Stop", None, 10)]
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


def main():
    if target == "opencode":
        return plugin_main()
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

# hooks_apply <install|uninstall> <python>: 있는 에이전트(~/.claude, ~/.codex, ~/.qwen)의 훅 설정 파일에 우리 항목을
# 넣거나 빼고, OpenCode(설정 폴더가 있을 때)는 전역 플러그인 파일(plugins/sheltie.js)을 쓰거나 지운다.
hooks_apply() {
  _act=$1; _py=$2
  _dryarg=""; [ "$DRY" != 1 ] || _dryarg=dry
  for _t in claude codex qwen opencode; do
    _dir="$HOME/.$_t"; _file="$_dir/settings.json"
    case "$_t" in
      codex) _file="$_dir/hooks.json" ;;
      opencode) _dir=$OPENCODE_DIR; _file="$_dir/plugins/sheltie.js" ;;
    esac
    if [ ! -d "$_dir" ]; then say "($_t: $_dir 없음, 훅 건너뜀)"; continue; fi
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
  [ ! -e "$SYS_PLIST" ] || say "/Library/LaunchAgents에도 설치돼 있다. 지우려면: sudo rm $SYS_PLIST"
  if [ "$NOHOOKS" != 1 ]; then
    if UPY=$(command -v python3 2>/dev/null) && [ -n "$UPY" ]; then hooks_apply uninstall "$UPY"
    else say "python3가 없어 에이전트 훅은 그대로다. ~/.claude/settings.json, ~/.codex/hooks.json, ~/.qwen/settings.json 에서 'sheltie.py' hook 항목을, $OPENCODE_DIR/plugins/sheltie.js 를 직접 지워라."; fi
  fi
  _kept=$CONFIG_DIR; [ -d "$_kept" ] || [ ! -d "$OLD_CONFIG_DIR" ] || _kept=$OLD_CONFIG_DIR
  say "해제 완료. 설정과 토큰은 남겼다: $_kept (지우려면 직접 삭제)"
  exit 0
fi

# ---- 1. 점검
[ "$(uname -s)" = Darwin ] || die 1 "macOS에서만 설치할 수 있다."

PYTHON=$(command -v python3 2>/dev/null || true)
[ -n "$PYTHON" ] || die 1 "python3가 없다. 터미널에서 'xcode-select --install'로 개발자 도구를 설치한 뒤 다시 실행해라."
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
  || die 1 "python3가 3.9보다 낮다($PYTHON). 'xcode-select --install' 또는 'brew install python'으로 새 버전을 설치해라."

HC=
if [ -n "${SHELTIE_HC:-}" ]; then
  [ -x "$SHELTIE_HC" ] && HC=$SHELTIE_HC
elif [ -x "$HOME/.local/bin/hc" ]; then
  HC="$HOME/.local/bin/hc"
elif p=$(command -v hc 2>/dev/null) && [ -x "$p" ]; then
  HC=$p
fi
[ -n "$HC" ] || say "herdr-core 없이 설치한다: 공식 훅으로만 동작, herdr 터미널 기능은 꺼짐 (hc를 쓰려면 SHELTIE_HC=<경로>)"

TS=
if [ "${SHELTIE_SKIP_TAILSCALE:-}" != 1 ]; then
  TS=$(find_tailscale) || die 1 "tailscale이 없다. https://tailscale.com/download 에서 설치하고 로그인한 뒤 다시 실행해라."
  if ! "$TS" status --json 2>/dev/null | "$PYTHON" -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("BackendState")=="Running" else 1)' 2>/dev/null; then
    die 1 "tailscale이 로그인돼 있지 않다. Tailscale 앱을 열어 로그인한 뒤 다시 실행해라."
  fi
fi

old_check_link

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
  chmod 0755 "$TMP"
  mv -f "$TMP" "$SERVER"
  trap - EXIT
fi
# 이전 설치: 새 서버 파일이 제자리에 있을 때만 내리고 설정을 옮긴다(init이 새 토큰을 만들기 전에).
old_stop_and_move
run "$PYTHON" "$SERVER" init
if [ "$DRY" != 1 ]; then
  # 설정에 hc 경로와(지정했으면) 포트를 반영한다.
  HR_CONFIG=$CONFIG HR_HC=$HC HR_PORT=$PORT "$PYTHON" -c '
import json, os
p = os.environ["HR_CONFIG"]
c = json.load(open(p))
c["hc_path"] = os.environ["HR_HC"]
if os.environ["HR_PORT"]:
    c["port"] = int(os.environ["HR_PORT"])
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

# ---- 3. LaunchAgent
SVC_PATH=""; [ -z "$HC" ] || SVC_PATH="$(dirname "$HC"):"
SVC_PATH="$SVC_PATH$(dirname "$PYTHON"):/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
PLIST_BUILTIN='<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!-- install.sh가 @…@ 자리를 채워 ~/Library/LaunchAgents에 둔다. install.sh 안의 내장 사본과 같아야 한다(테스트가 비교). -->
<plist version="1.0"><dict>
  <key>Label</key><string>@LABEL@</string>
  <key>ProgramArguments</key><array><string>@PYTHON@</string><string>@SERVER@</string><string>serve</string></array>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>@PATH@</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>@LOG@</string>
  <key>StandardErrorPath</key><string>@LOG@</string>
</dict></plist>'
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
  HR_TEMPLATE=$TEMPLATE HR_BUILTIN=$PLIST_BUILTIN HR_OUT=$PLIST HR_LABEL=$LABEL HR_PYTHON=$PYTHON \
  HR_SERVER=$SERVER HR_PATH=$SVC_PATH HR_LOG=$LOG "$PYTHON" -c '
import os
from xml.sax.saxutils import escape
t = open(os.environ["HR_TEMPLATE"]).read() if os.environ["HR_TEMPLATE"] else os.environ["HR_BUILTIN"] + "\n"
for k in ("LABEL", "PYTHON", "SERVER", "PATH", "LOG"):
    t = t.replace("@%s@" % k, escape(os.environ["HR_" + k]))
open(os.environ["HR_OUT"], "w").write(t)
' || die 1 "plist를 만들지 못했다."
  cp "$PLIST" "$PLIST_COPY"
fi
if [ "$SYS_INSTALLED" != 1 ]; then
  run launchctl bootout "gui/$UID_N/$LABEL" 2>/dev/null || true
  if ! run launchctl bootstrap "gui/$UID_N" "$PLIST"; then
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
      echo "tailscale Funnel이 포트 $PORT에 켜져 있다. 이대로면 서버가 인터넷에 공개된다."
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
  say "Claude Code·Codex·Qwen Code·OpenCode를 새로 시작해야 훅이 적용된다(Codex는 /hooks 에서 한 번 신뢰해야 한다)."
fi
