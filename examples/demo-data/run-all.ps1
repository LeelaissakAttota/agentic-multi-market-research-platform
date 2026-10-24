<#
.SYNOPSIS
  Runs every AMRP demo request (N* normal, E* error) and prints a PASS/FAIL table.
.EXAMPLE
  .\run-all.ps1
  .\run-all.ps1 -Filter "E*"
#>
param([string]$BaseUrl = "http://127.0.0.1:8000", [string]$Filter = "*")
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$results = foreach ($f in Get-ChildItem (Join-Path $here "requests") -Filter "$Filter.json" | Sort-Object Name) {
    & (Join-Path $here "send.ps1") -File $f.FullName -BaseUrl $BaseUrl -Quiet
}
$results | Select-Object Name, Status, Expected, @{ n = "Result"; e = { if ($_.Pass) { "PASS" } else { "FAIL" } } } | Format-Table -AutoSize
$p = @($results | Where-Object Pass).Count
"PASS: $p / $(@($results).Count)"
