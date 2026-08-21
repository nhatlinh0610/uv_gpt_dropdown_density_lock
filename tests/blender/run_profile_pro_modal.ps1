param(
    [string]$Fixture = '',
    [int]$MaxProcessSeconds = 90
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Blender = (Resolve-Path (Join-Path $ProjectRoot '.test_runtime\blender-5.0.0\blender-5.0.0-windows-x64\blender.exe')).Path
if ([string]::IsNullOrWhiteSpace($Fixture)) {
    $Fixture = Join-Path $ProjectRoot 'benchmarks\pro_02b_dedicated_fixture.blend'
}
$Fixture = (Resolve-Path -LiteralPath $Fixture).Path
$Harness = (Resolve-Path (Join-Path $ProjectRoot 'tests\blender\profile_pro_modal.py')).Path
$Result = Join-Path $ProjectRoot 'benchmarks\pro_03_modal.json'
$Stdout = Join-Path $ProjectRoot 'benchmarks\pro_03_modal_stdout.log'
$Stderr = Join-Path $ProjectRoot 'benchmarks\pro_03_modal_stderr.log'

$shaBefore = (Get-FileHash -Algorithm SHA256 -LiteralPath $Fixture).Hash.ToUpperInvariant()
$portablePath = $Blender
$args = @(
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
        if ($resolved -eq $portablePath) { Stop-Process -Id $process.Id -Force }
        throw "Portable Blender modal profile exceeded $MaxProcessSeconds seconds"
    }
    Start-Sleep -Milliseconds 250
}
$process.WaitForExit()
$process.Refresh()
if ($process.WorkingSet64 -gt $peak) { $peak = [int64]$process.WorkingSet64 }
$exitCode = $process.ExitCode
if ($null -eq $exitCode) { $exitCode = 0 }
if ($exitCode -ne 0) { throw "Modal profile Blender failed with exit code $exitCode" }
Start-Sleep -Milliseconds 250
$orphans = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.ExecutablePath -and ((Resolve-Path -LiteralPath $_.ExecutablePath -ErrorAction SilentlyContinue).Path -eq $portablePath)
})
if ($orphans.Count -gt 0) { throw "Portable Blender modal orphan remains: $($orphans.ProcessId -join ',')" }
$shaAfter = (Get-FileHash -Algorithm SHA256 -LiteralPath $Fixture).Hash.ToUpperInvariant()
if ($shaAfter -ne $shaBefore) { throw "Modal fixture SHA changed: before=$shaBefore after=$shaAfter" }
if (-not (Test-Path -LiteralPath $Result -PathType Leaf)) { throw "Modal result missing: $Result" }
$evidence = Get-Content -Raw -LiteralPath $Result | ConvertFrom-Json
if ($evidence.status -ne 'passed') { throw "Modal evidence not passed: $($evidence.status)" }
$evidence | Add-Member -NotePropertyName process_peak_working_set_bytes -NotePropertyValue $peak -Force
$evidence | Add-Member -NotePropertyName fixture_sha256_after_runner -NotePropertyValue $shaAfter -Force
$evidence | ConvertTo-Json -Depth 40 | Set-Content -LiteralPath $Result -Encoding UTF8
Write-Output ("PERF-P03 modal passed: max_tick_ms={0}; max_corr_ms={1}; peak_ws={2}; SHA={3}" -f
    $evidence.max_tick_ms, $evidence.max_correspondence_ms, $peak, $shaAfter)
