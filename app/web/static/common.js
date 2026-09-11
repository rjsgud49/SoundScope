/* Shared helpers for SoundScope pages */
window.SS = (() => {
  const $ = (id) => document.getElementById(id);

  function setConn(online, label) {
    const connState = $("connState");
    const connLabel = $("connLabel");
    if (!connState || !connLabel) return;
    connState.classList.toggle("online", !!online);
    connState.classList.toggle("offline", online === false);
    connLabel.textContent = label;
  }

  function showToast(message, isError = false) {
    const toast = $("toast");
    if (!toast) return;
    toast.hidden = false;
    toast.textContent = message;
    toast.classList.toggle("error", isError);
    clearTimeout(showToast._t);
    showToast._t = setTimeout(() => {
      toast.hidden = true;
    }, 5000);
  }

  async function api(path, options) {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(data.error || `HTTP ${res.status}`);
    }
    return data;
  }

  function dbToPercent(db, floor = -60, ceil = 0) {
    const clamped = Math.max(floor, Math.min(ceil, db));
    return ((clamped - floor) / (ceil - floor)) * 100;
  }

  function connectWs(handlers = {}) {
    let ws = null;
    let reconnectTimer = null;

    function connect() {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${location.host}/ws/meter`);
      ws.onopen = () => {
        setConn(true, "live");
        if (handlers.onOpen) handlers.onOpen(ws);
      };
      ws.onclose = () => {
        setConn(false, "disconnected");
        clearTimeout(reconnectTimer);
        reconnectTimer = setTimeout(connect, 1200);
      };
      ws.onerror = () => setConn(false, "error");
      ws.onmessage = (ev) => {
        let msg;
        try {
          msg = JSON.parse(ev.data);
        } catch {
          return;
        }
        if (msg.type === "meter" && handlers.onMeter) handlers.onMeter(msg);
        if (msg.type === "event" && handlers.onEvent) handlers.onEvent(msg);
        if (msg.type === "saved") showToast(`Saved: ${msg.path}`);
        if (msg.type === "error") showToast(msg.error || "error", true);
        if (handlers.onMessage) handlers.onMessage(msg);
      };
    }

    connect();
    return {
      send(obj) {
        if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
      },
    };
  }

  return { $, setConn, showToast, api, dbToPercent, connectWs };
})();
