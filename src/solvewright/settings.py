"""Where the connector talks to, and where it keeps its files.

The service URL and key below are the same public values every visitor's
browser already receives from solvewright.app: a publishable key that can only
call the functions Solvewright exposes to anonymous clients. Row-level security
and the per-computer key decide what any request can reach.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

SITE_URL = "https://solvewright.app"
SERVICE_URL = "https://zqdxsgetwbeiwpjwjhzd.supabase.co"
SERVICE_KEY = "sb_publishable_zhQrE__vhJ1W6Nr-DSDXBQ_slqYpYUq"


@dataclass(frozen=True)
class Settings:
    site_url: str
    service_url: str
    service_key: str
    home: Path

    @property
    def connection_file(self) -> Path:
        return self.home / "connection.json"

    @property
    def interviews(self) -> Path:
        return self.home / "interviews"


def default_home() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Solvewright"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Solvewright"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "solvewright"


def load(home: str | os.PathLike[str] | None = None) -> Settings:
    """Environment variables exist for development against a local stack."""
    chosen = Path(home or os.environ.get("SOLVEWRIGHT_HOME") or default_home()).expanduser().resolve()
    return Settings(
        site_url=os.environ.get("SOLVEWRIGHT_SITE_URL", SITE_URL).rstrip("/"),
        service_url=os.environ.get("SOLVEWRIGHT_SERVICE_URL", SERVICE_URL).rstrip("/"),
        service_key=os.environ.get("SOLVEWRIGHT_SERVICE_KEY", SERVICE_KEY),
        home=chosen,
    )
