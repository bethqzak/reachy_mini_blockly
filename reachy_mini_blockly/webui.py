"""Settings-page API for the Reachy Mini Blockly app.

Deliberately thin. The settings page's JavaScript talks *directly* to the
bridge on port 8080 rather than proxying through here, for two reasons:

  * The bridge's httpx client and the settings server live on different
    event loops (two uvicorn servers, two threads). Sharing one client
    across them is not safe, and a second client would just duplicate the
    bridge's own routes.
  * It makes the settings page an honest end-to-end test. The page is served
    from :8042 and calls :8080 cross-origin, exactly as the Block Console
    does from wadsih-liftoff.org — so if the preview and the test buttons
    work here, the console will work too.

That leaves this module with only what the bridge does not know: which console
to open, what version is installed, and whether the app framework actually
handed us a robot.

The one thing kept *off* the bridge on purpose is the OpenAI API key for voice
chat. It is saved and removed through this server, which only the settings
page talks to; the bridge, which the Block Console can reach, has no route
that takes or returns it.
"""

# NOTE: no `from __future__ import annotations` here. FastAPI resolves
# stringified annotations against module globals; with the future import,
# parameters annotated with FastAPI types degrade into query params.

import logging
import re
import threading
import webbrowser

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import bridge, voice_chat

log = logging.getLogger("reachy_mini_blockly.webui")

APP_NAME = "reachy_mini_blockly"


def _app_version() -> str:
    try:
        from importlib.metadata import version
        return version("reachy_mini_blockly")
    except Exception:
        return "dev"


class VoiceKeyRequest(BaseModel):
    key: str = ""


_LOCAL = r"(localhost|127\.0\.0\.1)(:\d+)?"


def _from_this_machine(request: Request) -> bool:
    """True if the request comes from a page served by this computer.

    The bridge refuses foreign origins for every route; this server is the
    SDK's and has no such guard, so the key routes carry their own. Without
    it any website open in the browser could post a key of its choosing here,
    or delete the saved one. Same two checks as the bridge: the Origin (absent
    for curl and for same-origin GETs) and the Host, which a DNS-rebinding
    page cannot fake.
    """
    origin = request.headers.get("origin")
    host = request.headers.get("host", "")
    return (origin is None or re.fullmatch("http://" + _LOCAL, origin) is not None) \
        and re.fullmatch(_LOCAL, host) is not None


def _refused() -> JSONResponse:
    return JSONResponse(
        {"status": "error", "message": "The API key can only be changed from this computer."},
        status_code=403,
    )


def register_routes(settings_app: FastAPI, reachy_mini) -> None:
    """Attach the settings-page API to the FastAPI instance the SDK provides."""
    from .main import BLOCK_CONSOLE_URL

    @settings_app.get("/api/info")
    def info() -> dict:
        """Everything the settings page needs to render itself."""
        media = getattr(reachy_mini, "media", None)
        return {
            "app": APP_NAME,
            "version": _app_version(),
            "console_url": BLOCK_CONSOLE_URL,
            "bridge_url": f"http://localhost:{bridge.BRIDGE_PORT}",
            "daemon_url": bridge.DAEMON_URL,
            "robot_attached": bridge._connect_reachy_mini() is not None,
            "media_available": media is not None,
        }

    @settings_app.get("/api/voice-key")
    def voice_key(request: Request):
        """Whether a key is saved, and its last four characters. Never the key."""
        if not _from_this_machine(request):
            return _refused()
        return {"status": "ok", **voice_chat.key_status()}

    @settings_app.post("/api/voice-key")
    def save_voice_key(req: VoiceKeyRequest, request: Request):
        if not _from_this_machine(request):
            return _refused()
        try:
            voice_chat.save_api_key(req.key)
        except ValueError as e:
            return {"status": "error", "message": str(e)}
        except OSError as e:
            return {"status": "error", "message": f"Could not save the key: {e}"}
        return {"status": "ok", "message": "Key saved.", **voice_chat.key_status()}

    @settings_app.delete("/api/voice-key")
    def delete_voice_key(request: Request):
        if not _from_this_machine(request):
            return _refused()
        voice_chat.clear_api_key()
        return {"status": "ok", "message": "Key removed.", **voice_chat.key_status()}

    @settings_app.post("/api/open-console")
    def open_console() -> dict:
        """Open the Block Console in a new window of the default browser.

        The settings page is embedded in the dashboard, which gives a
        target="_blank" link nowhere to go. Opening from this side gets a real
        browser window. That the browser lands on *this* machine is the right
        answer anyway: the console talks to the bridge on localhost, so it only
        ever works from here.
        """
        def _open() -> None:
            try:
                webbrowser.open_new(BLOCK_CONSOLE_URL)
            except Exception:
                log.exception("Could not open the Block Console")

        # Launching a browser can block for a second or two; don't hold the
        # request open while it happens.
        threading.Thread(target=_open, name="open_console", daemon=True).start()
        return {"status": "ok", "url": BLOCK_CONSOLE_URL}
