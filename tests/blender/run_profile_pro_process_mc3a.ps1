param(
    [string]$Fixture = '',
    [ValidateSet('HYBRID','VERIFIED_NEAREST_ONLY','EXACT_ONLY')]
    [string]$Mode = 'HYBRID',
    [switch]$FailureOnly,
    [int]$MaxProcessSeconds = 120
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Blender = (Resolve-Path (Join-Path $ProjectRoot '.test_runtime\blender-5.0.0\blender-5.0.0-windows-x64\blender.exe')).Path
if ([string]::IsNullOrWhiteSpace($Fixture)) {
    $Fixture = Join-Path $ProjectRoot 'benchmarks\pro_02b_dedicated_fixture.blend'
}
$Fixture = (Resolve-Path -LiteralPath $Fixture).Path
if ([IO.Path]::GetFileName($Fixture) -ieq 'cc.blend') { throw 'MC3A harness must not open cc.blend' }
$Harness = (Resolve-Path (Join-Path $ProjectRoot 'tests\blender\profile_pro_process_mc3a.py')).Path
$Python = (Resolve-Path (Join-Path $ProjectRoot '.test_runtime\blender-5.0.0\blender-5.0.0-windows-x64\5.0\python\bin\python.exe')).Path
$shaBefore = (Get-FileHash -Algorithm SHA256 -LiteralPath $Fixture).Hash.ToUpperInvariant()
$expectedSha = 'EA290F3437778639AFCA19332E73B4754688B05037A54D35483FFDB8E35A18C8'
if ($shaBefore -ne $expectedSha) { throw "MC3A fixture SHA mismatch: $shaBefore" }
if ($FailureOnly -and $Mode -eq 'HYBRID') {
    throw 'MC3A failure-only requires VERIFIED_NEAREST_ONLY or EXACT_ONLY'
}
$tempRoot = [IO.Path]::GetTempPath()
$token = [Guid]::NewGuid().ToString('N')
$Result = Join-Path $tempRoot ('uv_gpt_mc3a_' + $token + '.json')
$Stdout = Join-Path $tempRoot ('uv_gpt_mc3a_' + $token + '.stdout.log')
$Stderr = Join-Path $tempRoot ('uv_gpt_mc3a_' + $token + '.stderr.log')
$tempFiles = @($Result, $Stdout, $Stderr)
$portablePath = (Resolve-Path -LiteralPath $Blender).Path
$helperPath = (Resolve-Path -LiteralPath $Python).Path
$process = $null
function Get-ExactPathProcesses([string]$TargetPath) {
    return @(
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
            $_.ExecutablePath -and
            ((Resolve-Path -LiteralPath $_.ExecutablePath -ErrorAction SilentlyContinue).Path -eq $TargetPath)
        }
    )
}
function Get-LogTail([string]$Path, [int]$Count = 160) {
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        return ((Get-Content -Tail $Count -LiteralPath $Path -ErrorAction SilentlyContinue) -join "`n")
    }
    return ''
}
function Format-Rendezvous($Rendezvous) {
    return "ready=$($Rendezvous.ready);stage=$($Rendezvous.session_stage)/$($Rendezvous.pipeline_stage);submits=$($Rendezvous.worker_submissions);completions=$($Rendezvous.worker_completions);active_pids=$($Rendezvous.active_worker_pids -join ',');ticks=$($Rendezvous.ticks);wait_ms=$($Rendezvous.wait_ms)"
}
try {
    $portableExisting = @(Get-ExactPathProcesses $portablePath)
    $helperExisting = @(Get-ExactPathProcesses $helperPath)
    if ($portableExisting.Count -ne 0 -or $helperExisting.Count -ne 0) {
        throw "MC3A exact-path process baseline is not clean: portable=$($portableExisting.ProcessId -join ',') helper=$($helperExisting.ProcessId -join ',')"
    }
    $args = @(
        '--factory-startup',
        '--disable-autoexec',
        '--background',
        ('"{0}"' -f $Fixture),
        '--python',
        ('"{0}"' -f $Harness),
        '--',
        '--project-root',
        ('"{0}"' -f $ProjectRoot),
        '--fixture',
        ('"{0}"' -f $Fixture),
        '--fixture-sha-before',
        $shaBefore,
        '--result',
        ('"{0}"' -f $Result),
        '--mode',
        $Mode
    )
    if ($FailureOnly) { $args += '--failure-only' }
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
            throw "MC3A portable Blender exceeded $MaxProcessSeconds seconds`nSTDOUT:`n$(Get-LogTail $Stdout)`nSTDERR:`n$(Get-LogTail $Stderr)"
        }
        Start-Sleep -Milliseconds 200
    }
    $process.WaitForExit()
    $process.Refresh()
    if ($process.WorkingSet64 -gt $peak) { $peak = [int64]$process.WorkingSet64 }
    $exitCode = $process.ExitCode
    if ($null -eq $exitCode) { $exitCode = 0 }
    if ($exitCode -ne 0) {
        throw "MC3A Blender failed with exit code $exitCode`nSTDOUT:`n$(Get-LogTail $Stdout)`nSTDERR:`n$(Get-LogTail $Stderr)"
    }
    Start-Sleep -Milliseconds 250
    $orphans = @(Get-ExactPathProcesses $helperPath)
    $portableLeftovers = @(Get-ExactPathProcesses $portablePath)
    if ($orphans.Count -gt 0 -or $portableLeftovers.Count -gt 0) {
        throw "MC3A owned process remains: portable=$($portableLeftovers.ProcessId -join ',') helper=$($orphans.ProcessId -join ',')`nSTDOUT:`n$(Get-LogTail $Stdout)`nSTDERR:`n$(Get-LogTail $Stderr)"
    }
    $shaAfter = (Get-FileHash -Algorithm SHA256 -LiteralPath $Fixture).Hash.ToUpperInvariant()
    if ($shaAfter -ne $shaBefore) { throw "MC3A fixture SHA changed: before=$shaBefore after=$shaAfter`nSTDOUT:`n$(Get-LogTail $Stdout)`nSTDERR:`n$(Get-LogTail $Stderr)" }
    if (-not (Test-Path -LiteralPath $Result -PathType Leaf)) {
        throw "MC3A result missing: $Result`nSTDOUT:`n$(Get-LogTail $Stdout)`nSTDERR:`n$(Get-LogTail $Stderr)"
    }
    $evidence = Get-Content -Raw -LiteralPath $Result | ConvertFrom-Json
    if ($evidence.status -ne 'passed') { throw "MC3A evidence was not passed: $($evidence.status)" }
    if ($evidence.bundled_python -ne $helperPath) { throw "MC3A helper path mismatch: $($evidence.bundled_python)" }
    if ([string]$evidence.correspondence_mode -ne $Mode) {
        throw "MC3A mode evidence mismatch: expected=$Mode actual=$($evidence.correspondence_mode)"
    }
    if ([bool]$evidence.failure_only -ne [bool]$FailureOnly) {
        throw "MC3A failure_only evidence mismatch: expected=$FailureOnly actual=$($evidence.failure_only)"
    }
    if ($FailureOnly) {
        if ($evidence.worker_mode -ne 'external_bundled_python_group_first') {
            throw "MC3A worker_mode mismatch: $($evidence.worker_mode)"
        }
        if ($evidence.synchronous_fast_invoked) {
            throw 'MC3A failure-only invoked the synchronous route'
        }
        $cancel = $evidence.failure_guards
        $crash = $evidence.crash_cleanup
        $unregister = $evidence.unregister_cleanup
        $rendezvousChecks = @(
            $cancel.cancel_rendezvous,
            $cancel.invalidation_rendezvous,
            $crash.rendezvous,
            $unregister.rendezvous
        )
        foreach ($rendezvous in $rendezvousChecks) {
            if (-not $rendezvous.ready -or
                $rendezvous.session_terminal -or
                -not $rendezvous.group_first_route -or
                [string]$rendezvous.pipeline_type -ne 'GroupFirstProcessPipeline' -or
                -not $rendezvous.pending_work -or
                @($rendezvous.active_worker_pids).Count -lt 1 -or
                $rendezvous.wait_ms -gt 10000) {
                throw "MC3A group-first pending-work rendezvous invalid: $(Format-Rendezvous $rendezvous)"
            }
        }
        if ($cancel.cancel_report.worker_mode -ne 'external_bundled_python_group_first' -or
            $cancel.invalidation_report.worker_mode -ne 'external_bundled_python_group_first' -or
            $crash.report.worker_mode -ne 'external_bundled_python_group_first' -or
            $unregister.report.worker_mode -ne 'external_bundled_python_group_first') {
            throw 'MC3A failure evidence did not preserve group-first worker_mode'
        }
        if ($cancel.cancel_report.exact_loop_writes -ne 0 -or
            $cancel.invalidation_report.exact_loop_writes -ne 0 -or
            -not $cancel.cancel_zero_write -or -not $cancel.invalidation_zero_write -or
            -not $crash.zero_write -or -not $unregister.zero_write) {
            throw 'MC3A failure evidence was not zero-write'
        }
        if ($cancel.cancel_report.cancel_reason -ne 'user_cancelled' -or
            $cancel.cancel_response_ms -gt 500 -or $cancel.cancel_cleanup_ms -gt 10000 -or
            -not $cancel.cancel_state_preserved) {
            throw 'MC3A ESC guard failed'
        }
        if ($cancel.invalidation_report.cancel_reason -ne 'context_invalidated' -or
            $cancel.invalidation_ms -gt 10000 -or
            -not $cancel.invalidation_external_mutation_preserved -or
            -not $cancel.invalidation_state_preserved) {
            throw 'MC3A context invalidation guard failed'
        }
        if ($crash.retry_count -ne 1 -or $crash.crash_ms -gt 10000 -or
            -not $crash.uv_unchanged -or -not $crash.selection_unchanged -or
            -not $crash.active_unchanged -or -not $crash.worker_shutdown) {
            throw 'MC3A repeated crash guard failed'
        }
        if ($unregister.cancel_reason -ne 'unregister' -or
            $unregister.unregister_ms -gt 10000 -or
            $unregister.second_cleanup_ms -gt 10000 -or
            -not $unregister.second_cleanup_safe -or
            -not $unregister.worker_shutdown -or
            -not $unregister.uv_unchanged -or
            -not $unregister.selection_unchanged -or
            -not $unregister.active_unchanged) {
            throw 'MC3A unregister guard failed'
        }
        Write-Output (("MC3A mode={0}; failure_only=True; worker_mode={1}; " +
            "cancel_response_ms={2}; cancel_cleanup_ms={3}; cancel_reason={4}; " +
            "invalidation_ms={5}; invalidation_reason={6}; " +
            "crash_ms={7}; crash_retry={8}; crash_pids={9}; crash_error={10}; " +
            "unregister_ms={11}; unregister_reason={12}; second_unregister_ms={13}; " +
            "unregister_pids={14}; rendezvous={15}; orphan_count={16}; fixture_sha={17}; peak_ws={18}") -f
            $Mode,
            $evidence.worker_mode,
            $cancel.cancel_response_ms,
            $cancel.cancel_cleanup_ms,
            $cancel.cancel_report.cancel_reason,
            $cancel.invalidation_ms,
            $cancel.invalidation_report.cancel_reason,
            $crash.crash_ms,
            $crash.retry_count,
            ($crash.crash_pids -join ','),
            $crash.error,
            $unregister.unregister_ms,
            $unregister.cancel_reason,
            $unregister.second_cleanup_ms,
            ($unregister.worker_pids -join ','),
            (("cancel:[{0}]; invalidation:[{1}]; crash:[{2}]; unregister:[{3}]" -f
                (Format-Rendezvous $cancel.cancel_rendezvous),
                (Format-Rendezvous $cancel.invalidation_rendezvous),
                (Format-Rendezvous $crash.rendezvous),
                (Format-Rendezvous $unregister.rendezvous))),
            ($orphans.Count + $portableLeftovers.Count),
            $shaAfter,
            $peak)
    }
    else {
        $caseSummary = foreach ($case in $evidence.cases) {
            $proc = $case.process
            "exact=$($case.process_aligned_exact);pids=$($proc.process_worker_pids -join ',');submits=$($proc.worker_submissions);completions=$($proc.worker_completions);retry=$($proc.process_retry_count);startup_ms=$($proc.process_startup_ms);dispatch_ms=$($proc.process_dispatch_ms);poll_ms=$($proc.process_poll_ms);compute_ms=$($proc.process_compute_ms);max_tick_ms=$($proc.max_tick_ms);digest_equal=$($case.sync_result_digest -eq $case.process_result_digest);uv_digest_equal=$($case.sync_uv_digest -eq $case.process_uv_digest);mapping_delta=$($case.mapping_max_delta)"
        }
        Write-Output ("MC3A mode={0}; failure_only=False; helper={1}; python_version={2}; thread_caps={3}" -f
            $Mode,
            $helperPath,
            $evidence.cases[0].process.process_python_version,
            ($evidence.cases[0].process.process_thread_caps | ConvertTo-Json -Compress))
        Write-Output ("MC3A cases: {0}" -f ($caseSummary -join ' | '))
        Write-Output ("MC3A live process passed: oracle={0}; peak_ws={1}; fixture_sha={2}" -f
            (($evidence.oracle_aligned_exact | ForEach-Object { $_ }) -join ','), $peak, $shaAfter)
    }
}
finally {
    foreach ($path in $tempFiles) {
        if (Test-Path -LiteralPath $path) {
            Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
        }
    }
}
