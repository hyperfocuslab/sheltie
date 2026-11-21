# herdr-remote installer (Windows). Usage: powershell -File install.ps1 [-DryRun] [-Uninstall] [-NoHooks] [-Help]
#   Via irm | iex no arguments can be passed: use HERDR_REMOTE_UNINSTALL=1 / HERDR_REMOTE_DRY_RUN=1 instead.
#   Env: HERDR_REMOTE_SRC (server file https URL or local path; default: server\ next to this file, else GitHub raw),
#   HERDR_REMOTE_HC (hc.exe path), HERDR_REMOTE_PORT (default 8790), HERDR_REMOTE_PYTHON (python.exe path),
#   HERDR_REMOTE_TAILSCALE (tailscale CLI path), HERDR_REMOTE_SKIP_TAILSCALE=1 (skip tailscale check/serve/pair; for tests),
#   HERDR_REMOTE_HEALTH_TRIES (health check attempts, default 10), HERDR_REMOTE_STOP_WAIT (seconds to wait for the old
#   server to free its port before killing it, default 15)
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
  $NOHOOKS = $false
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
    no_hc_warn = 'aGVyZHItY29yZSDsl4bsnbQg7ISk7LmY7ZWc64ukOiDqs7Xsi50g7ZuF7Jy866Gc66eMIOuPmeyekSwgaGVyZHIg7YSw66+464SQIOq4sOuKpeydgCDqurzsp5AgKGhj66W8IOyTsOugpOuptCBIRVJEUl9SRU1PVEVfSEM9POqyveuhnD4p' # no_hc_warn
    hk_skip = 'KHswfTogezF9IOyXhuydjCwg7ZuFIOqxtOuEiOucgCk=' # hk_skip
    hk_added = 'ezB9ezF9IO2bhSDrk7HroZ06IHsyfQ==' # hk_added
    hk_updated = 'ezB9ezF9IO2bhSDqsLHsi6A6IHsyfQ==' # hk_updated
    hk_unchanged = 'ezB9IO2bheydtCDsnbTrr7gg65Ox66Gd64+8IOyeiOuLpDogezF9' # hk_unchanged
    hk_removed = 'ezB9ezF9IO2bhSDtlbTsoJw6IHsyfSAo67Cx7JeFIHsyfS5zaGVsdGllLWJhayDsnYAg64Ko6ri064ukKQ==' # hk_removed
    hk_absent = 'ezB9OiDsmrDrpqwg7ZuF7J20IOyXhuuLpCh7MX0p' # hk_absent
    hk_broken = '6rK96rOgOiB7MH0g7J20IOyYrOuwlOuluCBKU09O7J20IOyVhOuLiOudvCDqsbTrk5zrpqzsp4Ag7JWK7JWY64ukLiDqs6DsuZwg65KkIOuLpOyLnCDsi6TtlontlbTrnbwu' # hk_broken
    hk_fail = '6rK96rOgOiB7MH0g7ZuFIOyymOumrOyXkCDsi6TtjKjtlojri6QoezF9KS4g7ISk7LmY64qUIOqzhOyGje2VnOuLpC4=' # hk_fail
    hk_nopy = 'cHl0aG9u7J20IOyXhuyWtCBDbGF1ZGUgQ29kZcK3Q29kZXgg7ZuF7J2AIOq3uOuMgOuhnOuLpC4gJVVTRVJQUk9GSUxFJVwuY2xhdWRlXHNldHRpbmdzLmpzb24sICVVU0VSUFJPRklMRSVcLmNvZGV4XGhvb2tzLmpzb24g7JeQ7IScICdoZXJkcl9yZW1vdGUucHknIGhvb2sg7ZWt66qp7J2EIOyngeygkSDsp4Dsm4zrnbwu' # hk_nopy
    hk_off = 'KC1Ob0hvb2tzOiDtm4Ug65Ox66GdIOqxtOuEiOucgCk=' # hk_off
    hk_restart = 'Q2xhdWRlIENvZGXCt0NvZGV466W8IOyDiOuhnCDsi5zsnpHtlbTslbwg7ZuF7J20IOyggeyaqeuQnOuLpChDb2RleOuKlCAvaG9va3Mg7JeQ7IScIO2VnCDrsogg7Iug66Kw7ZW07JW8IO2VnOuLpCku' # hk_restart
    help5 = 'ICAtTm9Ib29rczogQ2xhdWRlIENvZGXCt0NvZGV4IO2bhSDrk7HroZ0o7ZW07KCcKeydhCDqsbTrhIjrnIAuIGhj6rCAIOyXhuyWtOuPhCDshKTsuZjripQg6rOE7IaN7ZWc64ukKOqzteyLnSDtm4XsnLzroZzrp4wg64+Z7J6RKS4=' # help5
    port_busy = '7Y+s7Yq4IHswfeydhCjrpbwpIOuLpOuluCDtlITroZzqt7jrnqjsnbQg6rOE7IaNIOyTsOqzoCDsnojri6QuIOq3uCDtlITroZzqt7jrnqjsnYQg64GE6rOgIOuLpOyLnCDsi6TtlontlbTrnbwu' # port still in use
    old_health = '7JibIOyEnOuyhOqwgCDslYTsp4Eg7J2R64u17ZWc64ukLiDsg4gg7ISc67KE66W8IOq4sOuLpOumrOuKlCDspJEuLi4=' # old server answered
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

  # Merge script for Claude Code / Codex hook files. Same text as in install.sh (a test compares them). Needs python.exe, not pythonw.
  $HOOKS_PY = @'
# ---- hooks_merge.py begin (install.sh and install.ps1 embed this identical text; a test compares them)
# usage: hooks_merge.py <install|uninstall> <claude|codex> <settings-file> <python-exe> <server-file> <posix|windows> [dry]
# Prints exactly one status word: added | updated | unchanged | removed | absent | broken | error
import json, os, shutil, sys

action, target, path, py, server, flavor = sys.argv[1:7]
dry = len(sys.argv) > 7 and sys.argv[7] == "dry"
MARK = "herdr_remote.py"


def ours(cmd):
    if not isinstance(cmd, str):
        return False
    i = cmd.find(MARK)
    return i >= 0 and cmd[i + len(MARK):i + len(MARK) + 7] in ('" hook ', "' hook ")


def spec():
    # (event, matcher or None, timeout seconds)
    if target == "claude":
        return [("PermissionRequest", "*", 600), ("PreToolUse", "AskUserQuestion|ExitPlanMode", 600),
                ("SessionStart", None, 10), ("SessionEnd", None, 10), ("UserPromptSubmit", None, 10), ("Stop", None, 10)]
    return [("PermissionRequest", "*", 600), ("SessionStart", None, 10), ("SessionEnd", None, 3),
            ("UserPromptSubmit", None, 10), ("Stop", None, 10)]


def handler(timeout):
    h = {"type": "command"}
    if flavor == "windows":
        if target == "claude":
            h["command"] = '& "%s" "%s" hook claude' % (py, server)
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


def main():
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
'@

  $HELP_TEXT = @((T 'help1'), (T 'help2'), (T 'help3'), (T 'help4'), (T 'help5'))

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
  # Add/remove our hook entries in ~/.claude/settings.json and ~/.codex/hooks.json (only agents that exist).
  function Apply-Hooks([string]$Act, [string]$Py) {
    $script = Join-Path ([IO.Path]::GetTempPath()) ('hooks_merge.' + [guid]::NewGuid().ToString('N').Substring(0, 8) + '.py')
    [IO.File]::WriteAllText($script, $HOOKS_PY, (New-Object System.Text.UTF8Encoding($false)))
    $pre = ''
    if ($DRY) { $pre = '(dry-run) ' }
    try {
      foreach ($agent in @('claude', 'codex')) {
        $dir = Join-Path $env:USERPROFILE ".$agent"
        $name = 'settings.json'
        if ($agent -eq 'codex') { $name = 'hooks.json' }
        $file = Join-Path $dir $name
        if (-not (Test-Path -LiteralPath $dir -PathType Container)) { Say (T 'hk_skip' $agent $dir); continue }
        $pa = @($script, $Act, $agent, $file, $Py, $SERVER, 'windows')
        if ($DRY) { $pa += 'dry' }
        $res = 'error'
        try { $res = "$(& $Py @pa 2>$null | Select-Object -First 1)".Trim() } catch { $res = 'error' }
        switch ($res) {
          'added' { Say (T 'hk_added' $pre $agent $file) }
          'updated' { Say (T 'hk_updated' $pre $agent $file) }
          'unchanged' { Say (T 'hk_unchanged' $agent $file) }
          'removed' { Say (T 'hk_removed' $pre $agent $file) }
          'absent' { Say (T 'hk_absent' $agent $file) }
          'broken' { [Console]::Error.WriteLine((T 'hk_broken' $file)) }
          default { [Console]::Error.WriteLine((T 'hk_fail' $agent $file)) }
        }
      }
    } finally {
      Remove-Item -LiteralPath $script -Force -ErrorAction SilentlyContinue
    }
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
  function Port-Open([int]$P) {
    # Listening sockets from the TCP table (a connect test is fooled by a full backlog). Only loopback/any:
    # tailscale serve listens on the same port number on the tailnet address.
    if (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue) {
      try {
        $l = @(Get-NetTCPConnection -LocalPort $P -State Listen -ErrorAction SilentlyContinue |
          Where-Object { @('127.0.0.1', '0.0.0.0', '::', '::1') -contains $_.LocalAddress })
        return ($l.Count -gt 0)
      } catch {}
    }
    $c = New-Object Net.Sockets.TcpClient
    try {
      $iar = $c.BeginConnect('127.0.0.1', $P, $null, $null)
      if (-not $iar.AsyncWaitHandle.WaitOne(500)) { return $false }
      $c.EndConnect($iar)
      return $true
    } catch { return $false } finally { $c.Close() }
  }
  # Stop the old server and wait until its port is free. schtasks /End returns before the process exits,
  # so without this wait the old server still answers health (false success) and the new one cannot bind.
  # If our server file still holds the port after the wait, kill that process (not hook calls).
  function Stop-Old([int[]]$Ports, [switch]$NoDie) {
    $rc = Run 'schtasks' @('/End', '/TN', $TASK) -Quiet
    if ($DRY -or $Ports.Count -eq 0) { return }
    $wait = 15
    if ($env:HERDR_REMOTE_STOP_WAIT -match '^[0-9]+$') { $wait = [int]$env:HERDR_REMOTE_STOP_WAIT }
    $deadline = (Get-Date).AddSeconds($wait)
    $killed = $false
    while ($true) {
      $busy = @($Ports | Select-Object -Unique | Where-Object { Port-Open $_ })
      if ($busy.Count -eq 0) { return }
      if ((Get-Date) -ge $deadline) {
        if ($killed) {
          if ($NoDie) { return }
          Die 9 (T 'port_busy' ($busy -join ', '))
        }
        foreach ($p in @(Get-CimInstance Win32_Process -Filter "Name like 'python%'" -ErrorAction SilentlyContinue)) {
          $cl = [string]$p.CommandLine
          if ($cl.IndexOf($SERVER, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and -not ($cl -match '\shook(\s|$)')) {
            Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
          }
        }
        $killed = $true
        $deadline = (Get-Date).AddSeconds(5)
      }
      Start-Sleep -Milliseconds 200
    }
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
        '^--?no-?hooks$' { $NOHOOKS = $true }
        '^(--?help|-h|-\?)$' { foreach ($l in $HELP_TEXT) { Say $l }; return }
        default { Die 2 (T 'unknown_opt' $a) }
      }
    }
    if ($env:HERDR_REMOTE_UNINSTALL -eq '1') { $MODE = 'uninstall' }
    if ($env:HERDR_REMOTE_DRY_RUN -eq '1') { $DRY = $true }
    if ($env:HERDR_REMOTE_NO_HOOKS -eq '1') { $NOHOOKS = $true }

    $PORT = $env:HERDR_REMOTE_PORT
    if ($PORT) {
      if ($PORT -notmatch '^[0-9]+$') { Die 2 (T 'port_nan' $PORT) }
      if ($PORT.Length -gt 5 -or [int]$PORT -lt 1 -or [int]$PORT -gt 65535) { Die 2 (T 'port_range' $PORT) }
    }

    if ($MODE -eq 'uninstall') {
      if (-not $PORT) { $PORT = [string](Config-Port) }
      Stop-Old @([int]$PORT) -NoDie
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
      if (-not $NOHOOKS) {
        $upy = Find-Python
        if ($upy) { Apply-Hooks 'uninstall' $upy } else { Say (T 'hk_nopy') }
      }
      Say (T 'uninstalled' $CONFIG_DIR)
      return
    }

    # ---- 1. checks
    # Health must come from a server started after this point (not the old one still exiting).
    $T0 = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0
    if ([Environment]::OSVersion.Platform -ne 'Win32NT') { Die 1 (T 'win_only') }

    $PY = Find-Python
    if (-not $PY) { Die 1 (T 'no_python') }
    & $PY -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { Die 1 (T 'old_python' $PY) }
    $PYW = Join-Path (Split-Path -Parent $PY) 'pythonw.exe'
    if (-not (Test-Path -LiteralPath $PYW -PathType Leaf)) { $PYW = $PY }

    $HC = Find-Hc
    if (-not $HC) { Say (T 'no_hc_warn'); $HC = '' }

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
        # A running server may hold the file and the port: stop it and wait for it to exit before replacing.
        # Old port only if a config exists (fresh install: nothing of ours is running).
        $ports = @()
        if (Test-Path -LiteralPath $CONFIG) { $ports += Config-Port }
        if ($PORT) { $ports += [int]$PORT }
        Stop-Old $ports
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

    # ---- 2b. Claude Code / Codex hooks (python.exe, not pythonw: the hook needs stdout)
    if ($NOHOOKS) { Say (T 'hk_off') } else { Apply-Hooks 'install' $PY }

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
      $warned = $false
      for ($i = 0; $i -lt $tries; $i++) {
        try {
          $r = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$PORT/api/health" -Headers @{ Authorization = "Bearer $token" } -TimeoutSec 3
          if ($r.StatusCode -eq 200) {
            $started = 0.0
            try {
              $body = $r.Content
              if ($body -is [byte[]]) { $body = [Text.Encoding]::UTF8.GetString($body) }  # PS 5.1 without Content-Type
              $started = [double](($body | ConvertFrom-Json).started)
            } catch {}
            if ($started -ge $T0) { $ok = $true; break }
            if (-not $warned) { Say (T 'old_health'); $warned = $true }
          }
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
    if (-not $NOHOOKS) { Say (T 'hk_restart') }
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
