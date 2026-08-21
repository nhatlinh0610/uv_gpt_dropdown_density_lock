param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('VERIFIED_NEAREST_ONLY','EXACT_ONLY')]
    [string]$Mode,
    [Parameter(Mandatory = $true)]
    [ValidateSet(1,4)]
    [int]$WorkerCount,
    [int]$MaxProcessSeconds = 600,
    [int]$SampleIntervalMs = 200
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Blender = (Resolve-Path (Join-Path $ProjectRoot '.test_runtime\blender-5.0.0\blender-5.0.0-windows-x64\blender.exe')).Path
$Helper = (Resolve-Path (Join-Path $ProjectRoot '.test_runtime\blender-5.0.0\blender-5.0.0-windows-x64\5.0\python\bin\python.exe')).Path
$Fixture = 'C:\Users\linhp\Downloads\cc.blend'
$Harness = (Resolve-Path (Join-Path $ProjectRoot 'tests\blender\profile_pro_process_cc_benchmark.py')).Path
$ExpectedSha = '3398B55425512AC0FCADCDFC8535B21847F209ADE0C25EDCA757B635D3DF145B'
$ExpectedLength = 3638416
$ExpectedSource = @{
    'uv_gpt\pro_process_payload.py' = '7E23D854010CBE786797BB83462FB4C4BC270494CE667371CF15CF470018DAC8'
    'uv_gpt\pro_process_shape.py' = '270634FF6E0D5E1C4E7AE89C29ADE29271480A3149704BDEFC3BDB28A97E55DE'
    'uv_gpt\pro_process_adapter.py' = 'FF0EAC1F4C262977835A21AC5C84B8D0C1C7F225368C3F2FE20C1D0F7A1BDEBC'
    'uv_gpt\pro_process_worker.py' = '2DEBAB9AFE855E22814D48D651B3C0AA9B5B082403196D22DB6AF04F8CFBF997'
    'uv_gpt\pro_process_pool.py' = 'AA39CCEF772A5207295A430DA73ED898E5600AF6259CCAE4FCDDFAEFE438F33C'
    'uv_gpt\pro_process_runtime.py' = '9FE0CE8D39A8642BCC7BAEB2B94BF89E08857FB5DC5434FFD9878B7D058F4D5F'
    'uv_gpt\pro_process_pipeline.py' = '2C3A366CD13AB84E7B6BFC325E5E0B4FE7D754222AF1444D7DAF6BED9284B28E'
    'uv_gpt\stack_tools.py' = '27F6061E082D8511B3643CD5CC7A34310691118E0B86071B41ED23D5F89827BA'
    'uv_gpt\ui.py' = 'C392D33B4962ED6EBD148F52507734913296F3967C7A30CED253FE755A3D939B'
}
$EvidencePath = Join-Path $ProjectRoot ('benchmarks\t2r4l_cc_{0}_{1}.json' -f $Mode.ToLowerInvariant(), $WorkerCount)
if (Test-Path -LiteralPath $EvidencePath) {
    throw "Refusing to overwrite existing benchmark evidence: $EvidencePath"
}
$Result = Join-Path ([IO.Path]::GetTempPath()) ('uv_gpt_t2r4l_' + $Mode.ToLowerInvariant() + '_' + $WorkerCount + '_' + [Guid]::NewGuid().ToString('N') + '.json')
$Stdout = [IO.Path]::ChangeExtension($Result, '.stdout.log')
$Stderr = [IO.Path]::ChangeExtension($Result, '.stderr.log')
$TempFiles = @($Result, $Stdout, $Stderr)
$process = $null
$samples = New-Object System.Collections.Generic.List[object]
$ownedRootPid = $null

function Get-ExactProcesses {
    $rows = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.ExecutablePath -and (($_.ExecutablePath -ieq $Blender) -or ($_.ExecutablePath -ieq $Helper))
    })
    $blenderRows = @($rows | Where-Object { $_.ExecutablePath -ieq $Blender })
    $helperRows = @($rows | Where-Object { $_.ExecutablePath -ieq $Helper })
    [pscustomobject]@{
        timestamp_utc = [DateTime]::UtcNow.ToString('o')
        blender_count = $blenderRows.Count
        blender_pids = @($blenderRows | ForEach-Object { [int]$_.ProcessId })
        blender_rss_bytes = [int64](($blenderRows | Measure-Object -Property WorkingSetSize -Sum).Sum)
        blender_cpu_100ns = [double](($blenderRows | ForEach-Object { [double]$_.UserModeTime + [double]$_.KernelModeTime } | Measure-Object -Sum).Sum)
        helper_count = $helperRows.Count
        helper_pids = @($helperRows | ForEach-Object { [int]$_.ProcessId })
        helper_rss_bytes = [int64](($helperRows | Measure-Object -Property WorkingSetSize -Sum).Sum)
        helper_cpu_100ns = [double](($helperRows | ForEach-Object { [double]$_.UserModeTime + [double]$_.KernelModeTime } | Measure-Object -Sum).Sum)
    }
}

function Assert-SourceSeals {
    param([string]$Phase)
    foreach ($relative in $ExpectedSource.Keys) {
        $path = Join-Path $ProjectRoot $relative
        $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.ToUpperInvariant()
        if ($actual -ne $ExpectedSource[$relative]) {
            throw "$Phase source seal mismatch: $relative = $actual"
        }
    }
}

function Get-Tail([string]$Path, [int]$Count = 120) {
    if (Test-Path -LiteralPath $Path) { return ((Get-Content -Tail $Count -LiteralPath $Path) -join "`n") }
    return ''
}

function Get-MaxValue {
    param([object[]]$Values)
    if (-not $Values -or $Values.Count -eq 0) { return 0.0 }
    return [double](($Values | Measure-Object -Maximum).Maximum)
}

function Get-UniqueInts {
    param([object[]]$Values)
    return @($Values | ForEach-Object { [int]$_ } | Sort-Object -Unique)
}

try {
    Assert-SourceSeals 'pre-row'
    $fixtureBefore = (Get-FileHash -Algorithm SHA256 -LiteralPath $Fixture).Hash.ToUpperInvariant()
    $fixtureLengthBefore = (Get-Item -LiteralPath $Fixture).Length
    if ($fixtureBefore -ne $ExpectedSha -or $fixtureLengthBefore -ne $ExpectedLength) {
        throw "cc fixture pre-row mismatch: sha=$fixtureBefore length=$fixtureLengthBefore"
    }
    $baseline = Get-ExactProcesses
    if ($baseline.blender_count -ne 0 -or $baseline.helper_count -ne 0) {
        throw "exact-path process baseline not clean: blender=$($baseline.blender_pids -join ',') helper=$($baseline.helper_pids -join ',')"
    }

    $args = @(
        '--factory-startup', '--disable-autoexec', '--background', ('"{0}"' -f $Fixture),
        '--python', ('"{0}"' -f $Harness), '--',
        '--project-root', ('"{0}"' -f $ProjectRoot),
        '--fixture', ('"{0}"' -f $Fixture),
        '--fixture-sha-before', $fixtureBefore,
        '--result', ('"{0}"' -f $Result),
        '--mode', $Mode,
        '--worker-count', $WorkerCount,
        '--batch-size', 32,
        '--time-budget-ms', 300000,
         '--run-id', ('t2r4l-{0}-{1}' -f $Mode.ToLowerInvariant(), $WorkerCount)
    )
    $watch = [Diagnostics.Stopwatch]::StartNew()
    $process = Start-Process -FilePath $Blender -ArgumentList $args -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $Stdout -RedirectStandardError $Stderr
    $ownedRootPid = $process.Id
    while (-not $process.HasExited) {
        $process.Refresh()
        $snap = Get-ExactProcesses
        $samples.Add($snap)
        if ($watch.Elapsed.TotalSeconds -gt $MaxProcessSeconds) {
            if ($ownedRootPid) { Stop-Process -Id $ownedRootPid -Force -ErrorAction SilentlyContinue }
            throw "T2R4L row exceeded $MaxProcessSeconds seconds`nSTDOUT:`n$(Get-Tail $Stdout)`nSTDERR:`n$(Get-Tail $Stderr)"
        }
        Start-Sleep -Milliseconds $SampleIntervalMs
    }
    $process.WaitForExit()
    $process.Refresh()
    $watch.Stop()
    $samples.Add((Get-ExactProcesses))
    $exitCode = $process.ExitCode
    $payload = $null
    if (Test-Path -LiteralPath $Result -PathType Leaf) {
        try { $payload = Get-Content -Raw -LiteralPath $Result | ConvertFrom-Json }
        catch { throw "invalid benchmark JSON: $($_.Exception.Message)`nSTDOUT:`n$(Get-Tail $Stdout)`nSTDERR:`n$(Get-Tail $Stderr)" }
    }

    $cleanupDeadline = (Get-Date).AddSeconds(10)
    do {
        $postRoot = Get-ExactProcesses
        if ($postRoot.helper_count -eq 0 -and $postRoot.blender_count -eq 0) { break }
        Start-Sleep -Milliseconds 100
    } while ((Get-Date) -lt $cleanupDeadline)
    $post = Get-ExactProcesses
    Assert-SourceSeals 'post-row'
    $fixtureAfter = (Get-FileHash -Algorithm SHA256 -LiteralPath $Fixture).Hash.ToUpperInvariant()
    $fixtureLengthAfter = (Get-Item -LiteralPath $Fixture).Length

    $sampleRows = @($samples.ToArray())
    $workerPids = Get-UniqueInts @($sampleRows | ForEach-Object { $_.helper_pids })
    $blenderPids = Get-UniqueInts @($sampleRows | ForEach-Object { $_.blender_pids })
    $peakWorkerRss = Get-MaxValue @($sampleRows | ForEach-Object { [double]$_.helper_rss_bytes })
    $peakBlenderRss = Get-MaxValue @($sampleRows | ForEach-Object { [double]$_.blender_rss_bytes })
    $peakTotalRss = Get-MaxValue @($sampleRows | ForEach-Object { [double]$_.helper_rss_bytes + [double]$_.blender_rss_bytes })
    $workerCpuSeconds = (Get-MaxValue @($sampleRows | ForEach-Object { [double]$_.helper_cpu_100ns })) / 10000000.0
    $blenderCpuSeconds = (Get-MaxValue @($sampleRows | ForEach-Object { [double]$_.blender_cpu_100ns })) / 10000000.0
    $run = if ($payload) { $payload.run } else { $null }
    $result = if ($run) { $run.result } else { $null }
    $contract = if ($run) { $run.benchmark_contract } else { $null }
    $tick = if ($run) { $run.tick_metrics } else { $null }
    $rowStatus = if ($payload) { [string]$payload.status } else { 'failed' }
    if ($null -eq $exitCode -and $rowStatus -eq 'passed') { $exitCode = 0 }

    $summary = [ordered]@{
         packet = 'T2R4L'
        status = $rowStatus
        exit_code = $exitCode
        mode = $Mode
        worker_count = $WorkerCount
        fixture = $Fixture
        fixture_sha256_before_runner = $fixtureBefore
        fixture_sha256_after_runner = $fixtureAfter
        fixture_length_before = $fixtureLengthBefore
        fixture_length_after = $fixtureLengthAfter
        fixture_sha256_in_process_before = if ($run) { $run.fixture_sha256_before_in_process } else { $null }
        fixture_sha256_in_process_after = if ($run) { $run.fixture_sha256_after_in_process } else { $null }
        target = if ($run) { $run.object } else { $null }
        uv_layer = if ($run) { $run.uv_map } else { $null }
        island_count = if ($run) { $run.island_count } else { $null }
        target_rule = if ($run) { $run.benchmark_target_rule } else { $null }
        selection_rule = if ($run) { $run.benchmark_selection_rule } else { $null }
        runner_wall_ms = $watch.Elapsed.TotalMilliseconds
        harness_wall_ms = if ($run) { $run.harness_wall_ms } else { $null }
        compute_timings_ms = if ($result) { $result.timings } else { $null }
        tick_metrics = $tick
        contract = $contract
        result_digest = if ($run) { $run.result_digest } else { $null }
        mapping_digest = if ($run) { $run.mapping_digest } else { $null }
        uv_digest = if ($run) { $run.uv_digest } else { $null }
        mapping_max_delta = if ($run) { $run.mapping_max_delta } else { $null }
        master_uv_delta = if ($run) { $run.master_uv_delta } else { $null }
        selection_unchanged = if ($run) { $run.selection_unchanged } else { $null }
        active_unchanged = if ($run) { $run.active_unchanged } else { $null }
        full_completion = if ($run) { $run.full_completion } else { $false }
        worker_mode = if ($result) { $result.worker_mode } else { $null }
        process_thread_caps = if ($result) { $result.process_thread_caps } else { $null }
        worker_pids = $workerPids
        blender_pids = $blenderPids
        unique_worker_process_count = $workerPids.Count
        peak_worker_rss_bytes = $peakWorkerRss
        peak_blender_rss_bytes = $peakBlenderRss
        peak_total_rss_bytes = $peakTotalRss
        worker_cpu_seconds_sampled = $workerCpuSeconds
        blender_cpu_seconds_sampled = $blenderCpuSeconds
        sampling_interval_ms = $SampleIntervalMs
        sample_count = $sampleRows.Count
        helper_orphan_count = $post.helper_count
        portable_blender_orphan_count = $post.blender_count
        source_seals = $ExpectedSource
        stdout_tail = Get-Tail $Stdout 40
        stderr_tail = Get-Tail $Stderr 40
    }
    $summary | ConvertTo-Json -Depth 40 | Set-Content -LiteralPath $EvidencePath -Encoding UTF8
     Write-Output ('T2R4L row: mode={0}; workers={1}; status={2}; exit={3}; target={4}/{5}; islands={6}; wall_ms={7}; max_tick_ms={8}; startup_max_tick_ms={9}; worker_mode={10}; unique_worker_pids={11}; peak_worker_rss={12}; worker_cpu_s={13}; orphan={14}; fixture_sha={15}; evidence={16}' -f
        $Mode, $WorkerCount, $rowStatus, $exitCode, $summary.target, $summary.uv_layer, $summary.island_count,
        $summary.runner_wall_ms, $summary.tick_metrics.max_tick_ms, $summary.tick_metrics.max_startup_tick_ms,
        $summary.worker_mode, ($workerPids -join ','), $peakWorkerRss, $workerCpuSeconds, $post.helper_count, $fixtureAfter, $EvidencePath)

    if ($fixtureAfter -ne $ExpectedSha -or $fixtureLengthAfter -ne $ExpectedLength) { throw "cc fixture changed after row: sha=$fixtureAfter length=$fixtureLengthAfter" }
    if ($post.helper_count -ne 0 -or $post.blender_count -ne 0) { throw "exact-path orphan remains: blender=$($post.blender_pids -join ',') helper=$($post.helper_pids -join ',')" }
     if ($exitCode -ne 0 -or $rowStatus -ne 'passed') { throw "T2R4L row failed; see evidence $EvidencePath`nSTDOUT:`n$(Get-Tail $Stdout)`nSTDERR:`n$(Get-Tail $Stderr)" }
     if (-not $contract.passed) { throw "T2R4L product contract failed; see evidence $EvidencePath" }
}
finally {
    foreach ($path in $TempFiles) {
        if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue }
    }
}
