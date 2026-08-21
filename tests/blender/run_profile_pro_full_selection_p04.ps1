param(
    [string]$ExpectedSha = '49A329EFA1DDA72C4BEB040786590F8B0946BB737266C0498DC6A828C941EEE6',
    [int]$MaxProcessSeconds = 120
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Blender = (Resolve-Path (Join-Path $ProjectRoot '.test_runtime\blender-5.0.0\blender-5.0.0-windows-x64\blender.exe')).Path
$Fixture = 'C:\Users\linhp\Downloads\cc.blend'
$Harness = (Resolve-Path (Join-Path $PSScriptRoot 'profile_pro_full_selection_p04.py')).Path
$Result = Join-Path $ProjectRoot 'benchmarks\pro_04_full_selection_1024.json'
$Stdout = Join-Path $ProjectRoot 'benchmarks\pro_04_full_selection_1024_stdout.log'
$Stderr = Join-Path $ProjectRoot 'benchmarks\pro_04_full_selection_1024_stderr.log'

$shaBefore = (Get-FileHash -Algorithm SHA256 -LiteralPath $Fixture).Hash.ToUpperInvariant()
$shaCheck = (Get-FileHash -Algorithm SHA256 -LiteralPath $Fixture).Hash.ToUpperInvariant()
if ($shaBefore -ne $shaCheck) { throw 'Full selection preflight SHA was unstable' }
if ($shaBefore -ne $ExpectedSha) { throw "Full selection fixture SHA mismatch: $shaBefore" }

$args = @(
    '--factory-startup', '--disable-autoexec', '--background', ('"{0}"' -f $Fixture),
    '--python', ('"{0}"' -f $Harness), '--',
    '--project-root', ('"{0}"' -f $ProjectRoot), '--fixture', ('"{0}"' -f $Fixture),
    '--fixture-sha-before', $shaBefore, '--result', ('"{0}"' -f $Result)
)
$process = Start-Process -FilePath $Blender -ArgumentList $args -WindowStyle Hidden `
    -PassThru -RedirectStandardOutput $Stdout -RedirectStandardError $Stderr
$peak = [int64]0
$deadline = (Get-Date).AddSeconds($MaxProcessSeconds)
while (-not $process.HasExited) {
    $process.Refresh()
    if ($process.WorkingSet64 -gt $peak) { $peak = [int64]$process.WorkingSet64 }
    if ((Get-Date) -gt $deadline) {
        $cim = Get-CimInstance Win32_Process -Filter ("ProcessId={0}" -f $process.Id) -ErrorAction SilentlyContinue
        $resolved = if ($cim -and $cim.ExecutablePath) { (Resolve-Path -LiteralPath $cim.ExecutablePath).Path } else { '' }
        if ($resolved -eq $Blender) { Stop-Process -Id $process.Id -Force }
        throw "Portable Blender full selection exceeded $MaxProcessSeconds seconds"
    }
    Start-Sleep -Milliseconds 250
}
$process.WaitForExit()
$process.Refresh()
if ($process.WorkingSet64 -gt $peak) { $peak = [int64]$process.WorkingSet64 }
$exitCode = $process.ExitCode
if ($null -eq $exitCode) { $exitCode = 0 }
Start-Sleep -Milliseconds 250
$orphans = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.ExecutablePath -and ((Resolve-Path -LiteralPath $_.ExecutablePath -ErrorAction SilentlyContinue).Path -eq $Blender)
})
if ($orphans.Count -gt 0) { throw "Portable Blender orphan remains: $($orphans.ProcessId -join ',')" }
$shaAfter = (Get-FileHash -Algorithm SHA256 -LiteralPath $Fixture).Hash.ToUpperInvariant()
if ($shaAfter -ne $shaBefore) { throw "Full selection fixture SHA changed: before=$shaBefore after=$shaAfter" }
if ($exitCode -ne 0) { throw "Full selection Blender failed with exit code $exitCode" }
if (-not (Test-Path -LiteralPath $Result -PathType Leaf)) { throw "Full selection result missing: $Result" }
$evidence = Get-Content -Raw -LiteralPath $Result | ConvertFrom-Json
if ($evidence.fixture_sha256_before -ne $shaBefore -or $evidence.fixture_sha256_after_in_process -ne $shaAfter) {
    throw 'Full selection evidence SHA mismatch'
}
$evidence | Add-Member -NotePropertyName process_peak_working_set_bytes -NotePropertyValue $peak -Force
$evidence | Add-Member -NotePropertyName fixture_sha256_after_runner -NotePropertyValue $shaAfter -Force
$evidence | ConvertTo-Json -Depth 40 | Set-Content -LiteralPath $Result -Encoding UTF8
Write-Output ("PERF-P04 full selection: status={0}; measured={1}; elapsed={2}; peak_ws={3}; SHA={4}" -f
    $evidence.status, $evidence.measured_runs,
    (($evidence.measured_elapsed_ms -join '/')), $peak, $shaAfter)
