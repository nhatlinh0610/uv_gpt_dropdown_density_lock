param(
    [string]$ProjectRoot
)

if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
}
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path

$Blender = Join-Path $ProjectRoot '.test_runtime\blender-5.0.0\blender-5.0.0-windows-x64\blender.exe'
$Fixture = 'C:\Users\linhp\Downloads\cc.blend'
$Script = Join-Path $ProjectRoot 'tests\blender\match_01_baseline.py'
$BenchmarkRoot = Join-Path $ProjectRoot 'benchmarks'
$VersionLog = Join-Path $BenchmarkRoot 'match_01_blender_version.txt'
$RunLog = Join-Path $BenchmarkRoot 'match_01_blender_stdout.log'

if (-not (Test-Path -LiteralPath $Blender -PathType Leaf)) {
    throw "Blender executable not found: $Blender"
}
if (-not (Test-Path -LiteralPath $Fixture -PathType Leaf)) {
    throw "Fixture not found: $Fixture"
}
if (-not (Test-Path -LiteralPath $Script -PathType Leaf)) {
    throw "Harness not found: $Script"
}
New-Item -ItemType Directory -Path $BenchmarkRoot -Force | Out-Null

$previousNoBytecode = $env:PYTHONDONTWRITEBYTECODE
$env:PYTHONDONTWRITEBYTECODE = '1'
try {
    $versionOutput = @(& $Blender --version 2>&1)
    $versionExit = $LASTEXITCODE
    $versionOutput | Set-Content -LiteralPath $VersionLog -Encoding UTF8
    if ($versionExit -ne 0) {
        throw "Blender --version failed with exit code $versionExit"
    }

    $runOutput = @(
        & $Blender --factory-startup --disable-autoexec --background $Fixture --python $Script -- --project-root $ProjectRoot --fixture $Fixture 2>&1
    )
    $runExit = $LASTEXITCODE
    $runOutput | Set-Content -LiteralPath $RunLog -Encoding UTF8
    $runOutput
    if ($runExit -ne 0) {
        throw "MATCH-01 Blender run failed with exit code $runExit"
    }
}
finally {
    if ($null -eq $previousNoBytecode) {
        Remove-Item Env:PYTHONDONTWRITEBYTECODE -ErrorAction SilentlyContinue
    } else {
        $env:PYTHONDONTWRITEBYTECODE = $previousNoBytecode
    }
}
