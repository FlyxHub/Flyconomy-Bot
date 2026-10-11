"""The owner's dashboard, run on their own machine rather than on the bot host.

Run with ``python -m flyconomy.dashboard`` and open the address it prints. It
serves one page on loopback and forwards that page's ``/api`` calls to the
bot's API (see :mod:`flyconomy.cogs.api`), adding the token on the way. Going
through this rather than calling the bot from the browser keeps the token out
of the page and makes every request same-origin, so there is no CORS to get
wrong.

It shares the bot's ``.env`` and its ``FLYCONOMY_API_TOKEN``, but nothing else:
it needs no Discord token and never opens the database.
"""

from __future__ import annotations

import logging
import sys
from importlib import resources
from typing import Final

import aiohttp
from aiohttp import web
from pydantic import Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from flyconomy.logging_config import configure_logging

log = logging.getLogger("flyconomy.dashboard")

PAGE_RESOURCE: Final = "data/dashboard.html"

_EXIT_CONFIG_ERROR: Final = 2


class DashboardSettings(BaseSettings):
    """What the dashboard reads from the environment or ``.env``."""

    model_config = SettingsConfigDict(
        env_prefix="FLYCONOMY_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    api_token: SecretStr = Field(min_length=16, description="The bot's FLYCONOMY_API_TOKEN.")
    dashboard_api_url: str = Field(
        default="http://127.0.0.1:8765",
        description="Where the bot's API answers, as seen from this machine.",
    )
    dashboard_port: int = Field(default=8766, gt=0, lt=65536)


def make_app(settings: DashboardSettings) -> web.Application:
    """Build the dashboard: the page, and a pass-through to the bot's API.

    Args:
        settings: Where the bot is and the token it expects.

    Returns:
        The application, ready to serve.
    """
    page = (resources.files("flyconomy") / PAGE_RESOURCE).read_text(encoding="utf-8")
    upstream = settings.dashboard_api_url.rstrip("/")
    headers = {"Authorization": f"Bearer {settings.api_token.get_secret_value()}"}

    async def index(_request: web.Request) -> web.Response:
        return web.Response(text=page, content_type="text/html")

    async def forward(request: web.Request) -> web.Response:
        body = await request.read()
        try:
            async with (
                aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session,
                session.request(
                    request.method,
                    upstream + request.path_qs,
                    data=body or None,
                    headers={**headers, "Content-Type": request.content_type},
                ) as response,
            ):
                return web.Response(
                    status=response.status,
                    body=await response.read(),
                    content_type=response.content_type,
                )
        except (aiohttp.ClientError, TimeoutError) as exc:
            log.warning("The bot's API at %s did not answer: %s", upstream, exc)
            return web.json_response(
                {"error": f"The bot did not answer at {upstream}."}, status=502
            )

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_route("*", "/api/{tail:.*}", forward)
    return app


def main() -> int:
    """Serve the dashboard on loopback until interrupted.

    Returns:
        ``0`` on a clean shutdown, ``2`` when the configuration is missing.
    """
    configure_logging("INFO")
    try:
        settings = DashboardSettings()
    except ValidationError as exc:
        log.error("Set FLYCONOMY_API_TOKEN in .env to the bot's token.\n%s", exc)
        return _EXIT_CONFIG_ERROR

    log.info("Dashboard at http://127.0.0.1:%d", settings.dashboard_port)
    web.run_app(
        make_app(settings),
        host="127.0.0.1",
        port=settings.dashboard_port,
        access_log=None,
        print=None,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
