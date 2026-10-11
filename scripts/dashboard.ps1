<#
.SYNOPSIS
    Runs the owner's dashboard from the local virtual environment.

.DESCRIPTION
    Serves the dashboard at http://127.0.0.1:8766 (FLYCONOMY_DASHBOARD_PORT)
    and forwards its requests to the bot's API at FLYCONOMY_DASHBOARD_API_URL,
    using FLYCONOMY_API_TOKEN from .env. Press Ctrl+C to stop it.

.EXAMPLE
    .\scripts\dashboard.ps1
#>
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $RepoRoot '.venv\Scripts\python.exe'

if (-not (Test-Path $VenvPython)) {
    throw 'No virtual environment found. Run .\scripts\setup.ps1 first.'
}

if (-not (Test-Path (Join-Path $RepoRoot '.env'))) {
    throw 'No .env file found. Run .\scripts\setup.ps1, then add FLYCONOMY_API_TOKEN to .env.'
}

# The dashboard reads .env relative to the working directory.
Push-Location $RepoRoot
try {
    & $VenvPython -m flyconomy.dashboard
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
