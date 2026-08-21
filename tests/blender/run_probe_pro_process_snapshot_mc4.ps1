param(
    [string]$Fixture = 'C:\Users\linhp\Downloads\cc.blend',
    [int]$MaxProcessSeconds = 180
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Blender = (Resolve-Path (Join-Path $ProjectRoot '.test_runtime\blender-5.0.0\blender-5.0.0-windows-x64\blender.exe')).Path
$Helper = (Resolve-Path (Join-Path $ProjectRoot '.test_runtime\blender-5.0.0\blender-5.0.0-windows-x64\5.0\python\bin\python.exe')).Path
$Fixture = (Resolve-Path -LiteralPath $Fixture).Path
$ExpectedSha = '49A329EFA1DDA72C4BEB040786590F8B0946BB737266C0498DC6A828C941EEE6'
$Harness = (Resolve-Path (Join-Path $ProjectRoot 'tests\blender\probe_pro_process_snapshot_mc4.py')).Path
$TempRoot = Join-Path ([IO.Path]::GetTempPath()) ('uvgpt_mc4c4_probe_' + [guid]::NewGuid().ToString('N'))
$null = New-Item -ItemType Directory -Path $TempRoot -Force
$Result = Join-Path $TempRoot 'snapshot.json'
$Stdout = Join-Path $TempRoot 'stdout.txt'
$Stderr = Join-Path $TempRoot 'stderr.txt'
$tempFiles = @($Result, $Stdout, $Stderr)
$processExitCode = $null
$processError = $null
$evidence = $null
$evidenceRaw = ''
$stdoutText = ''
$stderrText = ''
$guardErrors = @()

function Get-Canonical([string]$PathValue) {
    if (-not $PathValue) { return '' }
    try { return (Resolve-Path -LiteralPath $PathValue -ErrorAction Stop).Path } catch { return '' }
}

function Get-ExactPids([string]$ExecutablePath) {
    $canonical = Get-Canonical $ExecutablePath
    if (-not $canonical) { return @() }
    return @(
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object { $_.ExecutablePath -and (Get-Canonical $_.ExecutablePath) -eq $canonical } |
            ForEach-Object { [int]$_.ProcessId } |
            Sort-Object -Unique
    )
}

function Get-InteractiveSnapshot {
    return @(
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object { $_.ExecutablePath -and $_.ExecutablePath -like '*BlenderFoundation.Blender*5.2*blender.exe' } |
            ForEach-Object {
                [ordered]@{
                    pid = [int]$_.ProcessId
                    executable = [string]$_.ExecutablePath
                    creation_date = [string]$_.CreationDate
                }
            } |
            Sort-Object pid
    )
}

$fixtureBefore = (Get-FileHash -Algorithm SHA256 -LiteralPath $Fixture).Hash.ToUpperInvariant()
if ($fixtureBefore -ne $ExpectedSha) { throw "C4 fixture SHA mismatch before probe: $fixtureBefore" }
$portableBefore = @(Get-ExactPids $Blender)
$helperBefore = @(Get-ExactPids $Helper)
if ($portableBefore.Count -ne 0 -or $helperBefore.Count -ne 0) {
    throw "C4 process baseline is not clean: portable=$($portableBefore -join ',') helper=$($helperBefore -join ',')"
}
$interactiveBefore = @(Get-InteractiveSnapshot)
$interactiveBeforeJson = $interactiveBefore | ConvertTo-Json -Depth 8 -Compress
$interactiveReference = @($interactiveBefore | Where-Object { $_.pid -eq 25756 })
if ($interactiveReference.Count -ne 1) {
    throw "MC4-C4 expected interactive Blender PID 25756 in baseline, found $($interactiveReference.Count)"
}
$process = $null
try {
    $args = @(
        '--factory-startup', '--disable-autoexec', '--background', ('"{0}"' -f $Fixture),
        '--python', ('"{0}"' -f $Harness), '--',
        '--project-root', ('"{0}"' -f $ProjectRoot),
        '--fixture', ('"{0}"' -f $Fixture),
        '--fixture-sha-before', $fixtureBefore,
        '--result', ('"{0}"' -f $Result)
    )
    $process = Start-Process -FilePath $Blender -ArgumentList $args -WindowStyle Hidden `
        -PassThru -RedirectStandardOutput $Stdout -RedirectStandardError $Stderr
    $deadline = (Get-Date).AddSeconds($MaxProcessSeconds)
    while (-not $process.HasExited) {
        if ((Get-Date) -gt $deadline) {
            $cim = Get-CimInstance Win32_Process -Filter ("ProcessId={0}" -f $process.Id) -ErrorAction SilentlyContinue
            $resolved = if ($cim -and $cim.ExecutablePath) { Get-Canonical $cim.ExecutablePath } else { '' }
            if ($resolved -eq (Get-Canonical $Blender)) { Stop-Process -Id $process.Id -Force }
            throw "C4 snapshot probe exceeded $MaxProcessSeconds seconds"
        }
        Start-Sleep -Milliseconds 200
    }
    $process.WaitForExit()
    $processExitCode = $process.ExitCode
}
catch {
    $processError = $_.Exception.Message
}
finally {
    # Read every child artifact before checking ExitCode and before TEMP cleanup.
    $stdoutText = if (Test-Path -LiteralPath $Stdout -PathType Leaf) {
        Get-Content -Raw -LiteralPath $Stdout -ErrorAction SilentlyContinue
    } else { '' }
    $stderrText = if (Test-Path -LiteralPath $Stderr -PathType Leaf) {
        Get-Content -Raw -LiteralPath $Stderr -ErrorAction SilentlyContinue
    } else { '' }
    $evidenceRaw = if (Test-Path -LiteralPath $Result -PathType Leaf) {
        Get-Content -Raw -LiteralPath $Result -ErrorAction SilentlyContinue
    } else { '' }
    if ($evidenceRaw) {
        try { $evidence = $evidenceRaw | ConvertFrom-Json } catch { $guardErrors += "structured JSON parse failed: $($_.Exception.Message)" }
    } else {
        $guardErrors += 'structured JSON missing'
    }

    $fixtureAfter = try { (Get-FileHash -Algorithm SHA256 -LiteralPath $Fixture).Hash.ToUpperInvariant() } catch { '' }
    $portableAfter = @(Get-ExactPids $Blender | Where-Object { -not $portableBefore.Contains($_) })
    $helperAfter = @(Get-ExactPids $Helper | Where-Object { -not $helperBefore.Contains($_) })
    $interactiveAfter = @(Get-InteractiveSnapshot)
    $interactiveAfterJson = $interactiveAfter | ConvertTo-Json -Depth 8 -Compress
    $interactiveStable = $interactiveBeforeJson -eq $interactiveAfterJson
    $orphanCount = $portableAfter.Count + $helperAfter.Count
    if ($fixtureAfter -ne $ExpectedSha) { $guardErrors += "fixture SHA changed: $fixtureAfter" }
    if ($orphanCount -ne 0) { $guardErrors += "owned orphan count=$orphanCount" }
    if (-not $interactiveStable) { $guardErrors += 'interactive Blender snapshot changed externally' }
    if ($evidence -and $evidence.status -ne 'diagnostic') { $guardErrors += "probe status mismatch: $($evidence.status)" }

    $run = if ($evidence) { $evidence.run } else { $null }
    $durableSummary = [ordered]@{
        packet = 'MC4-C4-SNAPSHOT-PROBE'
        blender_exit_code = $processExitCode
        process_error = $processError
        result_available = [bool]$evidence
        fixture_sha256_before = $fixtureBefore
        fixture_sha256_after = $fixtureAfter
        portable_orphan_count = $portableAfter.Count
        helper_orphan_count = $helperAfter.Count
        orphan_count = $orphanCount
        interactive_pid_25756_present = (@($interactiveBefore | Where-Object { $_.pid -eq 25756 }).Count -eq 1)
        interactive_unchanged = $interactiveStable
        guard_errors = @($guardErrors)
        probe_status = if ($run) { [string]$run.status } else { '' }
        snapshot_completed = if ($run) { [bool]$run.snapshot_completed } else { $false }
        builder_phase = if ($run) { [string]$run.builder_phase } else { '' }
        builder_slices = if ($run) { [int]$run.builder_slices } else { 0 }
        builder_operations = if ($run) { [int]$run.builder_operations } else { 0 }
        error = if ($run) { [string]$run.error } else { $processError }
        stdout_tail = if ($stdoutText.Length -gt 12000) { $stdoutText.Substring($stdoutText.Length - 12000) } else { $stdoutText }
        stderr_tail = if ($stderrText.Length -gt 12000) { $stderrText.Substring($stderrText.Length - 12000) } else { $stderrText }
    }
    Write-Output 'MC4-C4 durable evidence (captured before exit evaluation):'
    Write-Output ($durableSummary | ConvertTo-Json -Depth 8 -Compress)
    if ($evidenceRaw) {
        Write-Output 'MC4-C4 raw result JSON (copied before TEMP cleanup):'
        Write-Output $evidenceRaw
    }

    foreach ($path in $tempFiles) {
        if (Test-Path -LiteralPath $path) {
            Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
        }
    }
    if (Test-Path -LiteralPath $TempRoot) {
        Remove-Item -LiteralPath $TempRoot -Force -Recurse -ErrorAction SilentlyContinue
    }
}

if ($guardErrors.Count -gt 0) {
    throw ("MC4-C4 guard failure: " + ($guardErrors -join '; '))
}
if ($processError) {
    throw ("MC4-C4 Blender launch/wait failure: " + $processError)
}
if ($processExitCode -ne 0) {
    throw ("MC4-C4 Blender exit code $processExitCode; durable evidence was captured above")
}
