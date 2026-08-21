param(
    [string]$ExpectedSha = '49A329EFA1DDA72C4BEB040786590F8B0946BB737266C0498DC6A828C941EEE6',
    [int]$MaxProcessSeconds = 120
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Blender = (Resolve-Path (Join-Path $ProjectRoot '.test_runtime\blender-5.0.0\blender-5.0.0-windows-x64\blender.exe')).Path
$Fixture = 'C:\Users\linhp\Downloads\cc.blend'
$Harness = (Resolve-Path (Join-Path $ProjectRoot 'tests\blender\profile_pro_full_selection.py')).Path
$BenchmarkRoot = Join-Path $ProjectRoot 'benchmarks'
$Result = Join-Path $BenchmarkRoot 'pro_02_full_selection.json'
$Stdout = Join-Path $BenchmarkRoot 'pro_02_full_selection_stdout.log'
$Stderr = Join-Path $BenchmarkRoot 'pro_02_full_selection_stderr.log'

if (-not (Test-Path -LiteralPath $Fixture -PathType Leaf)) { throw "Fixture missing: $Fixture" }
New-Item -ItemType Directory -Force -Path $BenchmarkRoot | Out-Null
$FixtureShaBefore = (Get-FileHash -Algorithm SHA256 -LiteralPath $Fixture).Hash.ToUpperInvariant()
$FixtureShaCheck = (Get-FileHash -Algorithm SHA256 -LiteralPath $Fixture).Hash.ToUpperInvariant()
if ($FixtureShaBefore -ne $FixtureShaCheck) { throw "Fixture changed during preflight hash checks" }
if ($FixtureShaBefore -ne $ExpectedSha) { throw "Fixture SHA mismatch: $FixtureShaBefore" }

$ArgumentList = @(
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
    $FixtureShaBefore,
    '--result',
    ('"{0}"' -f $Result)
)

$PortablePath = $Blender
$Process = Start-Process -FilePath $Blender -ArgumentList $ArgumentList -WindowStyle Hidden -PassThru -RedirectStandardOutput $Stdout -RedirectStandardError $Stderr
$PeakWorkingSet = [int64]0
$Deadline = (Get-Date).AddSeconds($MaxProcessSeconds)
while (-not $Process.HasExited) {
    $Process.Refresh()
    if ($Process.WorkingSet64 -gt $PeakWorkingSet) { $PeakWorkingSet = [int64]$Process.WorkingSet64 }
    if ((Get-Date) -gt $Deadline) {
        $cim = Get-CimInstance Win32_Process -Filter ("ProcessId={0}" -f $Process.Id) -ErrorAction SilentlyContinue
        $resolved = if ($cim -and $cim.ExecutablePath) { (Resolve-Path -LiteralPath $cim.ExecutablePath).Path } else { '' }
        if ($resolved -eq $PortablePath) { Stop-Process -Id $Process.Id -Force }
        throw "Portable Blender full profile exceeded $MaxProcessSeconds seconds"
    }
    Start-Sleep -Milliseconds 250
}
$Process.WaitForExit()
$Process.Refresh()
if ($Process.WorkingSet64 -gt $PeakWorkingSet) { $PeakWorkingSet = [int64]$Process.WorkingSet64 }
$ExitCode = $Process.ExitCode
# Some bundled PowerShell hosts expose a null ExitCode after a redirected
# Start-Process has already reaped the child.  The result/SHA/orphan checks
# below remain authoritative; preserve a real nonzero code when available.
if ($null -eq $ExitCode) { $ExitCode = 0 }
Start-Sleep -Milliseconds 250

$orphan = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.ExecutablePath -and ((Resolve-Path -LiteralPath $_.ExecutablePath -ErrorAction SilentlyContinue).Path -eq $PortablePath)
})
if ($orphan.Count -gt 0) { throw "Portable Blender orphan remains: $($orphan.ProcessId -join ',')" }

$FixtureShaAfter = (Get-FileHash -Algorithm SHA256 -LiteralPath $Fixture).Hash.ToUpperInvariant()
if ($FixtureShaAfter -ne $FixtureShaBefore) { throw "Fixture SHA changed: before=$FixtureShaBefore after=$FixtureShaAfter" }
if ($ExitCode -ne 0) { throw "Full profile Blender failed with exit code $ExitCode" }
if (-not (Test-Path -LiteralPath $Result -PathType Leaf)) { throw "Full profile result missing: $Result" }

$Evidence = Get-Content -Raw -LiteralPath $Result | ConvertFrom-Json
if ($Evidence.fixture_sha256_before -ne $FixtureShaBefore -or $Evidence.fixture_sha256_after_in_process -ne $FixtureShaAfter) {
    throw 'Full profile evidence SHA mismatch'
}
$Evidence | Add-Member -NotePropertyName process_peak_working_set_bytes -NotePropertyValue $PeakWorkingSet -Force
$Evidence | Add-Member -NotePropertyName fixture_sha256_after_runner -NotePropertyValue $FixtureShaAfter -Force
$Evidence | ConvertTo-Json -Depth 40 | Set-Content -LiteralPath $Result -Encoding UTF8

Write-Output ("PERF-P02 full profile: status={0}; measured={1}; elapsed={2}; peak_working_set_bytes={3}; SHA={4}" -f
    $Evidence.status,
    $Evidence.measured_runs,
    (($Evidence.measured_elapsed_ms -join '/')),
    $PeakWorkingSet,
    $FixtureShaAfter)
