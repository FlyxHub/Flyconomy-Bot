#!/usr/bin/env bash
#
# Deploys the Flyconomy bot on a Linux host with Docker Compose.
#
# Installs the Docker Compose plugin if it is missing, creates .env from
# .env.example on first run, restricts its permissions, and starts the bot
# with `docker compose up -d --build`.
#
# Usage:
#   ./scripts/deploy.sh
#
# Re-run it any time to rebuild and restart after `git pull`.

set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." >/dev/null 2>&1 && pwd)"
cd "$REPO_ROOT"

step() {
    printf '\033[36m==>\033[0m %s\n' "$1"
}

warn() {
    printf '\033[33m    %s\033[0m\n' "$1"
}

die() {
    printf '\033[31mError:\033[0m %s\n' "$1" >&2
    exit 1
}

# ------------------------------------------------------------------ docker --

if ! command -v docker >/dev/null 2>&1; then
    die "Docker is not installed. Follow https://docs.docker.com/engine/install/ for your distribution, then re-run this script."
fi

if ! docker compose version >/dev/null 2>&1; then
    step "Docker Compose plugin not found; installing it"

    if [ "$(id -u)" -eq 0 ]; then
        SUDO=""
    elif command -v sudo >/dev/null 2>&1; then
        SUDO="sudo"
    else
        die "The Compose plugin is missing and this script has no way to install it without root. Install docker-compose-plugin manually, then re-run this script."
    fi

    if command -v apt-get >/dev/null 2>&1; then
        $SUDO apt-get update
        $SUDO apt-get install -y docker-compose-plugin
    elif command -v dnf >/dev/null 2>&1; then
        $SUDO dnf install -y docker-compose-plugin
    elif command -v yum >/dev/null 2>&1; then
        $SUDO yum install -y docker-compose-plugin
    elif command -v pacman >/dev/null 2>&1; then
        $SUDO pacman -Sy --noconfirm docker-compose
    else
        die "Could not detect a supported package manager (apt, dnf, yum, pacman). Install the Compose plugin manually: https://docs.docker.com/compose/install/linux/"
    fi

    if ! docker compose version >/dev/null 2>&1; then
        die "The Compose plugin still is not available after installation. Install it manually: https://docs.docker.com/compose/install/linux/"
    fi
fi

step "Using $(docker compose version --short 2>/dev/null || docker compose version)"

# --------------------------------------------------------------------- env --

if [ ! -f .env ]; then
    step "Creating .env from .env.example"
    cp .env.example .env
    warn "Edit .env and set FLYCONOMY_DISCORD_TOKEN before the bot can log in."
else
    step ".env already exists; leaving it alone"
fi

chmod 600 .env

if grep -q '^FLYCONOMY_DISCORD_TOKEN=\s*$' .env; then
    warn "FLYCONOMY_DISCORD_TOKEN is empty in .env. The bot will exit until it is set."
fi

# ------------------------------------------------------------------- start --

step "Building and starting the bot"
docker compose up -d --build

step "Deployed. Follow the logs with: docker compose logs -f"
