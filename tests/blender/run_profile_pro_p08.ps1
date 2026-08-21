param(
    [ValidateSet('focus', 'dedicated', 'full-modal')]
    [string]$Mode = 'full-modal',
    [int]$MaxProcessSeconds = 90,
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
        Harness = 'profile_pro_focus_p08.py'
        Result = 'pro_08_focus.json'
    }
    'dedicated' = @{
        Fixture = $DedicatedFixture
        Harness = 'profile_pro_modal_p08.py'
        Result = 'pro_08_modal.json'
    }
    'full-modal' = @{
        Fixture = $CcFixture
        Harness = 'profile_pro_full_modal_p08.py'
        Result = 'pro_08_full_modal.json'
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
        throw "PERF-P08 required file missing: $path"
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
    throw "PERF-P08 fixture SHA unstable before launch: $shaBefore / $shaCheck"
}
if ($Mode -in @('focus', 'full-modal') -and $shaBefore -ne $ExpectedCcSha) {
    throw "PERF-P08 cc.blend SHA mismatch: $shaBefore"
}
Assert-NoPortableBlender

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
    '--result',
    ('"{0}"' -f $Result)
)

$process = Start-Process -FilePath $Blender -ArgumentList $arguments -WindowStyle Hidden `
    -PassThru -RedirectStandardOutput $Stdout -RedirectStandardError $Stderr
$peakWorkingSet = [int64]0
$deadline = (Get-Date).AddSeconds($MaxProcessSeconds)
$timedOut = $false
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
            $timedOut = $true
        } else {
            throw "Refused to terminate unverified process $($process.Id)"
        }
        break
    }
    Start-Sleep -Milliseconds 250
}
$process.WaitForExit()
$process.Refresh()
if ($process.WorkingSet64 -gt $peakWorkingSet) {
    $peakWorkingSet = $process.WorkingSet64
}
Start-Sleep -Milliseconds 500
Assert-NoPortableBlender
$shaAfter = Get-Sha $Fixture
if ($shaAfter -ne $shaBefore) {
    throw "PERF-P08 fixture changed: before=$shaBefore after=$shaAfter"
}
if ($timedOut) {
    throw "Portable Blender $Mode exceeded $MaxProcessSeconds seconds"
}
$exitCode = $process.ExitCode
if ($null -eq $exitCode) { $exitCode = 0 }
if ($exitCode -ne 0) {
    throw "PERF-P08 Blender failed for $Mode with exit code $exitCode"
}
if (-not (Test-Path -LiteralPath $Result -PathType Leaf)) {
    throw "PERF-P08 result missing: $Result"
}

$evidence = Get-Content -Raw -LiteralPath $Result | ConvertFrom-Json
if ($evidence.fixture_sha256_before -ne $shaBefore -or
    $evidence.fixture_sha256_after_in_process -ne $shaAfter) {
    throw 'PERF-P08 evidence SHA mismatch'
}
$evidence | Add-Member -NotePropertyName process_peak_working_set_bytes -NotePropertyValue ([int64]$peakWorkingSet) -Force
$evidence | Add-Member -NotePropertyName fixture_sha256_after_runner -NotePropertyValue $shaAfter -Force
$evidence | ConvertTo-Json -Depth 80 | Set-Content -LiteralPath $Result -Encoding UTF8

$summary = if ($Mode -eq 'full-modal') {
    'status={0}; ticks={1}; enum_ops={2}; records={3}; pairs={4}; graph_ops={5}; graph_slices={6}; max_tick_ms={7}; max_graph_slice_ms={8}' -f `
        $evidence.status,
        $evidence.timer_ticks,
        $evidence.session_report.enum_primitive_ops,
        $evidence.session_report.planner_record_count,
        $evidence.session_report.candidate_pairs_processed,
        $evidence.session_report.graph_primitive_ops,
        $evidence.session_report.graph_slices,
        $evidence.session_report.max_tick_ms,
        $evidence.session_report.max_graph_slice_ms
} elseif ($Mode -eq 'dedicated') {
    'status={0}; max_tick_ms={1}; max_graph_slice_ms={2}' -f `
        $evidence.status,
        $evidence.max_tick_ms,
        $evidence.completion.result.max_graph_slice_ms
} else {
    'measured={0}; elapsed={1}' -f $evidence.measured_runs, (($evidence.measured_elapsed_ms -join '/'))
}
Write-Output ("PERF-P08 {0}: {1}; peak_ws={2}; SHA={3}" -f $Mode, $summary, $peakWorkingSet, $shaAfter)
