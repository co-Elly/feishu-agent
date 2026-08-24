$ErrorActionPreference = "Stop"
$projectDir = Split-Path -Parent $PSScriptRoot
$source = Join-Path $projectDir "hermes_plugin\orchestration_feishu"
$wslSource = (wsl.exe -e wslpath -a ($source -replace '\\','/')).Trim()
wsl.exe -e bash -lc "set -e; mkdir -p /root/.hermes/plugins; rm -rf /root/.hermes/plugins/orchestration_feishu; cp -a '$wslSource' /root/.hermes/plugins/orchestration_feishu"
wsl.exe -e bash -lc "cd /root/.hermes/hermes-agent && ./venv/bin/python -m hermes_cli.main plugins enable orchestration-feishu"
if ($LASTEXITCODE -ne 0) { throw "Hermes plugin enable failed with exit code $LASTEXITCODE" }
Write-Output "Hermes Feishu orchestration delegate installed and enabled."
