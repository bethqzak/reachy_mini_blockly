"""
Reachy Mini Bridge Server for the Block Console.

A CORS-open FastAPI server that proxies the Block Console
(https://wadsih-liftoff.org/tools/blocks.html?preset=reachy) to the Reachy Mini
daemon REST API on localhost:8000, adding the few things the daemon has no
route for: text-to-speech and camera snapshots. Face tracking is also done
here, with OpenCV, even though the daemon has had its own tracker since SDK
1.9 (POST /api/media/tracking/enable, GET /api/media/tracking/face): the
blocks need face coordinates as well as a head that follows, and the daemon's
FaceTarget only reports them while its tracker is driving the head.

Verified against reachy-mini 1.11.0: every daemon route and SDK attribute used
below is unchanged from 1.10.0.

This module is the app's engine, not its entry point — `main.BlocklyApp` starts
it with `run_bridge()` and hands it the live `ReachyMini` the app framework
opened. See main.py for how the two fit together.

The console hardcodes `http://localhost:8080`, so the browser must be on the
same machine as this process. That holds for the Reachy Mini Lite, where the
daemon and apps run on the user's own computer.
"""

import asyncio
import base64
import concurrent.futures
import logging
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from typing import Optional

import httpx
import numpy as np
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logger = logging.getLogger("reachy_mini_blockly.bridge")

# ── Configuration ─────────────────────────────────────────────────────

DAEMON_URL = "http://localhost:8000"
DANCES_DATASET = "pollen-robotics/reachy-mini-dances-library"
EMOTIONS_DATASET = "pollen-robotics/reachy-mini-emotions-library"

# Travel limits, measured on a Mini Lite by walking the daemon past its stops.
# It saturates silently rather than erroring, so a block asking for 149 deg of
# body rotation would just quietly get 64. Clamping here lets the route say so.
HEAD_XYZ_LIMIT_MM = 25.0   # z topped out at ~22mm; x tracked cleanly to 20mm
BODY_YAW_LIMIT_DEG = 60.0  # hard stop measured at ~+64 / -61.5 deg

# How long a move takes when the block doesn't say. Short, so blocks feel
# snappy and a run of them doesn't crawl; a block that wants a glide passes
# its own duration.
DEFAULT_MOVE_DURATION = 0.5  # seconds

# Shared HTTP client (created in lifespan)
http_client: httpx.AsyncClient = None

# Track running move UUIDs so we can stop them
running_move_uuids: list[str] = []

# Cached list of clip names in the HF emotions library (populated on first use)
_emotion_library: list[str] = []

# ── TTS state ─────────────────────────────────────────────────────────
# Hide console windows for subprocesses on Windows
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# Primary TTS: edge-tts (async, Microsoft neural voices, free, no API key)
# Fallback: platform-native engines (macOS `say`, Windows SAPI, Linux pyttsx3)
_tts_executor: concurrent.futures.ThreadPoolExecutor = None
_tts_engine = None
_edge_tts_available = False
_EDGE_TTS_VOICE = "en-US-AriaNeural"  # natural-sounding female voice

# ── Reachy Mini SDK handle (for camera) ──────────────────────────────
# Injected by main.BlocklyApp via set_robot(). Unlike the desktop build, the
# bridge never opens its own ReachyMini(): the app framework already holds the
# robot's app slot, and a second connection would fight it for the media
# backend. This also removes the SDK/daemon version-skew failure mode, since
# the connection now comes from the same venv the daemon launched us in.
_reachy_mini = None
_reachy_mini_lock = threading.Lock()

# ── Direct OpenCV camera (fallback when SDK IPC gives no frames) ─────
_cv_cap = None
_cv_cap_lock = threading.Lock()
_use_direct_cv = False  # set True after SDK IPC fails

# ── Head tracking state ──────────────────────────────────────────────
# In-app tracker. The daemon's own (SDK 1.9+, /api/media/tracking/*) would be
# lighter and, since 1.11, aims 15 deg lower to look at faces rather than
# hairlines -- but it exposes the face only while it holds the head, and the
# on-demand `detect_head` block needs a detection without moving anything.
# Swapping the enable/disable half over to the daemon while keeping OpenCV for
# the coordinate blocks is the obvious next step once it can be tried on a
# robot.
_head_tracking_enabled = False
_head_tracking_task = None
_face_detected = False
_face_x = 0.0    # normalized -100 to 100
_face_y = 0.0    # normalized -100 to 100
_face_lock = threading.Lock()
_face_cascade = None

# Head tracking tuning
_TRACKING_FPS = 10
_FACE_MOVE_THRESHOLD = 5.0    # min change (out of 100) to send command
_FACE_LOST_DELAY = 2.0        # seconds before returning to neutral
_HEAD_YAW_RANGE = 35.0        # max yaw degrees
_HEAD_PITCH_RANGE = 20.0      # max pitch degrees


# ── Request models ────────────────────────────────────────────────────

class MoveHeadRequest(BaseModel):
    direction: str = "front"  # left, right, up, down, front


class MoveHeadCustomRequest(BaseModel):
    pitch: float = 0.0  # degrees, up/down
    yaw: float = 0.0    # degrees, left/right
    roll: float = 0.0   # degrees, tilt
    duration: float = DEFAULT_MOVE_DURATION


class EmotionRequest(BaseModel):
    emotion: str = "happy"


class HeadTrackingRequest(BaseModel):
    enable: bool = True


class SayRequest(BaseModel):
    text: str = ""


class DanceRequest(BaseModel):
    dance_name: str = ""


class VolumeRequest(BaseModel):
    volume: int = 50


class SetCameraRequest(BaseModel):
    index: int = 0


class AntennaRequest(BaseModel):
    left: float = 0.0   # degrees
    right: float = 0.0  # degrees
    duration: float = DEFAULT_MOVE_DURATION


class HeadPositionRequest(BaseModel):
    """Head *translation*, the counterpart to the pitch/yaw/roll block.

    Millimetres rather than the daemon's metres: a block asking for "20" reads
    better to a student than "0.02", and the whole usable range is under 3cm.
    """
    x: float = 0.0  # mm, + is forward
    y: float = 0.0  # mm, + is left
    z: float = 0.0  # mm, + is up
    duration: float = DEFAULT_MOVE_DURATION


class BodyYawRequest(BaseModel):
    angle: float = 0.0  # degrees, + turns left
    duration: float = DEFAULT_MOVE_DURATION


class MoveTogetherRequest(BaseModel):
    """Every axis the single-purpose blocks take, in one goto.

    The daemon animates a goto's head, antennas and body together over one
    duration, so this is how "antennas *and* head at the same time" is done.
    Two separate blocks can't do it: the second goto takes over every axis of
    the first (see goto_preserving).

    Every field is optional. An axis left out holds where the last block put
    it, exactly as the single-axis routes do. Units match those routes:
    degrees for rotations and antennas, millimetres for head translation.
    """
    pitch: Optional[float] = None    # head, degrees
    yaw: Optional[float] = None      # head, degrees
    roll: Optional[float] = None     # head, degrees
    x: Optional[float] = None        # head, mm
    y: Optional[float] = None        # head, mm
    z: Optional[float] = None        # head, mm
    left: Optional[float] = None     # antenna, degrees
    right: Optional[float] = None    # antenna, degrees
    body_yaw: Optional[float] = None # degrees
    duration: float = DEFAULT_MOVE_DURATION


# ── Head direction presets (degrees) ──────────────────────────────────

HEAD_DIRECTIONS = {
    "left":  {"pitch": 0, "yaw": 30, "roll": 0},
    "right": {"pitch": 0, "yaw": -30, "roll": 0},
    "up":    {"pitch": -20, "yaw": 0, "roll": 0},
    "down":  {"pitch": 20, "yaw": 0, "roll": 0},
    "front": {"pitch": 0, "yaw": 0, "roll": 0},
}

# ── Emotion name aliases ─────────────────────────────────────────────
# Maps the friendly names used by Block Console onto clip names in the HF
# emotions library. Only names that differ need an entry -- anything else
# falls back to appending "1" (e.g. "sad" -> "sad1"), then to EMOTION_POSES.

EMOTION_ALIASES = {
    "happy": "cheerful1",
    "angry": "furious1",
    "afraid": "fear1",
    "bored": "boredom1",
    "anxious": "anxiety1",
    "excited": "enthusiastic1",
    "calm": "serenity1",
    "sleepy": "sleep1",
    "goaway": "go_away1",
    "go away": "go_away1",
    "hello": "welcoming1",
    "hi": "welcoming1",
    "incomprehensible": "incomprehensible2",
}

# ── Emotion poses (fallback: head + antenna movements, no audio) ─────
# Used only when the HF emotions library is unavailable (e.g. no HF_TOKEN).
# Each emotion is a list of pose steps: {pitch, yaw, roll, antennas, duration}
# Angles in degrees. Antennas are [left_deg, right_deg].

EMOTION_POSES = {
    "happy": [
        {"pitch": -10, "yaw": 0, "roll": 0, "antennas": [30, 30], "duration": 0.4},
        {"pitch": -5, "yaw": 10, "roll": 5, "antennas": [20, 20], "duration": 0.3},
        {"pitch": -5, "yaw": -10, "roll": -5, "antennas": [20, 20], "duration": 0.3},
        {"pitch": 0, "yaw": 0, "roll": 0, "antennas": [0, 0], "duration": 0.5},
    ],
    "sad": [
        {"pitch": 20, "yaw": 0, "roll": 0, "antennas": [-20, -20], "duration": 0.8},
        {"pitch": 25, "yaw": -5, "roll": -3, "antennas": [-25, -25], "duration": 1.0},
        {"pitch": 0, "yaw": 0, "roll": 0, "antennas": [0, 0], "duration": 0.8},
    ],
    "curious": [
        {"pitch": -5, "yaw": 15, "roll": 10, "antennas": [15, -10], "duration": 0.5},
        {"pitch": -8, "yaw": -15, "roll": -10, "antennas": [-10, 15], "duration": 0.5},
        {"pitch": -5, "yaw": 0, "roll": 0, "antennas": [10, 10], "duration": 0.4},
        {"pitch": 0, "yaw": 0, "roll": 0, "antennas": [0, 0], "duration": 0.5},
    ],
    "amazed": [
        {"pitch": -15, "yaw": 0, "roll": 0, "antennas": [40, 40], "duration": 0.3},
        {"pitch": -18, "yaw": 0, "roll": 0, "antennas": [45, 45], "duration": 0.8},
        {"pitch": 0, "yaw": 0, "roll": 0, "antennas": [0, 0], "duration": 0.6},
    ],
    "proud": [
        {"pitch": -15, "yaw": 0, "roll": 0, "antennas": [35, 35], "duration": 0.5},
        {"pitch": -18, "yaw": 5, "roll": 3, "antennas": [30, 30], "duration": 0.8},
        {"pitch": 0, "yaw": 0, "roll": 0, "antennas": [0, 0], "duration": 0.6},
    ],
    "bored": [
        {"pitch": 15, "yaw": -10, "roll": -5, "antennas": [-15, -15], "duration": 0.8},
        {"pitch": 18, "yaw": 5, "roll": 3, "antennas": [-10, -10], "duration": 1.0},
        {"pitch": 0, "yaw": 0, "roll": 0, "antennas": [0, 0], "duration": 0.8},
    ],
    "anxious": [
        {"pitch": 5, "yaw": -8, "roll": -3, "antennas": [-10, -15], "duration": 0.3},
        {"pitch": 3, "yaw": 8, "roll": 3, "antennas": [-15, -10], "duration": 0.3},
        {"pitch": 5, "yaw": -5, "roll": -2, "antennas": [-12, -12], "duration": 0.3},
        {"pitch": 3, "yaw": 5, "roll": 2, "antennas": [-10, -10], "duration": 0.3},
        {"pitch": 0, "yaw": 0, "roll": 0, "antennas": [0, 0], "duration": 0.5},
    ],
    "laughing": [
        {"pitch": -10, "yaw": 0, "roll": 5, "antennas": [25, 25], "duration": 0.2},
        {"pitch": -5, "yaw": 0, "roll": -5, "antennas": [20, 20], "duration": 0.2},
        {"pitch": -10, "yaw": 0, "roll": 5, "antennas": [25, 25], "duration": 0.2},
        {"pitch": -5, "yaw": 0, "roll": -5, "antennas": [20, 20], "duration": 0.2},
        {"pitch": -8, "yaw": 0, "roll": 3, "antennas": [22, 22], "duration": 0.2},
        {"pitch": 0, "yaw": 0, "roll": 0, "antennas": [0, 0], "duration": 0.4},
    ],
    "thoughtful": [
        {"pitch": 5, "yaw": 15, "roll": 8, "antennas": [10, -5], "duration": 0.6},
        {"pitch": 8, "yaw": 12, "roll": 5, "antennas": [5, -5], "duration": 1.0},
        {"pitch": 0, "yaw": 0, "roll": 0, "antennas": [0, 0], "duration": 0.6},
    ],
    "cheerful": [
        {"pitch": -8, "yaw": 10, "roll": 5, "antennas": [25, 25], "duration": 0.3},
        {"pitch": -8, "yaw": -10, "roll": -5, "antennas": [25, 25], "duration": 0.3},
        {"pitch": -10, "yaw": 0, "roll": 0, "antennas": [30, 30], "duration": 0.3},
        {"pitch": 0, "yaw": 0, "roll": 0, "antennas": [0, 0], "duration": 0.5},
    ],
    "grateful": [
        {"pitch": 10, "yaw": 0, "roll": 0, "antennas": [15, 15], "duration": 0.5},
        {"pitch": 12, "yaw": 0, "roll": 0, "antennas": [10, 10], "duration": 0.8},
        {"pitch": 0, "yaw": 0, "roll": 0, "antennas": [0, 0], "duration": 0.6},
    ],
    "welcoming": [
        {"pitch": -5, "yaw": 0, "roll": 0, "antennas": [30, 30], "duration": 0.4},
        {"pitch": -5, "yaw": 15, "roll": 5, "antennas": [25, 25], "duration": 0.4},
        {"pitch": -5, "yaw": -15, "roll": -5, "antennas": [25, 25], "duration": 0.4},
        {"pitch": 0, "yaw": 0, "roll": 0, "antennas": [0, 0], "duration": 0.5},
    ],
    "enthusiastic": [
        {"pitch": -12, "yaw": 10, "roll": 5, "antennas": [35, 35], "duration": 0.25},
        {"pitch": -12, "yaw": -10, "roll": -5, "antennas": [35, 35], "duration": 0.25},
        {"pitch": -15, "yaw": 0, "roll": 0, "antennas": [40, 40], "duration": 0.25},
        {"pitch": -12, "yaw": 8, "roll": 3, "antennas": [35, 35], "duration": 0.25},
        {"pitch": 0, "yaw": 0, "roll": 0, "antennas": [0, 0], "duration": 0.5},
    ],
    "attentive": [
        {"pitch": -5, "yaw": 0, "roll": 0, "antennas": [20, 20], "duration": 0.4},
        {"pitch": -8, "yaw": 0, "roll": 3, "antennas": [15, 15], "duration": 0.8},
        {"pitch": 0, "yaw": 0, "roll": 0, "antennas": [0, 0], "duration": 0.5},
    ],
    "angry": [
        {"pitch": 10, "yaw": 0, "roll": 0, "antennas": [-30, -30], "duration": 0.3},
        {"pitch": 8, "yaw": -5, "roll": -3, "antennas": [-35, -35], "duration": 0.5},
        {"pitch": 10, "yaw": 5, "roll": 3, "antennas": [-30, -30], "duration": 0.5},
        {"pitch": 0, "yaw": 0, "roll": 0, "antennas": [0, 0], "duration": 0.5},
    ],
    "afraid": [
        {"pitch": 10, "yaw": -10, "roll": -5, "antennas": [-25, -25], "duration": 0.2},
        {"pitch": 8, "yaw": 10, "roll": 5, "antennas": [-20, -20], "duration": 0.2},
        {"pitch": 12, "yaw": -5, "roll": -3, "antennas": [-25, -25], "duration": 0.3},
        {"pitch": 15, "yaw": 0, "roll": 0, "antennas": [-30, -30], "duration": 0.5},
        {"pitch": 0, "yaw": 0, "roll": 0, "antennas": [0, 0], "duration": 0.6},
    ],
    "loving": [
        {"pitch": -5, "yaw": 10, "roll": 10, "antennas": [20, 20], "duration": 0.5},
        {"pitch": -5, "yaw": -10, "roll": -10, "antennas": [20, 20], "duration": 0.5},
        {"pitch": -8, "yaw": 0, "roll": 0, "antennas": [25, 25], "duration": 0.6},
        {"pitch": 0, "yaw": 0, "roll": 0, "antennas": [0, 0], "duration": 0.5},
    ],
    "idle": [
        {"pitch": -2, "yaw": 3, "roll": 1, "antennas": [5, 3], "duration": 0.8},
        {"pitch": -3, "yaw": -2, "roll": -1, "antennas": [3, 5], "duration": 1.0},
        {"pitch": -1, "yaw": 4, "roll": 2, "antennas": [4, 2], "duration": 0.9},
        {"pitch": -2, "yaw": -3, "roll": -1, "antennas": [2, 4], "duration": 1.0},
        {"pitch": 0, "yaw": 0, "roll": 0, "antennas": [0, 0], "duration": 0.8},
    ],
}


# ── Helpers ───────────────────────────────────────────────────────────

def deg2rad(degrees: float) -> float:
    return degrees * math.pi / 180.0


async def daemon_get(path: str) -> dict:
    """GET request to the daemon REST API."""
    try:
        resp = await http_client.get(f"{DAEMON_URL}{path}")
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as e:
        return {"status": "error", "message": f"Daemon returned {e.response.status_code}: {e.response.text}"}
    except httpx.ConnectError:
        return {"status": "error", "message": "Cannot connect to daemon. Is it running on " + DAEMON_URL + "?"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


async def daemon_post(path: str, json_body: dict = None) -> dict:
    """POST request to the daemon REST API."""
    try:
        resp = await http_client.post(f"{DAEMON_URL}{path}", json=json_body)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as e:
        return {"status": "error", "message": f"Daemon returned {e.response.status_code}: {e.response.text}"}
    except httpx.ConnectError:
        return {"status": "error", "message": "Cannot connect to daemon. Is it running on " + DAEMON_URL + "?"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def clamp(value: float, limit: float) -> float:
    return max(-limit, min(limit, value))


async def present_state() -> dict:
    """The robot's current pose, or {} if the daemon didn't answer."""
    state = await daemon_get("/api/state/full")
    return state if isinstance(state, dict) and "head_pose" in state else {}


_HEAD_AXES = ("x", "y", "z", "roll", "pitch", "yaw")

# The last whole-body target the bridge asked the daemon for.
#
# Held axes are restated from *this*, not from measured state. Measured state
# carries servo error and gravity sag, so echoing it back as a command bakes
# that error into the target -- and it compounds. Rotating the body four times
# walked the head from -0.5 to -11.6 degrees of yaw before this cache existed.
# Commanded values don't drift.
# The cache is only trustworthy while the bridge is the only thing driving the
# robot. The dashboard, another app or a direct daemon call can move it behind
# our back, and a stale target would then yank the robot back to a pose nobody
# asked for. So it expires: a run of blocks fires well inside this window and
# stays drift-free, while a session picked up later starts from reality.
_TARGET_TTL = 5.0  # seconds

_last_target: dict = {}
_last_target_at: float = 0.0
_last_target_lock = threading.Lock()


def _remember_target(goto_body: dict) -> None:
    global _last_target_at
    with _last_target_lock:
        for key in ("head_pose", "antennas", "body_yaw"):
            if key in goto_body:
                _last_target[key] = goto_body[key]
        _last_target_at = time.monotonic()


def _fresh_target() -> dict:
    """The cached target, or {} once it's too old to trust."""
    with _last_target_lock:
        if not _last_target or time.monotonic() - _last_target_at > _TARGET_TTL:
            return {}
        return dict(_last_target)


def _forget_target() -> None:
    """Drop the cache after a move whose end pose we can't predict.

    Recorded moves (dances, library emotions, wake/sleep) put the robot
    somewhere we never commanded, so the cache would be a lie. Clearing it
    makes the next held axis fall back to a fresh reading.
    """
    with _last_target_lock:
        _last_target.clear()


async def head_pose_holding(**updates) -> dict:
    """A full head_pose: the axes in `updates`, the rest left where they are.

    head_pose is all-or-nothing to the daemon -- there's no way to say "rotate
    but leave the translation alone". A route that only wants to change yaw has
    to restate x/y/z alongside it, or the head slides back to centre as it
    turns.
    """
    base = dict(_fresh_target().get("head_pose") or {})
    if not base:  # nothing recent to hold -- ask the robot where it is
        base = dict((await present_state()).get("head_pose") or {})
    pose = {axis: float(base.get(axis, 0.0)) for axis in _HEAD_AXES}
    pose.update({k: v for k, v in updates.items() if v is not None})
    return pose


async def antennas_holding(left: Optional[float] = None,
                           right: Optional[float] = None) -> list:
    """A full [left, right] antenna pair (radians), holding whichever is None.

    The antenna counterpart of head_pose_holding: the daemon wants both
    antennas in every goto, so moving just one means restating the other.
    """
    base = _fresh_target().get("antennas")
    if not base:  # nothing recent to hold -- ask the robot where it is
        base = (await present_state()).get("antennas_position")
    pair = [float(v) for v in base] if base else [0.0, 0.0]
    if left is not None:
        pair[0] = left
    if right is not None:
        pair[1] = right
    return pair


async def goto_preserving(
    duration: float,
    head_pose: Optional[dict] = None,
    antennas: Optional[list] = None,
    body_yaw: Optional[float] = None,
    interpolation: str = "minjerk",
) -> dict:
    """Send a goto that holds whatever the caller didn't ask to move.

    The daemon reads a goto as a *whole-body* target: every axis left out of
    the body is driven back to its default. So a bare antenna goto snaps the
    head to neutral, cutting off a head move still in flight -- two consecutive
    blocks cancel each other instead of stacking. Restating the untouched axes
    keeps them put.

    Held axes come from the last commanded target, falling back to a live
    reading only when there isn't one yet. If both are unavailable we send just
    what the caller gave, which is the old behaviour -- no worse than before.
    """
    if head_pose is None or antennas is None or body_yaw is None:
        cached = _fresh_target()
        head_pose = head_pose if head_pose is not None else cached.get("head_pose")
        antennas = antennas if antennas is not None else cached.get("antennas")
        body_yaw = body_yaw if body_yaw is not None else cached.get("body_yaw")

    if head_pose is None or antennas is None or body_yaw is None:
        state = await present_state()
        if state:
            if head_pose is None:
                head_pose = state.get("head_pose")
            if antennas is None:
                antennas = state.get("antennas_position")
            if body_yaw is None:
                body_yaw = state.get("body_yaw")

    goto_body: dict = {"duration": duration, "interpolation": interpolation}
    if head_pose is not None:
        goto_body["head_pose"] = head_pose
    if antennas is not None:
        goto_body["antennas"] = list(antennas)
    if body_yaw is not None:
        goto_body["body_yaw"] = body_yaw

    result = await daemon_post("/api/move/goto", goto_body)
    if isinstance(result, dict) and "uuid" in result:
        _remember_target(goto_body)
    return result


async def track_move(result: dict):
    """Track a move UUID returned by the daemon for later stopping."""
    if isinstance(result, dict) and "uuid" in result:
        running_move_uuids.append(result["uuid"])


async def run_move(result: dict, duration: float) -> Optional[str]:
    """Track a move and hold the request open until the robot has made it.

    The daemon's goto is fire-and-forget, but Block Console runs blocks one
    after another and takes the HTTP response as "that block is done". Without
    this the next block fires immediately and its goto lands on top of one
    still in flight -- and since a goto is a whole-body target, the loser isn't
    merely ignored, it gets dragged to the new pose mid-movement.
    `play_emotion` has always waited for exactly this reason.
    """
    await track_move(result)
    uuid = result.get("uuid")
    if uuid:
        await wait_for_move(uuid, timeout=duration + 5.0)
    return uuid


async def wait_for_move(uuid: str, timeout: float = 30.0):
    """Block until a daemon move finishes (or timeout).

    Recorded moves are fire-and-forget on the daemon side, but Block Console
    runs blocks sequentially and expects the HTTP call to last as long as the
    movement does -- otherwise consecutive blocks cut each other off.
    """
    elapsed = 0.0
    interval = 0.1
    while elapsed < timeout:
        await asyncio.sleep(interval)
        elapsed += interval
        running = await daemon_get("/api/move/running")
        if not isinstance(running, list):
            return  # daemon error -- don't hang the block
        if not any(m.get("uuid") == uuid for m in running if isinstance(m, dict)):
            return
    logger.warning(f"Move {uuid} still running after {timeout}s, not waiting further")


async def stop_all_running_moves():
    """Stop all currently running moves."""
    # First check what's actually running
    running = await daemon_get("/api/move/running")
    if isinstance(running, list):
        for move in running:
            if isinstance(move, dict) and "uuid" in move:
                await daemon_post("/api/move/stop", {"uuid": move["uuid"]})
    # Also try our tracked UUIDs
    for uuid in running_move_uuids:
        await daemon_post("/api/move/stop", {"uuid": uuid})
    running_move_uuids.clear()
    # A stopped move leaves the robot mid-trajectory, not at the target we
    # asked for, so the cached target no longer describes where it is.
    _forget_target()


# ── TTS helpers ───────────────────────────────────────────────────────

def _tts_init():
    """Initialize TTS on the dedicated TTS thread. Called once at startup."""
    global _tts_engine, _edge_tts_available
    # Try edge-tts first (best quality, cross-platform)
    try:
        import edge_tts  # noqa: F401
        _edge_tts_available = True
        logger.info(f"TTS: edge-tts available (voice: {_EDGE_TTS_VOICE})")
    except ImportError:
        logger.warning("edge-tts not installed, falling back to platform TTS")
    # Set up fallback engine for when edge-tts fails (e.g. no internet)
    if sys.platform == "darwin" and shutil.which("say"):
        _tts_engine = "macos_say"
        logger.info("TTS fallback: macOS built-in `say` command")
    elif sys.platform == "win32" and shutil.which("powershell"):
        _tts_engine = "windows_sapi"
        logger.info("TTS fallback: Windows SAPI via PowerShell")
    else:
        try:
            import pyttsx3
            _tts_engine = pyttsx3.init()
            _tts_engine.setProperty('rate', 150)
            _tts_engine.setProperty('volume', 0.9)
            logger.info("TTS fallback: pyttsx3 ready")
        except Exception as e:
            logger.warning(f"pyttsx3 init failed: {e}")
            _tts_engine = None
    if not _edge_tts_available and _tts_engine is None:
        logger.warning("TTS not available — no engines found")


def _play_mp3_blocking(tmp_path: str):
    """Play an MP3 file synchronously. Must run in a thread (blocks)."""
    try:
        if sys.platform == "darwin":
            subprocess.run(["afplay", tmp_path], check=True, timeout=30)
        elif sys.platform == "win32":
            import ctypes
            winmm = ctypes.windll.winmm
            buf = ctypes.create_string_buffer(256)
            mp3 = tmp_path.replace("/", "\\")
            # Unique alias per call to avoid collisions
            alias = f"tts_{id(buf)}"
            rc = winmm.mciSendStringA(f'open "{mp3}" type mpegvideo alias {alias}'.encode(), buf, 256, 0)
            if rc != 0:
                logger.error(f"mciSendString open failed (rc={rc})")
                return
            rc = winmm.mciSendStringA(f"play {alias} wait".encode(), buf, 256, 0)
            if rc != 0:
                logger.error(f"mciSendString play failed (rc={rc})")
            winmm.mciSendStringA(f"close {alias}".encode(), buf, 256, 0)
        else:
            if shutil.which("ffplay"):
                subprocess.run(
                    ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", tmp_path],
                    check=True, timeout=30,
                )
            elif shutil.which("mpv"):
                subprocess.run(["mpv", "--no-video", tmp_path], check=True, timeout=30)
            else:
                logger.error("No audio player found (need ffplay or mpv)")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


async def _tts_speak_edge(text: str) -> dict:
    """Speak text using edge-tts (async, neural voices)."""
    import edge_tts
    communicate = edge_tts.Communicate(text, voice=_EDGE_TTS_VOICE)
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        tmp_path = f.name
    await communicate.save(tmp_path)
    logger.info(f"edge-tts MP3 saved ({os.path.getsize(tmp_path)} bytes), playing...")
    # Play in thread so we don't block the event loop
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _play_mp3_blocking, tmp_path)
    return {"status": "ok", "message": f"Said: {text}"}


def _tts_speak_fallback(text: str) -> dict:
    """Speak text using platform-native fallback. Runs on dedicated TTS thread."""
    global _tts_engine
    if _tts_engine is None:
        return {"status": "error", "message": "TTS not available"}
    try:
        if _tts_engine == "macos_say":
            subprocess.run(["say", text], check=True, timeout=30)
        elif _tts_engine == "windows_sapi":
            safe_text = text.replace("'", "''")
            ps_cmd = (
                "Add-Type -AssemblyName System.Speech; "
                "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                f"$s.Speak('{safe_text}')"
            )
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                check=True, timeout=30,
                creationflags=_NO_WINDOW,
            )
        else:
            _tts_engine.say(text)
            _tts_engine.runAndWait()
        return {"status": "ok", "message": f"Said: {text}"}
    except Exception as e:
        logger.error(f"TTS fallback error: {e}")
        if _tts_engine not in ("macos_say", "windows_sapi"):
            _tts_engine = None
        return {"status": "error", "message": f"TTS error: {e}"}


# ── Camera helpers ────────────────────────────────────────────────────

def set_robot(reachy_mini) -> None:
    """Hand the bridge the live ReachyMini opened by the app framework.

    Called once by `main.BlocklyApp.run` before the server starts, and again
    with None on shutdown. The bridge only reads media (camera frames) from it;
    all motion goes through the daemon's REST API, which the app lock does not
    gate.
    """
    global _reachy_mini
    with _reachy_mini_lock:
        _reachy_mini = reachy_mini
    logger.info("Robot handle %s", "attached" if reachy_mini is not None else "detached")


def _connect_reachy_mini():
    """Return the injected ReachyMini, or None if the app hasn't attached one."""
    with _reachy_mini_lock:
        return _reachy_mini


def _open_direct_camera():
    """Open the camera directly via OpenCV after releasing daemon media."""
    global _cv_cap, _use_direct_cv
    import cv2
    with _cv_cap_lock:
        if _cv_cap is not None and _cv_cap.isOpened():
            return _cv_cap

        # Tell daemon to release camera hardware so we can grab it
        robot = _connect_reachy_mini()
        if robot is not None and not robot.media_released:
            try:
                robot.release_media()
                logger.info("Released daemon media for direct camera access")
            except Exception as e:
                logger.warning(f"Failed to release daemon media: {e}")

        # Try camera indices 0..2
        for idx in range(3):
            cap = cv2.VideoCapture(idx)
            if cap.isOpened():
                ret, frame = cap.read()
                if ret and frame is not None:
                    _cv_cap = cap
                    _use_direct_cv = True
                    logger.info(f"Direct OpenCV camera opened (index {idx})")
                    return _cv_cap
                cap.release()

        logger.error("No camera found via OpenCV")
        return None


def _release_camera():
    """Release the direct-OpenCV capture, if we ever opened one.

    The SDK handle itself is owned by the app framework, so it is only detached
    here (via set_robot(None) in the app's finally block), never closed.
    """
    global _cv_cap, _use_direct_cv
    with _cv_cap_lock:
        if _cv_cap is not None:
            _cv_cap.release()
            _cv_cap = None
            _use_direct_cv = False


_sdk_camera_retries = 0  # track consecutive None frames from SDK

def _get_frame() -> "np.ndarray | None":
    """Get a camera frame, trying SDK first then falling back to direct OpenCV."""
    global _sdk_camera_retries

    # If we already switched to direct OpenCV, use it
    if _use_direct_cv:
        with _cv_cap_lock:
            if _cv_cap is not None and _cv_cap.isOpened():
                ret, frame = _cv_cap.read()
                return frame if ret else None
        return None

    # Try SDK IPC first
    robot = _connect_reachy_mini()
    if robot is not None:
        try:
            frame = robot.media.get_frame()
            if frame is not None:
                _sdk_camera_retries = 0
                return frame
        except Exception as e:
            logger.warning(f"SDK camera read failed: {e}")

        _sdk_camera_retries += 1

        # No reconnect step here (unlike the desktop build): the handle belongs
        # to the app framework, so we can't rebuild its GStreamer pipeline.

        # After 3 consecutive failures, fall back to direct OpenCV
        if _sdk_camera_retries >= 3:
            logger.info("SDK camera returned no frames after retries, switching to direct OpenCV")
            cap = _open_direct_camera()
            if cap is not None:
                ret, frame = cap.read()
                return frame if ret else None

    return None


def _capture_frame_blocking() -> dict:
    """Capture a single frame from Reachy Mini's camera."""
    frame = _get_frame()

    if frame is None:
        return {"status": "error", "message": "No frame available from Reachy Mini camera"}

    try:
        import cv2
        success, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
        if not success:
            return {"status": "error", "message": "Failed to encode frame"}
        b64 = base64.b64encode(buffer.tobytes()).decode('utf-8')
        h, w = frame.shape[:2]
        return {"status": "ok", "image": f"data:image/jpeg;base64,{b64}", "width": w, "height": h}
    except Exception as e:
        return {"status": "error", "message": f"Frame encode error: {e}"}


# ── Face detection helpers ────────────────────────────────────────────

def _get_face_cascade():
    """Load the Haar cascade classifier (lazy)."""
    global _face_cascade
    if _face_cascade is None:
        try:
            import cv2
            cascade_path = cv2.data.haarcascades + 'haarcascade_frontalface_default.xml'
            _face_cascade = cv2.CascadeClassifier(cascade_path)
            if _face_cascade.empty():
                logger.error(f"Failed to load Haar cascade from {cascade_path}")
                _face_cascade = None
        except Exception as e:
            logger.error(f"Face cascade init error: {e}")
            _face_cascade = None
    return _face_cascade


def _detect_face_blocking():
    """Detect the largest face in the current camera frame.

    Returns (detected, norm_x, norm_y) where norm_x/y are in [-100, 100].
    """
    frame = _get_frame()
    if frame is None:
        return (False, 0.0, 0.0)

    import cv2
    cascade = _get_face_cascade()
    if cascade is None:
        return (False, 0.0, 0.0)

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(60, 60))

    if len(faces) == 0:
        return (False, 0.0, 0.0)

    # Pick the largest face
    largest = max(faces, key=lambda f: f[2] * f[3])
    x, y, w, h = largest
    center_x = x + w / 2.0
    center_y = y + h / 2.0
    frame_h, frame_w = frame.shape[:2]
    norm_x = ((center_x / frame_w) * 2.0 - 1.0) * 100.0
    norm_y = ((center_y / frame_h) * 2.0 - 1.0) * 100.0
    return (True, norm_x, norm_y)


async def _head_tracking_loop():
    """Background task: detect face, move head to follow it."""
    global _face_detected, _face_x, _face_y

    last_sent_x = 0.0
    last_sent_y = 0.0
    last_face_time = None
    returned_to_neutral = True

    logger.info("Head tracking loop started")

    while _head_tracking_enabled:
        try:
            detected, nx, ny = await asyncio.to_thread(_detect_face_blocking)

            with _face_lock:
                _face_detected = detected
                if detected:
                    _face_x = nx
                    _face_y = ny

            if detected:
                last_face_time = asyncio.get_event_loop().time()
                returned_to_neutral = False

                dx = abs(nx - last_sent_x)
                dy = abs(ny - last_sent_y)

                if dx > _FACE_MOVE_THRESHOLD or dy > _FACE_MOVE_THRESHOLD:
                    yaw_deg = -(nx / 100.0) * _HEAD_YAW_RANGE
                    pitch_deg = (ny / 100.0) * _HEAD_PITCH_RANGE

                    # Held axes come from the cache, so this stays a single
                    # request per frame even at tracking rate.
                    await goto_preserving(
                        duration=0.3,
                        head_pose={
                            "x": 0.0, "y": 0.0, "z": 0.0,
                            "roll": 0.0,
                            "pitch": deg2rad(pitch_deg),
                            "yaw": deg2rad(yaw_deg),
                        },
                    )
                    last_sent_x = nx
                    last_sent_y = ny
            else:
                if last_face_time is not None and not returned_to_neutral:
                    elapsed = asyncio.get_event_loop().time() - last_face_time
                    if elapsed >= _FACE_LOST_DELAY:
                        await goto_preserving(
                            duration=1.0,
                            head_pose={
                                "x": 0.0, "y": 0.0, "z": 0.0,
                                "roll": 0.0, "pitch": 0.0, "yaw": 0.0,
                            },
                        )
                        returned_to_neutral = True
                        last_sent_x = 0.0
                        last_sent_y = 0.0
                        with _face_lock:
                            _face_x = 0.0
                            _face_y = 0.0

            await asyncio.sleep(1.0 / _TRACKING_FPS)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Head tracking error: {e}")
            await asyncio.sleep(0.5)

    logger.info("Head tracking loop stopped")


def _stop_head_tracking():
    """Stop the head tracking background task."""
    global _head_tracking_enabled, _head_tracking_task, _face_detected
    _head_tracking_enabled = False
    if _head_tracking_task is not None and not _head_tracking_task.done():
        _head_tracking_task.cancel()
    _head_tracking_task = None
    with _face_lock:
        _face_detected = False


# ── App lifecycle ─────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client, _tts_executor
    http_client = httpx.AsyncClient(timeout=30.0)

    # Dedicated single-threaded executor for TTS — pyttsx3/COM must stay on one thread
    _tts_executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="tts_worker"
    )
    # Warm up: check edge-tts availability + init fallback engine
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(_tts_executor, _tts_init)

    # SDK connection is now lazy — created on first /camera request
    # to avoid stale GStreamer pipelines from early startup timing

    logger.info(f"Bridge started, proxying to daemon at {DAEMON_URL}")
    yield

    # Cleanup
    _stop_head_tracking()
    _release_camera()
    _tts_executor.shutdown(wait=False)
    await http_client.aclose()
    logger.info("Bridge stopped")


# ── FastAPI app ───────────────────────────────────────────────────────

app = FastAPI(title="Reachy Mini Bridge", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Request logging (skip noisy polling routes) ──────────────────────

_QUIET_PATHS = {"/status", "/volume", "/face_detected", "/face_position",
                "/speech_detected", "/state", "/head_pose", "/motors",
                "/camera"}

@app.middleware("http")
async def log_requests(request, call_next):
    if request.url.path not in _QUIET_PATHS:
        logger.info(f"{request.method} {request.url.path}")
    response = await call_next(request)
    return response


# ── Status ────────────────────────────────────────────────────────────

@app.get("/status")
async def status():
    result = await daemon_get("/api/daemon/status")
    if "state" in result:
        return {"status": "ok", "connected": True, "daemon": result}
    return {"status": "ok", "connected": False, "message": result.get("message", "Daemon not reachable")}


# ── Wake up / Sleep ───────────────────────────────────────────────────

@app.post("/wake_up")
async def wake_up():
    result = await daemon_post("/api/move/play/wake_up")
    _forget_target()
    await track_move(result)
    return {"status": "ok", "message": "Robot waking up", "uuid": result.get("uuid")}


@app.post("/go_to_sleep")
async def go_to_sleep():
    result = await daemon_post("/api/move/play/goto_sleep")
    _forget_target()
    await track_move(result)
    return {"status": "ok", "message": "Robot going to sleep", "uuid": result.get("uuid")}


# ── Head movement ─────────────────────────────────────────────────────

@app.post("/move_head")
async def move_head(req: MoveHeadRequest):
    direction = req.direction.lower().strip()
    if direction not in HEAD_DIRECTIONS:
        return {"status": "error", "message": f"Unknown direction: {direction}. Use: {', '.join(HEAD_DIRECTIONS.keys())}"}

    angles = HEAD_DIRECTIONS[direction]
    result = await goto_preserving(
        duration=DEFAULT_MOVE_DURATION,
        head_pose=await head_pose_holding(
            roll=deg2rad(angles["roll"]),
            pitch=deg2rad(angles["pitch"]),
            yaw=deg2rad(angles["yaw"]),
        ),
    )
    uuid = await run_move(result, DEFAULT_MOVE_DURATION)
    return {"status": "ok", "message": f"Moving head {direction}", "uuid": uuid}


@app.post("/move_head_custom")
async def move_head_custom(req: MoveHeadCustomRequest):
    result = await goto_preserving(
        duration=req.duration,
        head_pose=await head_pose_holding(
            roll=deg2rad(req.roll),
            pitch=deg2rad(req.pitch),
            yaw=deg2rad(req.yaw),
        ),
    )
    uuid = await run_move(result, req.duration)
    return {"status": "ok", "message": f"Moving head to pitch={req.pitch} yaw={req.yaw} roll={req.roll}", "uuid": uuid}


@app.post("/move_head_position")
async def move_head_position(req: HeadPositionRequest):
    """Slide the head without turning it: the x/y/z twin of move_head_custom."""
    x = clamp(req.x, HEAD_XYZ_LIMIT_MM)
    y = clamp(req.y, HEAD_XYZ_LIMIT_MM)
    z = clamp(req.z, HEAD_XYZ_LIMIT_MM)

    result = await goto_preserving(
        duration=req.duration,
        # mm in the block, metres on the wire.
        head_pose=await head_pose_holding(x=x / 1000.0, y=y / 1000.0, z=z / 1000.0),
    )
    uuid = await run_move(result, req.duration)

    message = f"Moving head to x={x}mm y={y}mm z={z}mm"
    if (x, y, z) != (req.x, req.y, req.z):
        message += f" (clamped to ±{HEAD_XYZ_LIMIT_MM:g}mm)"
    return {"status": "ok", "message": message, "uuid": uuid}


# ── Body rotation ─────────────────────────────────────────────────────

@app.post("/set_body_yaw")
async def set_body_yaw(req: BodyYawRequest):
    """Turn the body on its base, leaving the head and antennas alone."""
    angle = clamp(req.angle, BODY_YAW_LIMIT_DEG)

    result = await goto_preserving(duration=req.duration, body_yaw=deg2rad(angle))
    uuid = await run_move(result, req.duration)

    message = f"Body rotated to {angle}°"
    if angle != req.angle:
        message += f" (clamped to ±{BODY_YAW_LIMIT_DEG:g}°)"
    return {"status": "ok", "message": message, "uuid": uuid}


# ── Combined move ─────────────────────────────────────────────────────

@app.post("/move_together")
async def move_together(req: MoveTogetherRequest):
    """Move any mix of head, antennas and body in one goto, all at once.

    Fields left out hold their last commanded position, so this route does
    everything the single-axis routes do, plus lets them happen together.
    """
    moved: list[str] = []
    clamped: list[str] = []

    head_updates: dict = {}
    for axis in ("pitch", "yaw", "roll"):
        value = getattr(req, axis)
        if value is not None:
            head_updates[axis] = deg2rad(value)
            moved.append(f"{axis}={value:g}°")
    for axis in ("x", "y", "z"):
        value = getattr(req, axis)
        if value is not None:
            limited = clamp(value, HEAD_XYZ_LIMIT_MM)
            if limited != value:
                clamped.append(f"head {axis} to ±{HEAD_XYZ_LIMIT_MM:g}mm")
            head_updates[axis] = limited / 1000.0  # mm in the block, metres on the wire
            moved.append(f"{axis}={limited:g}mm")
    head_pose = await head_pose_holding(**head_updates) if head_updates else None

    antennas = None
    if req.left is not None or req.right is not None:
        antennas = await antennas_holding(
            left=deg2rad(req.left) if req.left is not None else None,
            right=deg2rad(req.right) if req.right is not None else None,
        )
        if req.left is not None:
            moved.append(f"left={req.left:g}°")
        if req.right is not None:
            moved.append(f"right={req.right:g}°")

    body_yaw = None
    if req.body_yaw is not None:
        limited = clamp(req.body_yaw, BODY_YAW_LIMIT_DEG)
        if limited != req.body_yaw:
            clamped.append(f"body to ±{BODY_YAW_LIMIT_DEG:g}°")
        body_yaw = deg2rad(limited)
        moved.append(f"body={limited:g}°")

    if not moved:
        return {"status": "ok", "message": "Nothing to move", "uuid": None}

    result = await goto_preserving(
        duration=req.duration,
        head_pose=head_pose,
        antennas=antennas,
        body_yaw=body_yaw,
    )
    uuid = await run_move(result, req.duration)

    message = "Moving together: " + ", ".join(moved)
    if clamped:
        message += " (clamped " + "; ".join(clamped) + ")"
    return {"status": "ok", "message": message, "uuid": uuid}


# ── Dance ─────────────────────────────────────────────────────────────

@app.post("/dance")
async def dance(req: DanceRequest):
    dance_name = req.dance_name.strip()

    # If no dance name given, pick the first available one
    if not dance_name:
        dances = await daemon_get(f"/api/move/recorded-move-datasets/list/{DANCES_DATASET}")
        if isinstance(dances, list) and len(dances) > 0:
            dance_name = dances[0]
        else:
            return {"status": "error", "message": "No dances available. Check HF_TOKEN and dataset access."}

    result = await daemon_post(f"/api/move/play/recorded-move-dataset/{DANCES_DATASET}/{dance_name}")
    _forget_target()
    await track_move(result)
    return {"status": "ok", "message": f"Dancing: {dance_name}", "uuid": result.get("uuid")}


@app.post("/stop_dance")
async def stop_dance():
    await stop_all_running_moves()
    return {"status": "ok", "message": "Dance stopped"}


@app.get("/list_dances")
async def list_dances():
    result = await daemon_get(f"/api/move/recorded-move-datasets/list/{DANCES_DATASET}")
    if isinstance(result, list):
        return {"status": "ok", "dances": result}
    return {"status": "error", "dances": [], "message": result.get("message", "Could not list dances")}


# ── Emotions ──────────────────────────────────────────────────────────

async def get_emotion_library() -> list[str]:
    """Clip names in the HF emotions library, cached after the first success.

    The daemon preloads this dataset at startup, so this is normally a local
    cache hit. Returns [] if the dataset is unreachable (offline), in which
    case callers fall back to the built-in EMOTION_POSES.
    """
    global _emotion_library
    if _emotion_library:
        return _emotion_library
    result = await daemon_get(f"/api/move/recorded-move-datasets/list/{EMOTIONS_DATASET}")
    if isinstance(result, list) and result:
        _emotion_library = result
    return _emotion_library


def resolve_emotion(emotion_key: str, library: list[str]) -> Optional[str]:
    """Map a friendly emotion name onto a clip in the HF emotions library."""
    if emotion_key in library:
        return emotion_key
    alias = EMOTION_ALIASES.get(emotion_key)
    if alias and alias in library:
        return alias
    # Library clips are numbered ("sad" -> "sad1"); take the lowest variant.
    numbered = sorted(c for c in library if c[:-1] == emotion_key and c[-1].isdigit())
    return numbered[0] if numbered else None


@app.post("/play_emotion")
async def play_emotion(req: EmotionRequest):
    emotion_key = req.emotion.lower().strip()

    # Preferred path: recorded emotion from the HF library (full-body
    # trajectory with synchronised audio).
    library = await get_emotion_library()
    clip = resolve_emotion(emotion_key, library) if library else None
    if clip is not None:
        result = await daemon_post(
            f"/api/move/play/recorded-move-dataset/{EMOTIONS_DATASET}/{clip}"
        )
        if "uuid" in result:
            _forget_target()
            await track_move(result)
            # Block for the duration of the move so consecutive emotion blocks
            # sequence rather than interrupt each other (matches the old
            # built-in pose behaviour).
            await wait_for_move(result["uuid"])
            return {
                "status": "ok",
                "message": f"Playing emotion: {emotion_key} ({clip})",
                "uuid": result["uuid"],
                "source": "library",
            }
        logger.warning(f"Emotion clip {clip} failed to play, falling back to built-in pose")

    # Fallback: built-in pose sequence (no audio).
    pose = EMOTION_POSES.get(emotion_key)
    if pose is None:
        known = sorted(set(library) | set(EMOTION_ALIASES) | set(EMOTION_POSES))
        return {"status": "error", "message": f"Unknown emotion: {emotion_key}. Use: {', '.join(known)}"}

    # Play the sequence of goto moves that make up this emotion
    last_uuid = None
    for step in pose:
        antennas = None
        if "antennas" in step:
            antennas = [deg2rad(step["antennas"][0]), deg2rad(step["antennas"][1])]
        # body_yaw is deliberately left out: these are head-and-antenna
        # animations, so a body the student turned earlier stays put.
        result = await goto_preserving(
            duration=step.get("duration", 0.5),
            head_pose={
                "x": 0.0, "y": 0.0, "z": 0.0,
                "roll": deg2rad(step.get("roll", 0)),
                "pitch": deg2rad(step.get("pitch", 0)),
                "yaw": deg2rad(step.get("yaw", 0)),
            },
            antennas=antennas,
        )
        await track_move(result)
        last_uuid = result.get("uuid")
        # Wait for this step to finish before starting the next
        await asyncio.sleep(step.get("duration", 0.5))

    return {"status": "ok", "message": f"Playing emotion: {emotion_key}", "uuid": last_uuid, "source": "builtin"}


@app.post("/stop_emotion")
async def stop_emotion():
    await stop_all_running_moves()
    return {"status": "ok", "message": "Emotion stopped"}


@app.get("/list_emotions")
async def list_emotions():
    library = await get_emotion_library()
    if not library:
        return {"status": "ok", "emotions": sorted(EMOTION_POSES.keys()), "source": "builtin"}
    # Friendly names first (so the console dropdown leads with the familiar
    # ones), then any remaining library clip.
    friendly = sorted(set(EMOTION_POSES) | set(EMOTION_ALIASES))
    extras = sorted(c for c in library if c not in friendly)
    return {"status": "ok", "emotions": friendly + extras, "source": "library"}


# ── Head tracking ─────────────────────────────────────────────────────

@app.post("/head_tracking")
async def head_tracking(req: HeadTrackingRequest):
    global _head_tracking_enabled, _head_tracking_task

    if req.enable:
        if _head_tracking_enabled:
            return {"status": "ok", "message": "Head tracking already enabled", "enabled": True}
        # Ensure motors are enabled
        await daemon_post("/api/motors/set_mode/enabled")
        _head_tracking_enabled = True
        _head_tracking_task = asyncio.create_task(_head_tracking_loop())
        return {"status": "ok", "message": "Head tracking enabled with face detection", "enabled": True}
    else:
        _stop_head_tracking()
        return {"status": "ok", "message": "Head tracking disabled", "enabled": False}


@app.get("/face_detected")
async def face_detected():
    with _face_lock:
        return {"status": "ok", "face_detected": _face_detected}


@app.get("/face_position")
async def face_position():
    with _face_lock:
        return {
            "status": "ok",
            "face_detected": _face_detected,
            "x": round(_face_x, 1),
            "y": round(_face_y, 1),
        }


@app.get("/detect_head")
async def detect_head():
    """On-demand head/face detection from the camera.

    Returns the (x, y) pixel-centre of the largest detected face,
    normalised to -100..100 (0,0 = centre of frame).
    Does NOT require head tracking to be enabled.
    """
    detected, nx, ny = await asyncio.to_thread(_detect_face_blocking)
    return {
        "status": "ok",
        "detected": detected,
        "x": round(nx, 1),
        "y": round(ny, 1),
    }


# ── Antennas ──────────────────────────────────────────────────────────

@app.post("/set_antennas")
async def set_antennas(req: AntennaRequest):
    result = await goto_preserving(
        duration=req.duration,
        antennas=[deg2rad(req.left), deg2rad(req.right)],
    )
    uuid = await run_move(result, req.duration)
    return {"status": "ok", "message": f"Antennas: left={req.left}° right={req.right}°", "uuid": uuid}


# ── Do nothing (idle animation) ───────────────────────────────────────

@app.post("/do_nothing")
async def do_nothing():
    pose = EMOTION_POSES["idle"]
    last_uuid = None
    for step in pose:
        antennas = None
        if "antennas" in step:
            antennas = [deg2rad(step["antennas"][0]), deg2rad(step["antennas"][1])]
        # body_yaw is deliberately left out: these are head-and-antenna
        # animations, so a body the student turned earlier stays put.
        result = await goto_preserving(
            duration=step.get("duration", 0.5),
            head_pose={
                "x": 0.0, "y": 0.0, "z": 0.0,
                "roll": deg2rad(step.get("roll", 0)),
                "pitch": deg2rad(step.get("pitch", 0)),
                "yaw": deg2rad(step.get("yaw", 0)),
            },
            antennas=antennas,
        )
        await track_move(result)
        last_uuid = result.get("uuid")
        await asyncio.sleep(step.get("duration", 0.5))

    return {"status": "ok", "message": "Idle animation complete", "uuid": last_uuid}


# ── Camera ────────────────────────────────────────────────────────────

@app.get("/camera")
async def camera():
    result = await asyncio.to_thread(_capture_frame_blocking)
    return result


@app.post("/set_camera")
async def set_camera(_req: SetCameraRequest):
    return {"status": "ok", "message": "Camera is accessed via Reachy Mini SDK (no index selection needed)"}


# ── Audio: volume control ────────────────────────────────────────────

@app.get("/volume")
async def get_volume():
    return await daemon_get("/api/volume/current")


@app.post("/set_volume")
async def set_volume(req: VolumeRequest):
    return await daemon_post("/api/volume/set", {"volume": req.volume})


@app.post("/test_sound")
async def test_sound():
    return await daemon_post("/api/volume/test-sound")


@app.post("/say")
async def say(req: SayRequest):
    if not req.text.strip():
        return {"status": "ok", "message": "Nothing to say"}
    engine_label = "edge-tts" if _edge_tts_available else str(_tts_engine)
    logger.info(f"TTS /say request: {req.text!r} (engine={engine_label})")
    # Try edge-tts first (natural neural voice)
    if _edge_tts_available:
        try:
            result = await _tts_speak_edge(req.text)
            logger.info(f"TTS /say result: {result}")
            return result
        except Exception as e:
            logger.warning(f"edge-tts failed, falling back to platform TTS: {e}")
    # Fallback to platform-native TTS
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(_tts_executor, _tts_speak_fallback, req.text)
    logger.info(f"TTS /say result: {result}")
    return result


# ── Microphone ────────────────────────────────────────────────────────

@app.get("/speech_detected")
async def speech_detected():
    """Check if speech is detected via the microphone DOA sensor."""
    result = await daemon_get("/api/state/doa")
    if result and "angle" in result:
        return {"status": "ok", "speech_detected": result.get("speech_detected", False), "angle": result.get("angle")}
    return {"status": "ok", "speech_detected": False, "message": "DOA not available"}


@app.post("/start_recording")
async def start_recording():
    return {
        "status": "error",
        "message": "Direct microphone recording not available via daemon REST API. "
                   "Use the 'speech detected?' block to check for voice activity.",
    }


@app.post("/stop_recording")
async def stop_recording():
    return {"status": "error", "message": "Direct microphone recording not available via daemon REST API."}


# ── Robot state ───────────────────────────────────────────────────────

@app.get("/state")
async def get_state():
    return await daemon_get("/api/state/full")


@app.get("/head_pose")
async def get_head_pose():
    return await daemon_get("/api/state/present_head_pose")


@app.get("/motors")
async def get_motors():
    return await daemon_get("/api/motors/status")


# ── Server ────────────────────────────────────────────────────────────

BRIDGE_HOST = "0.0.0.0"
BRIDGE_PORT = 8080


def run_bridge(stop_event: threading.Event,
               host: str = BRIDGE_HOST,
               port: int = BRIDGE_PORT) -> None:
    """Serve the bridge until `stop_event` is set, then shut down cleanly.

    Blocks for the lifetime of the app. `BlocklyApp.run` calls this on the
    thread it was given, so uvicorn owns no signal handlers and the app
    framework stays in charge of the process.
    """
    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    # The app framework already handles SIGINT/SIGTERM; uvicorn installing its
    # own handlers here would swallow them and leave the robot connection open.
    server.install_signal_handlers = lambda: None

    thread = threading.Thread(target=server.run, name="bridge_server", daemon=True)
    thread.start()

    try:
        stop_event.wait()
    finally:
        server.should_exit = True
        thread.join(timeout=10.0)
        if thread.is_alive():
            logger.warning("Bridge server did not stop within 10s")


if __name__ == "__main__":
    # Standalone dev mode: run the bridge with no robot attached. Motion still
    # works (it goes through the daemon), camera and face tracking do not.
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host=BRIDGE_HOST, port=BRIDGE_PORT)


