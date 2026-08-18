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
$env:PYTHONIOENCODING = "utf-8"
$pythonExe = "F:\anaconda\python.exe"
$projectDir = $PSScriptRoot
$logPath = Join-Path $projectDir "bot_service_v2.log"

function Get-RedactedLogLine([string]$line) {
    $safe = $line -replace '(?i)(access_key|ticket|api_key|app_secret|access_token|token)=([^&\s]+)', '$1=***'
    $safe = $safe -replace '(?i)Bearer\s+[A-Za-z0-9._~+/=-]+', 'Bearer ***'
    return $safe -replace '(?i)\b(?:sk|rk|pk)-[A-Za-z0-9._~+/=-]{12,}', '***'
}

while ($true) {
    Set-Location -LiteralPath $projectDir
    "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] starting bot" | Add-Content -LiteralPath $logPath -Encoding UTF8
    & $pythonExe bot.py 2>&1 | ForEach-Object { Get-RedactedLogLine $_.ToString() | Add-Content -LiteralPath $logPath -Encoding UTF8 }
    $exitCode = $LASTEXITCODE
    "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] bot exited ($exitCode); restarting in 5s" | Add-Content -LiteralPath $logPath -Encoding UTF8
    Start-Sleep -Seconds 5
}
