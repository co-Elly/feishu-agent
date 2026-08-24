$ErrorActionPreference = "Stop"
$projectDir = Split-Path -Parent $PSScriptRoot
$workspaceDir = Join-Path $projectDir "workspace"
$marker = Join-Path $workspaceDir "planned_shutdown.json"
New-Item -ItemType Directory -Path $workspaceDir -Force | Out-Null
@{
    requested_at = (Get-Date).ToString("o")
    reason = $(if ($args.Count) { $args -join " " } else { "planned deployment restart" })
    requested_by = [Environment]::UserName
} | ConvertTo-Json | Set-Content -LiteralPath $marker -Encoding UTF8

$pythonPath = [System.IO.Path]::GetFullPath((Join-Path $projectDir ".tools\test-venv\Scripts\python.exe"))
$processes = @(Get-CimInstance Win32_Process | Where-Object {
    $command = [string]$_.CommandLine
    $usesProjectPython = ($_.ExecutablePath -and
        [System.IO.Path]::GetFullPath($_.ExecutablePath) -eq $pythonPath) -or
        $command.IndexOf($pythonPath, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
    $_.Name -eq "python.exe" -and $usesProjectPython -and
        $command -match '(?i)(^|\s|[\\/])bot\.py(?:\s|$)'
})
foreach ($process in $processes) {
    Stop-Process -Id $process.ProcessId -Force
}
Write-Output "Planned restart requested for $($processes.Count) bot process(es)."
