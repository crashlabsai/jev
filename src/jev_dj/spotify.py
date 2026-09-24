"""Spotify: log in once, then play the DJ's pick on your device.

Jev decided *what* to play (the crate query); this module only talks to Spotify:
  - Authorization Code + PKCE login (needs only a Client ID, no secret)
  - search playlists for the crate query
  - start the best match on your active Spotify device (Premium)

Setup: create an app at https://developer.spotify.com/dashboard, add your
redirect URI, and put SPOTIFY_CLIENT_ID (+ SPOTIFY_REDIRECT_URI) in .env.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API_URL = "https://api.spotify.com/v1"
SCOPES = "user-read-playback-state user-modify-playback-state"
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8000/callback"

# Tokens live next to .env and are gitignored; delete the file to log out
TOKEN_FILE = Path(__file__).resolve().parents[2] / ".spotify-token.json"

# PKCE refresh tokens rotate, so two threads refreshing at once would race
_token_lock = threading.Lock()


class SpotifyError(Exception):
    pass


class NotConnected(SpotifyError):
    pass


class NoDevice(SpotifyError):
    pass


@dataclass
class Played:
    playlist: dict[str, Any]
    device: dict[str, Any]


def client_id() -> str | None:
    return os.environ.get("SPOTIFY_CLIENT_ID") or None


def redirect_uri() -> str:
    return os.environ.get("SPOTIFY_REDIRECT_URI") or DEFAULT_REDIRECT_URI


def is_connected() -> bool:
    return TOKEN_FILE.exists()


def disconnect() -> None:
    TOKEN_FILE.unlink(missing_ok=True)


# --- Login: Authorization Code with PKCE ---


def login_url(state: str) -> tuple[str, str]:
    """Return (url to send the listener to, code_verifier to keep for the callback)."""
    cid = client_id()
    if not cid:
        raise NotConnected("Set SPOTIFY_CLIENT_ID in .env first.")
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    params = {
        "response_type": "code",
        "client_id": cid,
        "scope": SCOPES,
        "redirect_uri": redirect_uri(),
        "state": state,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
    }
    return f"{AUTH_URL}?{urlencode(params)}", verifier


def finish_login(code: str, verifier: str) -> None:
    tokens = _token_request(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri(),
            "client_id": client_id(),
            "code_verifier": verifier,
        }
    )
    with _token_lock:
        _save(tokens, previous_refresh=None)


def _token_request(form: dict[str, Any]) -> dict[str, Any]:
    response = httpx.post(TOKEN_URL, data=form, timeout=10)
    if response.status_code != 200:
        detail = response.json().get("error_description", response.text) if response.content else ""
        raise NotConnected(f"Spotify login failed: {detail or response.status_code}")
    return response.json()


def _save(tokens: dict[str, Any], previous_refresh: str | None) -> dict[str, Any]:
    stored = {
        "access_token": tokens["access_token"],
        # Spotify may or may not rotate the refresh token; keep the old one if not
        "refresh_token": tokens.get("refresh_token") or previous_refresh,
        "expires_at": time.time() + tokens.get("expires_in", 3600) - 60,
    }
    TOKEN_FILE.write_text(json.dumps(stored))
    TOKEN_FILE.chmod(0o600)
    return stored


def _access_token() -> str:
    with _token_lock:
        if not TOKEN_FILE.exists():
            raise NotConnected("Connect Spotify first.")
        stored = json.loads(TOKEN_FILE.read_text())
        if time.time() >= stored["expires_at"]:
            try:
                tokens = _token_request(
                    {
                        "grant_type": "refresh_token",
                        "refresh_token": stored["refresh_token"],
                        "client_id": client_id(),
                    }
                )
            except NotConnected:
                # Refresh tokens now expire six months after login; start over
                disconnect()
                raise NotConnected("Spotify session expired. Connect again.") from None
            stored = _save(tokens, previous_refresh=stored["refresh_token"])
        return stored["access_token"]


# --- Web API ---


def _api(method: str, path: str, **kwargs: Any) -> dict[str, Any] | None:
    response = httpx.request(
        method,
        f"{API_URL}{path}",
        headers={"Authorization": f"Bearer {_access_token()}"},
        timeout=10,
        **kwargs,
    )
    if response.status_code in (200, 201):
        return response.json()
    if response.status_code in (202, 204):
        return None

    error = response.json().get("error", {}) if response.content else {}
    message = error.get("message", response.text) if isinstance(error, dict) else str(error)
    reason = error.get("reason", "") if isinstance(error, dict) else ""
    if response.status_code == 401:
        raise NotConnected("Spotify rejected the login. Connect again.")
    if reason == "NO_ACTIVE_DEVICE" or response.status_code == 404:
        raise NoDevice("Open Spotify on your phone or computer, then try again.")
    if reason == "PREMIUM_REQUIRED":
        raise SpotifyError("Playing on a device needs Spotify Premium.")
    if response.status_code == 429:
        wait = response.headers.get("Retry-After", "a few")
        raise SpotifyError(f"Spotify rate limit. Try again in {wait} seconds.")
    raise SpotifyError(f"Spotify error {response.status_code}: {message}")


def me() -> dict[str, Any]:
    return _api("GET", "/me") or {}


def find_playlist(query: str) -> dict[str, Any]:
    """Top playlist for the crate query. Search maxes out at 10 results since Feb 2026."""
    data = _api("GET", "/search", params={"q": query, "type": "playlist", "limit": 10}) or {}
    for item in data.get("playlists", {}).get("items", []):
        # Search can return null slots; Spotify-owned editorial playlists are off-limits to new apps
        if not item or item.get("owner", {}).get("id") == "spotify":
            continue
        images = item.get("images") or []
        return {
            "name": item["name"],
            "uri": item["uri"],
            "url": item["external_urls"]["spotify"],
            "owner": item.get("owner", {}).get("display_name") or "",
            "image": images[0]["url"] if images else None,
        }
    raise SpotifyError(f"No playlist found for {query!r}.")


def pick_device() -> dict[str, Any]:
    devices = (_api("GET", "/me/player/devices") or {}).get("devices", [])
    usable = [d for d in devices if not d.get("is_restricted")]
    if not usable:
        raise NoDevice("Open Spotify on your phone or computer, then try again.")
    device = next((d for d in usable if d.get("is_active")), usable[0])
    return {"id": device["id"], "name": device["name"], "type": device["type"]}


def play_query(query: str) -> Played:
    playlist = find_playlist(query)
    device = pick_device()
    _api("PUT", "/me/player/play", params={"device_id": device["id"]}, json={"context_uri": playlist["uri"]})
    return Played(playlist=playlist, device=device)
