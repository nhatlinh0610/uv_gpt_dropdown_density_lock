$ErrorActionPreference = 'Stop'

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Blender = Join-Path $ProjectRoot '.test_runtime\blender-5.0.0\blender-5.0.0-windows-x64\blender.exe'
$Fixture = 'C:\Users\linhp\Downloads\cc.blend'
$Harness = Join-Path $ProjectRoot 'tests\blender\align_similar_pro.py'
$BenchmarkRoot = Join-Path $ProjectRoot 'benchmarks'
$DedicatedCreator = Join-Path $ProjectRoot 'tests\blender\create_align_similar_pro_fixture.py'
$DedicatedFixture = Join-Path $ProjectRoot 'benchmarks\pro_02b_dedicated_fixture.blend'
$DedicatedResult = Join-Path $BenchmarkRoot 'pro_02b_dedicated.json'
$Result = Join-Path $BenchmarkRoot 'pro_02b_align_similar_pro.json'
$ExpectedFixtureSha = '49A329EFA1DDA72C4BEB040786590F8B0946BB737266C0498DC6A828C941EEE6'

if (-not (Test-Path -LiteralPath $Blender -PathType Leaf)) {
    throw "PRO-02B Blender missing: $Blender"
}
if (-not (Test-Path -LiteralPath $Fixture -PathType Leaf)) {
    throw "PRO-02B exact fixture missing: $Fixture"
}
if (-not (Test-Path -LiteralPath $Harness -PathType Leaf)) {
    throw "PRO-02B harness missing: $Harness"
}
if (-not (Test-Path -LiteralPath $DedicatedCreator -PathType Leaf)) {
    throw "PRO-02B dedicated fixture creator missing: $DedicatedCreator"
}

New-Item -ItemType Directory -Force -Path $BenchmarkRoot | Out-Null
$FixtureShaBefore = (Get-FileHash -Algorithm SHA256 -LiteralPath $Fixture).Hash.ToUpperInvariant()
if ($FixtureShaBefore -ne $ExpectedFixtureSha) {
    throw "Fixture SHA mismatch before Blender launch: $FixtureShaBefore"
}

$env:PYTHONDONTWRITEBYTECODE = '1'
$Stdout = Join-Path $BenchmarkRoot 'pro_02b_align_similar_pro_stdout.log'
$Arguments = @(
    '--factory-startup',
    '--disable-autoexec',
    '--background',
    $Fixture,
    '--python',
    $Harness,
    '--',
    '--project-root',
    $ProjectRoot,
    '--fixture',
    $Fixture,
    '--fixture-sha-before',
    $FixtureShaBefore,
    '--result',
    $Result
)

Write-Output 'PRO-02B: Blender 5.0 warmup + 3 measured runs'
& $Blender @Arguments 2>&1 | Tee-Object -FilePath $Stdout
$BlenderExit = $LASTEXITCODE

Start-Sleep -Milliseconds 250
$PortablePath = (Resolve-Path -LiteralPath $Blender).Path
$PortableProcesses = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.ExecutablePath -and ((Resolve-Path -LiteralPath $_.ExecutablePath -ErrorAction SilentlyContinue).Path -eq $PortablePath)
})
if ($PortableProcesses.Count -gt 0) {
    throw "Portable Blender process remains: $($PortableProcesses.ProcessId -join ',')"
}

$FixtureShaAfter = (Get-FileHash -Algorithm SHA256 -LiteralPath $Fixture).Hash.ToUpperInvariant()
if ($FixtureShaAfter -ne $FixtureShaBefore) {
    throw "Fixture SHA changed: before=$FixtureShaBefore after=$FixtureShaAfter"
}
if ($BlenderExit -ne 0) {
    throw "Blender harness failed with exit code $BlenderExit"
}
if (-not (Test-Path -LiteralPath $Result -PathType Leaf)) {
    throw "Runtime result missing: $Result"
}
$Evidence = Get-Content -Raw -LiteralPath $Result | ConvertFrom-Json
if ($Evidence.status -ne 'passed') {
    throw "PRO-02B runtime evidence is not passed: $($Evidence.status)"
}
$Measured = @($Evidence.runs | Where-Object { $_.run_kind -eq 'measured' })
if ($Measured.Count -ne 3) {
    throw "Expected 3 measured runs, got $($Measured.Count)"
}

Write-Output (("PRO-02B passed: elapsed_ms={0}; exact={1}; groups={2}; mapping_delta={3}; " +
    "master_delta={4}; unselected_delta={5}; SHA={6}") -f
    (($Evidence.measured_elapsed_ms -join '/')),
    (($Measured | ForEach-Object { $_.result.aligned_exact }) -join '/'),
    (($Measured | ForEach-Object { $_.result.group_count }) -join '/'),
    (($Evidence.measured_mapping_max_delta -join '/')),
    (($Evidence.measured_master_delta -join '/')),
    (($Evidence.measured_unselected_delta -join '/')),
    $FixtureShaAfter)

Write-Output 'PRO-02B: creating dedicated synthetic fixture'
& $Blender --factory-startup --disable-autoexec --background --python $DedicatedCreator -- --output $DedicatedFixture 2>&1
$CreateExit = $LASTEXITCODE
if ($CreateExit -ne 0) {
    throw "Dedicated fixture creation failed with exit code $CreateExit"
}
if (-not (Test-Path -LiteralPath $DedicatedFixture -PathType Leaf)) {
    throw "Dedicated fixture was not created: $DedicatedFixture"
}

$DedicatedShaBefore = (Get-FileHash -Algorithm SHA256 -LiteralPath $DedicatedFixture).Hash.ToUpperInvariant()
$DedicatedStdout = Join-Path $BenchmarkRoot 'pro_02b_dedicated_stdout.log'
$DedicatedArguments = @(
    '--factory-startup',
    '--disable-autoexec',
    '--background',
    $DedicatedFixture,
    '--python',
    $Harness,
    '--',
    '--mode',
    'dedicated',
    '--project-root',
    $ProjectRoot,
    '--fixture',
    $DedicatedFixture,
    '--result',
    $DedicatedResult
)
& $Blender @DedicatedArguments 2>&1 | Tee-Object -FilePath $DedicatedStdout
$DedicatedExit = $LASTEXITCODE
Start-Sleep -Milliseconds 250
$DedicatedShaAfter = (Get-FileHash -Algorithm SHA256 -LiteralPath $DedicatedFixture).Hash.ToUpperInvariant()
if ($DedicatedShaAfter -ne $DedicatedShaBefore) {
    throw "Dedicated fixture changed: before=$DedicatedShaBefore after=$DedicatedShaAfter"
}
if ($DedicatedExit -ne 0) {
    throw "Dedicated runtime failed with exit code $DedicatedExit"
}
if (-not (Test-Path -LiteralPath $DedicatedResult -PathType Leaf)) {
    throw "Dedicated runtime result missing: $DedicatedResult"
}
$DedicatedEvidence = Get-Content -Raw -LiteralPath $DedicatedResult | ConvertFrom-Json
if ($DedicatedEvidence.status -ne 'passed') {
    throw "Dedicated runtime evidence is not passed: $($DedicatedEvidence.status)"
}
if ($DedicatedEvidence.cases.Count -ne 6) {
    throw "Dedicated runtime evidence is stale or incomplete: expected 6 cases, got $($DedicatedEvidence.cases.Count)"
}
if ($DedicatedEvidence.fixture_sha256_before -ne $DedicatedShaBefore -or
    $DedicatedEvidence.fixture_sha256_after_in_process -ne $DedicatedShaAfter) {
    throw "Dedicated runtime evidence SHA mismatch"
}
$PortableProcesses = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.ExecutablePath -and ((Resolve-Path -LiteralPath $_.ExecutablePath -ErrorAction SilentlyContinue).Path -eq $PortablePath)
})
if ($PortableProcesses.Count -gt 0) {
    throw "Portable Blender process remains after dedicated runtime: $($PortableProcesses.ProcessId -join ',')"
}
Write-Output ("PRO-02B dedicated passed: cases={0}; fixture_sha={1}; result={2}" -f
    $DedicatedEvidence.cases.Count,
    $DedicatedShaAfter,
    $DedicatedResult)
