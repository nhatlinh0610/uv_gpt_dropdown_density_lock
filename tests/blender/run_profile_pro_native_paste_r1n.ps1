param(
    [string]$Fixture = '',
    [int]$MaxProcessSeconds = 180
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Blender = (Resolve-Path (Join-Path $ProjectRoot '.test_runtime\blender-5.0.0\blender-5.0.0-windows-x64\blender.exe')).Path
$Helper = (Resolve-Path (Join-Path $ProjectRoot '.test_runtime\blender-5.0.0\blender-5.0.0-windows-x64\5.0\python\bin\python.exe')).Path
if ([string]::IsNullOrWhiteSpace($Fixture)) {
    $Fixture = Join-Path $ProjectRoot 'benchmarks\pro_02b_dedicated_fixture.blend'
}
$Fixture = (Resolve-Path -LiteralPath $Fixture).Path
if ([IO.Path]::GetFileName($Fixture) -ieq 'cc.blend') {
    throw 'MC4-R1N harness must not open cc.blend'
}
$Harness = (Resolve-Path (Join-Path $ProjectRoot 'tests\blender\profile_pro_native_paste_r1n.py')).Path
$ExpectedFixtureSha = 'EA290F3437778639AFCA19332E73B4754688B05037A54D35483FFDB8E35A18C8'
$ShaBefore = (Get-FileHash -Algorithm SHA256 -LiteralPath $Fixture).Hash.ToUpperInvariant()
if ($ShaBefore -ne $ExpectedFixtureSha) {
    throw "MC4-R1N dedicated fixture SHA mismatch: $ShaBefore"
}

function Resolve-CanonicalPath([string]$PathValue) {
    if ([string]::IsNullOrWhiteSpace($PathValue)) { return '' }
    try { return (Resolve-Path -LiteralPath $PathValue -ErrorAction Stop).Path }
    catch { return '' }
}

$PortablePath = Resolve-CanonicalPath $Blender
$HelperPath = Resolve-CanonicalPath $Helper

function Get-ExactPathProcesses([string]$CanonicalPath) {
    if ([string]::IsNullOrWhiteSpace($CanonicalPath)) { return @() }
    @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.ExecutablePath -and ((Resolve-CanonicalPath $_.ExecutablePath) -ieq $CanonicalPath)
    } | ForEach-Object {
        [PSCustomObject]@{
            pid = [int]$_.ProcessId
            executable = (Resolve-CanonicalPath $_.ExecutablePath)
            creation = [string]$_.CreationDate
            command_line = [string]$_.CommandLine
        }
    })
}

function Get-InteractiveBlenderProcesses {
    @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.ExecutablePath -and
        ([IO.Path]::GetFileName($_.ExecutablePath) -ieq 'blender.exe') -and
        ((Resolve-CanonicalPath $_.ExecutablePath) -ine $PortablePath)
    } | ForEach-Object {
        [PSCustomObject]@{
            pid = [int]$_.ProcessId
            executable = (Resolve-CanonicalPath $_.ExecutablePath)
            creation = [string]$_.CreationDate
        }
    })
}

$PortableBefore = @(Get-ExactPathProcesses $PortablePath)
$HelperBefore = @(Get-ExactPathProcesses $HelperPath)
$InteractiveBefore = @(Get-InteractiveBlenderProcesses)
if ($PortableBefore.Count -gt 0) {
    throw "MC4-R1N portable Blender already running: $($PortableBefore.pid -join ',')"
}

$Result = Join-Path ([IO.Path]::GetTempPath()) ('uv_gpt_mc4r1n_' + [Guid]::NewGuid().ToString('N') + '.json')
$Stdout = [IO.Path]::ChangeExtension($Result, '.stdout.log')
$Stderr = [IO.Path]::ChangeExtension($Result, '.stderr.log')
$TempFiles = @($Result, $Stdout, $Stderr)
$Process = $null
$PeakWorkingSet = [int64]0
$ExitCode = $null
$Evidence = $null
$RunError = $null

try {
    $Args = @(
        '--factory-startup', '--disable-autoexec', '--background', ('"{0}"' -f $Fixture),
        '--python', ('"{0}"' -f $Harness), '--',
        '--project-root', ('"{0}"' -f $ProjectRoot),
        '--fixture', ('"{0}"' -f $Fixture),
        '--fixture-sha-before', $ShaBefore,
        '--result', ('"{0}"' -f $Result)
    )
    $Process = Start-Process -FilePath $Blender -ArgumentList $Args -WindowStyle Hidden `
        -PassThru -RedirectStandardOutput $Stdout -RedirectStandardError $Stderr
    $Deadline = (Get-Date).AddSeconds($MaxProcessSeconds)
    while (-not $Process.HasExited) {
        $Process.Refresh()
        if ($Process.WorkingSet64 -gt $PeakWorkingSet) { $PeakWorkingSet = [int64]$Process.WorkingSet64 }
        if ((Get-Date) -gt $Deadline) {
            $cim = Get-CimInstance Win32_Process -Filter ('ProcessId={0}' -f $Process.Id) -ErrorAction SilentlyContinue
            $resolved = if ($cim -and $cim.ExecutablePath) { Resolve-CanonicalPath $cim.ExecutablePath } else { '' }
            if ($resolved -ieq $PortablePath) {
                Stop-Process -Id $Process.Id -Force
            }
            throw "MC4-R1N portable Blender exceeded $MaxProcessSeconds seconds"
        }
        Start-Sleep -Milliseconds 200
    }
    $Process.WaitForExit()
    $Process.Refresh()
    if ($Process.WorkingSet64 -gt $PeakWorkingSet) { $PeakWorkingSet = [int64]$Process.WorkingSet64 }
    $ExitCode = $Process.ExitCode

    # Read durable evidence and both streams before evaluating exit status or
    # removing any exact TEMP file.  A semantic native-backend rejection is a
    # successful oracle run and is intentionally reported with exit code 0.
    if (Test-Path -LiteralPath $Result -PathType Leaf) {
        $Evidence = Get-Content -Raw -LiteralPath $Result | ConvertFrom-Json
    }
    $OutText = if (Test-Path -LiteralPath $Stdout) { Get-Content -Raw -LiteralPath $Stdout } else { '' }
    $ErrText = if (Test-Path -LiteralPath $Stderr) { Get-Content -Raw -LiteralPath $Stderr } else { '' }
    if ($null -eq $Evidence) {
        throw "MC4-R1N result JSON missing: $Result`nSTDOUT:`n$OutText`nSTDERR:`n$ErrText"
    }
    if ($ExitCode -ne 0 -or $Evidence.status -eq 'failed') {
        throw "MC4-R1N Blender/harness failed exit=$ExitCode status=$($Evidence.status)`nSTDOUT:`n$OutText`nSTDERR:`n$ErrText"
    }

    Start-Sleep -Milliseconds 250
    $PortableAfter = @(Get-ExactPathProcesses $PortablePath)
    $HelperAfter = @(Get-ExactPathProcesses $HelperPath)
    $NewHelperOrphans = @($HelperAfter | Where-Object {
        $beforePids = @($HelperBefore | ForEach-Object { $_.pid })
        $_.pid -notin $beforePids
    })
    if ($PortableAfter.Count -gt 0) {
        throw "MC4-R1N portable Blender orphan remains: $($PortableAfter.pid -join ',')"
    }
    if ($NewHelperOrphans.Count -gt 0) {
        throw "MC4-R1N helper orphan remains: $($NewHelperOrphans.pid -join ',')"
    }
    $ShaAfter = (Get-FileHash -Algorithm SHA256 -LiteralPath $Fixture).Hash.ToUpperInvariant()
    if ($ShaAfter -ne $ShaBefore) {
        throw "MC4-R1N dedicated fixture SHA changed: before=$ShaBefore after=$ShaAfter"
    }
    $InteractiveAfter = @(Get-InteractiveBlenderProcesses)
    $InteractiveGuard = @()
    foreach ($item in $InteractiveBefore) {
        $same = @($InteractiveAfter | Where-Object {
            $_.pid -eq $item.pid -and $_.executable -ieq $item.executable -and $_.creation -eq $item.creation
        })
        if ($same.Count -ne 1) {
            throw "MC4-R1N interactive Blender guard changed PID $($item.pid)"
        }
        $InteractiveGuard += [PSCustomObject]@{ pid=$item.pid; executable=$item.executable; creation=$item.creation; unchanged=$true }
    }

    Write-Output ("MC4-R1N decision={0}; status={1}; oracle={2}; mismatches={3}; fixture_sha={4}; peak_ws={5}; interactive={6}" -f
        $Evidence.decision, $Evidence.status, ($Evidence.oracle_aligned_exact -join ','), $Evidence.semantic_mismatch_count,
        $ShaAfter, $PeakWorkingSet, ($InteractiveGuard | ConvertTo-Json -Compress))
    foreach ($case in $Evidence.cases) {
        $individual = @($case.native.individual)
        $bulk = @($case.native.bulk)
        $indMax = ($individual | Measure-Object -Property paste_elapsed_ms -Maximum).Maximum
        $bulkMax = ($bulk | Measure-Object -Property paste_elapsed_ms -Maximum).Maximum
        Write-Output ("MC4-R1N case={0}; sync_exact={1}; accepted={2}; individual={3}; bulk={4}; paste_ms_max={5}/{6}; raw_state_changed={7}/{8}; negative={9}" -f
            $case.object, $case.sync.aligned_exact, (($case.native.accepted_target_keys | ConvertTo-Json -Compress)),
            $individual.Count, $bulk.Count, $indMax, $bulkMax,
            (($individual | Where-Object { -not $_.raw_selection_unchanged }).Count),
            (($bulk | Where-Object { -not $_.raw_selection_unchanged }).Count),
            (($case.native.negative_same_face_count | ConvertTo-Json -Compress)))
    }
}
catch {
    $RunError = $_.Exception.ToString()
    Write-Error $RunError
    throw
}
finally {
    foreach ($PathValue in $TempFiles) {
        if (Test-Path -LiteralPath $PathValue) {
            Remove-Item -LiteralPath $PathValue -Force -ErrorAction SilentlyContinue
        }
    }
}
