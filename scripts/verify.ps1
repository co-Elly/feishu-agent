$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $projectDir ".tools\test-venv\Scripts\python.exe"
$runId = "{0}-{1}" -f $PID, (Get-Date -Format "yyyyMMddHHmmssfff")
$baseTemp = Join-Path $projectDir ("workspace\pytest-runs\" + $runId)
$testRoot = Join-Path $projectDir "tests"

if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
    Write-Error "Project test interpreter not found: $pythonExe"
    exit 2
}

New-Item -ItemType Directory -Path $baseTemp -Force | Out-Null
Set-Location -LiteralPath $projectDir
& $pythonExe -m pytest -q $testRoot --basetemp=$baseTemp @args
$testExitCode = $LASTEXITCODE
try {
    if (Test-Path -LiteralPath $baseTemp) {
        Remove-Item -LiteralPath $baseTemp -Recurse -Force -ErrorAction Stop
    }
} catch {
    Write-Warning "Unable to remove test temp directory: $baseTemp"
}
exit $testExitCode
