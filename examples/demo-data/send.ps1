<#
.SYNOPSIS
  Sends ONE AERP demo request file and prints the status + response.
.EXAMPLE
  .\send.ps1 requests\N05_resolve_apple.json
  .\send.ps1 requests\E02_country_too_long.json -Quiet
.NOTES
  If AUTH is enabled, set $env:AERP_DEMO_API_KEY to one of your API_KEYS first. It is never printed.
#>
param(
    [Parameter(Mandatory = $true)][string]$File,
    [string]$BaseUrl = "http://127.0.0.1:8000",
    [switch]$Quiet
)
$spec = Get-Content -Raw $File | ConvertFrom-Json
$url = $BaseUrl.TrimEnd("/") + $spec.path
if ($spec.query) {
    $pairs = $spec.query.PSObject.Properties | ForEach-Object { [uri]::EscapeDataString($_.Name) + "=" + [uri]::EscapeDataString([string]$_.Value) }
    $url += "?" + ($pairs -join "&")
}
$headers = @{ "Accept" = "application/json" }
if ($env:AERP_DEMO_API_KEY) { $headers["Authorization"] = "Bearer $($env:AERP_DEMO_API_KEY)" }
$params = @{ Uri = $url; Method = $spec.method; Headers = $headers; UseBasicParsing = $true }
if ($null -ne $spec.body) {
    $params["ContentType"] = "application/json"
    $params["Body"] = [System.Text.Encoding]::UTF8.GetBytes(($spec.body | ConvertTo-Json -Depth 20 -Compress))
}
$status = 0; $content = ""
try {
    $resp = Invoke-WebRequest @params
    $status = [int]$resp.StatusCode; $content = $resp.Content
} catch {
    if ($_.Exception.Response) {
        $status = [int]$_.Exception.Response.StatusCode
        $reader = New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream())
        $content = $reader.ReadToEnd()
    } else { $content = "NO RESPONSE: $($_.Exception.Message)" }
}
$ok = ($status -eq [int]$spec.expect_status)
if ($ok -and $spec.expect_contains) { $ok = $content -like "*$($spec.expect_contains)*" }
$result = [pscustomobject]@{ Name = [IO.Path]::GetFileNameWithoutExtension($File); Status = $status; Expected = $spec.expect_status; Pass = $ok; Body = $content }
if (-not $Quiet) {
    "{0}  HTTP {1} (expected {2})  {3}" -f $result.Name, $status, $spec.expect_status, $(if ($ok) { "PASS" } else { "FAIL" })
    if ($spec.note) { "note: $($spec.note)" }
    try { $content | ConvertFrom-Json | ConvertTo-Json -Depth 8 } catch { $content }
}
return $result
