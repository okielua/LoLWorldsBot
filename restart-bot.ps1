$ErrorActionPreference = 'Stop'
$botRoot = $PSScriptRoot
$botPython = Join-Path $botRoot '.venv\Scripts\python.exe'
$botScript = Join-Path $botRoot 'bot.py'
if (-not (Test-Path -LiteralPath $botPython) -or -not (Test-Path -LiteralPath $botScript)) {
    throw 'Run this file from the LoLWorldsBot folder with its .venv installed.'
}

# Match this project's virtual environment, then its Python launcher children.
$pythonProcesses = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" )
$botProcesses = @($pythonProcesses | Where-Object {
    $_.ExecutablePath -eq $botPython -and $_.CommandLine -match '(?i)(?:^|[\s"\\])bot\.py(?:[\s"]|$)'
})
$botProcessIds = @($botProcesses | ForEach-Object { $_.ProcessId })
$botChildren = @($pythonProcesses | Where-Object {
    $_.ParentProcessId -in $botProcessIds -and $_.CommandLine -match '(?i)(?:^|[\s"\\])bot\.py(?:[\s"]|$)'
})
foreach ($botProcess in @($botChildren) + @($botProcesses)) {
    $runningBot = Get-Process -Id $botProcess.ProcessId -ErrorAction SilentlyContinue
    if ($runningBot) {
        try {
            Stop-Process -InputObject $runningBot -Force
            $runningBot.WaitForExit()
        } catch {
            # The virtual-environment launcher can exit with its child.
            if (Get-Process -Id $botProcess.ProcessId -ErrorAction SilentlyContinue) {
                throw
            }
        }
    }
}

$botLogs = Join-Path $botRoot '.logs'
New-Item -ItemType Directory -Path $botLogs -Force | Out-Null
$botRunId = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
$botErrorLog = Join-Path $botLogs "$botRunId.err.log"
$botOutputLog = Join-Path $botLogs "$botRunId.out.log"
$startedBot = Start-Process -FilePath $botPython -ArgumentList '-u', 'bot.py' `
    -WorkingDirectory $botRoot -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $botOutputLog -RedirectStandardError $botErrorLog
Write-Output "Started LoLWorldsBot (launcher PID $($startedBot.Id))."
Write-Output "Logs: $botErrorLog"
