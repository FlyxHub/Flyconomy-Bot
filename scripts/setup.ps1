<#
.SYNOPSIS
    Sets up a local development environment for the Flyconomy bot on Windows.

.DESCRIPTION
    Creates a virtual environment in .venv, installs the bot and its development
    dependencies in editable mode, creates a .env file from .env.example, and
    moves a version 1 database into place if one is found.

.PARAMETER Python
    The Python launcher or executable to build the virtual environment with.
    Defaults to the newest interpreter the py launcher knows about.

.PARAMETER Force
    Deletes an existing .venv before creating a new one.

.EXAMPLE
    .\scripts\setup.ps1

.EXAMPLE
    .\scripts\setup.ps1 -Force
#>
[CmdletBinding()]
param(
    [string]$Python,
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$VenvPath = Join-Path $RepoRoot '.venv'
$VenvPython = Join-Path $VenvPath 'Scripts\python.exe'
$MinimumVersion = [version]'3.13'

function Write-Step {
    param([string]$Message)
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Get-InterpreterVersion {
    <#
        Returns the [version] reported by an interpreter, or $null if it cannot
        be run. Probing must never abort the script, so native command failures
        are downgraded to a null result here.
    #>
    param(
        [Parameter(Mandatory)][string]$Exe,
        [string[]]$ExeArgs = @()
    )

    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'SilentlyContinue'
    try {
        # `--version` avoids passing a quoted `-c` snippet, which PowerShell
        # mangles when it hands arguments to a native executable.
        $output = & $Exe @ExeArgs '--version'
        if ($LASTEXITCODE -ne 0 -or -not $output) { return $null }
        $text = ($output | Select-Object -First 1)
        if ($text -notmatch '(\d+)\.(\d+)(?:\.(\d+))?') { return $null }
        return [version]"$($Matches[1]).$($Matches[2])"
    }
    catch {
        return $null
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }
}

function Resolve-Python {
    <#
        Returns the interpreter to build the virtual environment with, or throws
        with install instructions when nothing suitable is present.
    #>
    if ($Python) {
        $candidates = @(, @($Python, @()))
    }
    else {
        # The py launcher resolves -3 to the newest installed interpreter, so a
        # future Python release needs no change here.
        $candidates = @(
            @('py', @('-3')),
            @('python', @()),
            @('python3', @())
        )
    }

    foreach ($candidate in $candidates) {
        $exe = $candidate[0]
        $exeArgs = $candidate[1]

        $command = Get-Command $exe -ErrorAction SilentlyContinue
        if (-not $command) { continue }

        # The Microsoft Store stub named python.exe blocks waiting on the Store
        # instead of running Python, so never probe it.
        if ($command.Source -and $command.Source -like '*\WindowsApps\*') {
            Write-Verbose "Skipping the Microsoft Store stub at $($command.Source)"
            continue
        }

        $version = Get-InterpreterVersion -Exe $exe -ExeArgs $exeArgs
        if ($version -and $version -ge $MinimumVersion) {
            Write-Step "Using Python $version from '$exe $($exeArgs -join ' ')'".Trim()
            return @{ Exe = $exe; Args = $exeArgs }
        }
        if ($version) {
            Write-Verbose "Skipping Python $version at $($command.Source); need $MinimumVersion or newer"
        }
    }

    throw "Python $MinimumVersion or newer was not found. Install it from https://www.python.org/downloads/, then re-run this script. If Python is installed somewhere unusual, pass its path with -Python."
}

# --------------------------------------------------------------- virtualenv --

if ($Force -and (Test-Path $VenvPath)) {
    Write-Step 'Removing the existing virtual environment'
    Remove-Item -Recurse -Force $VenvPath
}

if (-not (Test-Path $VenvPython)) {
    $interpreter = Resolve-Python
    Write-Step "Creating a virtual environment in $VenvPath"
    & $interpreter.Exe @($interpreter.Args) -m venv $VenvPath
    if ($LASTEXITCODE -ne 0) { throw 'Failed to create the virtual environment.' }
}
else {
    Write-Step 'Reusing the existing virtual environment'
}

# ------------------------------------------------------------- dependencies --

Write-Step 'Upgrading pip'
& $VenvPython -m pip install --upgrade pip --quiet
if ($LASTEXITCODE -ne 0) { throw 'Failed to upgrade pip.' }

Write-Step 'Installing the bot and its development dependencies'
& $VenvPython -m pip install --editable "$RepoRoot[dev]"
if ($LASTEXITCODE -ne 0) { throw 'Failed to install dependencies.' }

# --------------------------------------------------------------- local data --

$EnvFile = Join-Path $RepoRoot '.env'
$EnvExample = Join-Path $RepoRoot '.env.example'
if (-not (Test-Path $EnvFile)) {
    Write-Step 'Creating .env from .env.example'
    Copy-Item $EnvExample $EnvFile
    Write-Host '    Edit .env and set FLYCONOMY_DISCORD_TOKEN before running the bot.' -ForegroundColor Yellow
}
else {
    Write-Step '.env already exists; leaving it alone'
}

$DataDir = Join-Path $RepoRoot 'data'
if (-not (Test-Path $DataDir)) {
    New-Item -ItemType Directory -Path $DataDir | Out-Null
}

$LegacyDb = Join-Path $RepoRoot 'bot.db'
$TargetDb = Join-Path $DataDir 'bot.db'
if ((Test-Path $LegacyDb) -and -not (Test-Path $TargetDb)) {
    Write-Step 'Found a version 1 database in the repository root'
    Copy-Item $LegacyDb $TargetDb
    Write-Host "    Copied bot.db to data\bot.db. Migrations run on the next start." -ForegroundColor Yellow
    Write-Host "    The original file was left untouched as a backup." -ForegroundColor Yellow
}

# -------------------------------------------------------------------- done ---

Write-Host ''
Write-Host 'Setup complete.' -ForegroundColor Green
Write-Host ''
Write-Host '  1. Put your bot token in .env'
Write-Host '  2. Run the checks:  .\scripts\check.ps1'
Write-Host '  3. Start the bot:   .\scripts\run.ps1'
Write-Host ''
