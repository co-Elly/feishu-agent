# Feishu bot guardian: user-session startup and automatic crash recovery.
$ErrorActionPreference = "Continue"
$mutex = New-Object System.Threading.Mutex($false, "Local\FeishuAgentGuardian")
if (-not $mutex.WaitOne(0, $false)) {
    exit 0
}
$utf8 = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$userOpenAIKey = [Environment]::GetEnvironmentVariable("OPENAI_API_KEY", "User")
if (-not [string]::IsNullOrWhiteSpace($userOpenAIKey)) {
    $env:OPENAI_API_KEY = $userOpenAIKey
}
$env:FEISHU_MAX_WORKERS = "4"
# P4: Codex quota exhausted (resets 22:50) - run roundtable as pm+arch duo until re-enabled.
$env:FEISHU_ROUNDTABLE_ORDER = "pm,arch"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUNBUFFERED = "1"
$projectDir = $PSScriptRoot
$pythonExe = Join-Path $projectDir ".tools\test-venv\Scripts\python.exe"
$logPath = Join-Path $projectDir "bot_service_v2.log"
$workspaceDir = Join-Path $projectDir "workspace"
$statusPath = Join-Path $workspaceDir "guardian_status.json"
$plannedShutdownPath = Join-Path $workspaceDir "planned_shutdown.json"
$logArchiveDir = Join-Path $workspaceDir "log-archive"
$restartHistory = [System.Collections.Generic.List[datetime]]::new()
$maxRestartsPerHour = 5
$lastExitCode = $null
$lastExitReason = $null
$lastExitedAt = $null

New-Item -ItemType Directory -Path $workspaceDir -Force | Out-Null

function Rotate-GuardianLog {
    if ((Test-Path -LiteralPath $logPath) -and (Get-Item -LiteralPath $logPath).Length -ge 5MB) {
        New-Item -ItemType Directory -Path $logArchiveDir -Force | Out-Null
        $archive = Join-Path $logArchiveDir ("bot_service_v2-{0}.log" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
        Move-Item -LiteralPath $logPath -Destination $archive -Force
        Get-ChildItem -LiteralPath $logArchiveDir -Filter "bot_service_v2-*.log" |
            Sort-Object LastWriteTime -Descending | Select-Object -Skip 10 |
            Remove-Item -Force -ErrorAction SilentlyContinue
    }
}

function Write-GuardianStatus([hashtable]$status) {
    $tempPath = "$statusPath.tmp"
    $status | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $tempPath -Encoding UTF8
    Move-Item -LiteralPath $tempPath -Destination $statusPath -Force
}

function Get-RedactedLogLine([string]$line) {
    $safe = $line -replace '(?i)(access_key|ticket|api_key|app_secret|access_token|token)=([^&\s]+)', '$1=***'
    $safe = $safe -replace '(?i)Bearer\s+[A-Za-z0-9._~+/=-]+', 'Bearer ***'
    return $safe -replace '(?i)\b(?:sk|rk|pk)-[A-Za-z0-9._~+/=-]{12,}', '***'
}

while ($true) {
    Rotate-GuardianLog
    Set-Location -LiteralPath $projectDir
    $startedAt = Get-Date
    Write-GuardianStatus @{
        state = "running"
        started_at = $startedAt.ToString("o")
        python = $pythonExe
        restarts_last_hour = $restartHistory.Count
        exit_code = $lastExitCode
        exit_reason = $lastExitReason
        exited_at = $lastExitedAt
    }
    "[$($startedAt.ToString('yyyy-MM-dd HH:mm:ss'))] starting bot with $pythonExe" | Add-Content -LiteralPath $logPath -Encoding UTF8
    & $pythonExe bot.py 2>&1 | ForEach-Object { Get-RedactedLogLine $_.ToString() | Add-Content -LiteralPath $logPath -Encoding UTF8 }
    $exitCode = $LASTEXITCODE
    $exitedAt = Get-Date
    $runtimeSeconds = [math]::Round(($exitedAt - $startedAt).TotalSeconds, 1)
    $planned = $false
    if (Test-Path -LiteralPath $plannedShutdownPath) {
        try {
            $plannedData = Get-Content -LiteralPath $plannedShutdownPath -Raw | ConvertFrom-Json
            $plannedAt = [datetime]::Parse($plannedData.requested_at)
            $planned = (($exitedAt - $plannedAt).TotalMinutes -ge 0 -and ($exitedAt - $plannedAt).TotalMinutes -le 5)
        } catch { $planned = $false }
        Remove-Item -LiteralPath $plannedShutdownPath -Force -ErrorAction SilentlyContinue
    }
    $reason = if ($planned) { "planned_restart" } elseif ($exitCode -eq 0) { "clean_exit" } elseif ($exitCode -eq 15) { "terminated" } else { "crash_or_external_stop" }
    $lastExitCode = $exitCode
    $lastExitReason = $reason
    $lastExitedAt = $exitedAt.ToString("o")
    if (-not $planned) { $restartHistory.Add($exitedAt) }
    for ($i = $restartHistory.Count - 1; $i -ge 0; $i--) {
        if (($exitedAt - $restartHistory[$i]).TotalHours -gt 1) { $restartHistory.RemoveAt($i) }
    }
    $halted = $restartHistory.Count -ge $maxRestartsPerHour
    Write-GuardianStatus @{
        state = $(if ($halted) { "halted" } else { "restarting" })
        started_at = $startedAt.ToString("o")
        exited_at = $exitedAt.ToString("o")
        exit_code = $exitCode
        exit_reason = $reason
        runtime_seconds = $runtimeSeconds
        restarts_last_hour = $restartHistory.Count
        alert = $(if ($halted) { "restart_limit_reached" } elseif ($restartHistory.Count -ge 3) { "restart_warning" } else { $null })
    }
    "[$($exitedAt.ToString('yyyy-MM-dd HH:mm:ss'))] bot exited ($exitCode, $reason) after ${runtimeSeconds}s" | Add-Content -LiteralPath $logPath -Encoding UTF8
    if ($halted) {
        "[$($exitedAt.ToString('yyyy-MM-dd HH:mm:ss'))] guardian halted after $($restartHistory.Count) exits in one hour; manual restart required" | Add-Content -LiteralPath $logPath -Encoding UTF8
        exit 1
    }
    "[$($exitedAt.ToString('yyyy-MM-dd HH:mm:ss'))] restarting in 5s" | Add-Content -LiteralPath $logPath -Encoding UTF8
    Start-Sleep -Seconds 5
}
