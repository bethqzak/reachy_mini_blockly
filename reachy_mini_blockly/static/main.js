/* Reachy Blocks settings page.
 *
 * This page is served from :8042 but calls the bridge on :8080 directly —
 * the same cross-origin path the Block Console takes from wadsih-liftoff.org.
 * So a working page here is real evidence the console will work too.
 */

const $ = (id) => document.getElementById(id);

let BRIDGE = "http://localhost:8080";
let camTimer = null;

// ── Bridge calls ────────────────────────────────────────────

async function bridgeGet(path) {
  const resp = await fetch(BRIDGE + path, { cache: "no-store" });
  if (!resp.ok) throw new Error(`${path} → HTTP ${resp.status}`);
  return resp.json();
}

async function bridgePost(path, body) {
  const resp = await fetch(BRIDGE + path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  if (!resp.ok) throw new Error(`${path} → HTTP ${resp.status}`);
  return resp.json();
}

// ── Setup ───────────────────────────────────────────────────

// True once /api/info has answered with a usable payload. Until then the page
// runs on the defaults above and keeps retrying on the status poll.
let infoLoaded = false;

async function loadInfo() {
  if (infoLoaded) return true;

  let info;
  try {
    const resp = await fetch("api/info", { cache: "no-store" });
    // Must check resp.ok: FastAPI answers 404 with valid JSON
    // ({"detail":"Not Found"}), so an unchecked .json() would happily hand us
    // an object whose bridge_url is undefined -- and BRIDGE would become the
    // string "undefined", sending every later call to a 404 on this origin.
    if (!resp.ok) return false;
    info = await resp.json();
  } catch {
    return false; // settings API not up yet; the retry will catch it
  }

  if (typeof info.bridge_url !== "string") return false;

  infoLoaded = true;
  BRIDGE = info.bridge_url;
  $("version").textContent = "v" + info.version;
  $("bridgeUrl").textContent = info.bridge_url;
  $("daemonUrl").textContent = info.daemon_url;
  $("consoleLink").href = info.console_url;
  setPill("pillRobot", info.robot_attached && info.media_available);
  return true;
}

function setPill(id, state) {
  const dot = $(id).querySelector(".dot");
  dot.className = "dot " + (state === null ? "wait" : state ? "ok" : "bad");
}

// ── Status polling ──────────────────────────────────────────

async function refreshStatus() {
  // If the page was opened while the app was still starting, /api/info would
  // have 404'd. Keep trying so the page heals instead of staying broken.
  await loadInfo();
  try {
    const s = await bridgeGet("/status");
    setPill("pillBridge", true);
    setPill("pillDaemon", !!s.connected);
    $("statusHint").textContent = s.connected
      ? "Everything is connected — you're good to go."
      : "The bridge is up but the robot daemon isn't answering. " +
        (s.message || "");
  } catch (err) {
    setPill("pillBridge", false);
    setPill("pillDaemon", null);
    $("statusHint").textContent =
      "Can't reach the bridge at " + BRIDGE + ". " +
      "If this page is open on a different computer from the robot, " +
      "the Block Console won't work either.";
  }
}

// ── Test actions ────────────────────────────────────────────

function report(message, ok) {
  const el = $("result");
  el.textContent = message;
  el.className = "result " + (ok ? "ok" : "err");
}

async function runAction(act, button) {
  button.disabled = true;
  try {
    let res;
    if (act === "say") {
      res = await bridgePost("/say", { text: $("sayText").value });
    } else if (act === "wake") {
      res = await bridgePost("/wake_up");
    } else if (act === "sleep") {
      res = await bridgePost("/go_to_sleep");
    } else if (act === "sound") {
      res = await bridgePost("/test_sound");
    }
    const failed = res && res.status === "error";
    report(
      (res && res.message) || (failed ? "Something went wrong." : "Done."),
      !failed
    );
  } catch (err) {
    report(String(err.message || err), false);
  } finally {
    button.disabled = false;
  }
}

async function setVolume(value) {
  try {
    // /set_volume takes an int 0..100, same scale as the slider.
    await bridgePost("/set_volume", { volume: value });
    report("Volume set to " + value + "%.", true);
  } catch (err) {
    report(String(err.message || err), false);
  }
}

async function loadVolume() {
  try {
    const v = await bridgeGet("/volume");
    const pct = Math.round(v.volume ?? 50);
    $("volume").value = pct;
    $("volumeOut").textContent = pct;
  } catch {
    /* leave the slider at its default; the status pill already says why */
  }
}

// ── Camera preview ──────────────────────────────────────────

async function tickCamera() {
  try {
    const shot = await bridgeGet("/camera");
    if (shot.status === "ok" && shot.image) {
      $("camImage").src = shot.image;
      $("camImage").hidden = false;
      $("camHint").textContent = `Live · ${shot.width}×${shot.height}`;
    } else {
      $("camHint").textContent = shot.message || "No frame available.";
    }
  } catch (err) {
    $("camHint").textContent = String(err.message || err);
  }
}

function toggleCamera() {
  if (camTimer) {
    clearInterval(camTimer);
    camTimer = null;
    $("camToggle").textContent = "Start preview";
    $("camImage").hidden = true;
    $("camHint").textContent = "Preview is off.";
    return;
  }
  $("camToggle").textContent = "Stop preview";
  $("camHint").textContent = "Starting…";
  tickCamera();
  camTimer = setInterval(tickCamera, 200); // ~5 fps is plenty for a check
}

// ── Voice chat ──────────────────────────────────────────────
// The key goes to this page's own server (:8042), never to the bridge: the
// Block Console can reach the bridge, and it has no business reading a key.

let voiceActive = false;
let voiceKeySet = false;

function voiceReport(message, ok) {
  const el = $("voiceResult");
  el.textContent = message;
  el.className = "result " + (ok ? "ok" : "err");
}

function showVoiceKey(k) {
  voiceKeySet = !!k.configured;
  $("voiceKeyHint").textContent = !voiceKeySet
    ? "No key saved. The Voice Chat blocks won't work until you add one."
    : k.source === "environment"
      ? `Using the OPENAI_API_KEY set on this computer (${k.hint}).`
      : `Key saved on this computer (${k.hint}).`;
  $("voiceKeyRemove").hidden = !(voiceKeySet && k.source === "saved");
  $("voiceTest").disabled = !voiceKeySet && !voiceActive;
}

async function voiceKeyCall(method, body) {
  const resp = await fetch("api/voice-key", {
    method,
    cache: "no-store",
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!resp.ok) throw new Error("HTTP " + resp.status);
  return resp.json();
}

async function loadVoiceKey() {
  try {
    showVoiceKey(await voiceKeyCall("GET"));
    return true;
  } catch {
    $("voiceKeyHint").textContent = "Still waiting on the app…";
    return false;
  }
}

async function saveVoiceKey() {
  const key = $("voiceKey").value.trim();
  if (!key) return voiceReport("Paste your OpenAI API key first.", false);
  try {
    const res = await voiceKeyCall("POST", { key });
    voiceReport(res.message, res.status === "ok");
    if (res.status === "ok") {
      $("voiceKey").value = ""; // don't leave it sitting in the page
      showVoiceKey(res);
    }
  } catch (err) {
    voiceReport(String(err.message || err), false);
  }
}

async function removeVoiceKey() {
  try {
    const res = await voiceKeyCall("DELETE");
    voiceReport(res.message, res.status === "ok");
    showVoiceKey(res);
  } catch (err) {
    voiceReport(String(err.message || err), false);
  }
}

function showVoiceActive(active) {
  voiceActive = active;
  $("voiceTest").textContent = active ? "Stop voice chat" : "Test voice chat";
  $("voiceTest").disabled = !voiceKeySet && !voiceActive;
}

async function toggleVoiceTest() {
  const button = $("voiceTest");
  button.disabled = true;
  try {
    const res = voiceActive
      ? await bridgePost("/voice_chat/stop")
      : await bridgePost("/voice_chat/start", {});
    const ok = res.status === "ok";
    voiceReport(res.message || (ok ? "Done." : "Something went wrong."), ok);
    if (ok) voiceActive = !voiceActive;
  } catch (err) {
    voiceReport(String(err.message || err), false);
  } finally {
    showVoiceActive(voiceActive);
  }
}

// A chat can end on its own (the time limit) or be started from the console,
// so keep the button honest. Also retries the key lookup if the settings API
// wasn't up yet when the page loaded.
let voiceKeyLoaded = false;

async function refreshVoice() {
  if (!voiceKeyLoaded) voiceKeyLoaded = await loadVoiceKey();
  try {
    const s = await bridgeGet("/voice_chat/status");
    if (voiceActive && !s.active && s.message) voiceReport("Voice chat " + s.message + ".", true);
    showVoiceActive(!!s.active);
  } catch {
    /* the status pill already says the bridge is down */
  }
}

// ── Wire up ─────────────────────────────────────────────────

document.querySelectorAll("[data-act]").forEach((btn) => {
  btn.addEventListener("click", () => runAction(btn.dataset.act, btn));
});

$("sayText").addEventListener("keydown", (e) => {
  if (e.key === "Enter") document.querySelector('[data-act="say"]').click();
});

$("volume").addEventListener("input", (e) => {
  $("volumeOut").textContent = e.target.value;
});
$("volume").addEventListener("change", (e) => setVolume(Number(e.target.value)));

$("camToggle").addEventListener("click", toggleCamera);

$("voiceKeySave").addEventListener("click", saveVoiceKey);
$("voiceKeyRemove").addEventListener("click", removeVoiceKey);
$("voiceKey").addEventListener("keydown", (e) => {
  if (e.key === "Enter") saveVoiceKey();
});
$("voiceTest").addEventListener("click", toggleVoiceTest);

// This page is embedded in the dashboard, where target="_blank" has nowhere to
// go -- the host swallows it. Ask the app to open the console in the machine's
// own browser instead, and fall back to the plain link if that route isn't
// there (an older build, or the page opened directly on :8042).
$("consoleLink").addEventListener("click", async (e) => {
  const href = $("consoleLink").href;
  e.preventDefault();
  try {
    const resp = await fetch("api/open-console", { method: "POST" });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    report("Opening the Block Console in your browser…", true);
  } catch {
    if (href.startsWith("http")) window.open(href, "_blank", "noopener");
    else report("Still waiting on the app — try again in a moment.", false);
  }
});

window.addEventListener("beforeunload", () => {
  if (camTimer) clearInterval(camTimer);
});

(async function start() {
  await refreshStatus(); // loads /api/info first, then polls the bridge
  await loadVolume();
  await refreshVoice();
  setInterval(refreshStatus, 3000);
  setInterval(refreshVoice, 3000);
})();
