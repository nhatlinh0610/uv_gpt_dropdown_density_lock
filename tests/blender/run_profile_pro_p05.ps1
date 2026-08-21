param(
    [ValidateSet('focus', 'dedicated', 'modal', 'full-modal', 'full-selection')]
    [string]$Mode = 'full-modal',
    [ValidateSet(16, 32, 64, 128)]
    [int]$YieldEvery = 64,
    [int]$MaxProcessSeconds = 120,
    [string]$ExpectedCcSha = '49A329EFA1DDA72C4BEB040786590F8B0946BB737266C0498DC6A828C941EEE6'
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Blender = (Resolve-Path (Join-Path $ProjectRoot '.test_runtime\blender-5.0.0\blender-5.0.0-windows-x64\blender.exe')).Path
$CcFixture = (Resolve-Path 'C:\Users\linhp\Downloads\cc.blend').Path
$DedicatedFixture = (Resolve-Path (Join-Path $ProjectRoot 'benchmarks\pro_02b_dedicated_fixture.blend')).Path
$BenchmarkRoot = Join-Path $ProjectRoot 'benchmarks'

$config = @{
    'focus' = @{
        Fixture = $CcFixture
        Harness = 'profile_pro_focus_p05.py'
        Result = 'pro_05_focus_y{0}.json' -f $YieldEvery
        Extra = @()
        RequirePassed = $true
    }
    'dedicated' = @{
        Fixture = $DedicatedFixture
        Harness = 'profile_pro_focus_p05.py'
        Result = 'pro_05_dedicated_y{0}.json' -f $YieldEvery
        Extra = @('--mode', 'dedicated')
        RequirePassed = $true
    }
    'modal' = @{
        Fixture = $DedicatedFixture
        Harness = 'profile_pro_modal_p05.py'
        Result = 'pro_05_modal_y{0}.json' -f $YieldEvery
        Extra = @()
        RequirePassed = $true
    }
    'full-modal' = @{
        Fixture = $CcFixture
        Harness = 'profile_pro_full_modal_p05.py'
        Result = 'pro_05_full_modal_y{0}.json' -f $YieldEvery
        Extra = @()
        RequirePassed = $true
    }
    'full-selection' = @{
        Fixture = $CcFixture
        Harness = 'profile_pro_full_selection_p05.py'
        Result = 'pro_05_full_y{0}.json' -f $YieldEvery
        Extra = @()
        RequirePassed = $false
    }
}[$Mode]

$Fixture = $config.Fixture
$Harness = (Resolve-Path (Join-Path $PSScriptRoot $config.Harness)).Path
$Result = Join-Path $BenchmarkRoot $config.Result
$Stdout = Join-Path $BenchmarkRoot ('{0}_stdout.log' -f [IO.Path]::GetFileNameWithoutExtension($config.Result))
$Stderr = Join-Path $BenchmarkRoot ('{0}_stderr.log' -f [IO.Path]::GetFileNameWithoutExtension($config.Result))
$PortablePath = $Blender

foreach ($path in @($Blender, $Fixture, $Harness)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "PERF-P05 required file missing: $path"
    }
}
New-Item -ItemType Directory -Force -Path $BenchmarkRoot | Out-Null

function Get-Sha([string]$Path) {
    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToUpperInvariant()
}

function Assert-NoPortableBlender {
    $orphans = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.ExecutablePath -and ((Resolve-Path -LiteralPath $_.ExecutablePath -ErrorAction SilentlyContinue).Path -eq $PortablePath)
    })
    if ($orphans.Count -gt 0) {
        throw "Portable Blender orphan remains: $($orphans.ProcessId -join ',')"
    }
}

$shaBefore = Get-Sha $Fixture
Start-Sleep -Milliseconds 100
$shaCheck = Get-Sha $Fixture
if ($shaBefore -ne $shaCheck) {
    throw "PERF-P05 fixture SHA was unstable before launch: $shaBefore / $shaCheck"
}
if ($Mode -in @('focus', 'full-modal', 'full-selection') -and $shaBefore -ne $ExpectedCcSha) {
    throw "PERF-P05 cc.blend SHA mismatch: $shaBefore"
}

$arguments = @(
    '--factory-startup',
    '--disable-autoexec',
    '--background',
    ('"{0}"' -f $Fixture),
    '--python',
    ('"{0}"' -f $Harness),
    '--',
    '--project-root',
    ('"{0}"' -f $ProjectRoot),
    '--fixture',
    ('"{0}"' -f $Fixture),
    '--fixture-sha-before',
    $shaBefore,
    '--yield-every',
    [string]$YieldEvery,
    '--result',
    ('"{0}"' -f $Result)
)
$arguments += $config.Extra

$process = Start-Process -FilePath $Blender -ArgumentList $arguments -WindowStyle Hidden `
    -PassThru -RedirectStandardOutput $Stdout -RedirectStandardError $Stderr
$peakWorkingSet = [int64]0
$deadline = (Get-Date).AddSeconds($MaxProcessSeconds)
while (-not $process.HasExited) {
    $process.Refresh()
    if ($process.WorkingSet64 -gt $peakWorkingSet) {
        $peakWorkingSet = [int64]$process.WorkingSet64
    }
    if ((Get-Date) -gt $deadline) {
        $cim = Get-CimInstance Win32_Process -Filter ("ProcessId={0}" -f $process.Id) -ErrorAction SilentlyContinue
        $resolved = if ($cim -and $cim.ExecutablePath) {
            (Resolve-Path -LiteralPath $cim.ExecutablePath -ErrorAction SilentlyContinue).Path
        } else { '' }
        if ($resolved -eq $PortablePath) {
            Stop-Process -Id $process.Id -Force
        } else {
            throw "Refused to terminate unverified process $($process.Id)"
        }
        Start-Sleep -Milliseconds 500
        Assert-NoPortableBlender
        $shaAfterTimeout = Get-Sha $Fixture
        if ($shaAfterTimeout -ne $shaBefore) {
            throw "Fixture changed after timed-out PERF-P05 process: $shaBefore / $shaAfterTimeout"
        }
        throw "Portable Blender $Mode yield=$YieldEvery exceeded $MaxProcessSeconds seconds"
    }
    Start-Sleep -Milliseconds 250
}
$process.WaitForExit()
$process.Refresh()
if ($process.WorkingSet64 -gt $peakWorkingSet) {
    $peakWorkingSet = [int64]$process.WorkingSet64
}
$exitCode = $process.ExitCode
if ($null -eq $exitCode) { $exitCode = 0 }
Start-Sleep -Milliseconds 250
Assert-NoPortableBlender

$shaAfter = Get-Sha $Fixture
if ($shaAfter -ne $shaBefore) {
    throw "PERF-P05 fixture changed: before=$shaBefore after=$shaAfter"
}
if ($exitCode -ne 0) {
    throw "PERF-P05 Blender failed for $Mode yield=$YieldEvery with exit code $exitCode"
}
if (-not (Test-Path -LiteralPath $Result -PathType Leaf)) {
    throw "PERF-P05 result missing: $Result"
}

$evidence = Get-Content -Raw -LiteralPath $Result | ConvertFrom-Json
if ($evidence.fixture_sha256_before -ne $shaBefore -or
    $evidence.fixture_sha256_after_in_process -ne $shaAfter) {
    throw 'PERF-P05 evidence SHA mismatch'
}
if ($config.RequirePassed -and $evidence.status -ne 'passed') {
    throw "PERF-P05 $Mode evidence was not passed: $($evidence.status)"
}
$evidence | Add-Member -NotePropertyName process_peak_working_set_bytes -NotePropertyValue $peakWorkingSet -Force
$evidence | Add-Member -NotePropertyName fixture_sha256_after_runner -NotePropertyValue $shaAfter -Force
$evidence | ConvertTo-Json -Depth 60 | Set-Content -LiteralPath $Result -Encoding UTF8

$report = switch ($Mode) {
    'modal' { 'max_tick_ms={0}; max_corr_ms={1}' -f $evidence.max_tick_ms, $evidence.max_correspondence_ms }
    'full-modal' { 'max_tick_ms={0}; max_corr_ms={1}; ticks={2}' -f $evidence.session_report.max_tick_ms, $evidence.session_report.max_correspondence_ms, $evidence.timer_ticks }
    'full-selection' { 'status={0}; measured={1}; elapsed={2}' -f $evidence.status, $evidence.measured_runs, (($evidence.measured_elapsed_ms -join '/')) }
    'focus' { 'measured={0}; elapsed={1}' -f $evidence.measured_runs, (($evidence.measured_elapsed_ms -join '/')) }
    'dedicated' { 'cases={0}' -f $evidence.cases.Count }
}
Write-Output ("PERF-P05 {0} yield={1}: {2}; peak_ws={3}; SHA={4}" -f $Mode, $YieldEvery, $report, $peakWorkingSet, $shaAfter)
