$ErrorActionPreference = 'Stop'

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Blender = Join-Path $ProjectRoot '.test_runtime\blender-5.0.0\blender-5.0.0-windows-x64\blender.exe'
$Fixture = 'C:\Users\linhp\Downloads\cc.blend'
$Harness = Join-Path $ProjectRoot 'tests\blender\align_similar_selected.py'
$Package = Join-Path $ProjectRoot 'uv_gpt_v1.2.6.zip'
$BenchmarkRoot = Join-Path $ProjectRoot 'benchmarks'
$Result = Join-Path $BenchmarkRoot 'as_02_package_smoke.json'
$StdoutOutput = Join-Path $BenchmarkRoot 'as_02_package_smoke_stdout.log'
$ExpectedFixtureSha = '76A72E7D0BB97E87D1EE5FABFFB9A57F6B175F9926AA98018AC3FD445D9BDD52'

if (-not (Test-Path -LiteralPath $Blender -PathType Leaf)) { throw "AS-02 package Blender missing: $Blender" }
if (-not (Test-Path -LiteralPath $Fixture -PathType Leaf)) { throw "AS-02 package fixture missing: $Fixture" }
if (-not (Test-Path -LiteralPath $Harness -PathType Leaf)) { throw "AS-02 package harness missing: $Harness" }
if (-not (Test-Path -LiteralPath $Package -PathType Leaf)) { throw "AS-02 package ZIP missing: $Package" }

New-Item -ItemType Directory -Force -Path $BenchmarkRoot | Out-Null
$FixtureShaBefore = (Get-FileHash -Algorithm SHA256 -LiteralPath $Fixture).Hash.ToUpperInvariant()
if ($FixtureShaBefore -ne $ExpectedFixtureSha) { throw "Fixture SHA mismatch before package smoke: $FixtureShaBefore (expected $ExpectedFixtureSha)" }

$env:PYTHONDONTWRITEBYTECODE = '1'
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
    '--match-scale',
    'true',
    '--allow-flipping',
    'true',
    '--package-zip',
    $Package
)

& $Blender @Arguments 2>&1 | Tee-Object -FilePath $StdoutOutput
$BlenderExit = $LASTEXITCODE

Start-Sleep -Milliseconds 250
$PortablePath = (Resolve-Path -LiteralPath $Blender).Path
$PortableProcesses = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.ExecutablePath -and ((Resolve-Path -LiteralPath $_.ExecutablePath -ErrorAction SilentlyContinue).Path -eq $PortablePath)
})
if ($PortableProcesses.Count -gt 0) { throw "Portable Blender process remains after AS-02 package smoke: $($PortableProcesses.ProcessId -join ',')" }

$FixtureShaAfter = (Get-FileHash -Algorithm SHA256 -LiteralPath $Fixture).Hash.ToUpperInvariant()
if ($FixtureShaAfter -ne $FixtureShaBefore) { throw "Fixture SHA changed after package smoke: before=$FixtureShaBefore after=$FixtureShaAfter" }
if ($BlenderExit -ne 0) { throw "AS-02 package Blender harness failed with exit code $BlenderExit" }
if (-not (Test-Path -LiteralPath $Result -PathType Leaf)) { throw "AS-02 package evidence missing: $Result" }
$Evidence = Get-Content -Raw -LiteralPath $Result | ConvertFrom-Json
if ($Evidence.status -ne 'passed') { throw "AS-02 package evidence status is not passed: $($Evidence.status)" }
if ($Evidence.package.mode -ne 'zip-import') { throw "AS-02 package harness did not use zip-import mode" }
if ($Evidence.package.loaded_from -notlike '*.zip*') { throw "AS-02 package was not loaded from ZIP: $($Evidence.package.loaded_from)" }

Write-Output "AS-02 package smoke completed: fixture SHA unchanged $FixtureShaAfter"
