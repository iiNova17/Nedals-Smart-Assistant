$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$nodePath = (Get-Command node -ErrorAction Stop).Source
$runtimePath = Join-Path $projectRoot 'data\runtime.json'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Install the Python environment first.' }
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'data\team.json'))) {
    throw 'Run python -m app.setup first; see README.md.'
}
foreach ($port in @(8000, 8787)) {
    $probe = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $port)
    try { $probe.Start() } catch { throw "Port $port is occupied. Stop the existing local services first." }
    finally { $probe.Stop() }
}
$launched = @()
try {
    $backend = Start-Process -FilePath $pythonPath -ArgumentList '-m','uvicorn','app.main:create_app','--factory','--host','127.0.0.1','--port','8000','--workers','1','--no-access-log' -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $projectRoot 'data\backend.stdout.log') -RedirectStandardError (Join-Path $projectRoot 'data\backend.stderr.log')
    $launched += $backend
    $bridge = Start-Process -FilePath $nodePath -ArgumentList 'whatsapp/src/main.mjs' -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $projectRoot 'data\bridge.stdout.log') -RedirectStandardError (Join-Path $projectRoot 'data\bridge.stderr.log')
    $launched += $bridge
    $records = @($launched | ForEach-Object {
        @{ id = $_.Id; started = $_.StartTime.ToUniversalTime().Ticks.ToString() }
    })
    $records | ConvertTo-Json | Set-Content -LiteralPath $runtimePath
    Start-Sleep -Seconds 3
    foreach ($process in $launched) {
        $process.Refresh()
        if ($process.HasExited) { throw 'A service exited. Inspect its local log under data/.' }
    }
    Write-Output 'Services started. WhatsApp pairing/status: http://127.0.0.1:8787/'
} catch {
    foreach ($process in $launched) {
        if (-not $process.HasExited) { Stop-Process -Id $process.Id -ErrorAction SilentlyContinue }
    }
    throw
}
