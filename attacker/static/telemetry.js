(function () {
  "use strict";

  const params = new URLSearchParams(window.location.search);
  function sessionFromPath() {
    const m = window.location.pathname.match(/^\/probe\/[^/]+\/([^/]+)/);
    return m ? decodeURIComponent(m[1]) : null;
  }
  const SESSION_ID = sessionFromPath() || params.get("session_id") || "anon";
  const t0 = performance.now();
  let queue = [];
  let firstActionLogged = false;

  function push(type, fields) {
    const ev = Object.assign(
      { type: type, client_ts: performance.now(), url: window.location.href },
      fields || {}
    );
    queue.push(ev);
    if (!firstActionLogged && (type === "click" || type === "hover" || type === "scroll")) {
      firstActionLogged = true;
      queue.push({ type: "first_action", client_ts: ev.client_ts,
                   extra: { delay_ms: ev.client_ts - t0, via: type } });
    }
    if (queue.length >= 8) flush();
  }

  function flush(useBeacon) {
    if (queue.length === 0) return;
    const body = JSON.stringify({ session_id: SESSION_ID, events: queue });
    queue = [];
    if (useBeacon && navigator.sendBeacon) {
      navigator.sendBeacon("/beacon", new Blob([body], { type: "application/json" }));
    } else {
      fetch("/beacon", { method: "POST", body: body,
        headers: { "Content-Type": "application/json" }, keepalive: true }).catch(() => {});
    }
  }

  function probeId(el) {
    const t = el.closest("[data-probe]");
    return t ? t.getAttribute("data-probe") : (el.id || el.tagName.toLowerCase());
  }

  document.addEventListener("click", function (e) {
    push("click", { target_id: probeId(e.target), x: e.clientX, y: e.clientY });
  }, true);

  const hoverStart = new Map();
  document.addEventListener("mouseover", function (e) {
    const id = probeId(e.target);
    hoverStart.set(id, performance.now());
  }, true);
  document.addEventListener("mouseout", function (e) {
    const id = probeId(e.target);
    if (hoverStart.has(id)) {
      push("hover", { target_id: id, dwell_ms: performance.now() - hoverStart.get(id) });
      hoverStart.delete(id);
    }
  }, true);

  document.addEventListener("focus", function (e) {
    push("focus", { target_id: probeId(e.target) });
  }, true);

  let maxScroll = 0, scrollTimer = null;
  window.addEventListener("scroll", function () {
    const depth = (window.scrollY + window.innerHeight) /
                  Math.max(1, document.body.scrollHeight);
    maxScroll = Math.max(maxScroll, depth);
    clearTimeout(scrollTimer);
    scrollTimer = setTimeout(function () {
      push("scroll", { extra: { max_depth: maxScroll } });
    }, 150);
  });

  document.addEventListener("visibilitychange", function () {
    push("visibility", { extra: { state: document.visibilityState } });
    if (document.visibilityState === "hidden") flush(true);
  });
  window.addEventListener("pagehide", function () { flush(true); });
  window.addEventListener("beforeunload", function () { flush(true); });

  setInterval(function () { flush(false); }, 2000);

  push("ready", { extra: { load_ms: performance.now() - t0 } });
})();
