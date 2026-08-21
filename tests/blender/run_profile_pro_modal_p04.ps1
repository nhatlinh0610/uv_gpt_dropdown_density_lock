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
$Harness = (Resolve-Path (Join-Path $PSScriptRoot 'profile_pro_modal_p04.py')).Path
$Result = Join-Path $ProjectRoot 'benchmarks\pro_04_modal_1024.json'
$Stdout = Join-Path $ProjectRoot 'benchmarks\pro_04_modal_1024_stdout.log'
$Stderr = Join-Path $ProjectRoot 'benchmarks\pro_04_modal_1024_stderr.log'
$shaBefore = (Get-FileHash -Algorithm SHA256 -LiteralPath $Fixture).Hash.ToUpperInvariant()
$args = @(
    '--factory-startup', '--disable-autoexec', '--background', ('"{0}"' -f $Fixture),
    '--python', ('"{0}"' -f $Harness), '--',
    '--project-root', ('"{0}"' -f $ProjectRoot), '--fixture', ('"{0}"' -f $Fixture),
    '--fixture-sha-before', $shaBefore, '--result', ('"{0}"' -f $Result)
)
$process = Start-Process -FilePath $Blender -ArgumentList $args -WindowStyle Hidden `
    -PassThru -RedirectStandardOutput $Stdout -RedirectStandardError $Stderr
$deadline = (Get-Date).AddSeconds($MaxProcessSeconds)
while (-not $process.HasExited) {
    if ((Get-Date) -gt $deadline) {
        $cim = Get-CimInstance Win32_Process -Filter ("ProcessId={0}" -f $process.Id) -ErrorAction SilentlyContinue
        $resolved = if ($cim -and $cim.ExecutablePath) { (Resolve-Path -LiteralPath $cim.ExecutablePath).Path } else { '' }
        if ($resolved -eq $Blender) { Stop-Process -Id $process.Id -Force }
        throw "Portable Blender dedicated modal exceeded $MaxProcessSeconds seconds"
    }
    Start-Sleep -Milliseconds 250
}
$process.WaitForExit()
$exitCode = $process.ExitCode
if ($null -eq $exitCode) { $exitCode = 0 }
Start-Sleep -Milliseconds 250
$orphans = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.ExecutablePath -and ((Resolve-Path -LiteralPath $_.ExecutablePath -ErrorAction SilentlyContinue).Path -eq $Blender)
})
if ($orphans.Count -gt 0) { throw "Portable Blender dedicated modal orphan remains: $($orphans.ProcessId -join ',')" }
$shaAfter = (Get-FileHash -Algorithm SHA256 -LiteralPath $Fixture).Hash.ToUpperInvariant()
if ($shaAfter -ne $shaBefore) { throw "Dedicated modal fixture changed: before=$shaBefore after=$shaAfter" }
if ($exitCode -ne 0) { throw "Dedicated modal Blender failed with exit code $exitCode" }
if (-not (Test-Path -LiteralPath $Result -PathType Leaf)) { throw "Dedicated modal result missing: $Result" }
$evidence = Get-Content -Raw -LiteralPath $Result | ConvertFrom-Json
if ($evidence.status -ne 'passed') { throw 'Dedicated modal evidence not passed' }
$evidence | Add-Member -NotePropertyName fixture_sha256_after_runner -NotePropertyValue $shaAfter -Force
$evidence | ConvertTo-Json -Depth 40 | Set-Content -LiteralPath $Result -Encoding UTF8
Write-Output ("PERF-P04 dedicated modal: max_tick_ms={0}; max_corr_ms={1}; SHA={2}" -f
    $evidence.max_tick_ms, $evidence.max_correspondence_ms, $shaAfter)
