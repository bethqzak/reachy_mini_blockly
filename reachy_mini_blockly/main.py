"""Reachy Mini Blockly app.

Runs the Block Console bridge as a managed Reachy Mini app. The dashboard
starts it, the app framework opens the robot, and this module serves two
things for the lifetime of the app:

  * port 8080 — the bridge the Block Console talks to (see bridge.py)
  * port 8042 — the settings page embedded in the Reachy Mini dashboard
                (see webui.py and static/)

This is the app edition of the WADSIH Reachy Blocks desktop launcher. The
desktop build had to start and supervise the daemon itself; here the dashboard
already owns it, so the daemon subprocess, the MuJoCo simulator fallback and
the tkinter GUI are all gone. What is left is the bridge plus a settings page.

Entry point (see pyproject.toml):
    [project.entry-points."reachy_mini_apps"]
    reachy_mini_blockly = "reachy_mini_blockly.main:ReachyMiniBlockly"
"""

import logging
import threading

from reachy_mini import ReachyMini, ReachyMiniApp

from . import bridge
from .webui import register_routes

logger = logging.getLogger("reachy_mini_blockly")

# Where students go to build their blocks. Surfaced on the settings page.
BLOCK_CONSOLE_URL = "https://wadsih-liftoff.org/tools/blocks.html?preset=reachy"


class ReachyMiniBlockly(ReachyMiniApp):
    """Serve the Block Console bridge for as long as the app is running."""

    # Settings page served from static/ and embedded in the dashboard.
    #
    # localhost, not the 0.0.0.0 the app template ships with: the SDK uses this
    # one string as both the uvicorn bind host *and* the address it hands the
    # browser (see ReachyMiniApp.wrapped_run). 0.0.0.0 is a fine thing to bind
    # to and not an address you can browse to -- Chrome has blocked requests to
    # it since v128 -- so the page would load but every fetch out of it failed,
    # its own same-origin /api/info included. Binding to localhost costs
    # nothing here: the Block Console only ever talks to the bridge over
    # localhost anyway, so the browser has to be on this machine regardless.
    custom_app_url: str | None = "http://localhost:8042"

    # "default" lets the SDK pick a backend with camera and audio. The camera
    # blocks and face tracking need video, so don't downgrade this to
    # gstreamer_no_video.
    request_media_backend: str | None = None

    def run(self, reachy_mini: ReachyMini, stop_event: threading.Event) -> None:
        """Start the bridge and block until the dashboard stops the app."""
        logger.info("Blockly app starting")
        logger.info("Bridge:        http://localhost:%d", bridge.BRIDGE_PORT)
        logger.info("Settings page: %s", self.custom_app_url)
        logger.info("Block Console: %s", BLOCK_CONSOLE_URL)

        # The bridge reads camera frames off this handle; motion still goes
        # through the daemon's REST API, which the app lock does not gate.
        bridge.set_robot(reachy_mini)

        if self.settings_app is not None:
            register_routes(self.settings_app, reachy_mini)

        try:
            bridge.run_bridge(stop_event)
        finally:
            bridge.set_robot(None)
            logger.info("Blockly app stopped")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app = ReachyMiniBlockly()
    try:
        app.wrapped_run()
    except KeyboardInterrupt:
        app.stop()
