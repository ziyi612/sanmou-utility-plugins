[CmdletBinding()]
param(
    [switch]$Console,
    [switch]$Once,
    [switch]$Check,
    [switch]$PvpOnly,
    [ValidateSet("all", "24h", "72h", "7d")][string]$TimeRange,
    [string]$PythonExecutable = "",
    [string]$Namespace = "auto",
    [int]$Interval = 60,
    [int]$GamePid = 0,
    [ValidateRange(1, 4)][int]$Parallel = 1
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$temporary = if ($env:SANMOU_TEMP_DIR) { $env:SANMOU_TEMP_DIR } else { Join-Path $root ".runtime\temp" }
New-Item -ItemType Directory -Path $temporary -Force | Out-Null
$env:TEMP = $temporary
$env:TMP = $temporary
$env:PYTHONDONTWRITEBYTECODE = "1"
$env:PYTHONIOENCODING = "utf-8"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$consoleMode = $Console -or $Once -or $Namespace -ne "auto" -or $Parallel -ne 1
$entry = Join-Path $root $(if ($consoleMode) { "battle_reports.py" } else { "battle_reports_gui.py" })
if (-not (Test-Path -LiteralPath $entry -PathType Leaf)) { throw "Program entry was not found: $entry" }
$pythonCandidates = @()
if ($PythonExecutable) { $pythonCandidates += $PythonExecutable }
$localPython = Join-Path $root ".venv\Scripts\python.exe"
if (Test-Path -LiteralPath $localPython) { $pythonCandidates += $localPython }
$runtimeConfig = Join-Path $root "config\launcher.json"
if (Test-Path -LiteralPath $runtimeConfig) {
    $configured = Get-Content -LiteralPath $runtimeConfig -Raw | ConvertFrom-Json
    if ($null -eq $configured -or $configured -isnot [System.Management.Automation.PSCustomObject]) { throw "Launcher configuration must be a JSON object; no report log was saved." }
    if ($configured.python_executable -and (Test-Path -LiteralPath $configured.python_executable -PathType Leaf)) {
        $pythonCandidates += [string]$configured.python_executable
    }
}
$foundPython = Get-Command python -CommandType Application -ErrorAction SilentlyContinue
if ($foundPython -and $foundPython.Source -notmatch 'WindowsApps') { $pythonCandidates += $foundPython.Source }
$pythonExe = $null
foreach ($candidate in ($pythonCandidates | Select-Object -Unique)) {
    $required = if ($consoleMode) { "('frida','psutil')" } else { "('frida','psutil','tkinter','openpyxl')" }
    & $candidate -B -c "import sys, importlib.util; sys.exit(0 if sys.version_info >= (3, 10) and all(importlib.util.find_spec(x) for x in $required) else 3)" 2>$null
    if ($LASTEXITCODE -eq 0) { $pythonExe = $candidate; break }
}
if (-not $pythonExe) {
    throw "No compatible Python 3.10+ found (frida, psutil; desktop mode also needs tkinter and openpyxl). Configure config\launcher.json with a Python environment containing the required dependencies. No packages were installed."
}
if ($Check) {
    $launchMode = if ($consoleMode) { "console" } else { "desktop" }
    & $pythonExe -B -c "import sys,json,pathlib; print(json.dumps({'ready':True,'python':sys.executable,'entry':str(pathlib.Path(sys.argv[1]).resolve()),'mode':'check_only_no_game_connection','launch_mode':sys.argv[2]},ensure_ascii=False))" $entry $launchMode
    exit $LASTEXITCODE
}
$memoryOnly = $configured.storage_backend -eq "quark-memory"
$defaultQuarkConfigPath = Join-Path $root "config\quark.local.json"
$selectedQuarkConfigPath = if ($env:SANMOU_QUARK_CONFIG) { $env:SANMOU_QUARK_CONFIG } else { $defaultQuarkConfigPath }
foreach ($quarkConfigPath in (@($defaultQuarkConfigPath, $selectedQuarkConfigPath) | Select-Object -Unique)) {
    if (Test-Path -LiteralPath $quarkConfigPath -PathType Leaf) {
        $quarkConfiguration = Get-Content -LiteralPath $quarkConfigPath -Raw | ConvertFrom-Json
        if ($null -eq $quarkConfiguration -or $quarkConfiguration -isnot [System.Management.Automation.PSCustomObject]) { throw "Quark configuration must be a JSON object; no report log was saved." }
        $memoryOnly = $memoryOnly -or $quarkConfiguration.storage_backend -eq "quark-memory"
    }
}
if (-not (Test-Path -LiteralPath $runtimeConfig -PathType Leaf) -and -not $memoryOnly) {
    throw "Missing runtime configuration. Initialize with tools/setup_quark.py --memory-only before starting."
}
if ($memoryOnly -and -not (Test-Path -LiteralPath $selectedQuarkConfigPath -PathType Leaf)) {
    throw "Memory-only provider configuration is missing; no report log was saved."
}
if ($consoleMode) {
    $arguments = @("-B", $entry, "collect", "--console", "--namespace", $Namespace, "--interval", [string]$Interval, "--parallel", [string]$Parallel)
    if ($Once) { $arguments += "--once" }
    if ($PvpOnly) { $arguments += "--pvp-only" }
    if ($TimeRange) { $arguments += @("--time-range", $TimeRange) }
    if ($GamePid -gt 0) { $arguments += @("--pid", [string]$GamePid) }
    & $pythonExe @arguments
    exit $LASTEXITCODE
}

$guiPython = Join-Path (Split-Path -Parent $pythonExe) "pythonw.exe"
if (-not (Test-Path -LiteralPath $guiPython -PathType Leaf)) { $guiPython = $pythonExe }
$arguments = @("--interval", [string]$Interval)
if ($PvpOnly) { $arguments += "--pvp-only" }
if ($TimeRange) { $arguments += @("--time-range", $TimeRange) }
if ($GamePid -gt 0) { $arguments += @("--pid", [string]$GamePid) }
if ($memoryOnly) {
    $ramLaunchCode = @'
import json, subprocess, sys
process = subprocess.Popen([sys.argv[1], '-B', sys.argv[2], *sys.argv[4:]],
    cwd=sys.argv[3], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), close_fds=True)
try:
    code = process.wait(timeout=1.2)
except subprocess.TimeoutExpired:
    code = None
print(json.dumps({'pid': process.pid, 'early_exit_code': code, 'report_logs_on_disk': False}))
raise SystemExit(0 if code is None else code)
'@
    & $pythonExe -B -c $ramLaunchCode $guiPython $entry $root @arguments
    if ($LASTEXITCODE -ne 0) { throw "Memory-only interface could not start; no report log was saved." }
    Write-Output "Memory-only interface opened; report data and runtime messages remain in RAM."
    exit 0
}
$logDirectory = Join-Path $root "logs"
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$stamp = (Get-Date -Format "yyyyMMdd_HHmmss_fff") + "_" + $PID
$outputLog = Join-Path $logDirectory ("desktop_" + $stamp + ".stdout.log")
$errorLog = Join-Path $logDirectory ("desktop_" + $stamp + ".stderr.log")
$launchCode = @'
import json, subprocess, sys
with open(sys.argv[1], 'ab', buffering=0) as out, open(sys.argv[2], 'ab', buffering=0) as err:
    try:
        process = subprocess.Popen([sys.argv[3], '-B', sys.argv[4], *sys.argv[6:]],
            cwd=sys.argv[5], stdin=subprocess.DEVNULL, stdout=out, stderr=err,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), close_fds=True)
    except Exception as error:
        err.write((str(error) + '\n').encode('utf-8'))
        raise
    try:
        code = process.wait(timeout=1.2)
    except subprocess.TimeoutExpired:
        code = None
    print(json.dumps({'pid':process.pid, 'early_exit_code':code}))
    raise SystemExit(0 if code is None else code)
'@
try {
    # Give the child real log file handles, not pipes serviced by this
    # PowerShell process. They remain usable after the launcher exits.
    $startupResult = & $pythonExe -B -c $launchCode $outputLog $errorLog $guiPython $entry $root @arguments
    $startupExit = $LASTEXITCODE
    # Detect immediate import or argument failures without waiting for the GUI
    # to close. Collection itself starts only when its Start button is pressed.
    if ($startupExit -ne 0) {
        throw "Desktop startup failed (exit $startupExit). See: $errorLog"
    }
    Write-Output ("Desktop interface opened. Startup log: " + $errorLog)
    exit 0
} catch {
    $message = $_.Exception.Message
    Write-Error $message -ErrorAction Continue
    if ($env:BATTLE_ARCHIVE_NO_PAUSE -ne "1") {
        try {
            Add-Type -AssemblyName System.Windows.Forms
            [System.Windows.Forms.MessageBox]::Show($message, "Battle Report Archive", "OK", "Error") | Out-Null
        } catch { }
    }
    exit 2
}
