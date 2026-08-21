$ErrorActionPreference = 'Stop'

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Blender = Join-Path $ProjectRoot '.test_runtime\blender-5.0.0\blender-5.0.0-windows-x64\blender.exe'
$Fixture = 'C:\Users\linhp\Downloads\cc.blend'
$Harness = Join-Path $ProjectRoot 'tests\blender\align_similar_selected.py'
$BenchmarkRoot = Join-Path $ProjectRoot 'benchmarks'
$AggregateResult = Join-Path $BenchmarkRoot 'as_02_align_similar_selected.json'
$VersionOutput = Join-Path $BenchmarkRoot 'as_02_blender_version.txt'
$ExpectedFixtureSha = '76A72E7D0BB97E87D1EE5FABFFB9A57F6B175F9926AA98018AC3FD445D9BDD52'

if (-not (Test-Path -LiteralPath $Blender -PathType Leaf)) {
    throw "AS-02 portable Blender missing: $Blender"
}
if (-not (Test-Path -LiteralPath $Fixture -PathType Leaf)) {
    throw "AS-02 exact fixture missing: $Fixture"
}
if (-not (Test-Path -LiteralPath $Harness -PathType Leaf)) {
    throw "AS-02 harness missing: $Harness"
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

$Configurations = @(
    [pscustomobject]@{ Name = 'scale_on_flip_on'; MatchScale = 'true'; AllowFlipping = 'true' },
    [pscustomobject]@{ Name = 'scale_on_flip_off'; MatchScale = 'true'; AllowFlipping = 'false' },
    [pscustomobject]@{ Name = 'scale_off_flip_on'; MatchScale = 'false'; AllowFlipping = 'true' },
    [pscustomobject]@{ Name = 'scale_off_flip_off'; MatchScale = 'false'; AllowFlipping = 'false' }
)

$Summaries = @()
foreach ($Configuration in $Configurations) {
    $Result = Join-Path $BenchmarkRoot ("as_02_{0}.json" -f $Configuration.Name)
    $StdoutOutput = Join-Path $BenchmarkRoot ("as_02_{0}_stdout.log" -f $Configuration.Name)
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
        $Result,
        '--match-scale',
        $Configuration.MatchScale,
        '--allow-flipping',
        $Configuration.AllowFlipping
    )

    Write-Output ("AS-02 {0}: Blender 5.0 warmup + 3 measured" -f $Configuration.Name)
    & $Blender @Arguments 2>&1 | Tee-Object -FilePath $StdoutOutput
    $BlenderExit = $LASTEXITCODE

    Start-Sleep -Milliseconds 250
    $PortablePath = (Resolve-Path -LiteralPath $Blender).Path
    $PortableProcesses = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.ExecutablePath -and ((Resolve-Path -LiteralPath $_.ExecutablePath -ErrorAction SilentlyContinue).Path -eq $PortablePath)
    })
    if ($PortableProcesses.Count -gt 0) {
        throw "Portable Blender process remains after AS-02 $($Configuration.Name): $($PortableProcesses.ProcessId -join ',')"
    }

    $FixtureShaAfter = (Get-FileHash -Algorithm SHA256 -LiteralPath $Fixture).Hash.ToUpperInvariant()
    if ($FixtureShaAfter -ne $FixtureShaBefore) {
        throw "Fixture SHA changed after AS-02 $($Configuration.Name): before=$FixtureShaBefore after=$FixtureShaAfter"
    }
    if ($BlenderExit -ne 0) {
        throw "AS-02 $($Configuration.Name) Blender harness failed with exit code $BlenderExit"
    }
    if (-not (Test-Path -LiteralPath $Result -PathType Leaf)) {
        throw "AS-02 result JSON missing: $Result"
    }
    $Evidence = Get-Content -Raw -LiteralPath $Result | ConvertFrom-Json
    if ($Evidence.status -ne 'passed') {
        throw "AS-02 evidence status is not passed for $($Configuration.Name): $($Evidence.status)"
    }
    $Measured = @($Evidence.runs | Where-Object { $_.run_kind -eq 'measured' })
    if ($Measured.Count -lt 3) {
        throw "AS-02 $($Configuration.Name) has fewer than 3 measured runs"
    }
    $ExpectedScale = $Configuration.MatchScale -eq 'true'
    $ExpectedFlipping = $Configuration.AllowFlipping -eq 'true'
    foreach ($Run in $Measured) {
        if ([bool]$Run.settings.stack_match_scale -ne $ExpectedScale) {
            throw "AS-02 $($Configuration.Name) Match Scale override was not applied"
        }
        if ([bool]$Run.settings.stack_allow_flipping -ne $ExpectedFlipping) {
            throw "AS-02 $($Configuration.Name) Allow Flipping override was not applied"
        }
        if ([int]$Run.diagnostics.full_fits -le 0) {
            throw "AS-02 $($Configuration.Name) has no full fits in measured run $($Run.run_index)"
        }
    }

    $FirstMeasured = $Measured[0]
    $Summaries += [pscustomobject]@{
        configuration = $Configuration.Name
        match_scale = $ExpectedScale
        allow_flipping = $ExpectedFlipping
        aligned = [int]$FirstMeasured.aligned_count
        groups = [int]$FirstMeasured.group_count
        full_fits = [int]$FirstMeasured.diagnostics.full_fits
        timing = $Evidence.timing
        result = $Result
    }
    Write-Output ("AS-02 {0}: aligned={1} groups={2} full_fits={3} timing={4}" -f $Configuration.Name, $FirstMeasured.aligned_count, $FirstMeasured.group_count, $FirstMeasured.diagnostics.full_fits, ($Evidence.timing.runs_ms -join '/'))
}

$Signatures = @($Summaries | ForEach-Object { "{0}:{1}:{2}" -f $_.aligned, $_.groups, $_.full_fits } | Sort-Object -Unique)
$Aggregate = [ordered]@{
    status = 'passed'
    packet = 'AS-02'
    schema = 'as-02-align-similar-selected-four-way-v1'
    fixture_sha256_before = $FixtureShaBefore
    fixture_sha256_after = (Get-FileHash -Algorithm SHA256 -LiteralPath $Fixture).Hash.ToUpperInvariant()
    measured_runs_per_configuration = 3
    distinct_operator_signatures = $Signatures.Count
    configurations = $Summaries
}
$Aggregate | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $AggregateResult -Encoding UTF8

Write-Output "AS-02 four-way runner completed: fixture SHA unchanged $($Aggregate.fixture_sha256_after)"
Write-Output "AS-02 aggregate result: $AggregateResult"
