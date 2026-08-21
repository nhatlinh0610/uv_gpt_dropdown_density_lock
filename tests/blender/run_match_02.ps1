$ErrorActionPreference = 'Stop'

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Blender = Join-Path $ProjectRoot '.test_runtime\blender-5.0.0\blender-5.0.0-windows-x64\blender.exe'
$Fixture = 'C:\Users\linhp\Downloads\cc.blend'
$Harness = Join-Path $ProjectRoot 'tests\blender\match_02_fixture.py'
$BenchmarkRoot = Join-Path $ProjectRoot 'benchmarks'
$VersionOutput = Join-Path $BenchmarkRoot 'match_02_blender_version.txt'
$StdoutOutput = Join-Path $BenchmarkRoot 'match_02_blender_stdout.log'
$ExpectedFixtureSha = '840EA32C822784201EFAB30B9441A98621E6FBD87DC9BDD431B7EB90A2FF93CD'

if (-not (Test-Path -LiteralPath $Blender -PathType Leaf)) {
    throw "MATCH-02 portable Blender missing: $Blender"
}
if (-not (Test-Path -LiteralPath $Fixture -PathType Leaf)) {
    throw "MATCH-02 exact fixture missing: $Fixture"
}
if (-not (Test-Path -LiteralPath $Harness -PathType Leaf)) {
    throw "MATCH-02 harness missing: $Harness"
}

New-Item -ItemType Directory -Force -Path $BenchmarkRoot | Out-Null
$FixtureShaBefore = (Get-FileHash -Algorithm SHA256 -LiteralPath $Fixture).Hash.ToUpperInvariant()
if ($FixtureShaBefore -ne $ExpectedFixtureSha) {
    throw "Fixture SHA mismatch before Blender launch: $FixtureShaBefore (expected $ExpectedFixtureSha)"
}

$env:PYTHONDONTWRITEBYTECODE = '1'
& $Blender --version | Tee-Object -FilePath $VersionOutput
$VersionExit = $LASTEXITCODE
if ($VersionExit -ne 0) {
    throw "Blender --version failed with exit code $VersionExit"
}

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
    $FixtureShaBefore
)

& $Blender @Arguments 2>&1 | Tee-Object -FilePath $StdoutOutput
$BlenderExit = $LASTEXITCODE

# Read-only orphan check.  Never terminate a process here; the user may have
# an unrelated Blender 5.2 process running.  Only the exact portable path is
# considered a MATCH-02 child process.
Start-Sleep -Milliseconds 250
$PortableProcesses = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.ExecutablePath -and ((Resolve-Path -LiteralPath $_.ExecutablePath -ErrorAction SilentlyContinue).Path -eq (Resolve-Path -LiteralPath $Blender).Path)
})
if ($PortableProcesses.Count -gt 0) {
    throw "Portable Blender process remains after MATCH-02: $($PortableProcesses.ProcessId -join ',')"
}

$FixtureShaAfter = (Get-FileHash -Algorithm SHA256 -LiteralPath $Fixture).Hash.ToUpperInvariant()
if ($FixtureShaAfter -ne $FixtureShaBefore) {
    throw "Fixture SHA changed after Blender run: before=$FixtureShaBefore after=$FixtureShaAfter"
}
if ($BlenderExit -ne 0) {
    throw "MATCH-02 Blender harness failed with exit code $BlenderExit"
}

Write-Output "MATCH-02 runner completed: fixture SHA unchanged $FixtureShaAfter"
