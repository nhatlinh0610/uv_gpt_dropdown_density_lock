param(
    [string]$Fixture = '',
    [int]$MaxProcessSeconds = 300,
    [ValidateSet('HYBRID','VERIFIED_NEAREST_ONLY','EXACT_ONLY')]
    [string]$Mode = 'HYBRID'
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Blender = (Resolve-Path (Join-Path $ProjectRoot '.test_runtime\blender-5.0.0\blender-5.0.0-windows-x64\blender.exe')).Path
$Helper = (Resolve-Path (Join-Path $ProjectRoot '.test_runtime\blender-5.0.0\blender-5.0.0-windows-x64\5.0\python\bin\python.exe')).Path
if ([string]::IsNullOrWhiteSpace($Fixture)) {
    $Fixture = Join-Path $ProjectRoot 'benchmarks\pro_02b_dedicated_fixture.blend'
}
$Fixture = (Resolve-Path -LiteralPath $Fixture).Path
if ([IO.Path]::GetFileName($Fixture) -ieq 'cc.blend') { throw 'R1F dedicated harness must not open cc.blend' }
$ExpectedSha = 'EA290F3437778639AFCA19332E73B4754688B05037A54D35483FFDB8E35A18C8'
$ShaBefore = (Get-FileHash -Algorithm SHA256 -LiteralPath $Fixture).Hash.ToUpperInvariant()
if ($ShaBefore -ne $ExpectedSha) { throw "R1F fixture SHA mismatch: $ShaBefore" }
$Harness = (Resolve-Path (Join-Path $ProjectRoot 'tests\blender\profile_pro_process_r1f.py')).Path
$Result = Join-Path ([IO.Path]::GetTempPath()) ('uv_gpt_r1f_' + [Guid]::NewGuid().ToString('N') + '.json')
$Stdout = [IO.Path]::ChangeExtension($Result, '.stdout.log')
$Stderr = [IO.Path]::ChangeExtension($Result, '.stderr.log')
$TempFiles = @($Result, $Stdout, $Stderr)
$process = $null
function Get-Tail([string]$Path, [int]$Count = 160) {
    if (Test-Path -LiteralPath $Path) { return ((Get-Content -Tail $Count -LiteralPath $Path) -join "`n") }
    return ''
}
try {
    $portableExisting = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.ExecutablePath -and ((Resolve-Path -LiteralPath $_.ExecutablePath -ErrorAction SilentlyContinue).Path -eq $Blender)
    })
    $helperExisting = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.ExecutablePath -and ((Resolve-Path -LiteralPath $_.ExecutablePath -ErrorAction SilentlyContinue).Path -eq $Helper)
    })
    if ($portableExisting.Count -ne 0 -or $helperExisting.Count -ne 0) {
        throw "R1F exact-path process baseline is not clean: portable=$($portableExisting.ProcessId -join ',') helper=$($helperExisting.ProcessId -join ',')"
    }
    $args = @(
        '--factory-startup', '--disable-autoexec', '--background', ('"{0}"' -f $Fixture),
        '--python', ('"{0}"' -f $Harness), '--',
        '--project-root', ('"{0}"' -f $ProjectRoot),
        '--fixture', ('"{0}"' -f $Fixture),
        '--fixture-sha-before', $ShaBefore,
        '--result', ('"{0}"' -f $Result),
        '--mode', $Mode
    )
    $process = Start-Process -FilePath $Blender -ArgumentList $args -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $Stdout -RedirectStandardError $Stderr
    $deadline = (Get-Date).AddSeconds($MaxProcessSeconds)
    while (-not $process.HasExited) {
        $process.Refresh()
        if ((Get-Date) -gt $deadline) {
            $cim = Get-CimInstance Win32_Process -Filter ('ProcessId={0}' -f $process.Id) -ErrorAction SilentlyContinue
            $resolved = if ($cim -and $cim.ExecutablePath) { (Resolve-Path -LiteralPath $cim.ExecutablePath).Path } else { '' }
            if ($resolved -eq $Blender) { Stop-Process -Id $process.Id -Force }
            throw "R1F portable Blender exceeded $MaxProcessSeconds seconds`nSTDOUT:`n$(Get-Tail $Stdout)`nSTDERR:`n$(Get-Tail $Stderr 80)"
        }
        Start-Sleep -Milliseconds 200
    }
    $process.WaitForExit()
    $process.Refresh()
    $exitCode = $process.ExitCode
    $Evidence = $null
    $EvidenceSummary = '<missing>'
    if (Test-Path -LiteralPath $Result -PathType Leaf) {
        try {
            $Evidence = Get-Content -Raw -LiteralPath $Result | ConvertFrom-Json
            $EvidenceSummary = $Evidence | Select-Object status,error,diagnostics,fixture_sha256_before,fixture_sha256_after_in_process | ConvertTo-Json -Compress -Depth 8
        }
        catch {
            $EvidenceSummary = '<invalid-json: ' + $_.Exception.Message + '>'
        }
    }
    # Some portable Blender background exits do not expose ExitCode through
    # the Start-Process handle after the process has reaped, even though the
    # durable harness record and stdout contain an explicit successful return.
    # Treat that state as success only when the harness itself passed and its
    # in-process fixture guard is intact; an unknown code never masks a failed
    # or missing evidence record.
    if ($null -eq $exitCode -and $Evidence -and $Evidence.status -eq 'passed' -and
        $Evidence.fixture_sha256_after_in_process -eq $ShaBefore) {
        $exitCode = 0
        Write-Output 'R1F runner warning: portable ExitCode unavailable; durable passed evidence used'
    }
    if ($exitCode -ne 0) {
        throw "R1F Blender failed with exit code $exitCode`nEVIDENCE:`n$EvidenceSummary`nSTDOUT:`n$(Get-Tail $Stdout)`nSTDERR:`n$(Get-Tail $Stderr 80)"
    }
    if (-not (Test-Path -LiteralPath $Result -PathType Leaf)) {
        throw "R1F result missing: $Result`nSTDOUT:`n$(Get-Tail $Stdout)`nSTDERR:`n$(Get-Tail $Stderr 80)"
    }
    if ($Evidence.status -ne 'passed') {
        throw "R1F evidence was not passed: $($Evidence.error)`nEVIDENCE:`n$EvidenceSummary`nSTDOUT:`n$(Get-Tail $Stdout)`nSTDERR:`n$(Get-Tail $Stderr 80)"
    }
    $orphans = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.ExecutablePath -and ((Resolve-Path -LiteralPath $_.ExecutablePath -ErrorAction SilentlyContinue).Path -eq $Helper)
    })
    if ($orphans.Count -ne 0) { throw "R1F helper orphan remains: $($orphans.ProcessId -join ',')" }
    $shaAfter = (Get-FileHash -Algorithm SHA256 -LiteralPath $Fixture).Hash.ToUpperInvariant()
    if ($shaAfter -ne $ShaBefore) { throw "R1F fixture SHA changed: before=$ShaBefore after=$shaAfter" }
    Write-Output ("R1F runner passed: mode={0}; oracle={1}; python={2}; fixture_sha={3}; cases={4}" -f
        $Evidence.correspondence_mode,
        ($Evidence.oracle_aligned_exact -join ','),
        $Evidence.bundled_python,
        $shaAfter,
        $Evidence.cases.Count)
    foreach ($case in $Evidence.cases) {
        $run = $case.fused
        Write-Output ("R1F case selected={0}; exact={1}; groups={2}; pids={3}; fused_batches={4}/{5}; shape={6}/{7}; exact_pairs={8}/{9}; merged={10}; cache={11}/{12}; frames={13}; max_tick={14}; waiters={15}" -f
            ($case.selected_keys | ConvertTo-Json -Compress),
            $run.aligned_exact,
            $run.group_count,
            ($run.process_worker_pids -join ','),
            $run.process_fused_batches_completed,
            $run.process_fused_batches_submitted,
            $run.process_shape_pairs_completed,
            $run.process_shape_pairs_submitted,
            $run.process_exact_pairs_completed,
            $run.process_exact_pairs_submitted,
            $run.process_merged_pairs,
            $run.process_fused_graph_cache_builds,
            $run.process_fused_graph_cache_hits,
            $run.process_fused_frame_bytes,
            $run.max_tick_ms,
             $run.process_graph_waiter_registrations)
        Write-Output ("R1F startup telemetry selected={0}; worker_start_owner_ms={1}; worker_start_background_ms={2}; worker_start_pending={3}; worker_start_states={4}; context_serialize_owner_ms={5}; context_serialize_background_ms={6}; context_write_background_ms={7}; context_send_pending={8}; pipeline_admission_owner_ms={9}" -f
            ($case.selected_keys | ConvertTo-Json -Compress),
            $run.process_worker_start_owner_ms,
            $run.process_worker_start_background_ms,
            $run.process_worker_start_pending,
            ($run.process_worker_start_states | ConvertTo-Json -Compress),
            $run.process_context_serialize_owner_ms,
            $run.process_context_serialize_background_ms,
            $run.process_context_write_background_ms,
            $run.process_context_send_pending,
            $run.process_pipeline_admission_owner_ms)
    }
}
finally {
    foreach ($path in $TempFiles) {
        if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue }
    }
}
