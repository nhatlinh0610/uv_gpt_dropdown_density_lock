$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Src = Join-Path $PSScriptRoot "pack_native.cpp"
$Out = Join-Path $RepoRoot "uv_gpt\uv_gpt_pack_native.dll"

$Gxx = (Get-Command g++ -ErrorAction Stop).Source
Write-Host "Building with: $Gxx"

& $Gxx -O3 -std=c++20 -mpopcnt -mbmi2 -mstackrealign -shared `
    -static -static-libgcc -static-libstdc++ `
    $Src -o $Out `
    "-Wl,-Bstatic" -l:libstdc++.a -l:libwinpthread.a "-Wl,-Bdynamic"

if ($LASTEXITCODE -ne 0) {
    throw "g++ build failed with exit code $LASTEXITCODE"
}

Write-Host "Built: $Out ($((Get-Item $Out).Length) bytes)"
& objdump -p $Out | Select-String "DLL Name:"
