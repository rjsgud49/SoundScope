(() => {
  const { $, showToast, api, setConn } = window.SS;

  async function loadMonitorsAndSettings() {
    const [mon, status] = await Promise.all([api("/api/monitors"), api("/api/status")]);
    const monitorSelect = $("monitorSelect");
    monitorSelect.innerHTML = "";
    for (const m of mon.monitors) {
      if (m.index === 0) continue; // skip virtual desktop
      const opt = document.createElement("option");
      opt.value = m.index;
      const primary = m.left === 0 && m.top === 0 ? " [primary]" : "";
      opt.textContent = `${m.name}${primary} (${m.width}x${m.height}) @${m.left},${m.top}`;
      if (m.index === (status.settings.monitor_index || 1)) opt.selected = true;
      monitorSelect.appendChild(opt);
    }
    if (![...monitorSelect.options].some((o) => o.selected) && monitorSelect.options.length) {
      monitorSelect.options[0].selected = true;
    }
    const roi = status.settings.compass_roi || {};
    $("roiX").value = roi.x ?? 850;
    $("roiY").value = roi.y ?? 20;
    $("roiW").value = roi.w ?? 220;
    $("roiH").value = roi.h ?? 48;
    if (status.settings.manual_compass_heading != null) {
      $("manualHeading").value = status.settings.manual_compass_heading;
    }
    if (status.settings.tesseract_cmd) {
      $("tesseractCmd").value = status.settings.tesseract_cmd;
    } else {
      $("tesseractCmd").value = "C:\\Program Files\\Tesseract-OCR\\tesseract.exe";
    }
    setConn(null, "ready");
  }

  $("btnRoiDefault").addEventListener("click", async () => {
    try {
      const idx = Number($("monitorSelect").value);
      const data = await api(`/api/compass/roi/default?monitor_index=${idx}`, {
        method: "POST",
        body: "{}",
      });
      $("roiX").value = data.roi.x;
      $("roiY").value = data.roi.y;
      $("roiW").value = data.roi.w;
      $("roiH").value = data.roi.h;
      showToast(`Auto ROI on monitor ${data.monitor_index}`);
    } catch (e) {
      showToast(e.message, true);
    }
  });

  $("btnRoiSave").addEventListener("click", async () => {
    try {
      await api("/api/compass/roi", {
        method: "POST",
        body: JSON.stringify({
          monitor_index: Number($("monitorSelect").value),
          x: Number($("roiX").value),
          y: Number($("roiY").value),
          w: Number($("roiW").value),
          h: Number($("roiH").value),
        }),
      });
      showToast("ROI saved");
    } catch (e) {
      showToast(e.message, true);
    }
  });

  $("btnTopStrip").addEventListener("click", async () => {
    try {
      const idx = Number($("monitorSelect").value);
      $("ocrStatus").textContent = "top strip…";
      const data = await api(`/api/compass/top_strip?monitor_index=${idx}&height=100`, {
        method: "POST",
        body: "{}",
      });
      if (!data.ok) throw new Error(data.error || "top strip failed");
      $("compassPreview").src = `data:image/png;base64,${data.preview_png_base64}`;
      $("compassTestResult").textContent =
        data.hint || "상단 전체 미리보기 — 나침반 숫자가 보이면 그 모니터가 맞습니다.";
      $("ocrStatus").textContent = `monitor ${idx}`;
      showToast(`Top strip: monitor ${idx}`);
    } catch (e) {
      showToast(e.message, true);
    }
  });

  $("btnCompassTest").addEventListener("click", async () => {
    try {
      $("ocrStatus").textContent = "capturing…";
      await api("/api/compass/roi", {
        method: "POST",
        body: JSON.stringify({
          monitor_index: Number($("monitorSelect").value),
          x: Number($("roiX").value),
          y: Number($("roiY").value),
          w: Number($("roiW").value),
          h: Number($("roiH").value),
        }),
      });
      const data = await api("/api/compass/test", { method: "POST", body: "{}" });
      $("compassPreview").src = `data:image/png;base64,${data.preview_png_base64}`;
      const r = data.reading || {};
      if (data.ok) {
        $("ocrStatus").textContent = `${r.heading_deg}°`;
        $("compassTestResult").textContent = `OCR heading: ${r.heading_deg}°  (raw: ${r.raw_text || ""})  ·  ${Number(r.elapsed_ms || 0).toFixed(0)} ms`;
        showToast(`Compass ${r.heading_deg}° (${Number(r.elapsed_ms || 0).toFixed(0)} ms)`);
      } else {
        $("ocrStatus").textContent = "failed";
        $("compassTestResult").textContent = `OCR failed: ${r.error || data.error || "unknown"}`;
        showToast(r.error || data.error || "OCR failed", true);
      }
    } catch (e) {
      $("ocrStatus").textContent = "error";
      showToast(e.message, true);
    }
  });

  $("btnTesseractSave").addEventListener("click", async () => {
    try {
      const tesseract_cmd = $("tesseractCmd").value.trim() || null;
      await api("/api/detection/settings", {
        method: "POST",
        body: JSON.stringify({ tesseract_cmd }),
      });
      showToast(tesseract_cmd ? `Tesseract: ${tesseract_cmd}` : "Tesseract path cleared");
    } catch (e) {
      showToast(e.message, true);
    }
  });

  $("btnManualApply").addEventListener("click", async () => {
    try {
      const v = $("manualHeading").value;
      if (v === "") throw new Error("Enter a heading 0-359");
      await api("/api/compass/manual", {
        method: "POST",
        body: JSON.stringify({ heading_deg: Number(v) }),
      });
      showToast(`Manual heading ${v}°`);
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
      showToast("Manual heading cleared");
    } catch (e) {
      showToast(e.message, true);
    }
  });

  loadMonitorsAndSettings().catch((e) => showToast(e.message, true));
})();
