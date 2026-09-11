(() => {
  const { $, showToast, api, dbToPercent, connectWs } = window.SS;

  const deviceSelect = $("deviceSelect");
  const rmsText = $("rmsText");
  const peakText = $("peakText");
  const rmsBar = $("rmsBar");
  const peakBar = $("peakBar");
  const metaLine = $("metaLine");
  const channelMeters = $("channelMeters");
  const bufferInfo = $("bufferInfo");
  const activeCapture = $("activeCapture");
  const silenceWarn = $("silenceWarn");
  const stallWarn = $("stallWarn");
  const waveCanvas = $("wave");
  const waveCtx = waveCanvas.getContext("2d");

  let silenceTicks = 0;
  let lastDeviceName = "";
  let lastBlocks = -1;
  let stallTicks = 0;

  async function loadDevices() {
    const [data, status] = await Promise.all([api("/api/devices"), api("/api/status")]);
    const activeId =
      status.capture?.device_name ||
      status.settings?.preferred_device_id ||
      null;

    deviceSelect.innerHTML = "";
    let defaultId = null;
    for (const d of data.devices) {
      const opt = document.createElement("option");
      opt.value = d.id;
      opt.textContent = `${d.is_default ? "★ " : ""}${d.name}  (${d.channels}ch)`;
      if (d.is_default) defaultId = d.id;
      deviceSelect.appendChild(opt);
    }

    const pick = activeId || defaultId;
    if (pick) {
      const match = [...deviceSelect.options].find((o) => o.value === pick);
      if (match) match.selected = true;
      else if (defaultId) deviceSelect.value = defaultId;
    }

    if (status.capture?.device_name) {
      activeCapture.textContent = `현재 캡처: ${status.capture.device_name} · ${
        status.capture.running ? "ON" : "OFF"
      }`;
    }
    return { data, status, defaultId };
  }

  function renderChannels(rmsList = []) {
    if (!rmsList.length) {
      channelMeters.innerHTML = "";
      return;
    }
    channelMeters.innerHTML = rmsList
      .map((rms, i) => {
        const db = 20 * Math.log10(Math.max(rms || 0, 1e-10));
        return `<div class="ch-row">
          <span>CH${i + 1}</span>
          <div class="mini-track"><div class="mini-fill" style="width:${dbToPercent(db)}%"></div></div>
          <span>${db.toFixed(1)} dB</span>
        </div>`;
      })
      .join("");
  }

  function drawWaveform(samples) {
    const w = waveCanvas.width;
    const h = waveCanvas.height;
    waveCtx.clearRect(0, 0, w, h);
    waveCtx.strokeStyle = "rgba(139, 155, 176, 0.35)";
    waveCtx.beginPath();
    waveCtx.moveTo(0, h / 2);
    waveCtx.lineTo(w, h / 2);
    waveCtx.stroke();
    if (!samples || !samples.length) return;
    waveCtx.strokeStyle = "#3ecfb2";
    waveCtx.lineWidth = 1.5;
    waveCtx.beginPath();
    const mid = h / 2;
    const amp = h * 0.42;
    for (let i = 0; i < samples.length; i++) {
      const x = (i / (samples.length - 1)) * (w - 1);
      const y = mid - samples[i] * amp;
      if (i === 0) waveCtx.moveTo(x, y);
      else waveCtx.lineTo(x, y);
    }
    waveCtx.stroke();
  }

  function onMeter(msg) {
    rmsText.textContent = `${msg.rms_db.toFixed(1)} dB`;
    peakText.textContent = `${msg.peak_db.toFixed(1)} dB`;
    rmsBar.style.width = `${dbToPercent(msg.rms_db)}%`;
    peakBar.style.width = `${dbToPercent(msg.peak_db)}%`;
    const filledSec = (msg.buffer_filled_frames || 0) / (msg.sample_rate || 48000);
    bufferInfo.textContent = `buffer ${filledSec.toFixed(2)} / ${Number(msg.buffer_seconds).toFixed(2)} s`;

    lastDeviceName = msg.device_name || lastDeviceName;
    const stalled = Boolean(msg.stalled) || (msg.running && (msg.buffer_filled_frames || 0) === 0 && (msg.blocks_captured || 0) === 0);
    const blocks = msg.blocks_captured || 0;
    if (msg.running && lastBlocks >= 0 && blocks === lastBlocks) {
      stallTicks += 1;
    } else {
      stallTicks = 0;
    }
    lastBlocks = blocks;

    const stateLabel = !msg.running ? "OFF" : stalled || stallTicks > 45 ? "STALLED" : "ON";
    activeCapture.textContent = `현재 캡처: ${msg.device_name || "?"} · ${stateLabel} · peak ${msg.peak_db.toFixed(1)} dB`;

    metaLine.textContent = [
      msg.running ? (stalled || stallTicks > 45 ? "CAPTURE STALLED" : "CAPTURE ON") : "CAPTURE OFF",
      msg.device_name || "no device",
      `${msg.sample_rate} Hz`,
      `${msg.channels} ch`,
      `blocks ${msg.blocks_captured}`,
      msg.error ? `ERR: ${msg.error}` : null,
    ]
      .filter(Boolean)
      .join("  ·  ");

    if (stallWarn) {
      stallWarn.hidden = !(msg.running && (stalled || stallTicks > 45));
    }

    // Warn when capturing but essentially silent for ~2s (not when stalled)
    if (msg.running && !stalled && stallTicks <= 45 && msg.peak_db <= -55) {
      silenceTicks += 1;
    } else {
      silenceTicks = 0;
    }
    silenceWarn.hidden = !(silenceTicks > 40) || (stallWarn && !stallWarn.hidden);

    renderChannels(msg.rms_per_channel || []);
    drawWaveform(msg.waveform);
  }

  async function applyDevice(deviceId) {
    await api("/api/devices/select", {
      method: "POST",
      body: JSON.stringify({ device_id: deviceId }),
    });
  }

  async function ensureCaptureRunning() {
    const status = await api("/api/status");
    if (status.capture?.running) {
      activeCapture.textContent = `현재 캡처: ${status.capture.device_name || "?"} · ON`;
      return status;
    }
    // Stopped (e.g. after Stop button / page refresh) → restart automatically
    await api("/api/capture/start", { method: "POST", body: "{}" });
    const again = await api("/api/status");
    activeCapture.textContent = `현재 캡처: ${again.capture?.device_name || "?"} · ${
      again.capture?.running ? "ON" : "OFF"
    }`;
    if (again.capture?.running) {
      showToast(`Capture auto-started: ${again.capture.device_name || "device"}`);
    } else {
      showToast(again.capture?.error || "Capture failed to start", true);
    }
    return again;
  }

  $("btnRefresh").addEventListener("click", async () => {
    try {
      await loadDevices();
      showToast("Device list refreshed");
    } catch (e) {
      showToast(e.message, true);
    }
  });

  $("btnUseDefault").addEventListener("click", async () => {
    try {
      const { defaultId } = await loadDevices();
      if (!defaultId) throw new Error("기본 출력 장치를 찾지 못했습니다");
      deviceSelect.value = defaultId;
      await applyDevice(defaultId);
      showToast(`기본 출력 적용: ${defaultId}`);
    } catch (e) {
      showToast(e.message, true);
    }
  });

  $("btnApply").addEventListener("click", async () => {
    try {
      await applyDevice(deviceSelect.value);
      showToast(`Selected: ${deviceSelect.value}`);
      silenceTicks = 0;
      silenceWarn.hidden = true;
    } catch (e) {
      showToast(e.message, true);
    }
  });

  $("btnStart").addEventListener("click", async () => {
    try {
      // Prefer full start endpoint (re-inits WASAPI); then sync selected device if needed
      const status = await api("/api/capture/start", { method: "POST", body: "{}" });
      const active = status.capture?.device_name;
      if (deviceSelect.value && active && deviceSelect.value !== active) {
        await applyDevice(deviceSelect.value);
      }
      showToast(`Capture started: ${deviceSelect.value || active || "?"}`);
      silenceTicks = 0;
      silenceWarn.hidden = true;
    } catch (e) {
      showToast(e.message, true);
    }
  });

  $("btnStop").addEventListener("click", async () => {
    try {
      await api("/api/capture/stop", { method: "POST", body: "{}" });
      showToast("Capture stopped");
    } catch (e) {
      showToast(e.message, true);
    }
  });

  $("btnSave").addEventListener("click", async () => {
    try {
      const data = await api("/api/save_wav", {
        method: "POST",
        body: JSON.stringify({ label: "ui" }),
      });
      showToast(`Saved: ${data.path}`);
    } catch (e) {
      showToast(e.message, true);
    }
  });

  window.addEventListener("keydown", (e) => {
    if (e.key.toLowerCase() === "s" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      $("btnSave").click();
    }
  });

  loadDevices()
    .then(() => ensureCaptureRunning())
    .catch((e) => showToast(e.message, true));
  connectWs({ onMeter });
  drawWaveform([]);
})();
