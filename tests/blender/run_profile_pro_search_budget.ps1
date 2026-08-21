param(
    [string]$ExpectedSha = '49A329EFA1DDA72C4BEB040786590F8B0946BB737266C0498DC6A828C941EEE6',
    [int]$MaxProcessSeconds = 120
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Blender = (Resolve-Path (Join-Path $ProjectRoot '.test_runtime\blender-5.0.0\blender-5.0.0-windows-x64\blender.exe')).Path
$Harness = (Resolve-Path (Join-Path $ProjectRoot 'tests\blender\profile_pro_search_budget.py')).Path
$CurrentFixture = 'C:\Users\linhp\Downloads\cc.blend'
$DedicatedFixture = (Resolve-Path (Join-Path $ProjectRoot 'benchmarks\pro_02b_dedicated_fixture.blend')).Path
$PortablePath = $Blender

function Invoke-SearchProfile([string]$Mode, [string]$Fixture, [string]$Expected, [string]$Result, [string]$Stdout, [string]$Stderr) {
    $sha = (Get-FileHash -Algorithm SHA256 -LiteralPath $Fixture).Hash.ToUpperInvariant()
    if ($Expected -and $sha -ne $Expected) { throw "Search profile SHA mismatch: $sha" }
    $args = @(
        '--factory-startup', '--disable-autoexec', '--background', ('"{0}"' -f $Fixture),
        '--python', ('"{0}"' -f $Harness), '--',
        '--project-root', ('"{0}"' -f $ProjectRoot), '--mode', $Mode,
        '--fixture', ('"{0}"' -f $Fixture), '--expected-sha', $sha,
        '--result', ('"{0}"' -f $Result)
    )
    $process = Start-Process -FilePath $Blender -ArgumentList $args -WindowStyle Hidden `
        -PassThru -RedirectStandardOutput $Stdout -RedirectStandardError $Stderr
    $deadline = (Get-Date).AddSeconds($MaxProcessSeconds)
    while (-not $process.HasExited) {
        if ((Get-Date) -gt $deadline) {
            $cim = Get-CimInstance Win32_Process -Filter ("ProcessId={0}" -f $process.Id) -ErrorAction SilentlyContinue
            $resolved = if ($cim -and $cim.ExecutablePath) { (Resolve-Path -LiteralPath $cim.ExecutablePath).Path } else { '' }
            if ($resolved -eq $PortablePath) { Stop-Process -Id $process.Id -Force }
            throw "Search profile $Mode exceeded $MaxProcessSeconds seconds"
        }
        Start-Sleep -Milliseconds 250
    }
    $process.WaitForExit()
    $exit = $process.ExitCode
    if ($null -eq $exit) { $exit = 0 }
    Start-Sleep -Milliseconds 250
    if ($exit -ne 0) { throw "Search profile $Mode failed with exit code $exit" }
    $orphans = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.ExecutablePath -and ((Resolve-Path -LiteralPath $_.ExecutablePath -ErrorAction SilentlyContinue).Path -eq $PortablePath)
    })
    if ($orphans.Count -gt 0) { throw "Portable Blender orphan after $($Mode): $($orphans.ProcessId -join ',')" }
    if ((Get-FileHash -Algorithm SHA256 -LiteralPath $Fixture).Hash.ToUpperInvariant() -ne $sha) {
        throw "Search profile $Mode changed fixture"
    }
    if (-not (Test-Path -LiteralPath $Result -PathType Leaf)) { throw "Search result missing: $Result" }
    $evidence = Get-Content -Raw -LiteralPath $Result | ConvertFrom-Json
    if ($evidence.status -ne 'passed') { throw "Search evidence failed: $Mode" }
    Write-Output ("P03 search {0}: rows={1}; SHA={2}" -f $Mode, $evidence.runs.Count, $sha)
}

New-Item -ItemType Directory -Force -Path (Join-Path $ProjectRoot 'benchmarks') | Out-Null
$currentFirst = (Get-FileHash -Algorithm SHA256 -LiteralPath $CurrentFixture).Hash.ToUpperInvariant()
Start-Sleep -Milliseconds 250
$currentSecond = (Get-FileHash -Algorithm SHA256 -LiteralPath $CurrentFixture).Hash.ToUpperInvariant()
if ($currentFirst -ne $currentSecond -or $currentFirst -ne $ExpectedSha) { throw "Current fixture unstable/mismatched" }
Invoke-SearchProfile 'cc' $CurrentFixture $ExpectedSha `
    (Join-Path $ProjectRoot 'benchmarks\pro_03_search_budget_cc.json') `
    (Join-Path $ProjectRoot 'benchmarks\pro_03_search_budget_cc_stdout.log') `
    (Join-Path $ProjectRoot 'benchmarks\pro_03_search_budget_cc_stderr.log')
Invoke-SearchProfile 'dedicated' $DedicatedFixture '' `
    (Join-Path $ProjectRoot 'benchmarks\pro_03_search_budget_dedicated.json') `
    (Join-Path $ProjectRoot 'benchmarks\pro_03_search_budget_dedicated_stdout.log') `
    (Join-Path $ProjectRoot 'benchmarks\pro_03_search_budget_dedicated_stderr.log')
$currentAfter = (Get-FileHash -Algorithm SHA256 -LiteralPath $CurrentFixture).Hash.ToUpperInvariant()
if ($currentAfter -ne $ExpectedSha) { throw "Current fixture changed after search profiles" }
Write-Output "P03 search profiles passed; current SHA=$currentAfter"
