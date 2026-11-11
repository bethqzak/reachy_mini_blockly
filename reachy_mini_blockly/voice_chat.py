"""Voice chat for the Block Console's Voice Chat blocks.

A session is a WebSocket to the OpenAI Realtime API with the robot's
microphone streamed up and the model's speech played back through the robot's
speaker, both via the `ReachyMini` handle the app framework gave us (the same
one the camera uses). The bridge exposes it as the `/voice_chat/*` routes the
console already calls.

The desktop launcher did this by proxying to the separate conversation app,
which cannot work here: only one app holds the robot at a time, and this is
it. So the session lives in-process instead.

Two things in here are deliberately conservative, because they spend the key
holder's money or are hard to judge without a robot on the desk:

  * A session ends itself after MAX_SESSION_SECONDS. Realtime audio is billed
    by the minute, and a student's program that never reaches its "stop voice
    chat" block would otherwise keep the line open all lesson.
  * The microphone is muted while the robot is talking. The mic sits a few
    centimetres from the speaker, and without this the model hears itself,
    takes it for the user interrupting, and cuts its own sentence off. The
    cost is that you can't talk over the robot. Set MUTE_MIC_WHILE_SPEAKING to
    False to try real barge-in on hardware whose echo cancellation is up to it.

The API key is read from here and nowhere else. It is set through the
settings page (webui.py, port 8042) and is never returned by any route, on
either port: the Block Console can start a chat but cannot read the key.

Audio I/O is confined to `_mic_loop`, `_play_loop` and `_start_audio` /
`_stop_audio`. Those are the only places that touch `robot.media`, so they are
the only places to look if a new SDK changes its audio calls.
"""

import asyncio
import base64
import json
import logging
import os
import queue
import threading
import time
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger("reachy_mini_blockly.voice_chat")

# ── Configuration ─────────────────────────────────────────────────────

# Overridable so the session can be pointed at a stand-in server in tests, or
# at a newer model without a release.
REALTIME_URL = os.environ.get("REACHY_BLOCKS_REALTIME_URL", "wss://api.openai.com/v1/realtime")
REALTIME_MODEL = os.environ.get("REACHY_BLOCKS_REALTIME_MODEL", "gpt-realtime-2.1")
# Turns what the user said into text, for the "last user message" block. The
# conversation itself is speech-to-speech and doesn't depend on it.
TRANSCRIBE_MODEL = "gpt-4o-mini-transcribe"

API_SAMPLE_RATE = 24000  # the Realtime API's PCM rate, both directions
DEFAULT_VOICE = "coral"
DEFAULT_INSTRUCTIONS = "You are a friendly voice assistant."

MAX_SESSION_SECONDS = 300
CONNECT_TIMEOUT = 15.0

MUTE_MIC_WHILE_SPEAKING = True
# How long after the last of the robot's speech the mic stays shut: the
# speaker's own buffer plus the room's echo.
ECHO_TAIL_SECONDS = 0.4
# How far ahead of real time speech is handed to the speaker. Enough that a
# late thread wake-up doesn't leave a gap, little enough that "stop voice
# chat" silences the robot almost at once.
PLAYBACK_LEAD_SECONDS = 0.2

# ── API key ───────────────────────────────────────────────────────────
# One small file in the user's profile. Not the app's install directory: the
# dashboard replaces that on every update, and the key should outlive one.

KEY_FILE = Path.home() / ".reachy_mini_blockly" / "openai_api_key"
_key_lock = threading.Lock()


def get_api_key() -> Optional[str]:
    """The saved key, else OPENAI_API_KEY from the environment, else None."""
    with _key_lock:
        try:
            saved = KEY_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            saved = ""
    return saved or os.environ.get("OPENAI_API_KEY", "").strip() or None


def save_api_key(key: str) -> None:
    """Store the key. Raises ValueError if it doesn't look like one."""
    key = key.strip()
    if not key.startswith("sk-") or len(key) < 20 or any(c.isspace() for c in key):
        raise ValueError("That doesn't look like an OpenAI API key. They start with \"sk-\".")
    with _key_lock:
        KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        KEY_FILE.write_text(key, encoding="utf-8")
        try:
            os.chmod(KEY_FILE, 0o600)  # owner-only; a no-op on Windows
        except OSError:
            pass


def clear_api_key() -> None:
    with _key_lock:
        try:
            KEY_FILE.unlink()
        except FileNotFoundError:
            pass


def key_status() -> dict:
    """What the settings page may show: that a key exists, never the key."""
    key = get_api_key()
    if not key:
        return {"configured": False}
    saved = KEY_FILE.exists()
    return {
        "configured": True,
        "hint": "…" + key[-4:],
        "source": "saved" if saved else "environment",
    }


# ── Audio conversion ──────────────────────────────────────────────────

def _resample(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Linear resampling. Plenty for speech, and keeps scipy out of the deps."""
    if src_rate == dst_rate or len(samples) == 0:
        return samples
    n_out = max(1, int(round(len(samples) * dst_rate / src_rate)))
    positions = np.minimum(np.arange(n_out) * (src_rate / dst_rate), len(samples) - 1)
    return np.interp(positions, np.arange(len(samples)), samples).astype(np.float32)


def mic_to_pcm16(chunk, rate: int) -> bytes:
    """A chunk from robot.media.get_audio_sample() as 24 kHz mono PCM16."""
    samples = np.asarray(chunk, dtype=np.float32)
    if samples.ndim == 2:
        if samples.shape[1] > samples.shape[0]:  # (channels, n) rather than (n, channels)
            samples = samples.T
        samples = samples[:, 0]
    samples = _resample(samples.reshape(-1), rate, API_SAMPLE_RATE)
    return (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


def pcm16_to_speaker(pcm: bytes, rate: int) -> np.ndarray:
    """24 kHz mono PCM16 as float32 at the speaker's rate."""
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    return _resample(samples, API_SAMPLE_RATE, rate)


# ── Session ───────────────────────────────────────────────────────────

def _explain(error: Exception) -> str:
    """A connection failure in words a teacher can act on."""
    code = getattr(getattr(error, "response", None), "status_code", None)
    if code == 401:
        return "OpenAI did not accept the API key. Check it on the Reachy Blocks settings page."
    if code == 429:
        return "OpenAI refused the connection: the key is out of credit or over its rate limit."
    if code is not None:
        return f"OpenAI refused the connection (HTTP {code})."
    if isinstance(error, (TimeoutError, OSError)):
        return "Could not reach OpenAI. Check the internet connection."
    return f"Voice chat failed: {error}"


class VoiceChatSession:
    """One conversation, from "start voice chat" to "stop voice chat"."""

    def __init__(self, robot, api_key: str, voice: str, instructions: str):
        self.robot = robot
        self.api_key = api_key
        self.voice = voice
        self.instructions = instructions

        self.active = False
        self.error: Optional[str] = None
        self.ended: Optional[str] = None  # why it stopped, once it has
        self.started_at = 0.0
        # In the order things were said. A user turn is entered as soon as the
        # API has the audio and its text filled in when transcription lands --
        # that finishes after the reply has begun, and appending on arrival
        # would list the answer before the question.
        self.transcript: list[dict] = []

        self._ready = asyncio.Event()   # set once the session is up, or has failed
        self._stop = threading.Event()  # tells the audio threads to finish
        self._task: Optional[asyncio.Task] = None
        self._mic_queue: asyncio.Queue = asyncio.Queue()
        self._play_queue: queue.Queue = queue.Queue()
        self._speaking_until = 0.0      # monotonic time the robot's speech ends
        self._threads: list[threading.Thread] = []

    # -- lifecycle --

    async def start(self) -> None:
        """Open the session. Returns once it is live or has failed (see .error)."""
        self._task = asyncio.create_task(self._run())
        await self._ready.wait()

    async def stop(self, reason: str = "stopped") -> None:
        if self.ended is None:
            self.ended = reason
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _run(self) -> None:
        from websockets.asyncio.client import connect

        try:
            async with connect(
                f"{REALTIME_URL}?model={REALTIME_MODEL}",
                additional_headers={"Authorization": f"Bearer {self.api_key}"},
                open_timeout=CONNECT_TIMEOUT,
                max_size=None,  # audio deltas can be large
            ) as ws:
                await ws.send(json.dumps(self._session_update()))
                sender = asyncio.create_task(self._send_mic(ws))
                try:
                    async with asyncio.timeout(MAX_SESSION_SECONDS):
                        async for raw in ws:
                            if not self._handle(json.loads(raw)):
                                break
                except TimeoutError:
                    self.ended = f"ended after the {MAX_SESSION_SECONDS // 60}-minute limit"
                    logger.info("Voice chat hit its time limit")
                finally:
                    sender.cancel()
            if self.ended is None:
                self.ended = self.error or "OpenAI closed the connection"
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.error = self.error or _explain(e)
            self.ended = self.error
            logger.warning(f"Voice chat ended: {self.error}")
        finally:
            self.active = False
            self._stop_audio()
            self._ready.set()

    def _session_update(self) -> dict:
        return {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "model": REALTIME_MODEL,
                "instructions": self.instructions,
                "output_modalities": ["audio"],
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": API_SAMPLE_RATE},
                        "transcription": {"model": TRANSCRIBE_MODEL},
                        "turn_detection": {"type": "server_vad"},
                    },
                    "output": {
                        "format": {"type": "audio/pcm", "rate": API_SAMPLE_RATE},
                        "voice": self.voice,
                    },
                },
            },
        }

    # -- events from the API --

    def _handle(self, event: dict) -> bool:
        """Act on one server event. False means the session should end."""
        kind = event.get("type", "")

        if kind == "session.updated":
            # Our settings were accepted -- only now is it worth opening the mic.
            if not self.active:
                try:
                    self._start_audio()
                except Exception as e:
                    self.error = f"The robot's microphone or speaker isn't available: {e}"
                    return False
                self.active = True
                self.started_at = time.monotonic()
                self._ready.set()

        elif kind in ("response.output_audio.delta", "response.audio.delta"):
            self._speaking_until = max(self._speaking_until, time.monotonic() + 0.5)
            self._play_queue.put(base64.b64decode(event.get("delta", "")))

        elif kind == "input_audio_buffer.committed":
            self.transcript.append({"role": "user", "text": "", "item_id": event.get("item_id")})

        elif kind == "conversation.item.input_audio_transcription.completed":
            text = (event.get("transcript") or "").strip()
            for entry in reversed(self.transcript):
                if entry.get("item_id") == event.get("item_id") and entry["role"] == "user":
                    entry["text"] = text
                    break
            else:
                self.transcript.append({"role": "user", "text": text})

        elif kind in ("response.output_audio_transcript.done", "response.audio_transcript.done"):
            text = (event.get("transcript") or "").strip()
            if text:
                self.transcript.append({"role": "assistant", "text": text})

        elif kind == "input_audio_buffer.speech_started":
            # Only reachable mid-sentence with the mic left open: the user
            # spoke over the robot, so drop what it hadn't said yet.
            if not MUTE_MIC_WHILE_SPEAKING:
                self._flush_playback()

        elif kind == "error":
            message = (event.get("error") or {}).get("message", "unknown error")
            logger.warning(f"Realtime API error: {message}")
            if not self.active:
                # Refused before the session was up: a bad model or voice.
                self.error = f"OpenAI rejected the voice chat settings: {message}"
                return False

        return True

    def messages(self) -> list[dict]:
        """The transcript as the console wants it: [{role, text}], no blanks."""
        return [{"role": e["role"], "text": e["text"]} for e in self.transcript if e["text"]]

    def last(self, role: str) -> str:
        for entry in reversed(self.transcript):
            if entry["role"] == role and entry["text"]:
                return entry["text"]
        return ""

    def seconds_left(self) -> int:
        if not self.active:
            return 0
        return max(0, int(MAX_SESSION_SECONDS - (time.monotonic() - self.started_at)))

    # -- audio: the only code that touches robot.media --

    def _start_audio(self) -> None:
        media = self.robot.media
        in_rate = media.get_input_audio_samplerate()
        out_rate = media.get_output_audio_samplerate()
        if in_rate <= 0 or out_rate <= 0:
            raise RuntimeError("no audio device on the robot handle")
        media.start_recording()
        media.start_playing()
        loop = asyncio.get_running_loop()
        self._threads = [
            threading.Thread(target=self._mic_loop, args=(loop, in_rate),
                             name="voice_chat_mic", daemon=True),
            threading.Thread(target=self._play_loop, args=(out_rate,),
                             name="voice_chat_speaker", daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def _stop_audio(self) -> None:
        self._stop.set()
        self._flush_playback()
        if not self._threads:
            return  # never got as far as opening the mic
        for thread in self._threads:
            thread.join(timeout=2.0)
        self._threads = []
        media = getattr(self.robot, "media", None)
        for close in ("stop_recording", "stop_playing"):
            try:
                getattr(media, close)()
            except Exception as e:
                logger.warning(f"{close} failed: {e}")

    def _robot_is_speaking(self) -> bool:
        return not self._play_queue.empty() or time.monotonic() < self._speaking_until

    def _mic_loop(self, loop: asyncio.AbstractEventLoop, rate: int) -> None:
        media = self.robot.media
        while not self._stop.is_set():
            try:
                chunk = media.get_audio_sample()
            except Exception as e:
                logger.warning(f"Microphone read failed: {e}")
                time.sleep(0.1)
                continue
            if chunk is None or len(chunk) == 0:
                time.sleep(0.01)
                continue
            if MUTE_MIC_WHILE_SPEAKING and self._robot_is_speaking():
                continue
            try:
                loop.call_soon_threadsafe(self._mic_queue.put_nowait, mic_to_pcm16(chunk, rate))
            except RuntimeError:
                return  # the bridge's event loop has gone; so should we

    async def _send_mic(self, ws) -> None:
        while True:
            pcm = await self._mic_queue.get()
            await ws.send(json.dumps({
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(pcm).decode("ascii"),
            }))

    def _play_loop(self, rate: int) -> None:
        media = self.robot.media
        step = max(1, rate // 50)  # 20 ms pieces
        clock = 0.0                # when the audio handed over so far runs out
        odd = b""                  # a PCM16 sample split across two deltas
        while not self._stop.is_set():
            try:
                pcm = odd + self._play_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            cut = len(pcm) - (len(pcm) % 2)
            pcm, odd = pcm[:cut], pcm[cut:]
            samples = pcm16_to_speaker(pcm, rate)
            for start in range(0, len(samples), step):
                if self._stop.is_set():
                    return
                piece = samples[start:start + step]
                try:
                    media.push_audio_sample(piece)
                except Exception as e:
                    logger.warning(f"Speaker write failed: {e}")
                    break
                now = time.monotonic()
                clock = max(clock, now) + len(piece) / rate
                self._speaking_until = clock + ECHO_TAIL_SECONDS
                # The API sends speech much faster than it is spoken. Hand it
                # over at speaking pace, a little ahead, so a stop is heard
                # promptly and _speaking_until is the truth.
                ahead = clock - now - PLAYBACK_LEAD_SECONDS
                if ahead > 0:
                    time.sleep(ahead)

    def _flush_playback(self) -> None:
        while True:
            try:
                self._play_queue.get_nowait()
            except queue.Empty:
                return


# ── What the bridge calls ─────────────────────────────────────────────
# All of it runs on the bridge's event loop. The last session is kept after it
# ends so a program can stop the chat and then read the transcript.

_session: Optional[VoiceChatSession] = None
_start_lock = asyncio.Lock()


async def start(robot, voice: Optional[str] = None, instructions: Optional[str] = None) -> dict:
    global _session
    api_key = get_api_key()
    if not api_key:
        return {"status": "error",
                "message": "No OpenAI API key yet. Add one on the Reachy Blocks settings page "
                           "in the Reachy Mini dashboard."}
    if robot is None or getattr(robot, "media", None) is None:
        return {"status": "error",
                "message": "No robot attached. Start Reachy Blocks from the dashboard to use voice chat."}

    async with _start_lock:
        # A program run twice shouldn't keep talking to the first run's
        # assistant: start again with the instructions it was just given.
        if _session is not None and _session.active:
            await _session.stop("restarted")
        session = VoiceChatSession(
            robot, api_key,
            voice=(voice or DEFAULT_VOICE).strip(),
            instructions=(instructions or DEFAULT_INSTRUCTIONS).strip(),
        )
        _session = session
        await session.start()

    if not session.active:
        return {"status": "error", "message": session.error or session.ended or "Voice chat could not start."}
    logger.info(f"Voice chat started (voice={session.voice})")
    return {"status": "ok",
            "message": f"Started. Talk to the robot! (ends by itself after {MAX_SESSION_SECONDS // 60} minutes)"}


async def stop() -> dict:
    if _session is None or not _session.active:
        return {"status": "ok", "message": "Voice chat was not running"}
    await _session.stop()
    logger.info("Voice chat stopped")
    return {"status": "ok", "message": "Stopped"}


def status() -> dict:
    session = _session
    result = {"status": "ok", "active": bool(session and session.active)}
    if session is not None:
        result["seconds_left"] = session.seconds_left()
        if not session.active and session.ended:
            result["message"] = session.ended
    return result


def last_message(role: str) -> dict:
    return {"status": "ok", "text": _session.last(role) if _session else ""}


def transcript() -> dict:
    return {"status": "ok", "transcript": _session.messages() if _session else []}
