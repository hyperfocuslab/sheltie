# herdr-remote installer (Windows). Usage: powershell -File install.ps1 [-DryRun] [-Uninstall] [-Help]
#   Via irm | iex no arguments can be passed: use HERDR_REMOTE_UNINSTALL=1 / HERDR_REMOTE_DRY_RUN=1 instead.
#   Env: HERDR_REMOTE_SRC (server file https URL or local path; default: server\ next to this file, else GitHub raw),
#   HERDR_REMOTE_HC (hc.exe path), HERDR_REMOTE_PORT (default 8790), HERDR_REMOTE_PYTHON (python.exe path),
#   HERDR_REMOTE_TAILSCALE (tailscale CLI path), HERDR_REMOTE_SKIP_TAILSCALE=1 (skip tailscale check/serve/pair; for tests),
#   HERDR_REMOTE_HEALTH_TRIES (health check attempts, default 10)
# The server starts at logon via Task Scheduler ("Sheltie herdr-remote") in the desktop session, non-elevated.
# This file must stay pure ASCII with no BOM: irm | iex chokes on a BOM, and PS 5.1 -File reads BOM-less files as cp949.
# User-facing Korean text lives in $MSG below as Base64 of UTF-8 bytes.
& {
  try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch {}
  $ProgressPreference = 'SilentlyContinue'
  $global:HRCode = 0

  # Default source of the server file. Change only this line if the repo is renamed.
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

  # ---- user-facing messages: Base64 of UTF-8 text, {0} {1} are -f placeholders. Keep keys in sync with T calls.
  # messages begin
  $MSG = @{
    xml_comment = 'PCEtLSBpbnN0YWxsLnBzMeydtCBA4oCmQCDsnpDrpqzrpbwg7LGE7JuMIFVURi0xNuycvOuhnCDsoIDsnqXtlZjqs6Agc2NodGFza3MgL0NyZWF0ZSAvWE1M66GcIOuTseuhne2VnOuLpC4gaW5zdGFsbC5wczEg7JWI7J2YIOuCtOyepSDsgqzrs7jqs7wg6rCZ7JWE7JW8IO2VnOuLpCjthYzsiqTtirjqsIAg67mE6rWQKS4gLS0+' # XML comment line
    xml_desc = 'U2hlbHRpZSBoZXJkci1yZW1vdGUg7ISc67KELiDroZzqt7jsmKjtlZjrqbQg642w7Iqk7YGs7YaxIOyEuOyFmOyXkOyEnCDsi5zsnpHtlZzri6Qu' # task description
    help1 = 'aGVyZHItcmVtb3RlIOyEpOy5mOq4sChXaW5kb3dzKS4g7IKs7JqpOiBwb3dlcnNoZWxsIC1GaWxlIGluc3RhbGwucHMxIFstRHJ5UnVuXSBbLVVuaW5zdGFsbF0gWy1IZWxwXQ==' # help line 1
    help2 = 'ICBpZXjroZwg64+M66a0IOuVjOuKlCDsnbjsnpDrpbwg66q7IOyjvOuvgOuhnCBIRVJEUl9SRU1PVEVfVU5JTlNUQUxMPTEg66GcIOygnOqxsO2VoCDsiJgg7J6I64ukLg==' # help line 2
    help3 = 'ICDtmZjqsr3rs4DsiJg6IEhFUkRSX1JFTU9URV9TUkMsIEhFUkRSX1JFTU9URV9IQywgSEVSRFJfUkVNT1RFX1BPUlQo6riw67O4IDg3OTApLCBIRVJEUl9SRU1PVEVfUFlUSE9OLA==' # help line 3
    help4 = 'ICBIRVJEUl9SRU1PVEVfVEFJTFNDQUxFLCBIRVJEUl9SRU1PVEVfU0tJUF9UQUlMU0NBTEU9MSwgSEVSRFJfUkVNT1RFX0hFQUxUSF9UUklFUyjquLDrs7ggMTAp' # help line 4
    unknown_opt = '7JWMIOyImCDsl4bripQg7Ji17IWYOiB7MH0gKC1EcnlSdW4sIC1Vbmluc3RhbGwp' # unknown option
    port_nan = 'SEVSRFJfUkVNT1RFX1BPUlTripQg7Iir7J6Q7Jes7JW8IO2VnOuLpDogezB9' # port must be numeric
    port_range = '7Y+s7Yq4IOuylOychOuKlCAxfjY1NTM1OiB7MH0=' # port range
    no_task = 'KOuTseuhneuQnCDsnpHsl4XsnbQg7JeG64ukKQ==' # no registered task
    serve_off_fail = 'dGFpbHNjYWxlIHNlcnZlIO2VtOygnCDsi6TtjKgo7J2066+4IOyXhuydhCDsiJgg7J6I64ukKQ==' # serve off failed
    dry_delete = 'KyDsgq3soJwgezB9IHsxfQ==' # dry-run delete
    uninstalled = '7ZW07KCcIOyZhOujjC4g7ISk7KCV6rO8IO2GoO2BsOydgCDrgqjqsrzri6Q6IHswfSAo7KeA7Jqw66Ck66m0IOyngeygkSDsgq3soJwp' # uninstall done
    win_only = 'V2luZG93c+yXkOyEnOunjCDshKTsuZjtlaAg7IiYIOyeiOuLpC4=' # Windows only
    no_python = 'cHl0aG9u7J20IOyXhuuLpC4gaHR0cHM6Ly93d3cucHl0aG9uLm9yZy9kb3dubG9hZHMvIOyXkOyEnCAzLjkg7J207IOB7J2EIOyEpOy5mO2VnCDrkqQg64uk7IucIOyLpO2Wie2VtOudvCjshKTsuZgg7ZmU66m07JeQ7IScICdBZGQgcHl0aG9uLmV4ZSB0byBQQVRIJ+ulvCDsvJzrnbwpLiDri6Trpbgg7JyE7LmY66m0IEhFUkRSX1JFTU9URV9QWVRIT049POqyveuhnD7roZwg7JWM66Ck7KSY6528Lg==' # python missing
    old_python = 'cHl0aG9u7J20IDMuOeuztOuLpCDrgq7ri6QoezB9KS4gcHl0aG9uLm9yZ+yXkOyEnCDsg4gg67KE7KCE7J2EIOyEpOy5mO2VtOudvC4=' # python too old
    no_hc = 'aGMoaGVyZHItY29yZSnqsIAg7JeG64ukLiDrqLzsoIAgaGVyZHItY29yZeulvCDshKTsuZjtlbTrnbw6IHBvd2Vyc2hlbGwgLWMgImlybSBodHRwczovL3JlbGVhc2VzLmhlcmRyLmRldi9jb3JlL2luc3RhbGwucHMxIHwgaWV4IiAo7ISk7LmY65CY66m0ICVMT0NBTEFQUERBVEElXFByb2dyYW1zXGhlcmRyLWNvcmVcaGMuZXhl6rCAIOyDneq4tOuLpCkuIOuLpOuluCDsnITsuZjrqbQgSEVSRFJfUkVNT1RFX0hDPTzqsr3roZw+66GcIOyVjOugpOykmOudvC4=' # hc missing
    no_ts = 'dGFpbHNjYWxl7J20IOyXhuuLpC4gaHR0cHM6Ly90YWlsc2NhbGUuY29tL2Rvd25sb2FkIOyXkOyEnCDshKTsuZjtlZjqs6Ag66Gc6re47J247ZWcIOuSpCDri6Tsi5wg7Iuk7ZaJ7ZW06528Lg==' # tailscale missing
    ts_logged_out = 'dGFpbHNjYWxl7J20IOuhnOq3uOyduOuPvCDsnojsp4Ag7JWK64ukLiBUYWlsc2NhbGUg7JWx7J2EIOyXtOyWtCDroZzqt7jsnbjtlZwg65KkIOuLpOyLnCDsi6TtlontlbTrnbwu' # tailscale logged out
    src_https = '7ISc67KEIO2MjOydvOydgCBodHRwc+uhnOunjCDrsJvripTri6Q6IHswfQ==' # https only
    dry_mkdir = 'KyDtj7TrjZQg66eM65Ok6riwIHswfQ==' # dry-run mkdir
    dry_copy = 'KyDrs7XsgqwgezB9IC0+IHsxfQ==' # dry-run copy
    dl_fail = '7ISc67KEIO2MjOydvOydhCDrsJvsp4Ag66q77ZaI64ukOiB7MH0=' # download failed
    src_missing = '7ISc67KEIO2MjOydvOydtCDsl4bri6Q6IHswfQ==' # source missing
    src_empty = '67Cb7J2AIOyEnOuyhCDtjIzsnbzsnbQg67mE7Ja0IOyeiOuLpDogezB9' # source empty
    src_badpy = '67Cb7J2AIOyEnOuyhCDtjIzsnbzsnbQg7Jis67CU66W4IO2MjOydtOyNrOydtCDslYTri4jri6Q6IHswfQ==' # source not valid python
    dry_init = 'KyB7MH0gezF9IGluaXQ=' # dry-run init
    cfg_read = '7ISk7KCVIO2MjOydvOydhCDsnb3sp4Ag66q77ZaI64ukOiB7MH0=' # config read failed
    cfg_write = '7ISk7KCVIO2MjOydvOydhCDqs6DsuZjsp4Ag66q77ZaI64ukOiB7MH0=' # config write failed
    dry_xml = 'KyB7MH0g7J6R7ISxKHB5dGhvbnc9ezF9LCBzZXJ2ZXI9ezJ9LCBsb2c9ezN9KQ==' # dry-run task xml
    xml_fail = '7J6R7JeFIFhNTOydhCDrp4zrk6Tsp4Ag66q77ZaI64ukLg==' # task xml failed
    create_fail = '7J6R7JeFIOyKpOy8gOykhOufrOyXkCDrk7HroZ3tlZjsp4Ag66q77ZaI64ukLiDsp4HsoJEg7Iuk7ZaJ7ZW0IOu0kOudvDogc2NodGFza3MgL0NyZWF0ZSAvVE4gInswfSIgL1hNTCAiezF9IiAvRg==' # schtasks create failed
    run_fail = '7J6R7JeF7J2EIOyLnOyeke2VmOyngCDrqrvtlojri6QuIOyngeygkSDsi6TtlontlbQg67SQ6528OiBzY2h0YXNrcyAvUnVuIC9UTiAiezB9IiAo66Gc6re4IO2ZleyduDogezF9KQ==' # schtasks run failed
    funnel1 = 'dGFpbHNjYWxlIEZ1bm5lbOydtCDtj6ztirggezB97JeQIOy8nOyguCDsnojri6QuIOydtOuMgOuhnOuptCDshJzrsoTqsIAg7J247YSw64S37JeQIOqzteqwnOuQnOuLpC4=' # funnel on
    funnel2 = 'RnVubmVs7J2EIOuBiCDrkqQg64uk7IucIOyLpO2Wie2VtOudvDogezB9IGZ1bm5lbCAtLWh0dHBzPXsxfSBvZmY=' # funnel off hint
    funnel3 = 'KOyngOq4iCDshKTsoJUg67O06riwOiB7MH0gZnVubmVsIHN0YXR1cyk=' # funnel status hint
    serve_exists = 'dGFpbHNjYWxlIHNlcnZl6rCAIOydtOuvuCDshKTsoJXrj7wg7J6I64ukKDp7MH0pLg==' # serve already set
    serve_fail = 'dGFpbHNjYWxlIHNlcnZlIOyEpOygleyXkCDsi6TtjKjtlojri6QuIFRhaWxzY2FsZSDqtIDrpqwg7L2Y7IaUKEROUyDtg60p7JeQ7IScIEhUVFBTIOyduOymneyEnOulvCDsvLDripTsp4Ag7ZmV7J247ZWY6rOgLCDsp4HsoJEg7Iuk7ZaJ7ZW0IOu0kOudvDogezB9IHNlcnZlIC0tYmcgLS1odHRwcz17MX0gaHR0cDovLzEyNy4wLjAuMTp7MX0=' # serve failed
    ts_skipped = 'KHRhaWxzY2FsZSDqsbTrhIjrnIAp' # tailscale skipped
    dry_health = 'KyBHRVQgaHR0cDovLzEyNy4wLjAuMTp7MH0vYXBpL2hlYWx0aCAo7Yag7YGwIOyduOymnSk=' # dry-run health
    no_health = '7ISc67KE6rCAIOydkeuLte2VmOyngCDslYrripTri6QuIOuhnOq3uOulvCDtmZXsnbjtlbTrnbw6IEdldC1Db250ZW50IC1UYWlsIDMwICJ7MH0i' # server not responding
    health_ok = '7ISc67KEIOuPmeyekSDtmZXsnbgo7Y+s7Yq4IHswfSku' # server ok
    dry_pair = 'KyB7MH0gezF9IHBhaXI=' # dry-run pair
    pair_fail = '7ISk7LmY64qUIOuBneuCrOyngOunjCBwYWlyIOuqheugueydtCDsl4bqsbDrgpgg7Iuk7Yyo7ZaI64ukLiDshJzrsoTrpbwg7LWc7IugIOuyhOyghOycvOuhnCDrsJvslYQg64uk7IucIOyLpO2Wie2VmOqxsOuCmCwg7JWx7JeQIOyjvOyGjOyZgCDthqDtgbAoezB9KeydhCDsp4HsoJEg7J6F66Cl7ZW06528Lg==' # pair failed
    pair_skipped = 'KHRhaWxzY2FsZSDsl4bsnbQg7ISk7LmY7ZW07IScIHBhaXLripQg6rG064SI65yAKQ==' # pair skipped
    unexpected = '7JiI7IOB7ZWY7KeAIOuqu+2VnCDsmKTrpZg6IHswfQ==' # unexpected error
  }
  # messages end
  function T([string]$Key) {
    $s = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($MSG[$Key]))
    if ($args.Count -gt 0) { return ($s -f $args) }
    return $s
  }

  $TASK_XML_BUILTIN = @'
<?xml version="1.0" encoding="UTF-16"?>
@XML_COMMENT@
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>@XML_DESC@</Description>
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

  $HELP_TEXT = @((T 'help1'), (T 'help2'), (T 'help3'), (T 'help4'))

  function Say([string]$Msg) { Write-Host $Msg }
  function Die([int]$Code, [string]$Msg) {
    [Console]::Error.WriteLine($Msg)
    $global:HRCode = $Code
    throw 'HRExit'
  }
  # Mutating commands run only through here. With dry-run it just prints.
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
      $exe = "$($out | Select-Object -First 1)".Trim()  # empty output (py launcher but no Python) must not throw
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
    # True if Funnel (public internet) is on for this port. False if it cannot be determined.
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
        default { Die 2 (T 'unknown_opt' $a) }
      }
    }
    if ($env:HERDR_REMOTE_UNINSTALL -eq '1') { $MODE = 'uninstall' }
    if ($env:HERDR_REMOTE_DRY_RUN -eq '1') { $DRY = $true }

    $PORT = $env:HERDR_REMOTE_PORT
    if ($PORT) {
      if ($PORT -notmatch '^[0-9]+$') { Die 2 (T 'port_nan' $PORT) }
      if ($PORT.Length -gt 5 -or [int]$PORT -lt 1 -or [int]$PORT -gt 65535) { Die 2 (T 'port_range' $PORT) }
    }

    if ($MODE -eq 'uninstall') {
      if (-not $PORT) { $PORT = [string](Config-Port) }
      $rc = Run 'schtasks' @('/End', '/TN', $TASK) -Quiet
      $rc = Run 'schtasks' @('/Delete', '/TN', $TASK, '/F') -Quiet
      if ($rc -ne 0) { Say (T 'no_task') }
      if ($env:HERDR_REMOTE_SKIP_TAILSCALE -ne '1') {
        $TS = Find-Tailscale
        if ($TS) {
          $rc = Run $TS @('serve', "--https=$PORT", 'off')
          if ($rc -ne 0) { Say (T 'serve_off_fail') }
        }
      }
      if ($DRY) { Say (T 'dry_delete' $SERVER $TASK_COPY) }
      else { Remove-Item -LiteralPath $SERVER, $TASK_COPY -Force -ErrorAction SilentlyContinue }
      Say (T 'uninstalled' $CONFIG_DIR)
      return
    }

    # ---- 1. checks
    if ([Environment]::OSVersion.Platform -ne 'Win32NT') { Die 1 (T 'win_only') }

    $PY = Find-Python
    if (-not $PY) { Die 1 (T 'no_python') }
    & $PY -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { Die 1 (T 'old_python' $PY) }
    $PYW = Join-Path (Split-Path -Parent $PY) 'pythonw.exe'
    if (-not (Test-Path -LiteralPath $PYW -PathType Leaf)) { $PYW = $PY }

    $HC = Find-Hc
    if (-not $HC) { Die 1 (T 'no_hc') }

    $TS = $null
    if ($env:HERDR_REMOTE_SKIP_TAILSCALE -ne '1') {
      $TS = Find-Tailscale
      if (-not $TS) { Die 1 (T 'no_ts') }
      $running = $false
      try {
        $st = (& $TS status --json 2>$null | Out-String) | ConvertFrom-Json
        $running = ($st.BackendState -eq 'Running')
      } catch {}
      if (-not $running) { Die 1 (T 'ts_logged_out') }
    }

    # ---- 2. server file
    $SRC = $env:HERDR_REMOTE_SRC
    # With irm | iex there is no script path. Use sibling files only when run as a file.
    $HERE = ''
    $self = $PSCommandPath
    if ($self -and $self -like '*install.ps1' -and (Test-Path -LiteralPath $self -PathType Leaf)) { $HERE = Split-Path -Parent $self }
    if (-not $SRC -and $HERE -and (Test-Path -LiteralPath (Join-Path $HERE 'server\herdr_remote.py') -PathType Leaf)) {
      $SRC = Join-Path $HERE 'server\herdr_remote.py'
    }
    if (-not $SRC) { $SRC = $DEFAULT_SRC }
    if ($SRC -like 'http://*') { Die 1 (T 'src_https' $SRC) }

    if ($DRY) {
      Say (T 'dry_mkdir' $DATA)
      Say (T 'dry_copy' $SRC $SERVER)
    } else {
      New-Item -ItemType Directory -Force -Path $DATA | Out-Null
      $tmp = Join-Path $DATA ('herdr_remote.' + [guid]::NewGuid().ToString('N').Substring(0, 8) + '.tmp')
      try {
        if ($SRC -like 'https://*') {
          try {
            [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
          } catch {}
          try { Invoke-WebRequest -UseBasicParsing -Uri $SRC -OutFile $tmp } catch { Die 1 (T 'dl_fail' $SRC) }
        } else {
          if (-not (Test-Path -LiteralPath $SRC -PathType Leaf)) { Die 1 (T 'src_missing' $SRC) }
          Copy-Item -LiteralPath $SRC -Destination $tmp -Force
        }
        if (-not (Test-Path -LiteralPath $tmp) -or (Get-Item -LiteralPath $tmp).Length -eq 0) { Die 1 (T 'src_empty' $SRC) }
        & $PY -c "import sys; compile(open(sys.argv[1], encoding='utf-8-sig').read(), sys.argv[1], 'exec')" $tmp 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) { Die 1 (T 'src_badpy' $SRC) }
        # A running server may hold the file: stop it before replacing.
        $rc = Run 'schtasks' @('/End', '/TN', $TASK) -Quiet
        Move-Item -LiteralPath $tmp -Destination $SERVER -Force
      } finally {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
      }
    }
    if ($DRY) { Say (T 'dry_init' $PY $SERVER) }
    else {
      & $PY $SERVER init | Out-Host
      # Reflect the hc path and (if given) the port in the config.
      $c = Read-Config
      if (-not $c) { Die 1 (T 'cfg_read' $CONFIG) }
      try {
        $c | Add-Member -NotePropertyName hc_path -NotePropertyValue $HC -Force
        if ($PORT) { $c | Add-Member -NotePropertyName port -NotePropertyValue ([int]$PORT) -Force }
        $utf8 = New-Object System.Text.UTF8Encoding($false)
        [IO.File]::WriteAllText("$CONFIG.tmp", ($c | ConvertTo-Json -Depth 10), $utf8)
        Move-Item -LiteralPath "$CONFIG.tmp" -Destination $CONFIG -Force
      } catch { Die 1 (T 'cfg_write' $CONFIG) }
      $PORT = [string](Config-Port)
    }
    if (-not $PORT) { $PORT = '8790' }

    # ---- 3. Task Scheduler
    $tpl = $TASK_XML_BUILTIN.Replace('@XML_COMMENT@', (T 'xml_comment')).Replace('@XML_DESC@', (T 'xml_desc'))
    if ($HERE -and (Test-Path -LiteralPath (Join-Path $HERE 'deploy\windows-task.xml.in') -PathType Leaf)) {
      $tpl = [IO.File]::ReadAllText((Join-Path $HERE 'deploy\windows-task.xml.in'), (New-Object System.Text.UTF8Encoding($false)))
    }
    if ($DRY) {
      Say (T 'dry_xml' $TASK_COPY $PYW $SERVER $LOGFILE)
    } else {
      $userName = [Security.Principal.WindowsIdentity]::GetCurrent().Name
      $xml = Fill-Template $tpl @{ PYTHONW = $PYW; SERVER = $SERVER; USER = $userName; WORKDIR = $DATA }
      try { [IO.File]::WriteAllText($TASK_COPY, $xml, [Text.Encoding]::Unicode) } catch { Die 1 (T 'xml_fail') }
    }
    $rc = Run 'schtasks' @('/Create', '/TN', $TASK, '/XML', $TASK_COPY, '/F') -Quiet
    if ($rc -ne 0) {
      Die 5 (T 'create_fail' $TASK $TASK_COPY)
    }
    $rc = Run 'schtasks' @('/Run', '/TN', $TASK) -Quiet
    if ($rc -ne 0) {
      Die 5 (T 'run_fail' $TASK $LOGFILE)
    }

    # ---- 4. tailscale serve (tailnet only, not Funnel)
    if ($TS) {
      if (-not $DRY -and (Funnel-On)) {
        [Console]::Error.WriteLine((T 'funnel1' $PORT))
        [Console]::Error.WriteLine((T 'funnel2' $TS $PORT))
        [Console]::Error.WriteLine((T 'funnel3' $TS))
        $global:HRCode = 8
        throw 'HRExit'
      }
      $stText = ''
      if (-not $DRY) { $stText = (& $TS serve status 2>$null | Out-String) }
      if (-not $DRY -and $stText.Contains(":$PORT ") -and $stText.Contains("proxy http://127.0.0.1:$PORT")) {
        Say (T 'serve_exists' $PORT)
      } else {
        $rc = Run $TS @('serve', '--bg', "--https=$PORT", "http://127.0.0.1:$PORT")
        if ($rc -ne 0) {
          Die 6 (T 'serve_fail' $TS $PORT)
        }
      }
    } else {
      Say (T 'ts_skipped')
    }

    # ---- 5. health
    if ($DRY) {
      Say (T 'dry_health' $PORT)
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
      if (-not $ok) { Die 7 (T 'no_health' $LOGFILE) }
      Say (T 'health_ok' $PORT)
    }

    # ---- 6. pairing
    if ($TS) {
      if ($DRY) {
        Say (T 'dry_pair' $PY $SERVER)
      } else {
        # Print straight to the console, not through a pipe (UTF-8 forced so QR block characters do not break).
        $env:PYTHONUTF8 = '1'
        & $PY $SERVER pair
        if ($LASTEXITCODE -ne 0) {
          Die 4 (T 'pair_fail' $CONFIG)
        }
      }
    } else {
      Say (T 'pair_skipped')
    }
  } catch {
    if ($_.Exception.Message -ne 'HRExit') {
      [Console]::Error.WriteLine((T 'unexpected' $_.Exception.Message)); if ($env:HERDR_REMOTE_DEBUG -eq '1') { [Console]::Error.WriteLine($_.InvocationInfo.PositionMessage) }
      $global:HRCode = 1
    }
  }
} @args
# Variable names are case-insensitive: capturing into $hrCode would make the Remove-Variable below delete this value too (exit code always 0).
$finalExit = $global:HRCode
Remove-Variable HRCode -Scope Global -ErrorAction SilentlyContinue
# Pass the exit code only when run as a file. exit under iex would close the window.
if ($PSCommandPath) { exit $finalExit }
