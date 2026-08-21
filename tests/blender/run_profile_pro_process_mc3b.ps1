param(
    [string]$Fixture = '',
    [int]$MaxProcessSeconds = 300
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Blender = (Resolve-Path (Join-Path $ProjectRoot '.test_runtime\blender-5.0.0\blender-5.0.0-windows-x64\blender.exe')).Path
$Python = (Resolve-Path (Join-Path $ProjectRoot '.test_runtime\blender-5.0.0\blender-5.0.0-windows-x64\5.0\python\bin\python.exe')).Path
if ([string]::IsNullOrWhiteSpace($Fixture)) {
    $Fixture = Join-Path $ProjectRoot 'benchmarks\pro_02b_dedicated_fixture.blend'
}
$Fixture = (Resolve-Path -LiteralPath $Fixture).Path
if ([IO.Path]::GetFileName($Fixture) -ieq 'cc.blend') { throw 'MC3B harness must not open cc.blend' }
$shaBefore = (Get-FileHash -Algorithm SHA256 -LiteralPath $Fixture).Hash.ToUpperInvariant()
$expectedSha = 'EA290F3437778639AFCA19332E73B4754688B05037A54D35483FFDB8E35A18C8'
if ($shaBefore -ne $expectedSha) { throw "MC3B fixture SHA mismatch: $shaBefore" }
$Harness = (Resolve-Path (Join-Path $ProjectRoot 'tests\blender\profile_pro_process_mc3b.py')).Path
$Result = Join-Path ([IO.Path]::GetTempPath()) ('uv_gpt_mc3b_' + [Guid]::NewGuid().ToString('N') + '.json')
$Stdout = [IO.Path]::ChangeExtension($Result, '.stdout.log')
$Stderr = [IO.Path]::ChangeExtension($Result, '.stderr.log')
$tempFiles = @($Result, $Stdout, $Stderr)
$portablePath = (Resolve-Path -LiteralPath $Blender).Path
$helperPath = (Resolve-Path -LiteralPath $Python).Path
$process = $null
try {
    $args = @(
        '--factory-startup', '--disable-autoexec', '--background', ('"{0}"' -f $Fixture),
        '--python', ('"{0}"' -f $Harness), '--',
        '--project-root', ('"{0}"' -f $ProjectRoot),
        '--fixture', ('"{0}"' -f $Fixture),
        '--fixture-sha-before', $shaBefore,
        '--result', ('"{0}"' -f $Result)
    )
    $process = Start-Process -FilePath $Blender -ArgumentList $args -WindowStyle Hidden `
        -PassThru -RedirectStandardOutput $Stdout -RedirectStandardError $Stderr
    $peak = [int64]0
    $deadline = (Get-Date).AddSeconds($MaxProcessSeconds)
    while (-not $process.HasExited) {
        $process.Refresh()
        if ($process.WorkingSet64 -gt $peak) { $peak = [int64]$process.WorkingSet64 }
        if ((Get-Date) -gt $deadline) {
            $cim = Get-CimInstance Win32_Process -Filter ('ProcessId={0}' -f $process.Id) -ErrorAction SilentlyContinue
            $resolved = if ($cim -and $cim.ExecutablePath) { (Resolve-Path -LiteralPath $cim.ExecutablePath).Path } else { '' }
            if ($resolved -eq $portablePath) { Stop-Process -Id $process.Id -Force }
            throw "MC3B portable Blender exceeded $MaxProcessSeconds seconds"
        }
        Start-Sleep -Milliseconds 200
    }
    $process.WaitForExit()
    $process.Refresh()
    if ($process.WorkingSet64 -gt $peak) { $peak = [int64]$process.WorkingSet64 }
    $exitCode = $process.ExitCode
    if ($null -eq $exitCode) { $exitCode = 0 }
    if ($exitCode -ne 0) {
        $outTail = if (Test-Path -LiteralPath $Stdout) { (Get-Content -Tail 160 -LiteralPath $Stdout) -join "`n" } else { '' }
        $errTail = if (Test-Path -LiteralPath $Stderr) { (Get-Content -Tail 80 -LiteralPath $Stderr) -join "`n" } else { '' }
        throw "MC3B Blender failed with exit code $exitCode`nSTDOUT:`n$outTail`nSTDERR:`n$errTail"
    }
    Start-Sleep -Milliseconds 250
    $orphans = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.ExecutablePath -and ((Resolve-Path -LiteralPath $_.ExecutablePath -ErrorAction SilentlyContinue).Path -eq $helperPath)
    })
    if ($orphans.Count -gt 0) { throw "MC3B helper orphan remains: $($orphans.ProcessId -join ',')" }
    $shaAfter = (Get-FileHash -Algorithm SHA256 -LiteralPath $Fixture).Hash.ToUpperInvariant()
    if ($shaAfter -ne $shaBefore) { throw "MC3B fixture SHA changed: before=$shaBefore after=$shaAfter" }
    if (-not (Test-Path -LiteralPath $Result -PathType Leaf)) {
        $outTail = if (Test-Path -LiteralPath $Stdout) { (Get-Content -Tail 100 -LiteralPath $Stdout) -join "`n" } else { '' }
        $errTail = if (Test-Path -LiteralPath $Stderr) { (Get-Content -Tail 100 -LiteralPath $Stderr) -join "`n" } else { '' }
        throw "MC3B result missing: $Result`nSTDOUT:`n$outTail`nSTDERR:`n$errTail"
    }
    $evidence = Get-Content -Raw -LiteralPath $Result | ConvertFrom-Json
    if ($evidence.status -ne 'passed') { throw "MC3B evidence was not passed: $($evidence.status)" }
    if ($evidence.bundled_python -ne $helperPath) { throw "MC3B helper path mismatch: $($evidence.bundled_python)" }
    Write-Output ("MC3B helper={0}; blender_python={1}; thread_caps={2}" -f
        $helperPath,
        $evidence.bundled_python,
        ($evidence.cases[0].process_runs.'2'.process.process_thread_caps | ConvertTo-Json -Compress))
    foreach ($case in $evidence.cases) {
        $run = $case.process_runs.'4'.process
        $run1 = $case.process_runs.'1'.process
        $run2 = $case.process_runs.'2'.process
        Write-Output ("MC3B case selected={0}; oracle={1}; pids_1={2}; pids_2={3}; pids_4={4}; shape={5}/{6}; exact={7}/{8}; merged={9}; retry={10}; max_tick_ms={11}; queue={12}; digest_equal={13}; uv_equal={14}; stage_dist={15}; frames={16}; peak_inflight={17}; cache_hits={18}" -f
            ($case.selected_keys | ConvertTo-Json -Compress),
            $case.sync_aligned_exact,
            ($run1.process_worker_pids -join ','),
            ($run2.process_worker_pids -join ','),
            ($run.process_worker_pids -join ','),
            $run.process_shape_pairs_completed,
            $run.process_shape_pairs_submitted,
            $run.process_exact_pairs_completed,
            $run.process_exact_pairs_submitted,
            $run.process_merged_pairs,
            $run.process_retry_count,
            $run.max_tick_ms,
            $run.process_queue_depth,
            ($case.sync_result_digest -eq $case.process_runs.'4'.result_digest),
            ($case.sync_uv_digest -eq $case.process_runs.'4'.uv_digest),
            ($run.process_stage_distributions | ConvertTo-Json -Compress),
            ($run.process_frame_bytes | ConvertTo-Json -Compress),
            $run.worker_in_flight_peak,
            $run.process_cache_hits)
    }
    Write-Output ("MC3B smoke_6_8={0}; failure_guards={1}; unregister={2}; peak_ws={3}; fixture_sha={4}" -f
        ($evidence.smoke_6_8 | ConvertTo-Json -Compress),
        ($evidence.failure_guards | ConvertTo-Json -Compress),
        ($evidence.unregister | ConvertTo-Json -Compress),
        $peak, $shaAfter)

    # MC3A remains a checked-in lifecycle regression and is rerun in a clean
    # portable Blender process as part of this packet's acceptance guard.
    # MC3A owns its own dedicated-fixture default and is rerun here as a
    # separate clean portable process.  Do not pass a path through the nested
    # PowerShell invocation: this keeps the two runner argument parsers
    # independent while retaining the lifecycle regression guard.
    & (Join-Path $PSScriptRoot 'run_profile_pro_process_mc3a.ps1') -MaxProcessSeconds $MaxProcessSeconds
}
finally {
    foreach ($path in $tempFiles) {
        if (Test-Path -LiteralPath $path) {
            Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
        }
    }
}
