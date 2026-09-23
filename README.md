---
title: Reachy Blocks
emoji: 🧩
colorFrom: yellow
colorTo: blue
sdk: static
pinned: false
license: apache-2.0
short_description: Block coding for Reachy Mini with the WADSIH Block Console
tags:
  - reachy_mini
  - reachy_mini_python_app
---

# Reachy Blocks 🧩

Drive your **Reachy Mini** from drag-and-drop blocks, using the
[WADSIH Block Console](https://wadsih-liftoff.org/tools/blocks.html?preset=reachy).

Install this app from the Reachy Mini dashboard, press start, and open the
Block Console in your browser. The **Reachy Mini** block category lights up and
your blocks move the real robot: head poses, emotions, dances, speech, camera
snapshots and face tracking.

Built for classrooms — students write code by snapping blocks together, with no
Python, no terminal and no install steps beyond adding this app.

---

## What you get

| Blocks | What they do |
| --- | --- |
| 😊 **Emotions** | Play any of the 81 clips in the official Reachy emotions library, with audio |
| 💃 **Dances** | Play from the Pollen dances library |
| 🙂 **Head & antennas** | Preset directions, or exact pitch/yaw/roll with a duration |
| 🤸 **Move together** | Head, antennas and body in one smooth move, all at the same time |
| 🗣️ **Speech** | Text to speech through the robot's speaker (neural voices via edge-tts) |
| 📸 **Camera** | Grab a snapshot as an image the console can display |
| 👀 **Face tracking** | OpenCV face detection; the head follows the nearest face |
| 🔊 **Audio** | Volume, test sound, "is someone speaking?" |
| 🤖 **State** | Head pose, motor status, full robot state |

A settings page in the dashboard (`http://localhost:8042`) shows connection
status, a live camera preview, and test buttons for speech, wake/sleep and
volume — so you can check everything works before handing it to a class.

---

## Setup

1. Install **Reachy Blocks** from the Reachy Mini dashboard.
2. Press **start**. The app opens the bridge on port `8080`.
3. Open <https://wadsih-liftoff.org/tools/blocks.html?preset=reachy> in
   **Chrome or Edge**, on the same computer.
4. Pick the **Reachy Mini** category and start building.

No API keys, no accounts. The emotions and dances datasets are public, so no
`HF_TOKEN` is needed.

---

## Requirements, and one important limitation

**Reachy Mini SDK 1.9.0 or newer**, which means Python 3.11 or newer in the
apps venv. Tested with 1.11.0.

**The browser must be on the same computer as the robot's daemon.**

The Block Console reaches the bridge at a hardcoded `http://localhost:8080`.
That resolves correctly when the daemon and this app run on the same machine
as the browser — the normal setup for a **Reachy Mini Lite** plugged into your
computer.

On a **wireless Reachy Mini**, the app runs on the robot's onboard computer
while the browser is on your laptop, so `localhost:8080` points at the laptop
and finds nothing. Supporting that needs a change on the Block Console side (a
configurable bridge host) plus a fix for browser mixed-content blocking, since
an HTTPS page cannot fetch `http://<robot-ip>:8080`.

**Use Chrome or Edge.** They treat `localhost` as a trustworthy origin and
allow the plain-HTTP request from the HTTPS console page. Safari has no such
exception and no setting to enable one, so blocks fail silently there.

---

## How it works

```
Block Console (browser)          this app (managed by the dashboard)
wadsih-liftoff.org        →      bridge  :8080  ──→  daemon REST  :8000  →  robot
                                 settings page :8042
```

The bridge is a CORS-open FastAPI server. Almost every block becomes a proxied
call to the daemon's own REST API, which is what keeps this thin and keeps it
working across SDK upgrades. Only text to speech and camera snapshots are
implemented here, because the daemon has no route for them. Face tracking is
also done in-app (OpenCV on frames from the app's robot handle) even though
the daemon has shipped its own tracker since SDK 1.9: the in-app tracker also
feeds the "face detected?" and "face position" blocks, which need the face
coordinates, not just a robot that looks at you.

Verified against reachy-mini **1.11.0**. That release is mostly faster imports
and boots; nothing the bridge calls changed.

Motion goes through the daemon's REST API rather than the SDK, which the
robot's app lock does not gate — so the bridge and the app framework never
fight over the robot.

### Relation to the desktop launcher

This is the app edition of the WADSIH **Reachy Blocks** desktop launcher (a
packaged `.exe` / `.app` that started the daemon itself). Installing from the
dashboard replaces all of that, so the app edition drops:

- the tkinter GUI, the PyInstaller build and the first-run `uv` bootstrapper
- the daemon subprocess and its MuJoCo simulator fallback — the dashboard owns
  the daemon now
- voice chat and the conversation-app proxy — only one app can hold the robot
  at a time, so those blocks cannot work while this app is running

Everything else behaves the same, and the console needs no changes.

---

## Development

```bash
git clone https://github.com/bethqzak/reachy_mini_blockly
cd reachy_mini_blockly
uv pip install -e .

# Run the bridge on its own (motion works; camera needs a live robot handle)
python -m reachy_mini_blockly.bridge

# Or run the full app against a running daemon
python -m reachy_mini_blockly.main
```

Pushes to `main` are mirrored to the Hugging Face Space by
`.github/workflows/sync-to-hf.yml`, which is what makes "Update available"
appear in the dashboard.

Apache-2.0 licensed.
