<#
.SYNOPSIS
    Runs the Flyconomy bot from the local virtual environment.

.DESCRIPTION
    Starts the bot with the settings in .env. Press Ctrl+C to stop it.

.PARAMETER LogLevel
    Overrides FLYCONOMY_LOG_LEVEL for this run. Use DEBUG to trace gateway
    activity while troubleshooting.

.EXAMPLE
    .\scripts\run.ps1

.EXAMPLE
    .\scripts\run.ps1 -LogLevel DEBUG
#>
[CmdletBinding()]
param(
    [ValidateSet('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL')]
    [string]$LogLevel
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $RepoRoot '.venv\Scripts\python.exe'

if (-not (Test-Path $VenvPython)) {
    throw 'No virtual environment found. Run .\scripts\setup.ps1 first.'
}

if (-not (Test-Path (Join-Path $RepoRoot '.env'))) {
    throw 'No .env file found. Run .\scripts\setup.ps1, then add your bot token to .env.'
}

if ($LogLevel) {
    $env:FLYCONOMY_LOG_LEVEL = $LogLevel
}

# The bot reads .env relative to the working directory.
Push-Location $RepoRoot
try {
    & $VenvPython -m flyconomy
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
