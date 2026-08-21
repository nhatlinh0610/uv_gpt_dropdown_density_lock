param(
    [ValidateSet('All', 'Phase0', 'Pilot', 'Matrix', 'Phase3', 'Single')]
    [string]$Mode = 'All',
    [string]$ExpectedSha = '49A329EFA1DDA72C4BEB040786590F8B0946BB737266C0498DC6A828C941EEE6',
    [int]$MaxProcessSeconds = 240,
    [double]$SessionTimeBudgetMs = 180000,
    [ValidateSet('complete', 'diagnostic')]
    [string]$Scenario = 'complete',
    [switch]$ProcessFused,
    [int]$Phase0BatchSize = 64,
    [string]$Phase0RunId = 'phase0_w1_b64',
    [int]$SingleWorkerCount = 1,
    [int]$SingleBatchSize = 64,
    [string]$SingleRunId = 'mc4_single',
    [string]$SingleRunClass = 'single',
    [string]$SingleSupersedesRunId = ''
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Blender = (Resolve-Path (Join-Path $ProjectRoot '.test_runtime\blender-5.0.0\blender-5.0.0-windows-x64\blender.exe')).Path
$Helper = (Resolve-Path (Join-Path $ProjectRoot '.test_runtime\blender-5.0.0\blender-5.0.0-windows-x64\5.0\python\bin\python.exe')).Path
$Fixture = 'C:\Users\linhp\Downloads\cc.blend'
$Harness = (Resolve-Path (Join-Path $ProjectRoot 'tests\blender\profile_pro_process_mc4.py')).Path
$MatrixPath = Join-Path $ProjectRoot 'benchmarks\pro_mc4_matrix.json'
$SummaryPath = Join-Path $ProjectRoot 'benchmarks\pro_mc4_summary.json'
$TempRoot = Join-Path ([IO.Path]::GetTempPath()) ('uvgpt_mc4_' + [guid]::NewGuid().ToString('N'))
$null = New-Item -ItemType Directory -Path $TempRoot -Force
$Runs = [System.Collections.Generic.List[object]]::new()

function Get-Canonical([string]$PathValue) {
    if (-not $PathValue) { return '' }
    try { return (Resolve-Path -LiteralPath $PathValue -ErrorAction Stop).Path } catch { return '' }
}

function Get-ExactPids([string]$ExecutablePath) {
    $canonical = Get-Canonical $ExecutablePath
    if (-not $canonical) { return @() }
    $items = @()
    foreach ($item in @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)) {
        if (-not $item.ExecutablePath) { continue }
        if ((Get-Canonical $item.ExecutablePath) -eq $canonical) {
            $items += [int]$item.ProcessId
        }
    }
    return @($items | Sort-Object -Unique)
}

function Get-InteractiveBlenderSnapshot {
    $items = @()
    foreach ($item in @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)) {
        if (-not $item.ExecutablePath) { continue }
        $path = [string]$item.ExecutablePath
        if ($path -notlike '*BlenderFoundation.Blender*5.2*blender.exe') { continue }
        $items += [ordered]@{
            pid = [int]$item.ProcessId
            executable = $path
            creation_date = [string]$item.CreationDate
        }
    }
    return @($items)
}

function Get-SourceHashes {
    $names = @(
        'uv_gpt\__init__.py',
        'uv_gpt\stack_tools.py',
        'uv_gpt\pro_process_protocol.py',
        'uv_gpt\pro_process_payload.py',
        'uv_gpt\pro_process_worker.py',
        'uv_gpt\pro_process_runtime.py',
        'uv_gpt\pro_process_pool.py',
        'uv_gpt\pro_process_adapter.py',
        'uv_gpt\pro_process_shape.py',
        'uv_gpt\pro_process_pipeline.py'
    )
    $hashes = [ordered]@{}
    foreach ($name in $names) {
        $path = Join-Path $ProjectRoot $name
        $hashes[$name.Replace('\', '/')] = (Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.ToUpperInvariant()
    }
    return $hashes
}

function Get-ProcessCpuSeconds([int]$PidValue) {
    try {
        $process = Get-Process -Id $PidValue -ErrorAction Stop
        return [double]$process.CPU
    } catch {
        return 0.0
    }
}

function Get-ProcessWorkingSet([int]$PidValue) {
    try {
        $process = Get-Process -Id $PidValue -ErrorAction Stop
        return [int64]$process.WorkingSet64
    } catch {
        return [int64]0
    }
}

function Get-Tail([string]$PathValue, [int]$Count = 100) {
    if (-not (Test-Path -LiteralPath $PathValue -PathType Leaf)) { return '' }
    return ((Get-Content -LiteralPath $PathValue -Tail $Count -ErrorAction SilentlyContinue) -join "`n")
}

function New-FailureRecord(
    [string]$RunId,
    [string]$RunClass,
    [int]$WorkerCount,
    [int]$BatchSize,
    [string]$ErrorMessage,
    [int]$ExitCode,
    [int]$ParentPid,
    [int64]$ParentPeakWorkingSetBytes,
    [double]$ParentCpuSeconds,
    [object[]]$HelperPids,
    [int]$OrphanCount,
    [string]$StdoutTail,
    [string]$StderrTail,
    [string]$FixtureShaBefore,
    [string]$FixtureShaAfter
) {
    return [pscustomobject][ordered]@{
        status = 'failed'
        run_id = $RunId
        run_class = $RunClass
        worker_count = $WorkerCount
        batch_size = $BatchSize
        error = $ErrorMessage
        exit_code = $ExitCode
        blender_pid = $ParentPid
        parent_peak_working_set_bytes = $ParentPeakWorkingSetBytes
        parent_cpu_seconds = $ParentCpuSeconds
        worker_pids = @($HelperPids)
        worker_peak_working_set_bytes = [ordered]@{}
        worker_cpu_seconds = [ordered]@{}
        aggregate_worker_cpu_seconds = 0.0
        orphan_count = $OrphanCount
        stdout_tail = $StdoutTail
        stderr_tail = $StderrTail
        fixture_sha256_before_runner = $FixtureShaBefore
        fixture_sha256_after_runner = $FixtureShaAfter
        evidence = $null
    }
}

function Invoke-Mc4Run {
    param(
        [int]$WorkerCount,
        [int]$BatchSize,
    [string]$RunClass,
    [string]$RunId,
    [double]$TimeBudgetMs = $SessionTimeBudgetMs,
    [string]$SupersedesRunId = '',
    [bool]$Fused = $false
)
    $fixtureBefore = (Get-FileHash -Algorithm SHA256 -LiteralPath $Fixture).Hash.ToUpperInvariant()
    if ($fixtureBefore -ne $ExpectedSha) { throw "MC4 fixture SHA mismatch before ${RunId}: $fixtureBefore" }
    $portableBefore = @(Get-ExactPids $Blender)
    $helperBefore = @(Get-ExactPids $Helper)
    if ($portableBefore.Count -ne 0 -or $helperBefore.Count -ne 0) {
        throw "MC4 process baseline is not clean: portable=$($portableBefore -join ',') helper=$($helperBefore -join ',')"
    }
    $runDir = Join-Path $TempRoot $RunId
    $null = New-Item -ItemType Directory -Path $runDir -Force
    $resultPath = Join-Path $runDir 'result.json'
    $stdoutPath = Join-Path $runDir 'stdout.txt'
    $stderrPath = Join-Path $runDir 'stderr.txt'
    $args = @(
        '--factory-startup', '--disable-autoexec', '--background', ('"{0}"' -f $Fixture),
        '--python', ('"{0}"' -f $Harness), '--',
        '--project-root', ('"{0}"' -f $ProjectRoot),
        '--fixture', ('"{0}"' -f $Fixture),
        '--fixture-sha-before', $fixtureBefore,
        '--result', ('"{0}"' -f $resultPath),
        '--worker-count', [string]$WorkerCount,
        '--batch-size', [string]$BatchSize,
        '--run-class', $RunClass,
        '--run-id', $RunId,
        '--time-budget-ms', [string]$TimeBudgetMs,
        '--scenario', $Scenario
    )
    if ($Fused) { $args += @('--process-fused', '1') }
    if ($SupersedesRunId) {
        $args += @('--supersedes-run-id', $SupersedesRunId)
    }
    $process = Start-Process -FilePath $Blender -ArgumentList $args -WindowStyle Hidden `
        -PassThru -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath
    $ownedHelpers = [System.Collections.Generic.HashSet[int]]::new()
    $helperPeak = @{}
    $helperCpu = @{}
    $parentPeak = [int64]0
    $parentCpu = 0.0
    $timedOut = $false
    $deadline = (Get-Date).AddSeconds($MaxProcessSeconds)
    try {
        while (-not $process.HasExited) {
            $process.Refresh()
            $ws = Get-ProcessWorkingSet $process.Id
            if ($ws -gt $parentPeak) { $parentPeak = $ws }
            $parentCpu = [math]::Max($parentCpu, (Get-ProcessCpuSeconds $process.Id))
            foreach ($pidValue in @(Get-ExactPids $Helper)) {
                if (-not $helperBefore.Contains($pidValue)) { $null = $ownedHelpers.Add($pidValue) }
                $helperWs = Get-ProcessWorkingSet $pidValue
                if (-not $helperPeak.ContainsKey($pidValue) -or $helperWs -gt [int64]$helperPeak[$pidValue]) { $helperPeak[$pidValue] = $helperWs }
                $helperCpu[$pidValue] = [math]::Max([double]($helperCpu[$pidValue]), (Get-ProcessCpuSeconds $pidValue))
            }
            if ((Get-Date) -gt $deadline) {
                $cim = Get-CimInstance Win32_Process -Filter ("ProcessId={0}" -f $process.Id) -ErrorAction SilentlyContinue
                $resolved = if ($cim -and $cim.ExecutablePath) { Get-Canonical $cim.ExecutablePath } else { '' }
                if ($resolved -eq $Blender) { Stop-Process -Id $process.Id -Force }
                $timedOut = $true
                break
            }
            Start-Sleep -Milliseconds 250
        }
        $process.WaitForExit()
        $process.Refresh()
        $parentPeak = [math]::Max($parentPeak, (Get-ProcessWorkingSet $process.Id))
        $parentCpu = [math]::Max($parentCpu, (Get-ProcessCpuSeconds $process.Id))
    } finally {
        $process.Refresh()
    }
    $exitCode = $process.ExitCode
    Start-Sleep -Milliseconds 350
    $fixtureAfter = (Get-FileHash -Algorithm SHA256 -LiteralPath $Fixture).Hash.ToUpperInvariant()
    $portableOrphans = @(Get-ExactPids $Blender | Where-Object { -not $portableBefore.Contains($_) })
    $helperOrphans = @(Get-ExactPids $Helper | Where-Object { -not $helperBefore.Contains($_) })
    foreach ($pidValue in $helperOrphans) {
        if ($ownedHelpers.Contains([int]$pidValue)) {
            $resolvedHelper = Get-Canonical $Helper
            $cim = Get-CimInstance Win32_Process -Filter ("ProcessId={0}" -f $pidValue) -ErrorAction SilentlyContinue
            $actual = if ($cim -and $cim.ExecutablePath) { Get-Canonical $cim.ExecutablePath } else { '' }
            if ($actual -eq $resolvedHelper) { Stop-Process -Id $pidValue -Force }
        }
    }
    Start-Sleep -Milliseconds 250
    $helperOrphansAfterCleanup = @(Get-ExactPids $Helper | Where-Object { -not $helperBefore.Contains($_) })
    $orphanCount = $portableOrphans.Count + $helperOrphansAfterCleanup.Count
    $stdoutTail = Get-Tail $stdoutPath
    $stderrTail = Get-Tail $stderrPath
    $evidence = $null
    $errorMessage = $null
    if (Test-Path -LiteralPath $resultPath -PathType Leaf) {
        try { $evidence = Get-Content -Raw -LiteralPath $resultPath | ConvertFrom-Json } catch { $errorMessage = "result JSON parse failed: $($_.Exception.Message)" }
    } else {
        $errorMessage = 'result JSON missing'
    }
    if ($evidence -and $evidence.status -eq 'failed') {
        $detail = if ($evidence.error) { [string]$evidence.error } elseif ($evidence.run -and $evidence.run.error) { [string]$evidence.run.error } else { 'structured harness failure' }
        $errorMessage = "harness failure: $detail"
    }
    if ($null -eq $exitCode) {
        $passedEvidence = ($null -ne $evidence) -and ($evidence.status -eq 'passed') -and ($null -ne $evidence.run) -and ($evidence.run.fixture_sha256_after_in_process -eq $ExpectedSha)
        if ((-not $timedOut) -and $passedEvidence) {
            $exitCode = 0
            Write-Output "MC4 runner warning: portable ExitCode unavailable; durable passed evidence used for $RunId"
        } else {
            $exitCode = if ($timedOut) { 124 } else { 1 }
        }
    }
    if ($timedOut) { $errorMessage = "portable Blender exceeded $MaxProcessSeconds seconds" }
    if ($exitCode -ne 0 -and -not $errorMessage) { $errorMessage = "portable Blender exit code $exitCode" }
    if ($evidence -and $evidence.status -ne 'passed' -and -not $errorMessage) { $errorMessage = "harness status=$($evidence.status)" }
    if ($fixtureAfter -ne $fixtureBefore) { $errorMessage = "fixture SHA changed: before=$fixtureBefore after=$fixtureAfter" }
    if ($orphanCount -ne 0) { $errorMessage = "owned process orphan count=$orphanCount" }
    if ($evidence -and $evidence.run -and $evidence.run.fixture_sha256_after_in_process -and $evidence.run.fixture_sha256_after_in_process -ne $ExpectedSha) { $errorMessage = 'in-process fixture SHA guard failed' }
    $status = if ($errorMessage) { 'failed' } else { 'passed' }
    $record = if ($status -eq 'failed') {
        New-FailureRecord $RunId $RunClass $WorkerCount $BatchSize $errorMessage $exitCode $process.Id $parentPeak $parentCpu @($ownedHelpers) $orphanCount $stdoutTail $stderrTail $fixtureBefore $fixtureAfter
    } else {
        [pscustomobject][ordered]@{
            status = 'passed'
            run_id = $RunId
            run_class = $RunClass
            worker_count = $WorkerCount
            batch_size = $BatchSize
            exit_code = [int]$exitCode
            blender_pid = [int]$process.Id
            parent_peak_working_set_bytes = [int64]$parentPeak
            parent_cpu_seconds = [double]$parentCpu
            worker_pids = @($ownedHelpers | Sort-Object)
            worker_peak_working_set_bytes = [ordered]@{}
            worker_cpu_seconds = [ordered]@{}
            aggregate_worker_cpu_seconds = 0.0
            orphan_count = $orphanCount
            fixture_sha256_before_runner = $fixtureBefore
            fixture_sha256_after_runner = $fixtureAfter
            stdout_tail = $stdoutTail
            stderr_tail = $stderrTail
            evidence = $evidence
        }
    }
    if ($evidence) { $record.evidence = $evidence }
    if ($SupersedesRunId) {
        $record | Add-Member -NotePropertyName supersedes_run_id -NotePropertyValue $SupersedesRunId -Force
    }
    foreach ($pidValue in @($ownedHelpers)) {
        $record.worker_peak_working_set_bytes[[string]$pidValue] = if ($helperPeak.ContainsKey($pidValue)) { [int64]$helperPeak[$pidValue] } else { 0 }
        $record.worker_cpu_seconds[[string]$pidValue] = if ($helperCpu.ContainsKey($pidValue)) { [double]$helperCpu[$pidValue] } else { 0.0 }
        $record.aggregate_worker_cpu_seconds += [double]$record.worker_cpu_seconds[[string]$pidValue]
    }
    return $record
}

function Add-RunRecord([object]$Record) {
    $Runs.Add($Record)
    $compact = [ordered]@{
        packet = 'MC4'
        mode = $Mode
        updated_utc = (Get-Date).ToUniversalTime().ToString('o')
        fixture = $Fixture
        expected_fixture_sha256 = $ExpectedSha
        machine = $Machine
        baseline = $Baseline
        runs = @($Runs)
    }
    $compact | ConvertTo-Json -Depth 80 | Set-Content -LiteralPath $MatrixPath -Encoding UTF8
    Write-Output ("MC4 run {0}: status={1}; worker={2}; batch={3}; wall_ms={4}; error={5}" -f
        $Record.run_id, $Record.status, $Record.worker_count, $Record.batch_size,
        $(if ($Record.evidence -and $Record.evidence.run) { $Record.evidence.run.harness_wall_ms } else { '' }),
        $(if ($Record.error) { $Record.error } else { '' }))
}

function Invoke-RecordedRun(
    [int]$WorkerCount,
    [int]$BatchSize,
    [string]$RunClass,
    [string]$RunId,
    [string]$SupersedesRunId = '',
    [bool]$Fused = $false
) {
    $record = Invoke-Mc4Run -WorkerCount $WorkerCount -BatchSize $BatchSize -RunClass $RunClass -RunId $RunId -SupersedesRunId $SupersedesRunId -Fused $Fused
    Add-RunRecord $record
    return $record
}

function Get-PassingRuns([object[]]$Items) {
    return @($Items | Where-Object {
        $_.status -eq 'passed' -and $_.evidence -and $_.evidence.run -and
        $_.evidence.run.full_completion -eq $true -and
        $_.orphan_count -eq 0 -and
        [double]$_.evidence.run.result.max_tick_ms -le 250.0 -and
        $_.evidence.run.fixture_sha256_after_in_process -eq $ExpectedSha
    })
}

function Get-WallMs([object]$Record) {
    if ($Record.evidence -and $Record.evidence.run) { return [double]$Record.evidence.run.harness_wall_ms }
    return [double]::PositiveInfinity
}

function Get-Median([double[]]$Values) {
    $ordered = @($Values | Sort-Object)
    if ($ordered.Count -eq 0) { return [double]::PositiveInfinity }
    $middle = [int][math]::Floor($ordered.Count / 2)
    if (($ordered.Count % 2) -eq 1) { return [double]$ordered[$middle] }
    return ([double]$ordered[$middle - 1] + [double]$ordered[$middle]) / 2.0
}

function Write-Summary([int]$ChosenWorkers, [int]$ChosenBatch, [object]$ChosenRecord) {
    $passing = Get-PassingRuns @($Runs)
    $batchTable = @()
    foreach ($group in @($passing | Group-Object batch_size)) {
        $values = @($group.Group | ForEach-Object { Get-WallMs $_ })
        $batchTable += [ordered]@{
            batch_size = [int]$group.Name
            runs = $group.Count
            median_wall_ms = Get-Median $values
        }
    }
    $worker1 = @($passing | Where-Object { [int]$_.worker_count -eq 1 -and [int]$_.batch_size -eq $ChosenBatch } | ForEach-Object { Get-WallMs $_ })
    $worker1Median = Get-Median $worker1
    $workerTable = @()
    foreach ($count in 1, 2, 4, 6, 8) {
        $items = @($passing | Where-Object { [int]$_.worker_count -eq $count -and [int]$_.batch_size -eq $ChosenBatch })
        $values = @($items | ForEach-Object { Get-WallMs $_ })
        $median = Get-Median $values
        $workerTable += [ordered]@{
            worker_count = $count
            runs = $items.Count
            median_wall_ms = $median
            speedup_vs_worker1 = if ($median -and $median -lt [double]::PositiveInfinity -and $worker1Median -lt [double]::PositiveInfinity) { $worker1Median / $median } else { $null }
            median_parent_cpu_seconds = Get-Median @($items | ForEach-Object { [double]$_.parent_cpu_seconds })
            median_aggregate_worker_cpu_seconds = Get-Median @($items | ForEach-Object { [double]$_.aggregate_worker_cpu_seconds })
            peak_working_set_bytes = if ($items.Count) { ($items | Measure-Object -Property parent_peak_working_set_bytes -Maximum).Maximum } else { $null }
            max_tick_ms = if ($items.Count) { ($items | ForEach-Object { [double]$_.evidence.run.result.max_tick_ms } | Measure-Object -Maximum).Maximum } else { $null }
            p95_tick_ms = if ($items.Count) { ($items | ForEach-Object { [double]$_.evidence.run.result.tick_p95_ms } | Measure-Object -Maximum).Maximum } else { $null }
            p99_tick_ms = if ($items.Count) { ($items | ForEach-Object { [double]$_.evidence.run.result.tick_p99_ms } | Measure-Object -Maximum).Maximum } else { $null }
        }
    }
    $summary = [ordered]@{
        packet = 'MC4'
        generated_utc = (Get-Date).ToUniversalTime().ToString('o')
        fixture = $Fixture
        fixture_sha256 = $ExpectedSha
        machine = $Machine
        baseline = $Baseline
        policy = [ordered]@{
            phase0 = 'one full worker=1 batch=64 oracle run'
            phase1 = 'worker=4 batch 32/64/96 pilot; repeat top two when within 10 percent'
            phase2 = 'worker 1/2/4/6/8, three measured fresh-process runs per passing count'
            selection = 'fastest safe median; <=5 percent ties choose lower worker count'
            hard_tick_limit_ms = 250
            ram_limit_fraction = 0.8
        }
        chosen = [ordered]@{
            worker_count = $ChosenWorkers
            batch_size = $ChosenBatch
            source_run_id = if ($ChosenRecord) { $ChosenRecord.run_id } else { $null }
        }
        batch_table = $batchTable
        worker_table = $workerTable
        raw_run_count = $Runs.Count
        passing_run_count = $passing.Count
        source_hashes = Get-SourceHashes
    }
    $summary | ConvertTo-Json -Depth 80 | Set-Content -LiteralPath $SummaryPath -Encoding UTF8
}

try {
    if ((Get-FileHash -Algorithm SHA256 -LiteralPath $Fixture).Hash.ToUpperInvariant() -ne $ExpectedSha) { throw 'MC4 fixture preflight SHA mismatch' }
    $logical = [Environment]::ProcessorCount
    $physical = 0
    try { $physical = [int]((Get-CimInstance Win32_Processor | Measure-Object -Property NumberOfCores -Sum).Sum) } catch { $physical = 0 }
    $ram = [int64]0
    try { $ram = [int64](Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory } catch { $ram = 0 }
    $Machine = [ordered]@{
        logical_threads = $logical
        physical_cores = $physical
        total_ram_bytes = $ram
        interactive_blender = @(Get-InteractiveBlenderSnapshot)
        portable_path = $Blender
        helper_path = $Helper
    }
    $Baseline = [ordered]@{
        fixture_sha256 = $ExpectedSha
        baseline_zip_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $ProjectRoot 'uv_gpt_v1.2.6.zip')).Hash.ToUpperInvariant()
        portable_pids = @(Get-ExactPids $Blender)
        helper_pids = @(Get-ExactPids $Helper)
        source_hashes = Get-SourceHashes
    }
    if ($Baseline.portable_pids.Count -ne 0 -or $Baseline.helper_pids.Count -ne 0) { throw 'MC4 process preflight found existing owned-path processes' }
    if (Test-Path -LiteralPath $MatrixPath -PathType Leaf) {
        $existingMatrix = Get-Content -Raw -LiteralPath $MatrixPath | ConvertFrom-Json
        foreach ($priorRun in @($existingMatrix.runs)) {
            if ($null -ne $priorRun) { $Runs.Add($priorRun) }
        }
    }

    if ($Mode -eq 'Single') {
        if ($SingleWorkerCount -lt 1 -or $SingleWorkerCount -gt 8) {
            throw 'SingleWorkerCount must be in 1..8'
        }
        if ($SingleBatchSize -lt 1) { throw 'SingleBatchSize must be positive' }
        $single = Invoke-RecordedRun $SingleWorkerCount $SingleBatchSize $SingleRunClass $SingleRunId $SingleSupersedesRunId ([bool]$ProcessFused)
        if ($single.status -ne 'passed') { throw "MC4 single run failed: $($single.error)" }
        exit 0
    }

    $phase0 = $null
    $pilotPassing = @()
    $selectedBatch = 64
    $chosenWorkers = 1
    $chosenRecord = $null

    if ($Mode -in @('All', 'Phase0', 'Pilot', 'Matrix', 'Phase3')) {
        $phase0RunId = if ($Mode -eq 'Phase0') { $Phase0RunId } else { 'phase0_w1_b64' }
$phase0Supersedes = if ($Mode -eq 'Phase0') { 'phase0_w1_b64_c11' } else { '' }
        $phase0Batch = if ($Mode -eq 'Phase0') { $Phase0BatchSize } else { 64 }
        $phase0 = Invoke-RecordedRun 1 $phase0Batch 'correctness_baseline' $phase0RunId $phase0Supersedes ([bool]$ProcessFused)
        if ($Scenario -eq 'diagnostic') { Write-Output 'MC4 diagnostic completed; no performance acceptance claimed'; exit 0 }
        if ($phase0.status -ne 'passed' -or -not $phase0.evidence.run.full_completion) { throw "MC4 Phase 0 failed: $($phase0.error)" }
        if ($Mode -eq 'Phase0') { exit 0 }
    }

    if ($Mode -in @('All', 'Pilot')) {
        $null = Invoke-RecordedRun 4 64 'warmup' 'pilot_w4_b64_warmup'
        foreach ($batch in 32, 64, 96) { $null = Invoke-RecordedRun 4 $batch 'pilot_measured' ("pilot_w4_b{0}" -f $batch) }
        $pilotPassing = Get-PassingRuns @($Runs | Where-Object { $_.run_class -eq 'pilot_measured' })
        if ($pilotPassing.Count -eq 0) { throw 'MC4 batch pilot has no passing configuration' }
        $orderedPilot = @($pilotPassing | Sort-Object @{Expression={Get-WallMs $_}; Ascending=$true})
        if ($orderedPilot.Count -ge 2 -and (Get-WallMs $orderedPilot[0]) -ge (Get-WallMs $orderedPilot[1]) * 0.9) {
            $null = Invoke-RecordedRun 4 $orderedPilot[0].batch_size 'pilot_repeat' ("pilot_repeat_w4_b{0}" -f $orderedPilot[0].batch_size)
            $null = Invoke-RecordedRun 4 $orderedPilot[1].batch_size 'pilot_repeat' ("pilot_repeat_w4_b{0}" -f $orderedPilot[1].batch_size)
            $pilotPassing = Get-PassingRuns @($Runs | Where-Object { $_.run_class -in @('pilot_measured', 'pilot_repeat') })
        }
        $selectedBatch = [int](@($pilotPassing | Sort-Object @{Expression={Get-WallMs $_}; Ascending=$true})[0].batch_size)
        if ($Mode -eq 'Pilot') { Write-Summary 1 $selectedBatch $phase0; exit 0 }
    }

    if ($Mode -in @('All', 'Matrix')) {
        $null = Invoke-RecordedRun 1 $selectedBatch 'matrix_warmup' ("matrix_w1_b{0}_warmup" -f $selectedBatch)
        foreach ($count in 1, 2, 4, 6, 8) {
            $passedBefore = $true
            $countRecords = @()
            for ($index = 1; $index -le 3; $index++) {
                $item = Invoke-RecordedRun $count $selectedBatch 'matrix_measured' ("matrix_w{0}_b{1}_r{2}" -f $count, $selectedBatch, $index)
                $countRecords += $item
                if ($item.status -ne 'passed') {
                    $passedBefore = $false
                    $diag = Invoke-RecordedRun $count $selectedBatch 'matrix_diagnostic' ("matrix_w{0}_b{1}_diagnostic" -f $count, $selectedBatch)
                    if ($diag.status -ne 'passed') { break }
                }
            }
            if (-not $passedBefore) { Write-Output ("MC4 worker count {0} eliminated after bounded diagnostic policy" -f $count) }
        }
        $passingMatrix = Get-PassingRuns @($Runs | Where-Object { $_.run_class -eq 'matrix_measured' })
        if ($passingMatrix.Count -eq 0) { throw 'MC4 worker matrix has no passing configuration' }
        $byCount = @()
        foreach ($count in 1, 2, 4, 6, 8) {
            $items = @($passingMatrix | Where-Object { [int]$_.worker_count -eq $count })
            if ($items.Count -ge 3) {
                $byCount += [pscustomobject]@{ count = $count; median = Get-Median @($items | ForEach-Object { Get-WallMs $_ }); record = $items[0] }
            }
        }
        if ($byCount.Count -eq 0) { throw 'MC4 worker matrix has no count with three passing runs' }
        $fastest = @($byCount | Sort-Object median)[0]
        $tied = @($byCount | Where-Object { $_.median -le $fastest.median * 1.05 }) | Sort-Object count
        $chosenWorkers = [int]$tied[0].count
        $chosenRecord = @($passingMatrix | Where-Object { [int]$_.worker_count -eq $chosenWorkers } | Sort-Object @{Expression={Get-WallMs $_}; Ascending=$true})[0]
        if ($Mode -eq 'Matrix') { Write-Summary $chosenWorkers $selectedBatch $chosenRecord; exit 0 }
    }

    if ($Mode -eq 'All') {
        $determinism = Invoke-RecordedRun $chosenWorkers $selectedBatch 'chosen_determinism' ("chosen_w{0}_b{1}_determinism" -f $chosenWorkers, $selectedBatch)
        if ($determinism.status -ne 'passed') { throw 'chosen determinism run failed' }
        $budget = Invoke-Mc4Run -WorkerCount $chosenWorkers -BatchSize $selectedBatch -RunClass 'production_budget_30s' -RunId ("chosen_w{0}_b{1}_budget30s" -f $chosenWorkers, $selectedBatch) -TimeBudgetMs 30000
        Add-RunRecord $budget
        if ($budget.status -ne 'passed') { throw 'chosen 30-second budget run failed hard guards' }
        Write-Summary $chosenWorkers $selectedBatch $chosenRecord
    }
    $shaAfterAll = (Get-FileHash -Algorithm SHA256 -LiteralPath $Fixture).Hash.ToUpperInvariant()
    if ($shaAfterAll -ne $ExpectedSha) { throw "MC4 final fixture SHA mismatch: $shaAfterAll" }
    if ((Get-ExactPids $Blender).Count -ne 0 -or (Get-ExactPids $Helper).Count -ne 0) { throw 'MC4 final exact-path orphan guard failed' }
    Write-Output ("MC4 runner passed: mode={0}; runs={1}; selected_workers={2}; selected_batch={3}; fixture_sha={4}" -f $Mode, $Runs.Count, $chosenWorkers, $selectedBatch, $shaAfterAll)
} finally {
    if (Test-Path -LiteralPath $TempRoot) { Remove-Item -LiteralPath $TempRoot -Recurse -Force -ErrorAction SilentlyContinue }
}
