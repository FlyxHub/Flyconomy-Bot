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

# Tries the host's package manager. This is the right source on a supported,
# in-repo distro, but it is a no-op failure on an EOL release like Ubuntu
# 23.04 (lunar), whose apt sources have moved to old-releases.ubuntu.com and
# never carried docker-compose-plugin in the first place -- that package only
# ever came from Docker's own apt repo, not Ubuntu's.
install_compose_plugin_via_package_manager() {
    if command -v apt-get >/dev/null 2>&1; then
        $SUDO apt-get update && $SUDO apt-get install -y docker-compose-plugin
    elif command -v dnf >/dev/null 2>&1; then
        $SUDO dnf install -y docker-compose-plugin
    elif command -v yum >/dev/null 2>&1; then
        $SUDO yum install -y docker-compose-plugin
    elif command -v pacman >/dev/null 2>&1; then
        $SUDO pacman -Sy --noconfirm docker-compose
    else
        return 1
    fi
}

# Falls back to Docker's documented manual install: drop the compose binary
# straight into a cli-plugins directory. Works regardless of what (if
# anything) the distro's package repos carry.
install_compose_plugin_from_binary() {
    step "Installing the Compose plugin binary directly from GitHub"

    local downloader
    if command -v curl >/dev/null 2>&1; then
        downloader="curl"
    elif command -v wget >/dev/null 2>&1; then
        downloader="wget"
    else
        die "Neither curl nor wget is available to download the Compose plugin. Install one, then re-run this script."
    fi

    local arch
    case "$(uname -m)" in
        x86_64 | amd64) arch="x86_64" ;;
        aarch64 | arm64) arch="aarch64" ;;
        armv7l | armv6l) arch="armv7" ;;
        *) die "Unsupported architecture $(uname -m). Install the Compose plugin manually: https://docs.docker.com/compose/install/linux/#install-the-plugin-manually" ;;
    esac

    local version="v2.29.7"
    local latest=""
    if [ "$downloader" = "curl" ]; then
        latest="$(curl -fsSL https://api.github.com/repos/docker/compose/releases/latest 2>/dev/null | grep -m1 '"tag_name"' | sed -E 's/.*"tag_name": *"([^"]+)".*/\1/')" || true
    else
        latest="$(wget -qO- https://api.github.com/repos/docker/compose/releases/latest 2>/dev/null | grep -m1 '"tag_name"' | sed -E 's/.*"tag_name": *"([^"]+)".*/\1/')" || true
    fi
    if [ -n "$latest" ]; then
        version="$latest"
    fi

    local dest
    if [ "$(id -u)" -eq 0 ]; then
        dest="/usr/local/lib/docker/cli-plugins"
    else
        dest="$HOME/.docker/cli-plugins"
    fi
    mkdir -p "$dest"

    local url="https://github.com/docker/compose/releases/download/${version}/docker-compose-linux-${arch}"
    step "Downloading Docker Compose $version for linux/$arch"
    if [ "$downloader" = "curl" ]; then
        curl -fsSL "$url" -o "$dest/docker-compose"
    else
        wget -qO "$dest/docker-compose" "$url"
    fi
    chmod +x "$dest/docker-compose"
}

if ! docker compose version >/dev/null 2>&1; then
    step "Docker Compose plugin not found; installing it"

    if [ "$(id -u)" -eq 0 ]; then
        SUDO=""
    elif command -v sudo >/dev/null 2>&1; then
        SUDO="sudo"
    else
        SUDO=""
        warn "Not running as root and sudo is unavailable; installing the Compose plugin for the current user only."
    fi

    if ! install_compose_plugin_via_package_manager; then
        warn "The package manager could not install docker-compose-plugin (its repository may not be configured -- common on an end-of-life release like Ubuntu 23.04/lunar). Falling back to a direct binary install."
        install_compose_plugin_from_binary
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
