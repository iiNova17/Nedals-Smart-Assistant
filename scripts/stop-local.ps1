$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimePath = Join-Path $projectRoot 'data\runtime.json'
if (-not (Test-Path -LiteralPath $runtimePath)) { throw 'No local process record found.' }
$records = @(Get-Content -LiteralPath $runtimePath -Raw | ConvertFrom-Json)
foreach ($record in $records) {
    if (-not $record.id -or -not $record.started) { throw 'Invalid process record; refusing to stop unrelated processes.' }
    $process = Get-Process -Id $record.id -ErrorAction SilentlyContinue
    if ($process -and $process.StartTime.ToUniversalTime().Ticks.ToString() -eq $record.started) {
        Stop-Process -Id $process.Id
    }
}
Write-Output 'Recorded local services stopped. WhatsApp login is preserved.'
