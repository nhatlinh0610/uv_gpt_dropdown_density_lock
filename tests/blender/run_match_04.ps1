param(
    [string]$ProjectRoot = '',
    [string]$Package = ''
)

$ErrorActionPreference = 'Stop'

if (-not $ProjectRoot) {
    $ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
} else {
    $ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
}

$Blender = Join-Path $ProjectRoot '.test_runtime\blender-5.0.0\blender-5.0.0-windows-x64\blender.exe'
$Fixture = 'C:\Users\linhp\Downloads\cc.blend'
$Harness = Join-Path $ProjectRoot 'tests\blender\match_04_package_smoke.py'
$BenchmarkRoot = Join-Path $ProjectRoot 'benchmarks'
$Result = Join-Path $BenchmarkRoot 'match_04_package_smoke.json'
$Stdout = Join-Path $BenchmarkRoot 'match_04_package_smoke.log'
$VersionOutput = Join-Path $BenchmarkRoot 'match_04_blender_version.txt'
$ExpectedFixtureSha = '840EA32C822784201EFAB30B9441A98621E6FBD87DC9BDD431B7EB90A2FF93CD'
$OldZip = Join-Path $ProjectRoot 'uv_gpt_v1.2.5.zip'
if (-not $Package) {
    $Package = Join-Path $ProjectRoot 'uv_gpt_v1.2.6.zip'
} else {
    $Package = (Resolve-Path -LiteralPath $Package).Path
}

foreach ($path in @($Blender, $Fixture, $Harness, $Package, $OldZip)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "MATCH-04 required file missing: $path"
    }
}

$fixtureBefore = (Get-FileHash -Algorithm SHA256 -LiteralPath $Fixture).Hash.ToUpperInvariant()
if ($fixtureBefore -ne $ExpectedFixtureSha) {
    throw "Fixture SHA mismatch before package smoke: $fixtureBefore"
}
$oldZipBefore = (Get-FileHash -Algorithm SHA256 -LiteralPath $OldZip).Hash.ToUpperInvariant()
$newZipBefore = (Get-FileHash -Algorithm SHA256 -LiteralPath $Package).Hash.ToUpperInvariant()

$zip = [System.IO.Compression.ZipFile]::OpenRead($Package)
try {
    $entries = @($zip.Entries | ForEach-Object { $_.FullName })
} finally {
    $zip.Dispose()
}
if ($entries.Count -ne 14 -or @($entries | Where-Object { $_ -notmatch '^uv_gpt/[^/]+\.py$' }).Count -ne 0) {
    throw "ZIP structure is not exactly one uv_gpt root with 14 Python files"
}

$sourceFiles = @(Get-ChildItem -LiteralPath (Join-Path $ProjectRoot 'uv_gpt') -Filter '*.py' -File | Sort-Object Name)
if ($sourceFiles.Count -ne 14) {
    throw "Expected 14 source Python files, found $($sourceFiles.Count)"
}

$extractParent = Join-Path $ProjectRoot '.test_runtime'
$extractRoot = Join-Path $extractParent ('match_04_package_extract_' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $extractRoot | Out-Null
Expand-Archive -LiteralPath $Package -DestinationPath $extractRoot -Force
$packageRoot = Join-Path $extractRoot 'uv_gpt'
if (-not (Test-Path -LiteralPath $packageRoot -PathType Container)) {
    throw "Extracted ZIP has no uv_gpt root: $packageRoot"
}

foreach ($source in $sourceFiles) {
    $relative = $source.Name
    $extracted = Join-Path $packageRoot $relative
    if (-not (Test-Path -LiteralPath $extracted -PathType Leaf)) {
        throw "Extracted package missing source file: $relative"
    }
    $sourceHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $source.FullName).Hash.ToUpperInvariant()
    $extractedHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $extracted).Hash.ToUpperInvariant()
    if ($sourceHash -ne $extractedHash) {
        throw "Extracted source byte mismatch: $relative"
    }
}

$env:PYTHONDONTWRITEBYTECODE = '1'
& $Blender --version | Tee-Object -FilePath $VersionOutput
if ($LASTEXITCODE -ne 0) {
    throw 'Blender --version failed'
}

$arguments = @(
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
    $fixtureBefore,
    '--package-root',
    $packageRoot,
    '--result',
    $Result
)

& $Blender @arguments 2>&1 | Tee-Object -FilePath $Stdout
$blenderExit = $LASTEXITCODE

Start-Sleep -Milliseconds 250
$portablePath = (Resolve-Path -LiteralPath $Blender).Path
$portableProcesses = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    if (-not $_.ExecutablePath) {
        return $false
    }
    $resolved = Resolve-Path -LiteralPath $_.ExecutablePath -ErrorAction SilentlyContinue
    return $resolved -and $resolved.Path -eq $portablePath
})
if ($portableProcesses.Count -gt 0) {
    throw "Portable Blender process remains after MATCH-04: $($portableProcesses.ProcessId -join ',')"
}
if ($blenderExit -ne 0) {
    throw "MATCH-04 package smoke failed with exit code $blenderExit"
}

$fixtureAfter = (Get-FileHash -Algorithm SHA256 -LiteralPath $Fixture).Hash.ToUpperInvariant()
$oldZipAfter = (Get-FileHash -Algorithm SHA256 -LiteralPath $OldZip).Hash.ToUpperInvariant()
$newZipAfter = (Get-FileHash -Algorithm SHA256 -LiteralPath $Package).Hash.ToUpperInvariant()
if ($fixtureAfter -ne $fixtureBefore) {
    throw "Fixture SHA changed: before=$fixtureBefore after=$fixtureAfter"
}
if ($oldZipAfter -ne $oldZipBefore) {
    throw "Existing v1.2.5 ZIP changed during package smoke"
}
if ($newZipAfter -ne $newZipBefore) {
    throw "v1.2.6 ZIP changed during package smoke"
}

$cacheFiles = @(Get-ChildItem -LiteralPath (Join-Path $ProjectRoot 'uv_gpt') -Recurse -File -Include '*.pyc', '*.pyo' -ErrorAction SilentlyContinue)
if ($cacheFiles.Count -gt 0) {
    throw "Workspace uv_gpt cache remains: $($cacheFiles.FullName -join ', ')"
}
$extractedCache = @(Get-ChildItem -LiteralPath $packageRoot -Recurse -File -Include '*.pyc', '*.pyo' -ErrorAction SilentlyContinue)
if ($extractedCache.Count -gt 0) {
    throw "Extracted package cache remains: $($extractedCache.FullName -join ', ')"
}

Write-Output "MATCH-04 package smoke completed: version 1.2.6, fixture SHA unchanged $fixtureAfter"
Write-Output "MATCH-04 extracted package: $packageRoot"
Write-Output "MATCH-04 result: $Result"
