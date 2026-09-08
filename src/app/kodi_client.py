"""Reusable Kodi JSON-RPC client for Floppy."""

from __future__ import annotations

import itertools
import logging
import os
from typing import Any

import requests


logger = logging.getLogger(__name__)


class KodiError(Exception):
    """Base exception for Kodi integration failures."""


class KodiConfigurationError(KodiError):
    """Kodi configuration is missing or invalid."""


class KodiConnectionError(KodiError):
    """Kodi could not be reached over HTTP."""


class KodiAuthenticationError(KodiError):
    """Kodi rejected the configured credentials."""


class KodiProtocolError(KodiError):
    """Kodi returned an invalid or unexpected response."""


class KodiRPCError(KodiError):
    """Kodi returned a JSON-RPC error."""

    def __init__(self, *, code: int | None, message: str, data: Any = None) -> None:
        self.code = code
        self.message = message
        self.data = data
        text = "Kodi JSON-RPC error"
        if code is not None:
            text += f" {code}"
        if message:
            text += f": {message}"
        super().__init__(text)


class KodiClient:
    """Small synchronous client for Kodi's JSON-RPC API."""

    DEFAULT_PORT = 8080
    DEFAULT_TIMEOUT = 6.0
    DEFAULT_PLAYER_PROPERTIES = (
        "speed",
        "time",
        "totaltime",
        "percentage",
        "position",
    )

    def __init__(
        self,
        *,
        host: str,
        port: int = DEFAULT_PORT,
        username: str = "",
        password: str = "",
        scheme: str = "http",
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        host = str(host or "").strip()
        scheme = str(scheme or "http").strip().lower()
        if not host:
            raise KodiConfigurationError("KODI_HOST is not configured.")
        if scheme not in {"http", "https"}:
            raise KodiConfigurationError("KODI_SCHEME must be http or https.")
        try:
            port = int(port)
        except (TypeError, ValueError) as exc:
            raise KodiConfigurationError("KODI_PORT must be an integer.") from exc
        if port < 1 or port > 65535:
            raise KodiConfigurationError("KODI_PORT is outside the valid TCP port range.")
        try:
            timeout = float(timeout)
        except (TypeError, ValueError) as exc:
            raise KodiConfigurationError("KODI_TIMEOUT must be numeric.") from exc
        if timeout <= 0:
            raise KodiConfigurationError("KODI_TIMEOUT must be greater than zero.")

        self.host = host
        self.port = port
        self.username = username or ""
        self.password = password or ""
        self.scheme = scheme
        self.timeout = timeout
        self.url = f"{self.scheme}://{self.host}:{self.port}/jsonrpc"
        self._request_ids = itertools.count(1)

    @classmethod
    def from_env(cls) -> "KodiClient":
        """Construct the client from Floppy's environment."""
        return cls(
            host=os.getenv("KODI_HOST") or "",
            port=os.getenv("KODI_PORT") or str(cls.DEFAULT_PORT),
            username=os.getenv("KODI_USERNAME") or "",
            password=os.getenv("KODI_PASSWORD") or "",
            scheme=os.getenv("KODI_SCHEME") or "http",
            timeout=os.getenv("KODI_TIMEOUT") or str(cls.DEFAULT_TIMEOUT),
        )

    @property
    def auth(self):
        """Return requests-compatible HTTP Basic Auth."""
        if not self.username:
            return None
        return self.username, self.password

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Perform one Kodi JSON-RPC request."""
        request_id = next(self._request_ids)
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "id": request_id}
        if params is not None:
            payload["params"] = params
        try:
            response = requests.post(
                self.url,
                json=payload,
                auth=self.auth,
                timeout=self.timeout,
            )
        except requests.Timeout as exc:
            raise KodiConnectionError(
                f"Kodi request timed out after {self.timeout:g}s."
            ) from exc
        except requests.ConnectionError as exc:
            raise KodiConnectionError("Could not connect to Kodi.") from exc
        except requests.RequestException as exc:
            raise KodiConnectionError(f"Kodi HTTP request failed: {exc}") from exc

        if response.status_code in {401, 403}:
            raise KodiAuthenticationError("Kodi rejected the configured credentials.")
        try:
            response.raise_for_status()
        except requests.RequestException as exc:
            raise KodiConnectionError(
                f"Kodi returned HTTP {response.status_code}."
            ) from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise KodiProtocolError("Kodi returned invalid JSON.") from exc
        if not isinstance(data, dict):
            raise KodiProtocolError("Kodi returned a non-object JSON-RPC response.")
        if data.get("id") != request_id:
            raise KodiProtocolError("Kodi returned an unexpected JSON-RPC request ID.")
        error = data.get("error")
        if error:
            if isinstance(error, dict):
                raise KodiRPCError(
                    code=error.get("code"),
                    message=str(error.get("message") or ""),
                    data=error.get("data"),
                )
            raise KodiRPCError(code=None, message=str(error))
        if "result" not in data:
            raise KodiProtocolError("Kodi JSON-RPC response has no result.")
        return data["result"]

    def ping(self) -> bool:
        return self.call("JSONRPC.Ping") == "pong"

    def get_active_players(self) -> list[dict[str, Any]]:
        result = self.call("Player.GetActivePlayers")
        if not isinstance(result, list):
            raise KodiProtocolError("Player.GetActivePlayers did not return a list.")
        return result

    def get_player_properties(
        self,
        player_id: int,
        properties: tuple[str, ...] | list[str] | None = None,
    ) -> dict[str, Any]:
        selected = list(properties or self.DEFAULT_PLAYER_PROPERTIES)
        result = self.call(
            "Player.GetProperties",
            {"playerid": int(player_id), "properties": selected},
        )
        if not isinstance(result, dict):
            raise KodiProtocolError("Player.GetProperties did not return an object.")
        return result

    def get_player_item(
        self,
        player_id: int,
        properties: tuple[str, ...] | list[str] | None = None,
    ) -> dict[str, Any]:
        selected = list(
            properties
            or (
                "title",
                "showtitle",
                "season",
                "episode",
                "tvshowid",
                "uniqueid",
                "year",
                "file",
            )
        )
        result = self.call(
            "Player.GetItem",
            {"playerid": int(player_id), "properties": selected},
        )
        if not isinstance(result, dict):
            raise KodiProtocolError("Player.GetItem did not return an object.")
        item = result.get("item")
        if not isinstance(item, dict):
            raise KodiProtocolError("Player.GetItem response has no item.")
        return item

    def get_tvshow_details(
        self,
        tvshow_id: int,
        properties: tuple[str, ...] | list[str] | None = None,
    ) -> dict[str, Any]:
        selected = list(properties or ("title", "uniqueid", "year"))
        result = self.call(
            "VideoLibrary.GetTVShowDetails",
            {"tvshowid": int(tvshow_id), "properties": selected},
        )
        if not isinstance(result, dict):
            raise KodiProtocolError(
                "VideoLibrary.GetTVShowDetails did not return an object."
            )
        details = result.get("tvshowdetails")
        if not isinstance(details, dict):
            raise KodiProtocolError("TV-show details response has no tvshowdetails.")
        return details

    def open_file(self, file_url: str) -> None:
        result = self.call("Player.Open", {"item": {"file": file_url}})
        if result != "OK":
            raise KodiProtocolError(f"Unexpected Player.Open result: {result!r}")

    def activate_window(self, window: str, parameters: list[str] | None = None) -> None:
        params: dict[str, Any] = {"window": window}
        if parameters:
            params["parameters"] = parameters
        result = self.call("GUI.ActivateWindow", params)
        if result != "OK":
            raise KodiProtocolError(
                f"Unexpected GUI.ActivateWindow result: {result!r}"
            )

    def play_pause(self, player_id: int, play: bool | None = None) -> Any:
        params: dict[str, Any] = {"playerid": int(player_id)}
        if play is not None:
            params["play"] = bool(play)
        return self.call("Player.PlayPause", params)

    def stop(self, player_id: int) -> None:
        result = self.call("Player.Stop", {"playerid": int(player_id)})
        if result != "OK":
            raise KodiProtocolError(f"Unexpected Player.Stop result: {result!r}")

    def seek_seconds(self, player_id: int, seconds: int) -> dict[str, Any]:
        seconds = int(seconds)
        if seconds < 0:
            raise ValueError("Seek time must be zero or greater.")
        hours, remainder = divmod(seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        result = self.call(
            "Player.Seek",
            {
                "playerid": int(player_id),
                "value": {
                    "time": {
                        "hours": hours,
                        "minutes": minutes,
                        "seconds": secs,
                        "milliseconds": 0,
                    }
                },
            },
        )
        if not isinstance(result, dict):
            raise KodiProtocolError("Player.Seek did not return an object.")
        return result

    def seek_percentage(self, player_id: int, percentage: float) -> dict[str, Any]:
        percentage = float(percentage)
        if not 0 <= percentage <= 100:
            raise ValueError("Seek percentage must be between 0 and 100.")
        result = self.call(
            "Player.Seek",
            {"playerid": int(player_id), "value": percentage},
        )
        if not isinstance(result, dict):
            raise KodiProtocolError("Player.Seek did not return an object.")
        return result
