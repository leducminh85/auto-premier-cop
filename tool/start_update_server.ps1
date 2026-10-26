param([int]$Port = 8765)
$ErrorActionPreference = 'Stop'
$repositoryPath = Split-Path -Parent $PSScriptRoot
$logDirectory = Join-Path $repositoryPath 'build'
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
try {
    $response = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/healthz" -UseBasicParsing -TimeoutSec 2
    if ($response.Content -match 'Dumi Cut Room update server is running') {
        Write-Output "Update server is already running on port $Port."
        exit 0
    }
} catch {
    # Start our server if it is not already answering.
}
$pythonPath = (Get-Command python -ErrorAction Stop).Source
$serverScript = Join-Path $PSScriptRoot 'serve_updates.py'
$serverArguments = '"{0}" --port {1}' -f $serverScript, $Port
$serverProcess = Start-Process -FilePath $pythonPath -ArgumentList $serverArguments -WorkingDirectory $repositoryPath -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logDirectory 'update-server.log') -RedirectStandardError (Join-Path $logDirectory 'update-server-error.log') -PassThru
for ($attempt = 0; $attempt -lt 10; $attempt++) {
    Start-Sleep -Milliseconds 300
    if ($serverProcess.HasExited) {
        throw 'Update server could not start. See build/update-server-error.log.'
    }
    try {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/healthz" -UseBasicParsing -TimeoutSec 2
        if ($response.Content -match 'Dumi Cut Room update server is running') {
            Write-Output "Update server started on port $Port (PID $($serverProcess.Id))."
            exit 0
        }
    } catch {}
}
throw 'Update server did not become ready. See build/update-server-error.log.'
