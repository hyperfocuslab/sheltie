# Sheltie installer (Windows). Usage: powershell -File install.ps1 [-DryRun] [-Uninstall] [-NoHooks] [-NoHc] [-NoAutoUpdate] [-Help]
#   -NoAutoUpdate: turn off server auto-update (config auto_update: false; kept on reinstall).
#   -Update: called by the server's auto-update, not by people. Replaces the server with SHELTIE_SRC keeping config, token
#   and devices, skips tailscale/pairing/hc install, re-registers hooks, restarts the task. If the new version does not
#   answer /api/health within SHELTIE_UPDATE_WAIT seconds (default 120) it restores sheltie.py.prev. The result goes to
#   update_status.json in the config folder. SHELTIE_TASK (task name, default Sheltie): for tests, so a test task does not
#   collide with the real one; the test task's server also gets USERPROFILE/LOCALAPPDATA/SHELTIE_TASK/SHELTIE_ENV_KEY.
#   Via irm | iex no arguments can be passed: use SHELTIE_UNINSTALL=1 / SHELTIE_DRY_RUN=1 instead.
#   Env: SHELTIE_SRC (server file https URL or local path; default: server\ next to this file, else the latest server release),
#   SHELTIE_HC (hc.exe path), SHELTIE_PORT (default 8790), SHELTIE_PYTHON (python.exe path),
#   SHELTIE_TAILSCALE (tailscale CLI path), SHELTIE_SKIP_TAILSCALE=1 (skip tailscale check/serve/pair; for tests),
#   SHELTIE_HEALTH_TRIES (health check attempts, default 10), SHELTIE_STOP_WAIT (seconds to wait for the old
#   server to free its port before killing it, default 15)
#   -NoHc / SHELTIE_NO_HC=1: do not auto-install herdr-core when hc.exe is missing (otherwise the pinned
#   herdr-core zip is downloaded, checked against the SHA256 below and installed into
#   %LOCALAPPDATA%\Programs\herdr-core; failure never stops this install).
#   If the original URL (HC_BASE_URL) fails to download or the hash differs, the same file is fetched from our
#   mirror (HC_MIRROR_URL) and checked against the same hash.
#   SHELTIE_HC_URL / SHELTIE_HC_SHA256: override that zip source (https URL or local file, http refused; no mirror
#   fallback) and its expected hash (for tests).
#   Uninstall never removes herdr-core.
#   Also creates %LOCALAPPDATA%\sheltie\bin\sheltie.cmd (run: sheltie run gemini) and adds that folder to the
#   user PATH (HKCU\Environment, or the key in SHELTIE_ENV_KEY; for tests). Uninstall removes both.
# The server starts at logon via Task Scheduler ("Sheltie") in the desktop session, non-elevated.
# This file must stay pure ASCII with no BOM: irm | iex chokes on a BOM, and PS 5.1 -File reads BOM-less files as cp949.
# User-facing Korean text lives in $MSG below as Base64 of UTF-8 bytes.
& {
  try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch {}
  $ProgressPreference = 'SilentlyContinue'
  $global:HRCode = 0

  # Pinned herdr-core version. If you change it, change server/sheltie.py HC_TESTED_VERSIONS too; measurements live in docs/plans/standalone-runner.md.
  $HC_PIN_VERSION = '0.1.21'
  $HC_SHA_WIN = 'a8984634c35b58ba82d0aec078d5a5e6004da01d5a25b3daf822ab5e31c7312b'
  $HC_BASE_URL = "https://releases.herdr.dev/core/$HC_PIN_VERSION"
  # Mirror used when the original URL fails (same file names and hashes). Must equal HC_MIRROR_URL in install.sh (tested).
  $HC_MIRROR_URL = "https://github.com/hyperfocuslab/sheltie/releases/download/hc-$HC_PIN_VERSION"
  # Default source of the server file: the latest server release asset (ops/release-server.sh), so only released code is
  # installed. It must match the sha256 in manifest.json next to it. Change only this line if the repo is renamed.
  $DEFAULT_SRC = 'https://github.com/hyperfocuslab/sheltie/releases/latest/download/sheltie.py'
  $TASK = $env:SHELTIE_TASK
  if (-not $TASK) { $TASK = 'Sheltie' }
  # Result file of server auto-update (-Update) in the config folder. Same as UPDATE_STATUS_FILE in server/sheltie.py (tested).
  $UPDATE_STATUS_NAME = 'update_status.json'
  $LOCAL = $env:LOCALAPPDATA
  if (-not $LOCAL) { $LOCAL = Join-Path $env:USERPROFILE 'AppData\Local' }
  $DATA = Join-Path $LOCAL 'sheltie'
  $SERVER = Join-Path $DATA 'sheltie.py'
  $TASK_COPY = Join-Path $DATA 'windows-task.xml'
  $LOGFILE = Join-Path $DATA 'sheltie.log'
  $CONFIG_DIR = Join-Path $env:USERPROFILE '.config\sheltie'
  $CONFIG = Join-Path $CONFIG_DIR 'config.json'
  # Previous install paths: moved and cleaned up on install.
  $OLD_TASK = 'Sheltie herdr-remote'
  $OLD_DATA = Join-Path $LOCAL 'herdr-remote'
  $OLD_SERVER = Join-Path $OLD_DATA 'herdr_remote.py'
  $OLD_CONFIG_DIR = Join-Path $env:USERPROFILE '.config\herdr-remote'
  $OLD_SERVER_NAME = 'herdr_remote.py'
  $DRY = $false
  $MODE = 'install'
  $NOHOOKS = $false
  $NOHC = $false
  $NOAUTO = $false
  $CMD_DIR = Join-Path $DATA 'bin'
  $CMD_PATH = Join-Path $CMD_DIR 'sheltie.cmd'
  $CMD_MARK = 'rem sheltie-command'
  $ENVKEY = $env:SHELTIE_ENV_KEY
  if (-not $ENVKEY) { $ENVKEY = 'HKCU:\Environment' }
  Remove-Item Env:SHELTIE_CONFIG -ErrorAction SilentlyContinue

  # ---- user-facing messages: Base64 of UTF-8 text, {0} {1} are -f placeholders. Keep keys in sync with T calls.
  # messages begin
  $MSG = @{
    xml_comment = 'PCEtLSBpbnN0YWxsLnBzMeydtCBA4oCmQCDsnpDrpqzrpbwg7LGE7JuMIFVURi0xNuycvOuhnCDsoIDsnqXtlZjqs6Agc2NodGFza3MgL0NyZWF0ZSAvWE1M66GcIOuTseuhne2VnOuLpC4gaW5zdGFsbC5wczEg7JWI7J2YIOuCtOyepSDsgqzrs7jqs7wg6rCZ7JWE7JW8IO2VnOuLpCjthYzsiqTtirjqsIAg67mE6rWQKS4gLS0+' # XML comment line
    xml_desc = 'U2hlbHRpZSDshJzrsoQuIOuhnOq3uOyYqO2VmOuptCDrjbDsiqTtgazthrEg7IS47IWY7JeQ7IScIOyLnOyeke2VnOuLpC4=' # task description
    help1 = 'U2hlbHRpZSDshKTsuZjquLAoV2luZG93cykuIOyCrOyaqTogcG93ZXJzaGVsbCAtRmlsZSBpbnN0YWxsLnBzMSBbLURyeVJ1bl0gWy1Vbmluc3RhbGxdIFstSGVscF0=' # help line 1
    help2 = 'ICBpZXjroZwg64+M66a0IOuVjOuKlCDsnbjsnpDrpbwg66q7IOyjvOuvgOuhnCBTSEVMVElFX1VOSU5TVEFMTD0xIOuhnCDsoJzqsbDtlaAg7IiYIOyeiOuLpC4=' # help line 2
    help3 = 'ICDtmZjqsr3rs4DsiJg6IFNIRUxUSUVfU1JDLCBTSEVMVElFX0hDLCBTSEVMVElFX1BPUlQo6riw67O4IDg3OTApLCBTSEVMVElFX1BZVEhPTiw=' # help line 3
    help4 = 'ICBTSEVMVElFX1RBSUxTQ0FMRSwgU0hFTFRJRV9TS0lQX1RBSUxTQ0FMRT0xLCBTSEVMVElFX0hFQUxUSF9UUklFUyjquLDrs7ggMTAp' # help line 4
    unknown_opt = '7JWMIOyImCDsl4bripQg7Ji17IWYOiB7MH0gKC1EcnlSdW4sIC1Vbmluc3RhbGwp' # unknown option
    port_nan = 'U0hFTFRJRV9QT1JU64qUIOyIq+yekOyXrOyVvCDtlZzri6Q6IHswfQ==' # port must be numeric
    port_range = '7Y+s7Yq4IOuylOychOuKlCAxfjY1NTM1OiB7MH0=' # port range
    no_task = 'KOuTseuhneuQnCDsnpHsl4XsnbQg7JeG64ukKQ==' # no registered task
    serve_off_fail = 'dGFpbHNjYWxlIHNlcnZlIO2VtOygnCDsi6TtjKgo7J2066+4IOyXhuydhCDsiJgg7J6I64ukKQ==' # serve off failed
    dry_delete = 'KyDsgq3soJwgezB9IHsxfQ==' # dry-run delete
    uninstalled = '7ZW07KCcIOyZhOujjC4g7ISk7KCV6rO8IO2GoO2BsOydgCDrgqjqsrzri6Q6IHswfSAo7KeA7Jqw66Ck66m0IOyngeygkSDsgq3soJwp' # uninstall done
    win_only = 'V2luZG93c+yXkOyEnOunjCDshKTsuZjtlaAg7IiYIOyeiOuLpC4=' # Windows only
    no_python = 'cHl0aG9u7J20IOyXhuuLpC4gaHR0cHM6Ly93d3cucHl0aG9uLm9yZy9kb3dubG9hZHMvIOyXkOyEnCAzLjkg7J207IOB7J2EIOyEpOy5mO2VnCDrkqQg64uk7IucIOyLpO2Wie2VtOudvCjshKTsuZgg7ZmU66m07JeQ7IScICdBZGQgcHl0aG9uLmV4ZSB0byBQQVRIJ+ulvCDsvJzrnbwpLiDri6Trpbgg7JyE7LmY66m0IFNIRUxUSUVfUFlUSE9OPTzqsr3roZw+66GcIOyVjOugpOykmOudvC4=' # python missing
    old_python = 'cHl0aG9u7J20IDMuOeuztOuLpCDrgq7ri6QoezB9KS4gcHl0aG9uLm9yZ+yXkOyEnCDsg4gg67KE7KCE7J2EIOyEpOy5mO2VtOudvC4=' # python too old
    no_hc = 'aGMoaGVyZHItY29yZSnqsIAg7JeG64ukLiDrqLzsoIAgaGVyZHItY29yZeulvCDshKTsuZjtlbTrnbw6IHBvd2Vyc2hlbGwgLWMgImlybSBodHRwczovL3JlbGVhc2VzLmhlcmRyLmRldi9jb3JlL2luc3RhbGwucHMxIHwgaWV4IiAo7ISk7LmY65CY66m0ICVMT0NBTEFQUERBVEElXFByb2dyYW1zXGhlcmRyLWNvcmVcaGMuZXhl6rCAIOyDneq4tOuLpCkuIOuLpOuluCDsnITsuZjrqbQgU0hFTFRJRV9IQz086rK966GcPuuhnCDslYzroKTspJjrnbwu' # hc missing
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
    no_hc_warn = 'aGVyZHItY29yZSDsl4bsnbQg7ISk7LmY7ZWc64ukOiDqs7Xsi50g7ZuF7Jy866Gc66eMIOuPmeyekSwgaGVyZHIg7YSw66+464SQIOq4sOuKpeydgCDqurzsp5AgKGhj66W8IOyTsOugpOuptCBTSEVMVElFX0hDPTzqsr3roZw+KQ==' # no_hc_warn
    hk_skip = 'KHswfTogezF9IOyXhuydjCwg7ZuFIOqxtOuEiOucgCk=' # hk_skip
    hk_added = 'ezB9ezF9IO2bhSDrk7HroZ06IHsyfQ==' # hk_added
    hk_updated = 'ezB9ezF9IO2bhSDqsLHsi6A6IHsyfQ==' # hk_updated
    hk_unchanged = 'ezB9IO2bheydtCDsnbTrr7gg65Ox66Gd64+8IOyeiOuLpDogezF9' # hk_unchanged
    hk_removed = 'ezB9ezF9IO2bhSDtlbTsoJw6IHsyfSAo67Cx7JeFIHsyfS5zaGVsdGllLWJhayDsnYAg64Ko6ri064ukKQ==' # hk_removed
    hk_absent = 'ezB9OiDsmrDrpqwg7ZuF7J20IOyXhuuLpCh7MX0p' # hk_absent
    hk_broken = '6rK96rOgOiB7MH0g7J20IOyYrOuwlOuluCBKU09O7J20IOyVhOuLiOudvCDqsbTrk5zrpqzsp4Ag7JWK7JWY64ukLiDqs6DsuZwg65KkIOuLpOyLnCDsi6TtlontlbTrnbwu' # hk_broken
    hk_fail = '6rK96rOgOiB7MH0g7ZuFIOyymOumrOyXkCDsi6TtjKjtlojri6QoezF9KS4g7ISk7LmY64qUIOqzhOyGje2VnOuLpC4=' # hk_fail
    hk_nopy = 'cHl0aG9u7J20IOyXhuyWtCDsl5DsnbTsoITtirgg7ZuF7J2AIOq3uOuMgOuhnOuLpC4gJVVTRVJQUk9GSUxFJVwuY2xhdWRlXHNldHRpbmdzLmpzb24sICVVU0VSUFJPRklMRSVcLmNvZGV4XGhvb2tzLmpzb24sICVVU0VSUFJPRklMRSVcLnF3ZW5cc2V0dGluZ3MuanNvbiDsl5DshJwgJ3NoZWx0aWUucHknIGhvb2sg7ZWt66qp7J2ELCAlVVNFUlBST0ZJTEUlXC5jb25maWdcb3BlbmNvZGVccGx1Z2luc1xzaGVsdGllLmpzIOulvCDsp4HsoJEg7KeA7JuM6528Lg==' # hk_nopy
    hk_off = 'KC1Ob0hvb2tzOiDtm4Ug65Ox66GdIOqxtOuEiOucgCk=' # hk_off
    hk_restart = 'Q2xhdWRlIENvZGXCt0NvZGV4wrdRd2VuIENvZGXCt09wZW5Db2Rl66W8IOyDiOuhnCDsi5zsnpHtlbTslbwg7ZuF7J20IOyggeyaqeuQnOuLpChDb2RleOuKlCAvaG9va3Mg7JeQ7IScIO2VnCDrsogg7Iug66Kw7ZW07JW8IO2VnOuLpCku' # hk_restart
    help5 = 'ICAtTm9Ib29rczogQ2xhdWRlIENvZGXCt0NvZGV4wrdRd2VuIENvZGUg7ZuF6rO8IE9wZW5Db2RlIO2UjOufrOq3uOyduCDrk7HroZ0o7ZW07KCcKeydhCDqsbTrhIjrnIAuIGhj6rCAIOyXhuyWtOuPhCDshKTsuZjripQg6rOE7IaN7ZWc64ukKOqzteyLnSDtm4XsnLzroZzrp4wg64+Z7J6RKS4=' # help5
    port_busy = '7Y+s7Yq4IHswfeydhCjrpbwpIOuLpOuluCDtlITroZzqt7jrnqjsnbQg6rOE7IaNIOyTsOqzoCDsnojri6QuIOq3uCDtlITroZzqt7jrnqjsnYQg64GE6rOgIOuLpOyLnCDsi6TtlontlbTrnbwu' # port still in use
    old_health = '7JibIOyEnOuyhOqwgCDslYTsp4Eg7J2R64u17ZWc64ukLiDsg4gg7ISc67KE66W8IOq4sOuLpOumrOuKlCDspJEuLi4=' # old server answered
    unexpected = '7JiI7IOB7ZWY7KeAIOuqu+2VnCDsmKTrpZg6IHswfQ==' # unexpected error
    mig_found = '7J207KCEIOyEpOy5mOulvCDssL7slZjri6QuIOyEnOu5hOyKpOulvCDrgrTrpqzqs6Ag7IOIIOychOy5mOuhnCDsmK7quLTri6Qu' # previous install found
    mig_keep = '6rK96rOgOiB7MH0g7J20IOydtOuvuCDsnojslrQg7J207KCEIOyEpOyglSh7MX0p7J2AIOyYruq4sOyngCDslYrqs6Ag6re464yA66GcIOuRlOuLpC4=' # new config exists, old kept
    mig_dry_mv = 'KyDsnbTrj5kgezB9IC0+IHsxfSAo7JWI7J2YIOqyveuhnOuPhCDsg4gg7Y+0642U66GcKQ==' # dry-run config move
    mig_fail = '7J207KCEIOyEpOyglSDtj7TrjZTrpbwg7Jiu6riw7KeAIOuqu+2WiOuLpDogezB9IC0+IHsxfS4g7JWE66y06rKD64+EIOyngOyasOyngCDslYrslZjri6QuIOq2jO2VnOydhCDtmZXsnbjtlZjqs6Ag64uk7IucIOyLpO2Wie2VtOudvC4=' # config move failed
    mig_fix_fail = '7ISk7KCVIO2PtOuNlOuKlCDsmK7qsrzsp4Drp4wg7JWI7J2YIOqyveuhnOulvCDqs6DsuZjsp4Ag66q77ZaI64ukOiB7MH0gKGFwbnNfa2V5X3BhdGgg65Ox7J2EIHsxfSDroZwg7KeB7KCRIOqzoOyzkOudvCk=' # config path fix failed
    mig_moved = '7ISk7KCV7J2EIOyYruqyvOuLpDogezB9IC0+IHsxfQ==' # config moved
    mig_link = 'ezB9IOqwgCDrp4Htgazri6QuIOunge2BrCDrjIDsg4Eg7Y+0642U66W8IHsxfSDroZwg7Jiu6riw6rGw64KYIHsxfSDrp4Htgazrpbwg7IOI66GcIOunjOuToCDrkqQg64uk7IucIOyLpO2Wie2VtOudvC4=' # old config is a link
    mig_hooks_left = '6rK96rOgOiDtm4XsnbQg7JWE7KeBIOydtOyghCDshJzrsoQg7YyM7J287J2EIOu2gOuluOuLpCh7MH0pLiB7MX0g7J2AIOyngOyasOyngCDslYrslZjri6QuIO2bheqzvCDtlajqu5ggaW5zdGFsbC5wczHsnYQg64uk7IucIOyLpO2Wie2VmOqxsOuCmCDqt7gg7YyM7J287JeQ7IScICd7Mn0nIGhvb2sg7ZWt66qp7J2EIOyngOybjOudvC4=' # old hooks left
    help6 = 'ICAtTm9IYzogaGMoaGVyZHItY29yZSnqsIAg7JeG7Ja064+EIOyekOuPmSDshKTsuZjtlZjsp4Ag7JWK64qU64ukLiDtmZjqsr3rs4DsiJggU0hFTFRJRV9OT19IQz0xLCDshKTsuZgg7Iqk7YGs66a97Yq4IOy2nOyymCBTSEVMVElFX0hDX0lOU1RBTExFUihodHRwcyBVUkwg65iQ64qUIOuhnOy7rCDqsr3roZwp' # help line 6
    hc_skip = 'KGhlcmRyLWNvcmUg7J6Q64+ZIOyEpOy5mCDqsbTrhIjrnIAp' # hc auto-install skipped
    hc_fail = 'aGVyZHItY29yZSDsnpDrj5kg7ISk7LmY66W8IOuquyDtlojri6QoezB9KS4=' # hc auto-install failed
    hc_http = 'aGVyZHItY29yZeuKlCBodHRwc+uhnOunjCDrsJvripTri6Q6IHswfQ==' # hc download https only
    hc_dry = 'KyBoZXJkci1jb3JlIHswfSDshKTsuZgoezF9KQ==' # dry-run hc install
    hc_installing = 'aGVyZHItY29yZSB7MH0g7J2EIOyEpOy5mO2VnOuLpCh7MX0p' # hc installing
    hc_dl = '67Cb7KeAIOuqu+2WiOuLpDogezB9' # hc installer download failed
    hc_nofile = '7YyM7J287J20IOyXhuuLpDogezB9' # hc installer file missing
    hc_sum = '7LK07YGs7ISsIOu2iOydvOy5mDog67Cb7J2AIO2MjOydvOydtCDsmIjsg4Hqs7wg64uk66W064uk' # hc checksum mismatch
    hc_noarch = 'KGhlcmRyLWNvcmUg7J6Q64+ZIOyEpOy5mCDqsbTrhIjrnIA6IOydtCBDUFXsmqkg67mM65Oc6rCAIOyXhuuLpCk=' # hc no build for this CPU
    hc_nosum = '6rKA7Kad7ZWgIOyytO2BrOyErOydtCDsl4bri6Q=' # hc no checksum
    hc_extract = '7JWV7LaV7J2EIO2SgOyngCDrqrvtlojri6Q=' # hc extract failed
    hc_nocore = '67Cb7J2AIOustuydjOyXkCBoZXJkci1jb3Jl6rCAIOyXhuqxsOuCmCDrtojsmYTsoITtlZjri6Q=' # hc bundle incomplete
    hc_norun = '67Cb7J2AIGhlcmRyLWNvcmXqsIAg7J20IOq4sOq4sOyXkOyEnCDsi6TtlonrkJjsp4Ag7JWK64qU64uk' # hc does not run
    hc_copy = 'ezB9IOyXkCDshKTsuZjtlZjsp4Ag66q77ZaI64uk' # hc copy failed
    hc_mirror = 'aGVyZHItY29yZeulvCDsm5Drnpgg7KO87IaM7JeQ7IScIOuquyDrsJvslZjri6QoezB9KS4g66+465+s7JeQ7IScIOuwm+uKlOuLpCh7MX0p' # hc primary failed, trying mirror
    hc_dry_mirror = 'KyAgIOyLpO2MqO2VmOuptCDrr7jrn6zsl5DshJwg67Cb6riwKHswfSk=' # dry-run hc mirror
    hc_nohc = '7ISk7LmYIOuSpOyXkOuPhCBoY+ulvCDssL7sp4Ag66q77ZaI64uk' # hc still missing
    cmd_foreign = 'ezB9IOqwgCDsnbTrr7gg7J6I6rOgIFNoZWx0aWXqsIAg66eM65OgIO2MjOydvOydtCDslYTri4jrnbwg6rG065Oc66as7KeAIOyViuyVmOuLpC4gc2hlbHRpZSDrqoXroLnsnYAg66eM65Ok7KeAIOyViuyVmOuLpC4=' # sheltie.cmd foreign
    cmd_dry = 'KyB7MH0g7J6R7ISxKHNoZWx0aWUgcnVuIDzsl5DsnbTsoITtirg+KQ==' # dry-run sheltie.cmd
    cmd_fail = 'c2hlbHRpZSDrqoXroLnsnYQg66eM65Ok7KeAIOuqu+2WiOuLpDogezB9' # sheltie.cmd failed
    hc_path_added = '7IKs7Jqp7J6QIFBBVEjsl5AgezB9IOulvCDstpTqsIDtlojri6QuIOyDiCDthLDrr7jrhJDrtoDthLAgaGPqsIAg7J6h7Z6M64ukLg==' # hc folder added to PATH
    cmd_path_added = '7IKs7Jqp7J6QIFBBVEjsl5AgezB9IOulvCDstpTqsIDtlojri6QuIOyDiCDthLDrr7jrhJDsnYQg7Je07Ja07JW8IHNoZWx0aWUg66qF66C57J20IOyeoe2ejOuLpC4=' # PATH entry added
    cmd_path_dry = 'KyDsgqzsmqnsnpAgUEFUSOyXkCB7MH0g7LaU6rCA' # dry-run PATH add
    cmd_path_dry_rm = 'KyDsgqzsmqnsnpAgUEFUSOyXkOyEnCB7MH0g7KCc6rGw' # dry-run PATH remove
    upd_start = 'U2hlbHRpZSDshJzrsoQg7JeF642w7J207Yq4OiB7MH0gLT4gezF9' # update start
    upd_abort = '7JeF642w7J207Yq4IOykkeuLqDogezB9' # update aborted
    upd_nocfg = '7ISk7KCV7J20IOyXhuuLpDogezB9' # update: no config
    upd_noserver = '7ISk7LmY65CcIOyEnOuyhCDtjIzsnbzsnbQg7JeG64ukOiB7MH0=' # update: no installed server
    upd_nosrc = '7IOIIOyEnOuyhCDtjIzsnbzsnbQg7JeG64ukOiB7MH0=' # update: no new server file
    upd_badpy = '7IOIIOyEnOuyhCDtjIzsnbzsnbQg7Jis67CU66W4IO2MjOydtOyNrOydtCDslYTri4jri6Q6IHswfQ==' # update: new server not valid python
    upd_nover = '7IOIIOyEnOuyhCDtjIzsnbzsl5AgU0VSVkVSX1ZFUlNJT07snbQg7JeG64ukOiB7MH0=' # update: no SERVER_VERSION
    upd_down = '67KE7KCE7J20IOyYrOudvOqwgOyngCDslYrripTri6QoezB9IC0+IHsxfSk=' # update: version not higher
    upd_token = '7ISk7KCV7JeQ7IScIO2GoO2BsOydhCDsnb3sp4Ag66q77ZaI64ukOiB7MH0=' # update: token unreadable
    upd_keep = '7KeA6riIIOyEnOuyhOulvCDrs7TqtIDtlZjsp4Ag66q77ZaI64ukOiB7MH0=' # update: could not keep .prev
    upd_place = '7IOIIOyEnOuyhCDtjIzsnbzsnYQg65GQ7KeAIOuqu+2WiOuLpDogezB9' # update: could not place new server
    upd_portbusy = '7J207KCEIOyEnOuyhOqwgCDtj6ztirggezB97J2EKOulvCkg64aT7KeAIOyViuyVhCDri6Tsi5wg652E7Jug64uk' # update: old server still holds port
    upd_dry = 'KyB7MH0gLT4gezB9LnByZXYsIHsxfSAtPiB7MH0sIO2bhSDri6Tsi5wg65Ox66GdLCDsnpHsl4Ug64uk7IucIOyLnOyekSwgezJ97LSIIOyViOyXkCB7M30g7ZmV7J24KOyVhOuLiOuptCDrkJjrj4zrprwp' # dry-run update
    upd_ok = '7JeF642w7J207Yq4IOyZhOujjDogezB9' # update done
    upd_rb = '7IOIIOyEnOuyhCh7MH0p6rCAIHsxfey0iCDslYjsl5Ag64u17ZWY7KeAIOyViuyVhCDsnbTsoIQg7ISc67KEKHsyfSnroZwg65CY64+M66aw64ukLg==' # update rolling back
    upd_rb_reason = '7IOIIOyEnOuyhOqwgCB7MH3stIgg7JWI7JeQIC9hcGkvaGVhbHRo7JeQIHsxfeuhnCDri7XtlZjsp4Ag7JWK7JWY64uk' # rollback reason
    upd_rb_ok = '7J207KCEIOyEnOuyhOuhnCDrkJjrj4zroLjri6QoezB9KS4=' # rolled back
    upd_rb_fail = '6rK96rOgOiDrkJjrj4zrprAg7ISc67KE64+EIOuLte2VmOyngCDslYrripTri6QuIOuhnOq3uOulvCDtmZXsnbjtlbTrnbw6IHswfQ==' # rolled back server silent
    upd_rb_copy = '6rK96rOgOiDsnbTsoIQg7ISc67KEIO2MjOydvOydhCDrkJjrj4zrpqzsp4Ag66q77ZaI64ukLiDsp4HsoJEg67O17IKs7ZW06528OiB7MH0gLT4gezF9' # rollback copy failed
    upd_status_fail = '6rK96rOgOiDsl4XrjbDsnbTtirgg6rKw6rO866W8IOuCqOq4sOyngCDrqrvtlojri6Q6IHswfQ==' # status write failed
    upd_nohooks = 'KO2bhSDrk7HroZ0g6rG064SI65yAKQ==' # update: hooks skipped
    upd_run_fail = '6rK96rOgOiDsnpHsl4XsnYQg64uk7IucIOyLnOyeke2VmOyngCDrqrvtlojri6QoezB9KS4=' # update: task restart failed
    task_bad = 'U0hFTFRJRV9UQVNL64qUIOyYgeusuMK37Iir7J6Qwrfqs7XrsLHCt+ygkMK367CR7KSEwrftlZjsnbTtlIjrp4w6IHswfQ==' # bad SHELTIE_TASK
    src_manifest_fail = '66a066as7IqkIG1hbmlmZXN0Lmpzb27snYQg67Cb7KeAIOuqu+2WiOuLpDogezB9' # release manifest download failed
    src_hash = '67Cb7J2AIOyEnOuyhCDtjIzsnbzsnbQg66a066as7IqkIG1hbmlmZXN0Lmpzb27snZgg7ZW07Iuc7JmAIOuLpOultOuLpDogezB9' # server file hash differs from release manifest
    upd_task_fail = '6rK96rOgOiDsnpHsl4Ug7KCV7J2Y66W8IOuwlOq+uOyngCDrqrvtlojri6QoezB9KS4g6rSA66as7J6QIOq2jO2VnOycvOuhnCDshKTsuZjtlZwg7J6R7JeF7J28IOyImCDsnojri6QuIOyEpOy5mOq4sOulvCDsnbzrsJggUG93ZXJTaGVsbOyXkOyEnCDtlZwg67KIIOuLpOyLnCDsi6TtlontlZjrqbQg67CU64CQ64ukLg==' # task XML not changed (admin-created task)
    upd_err = '7JeF642w7J207Yq4IOykkSDsmKTrpZg6IHswfQ==' # error during update
    help7 = 'ICAtTm9BdXRvVXBkYXRlOiDshJzrsoQg7J6Q64+ZIOyXheuNsOydtO2KuOulvCDrgYjri6Qo7ISk7KCVIGF1dG9fdXBkYXRlOiBmYWxzZSwg64uk7IucIOyEpOy5mO2VtOuPhCDqt7jrjIDroZwpLiAtVXBkYXRl64qUIOyekOuPmSDsl4XrjbDsnbTtirjqsIAg67aA66W464ukKOyCrOuejOydtCDsk7Dsp4Ag7JWK64qU64ukKS4=' # help line 7
  }
  # messages end
  function T([string]$Key) {
    $s = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($MSG[$Key]))
    if ($args.Count -gt 0) { return ($s -f $args) }
    return $s
  }

  # Task XML. The TimeTrigger repeats every minute while the user is logged on: with IgnoreNew it starts the server
  # only when it is not running. RestartOnFailure does not restart a server that exits non-zero (7950: exit 1 and
  # os._exit both stayed down), so this is what brings a crashed server back (and lets the crash guard count starts).
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
    <TimeTrigger>
      <Repetition>
        <Interval>PT1M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>2026-01-01T00:00:00</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
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

  # Merge script for Claude Code / Codex / Qwen Code hook files and the OpenCode plugin file. Same text as in install.sh (a test compares them). Needs python.exe, not pythonw.
  $HOOKS_PY = @'
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
'@

  $HELP_TEXT = @((T 'help1'), (T 'help2'), (T 'help3'), (T 'help4'), (T 'help5'), (T 'help6'), (T 'help7'))

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
  # Add/remove our hook entries in ~/.claude/settings.json, ~/.codex/hooks.json, ~/.qwen/settings.json and write/remove
  # ~/.config/opencode/plugins/sheltie.js (only agents whose folder exists).
  function Apply-Hooks([string]$Act, [string]$Py) {
    $script = Join-Path ([IO.Path]::GetTempPath()) ('hooks_merge.' + [guid]::NewGuid().ToString('N').Substring(0, 8) + '.py')
    [IO.File]::WriteAllText($script, $HOOKS_PY, (New-Object System.Text.UTF8Encoding($false)))
    $pre = ''
    if ($DRY) { $pre = '(dry-run) ' }
    try {
      foreach ($agent in @('claude', 'codex', 'qwen', 'opencode')) {
        $dir = Join-Path $env:USERPROFILE ".$agent"
        $name = 'settings.json'
        if ($agent -eq 'codex') { $name = 'hooks.json' }
        if ($agent -eq 'opencode') {
          $dir = Join-Path $env:USERPROFILE '.config\opencode'
          $name = 'plugins\sheltie.js'
        }
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
  function Read-Config([string]$Path = $CONFIG) {
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    try { return ([IO.File]::ReadAllText($Path) | ConvertFrom-Json) } catch { return $null }
  }
  function Config-Port {
    # The new config, or the previous install's config if there is no new one yet.
    $c = Read-Config
    if (-not $c) { $c = Read-Config (Join-Path $OLD_CONFIG_DIR 'config.json') }
    if ($c -and $c.port) { return [int]$c.port }
    return 8790
  }
  function Find-Python {
    $over = $env:SHELTIE_PYTHON
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
  function Task-Python {
    # python.exe of the registered task (its <Command> is pythonw.exe): -Update keeps the interpreter the service uses.
    try {
      $x = [xml][IO.File]::ReadAllText($TASK_COPY)
      $c = [string]$x.Task.Actions.Exec.Command
    } catch { return $null }
    if (-not $c -or -not (Test-Path -LiteralPath $c -PathType Leaf)) { return $null }
    $p = Join-Path (Split-Path -Parent $c) 'python.exe'
    if (Test-Path -LiteralPath $p -PathType Leaf) { return $p }
    return $c
  }
  function Find-Tailscale {
    $over = $env:SHELTIE_TAILSCALE
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
    $over = $env:SHELTIE_HC
    if ($over) {
      if (Test-Path -LiteralPath $over -PathType Leaf) { return $over }
      return $null
    }
    $p = Join-Path $LOCAL 'Programs\herdr-core\hc.exe'
    if (Test-Path -LiteralPath $p -PathType Leaf) { return $p }
    $h = Get-Command hc -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($h) { return $h.Source }
    $c = Join-Path $LOCAL 'Programs\herdr-core\herdr-core.exe'
    if (Test-Path -LiteralPath $c -PathType Leaf) { return $c }
    return $null
  }
  function Install-Hc {
    # Download the pinned herdr-core zip, check its SHA256 and install it. Never throws: returns the hc path or $null.
    if ($NOHC -or $env:SHELTIE_NO_HC -eq '1') { Say (T 'hc_skip'); return $null }
    $src = $env:SHELTIE_HC_URL
    $mirror = $null
    if (-not $src) {
      $arch = $env:PROCESSOR_ARCHITEW6432
      if (-not $arch) { $arch = $env:PROCESSOR_ARCHITECTURE }
      if ($arch -ne 'AMD64') { Say (T 'hc_noarch'); return $null }
      $src = "$HC_BASE_URL/herdr-core-windows-x86_64.zip"
      $mirror = "$HC_MIRROR_URL/herdr-core-windows-x86_64.zip"
    }
    $want = $env:SHELTIE_HC_SHA256
    if (-not $want) { $want = $HC_SHA_WIN }
    if ($src -like 'http://*') { Say (T 'hc_fail' (T 'hc_http' $src)); return $null }
    if ($DRY) {
      Say (T 'hc_dry' $HC_PIN_VERSION $src)
      if ($mirror) { Say (T 'hc_dry_mirror' $mirror) }
      return $null
    }
    if (-not $want) { Say (T 'hc_fail' (T 'hc_nosum')); return $null }
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ('sheltie-hc.' + [guid]::NewGuid().ToString('N').Substring(0, 8))
    try {
      New-Item -ItemType Directory -Force -Path $tmp | Out-Null
      $zip = Join-Path $tmp 'hc.zip'
      Say (T 'hc_installing' $HC_PIN_VERSION $src)
      # Download $from into $zip and check its hash. Returns $null on success, else the failure reason.
      $fetch = {
        param([string]$from)
        Remove-Item -LiteralPath $zip -Force -ErrorAction SilentlyContinue
        if ($from -like 'https://*') {
          try {
            [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
          } catch {}
          try { Invoke-WebRequest -UseBasicParsing -Uri $from -OutFile $zip } catch { return (T 'hc_dl' $from) }
        } else {
          if (-not (Test-Path -LiteralPath $from -PathType Leaf)) { return (T 'hc_nofile' $from) }
          Copy-Item -LiteralPath $from -Destination $zip -Force
        }
        # .NET, not Get-FileHash: that one fails when PowerShell 7 starts 5.1.
        $stream = [IO.File]::OpenRead($zip)
        try { $got = -join ([Security.Cryptography.SHA256]::Create().ComputeHash($stream) | ForEach-Object { $_.ToString('x2') }) } finally { $stream.Close() }
        if ($got -ne $want.Trim().ToLowerInvariant()) { return (T 'hc_sum') }
        return $null
      }
      $why = & $fetch $src
      if ($why -and $mirror) {
        Say (T 'hc_mirror' $why $mirror)
        $why = & $fetch $mirror
      }
      if ($why) { Say (T 'hc_fail' $why); return $null }
      # Windows' own tar opens zips; Git Bash's, often first on PATH, cannot.
      $tar = Join-Path $env:SystemRoot 'System32\tar.exe'
      $x = Join-Path $tmp 'x'
      New-Item -ItemType Directory -Force -Path $x | Out-Null
      if (-not (Test-Path -LiteralPath $tar -PathType Leaf)) { Say (T 'hc_fail' (T 'hc_extract')); return $null }
      & $tar -xf $zip -C $x
      if ($LASTEXITCODE -ne 0) { Say (T 'hc_fail' (T 'hc_extract')); return $null }
      $new = Join-Path $x 'herdr-core.exe'
      if (-not (Test-Path -LiteralPath $new -PathType Leaf) -or -not (Test-Path -LiteralPath (Join-Path $x 'conpty\conpty.dll') -PathType Leaf)) {
        Say (T 'hc_fail' (T 'hc_nocore')); return $null
      }
      $runs = $false
      try { & $new --version *> $null; $runs = ($LASTEXITCODE -eq 0) } catch { $runs = $false }
      if (-not $runs) { Say (T 'hc_fail' (T 'hc_norun')); return $null }
      $dir = Join-Path $LOCAL 'Programs\herdr-core'
      $exe = Join-Path $dir 'herdr-core.exe'
      try {
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
        Copy-Item -LiteralPath $new -Destination $exe -Force
        Copy-Item -LiteralPath (Join-Path $x 'conpty') -Destination $dir -Recurse -Force
        $notices = Join-Path $x 'THIRD-PARTY-NOTICES'
        if (Test-Path -LiteralPath $notices) { Copy-Item -LiteralPath $notices -Destination $dir -Recurse -Force }
      } catch { Say (T 'hc_fail' (T 'hc_copy' $dir)); return $null }
      $hcExe = Join-Path $dir 'hc.exe'
      if (-not (Get-Command hc -CommandType Application -ErrorAction SilentlyContinue) -and -not (Test-Path -LiteralPath $hcExe)) {
        try { New-Item -ItemType HardLink -Path $hcExe -Target $exe | Out-Null } catch { Say (T 'hc_fail' $_.Exception.Message) }
      }
      try { if (Add-UserPath $dir) { Say (T 'hc_path_added' $dir) } } catch { Say (T 'hc_fail' $_.Exception.Message) }
    } catch {
      Say (T 'hc_fail' $_.Exception.Message)
      return $null
    } finally {
      Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
    $h = Find-Hc
    if (-not $h) { Say (T 'hc_fail' (T 'hc_nohc')); return $null }
    return $h
  }
  # ---- sheltie command: %LOCALAPPDATA%\sheltie\bin\sheltie.cmd on the user PATH
  function Norm-PathEntry([string]$E) {
    return [Environment]::ExpandEnvironmentVariables($E.Trim()).TrimEnd('\').ToLowerInvariant()
  }
  function Get-UserPathRaw {
    # Returns @{ Value; Kind } with variables left unexpanded; Value is $null when there is no Path value yet.
    $r = @{ Value = $null; Kind = 'ExpandString' }
    $k = $null
    try { $k = Get-Item -LiteralPath $ENVKEY -ErrorAction Stop } catch { return $r }
    $names = @($k.GetValueNames())
    if ($names -contains 'Path') {
      $r.Value = [string]$k.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
      $r.Kind = [string]$k.GetValueKind('Path')
    }
    return $r
  }
  function Set-UserPathRaw([string]$Value, [string]$Kind) {
    if (-not (Test-Path -LiteralPath $ENVKEY)) { New-Item -Path $ENVKEY -Force | Out-Null }
    if ($Kind -ne 'String') { $Kind = 'ExpandString' }
    New-ItemProperty -LiteralPath $ENVKEY -Name 'Path' -Value $Value -PropertyType $Kind -Force | Out-Null
    if (-not $env:SHELTIE_ENV_KEY) {
      # Tell running programs (Explorer) the environment changed so new terminals pick it up.
      try {
        Add-Type -Namespace Sheltie -Name Win -MemberDefinition '[System.Runtime.InteropServices.DllImport("user32.dll", SetLastError = true, CharSet = System.Runtime.InteropServices.CharSet.Auto)] public static extern System.IntPtr SendMessageTimeout(System.IntPtr hWnd, uint Msg, System.UIntPtr wParam, string lParam, uint fuFlags, uint uTimeout, out System.UIntPtr lpdwResult);'
        $res = [UIntPtr]::Zero
        [void][Sheltie.Win]::SendMessageTimeout([IntPtr]0xffff, 0x1A, [UIntPtr]::Zero, 'Environment', 2, 3000, [ref]$res)
      } catch {}
    }
  }
  function Add-UserPath([string]$Dir) {
    $cur = Get-UserPathRaw
    $want = Norm-PathEntry $Dir
    $parts = @()
    if ($cur.Value) { $parts = @($cur.Value.Split(';') | Where-Object { $_.Trim() -ne '' }) }
    foreach ($e in $parts) { if ((Norm-PathEntry $e) -eq $want) { return $false } }
    Set-UserPathRaw (($parts + $Dir) -join ';') $cur.Kind
    return $true
  }
  function Remove-UserPath([string]$Dir) {
    $cur = Get-UserPathRaw
    if (-not $cur.Value) { return $false }
    $want = Norm-PathEntry $Dir
    $parts = @($cur.Value.Split(';') | Where-Object { $_.Trim() -ne '' })
    $keep = @($parts | Where-Object { (Norm-PathEntry $_) -ne $want })
    if ($keep.Count -eq $parts.Count) { return $false }
    Set-UserPathRaw ($keep -join ';') $cur.Kind
    return $true
  }
  function Test-OurCmd {
    if (-not (Test-Path -LiteralPath $CMD_PATH -PathType Leaf)) { return $false }
    try { return ([IO.File]::ReadAllText($CMD_PATH).Contains($CMD_MARK)) } catch { return $false }
  }
  function Install-Command {
    if ((Test-Path -LiteralPath $CMD_PATH) -and -not (Test-OurCmd)) { Say (T 'cmd_foreign' $CMD_PATH); return }
    if ($DRY) { Say (T 'cmd_dry' $CMD_PATH); Say (T 'cmd_path_dry' $CMD_DIR); return }
    try {
      New-Item -ItemType Directory -Force -Path $CMD_DIR | Out-Null
      $body = "@echo off`r`n${CMD_MARK}: created by the Sheltie installer, removed on uninstall`r`n`"$PY`" `"$SERVER`" %*`r`nexit /b %ERRORLEVEL%`r`n"
      $enc = [Text.Encoding]::GetEncoding([Globalization.CultureInfo]::CurrentCulture.TextInfo.OEMCodePage)
      $tmpc = "$CMD_PATH.tmp"
      [IO.File]::WriteAllText($tmpc, $body, $enc)
      Move-Item -LiteralPath $tmpc -Destination $CMD_PATH -Force
      if (Add-UserPath $CMD_DIR) { Say (T 'cmd_path_added' $CMD_DIR) }
    } catch {
      Say (T 'cmd_fail' $_.Exception.Message)
    }
  }
  function Uninstall-Command {
    # Remove only our own sheltie.cmd; drop the PATH entry and folder only when nothing else lives there.
    $ours = Test-OurCmd
    $foreign = (Test-Path -LiteralPath $CMD_PATH) -and -not $ours
    if ($ours) {
      if ($DRY) { Say (T 'dry_delete' $CMD_PATH '') } else { Remove-Item -LiteralPath $CMD_PATH -Force -ErrorAction SilentlyContinue }
    }
    $left = @()
    if (Test-Path -LiteralPath $CMD_DIR -PathType Container) {
      $left = @(Get-ChildItem -LiteralPath $CMD_DIR -Force -ErrorAction SilentlyContinue | Where-Object { $_.FullName -ne $CMD_PATH })
    }
    if ($foreign -or $left.Count -gt 0) { return }
    if ($DRY) { Say (T 'cmd_path_dry_rm' $CMD_DIR); return }
    try {
      [void](Remove-UserPath $CMD_DIR)
      Remove-Item -LiteralPath $CMD_DIR -Force -ErrorAction SilentlyContinue
    } catch {}
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
  function Stop-Old([int[]]$Ports, [switch]$NoDie, [string]$Task = $TASK, [string]$Srv = $SERVER) {
    $rc = Run 'schtasks' @('/End', '/TN', $Task) -Quiet
    if ($DRY -or $Ports.Count -eq 0) { return }
    $wait = 15
    if ($env:SHELTIE_STOP_WAIT -match '^[0-9]+$') { $wait = [int]$env:SHELTIE_STOP_WAIT }
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
          if ($cl.IndexOf($Srv, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and -not ($cl -match '\shook(\s|$)')) {
            Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
          }
        }
        $killed = $true
        $deadline = (Get-Date).AddSeconds(5)
      }
      Start-Sleep -Milliseconds 200
    }
  }
  # Rewrite absolute or ~ paths inside the config that point into the old config folder (apns_key_path etc.).
  $FIXPATHS_PY = @'
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
    with open(p + ".tmp", "w", encoding="utf-8") as f:
        json.dump(n, f, indent=2)
    os.replace(p + ".tmp", p)
'@
  # An old config folder that is a link cannot be moved; a fresh token in the new place would break pairing.
  # Stop before changing anything.
  function Old-CheckLink {
    $i = Get-Item -LiteralPath $OLD_CONFIG_DIR -Force -ErrorAction SilentlyContinue
    if ($i -and ($i.Attributes -band [IO.FileAttributes]::ReparsePoint) -and -not (Test-Path -LiteralPath $CONFIG_DIR)) {
      Die 1 (T 'mig_link' $OLD_CONFIG_DIR $CONFIG_DIR)
    }
  }
  # Called once the new server file is in place, before init: stop the previous install's task (same port) and
  # move its config folder (only if the new one does not exist). If the move fails, restart the old task and stop.
  # Deleting old files waits until the new server answers health (Old-Cleanup).
  function Old-StopMove {
    if (Test-Path -LiteralPath $OLD_DATA -PathType Container) {
      Say (T 'mig_found')
      $oc = Read-Config (Join-Path $OLD_CONFIG_DIR 'config.json')
      $ports = @()
      if ($oc) { $p = 8790; if ($oc.port) { $p = [int]$oc.port }; $ports += $p }
      if ($PORT) { $ports += [int]$PORT }
      Stop-Old $ports -NoDie -Task $OLD_TASK -Srv $OLD_SERVER
    }
    if (Test-Path -LiteralPath $OLD_CONFIG_DIR -PathType Container) {
      if (Test-Path -LiteralPath $CONFIG_DIR) {
        Say (T 'mig_keep' $CONFIG_DIR $OLD_CONFIG_DIR)
      } elseif ($DRY) {
        Say (T 'mig_dry_mv' $OLD_CONFIG_DIR $CONFIG_DIR)
      } else {
        $moved = $false
        try { Move-Item -LiteralPath $OLD_CONFIG_DIR -Destination $CONFIG_DIR -ErrorAction Stop; $moved = $true } catch {}
        if (-not $moved) {
          if (Test-Path -LiteralPath $OLD_DATA -PathType Container) { $rc = Run 'schtasks' @('/Run', '/TN', $OLD_TASK) -Quiet }
          Die 1 (T 'mig_fail' $OLD_CONFIG_DIR $CONFIG_DIR)
        }
        if (Test-Path -LiteralPath $CONFIG) {
          $fix = Join-Path ([IO.Path]::GetTempPath()) ('fixpaths.' + [guid]::NewGuid().ToString('N').Substring(0, 8) + '.py')
          [IO.File]::WriteAllText($fix, $FIXPATHS_PY, (New-Object System.Text.UTF8Encoding($false)))
          try { & $PY $fix $CONFIG $OLD_CONFIG_DIR $CONFIG_DIR 2>$null | Out-Null; $frc = $LASTEXITCODE } catch { $frc = 1 }
          finally { Remove-Item -LiteralPath $fix -Force -ErrorAction SilentlyContinue }
          if ($frc -ne 0) { Die 1 (T 'mig_fix_fail' $CONFIG $CONFIG_DIR) }
        }
        Say (T 'mig_moved' $OLD_CONFIG_DIR $CONFIG_DIR)
      }
    }
  }
  # After the new server answered health: delete the old task and install folder. If a hook file still calls the
  # old server file, keep the folder and warn.
  function Old-Cleanup {
    if (-not (Test-Path -LiteralPath $OLD_DATA -PathType Container)) { return }
    $rc = Run 'schtasks' @('/Delete', '/TN', $OLD_TASK, '/F') -Quiet
    $left = @()
    # Dry-run does not change hook files: if hooks would be updated, count them as updated.
    if (-not ($DRY -and -not $NOHOOKS)) {
      $hookFiles = @((Join-Path $env:USERPROFILE '.claude\settings.json'), (Join-Path $env:USERPROFILE '.codex\hooks.json'),
        (Join-Path $env:USERPROFILE '.qwen\settings.json'), (Join-Path $env:USERPROFILE '.config\opencode\plugins\sheltie.js'))
      foreach ($f in $hookFiles) {
        if ((Test-Path -LiteralPath $f -PathType Leaf) -and ([IO.File]::ReadAllText($f).Contains($OLD_SERVER_NAME))) { $left += $f }
      }
    }
    if ($left.Count -gt 0) {
      [Console]::Error.WriteLine((T 'mig_hooks_left' ($left -join ', ') $OLD_DATA $OLD_SERVER_NAME))
      $oldCopy = Join-Path $OLD_DATA 'windows-task.xml'
      if ($DRY) { Say (T 'dry_delete' $oldCopy '') } else { Remove-Item -LiteralPath $oldCopy -Force -ErrorAction SilentlyContinue }
    } elseif ($DRY) {
      Say (T 'dry_delete' $OLD_DATA '')
    } else {
      Remove-Item -LiteralPath $OLD_DATA -Recurse -Force -ErrorAction SilentlyContinue
    }
  }
  function Fill-Template([string]$Text, $Map) {
    foreach ($k in $Map.Keys) {
      $Text = $Text.Replace("@$k@", [Security.SecurityElement]::Escape([string]$Map[$k]))
    }
    return $Text
  }
  # Task XML text: deploy\windows-task.xml.in next to this file, else the built-in copy (same text, tested).
  # With a test task name (SHELTIE_TASK) the server runs through serve-test.py, which sets this installer's
  # USERPROFILE/LOCALAPPDATA/SHELTIE_TASK/SHELTIE_ENV_KEY first (Task Scheduler has no per-task environment).
  function Task-Xml([string]$Here) {
    $tpl = $TASK_XML_BUILTIN.Replace('@XML_COMMENT@', (T 'xml_comment')).Replace('@XML_DESC@', (T 'xml_desc'))
    if ($Here -and (Test-Path -LiteralPath (Join-Path $Here 'deploy\windows-task.xml.in') -PathType Leaf)) {
      $tpl = [IO.File]::ReadAllText((Join-Path $Here 'deploy\windows-task.xml.in'), (New-Object System.Text.UTF8Encoding($false)))
    }
    $userName = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $xml = Fill-Template $tpl @{ PYTHONW = $PYW; SERVER = $SERVER; USER = $userName; WORKDIR = $DATA }
    if ($env:SHELTIE_TASK) {
      $launcher = Join-Path $DATA 'serve-test.py'
      $envMap = @{ USERPROFILE = $env:USERPROFILE; LOCALAPPDATA = $env:LOCALAPPDATA; SHELTIE_TASK = $env:SHELTIE_TASK }
      if ($env:SHELTIE_ENV_KEY) { $envMap.SHELTIE_ENV_KEY = $env:SHELTIE_ENV_KEY }
      # Base64 so quotes/backslashes in paths cannot break the Python literal.
      $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(($envMap | ConvertTo-Json -Compress)))
      $code = "import base64, json, os, runpy, sys`nos.environ.update(json.loads(base64.b64decode('$b64').decode('utf-8')))`nsys.argv = sys.argv[1:]`nrunpy.run_path(sys.argv[0], run_name='__main__')`n"
      [IO.File]::WriteAllText($launcher, $code, (New-Object System.Text.UTF8Encoding($false)))
      $xml = $xml.Replace('<Arguments>"', '<Arguments>"' + [Security.SecurityElement]::Escape($launcher) + '" "')
    }
    return $xml
  }
  function Update-Status([string]$Result, [string]$Ver, [string]$From, [string]$Reason, [bool]$Transient = $false) {
    # The server reads this on its next start. Transient failures (no answer in time, port busy, write errors) are retried
    # by the server and only the third one makes the version permanently failed; other failures are permanent at once.
    $p = Join-Path $CONFIG_DIR $UPDATE_STATUS_NAME
    try {
      $o = [ordered]@{ result = $Result; version = $Ver; from = $From; reason = $Reason; transient = $Transient; time = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() }
      [IO.File]::WriteAllText("$p.tmp", ($o | ConvertTo-Json -Compress), (New-Object System.Text.UTF8Encoding($false)))
      Move-Item -LiteralPath "$p.tmp" -Destination $p -Force
    } catch { [Console]::Error.WriteLine((T 'upd_status_fail' $p)) }
  }
  function Server-Version([string]$Path) {
    # SERVER_VERSION line, same shape as SERVER_VERSION_LINE_RE in server/sheltie.py.
    try { $t = [IO.File]::ReadAllText($Path) } catch { return '' }
    $m = [regex]::Match($t, '(?m)^SERVER_VERSION = "([^"\r\n]*)"[ \t]*\r?$')
    if ($m.Success) { return $m.Groups[1].Value }
    return ''
  }
  function Version-Key([string]$V) {
    $m = [regex]::Match($V, '^(0|[1-9][0-9]{0,4})\.(0|[1-9][0-9]{0,4})\.(0|[1-9][0-9]{0,4})$')
    if (-not $m.Success) { return $null }
    return ([long]$m.Groups[1].Value * 10000000000) + ([long]$m.Groups[2].Value * 100000) + [long]$m.Groups[3].Value
  }
  function Wait-Version([string]$Want, [double]$Since, [int]$Seconds, [string]$Token, [string]$Port) {
    # True when a server started after $Since answers /api/health with $Want (any version if $Want is empty).
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ($true) {
      try {
        $r = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$Port/api/health" -Headers @{ Authorization = "Bearer $Token" } -TimeoutSec 3
        if ($r.StatusCode -eq 200) {
          $body = $r.Content
          if ($body -is [byte[]]) { $body = [Text.Encoding]::UTF8.GetString($body) }
          $d = $body | ConvertFrom-Json
          if (((-not $Want) -or ([string]$d.server_version -eq $Want)) -and ([double]$d.started -ge $Since)) { return $true }
        }
      } catch {}
      if ((Get-Date) -ge $deadline) { return $false }
      Start-Sleep -Seconds 1
    }
  }
  function Update-Fail([string]$Why, [switch]$Permanent) {
    # Before the server file changed. -Permanent: the release itself is wrong (syntax, version line).
    [Console]::Error.WriteLine((T 'upd_abort' $Why))
    if (Test-Path -LiteralPath $CONFIG_DIR -PathType Container) { Update-Status 'failed' $script:NEWV $script:OLDV $Why (-not $Permanent) }
    $global:HRCode = 1
    throw 'HRExit'
  }
  # Server auto-update (-Update): started detached by the server with the downloaded server file in SHELTIE_SRC.
  # Config, token and devices are only read. No tailscale, hc install or pairing. Rolls back if the new version is silent.
  function Update-Main {
    $now = { [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0 }
    $script:NEWV = ''
    $script:OLDV = ''
    if (-not (Test-Path -LiteralPath $CONFIG -PathType Leaf)) { Update-Fail (T 'upd_nocfg' $CONFIG) }
    if (-not (Test-Path -LiteralPath $SERVER -PathType Leaf)) { Update-Fail (T 'upd_noserver' $SERVER) }
    $src = $env:SHELTIE_SRC
    if (-not $src -or -not (Test-Path -LiteralPath $src -PathType Leaf)) { Update-Fail (T 'upd_nosrc' $src) }
    & $PY -c "import sys; compile(open(sys.argv[1], encoding='utf-8-sig').read(), sys.argv[1], 'exec')" $src 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { Update-Fail (T 'upd_badpy' $src) -Permanent }
    $script:NEWV = Server-Version $src
    if (-not $script:NEWV) { Update-Fail (T 'upd_nover' $src) -Permanent }
    $script:OLDV = Server-Version $SERVER
    $nk = Version-Key $script:NEWV
    $ok = ($null -ne $nk)
    if ($ok -and $script:OLDV) { $okey = Version-Key $script:OLDV; $ok = ($null -ne $okey) -and ($nk -gt $okey) }
    if (-not $ok) { Update-Fail (T 'upd_down' $script:OLDV $script:NEWV) -Permanent }
    $cfg = Read-Config
    $token = ''
    if ($cfg) { $token = [string]$cfg.token }
    if (-not $token) { Update-Fail (T 'upd_token' $CONFIG) }
    $port = [string](Config-Port)
    $wait = 120
    if ($env:SHELTIE_UPDATE_WAIT -match '^[0-9]+$') { $wait = [int]$env:SHELTIE_UPDATE_WAIT }
    $oldv = $script:OLDV
    if (-not $oldv) { $oldv = '?' }
    Say (T 'upd_start' $oldv $script:NEWV)
    if ($DRY) { Say (T 'upd_dry' $SERVER $src $wait $script:NEWV); return }
    $prev = "$SERVER.prev"
    try { Copy-Item -LiteralPath $SERVER -Destination $prev -Force -ErrorAction Stop } catch { Update-Fail (T 'upd_keep' $prev) }
    $t0 = & $now
    # The server holds the port: stop it (this process is detached from it) and wait until the port is free.
    # From here on the task is always started again (finally), whatever fails.
    Stop-Old @([int]$port) -NoDie
    $script:updRan = $false
    try {
      # The task's 1-minute repetition can start a server right after the stop: stop again (it fires once a minute).
      for ($i = 0; $i -lt 2 -and (Port-Open ([int]$port)); $i++) { Stop-Old @([int]$port) -NoDie }
      if (Port-Open ([int]$port)) { Update-Fail (T 'upd_portbusy' $port) }
      $tmp = Join-Path $DATA ('sheltie.' + [guid]::NewGuid().ToString('N').Substring(0, 8) + '.tmp')
      $placed = $false
      try {
        Copy-Item -LiteralPath $src -Destination $tmp -Force -ErrorAction Stop
        Move-Item -LiteralPath $tmp -Destination $SERVER -Force -ErrorAction Stop
        $placed = $true
      } catch {} finally { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue }
      if (-not $placed) { Update-Fail (T 'upd_place' $SERVER) }
      # The server file changed: any error from here on rolls back instead of leaving the task stopped.
      $reload = $false
      $good = $false
      try {
        # Start the new server first (the task's 1-minute repetition must not bring the old one back meanwhile),
        # then hooks and the command file.
        # Task: re-register only when its XML changed (the previous copy is kept for rollback).
        $xml = Task-Xml ''
        $cur = ''
        if (Test-Path -LiteralPath $TASK_COPY -PathType Leaf) { try { $cur = [IO.File]::ReadAllText($TASK_COPY) } catch {} }
        # Compare without line-ending/trailing-space differences (repo template vs built-in copy).
        $reload = (($xml -replace "`r`n", "`n").Trim() -ne ($cur -replace "`r`n", "`n").Trim())
        if ($reload) {
          if ($cur) { Copy-Item -LiteralPath $TASK_COPY -Destination "$TASK_COPY.prev" -Force -ErrorAction SilentlyContinue }
          try { [IO.File]::WriteAllText($TASK_COPY, $xml, [Text.Encoding]::Unicode) } catch {}
          $rc = Run 'schtasks' @('/Create', '/TN', $TASK, '/XML', $TASK_COPY, '/F') -Quiet
          if ($rc -ne 0) {
            # A task created from an elevated PowerShell cannot be changed from here (access denied, 7950). Keep the old
            # copy so the next update tries again, and go on with the old task definition.
            [Console]::Error.WriteLine((T 'upd_task_fail' $TASK))
            if ($cur) { try { [IO.File]::WriteAllText($TASK_COPY, $cur, [Text.Encoding]::Unicode) } catch {} }
            Remove-Item -LiteralPath "$TASK_COPY.prev" -Force -ErrorAction SilentlyContinue
            $reload = $false
          }
        }
        $script:updRan = $true
        $rc = Run 'schtasks' @('/Run', '/TN', $TASK) -Quiet
        if ($rc -ne 0) { [Console]::Error.WriteLine((T 'upd_run_fail' $TASK)) }
        if ($NOHOOKS -or ($cfg.PSObject.Properties.Name -contains 'hooks' -and $cfg.hooks -eq $false)) { Say (T 'upd_nohooks') } else { Apply-Hooks 'install' $PY }
        Install-Command
        $good = Wait-Version $script:NEWV $t0 $wait $token $port
      } catch {
        [Console]::Error.WriteLine((T 'upd_err' $_.Exception.Message))
        $good = $false
      }
      if ($good) {
        Update-Status 'ok' $script:NEWV $script:OLDV ''
        Remove-Item -LiteralPath "$TASK_COPY.prev" -Force -ErrorAction SilentlyContinue
        Say (T 'upd_ok' $script:NEWV)
        return
      }
      # Roll back to the previous server (and task XML). Write the result first so the restarted old server reads it.
      [Console]::Error.WriteLine((T 'upd_rb' $script:NEWV $wait $oldv))
      Stop-Old @([int]$port) -NoDie
      $script:updRan = $false
      $rt = Join-Path $DATA ('sheltie.' + [guid]::NewGuid().ToString('N').Substring(0, 8) + '.tmp')
      $back = $false
      try {
        Copy-Item -LiteralPath $prev -Destination $rt -Force -ErrorAction Stop
        Move-Item -LiteralPath $rt -Destination $SERVER -Force -ErrorAction Stop
        $back = $true
      } catch {} finally { Remove-Item -LiteralPath $rt -Force -ErrorAction SilentlyContinue }
      if (-not $back) { [Console]::Error.WriteLine((T 'upd_rb_copy' $prev $SERVER)) }
      if ($reload -and (Test-Path -LiteralPath "$TASK_COPY.prev" -PathType Leaf)) {
        Move-Item -LiteralPath "$TASK_COPY.prev" -Destination $TASK_COPY -Force -ErrorAction SilentlyContinue
        $rc = Run 'schtasks' @('/Create', '/TN', $TASK, '/XML', $TASK_COPY, '/F') -Quiet
      }
      Update-Status 'rolled_back' $script:NEWV $script:OLDV (T 'upd_rb_reason' $wait $script:NEWV) $true
      $t1 = & $now
      $script:updRan = $true
      $rc = Run 'schtasks' @('/Run', '/TN', $TASK) -Quiet
      if (Wait-Version $script:OLDV $t1 $wait $token $port) { Say (T 'upd_rb_ok' $oldv) }
      else { [Console]::Error.WriteLine((T 'upd_rb_fail' $LOGFILE)) }
      $global:HRCode = 3
    } finally {
      if (-not $script:updRan) { $rc = Run 'schtasks' @('/Run', '/TN', $TASK) -Quiet }
    }
  }

  try {
    foreach ($a in $args) {
      switch -Regex ([string]$a) {
        '^--?(dry-?run)$' { $DRY = $true }
        '^--?uninstall$' { $MODE = 'uninstall' }
        '^--?no-?hooks$' { $NOHOOKS = $true }
        '^--?no-?hc$' { $NOHC = $true }
        '^--?no-?auto-?update$' { $NOAUTO = $true }
        '^--?update$' { $MODE = 'update' }
        '^(--?help|-h|-\?)$' { foreach ($l in $HELP_TEXT) { Say $l }; return }
        default { Die 2 (T 'unknown_opt' $a) }
      }
    }
    if ($env:SHELTIE_UNINSTALL -eq '1') { $MODE = 'uninstall' }
    if ($env:SHELTIE_DRY_RUN -eq '1') { $DRY = $true }
    if ($env:SHELTIE_NO_HOOKS -eq '1') { $NOHOOKS = $true }
    if ($TASK -notmatch '^[A-Za-z0-9 ._-]+$') { Die 2 (T 'task_bad' $TASK) }

    $PORT = $env:SHELTIE_PORT
    if ($PORT) {
      if ($PORT -notmatch '^[0-9]+$') { Die 2 (T 'port_nan' $PORT) }
      if ($PORT.Length -gt 5 -or [int]$PORT -lt 1 -or [int]$PORT -gt 65535) { Die 2 (T 'port_range' $PORT) }
    }

    if ($MODE -eq 'uninstall') {
      if (-not $PORT) { $PORT = [string](Config-Port) }
      if (Test-Path -LiteralPath $OLD_DATA -PathType Container) {
        # Previous install: stop and delete its task first (it holds the same port).
        Stop-Old @([int]$PORT) -NoDie -Task $OLD_TASK -Srv $OLD_SERVER
        $rc = Run 'schtasks' @('/Delete', '/TN', $OLD_TASK, '/F') -Quiet
        $oldCopy = Join-Path $OLD_DATA 'windows-task.xml'
        if ($DRY) { Say (T 'dry_delete' $OLD_SERVER $oldCopy) }
        else { Remove-Item -LiteralPath $OLD_SERVER, $oldCopy -Force -ErrorAction SilentlyContinue }
      }
      Stop-Old @([int]$PORT) -NoDie
      $rc = Run 'schtasks' @('/Delete', '/TN', $TASK, '/F') -Quiet
      if ($rc -ne 0) { Say (T 'no_task') }
      if ($env:SHELTIE_SKIP_TAILSCALE -ne '1') {
        $TS = Find-Tailscale
        if ($TS) {
          $rc = Run $TS @('serve', "--https=$PORT", 'off')
          if ($rc -ne 0) { Say (T 'serve_off_fail') }
        }
      }
      if ($DRY) { Say (T 'dry_delete' $SERVER $TASK_COPY) }
      else { Remove-Item -LiteralPath $SERVER, $TASK_COPY -Force -ErrorAction SilentlyContinue }
      Uninstall-Command
      if (-not $NOHOOKS) {
        $upy = Find-Python
        if ($upy) { Apply-Hooks 'uninstall' $upy } else { Say (T 'hk_nopy') }
      }
      $kept = $CONFIG_DIR
      if (-not (Test-Path -LiteralPath $CONFIG_DIR) -and (Test-Path -LiteralPath $OLD_CONFIG_DIR)) { $kept = $OLD_CONFIG_DIR }
      Say (T 'uninstalled' $kept)
      return
    }

    # ---- 1. checks
    # Health must come from a server started after this point (not the old one still exiting).
    $T0 = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0
    if ([Environment]::OSVersion.Platform -ne 'Win32NT') { Die 1 (T 'win_only') }

    $PY = $null
    if ($MODE -eq 'update') { $PY = Task-Python }
    if (-not $PY) { $PY = Find-Python }
    if (-not $PY) { Die 1 (T 'no_python') }
    & $PY -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { Die 1 (T 'old_python' $PY) }
    $PYW = Join-Path (Split-Path -Parent $PY) 'pythonw.exe'
    if (-not (Test-Path -LiteralPath $PYW -PathType Leaf)) { $PYW = $PY }
    if ($MODE -eq 'update') { Update-Main; return }

    $HC = Find-Hc
    $HC_TRY = (-not $HC) -and (-not $env:SHELTIE_HC)

    $TS = $null
    if ($env:SHELTIE_SKIP_TAILSCALE -ne '1') {
      $TS = Find-Tailscale
      if (-not $TS) { Die 1 (T 'no_ts') }
      $running = $false
      try {
        $st = (& $TS status --json 2>$null | Out-String) | ConvertFrom-Json
        $running = ($st.BackendState -eq 'Running')
      } catch {}
      if (-not $running) { Die 1 (T 'ts_logged_out') }
    }

    Old-CheckLink

    # Only after the checks above pass: do not leave hc installed by an install that dies at the tailscale check.
    if ($HC_TRY) { $HC = Install-Hc }
    if (-not $HC) { Say (T 'no_hc_warn'); $HC = '' }

    # ---- 2. server file
    $SRC = $env:SHELTIE_SRC
    # With irm | iex there is no script path. Use sibling files only when run as a file.
    $HERE = ''
    $self = $PSCommandPath
    if ($self -and $self -like '*install.ps1' -and (Test-Path -LiteralPath $self -PathType Leaf)) { $HERE = Split-Path -Parent $self }
    if (-not $SRC -and $HERE -and (Test-Path -LiteralPath (Join-Path $HERE 'server\sheltie.py') -PathType Leaf)) {
      $SRC = Join-Path $HERE 'server\sheltie.py'
    }
    if (-not $SRC) { $SRC = $DEFAULT_SRC }
    if ($SRC -like 'http://*') { Die 1 (T 'src_https' $SRC) }

    if ($DRY) {
      Say (T 'dry_mkdir' $DATA)
      Say (T 'dry_copy' $SRC $SERVER)
    } else {
      New-Item -ItemType Directory -Force -Path $DATA | Out-Null
      $tmp = Join-Path $DATA ('sheltie.' + [guid]::NewGuid().ToString('N').Substring(0, 8) + '.tmp')
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
        if ($SRC -eq $DEFAULT_SRC) {
          # Must match the release manifest (the installed server checks the manifest signature on updates).
          $man = $DEFAULT_SRC.Substring(0, $DEFAULT_SRC.LastIndexOf('/')) + '/manifest.json'
          $want = ''
          try {
            $mr = Invoke-WebRequest -UseBasicParsing -Uri $man
            $mt = $mr.Content
            if ($mt -is [byte[]]) { $mt = [Text.Encoding]::UTF8.GetString($mt) }
            $want = [string](($mt | ConvertFrom-Json).files.'sheltie.py')
          } catch { Die 1 (T 'src_manifest_fail' $man) }
          if ((Get-FileHash -LiteralPath $tmp -Algorithm SHA256).Hash.ToLower() -ne $want.ToLower()) { Die 1 (T 'src_hash' $SRC) }
        }
        # A running server may hold the file and the port: stop it and wait for it to exit before replacing.
        # Old port only if a config exists (fresh install: nothing of ours is running).
        # A previous install still holding the port is stopped right after this (Old-StopMove).
        $ports = @()
        if (Test-Path -LiteralPath $CONFIG) { $ports += Config-Port }
        if ($PORT -and -not (Test-Path -LiteralPath $OLD_DATA -PathType Container)) { $ports += [int]$PORT }
        Stop-Old $ports
        Move-Item -LiteralPath $tmp -Destination $SERVER -Force
      } finally {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
      }
    }
    # Previous install: only now that the new server file is in place (and before init makes a new token).
    Old-StopMove
    if ($DRY) { Say (T 'dry_init' $PY $SERVER) }
    else {
      & $PY $SERVER init | Out-Host
      # Reflect the hc path and (if given) the port in the config.
      $c = Read-Config
      if (-not $c) { Die 1 (T 'cfg_read' $CONFIG) }
      try {
        $c | Add-Member -NotePropertyName hc_path -NotePropertyValue $HC -Force
        if ($PORT) { $c | Add-Member -NotePropertyName port -NotePropertyValue ([int]$PORT) -Force }
        # -NoHooks is remembered so auto-update leaves hooks alone too; -NoAutoUpdate is kept on later reinstalls.
        if ($NOHOOKS) { $c | Add-Member -NotePropertyName hooks -NotePropertyValue $false -Force }
        else { $c.PSObject.Properties.Remove('hooks') }
        if ($NOAUTO) { $c | Add-Member -NotePropertyName auto_update -NotePropertyValue $false -Force }
        $utf8 = New-Object System.Text.UTF8Encoding($false)
        [IO.File]::WriteAllText("$CONFIG.tmp", ($c | ConvertTo-Json -Depth 10), $utf8)
        Move-Item -LiteralPath "$CONFIG.tmp" -Destination $CONFIG -Force
      } catch { Die 1 (T 'cfg_write' $CONFIG) }
      $PORT = [string](Config-Port)
    }
    if (-not $PORT) { $PORT = '8790' }

    # ---- 2b. Claude Code / Codex hooks (python.exe, not pythonw: the hook needs stdout)
    if ($NOHOOKS) { Say (T 'hk_off') } else { Apply-Hooks 'install' $PY }

    # ---- 2c. sheltie command
    Install-Command

    # ---- 3. Task Scheduler
    if ($DRY) {
      Say (T 'dry_xml' $TASK_COPY $PYW $SERVER $LOGFILE)
    } else {
      $xml = Task-Xml $HERE
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
      if ($env:SHELTIE_HEALTH_TRIES -match '^[0-9]+$') { $tries = [int]$env:SHELTIE_HEALTH_TRIES }
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
    Old-Cleanup

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
      [Console]::Error.WriteLine((T 'unexpected' $_.Exception.Message)); if ($env:SHELTIE_DEBUG -eq '1') { [Console]::Error.WriteLine($_.InvocationInfo.PositionMessage) }
      $global:HRCode = 1
    }
  }
} @args
# Variable names are case-insensitive: capturing into $hrCode would make the Remove-Variable below delete this value too (exit code always 0).
$finalExit = $global:HRCode
Remove-Variable HRCode -Scope Global -ErrorAction SilentlyContinue
# Pass the exit code only when run as a file. exit under iex would close the window.
if ($PSCommandPath) { exit $finalExit }
