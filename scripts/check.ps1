<#
.SYNOPSIS
    Runs the lint, type, and test checks against the local virtual environment.

.DESCRIPTION
    Runs the same checks as continuous integration: ruff for linting and
    formatting, mypy for type checking, and pytest for the test suite. Every
    check runs even if an earlier one fails, so a single pass reports every
    problem.

.PARAMETER Fix
    Applies ruff's automatic fixes and formatting instead of only reporting.

.PARAMETER Coverage
    Adds a coverage report to the pytest run.

.EXAMPLE
    .\scripts\check.ps1

.EXAMPLE
    .\scripts\check.ps1 -Fix -Coverage
#>
[CmdletBinding()]
param(
    [switch]$Fix,
    [switch]$Coverage
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $RepoRoot '.venv\Scripts\python.exe'

if (-not (Test-Path $VenvPython)) {
    throw 'No virtual environment found. Run .\scripts\setup.ps1 first.'
}

$Failures = [System.Collections.Generic.List[string]]::new()

function Invoke-Check {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string[]]$Arguments
    )
    Write-Host "==> $Name" -ForegroundColor Cyan
    & $VenvPython @Arguments
    if ($LASTEXITCODE -ne 0) {
        $Failures.Add($Name)
        Write-Host "    $Name failed" -ForegroundColor Red
    }
    Write-Host ''
}

Push-Location $RepoRoot
try {
    if ($Fix) {
        Invoke-Check -Name 'ruff format' -Arguments @('-m', 'ruff', 'format', '.')
        Invoke-Check -Name 'ruff check --fix' -Arguments @('-m', 'ruff', 'check', '--fix', '.')
    }
    else {
        Invoke-Check -Name 'ruff format --check' -Arguments @('-m', 'ruff', 'format', '--check', '.')
        Invoke-Check -Name 'ruff check' -Arguments @('-m', 'ruff', 'check', '.')
    }

    Invoke-Check -Name 'mypy' -Arguments @('-m', 'mypy')

    $PytestArgs = @('-m', 'pytest')
    if ($Coverage) {
        $PytestArgs += @('--cov', '--cov-report=term-missing')
    }
    Invoke-Check -Name 'pytest' -Arguments $PytestArgs
}
finally {
    Pop-Location
}

if ($Failures.Count -gt 0) {
    Write-Host "Failed checks: $($Failures -join ', ')" -ForegroundColor Red
    exit 1
}

Write-Host 'All checks passed.' -ForegroundColor Green
exit 0
