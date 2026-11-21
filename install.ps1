# herdr-remote 설치기(Windows). 사용: powershell -File install.ps1 [-DryRun] [-Uninstall] [-Help]
#   iex로 돌릴 때는 인자를 못 주므로 HERDR_REMOTE_UNINSTALL=1 로 제거할 수 있다.
#   환경변수: HERDR_REMOTE_SRC(서버 파일 https URL 또는 로컬 경로. 기본: 옆의 server\ 파일, 없으면 GitHub raw),
#   HERDR_REMOTE_HC(hc.exe 경로), HERDR_REMOTE_PORT(기본 8790), HERDR_REMOTE_PYTHON(python.exe 경로),
#   HERDR_REMOTE_TAILSCALE(tailscale CLI 경로), HERDR_REMOTE_SKIP_TAILSCALE=1(tailscale 점검·serve·pair 건너뜀, 시험용),
#   HERDR_REMOTE_HEALTH_TRIES(헬스 확인 횟수, 기본 10)
# 서버는 작업 스케줄러("Sheltie herdr-remote")로 로그온할 때 데스크톱 세션에서 일반 권한으로 시작한다.
& {
  try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch {}
  $ProgressPreference = 'SilentlyContinue'
  $global:HRCode = 0

  # 서버 파일 기본 출처. 저장소 이름이 바뀌면 이 한 줄만 고친다.
  $DEFAULT_SRC = 'https://raw.githubusercontent.com/hyperfocuslab/sheltie/main/server/herdr_remote.py'
  $TASK = 'Sheltie herdr-remote'
  $LOCAL = $env:LOCALAPPDATA
  if (-not $LOCAL) { $LOCAL = Join-Path $env:USERPROFILE 'AppData\Local' }
  $DATA = Join-Path $LOCAL 'herdr-remote'
  $SERVER = Join-Path $DATA 'herdr_remote.py'
  $TASK_COPY = Join-Path $DATA 'windows-task.xml'
  $LOGFILE = Join-Path $DATA 'herdr-remote.log'
  $CONFIG_DIR = Join-Path $env:USERPROFILE '.config\herdr-remote'
  $CONFIG = Join-Path $CONFIG_DIR 'config.json'
  $DRY = $false
  $MODE = 'install'
  Remove-Item Env:HERDR_REMOTE_CONFIG -ErrorAction SilentlyContinue

  $TASK_XML_BUILTIN = @'
<?xml version="1.0" encoding="UTF-16"?>
<!-- install.ps1이 @…@ 자리를 채워 UTF-16으로 저장하고 schtasks /Create /XML로 등록한다. install.ps1 안의 내장 사본과 같아야 한다(테스트가 비교). -->
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Sheltie herdr-remote 서버. 로그온하면 데스크톱 세션에서 시작한다.</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>@USER@</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>@USER@</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>true</Hidden>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>999</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>@PYTHONW@</Command>
      <Arguments>"@SERVER@" serve</Arguments>
      <WorkingDirectory>@WORKDIR@</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
'@

  $HELP_TEXT = @(
    'herdr-remote 설치기(Windows). 사용: powershell -File install.ps1 [-DryRun] [-Uninstall] [-Help]',
    '  iex로 돌릴 때는 인자를 못 주므로 HERDR_REMOTE_UNINSTALL=1 로 제거할 수 있다.',
    '  환경변수: HERDR_REMOTE_SRC, HERDR_REMOTE_HC, HERDR_REMOTE_PORT(기본 8790), HERDR_REMOTE_PYTHON,',
    '  HERDR_REMOTE_TAILSCALE, HERDR_REMOTE_SKIP_TAILSCALE=1, HERDR_REMOTE_HEALTH_TRIES(기본 10)'
  )

  function Say([string]$Msg) { Write-Host $Msg }
  function Die([int]$Code, [string]$Msg) {
    [Console]::Error.WriteLine($Msg)
    $global:HRCode = $Code
    throw 'HRExit'
  }
  # 변경 명령은 여기로만 실행한다. dry-run이면 출력만 한다.
  function Run([string]$Exe, [string[]]$A, [switch]$Quiet) {
    if ($DRY) { Say ('+ ' + ((@($Exe) + $A) -join ' ')); return 0 }
    if ($Quiet) { & $Exe @A 2>$null | Out-Null } else { & $Exe @A | Out-Host }
    return $LASTEXITCODE
  }
  function Read-Config {
    if (-not (Test-Path -LiteralPath $CONFIG)) { return $null }
    try { return ([IO.File]::ReadAllText($CONFIG) | ConvertFrom-Json) } catch { return $null }
  }
  function Config-Port {
    $c = Read-Config
    if ($c -and $c.port) { return [int]$c.port }
    return 8790
  }
  function Find-Python {
    $over = $env:HERDR_REMOTE_PYTHON
    if ($over) {
      if (Test-Path -LiteralPath $over -PathType Leaf) { return $over }
      return $null
    }
    $py = Get-Command py -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($py) {
      $out = & $py.Source -3 -c 'import sys; print(sys.executable)' 2>$null
      $exe = ([string]($out | Select-Object -First 1)).Trim()
      if ($LASTEXITCODE -eq 0 -and $exe -and (Test-Path -LiteralPath $exe -PathType Leaf)) { return $exe }
    }
    foreach ($c in @(Get-Command python -CommandType Application -All -ErrorAction SilentlyContinue)) {
      if ($c.Source -notlike '*\WindowsApps\*') { return $c.Source }
    }
    return $null
  }
  function Find-Tailscale {
    $over = $env:HERDR_REMOTE_TAILSCALE
    if ($over) {
      if (Test-Path -LiteralPath $over -PathType Leaf) { return $over }
      return $null
    }
    $t = Get-Command tailscale -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($t) { return $t.Source }
    $p = Join-Path $env:ProgramFiles 'Tailscale\tailscale.exe'
    if (Test-Path -LiteralPath $p -PathType Leaf) { return $p }
    return $null
  }
  function Find-Hc {
    $over = $env:HERDR_REMOTE_HC
    if ($over) {
      if (Test-Path -LiteralPath $over -PathType Leaf) { return $over }
      return $null
    }
    $p = Join-Path $LOCAL 'Programs\herdr-core\hc.exe'
    if (Test-Path -LiteralPath $p -PathType Leaf) { return $p }
    $h = Get-Command hc -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($h) { return $h.Source }
    return $null
  }
  function Funnel-On {
    # 이 포트에 Funnel(인터넷 공개)이 켜져 있으면 true. 판정 못 하면 false.
    $raw = (& $TS serve status --json 2>$null | Out-String)
    try { $d = $raw | ConvertFrom-Json } catch { return $false }
    if (-not $d -or -not $d.AllowFunnel) { return $false }
    foreach ($p in $d.AllowFunnel.PSObject.Properties) {
      if ($p.Value -and $p.Name.EndsWith(":$PORT")) { return $true }
    }
    return $false
  }
  function Fill-Template([string]$Text, $Map) {
    foreach ($k in $Map.Keys) {
      $Text = $Text.Replace("@$k@", [Security.SecurityElement]::Escape([string]$Map[$k]))
    }
    return $Text
  }

  try {
    foreach ($a in $args) {
      switch -Regex ([string]$a) {
        '^--?(dry-?run)$' { $DRY = $true }
        '^--?uninstall$' { $MODE = 'uninstall' }
        '^(--?help|-h|-\?)$' { foreach ($l in $HELP_TEXT) { Say $l }; return }
        default { Die 2 "알 수 없는 옵션: $a (-DryRun, -Uninstall)" }
      }
    }
    if ($env:HERDR_REMOTE_UNINSTALL -eq '1') { $MODE = 'uninstall' }

    $PORT = $env:HERDR_REMOTE_PORT
    if ($PORT) {
      if ($PORT -notmatch '^[0-9]+$') { Die 2 "HERDR_REMOTE_PORT는 숫자여야 한다: $PORT" }
      if ($PORT.Length -gt 5 -or [int]$PORT -lt 1 -or [int]$PORT -gt 65535) { Die 2 "포트 범위는 1~65535: $PORT" }
    }

    if ($MODE -eq 'uninstall') {
      if (-not $PORT) { $PORT = [string](Config-Port) }
      $rc = Run 'schtasks' @('/End', '/TN', $TASK) -Quiet
      $rc = Run 'schtasks' @('/Delete', '/TN', $TASK, '/F') -Quiet
      if ($rc -ne 0) { Say '(등록된 작업이 없다)' }
      if ($env:HERDR_REMOTE_SKIP_TAILSCALE -ne '1') {
        $TS = Find-Tailscale
        if ($TS) {
          $rc = Run $TS @('serve', "--https=$PORT", 'off')
          if ($rc -ne 0) { Say 'tailscale serve 해제 실패(이미 없을 수 있다)' }
        }
      }
      if ($DRY) { Say "+ 삭제 $SERVER $TASK_COPY" }
      else { Remove-Item -LiteralPath $SERVER, $TASK_COPY -Force -ErrorAction SilentlyContinue }
      Say "해제 완료. 설정과 토큰은 남겼다: $CONFIG_DIR (지우려면 직접 삭제)"
      return
    }

    # ---- 1. 점검
    if ([Environment]::OSVersion.Platform -ne 'Win32NT') { Die 1 'Windows에서만 설치할 수 있다.' }

    $PY = Find-Python
    if (-not $PY) { Die 1 "python이 없다. https://www.python.org/downloads/ 에서 3.9 이상을 설치한 뒤 다시 실행해라(설치 화면에서 'Add python.exe to PATH'를 켜라). 다른 위치면 HERDR_REMOTE_PYTHON=<경로>로 알려줘라." }
    & $PY -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { Die 1 "python이 3.9보다 낮다($PY). python.org에서 새 버전을 설치해라." }
    $PYW = Join-Path (Split-Path -Parent $PY) 'pythonw.exe'
    if (-not (Test-Path -LiteralPath $PYW -PathType Leaf)) { $PYW = $PY }

    $HC = Find-Hc
    if (-not $HC) { Die 1 'hc(herdr-core)가 없다. 먼저 herdr-core를 설치해라: powershell -c "irm https://releases.herdr.dev/core/install.ps1 | iex" (설치되면 %LOCALAPPDATA%\Programs\herdr-core\hc.exe가 생긴다). 다른 위치면 HERDR_REMOTE_HC=<경로>로 알려줘라.' }

    $TS = $null
    if ($env:HERDR_REMOTE_SKIP_TAILSCALE -ne '1') {
      $TS = Find-Tailscale
      if (-not $TS) { Die 1 'tailscale이 없다. https://tailscale.com/download 에서 설치하고 로그인한 뒤 다시 실행해라.' }
      $running = $false
      try {
        $st = (& $TS status --json 2>$null | Out-String) | ConvertFrom-Json
        $running = ($st.BackendState -eq 'Running')
      } catch {}
      if (-not $running) { Die 1 'tailscale이 로그인돼 있지 않다. Tailscale 앱을 열어 로그인한 뒤 다시 실행해라.' }
    }

    # ---- 2. 서버 파일
    $SRC = $env:HERDR_REMOTE_SRC
    # irm | iex로 돌면 스크립트 경로가 없다. 파일로 실행했을 때만 옆 파일을 쓴다.
    $HERE = ''
    $self = $PSCommandPath
    if ($self -and $self -like '*install.ps1' -and (Test-Path -LiteralPath $self -PathType Leaf)) { $HERE = Split-Path -Parent $self }
    if (-not $SRC -and $HERE -and (Test-Path -LiteralPath (Join-Path $HERE 'server\herdr_remote.py') -PathType Leaf)) {
      $SRC = Join-Path $HERE 'server\herdr_remote.py'
    }
    if (-not $SRC) { $SRC = $DEFAULT_SRC }
    if ($SRC -like 'http://*') { Die 1 "서버 파일은 https로만 받는다: $SRC" }

    if ($DRY) {
      Say "+ 폴더 만들기 $DATA"
      Say "+ 복사 $SRC -> $SERVER"
    } else {
      New-Item -ItemType Directory -Force -Path $DATA | Out-Null
      $tmp = Join-Path $DATA ('herdr_remote.' + [guid]::NewGuid().ToString('N').Substring(0, 8) + '.tmp')
      try {
        if ($SRC -like 'https://*') {
          try {
            [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
          } catch {}
          try { Invoke-WebRequest -UseBasicParsing -Uri $SRC -OutFile $tmp } catch { Die 1 "서버 파일을 받지 못했다: $SRC" }
        } else {
          if (-not (Test-Path -LiteralPath $SRC -PathType Leaf)) { Die 1 "서버 파일이 없다: $SRC" }
          Copy-Item -LiteralPath $SRC -Destination $tmp -Force
        }
        if (-not (Test-Path -LiteralPath $tmp) -or (Get-Item -LiteralPath $tmp).Length -eq 0) { Die 1 "받은 서버 파일이 비어 있다: $SRC" }
        & $PY -c "import sys; compile(open(sys.argv[1], encoding='utf-8-sig').read(), sys.argv[1], 'exec')" $tmp 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) { Die 1 "받은 서버 파일이 올바른 파이썬이 아니다: $SRC" }
        # 이미 도는 서버가 파일을 쥐고 있을 수 있으니 교체 전에 끝낸다.
        $rc = Run 'schtasks' @('/End', '/TN', $TASK) -Quiet
        Move-Item -LiteralPath $tmp -Destination $SERVER -Force
      } finally {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
      }
    }
    if ($DRY) { Say "+ $PY $SERVER init" }
    else {
      & $PY $SERVER init | Out-Host
      # 설정에 hc 경로와(지정했으면) 포트를 반영한다.
      $c = Read-Config
      if (-not $c) { Die 1 "설정 파일을 읽지 못했다: $CONFIG" }
      try {
        $c | Add-Member -NotePropertyName hc_path -NotePropertyValue $HC -Force
        if ($PORT) { $c | Add-Member -NotePropertyName port -NotePropertyValue ([int]$PORT) -Force }
        $utf8 = New-Object System.Text.UTF8Encoding($false)
        [IO.File]::WriteAllText("$CONFIG.tmp", ($c | ConvertTo-Json -Depth 10), $utf8)
        Move-Item -LiteralPath "$CONFIG.tmp" -Destination $CONFIG -Force
      } catch { Die 1 "설정 파일을 고치지 못했다: $CONFIG" }
      $PORT = [string](Config-Port)
    }
    if (-not $PORT) { $PORT = '8790' }

    # ---- 3. 작업 스케줄러
    $tpl = $TASK_XML_BUILTIN
    if ($HERE -and (Test-Path -LiteralPath (Join-Path $HERE 'deploy\windows-task.xml.in') -PathType Leaf)) {
      $tpl = [IO.File]::ReadAllText((Join-Path $HERE 'deploy\windows-task.xml.in'), (New-Object System.Text.UTF8Encoding($false)))
    }
    if ($DRY) {
      Say "+ $TASK_COPY 작성(pythonw=$PYW, server=$SERVER, log=$LOGFILE)"
    } else {
      $userName = [Security.Principal.WindowsIdentity]::GetCurrent().Name
      $xml = Fill-Template $tpl @{ PYTHONW = $PYW; SERVER = $SERVER; USER = $userName; WORKDIR = $DATA }
      try { [IO.File]::WriteAllText($TASK_COPY, $xml, [Text.Encoding]::Unicode) } catch { Die 1 '작업 XML을 만들지 못했다.' }
    }
    $rc = Run 'schtasks' @('/Create', '/TN', $TASK, '/XML', $TASK_COPY, '/F') -Quiet
    if ($rc -ne 0) {
      Die 5 "작업 스케줄러에 등록하지 못했다. 직접 실행해 봐라: schtasks /Create /TN `"$TASK`" /XML `"$TASK_COPY`" /F"
    }
    $rc = Run 'schtasks' @('/Run', '/TN', $TASK) -Quiet
    if ($rc -ne 0) {
      Die 5 "작업을 시작하지 못했다. 직접 실행해 봐라: schtasks /Run /TN `"$TASK`" (로그 확인: $LOGFILE)"
    }

    # ---- 4. tailscale serve (tailnet 전용, Funnel 아님)
    if ($TS) {
      if (-not $DRY -and (Funnel-On)) {
        [Console]::Error.WriteLine("tailscale Funnel이 포트 $PORT에 켜져 있다. 이대로면 서버가 인터넷에 공개된다.")
        [Console]::Error.WriteLine("Funnel을 끈 뒤 다시 실행해라: $TS funnel --https=$PORT off")
        [Console]::Error.WriteLine("(지금 설정 보기: $TS funnel status)")
        $global:HRCode = 8
        throw 'HRExit'
      }
      $stText = ''
      if (-not $DRY) { $stText = (& $TS serve status 2>$null | Out-String) }
      if (-not $DRY -and $stText.Contains(":$PORT ") -and $stText.Contains("proxy http://127.0.0.1:$PORT")) {
        Say "tailscale serve가 이미 설정돼 있다(:$PORT)."
      } else {
        $rc = Run $TS @('serve', '--bg', "--https=$PORT", "http://127.0.0.1:$PORT")
        if ($rc -ne 0) {
          Die 6 "tailscale serve 설정에 실패했다. Tailscale 관리 콘솔(DNS 탭)에서 HTTPS 인증서를 켰는지 확인하고, 직접 실행해 봐라: $TS serve --bg --https=$PORT http://127.0.0.1:$PORT"
        }
      }
    } else {
      Say '(tailscale 건너뜀)'
    }

    # ---- 5. health
    if ($DRY) {
      Say "+ GET http://127.0.0.1:$PORT/api/health (토큰 인증)"
    } else {
      $cfg = Read-Config
      $token = if ($cfg) { [string]$cfg.token } else { '' }
      $tries = 10
      if ($env:HERDR_REMOTE_HEALTH_TRIES -match '^[0-9]+$') { $tries = [int]$env:HERDR_REMOTE_HEALTH_TRIES }
      $ok = $false
      for ($i = 0; $i -lt $tries; $i++) {
        try {
          $r = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$PORT/api/health" -Headers @{ Authorization = "Bearer $token" } -TimeoutSec 3
          if ($r.StatusCode -eq 200) { $ok = $true; break }
        } catch {}
        Start-Sleep -Seconds 1
      }
      if (-not $ok) { Die 7 "서버가 응답하지 않는다. 로그를 확인해라: Get-Content -Tail 30 `"$LOGFILE`"" }
      Say "서버 동작 확인(포트 $PORT)."
    }

    # ---- 6. 페어링
    if ($TS) {
      if ($DRY) {
        Say "+ $PY $SERVER pair"
      } else {
        # 파이프로 받지 않고 콘솔에 그대로 낸다(QR 블록 문자가 깨지지 않게 UTF-8 고정).
        $env:PYTHONUTF8 = '1'
        & $PY $SERVER pair
        if ($LASTEXITCODE -ne 0) {
          Die 4 "설치는 끝났지만 pair 명령이 없거나 실패했다. 서버를 최신 버전으로 받아 다시 실행하거나, 앱에 주소와 토큰($CONFIG)을 직접 입력해라."
        }
      }
    } else {
      Say '(tailscale 없이 설치해서 pair는 건너뜀)'
    }
  } catch {
    if ($_.Exception.Message -ne 'HRExit') {
      [Console]::Error.WriteLine("예상하지 못한 오류: $($_.Exception.Message)")
      $global:HRCode = 1
    }
  }
} @args
# 변수 이름은 대소문자를 구분하지 않는다: $hrCode로 받으면 아래 Remove-Variable이 이 값까지 지운다(종료 코드가 항상 0이 된다).
$finalExit = $global:HRCode
Remove-Variable HRCode -Scope Global -ErrorAction SilentlyContinue
# 파일로 실행했을 때만 종료 코드를 넘긴다. iex로 돌 때 exit하면 창이 닫힌다.
if ($PSCommandPath) { exit $finalExit }
