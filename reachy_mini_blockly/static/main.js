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

async function loadInfo() {
  const info = await (await fetch("api/info", { cache: "no-store" })).json();
  BRIDGE = info.bridge_url;
  $("version").textContent = "v" + info.version;
  $("bridgeUrl").textContent = info.bridge_url;
  $("daemonUrl").textContent = info.daemon_url;
  $("consoleLink").href = info.console_url;
  setPill("pillRobot", info.robot_attached && info.media_available);
  return info;
}

function setPill(id, state) {
  const dot = $(id).querySelector(".dot");
  dot.className = "dot " + (state === null ? "wait" : state ? "ok" : "bad");
}

// ── Status polling ──────────────────────────────────────────

async function refreshStatus() {
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

window.addEventListener("beforeunload", () => {
  if (camTimer) clearInterval(camTimer);
});

(async function start() {
  await loadInfo();
  await refreshStatus();
  await loadVolume();
  setInterval(refreshStatus, 3000);
})();
