"""Web UI: `uv run jev-dj-ui` → http://127.0.0.1:8000

A tiny stdlib server so the API key stays server-side. The page posts your
moment, the server makes the one Jev call, and the browser draws the answers.
With SPOTIFY_CLIENT_ID set, it also logs you into Spotify and plays the pick.
"""

from __future__ import annotations

import argparse
import json
import secrets
import threading
import uuid
from collections import OrderedDict
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

import httpx
from typesafe_sdk import TypeSafeError

from jev_dj import SAMPLES, parse_time
from jev_dj import dj, spotify
from jev_dj.dj import DJAnswers, Spin, ask_jev, spin

INDEX = files("jev_dj").joinpath("static/index.html")
MAX_MESSAGE_CHARS = 1000

# Recent answers (+ the state they answered) by spin id, so the listener's
# tie-break re-spins without a second Jev call
_RECENT: OrderedDict[str, tuple[DJAnswers, dict[str, Any]]] = OrderedDict()
_RECENT_MAX = 64
_lock = threading.Lock()

# Spotify login state → PKCE code_verifier, held between /login and /callback
_PENDING_LOGINS: OrderedDict[str, str] = OrderedDict()


class BadRequest(Exception):
    pass


def config() -> dict[str, Any]:
    return {
        "samples": [{"label": label, "message": message} for label, message in SAMPLES],
        "energy_levels": dj.ENERGY_LEVELS,
        "crate": dj.CRATE,
        # Exactly what goes over the wire, so the walkthrough can show the request
        "questions": {
            name: question.model_dump(exclude_none=True)
            for name, question in dj.build_questions().items()
        },
        "thresholds": {
            "vibe_confidence_floor": dj.VIBE_CONFIDENCE_FLOOR,
            "energy_confidence_floor": dj.ENERGY_CONFIDENCE_FLOOR,
            "lift_likely": dj.LIFT_LIKELY,
            "lyrics_ok": dj.LYRICS_OK,
            "nostalgia_likely": dj.NOSTALGIA_LIKELY,
        },
    }


def to_payload(spin_id: str, s: Spin, state: dict[str, Any], resolved: bool = False) -> dict[str, Any]:
    a = s.answers
    return {
        "id": spin_id,
        "resolved": resolved,
        "model": a.model,
        "input_tokens": a.usage.input_tokens,
        "output_tokens": a.usage.output_tokens,
        "state": state,
        "spin": {
            "vibe": s.vibe,
            "energy_level": s.energy_level,
            "query": s.query,
            "url": s.url,
            "reasons": s.reasons,
            "ask_listener": s.ask_listener,
        },
        "answers": {name: answer.model_dump() for name, answer in a.answers.items()},
    }


def handle_spin(body: dict[str, Any]) -> dict[str, Any]:
    message = str(body.get("message", "")).strip()
    if not message:
        raise BadRequest("Describe your moment first.")
    if len(message) > MAX_MESSAGE_CHARS:
        raise BadRequest(f"Keep it under {MAX_MESSAGE_CHARS} characters.")
    try:
        now = parse_time(body["time"]) if body.get("time") else datetime.now()
    except ValueError:
        raise BadRequest("Time must be HH:MM.") from None

    answers = ask_jev(message, now)
    state = dj.build_state(message, now)
    spin_id = uuid.uuid4().hex
    with _lock:
        _RECENT[spin_id] = (answers, state)
        while len(_RECENT) > _RECENT_MAX:
            _RECENT.popitem(last=False)
    return to_payload(spin_id, spin(answers), state)


def handle_resolve(body: dict[str, Any]) -> dict[str, Any]:
    spin_id = str(body.get("id", ""))
    vibe = str(body.get("vibe", ""))
    with _lock:
        recent = _RECENT.get(spin_id)
    if recent is None:
        raise BadRequest("That spin expired — spin again.")
    if vibe not in dj.CRATE:
        raise BadRequest(f"Unknown vibe {vibe!r}.")
    answers, state = recent
    return to_payload(spin_id, spin(answers, vibe_override=vibe), state, resolved=True)


def handle_spotify_status(_body: dict[str, Any]) -> dict[str, Any]:
    status: dict[str, Any] = {
        "configured": spotify.client_id() is not None,
        "connected": spotify.is_connected(),
        "redirect_uri": spotify.redirect_uri(),
    }
    if status["connected"]:
        try:
            status["user"] = spotify.me().get("display_name")
        except spotify.NotConnected:
            status["connected"] = False
    return status


def handle_spotify_play(body: dict[str, Any]) -> dict[str, Any]:
    query = str(body.get("query", "")).strip()
    if not query or len(query) > 200:
        raise BadRequest("Nothing to play yet. Spin first.")
    played = spotify.play_query(query)
    return {"playlist": played.playlist, "device": played.device}


def handle_spotify_disconnect(_body: dict[str, Any]) -> dict[str, Any]:
    spotify.disconnect()
    return {"connected": False}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        url = urlsplit(self.path)
        if url.path == "/":
            self._send(HTTPStatus.OK, INDEX.read_bytes(), "text/html; charset=utf-8")
        elif url.path == "/api/config":
            self._json(HTTPStatus.OK, config())
        elif url.path == "/login":
            self._login()
        elif url.path == "/callback":
            self._callback({k: v[0] for k, v in parse_qs(url.query).items()})
        else:
            self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})

    def _login(self) -> None:
        state = secrets.token_urlsafe(16)
        try:
            url, verifier = spotify.login_url(state)
        except spotify.NotConnected as error:
            self._redirect(f"/?spotify_error={quote(str(error))}")
            return
        with _lock:
            _PENDING_LOGINS[state] = verifier
            while len(_PENDING_LOGINS) > 16:
                _PENDING_LOGINS.popitem(last=False)
        self._redirect(url)

    def _callback(self, params: dict[str, str]) -> None:
        with _lock:
            verifier = _PENDING_LOGINS.pop(params.get("state", ""), None)
        if "error" in params or verifier is None or "code" not in params:
            reason = params.get("error", "login expired, try again")
            self._redirect(f"/?spotify_error={quote(reason)}")
            return
        try:
            spotify.finish_login(params["code"], verifier)
        except spotify.SpotifyError as error:
            self._redirect(f"/?spotify_error={quote(str(error))}")
            return
        self._redirect("/?spotify=connected")

    def do_POST(self) -> None:
        routes = {
            "/api/spin": handle_spin,
            "/api/resolve": handle_resolve,
            "/api/spotify/status": handle_spotify_status,
            "/api/spotify/play": handle_spotify_play,
            "/api/spotify/disconnect": handle_spotify_disconnect,
        }
        route = routes.get(self.path)
        if route is None:
            self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise BadRequest("Expected a JSON object.")
            self._json(HTTPStatus.OK, route(body))
        except (BadRequest, json.JSONDecodeError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        except TypeSafeError as error:
            self._json(HTTPStatus.BAD_GATEWAY, {"error": f"Jev call failed: {error}"})
        except spotify.NotConnected as error:
            self._json(HTTPStatus.UNAUTHORIZED, {"error": str(error), "reconnect": True})
        except spotify.SpotifyError as error:
            self._json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
        except httpx.HTTPError as error:
            self._json(HTTPStatus.BAD_GATEWAY, {"error": f"Couldn't reach Spotify: {error}"})

    def _redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _json(self, status: HTTPStatus, data: dict[str, Any]) -> None:
        self._send(status, json.dumps(data).encode(), "application/json")

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the Jev DJ web UI.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"🎧 jev-dj UI on http://{args.host}:{args.port}  (Ctrl+C to stop)")
    if spotify.client_id():
        print(f"   Spotify redirect URI: {spotify.redirect_uri()}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
