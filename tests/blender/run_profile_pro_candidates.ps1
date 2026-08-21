param(
    [string]$ExpectedSha = '558CE02B2B36394A528290B38B7E5FE072B5853EAEB7EBAB71E515EDC6C5E905',
    [switch]$WithTracemalloc
)

$ErrorActionPreference = 'Stop'

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Blender = Join-Path $ProjectRoot '.test_runtime\blender-5.0.0\blender-5.0.0-windows-x64\blender.exe'
$Fixture = 'C:\Users\linhp\Downloads\cc.blend'
$Harness = Join-Path $ProjectRoot 'tests\blender\profile_pro_candidates.py'
$Result = Join-Path $ProjectRoot 'benchmarks\pro_01_candidate_profile.json'
$Stdout = Join-Path $ProjectRoot 'benchmarks\pro_01_candidate_profile_stdout.log'
$Stderr = Join-Path $ProjectRoot 'benchmarks\pro_01_candidate_profile_stderr.log'

foreach ($Path in @($Blender, $Fixture, $Harness)) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required profile path is missing: $Path"
    }
}

$FixtureShaBefore = (Get-FileHash -Algorithm SHA256 -LiteralPath $Fixture).Hash.ToUpperInvariant()
if ($FixtureShaBefore -ne $ExpectedSha) {
    throw "Fixture SHA before profile mismatch: $FixtureShaBefore"
}

$ArgumentList = @(
    '--background', "`"$Fixture`"",
    '--python', "`"$Harness`"",
    '--',
    '--project-root', "`"$ProjectRoot`"",
    '--fixture', "`"$Fixture`"",
    '--expected-sha', $ExpectedSha,
    '--result', "`"$Result`""
)
if (-not $WithTracemalloc) {
    $ArgumentList += '--no-tracemalloc'
}
$ProfileProcess = Start-Process -FilePath $Blender -ArgumentList $ArgumentList -PassThru `
    -WindowStyle Hidden -RedirectStandardOutput $Stdout -RedirectStandardError $Stderr
$PeakWorkingSet = 0
while (-not $ProfileProcess.HasExited) {
    try {
        $Sample = Get-Process -Id $ProfileProcess.Id -ErrorAction Stop
        if ([int64]$Sample.WorkingSet64 -gt $PeakWorkingSet) {
            $PeakWorkingSet = [int64]$Sample.WorkingSet64
        }
    } catch {
        # The process may exit between the loop condition and the sample.
    }
    Start-Sleep -Milliseconds 250
}
$ProfileProcess.WaitForExit()
try {
    $FinalSample = Get-Process -Id $ProfileProcess.Id -ErrorAction Stop
    if ([int64]$FinalSample.WorkingSet64 -gt $PeakWorkingSet) {
        $PeakWorkingSet = [int64]$FinalSample.WorkingSet64
    }
} catch {
}
$BlenderExit = $ProfileProcess.ExitCode

$FixtureShaAfter = (Get-FileHash -Algorithm SHA256 -LiteralPath $Fixture).Hash.ToUpperInvariant()
if ($FixtureShaAfter -ne $FixtureShaBefore) {
    throw "Fixture SHA changed after profile: before=$FixtureShaBefore after=$FixtureShaAfter"
}
if ($BlenderExit -ne 0) {
    throw "PERF-P01 Blender profile failed with exit code $BlenderExit"
}
if (-not (Test-Path -LiteralPath $Result -PathType Leaf)) {
    throw "PERF-P01 profile result is missing: $Result"
}

$Evidence = Get-Content -Raw -LiteralPath $Result | ConvertFrom-Json
if ($Evidence.status -ne 'passed') {
    throw "PERF-P01 profile evidence is not passed: $($Evidence.status)"
}
if ([int]$Evidence.island_count -ne 577) {
    throw "PERF-P01 profile island count mismatch: $($Evidence.island_count)"
}
if ($Evidence.fixture_sha_before -ne $ExpectedSha -or $Evidence.fixture_sha_after -ne $ExpectedSha) {
    throw "PERF-P01 evidence fixture SHA mismatch"
}
$Evidence | Add-Member -NotePropertyName process_peak_working_set_bytes `
    -NotePropertyValue $PeakWorkingSet -Force
$Evidence | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $Result -Encoding utf8

Write-Output "PERF-P01 profile passed: 577 islands, fixture SHA unchanged $FixtureShaAfter"
Write-Output "PERF-P01 peak process working set: $PeakWorkingSet bytes"
Write-Output "PERF-P01 result: $Result"
