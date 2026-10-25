# Start one hidden local server and open the operator console when HTTP is available.
$ErrorActionPreference = 'Stop'
$consoleRoot = Split-Path -Parent $PSScriptRoot
$consoleUrl = 'http://127.0.0.1:8765'
$consolePython = Join-Path $consoleRoot '.venv\Scripts\python.exe'
$consoleLogs = Join-Path $consoleRoot 'workspace\console'

function Test-ConsoleHttp {
    try {
        $response = Invoke-RestMethod -Uri "$consoleUrl/api/status" -TimeoutSec 2
        return $response.service -eq 'signal-court-console'
    } catch { return $false }
}

function Open-ConsoleBrowser {
    $chromeCandidates = @(
        $env:CHROME_PATH,
        "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
        "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
        "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
    )
    $chrome = $chromeCandidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
    if ($chrome) { Start-Process -FilePath $chrome -ArgumentList $consoleUrl -WindowStyle Normal }
    else { Start-Process $consoleUrl }
}

if (Test-ConsoleHttp) { Open-ConsoleBrowser; exit 0 }
$portProbe = New-Object System.Net.Sockets.TcpClient
try {
    $portProbe.Connect('127.0.0.1', 8765)
    throw 'Port 8765 is occupied by another application. Close it and double-click Signal Court.bat again.'
} catch [System.Net.Sockets.SocketException] {
    # No listener: start the console below.
} finally { $portProbe.Dispose() }

if (-not (Test-Path -LiteralPath $consolePython)) { throw 'The .venv Python environment is missing. Run uv sync once.' }
New-Item -ItemType Directory -Path $consoleLogs -Force | Out-Null
$consoleProcess = Start-Process -FilePath $consolePython -ArgumentList '-m', 'signalcourt.web' `
    -WorkingDirectory $consoleRoot -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $consoleLogs 'server.out.log') `
    -RedirectStandardError (Join-Path $consoleLogs 'server.err.log')
$consoleDeadline = (Get-Date).AddSeconds(120)
do {
    if (Test-ConsoleHttp) { Open-ConsoleBrowser; exit 0 }
    if ($consoleProcess.HasExited) { throw "The console stopped. See workspace\console\server.err.log." }
    Start-Sleep -Milliseconds 300
} while ((Get-Date) -lt $consoleDeadline)
throw 'The console did not answer within two minutes. See workspace\console\server.err.log.'
