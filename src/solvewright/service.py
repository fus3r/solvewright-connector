"""HTTPS calls to Solvewright's account service. Nothing else leaves the machine."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from urllib.parse import urlparse

from . import __version__


class ServiceError(Exception):
    """The service refused the request; retrying the same request will not help."""

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


class Unavailable(OSError):
    """The network or the service is down; the same request can be retried."""


class Service:
    def __init__(self, url: str, key: str, timeout: float = 25):
        parsed = urlparse(url)
        local = parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost")
        if not key or not (parsed.scheme == "https" or local):
            raise ValueError("The Solvewright service address must use HTTPS.")
        self.url, self.key, self.timeout = url.rstrip("/"), key, timeout

    def rpc(self, name: str, data: dict):
        request = urllib.request.Request(
            self.url + "/rest/v1/rpc/" + name,
            data=json.dumps(data).encode("utf-8"),
            headers={"apikey": self.key, "Content-Type": "application/json",
                     "User-Agent": f"solvewright-connector/{__version__}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                content = response.read()
                return json.loads(content) if content else None
        except urllib.error.HTTPError as error:
            if error.code >= 500 or error.code in (408, 429):
                raise Unavailable("Solvewright is temporarily unavailable.") from error
            try:
                detail = json.loads(error.read().decode("utf-8"))
            except (ValueError, UnicodeError):
                detail = {}
            raise ServiceError(detail.get("message") or "Solvewright refused the request.", detail.get("code")) from error
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            raise Unavailable("Could not reach Solvewright. Check the internet connection.") from error
