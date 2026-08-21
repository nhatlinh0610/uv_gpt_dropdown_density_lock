param(
    [Parameter(Mandatory = $true)]
    [string]$SchedulerModule,
    [string]$SchedulerEntrypoint = 'run_match_03'
)

$ErrorActionPreference = 'Stop'

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Blender = Join-Path $ProjectRoot '.test_runtime\blender-5.0.0\blender-5.0.0-windows-x64\blender.exe'
$Fixture = 'C:\Users\linhp\Downloads\cc.blend'
$Harness = Join-Path $ProjectRoot 'tests\blender\match_03_fixture.py'
$BenchmarkRoot = Join-Path $ProjectRoot 'benchmarks'
$VersionOutput = Join-Path $BenchmarkRoot 'match_03_blender_version.txt'
$StdoutOutput = Join-Path $BenchmarkRoot 'match_03_blender_stdout.log'
$ExpectedFixtureSha = '840EA32C822784201EFAB30B9441A98621E6FBD87DC9BDD431B7EB90A2FF93CD'

if (-not $SchedulerModule.Trim()) {
    throw 'MATCH-03 scheduler adapter module is required after the C14 primary handoff'
}
if (-not $SchedulerEntrypoint.Trim()) {
    throw 'MATCH-03 scheduler adapter entrypoint must be non-empty'
}
if (-not (Test-Path -LiteralPath $Blender -PathType Leaf)) {
    throw "MATCH-03 portable Blender missing: $Blender"
}
if (-not (Test-Path -LiteralPath $Fixture -PathType Leaf)) {
    throw "MATCH-03 exact fixture missing: $Fixture"
}
if (-not (Test-Path -LiteralPath $Harness -PathType Leaf)) {
    throw "MATCH-03 exact-fixture harness missing: $Harness"
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
    $FixtureShaBefore,
    '--scheduler-module',
    $SchedulerModule,
    '--scheduler-entrypoint',
    $SchedulerEntrypoint
)

& $Blender @Arguments 2>&1 | Tee-Object -FilePath $StdoutOutput
$BlenderExit = $LASTEXITCODE

# Read-only orphan check.  Never terminate a process here: a user Blender 5.2
# process is outside scope.  Only the exact portable MATCH-03 executable path
# is considered a child process of this runner.
Start-Sleep -Milliseconds 250
$PortableBlenderPath = (Resolve-Path -LiteralPath $Blender).Path
$PortableProcesses = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    if (-not $_.ExecutablePath) {
        return $false
    }
    $ResolvedExecutablePath = Resolve-Path -LiteralPath $_.ExecutablePath -ErrorAction SilentlyContinue
    return $ResolvedExecutablePath -and $ResolvedExecutablePath.Path -eq $PortableBlenderPath
})
if ($PortableProcesses.Count -gt 0) {
    throw "Portable Blender process remains after MATCH-03: $($PortableProcesses.ProcessId -join ',')"
}

$FixtureShaAfter = (Get-FileHash -Algorithm SHA256 -LiteralPath $Fixture).Hash.ToUpperInvariant()
if ($FixtureShaAfter -ne $FixtureShaBefore) {
    throw "Fixture SHA changed after Blender run: before=$FixtureShaBefore after=$FixtureShaAfter"
}
if ($BlenderExit -ne 0) {
    throw "MATCH-03 Blender harness failed with exit code $BlenderExit"
}

Write-Output "MATCH-03 runner completed: fixture SHA unchanged $FixtureShaAfter"
