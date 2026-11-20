#!/bin/sh
# herdr-remote 설치기(macOS). 사용: sh install.sh [--dry-run] [--uninstall]
#   환경변수: HERDR_REMOTE_SRC(서버 파일 https URL 또는 로컬 경로. 기본: 옆의 server/ 파일, 없으면 GitHub raw), HERDR_REMOTE_HC(hc 경로),
#   HERDR_REMOTE_PORT(기본 8790), HERDR_REMOTE_TAILSCALE(tailscale CLI 경로),
#   HERDR_REMOTE_SKIP_TAILSCALE=1(tailscale 점검·serve·pair 건너뜀, 시험용),
#   HERDR_REMOTE_SYS_AGENTS_DIR(시스템 LaunchAgents 폴더, 기본 /Library/LaunchAgents, 시험용)
set -eu

# 서버 파일 기본 출처. 저장소 이름이 바뀌면 이 한 줄만 고친다.
DEFAULT_SRC="https://raw.githubusercontent.com/hyperfocuslab/sheltie/main/server/herdr_remote.py"
LABEL="com.hyperfocuslab.herdr-remote"
DATA_DIR="$HOME/.local/share/herdr-remote"
SERVER="$DATA_DIR/herdr_remote.py"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST="$PLIST_DIR/$LABEL.plist"
PLIST_COPY="$DATA_DIR/$LABEL.plist"
SYS_PLIST="${HERDR_REMOTE_SYS_AGENTS_DIR:-/Library/LaunchAgents}/$LABEL.plist"
LOG="$HOME/Library/Logs/herdr-remote.log"
CONFIG="$HOME/.config/herdr-remote/config.json"
UID_N=$(id -u)
DRY=0
MODE=install
unset HERDR_REMOTE_CONFIG || true

for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    --uninstall) MODE=uninstall ;;
    -h|--help) sed -n '2,7p' "$0"; exit 0 ;;
    *) echo "알 수 없는 옵션: $a (--dry-run, --uninstall)" >&2; exit 2 ;;
  esac
done

say() { printf '%s\n' "$*"; }
die() { code=$1; shift; printf '%s\n' "$*" >&2; exit "$code"; }
# 변경 명령은 여기로만 실행한다. dry-run이면 출력만 한다. 인자는 쉘을 거치지 않는다.
run() {
  if [ "$DRY" = 1 ]; then printf '+ %s\n' "$*"; return 0; fi
  "$@"
}

PORT=${HERDR_REMOTE_PORT:-}
case "$PORT" in
  "") ;;
  *[!0-9]*) die 2 "HERDR_REMOTE_PORT는 숫자여야 한다: $PORT" ;;
  *) if [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then die 2 "포트 범위는 1~65535: $PORT"; fi ;;
esac

find_tailscale() {
  if [ -n "${HERDR_REMOTE_TAILSCALE:-}" ]; then
    [ -x "$HERDR_REMOTE_TAILSCALE" ] && { printf '%s' "$HERDR_REMOTE_TAILSCALE"; return 0; }
    return 1
  fi
  if p=$(command -v tailscale 2>/dev/null) && [ -x "$p" ]; then printf '%s' "$p"; return 0; fi
  for p in /opt/homebrew/bin/tailscale /Applications/Tailscale.app/Contents/MacOS/Tailscale /usr/local/bin/tailscale; do
    [ -x "$p" ] && { printf '%s' "$p"; return 0; }
  done
  return 1
}

config_port() { # 설정 파일의 port. 없으면 8790
  if [ -f "$CONFIG" ] && command -v python3 >/dev/null 2>&1; then
    python3 -c 'import json,sys; print(int(json.load(open(sys.argv[1])).get("port",8790)))' "$CONFIG" 2>/dev/null && return 0
  fi
  echo 8790
}

if [ "$MODE" = uninstall ]; then
  [ -n "$PORT" ] || PORT=$(config_port)
  run launchctl bootout "gui/$UID_N/$LABEL" 2>/dev/null || say "(실행 중인 서비스가 없다)"
  run rm -f "$PLIST" "$PLIST_COPY"
  if [ "${HERDR_REMOTE_SKIP_TAILSCALE:-}" != 1 ] && TS=$(find_tailscale); then
    run "$TS" serve "--https=$PORT" off || say "tailscale serve 해제 실패(이미 없을 수 있다)"
  fi
  [ ! -e "$SYS_PLIST" ] || say "/Library/LaunchAgents에도 설치돼 있다. 지우려면: sudo rm $SYS_PLIST"
  say "해제 완료. 설정과 토큰은 남겼다: $HOME/.config/herdr-remote (지우려면 직접 삭제)"
  exit 0
fi

# ---- 1. 점검
[ "$(uname -s)" = Darwin ] || die 1 "macOS에서만 설치할 수 있다."

PYTHON=$(command -v python3 2>/dev/null || true)
[ -n "$PYTHON" ] || die 1 "python3가 없다. 터미널에서 'xcode-select --install'로 개발자 도구를 설치한 뒤 다시 실행해라."
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
  || die 1 "python3가 3.9보다 낮다($PYTHON). 'xcode-select --install' 또는 'brew install python'으로 새 버전을 설치해라."

HC=
if [ -n "${HERDR_REMOTE_HC:-}" ]; then
  [ -x "$HERDR_REMOTE_HC" ] && HC=$HERDR_REMOTE_HC
elif [ -x "$HOME/.local/bin/hc" ]; then
  HC="$HOME/.local/bin/hc"
elif p=$(command -v hc 2>/dev/null) && [ -x "$p" ]; then
  HC=$p
fi
[ -n "$HC" ] || die 1 "hc(herdr-core)가 없다. 먼저 herdr-core를 설치해라(설치되면 ~/.local/bin/hc가 생긴다). 다른 위치면 HERDR_REMOTE_HC=<경로>로 알려줘라."

TS=
if [ "${HERDR_REMOTE_SKIP_TAILSCALE:-}" != 1 ]; then
  TS=$(find_tailscale) || die 1 "tailscale이 없다. https://tailscale.com/download 에서 설치하고 로그인한 뒤 다시 실행해라."
  if ! "$TS" status --json 2>/dev/null | "$PYTHON" -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("BackendState")=="Running" else 1)' 2>/dev/null; then
    die 1 "tailscale이 로그인돼 있지 않다. Tailscale 앱을 열어 로그인한 뒤 다시 실행해라."
  fi
fi

# ---- 2. 서버 파일
SRC=${HERDR_REMOTE_SRC:-}
HERE=$(cd "$(dirname "$0")" 2>/dev/null && pwd || true)
if [ -z "$SRC" ] && [ -n "$HERE" ] && [ -f "$HERE/server/herdr_remote.py" ]; then
  SRC="$HERE/server/herdr_remote.py"
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
  TMP=$(mktemp "$DATA_DIR/herdr_remote.XXXXXX")
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

# ---- 3. LaunchAgent
SVC_PATH="$(dirname "$HC"):$(dirname "$PYTHON"):/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
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
  TRIES=${HERDR_REMOTE_HEALTH_TRIES:-10}
  i=0; ok=0
  while [ "$i" -lt "$TRIES" ]; do
    if printf 'header = "Authorization: Bearer %s"\nurl = "http://127.0.0.1:%s/api/health"\n' "$TOKEN" "$PORT" \
       | curl -fsS -m 3 -o /dev/null -K - 2>/dev/null; then ok=1; break; fi
    i=$((i + 1)); sleep 1
  done
  [ "$ok" = 1 ] || die 7 "서버가 응답하지 않는다. 로그를 확인해라: tail -n 30 $LOG"
  say "서버 동작 확인(포트 $PORT)."
fi

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
