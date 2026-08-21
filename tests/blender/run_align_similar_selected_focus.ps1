param(
    [string]$PackagePath = ''
)

$ErrorActionPreference = 'Stop'

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Blender = Join-Path $ProjectRoot '.test_runtime\blender-5.0.0\blender-5.0.0-windows-x64\blender.exe'
$Fixture = Join-Path $ProjectRoot '.test_runtime\as_02_focus.blend'
$FixtureCreator = Join-Path $ProjectRoot 'tests\blender\create_align_similar_selected_focus_fixture.py'
$Harness = Join-Path $ProjectRoot 'tests\blender\align_similar_selected_focus.py'
$BenchmarkRoot = Join-Path $ProjectRoot 'benchmarks'
$Package = if ($PackagePath) { (Resolve-Path -LiteralPath $PackagePath).Path } else { '' }
$Suffix = if ($Package) { '_package' } else { '' }
$AggregateResult = Join-Path $BenchmarkRoot ("as_02_focus_four_way{0}.json" -f $Suffix)

if (-not (Test-Path -LiteralPath $Blender -PathType Leaf)) { throw "Focused Blender missing: $Blender" }
if (-not (Test-Path -LiteralPath $FixtureCreator -PathType Leaf)) { throw "Focused fixture creator missing: $FixtureCreator" }
if (-not (Test-Path -LiteralPath $Harness -PathType Leaf)) { throw "Focused harness missing: $Harness" }
if ($Package -and -not (Test-Path -LiteralPath $Package -PathType Leaf)) { throw "Focused package missing: $Package" }

New-Item -ItemType Directory -Force -Path $BenchmarkRoot | Out-Null
$env:PYTHONDONTWRITEBYTECODE = '1'
if (-not (Test-Path -LiteralPath $Fixture -PathType Leaf)) {
    & $Blender --factory-startup --disable-autoexec --background --python $FixtureCreator -- --output $Fixture
    if ($LASTEXITCODE -ne 0) { throw "Focused fixture creation failed with exit code $LASTEXITCODE" }
}

$FixtureShaBefore = (Get-FileHash -Algorithm SHA256 -LiteralPath $Fixture).Hash.ToUpperInvariant()
$Configurations = @(
    [pscustomobject]@{ Name = 'scale_on_flip_on'; MatchScale = 'true'; AllowFlipping = 'true' },
    [pscustomobject]@{ Name = 'scale_on_flip_off'; MatchScale = 'true'; AllowFlipping = 'false' },
    [pscustomobject]@{ Name = 'scale_off_flip_on'; MatchScale = 'false'; AllowFlipping = 'true' },
    [pscustomobject]@{ Name = 'scale_off_flip_off'; MatchScale = 'false'; AllowFlipping = 'false' }
)

$Summaries = @()
foreach ($Configuration in $Configurations) {
    $Result = Join-Path $BenchmarkRoot ("as_02_focus_{0}{1}.json" -f $Configuration.Name, $Suffix)
    $Stdout = Join-Path $BenchmarkRoot ("as_02_focus_{0}{1}_stdout.log" -f $Configuration.Name, $Suffix)
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
    if ($Package) { $Arguments += @('--package-zip', $Package) }

    Write-Output ("AS-02 focused {0}: Blender 5.0 warmup + 3 measured{1}" -f $Configuration.Name, $(if ($Package) { ' package' } else { '' }))
    & $Blender @Arguments 2>&1 | Tee-Object -FilePath $Stdout
    $BlenderExit = $LASTEXITCODE

    Start-Sleep -Milliseconds 250
    $PortablePath = (Resolve-Path -LiteralPath $Blender).Path
    $PortableProcesses = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.ExecutablePath -and ((Resolve-Path -LiteralPath $_.ExecutablePath -ErrorAction SilentlyContinue).Path -eq $PortablePath)
    })
    if ($PortableProcesses.Count -gt 0) { throw "Focused portable Blender process remains: $($PortableProcesses.ProcessId -join ',')" }

    $FixtureShaAfter = (Get-FileHash -Algorithm SHA256 -LiteralPath $Fixture).Hash.ToUpperInvariant()
    if ($FixtureShaAfter -ne $FixtureShaBefore) { throw "Focused fixture SHA changed: before=$FixtureShaBefore after=$FixtureShaAfter" }
    if ($BlenderExit -ne 0) { throw "Focused $($Configuration.Name) failed with exit code $BlenderExit" }
    if (-not (Test-Path -LiteralPath $Result -PathType Leaf)) { throw "Focused result missing: $Result" }
    $Evidence = Get-Content -Raw -LiteralPath $Result | ConvertFrom-Json
    if ($Evidence.status -ne 'passed') { throw "Focused evidence status is not passed: $($Evidence.status)" }
    if (@($Evidence.measured_runs)[0] -lt 3) { throw "Focused result has fewer than 3 measured runs" }
    $Measured = @($Evidence.runs | Where-Object { $_.run_kind -eq 'measured' })
    if ($Measured.Count -lt 3) { throw "Focused result runs have fewer than 3 measured entries" }
    $First = $Measured[0]
    $Summaries += [pscustomobject]@{
        configuration = $Configuration.Name
        match_scale = ($Configuration.MatchScale -eq 'true')
        allow_flipping = ($Configuration.AllowFlipping -eq 'true')
        aligned = [int]$First.aligned_count
        groups = [int]$First.group_count
        full_fits = [int]$First.full_fits
        timing = $Evidence.timing
        result = $Result
    }
    Write-Output ("AS-02 focused {0}: aligned={1} groups={2} full_fits={3} timing={4}" -f $Configuration.Name, $First.aligned_count, $First.group_count, $First.full_fits, ($Evidence.timing.runs_ms -join '/'))
}

$Signatures = @($Summaries | ForEach-Object { "{0}:{1}:{2}" -f $_.aligned, $_.groups, $_.full_fits } | Sort-Object -Unique)
$Aggregate = [ordered]@{
    status = 'passed'
    packet = 'AS-02'
    schema = 'as-02-focused-four-way-v1'
    fixture = $Fixture
    fixture_sha256_before = $FixtureShaBefore
    fixture_sha256_after = (Get-FileHash -Algorithm SHA256 -LiteralPath $Fixture).Hash.ToUpperInvariant()
    package_mode = [bool]$Package
    package_path = if ($Package) { $Package } else { $null }
    measured_runs_per_configuration = 3
    distinct_operator_signatures = $Signatures.Count
    configurations = $Summaries
}
$Aggregate | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $AggregateResult -Encoding UTF8
Write-Output "AS-02 focused four-way completed: fixture SHA unchanged $($Aggregate.fixture_sha256_after)"
Write-Output "AS-02 focused aggregate result: $AggregateResult"
