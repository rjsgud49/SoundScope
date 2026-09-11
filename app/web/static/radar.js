(() => {
  const { $, showToast, api, connectWs } = window.SS;

  const radarInfo = $("radarInfo");
  const eventLog = $("eventLog");
  const metaLine = $("metaLine");
  const radarStage = $("radarStage");
  const radarCanvas = $("radar");
  const radarCtx = radarCanvas.getContext("2d");
  const absoluteControls = $("absoluteControls");
  const pageTag = $("pageTag");
  const radarTitle = $("radarTitle");
  const modeHint = $("modeHint");

  let facingDeg = null;
  let radarMode = "sound_only";
  const markers = [];
  let cssSize = 640;

  function polar(cx, cy, r, deg) {
    const rad = ((deg - 90) * Math.PI) / 180;
    return { x: cx + Math.cos(rad) * r, y: cy + Math.sin(rad) * r };
  }

  function applyModeUi(mode) {
    radarMode = mode === "absolute" ? "absolute" : "sound_only";
    $("btnModeSound").classList.toggle("active", radarMode === "sound_only");
    $("btnModeAbsolute").classList.toggle("active", radarMode === "absolute");
    absoluteControls.classList.toggle("hidden", radarMode !== "absolute");
    if (radarMode === "sound_only") {
      pageTag.textContent = "Sound-only · relative direction";
      radarTitle.textContent = "Sound-only Radar";
      modeHint.textContent =
        "사운드만: 스테레오는 좌·우·대각(전방) 연속 각도. 뒤쪽(REAR)은 7.1에서만 확실합니다.";
      facingDeg = 0; // player forward = up
    } else {
      pageTag.textContent = "Absolute · compass + audio";
      radarTitle.textContent = "Absolute Radar";
      modeHint.textContent =
        "절대 방위: 나침반(시선) + 오디오 상대 방향을 합성합니다.";
    }
  }

  async function setMode(mode) {
    const data = await api("/api/radar/mode", {
      method: "POST",
      body: JSON.stringify({ radar_mode: mode }),
    });
    applyModeUi(data.radar_mode || mode);
    showToast(`Mode: ${data.radar_mode}`);
  }

  function resizeRadarToViewport() {
    const stage = radarStage.getBoundingClientRect();
    const avail = Math.floor(Math.min(stage.width, stage.height) - 16);
    const screenMin = Math.min(window.screen.width || 1920, window.screen.height || 1080);
    const targetByScreen = Math.floor(screenMin * 0.55);
    const size = Math.max(280, Math.min(avail > 0 ? avail : targetByScreen, 1200));
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    cssSize = size;
    radarCanvas.style.width = `${size}px`;
    radarCanvas.style.height = `${size}px`;
    radarCanvas.width = Math.floor(size * dpr);
    radarCanvas.height = Math.floor(size * dpr);
    radarCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function drawRadar() {
    const w = cssSize;
    const h = cssSize;
    const cx = w / 2;
    const cy = h / 2;
    const R = Math.min(w, h) * 0.4;
    const scale = w / 640;
    const fontCardinal = Math.max(12, Math.round(16 * scale));
    const fontMarker = Math.max(11, Math.round(13 * scale));
    const markerR = Math.max(5, 8 * scale);
    const centerR = Math.max(4, 6 * scale);

    radarCtx.clearRect(0, 0, w, h);
    radarCtx.strokeStyle = "rgba(139, 155, 176, 0.35)";
    radarCtx.lineWidth = Math.max(1, scale);
    for (let i = 1; i <= 4; i++) {
      radarCtx.beginPath();
      radarCtx.arc(cx, cy, (R * i) / 4, 0, Math.PI * 2);
      radarCtx.stroke();
    }
    radarCtx.beginPath();
    radarCtx.moveTo(cx - R, cy);
    radarCtx.lineTo(cx + R, cy);
    radarCtx.moveTo(cx, cy - R);
    radarCtx.lineTo(cx, cy + R);
    radarCtx.stroke();

    // Diagonal ticks (45°)
    radarCtx.strokeStyle = "rgba(139, 155, 176, 0.2)";
    for (const a of [45, 135, 225, 315]) {
      const inner = polar(cx, cy, R * 0.86, a);
      const outer = polar(cx, cy, R, a);
      radarCtx.beginPath();
      radarCtx.moveTo(inner.x, inner.y);
      radarCtx.lineTo(outer.x, outer.y);
      radarCtx.stroke();
    }

    const labels =
      radarMode === "sound_only"
        ? { n: "FRONT", e: "RIGHT", s: "REAR", w: "LEFT" }
        : { n: "N", e: "E", s: "S", w: "W" };

    radarCtx.fillStyle = "#8b9bb0";
    radarCtx.font = `600 ${fontCardinal}px IBM Plex Mono`;
    radarCtx.textAlign = "center";
    radarCtx.fillText(labels.n, cx, cy - R - 10 * scale);
    radarCtx.fillText(labels.e, cx + R + 14 * scale, cy + 4 * scale);
    radarCtx.fillText(labels.s, cx, cy + R + 18 * scale);
    radarCtx.fillText(labels.w, cx - R - 14 * scale, cy + 4 * scale);

    // Facing wedge: sound_only always forward-up; absolute uses compass
    const face = radarMode === "sound_only" ? 0 : facingDeg;
    if (face != null) {
      const a = Number(face);
      radarCtx.fillStyle = "rgba(232, 162, 58, 0.18)";
      radarCtx.strokeStyle = "#e8a23a";
      radarCtx.beginPath();
      radarCtx.moveTo(cx, cy);
      const p1 = polar(cx, cy, R * 0.92, a - 12);
      radarCtx.lineTo(p1.x, p1.y);
      radarCtx.arc(
        cx,
        cy,
        R * 0.92,
        ((a - 12 - 90) * Math.PI) / 180,
        ((a + 12 - 90) * Math.PI) / 180
      );
      radarCtx.closePath();
      radarCtx.fill();
      radarCtx.stroke();
    }

    radarCtx.fillStyle = "#3ecfb2";
    radarCtx.beginPath();
    radarCtx.arc(cx, cy, centerR, 0, Math.PI * 2);
    radarCtx.fill();

    const now = performance.now();
    for (let i = markers.length - 1; i >= 0; i--) {
      const m = markers[i];
      const fade = Math.max(0, 1 - (now - m.born) / m.fadeMs);
      if (fade <= 0) {
        markers.splice(i, 1);
        continue;
      }
      let ring = 0.62;
      const d = String(m.distance || "");
      if (d.includes("0~100") || d.includes("~50")) ring = 0.3;
      else if (d.includes("100~200") || d.includes("~200")) ring = 0.45;
      else if (d.includes("200~300") || d.includes("~300")) ring = 0.58;
      else if (d.includes("300~400") || d.includes("~400")) ring = 0.7;
      else if (d.includes("400~500") || d.includes("~500")) ring = 0.8;
      else if (d.includes("500~600") || d.includes("~600")) ring = 0.88;
      else ring = 0.95;

      const p = polar(cx, cy, R * ring, m.bearing);
      radarCtx.globalAlpha = fade;
      radarCtx.fillStyle = "#e8a23a";
      radarCtx.beginPath();
      radarCtx.arc(p.x, p.y, markerR, 0, Math.PI * 2);
      radarCtx.fill();
      radarCtx.fillStyle = "#e8eef6";
      radarCtx.font = `600 ${fontMarker}px IBM Plex Mono`;
      radarCtx.textAlign = "center";
      const topLabel =
        radarMode === "sound_only"
          ? m.direction || `${Math.round(m.bearing)}°`
          : `${Math.round(m.bearing)}°`;
      radarCtx.fillText(topLabel, p.x, p.y - 12 * scale);
      if (radarMode === "absolute") {
        radarCtx.fillText(m.distance, p.x, p.y + 18 * scale);
      } else {
        radarCtx.fillText(`${Math.round(m.bearing)}°`, p.x, p.y + 18 * scale);
      }
      radarCtx.globalAlpha = 1;
    }
    requestAnimationFrame(drawRadar);
  }

  function onMeter(msg) {
    if (msg.radar_mode) applyModeUi(msg.radar_mode);
    if (msg.manual_compass_heading != null) facingDeg = msg.manual_compass_heading;
    else if (msg.compass_heading_deg != null) facingDeg = msg.compass_heading_deg;

    const det = msg.detection;
    const detTxt = det
      ? `excess ${Number(det.excess_db || 0).toFixed(1)}dB · g ${Number(det.gunshot_score || 0).toFixed(2)} · evt ${msg.events_emitted || 0}`
      : "";
    const modeTxt = radarMode === "sound_only" ? "FRONT-up" : "N-up";
    radarInfo.textContent = `${modeTxt} · ${cssSize}px${detTxt ? " · " + detTxt : ""}`;
    metaLine.textContent = [
      msg.running ? "CAPTURE ON" : "CAPTURE OFF",
      radarMode,
      msg.device_name || "no device",
      detTxt || null,
    ]
      .filter(Boolean)
      .join("  ·  ");

    const dbg = $("detDebug");
    if (dbg && det) {
      dbg.textContent = [
        `score ${Number(det.gunshot_score || 0).toFixed(2)}`,
        `rej ${det.rejected_spectral ?? 0}`,
        `drop ${det.dropped_events ?? 0}`,
        det.last_error ? `err ${det.last_error}` : null,
      ]
        .filter(Boolean)
        .join(" · ");
    }
  }

  function bindDetSliders() {
    const pairs = [
      ["riseDb", "riseVal", (v) => Number(v).toFixed(1)],
      ["minPeak", "peakVal", (v) => Number(v).toFixed(3)],
      ["cooldownMs", "coolVal", (v) => String(Math.round(Number(v)))],
      ["gateScore", "gateVal", (v) => Number(v).toFixed(2)],
    ];
    for (const [id, labelId, fmt] of pairs) {
      const el = $(id);
      const lab = $(labelId);
      if (!el || !lab) continue;
      const sync = () => {
        lab.textContent = fmt(el.value);
      };
      el.addEventListener("input", sync);
      sync();
    }
  }

  function fillDetFromSettings(s) {
    if (!s) return;
    if (s.detection_rise_db != null) $("riseDb").value = s.detection_rise_db;
    if (s.detection_min_peak != null) $("minPeak").value = s.detection_min_peak;
    if (s.event_cooldown_ms != null) $("cooldownMs").value = s.event_cooldown_ms;
    if (s.gunshot_min_score != null) $("gateScore").value = s.gunshot_min_score;
    if ($("spectralGate")) $("spectralGate").checked = s.spectral_gate !== false;
    bindDetSliders();
  }

  async function applyDetection() {
    const body = {
      detection_rise_db: Number($("riseDb").value),
      detection_min_peak: Number($("minPeak").value),
      event_cooldown_ms: Number($("cooldownMs").value),
      gunshot_min_score: Number($("gateScore").value),
      spectral_gate: Boolean($("spectralGate").checked),
    };
    const data = await api("/api/detection/settings", {
      method: "POST",
      body: JSON.stringify(body),
    });
    fillDetFromSettings(data.settings);
    showToast("Detection settings applied");
  }

  function onEvent(msg) {
    if (msg.radar_mode) applyModeUi(msg.radar_mode);
    markers.push({
      bearing: Number(msg.bearing_deg) || 0,
      distance: msg.display_distance || msg.distance?.label || "?",
      direction: msg.display_direction || msg.relative?.label || "?",
      born: performance.now(),
      fadeMs: msg.fade_ms || 900,
    });
    const line = document.createElement("div");
    const rel = msg.display_direction || msg.relative?.label || "?";
    if (radarMode === "sound_only") {
      line.innerHTML = `<strong>${rel}</strong> ${Math.round(msg.bearing_deg)}°
        · ${msg.display_distance || "?"} · conf ${Math.round((msg.confidence || 0) * 100)}%`;
    } else {
      const comp =
        msg.compass_heading_deg != null ? `${Math.round(msg.compass_heading_deg)}°` : "?";
      line.innerHTML = `<strong>${Math.round(msg.bearing_deg)}°</strong> ${msg.display_distance}
        · rel ${rel} · face ${comp} · conf ${Math.round((msg.confidence || 0) * 100)}%`;
    }
    eventLog.prepend(line);
    while (eventLog.children.length > 12) eventLog.removeChild(eventLog.lastChild);
    if (msg.compass_heading_deg != null) facingDeg = msg.compass_heading_deg;
    showToast(
      radarMode === "sound_only"
        ? `DIR ${rel} (${Math.round(msg.bearing_deg)}°)`
        : `Event ${Math.round(msg.bearing_deg)}° / ${msg.display_distance}`
    );
  }

  async function simulate(relative_label) {
    try {
      await api("/api/events/simulate", {
        method: "POST",
        body: JSON.stringify({
          relative_label,
          compass_heading: radarMode === "sound_only" ? 0 : 180,
          distance_class: "DIST_100_200",
        }),
      });
    } catch (e) {
      showToast(e.message, true);
    }
  }

  $("btnModeSound").addEventListener("click", () => setMode("sound_only").catch((e) => showToast(e.message, true)));
  $("btnModeAbsolute").addEventListener("click", () => setMode("absolute").catch((e) => showToast(e.message, true)));
  $("btnSimLeft").addEventListener("click", () => simulate("LEFT"));
  $("btnSimRight").addEventListener("click", () => simulate("RIGHT"));
  $("btnSimFront").addEventListener("click", () => simulate("FRONT"));
  $("btnSimBack").addEventListener("click", () => simulate("BACK"));
  $("btnSimFL").addEventListener("click", () => simulate("FRONT_LEFT"));
  $("btnSimFR").addEventListener("click", () => simulate("FRONT_RIGHT"));

  $("btnManualApply").addEventListener("click", async () => {
    try {
      const v = $("manualHeading").value;
      if (v === "") throw new Error("Enter a heading 0-359");
      const heading_deg = Number(v);
      await api("/api/compass/manual", {
        method: "POST",
        body: JSON.stringify({ heading_deg }),
      });
      facingDeg = heading_deg;
      showToast(`Manual facing ${heading_deg}°`);
    } catch (e) {
      showToast(e.message, true);
    }
  });

  $("btnManualClear").addEventListener("click", async () => {
    try {
      await api("/api/compass/manual", {
        method: "POST",
        body: JSON.stringify({ heading_deg: null }),
      });
      $("manualHeading").value = "";
      showToast("OCR mode");
    } catch (e) {
      showToast(e.message, true);
    }
  });

  if ($("btnDetApply")) {
    $("btnDetApply").addEventListener("click", () =>
      applyDetection().catch((e) => showToast(e.message, true))
    );
  }
  bindDetSliders();

  api("/api/status")
    .then((status) => {
      applyModeUi(status.settings?.radar_mode || "sound_only");
      fillDetFromSettings(status.settings);
      if (status.settings?.manual_compass_heading != null) {
        $("manualHeading").value = status.settings.manual_compass_heading;
        facingDeg = status.settings.manual_compass_heading;
      }
    })
    .catch(() => applyModeUi("sound_only"));

  resizeRadarToViewport();
  window.addEventListener("resize", resizeRadarToViewport);
  if (window.ResizeObserver) {
    new ResizeObserver(resizeRadarToViewport).observe(radarStage);
  }

  connectWs({ onMeter, onEvent });
  requestAnimationFrame(drawRadar);
})();
