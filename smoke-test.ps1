# smoke-test.ps1 — Testa todos os endpoints do sre-demo-service
# Usage: .\smoke-test.ps1 -ServiceUrl "https://sre-demo-service-xxx-ew.a.run.app"

param(
    [Parameter(Mandatory=$true)]
    [string]$ServiceUrl
)

$url = $ServiceUrl.TrimEnd("/")

function Test-Endpoint($method, $path, $expectedStatus, $body = $null) {
    try {
        $params = @{ Uri = "$url$path"; Method = $method; TimeoutSec = 30 }
        if ($body) { $params.Body = $body | ConvertTo-Json; $params.ContentType = "application/json" }
        $resp = Invoke-WebRequest @params -ErrorAction SilentlyContinue
        $status = $resp.StatusCode
    } catch {
        $status = $_.Exception.Response.StatusCode.value__
    }
    $ok  = $status -eq $expectedStatus
    $col = if ($ok) { "Green" } else { "Red" }
    $sym = if ($ok) { "✓" } else { "✗" }
    Write-Host "  $sym $method $path → $status (expected $expectedStatus)" -ForegroundColor $col
}

Write-Host "`nSmoke test: $url" -ForegroundColor Cyan
Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

Test-Endpoint "GET"  "/"           200
Test-Endpoint "GET"  "/health"     200
Test-Endpoint "GET"  "/error"      500
Test-Endpoint "GET"  "/db-timeout" 200   # probabilistic — usually 200
Test-Endpoint "POST" "/webhook"    200  @{ event_type = "order.created"; order_id = "ord-123" }
Test-Endpoint "GET"  "/chaos"      200   # probabilistic — may be 500
Test-Endpoint "GET"  "/slow"       200   # will take 6-9s

Write-Host "`nAll done. Check Log Explorer for errors and latency spikes." -ForegroundColor Cyan
Write-Host "https://console.cloud.google.com/logs/query?project=$((gcloud config get-value project 2>$null))"
