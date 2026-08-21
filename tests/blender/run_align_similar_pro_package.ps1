$ErrorActionPreference = 'Stop'

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Blender = Join-Path $ProjectRoot '.test_runtime\blender-5.0.0\blender-5.0.0-windows-x64\blender.exe'
$Package = Join-Path $ProjectRoot 'uv_gpt_v1.2.6.zip'
$OldPackage = Join-Path $ProjectRoot 'uv_gpt_v1.2.5.zip'
$Harness = Join-Path $ProjectRoot 'tests\blender\align_similar_pro.py'
$Fixture = 'C:\Users\linhp\Downloads\cc.blend'
$DedicatedFixture = Join-Path $ProjectRoot 'benchmarks\pro_02b_dedicated_fixture.blend'
$BenchmarkRoot = Join-Path $ProjectRoot 'benchmarks'
$CcResult = Join-Path $BenchmarkRoot 'pro_02c_package_cc.json'
$DedicatedResult = Join-Path $BenchmarkRoot 'pro_02c_package_dedicated.json'
$CcStdout = Join-Path $BenchmarkRoot 'pro_02c_package_cc_stdout.log'
$DedicatedStdout = Join-Path $BenchmarkRoot 'pro_02c_package_dedicated_stdout.log'
$EvidenceResult = Join-Path $BenchmarkRoot 'pro_02c_package_smoke.json'
$ExpectedFixtureSha = '558CE02B2B36394A528290B38B7E5FE072B5853EAEB7EBAB71E515EDC6C5E905'
$ExpectedVersion = '1.2.6'

foreach ($path in @($Blender, $Package, $Harness, $Fixture, $DedicatedFixture)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "PRO-02C required file missing: $path"
    }
}
$OldPackagePresent = Test-Path -LiteralPath $OldPackage -PathType Leaf

New-Item -ItemType Directory -Force -Path $BenchmarkRoot | Out-Null
$FixtureShaBefore = (Get-FileHash -Algorithm SHA256 -LiteralPath $Fixture).Hash.ToUpperInvariant()
if ($FixtureShaBefore -ne $ExpectedFixtureSha) {
    throw "Locked fixture SHA mismatch: $FixtureShaBefore"
}
$DedicatedShaBefore = (Get-FileHash -Algorithm SHA256 -LiteralPath $DedicatedFixture).Hash.ToUpperInvariant()
$PackageShaBefore = (Get-FileHash -Algorithm SHA256 -LiteralPath $Package).Hash.ToUpperInvariant()
$OldPackageShaBefore = if ($OldPackagePresent) { (Get-FileHash -Algorithm SHA256 -LiteralPath $OldPackage).Hash.ToUpperInvariant() } else { $null }

$sourceFiles = @(Get-ChildItem -LiteralPath (Join-Path $ProjectRoot 'uv_gpt') -Filter '*.py' -File | Sort-Object Name)
if ($sourceFiles.Count -ne 16) {
    throw "Expected 16 current source Python files, found $($sourceFiles.Count)"
}

$zip = [System.IO.Compression.ZipFile]::OpenRead($Package)
try {
    $entries = @($zip.Entries | ForEach-Object { $_.FullName })
} finally {
    $zip.Dispose()
}
$expectedEntries = @($sourceFiles | ForEach-Object { 'uv_gpt/' + $_.Name })
if ($entries.Count -ne 16 -or (Compare-Object -ReferenceObject $expectedEntries -DifferenceObject $entries)) {
    throw "ZIP root/entry set is not exactly the current 16-file uv_gpt package"
}
if (@($entries | Where-Object { $_ -notmatch '^uv_gpt/[^/]+\.py$' }).Count -ne 0) {
    throw 'ZIP contains an unexpected path'
}

$extractParent = Join-Path $ProjectRoot '.test_runtime'
$extractRoot = Join-Path $extractParent ('pro_02c_package_extract_' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $extractRoot | Out-Null
Expand-Archive -LiteralPath $Package -DestinationPath $extractRoot -Force
$PackageRoot = Join-Path $extractRoot 'uv_gpt'
if (-not (Test-Path -LiteralPath $PackageRoot -PathType Container)) {
    throw "Extracted package root missing: $PackageRoot"
}

foreach ($source in $sourceFiles) {
    $extracted = Join-Path $PackageRoot $source.Name
    if (-not (Test-Path -LiteralPath $extracted -PathType Leaf)) {
        throw "Extracted package missing: $($source.Name)"
    }
    $sourceHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $source.FullName).Hash.ToUpperInvariant()
    $extractedHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $extracted).Hash.ToUpperInvariant()
    if ($sourceHash -ne $extractedHash) {
        throw "Extracted byte parity mismatch: $($source.Name)"
    }
}
$extractedFiles = @(Get-ChildItem -LiteralPath $PackageRoot -Recurse -File)
if ($extractedFiles.Count -ne 16 -or @($extractedFiles | Where-Object { $_.Extension -ne '.py' }).Count -ne 0) {
    throw 'Extracted package contains unexpected files or cache artifacts'
}

function Assert-NoPortableBlender {
    param([string]$PortablePath)
    $processes = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.ExecutablePath -and ((Resolve-Path -LiteralPath $_.ExecutablePath -ErrorAction SilentlyContinue).Path -eq $PortablePath)
    })
    if ($processes.Count -gt 0) {
        throw "Portable Blender process remains: $($processes.ProcessId -join ',')"
    }
}

$PortablePath = (Resolve-Path -LiteralPath $Blender).Path
$env:PYTHONDONTWRITEBYTECODE = '1'
$CcArguments = @(
    '--factory-startup', '--disable-autoexec', '--background', $Fixture,
    '--python', $Harness, '--',
    '--mode', 'cc', '--project-root', $ProjectRoot, '--fixture', $Fixture,
    '--fixture-sha-before', $FixtureShaBefore, '--package-root', $PackageRoot,
    '--result', $CcResult
)
& $Blender @CcArguments 2>&1 | Tee-Object -FilePath $CcStdout
$CcExit = $LASTEXITCODE
Start-Sleep -Milliseconds 250
Assert-NoPortableBlender $PortablePath
if ($CcExit -ne 0) { throw "Packaged cc smoke failed with exit code $CcExit" }

$DedicatedArguments = @(
    '--factory-startup', '--disable-autoexec', '--background', $DedicatedFixture,
    '--python', $Harness, '--',
    '--mode', 'dedicated', '--project-root', $ProjectRoot,
    '--fixture', $DedicatedFixture, '--package-root', $PackageRoot,
    '--result', $DedicatedResult
)
& $Blender @DedicatedArguments 2>&1 | Tee-Object -FilePath $DedicatedStdout
$DedicatedExit = $LASTEXITCODE
Start-Sleep -Milliseconds 250
Assert-NoPortableBlender $PortablePath
if ($DedicatedExit -ne 0) { throw "Packaged dedicated smoke failed with exit code $DedicatedExit" }

$FixtureShaAfter = (Get-FileHash -Algorithm SHA256 -LiteralPath $Fixture).Hash.ToUpperInvariant()
$DedicatedShaAfter = (Get-FileHash -Algorithm SHA256 -LiteralPath $DedicatedFixture).Hash.ToUpperInvariant()
$PackageShaAfter = (Get-FileHash -Algorithm SHA256 -LiteralPath $Package).Hash.ToUpperInvariant()
$OldPackageShaAfter = if ($OldPackagePresent) { (Get-FileHash -Algorithm SHA256 -LiteralPath $OldPackage).Hash.ToUpperInvariant() } else { $null }
if ($FixtureShaAfter -ne $FixtureShaBefore) { throw "Locked fixture changed: $FixtureShaBefore -> $FixtureShaAfter" }
if ($DedicatedShaAfter -ne $DedicatedShaBefore) { throw "Dedicated fixture changed: $DedicatedShaBefore -> $DedicatedShaAfter" }
if ($PackageShaAfter -ne $PackageShaBefore) { throw 'Release ZIP changed during package smoke' }
if ($OldPackagePresent -and $OldPackageShaAfter -ne $OldPackageShaBefore) { throw 'v1.2.5 ZIP changed during package smoke' }

if (-not (Test-Path -LiteralPath $CcResult -PathType Leaf)) { throw "Missing cc package evidence: $CcResult" }
if (-not (Test-Path -LiteralPath $DedicatedResult -PathType Leaf)) { throw "Missing dedicated package evidence: $DedicatedResult" }
$CcEvidence = Get-Content -Raw -LiteralPath $CcResult | ConvertFrom-Json
$DedicatedEvidence = Get-Content -Raw -LiteralPath $DedicatedResult | ConvertFrom-Json

foreach ($evidence in @($CcEvidence, $DedicatedEvidence)) {
    if ($evidence.status -ne 'passed') { throw "Package evidence is not passed: $($evidence.status)" }
    if ($evidence.package.mode -ne 'zip-import') { throw 'Package smoke did not report zip-import mode' }
    $expectedInit = (Resolve-Path -LiteralPath (Join-Path $PackageRoot '__init__.py')).Path
    $loadedPath = (Resolve-Path -LiteralPath $evidence.package.loaded_from).Path
    if ($loadedPath -ne $expectedInit) { throw "Wrong imported add-on path: $loadedPath" }
    if (($evidence.package.version -join '.') -ne $ExpectedVersion) { throw "Package smoke version mismatch: $($evidence.package.version -join '.')" }
    if (@($evidence.package.operator_ids) -notcontains 'uv_gpt.align_to_selected') { throw 'Packaged current Align Similar operator is missing' }
    if (@($evidence.package.operator_ids) -notcontains 'uv_gpt.align_similar_pro_fast') { throw 'Packaged Fast Align Similar Pro operator is missing' }
    if (@($evidence.package.operator_ids) -notcontains 'uv_gpt.align_similar_pro_exact') { throw 'Packaged Exact Align Similar Pro operator is missing' }
    $legacyProId = 'uv_gpt.' + 'align_similar_' + 'pro'
    if (@($evidence.package.operator_ids) -contains $legacyProId) { throw 'Packaged legacy composite Align Similar Pro operator is still present' }
}

if ($CcEvidence.island_count -ne 577 -or $CcEvidence.measured_runs -ne 3) {
    throw 'Packaged cc evidence has wrong island or measured-run count'
}
$measured = @($CcEvidence.runs | Where-Object { $_.run_kind -eq 'measured' })
if ($measured.Count -ne 3) { throw "Expected 3 packaged cc measured runs, got $($measured.Count)" }
if (@($measured | Where-Object { $_.result.aligned_exact -ne 1 -or $_.result.group_count -ne 1 }).Count -ne 0) {
    throw 'Packaged cc exact/group result mismatch'
}
if (@($measured | Where-Object { -not $_.selection_unchanged -or -not $_.active_unchanged -or $_.mapping_max_delta -gt 1.0e-7 -or $_.master_delta -gt 1.0e-7 -or $_.unselected_delta -gt 1.0e-7 }).Count -ne 0) {
    throw 'Packaged cc safety delta mismatch'
}
$ccGroup = $measured[0].result.groups[0]
if (($ccGroup.master_key -join ',') -ne '9448,9484,9967,17967') { throw "Packaged cc master mismatch: $($ccGroup.master_key -join ',')" }
if (($ccGroup.member_keys[0] -join ',') -ne '602,603,604,605') { throw 'Packaged cc member mismatch' }
if ($ccGroup.mapping_pairs[0].Count -ne 16) { throw "Packaged cc mapping count mismatch: $($ccGroup.mapping_pairs[0].Count)" }

if ($DedicatedEvidence.cases.Count -ne 6) { throw "Expected 6 packaged dedicated cases, got $($DedicatedEvidence.cases.Count)" }
$expectedCases = @{
    'PROExact|False' = 2
    'PROExact|True' = 3
    'PROHole|False' = 1
    'PROInterior|False' = 1
    'PROSeam|False' = 1
    'PRONonIso|False' = 0
}
foreach ($case in $DedicatedEvidence.cases) {
    $key = '{0}|{1}' -f $case.object, $case.allow_flipping
    if (-not $expectedCases.ContainsKey($key)) { throw "Unexpected dedicated case: $key" }
    if ($case.result.aligned_exact -ne $expectedCases[$key]) { throw "Dedicated exact mismatch for $key" }
    if ($case.mapping_max_delta -gt 1.0e-7 -or $case.master_delta -gt 1.0e-7 -or $case.unselected_delta -gt 1.0e-7 -or $case.duplicate_targets -ne 0) {
        throw "Dedicated safety mismatch for $key"
    }
    if ($case.result.aligned_exact -eq 0 -and $case.uv_changed -gt 1.0e-7) { throw "Rejected dedicated case wrote UV: $key" }
}

$summary = [ordered]@{
    status = 'passed'
    packet = 'PRO-02C/package-smoke'
    package = [ordered]@{ path = $Package; size = (Get-Item -LiteralPath $Package).Length; sha256 = $PackageShaAfter; entry_count = 16; byte_parity = 'passed'; version = $ExpectedVersion; imported_from = (Resolve-Path -LiteralPath (Join-Path $PackageRoot '__init__.py')).Path }
    cc = [ordered]@{ result = $CcResult; fixture_sha_before = $FixtureShaBefore; fixture_sha_after = $FixtureShaAfter; measured_elapsed_ms = @($CcEvidence.measured_elapsed_ms); master = @($ccGroup.master_key); mapping_loop_count = $ccGroup.mapping_pairs[0].Count; safety_deltas_zero = $true }
    dedicated = [ordered]@{ result = $DedicatedResult; fixture_sha_before = $DedicatedShaBefore; fixture_sha_after = $DedicatedShaAfter; cases = $DedicatedEvidence.cases.Count; safety_deltas_zero = $true }
    portable_blender_processes = 0
}
$summary | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $EvidenceResult -Encoding UTF8
Write-Output ("PRO-02C package smoke passed: ZIP={0}; size={1}; SHA={2}; cc_master={3}; dedicated_cases={4}" -f $Package, (Get-Item -LiteralPath $Package).Length, $PackageShaAfter, ($ccGroup.master_key -join ','), $DedicatedEvidence.cases.Count)
Write-Output "PRO-02C evidence: $EvidenceResult"
