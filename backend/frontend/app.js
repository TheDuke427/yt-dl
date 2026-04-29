const $ = id => document.getElementById(id);

let state = { vpn: {}, recordings: [] };

// ── VPN status ──────────────────────────────────────────────────────────────

function renderCookies(loaded) {
  const el = $("cookies-status");
  el.className = "vpn-pill " + (loaded ? "vpn-running" : "vpn-stopped");
  el.innerHTML = `<span class="vpn-dot"></span><span>${loaded ? "Cookies loaded" : "No cookies"}</span>`;
}

function renderVPN(vpn) {
  const pill  = $("vpn-status");
  const label = $("vpn-label");

  pill.className = "vpn-pill vpn-" + (vpn.status || "unknown");

  if (vpn.status === "running") {
    const loc = vpn.region ? ` · ${vpn.region}` : "";
    const ip  = vpn.public_ip ? ` (${vpn.public_ip})` : "";
    label.textContent = `VPN connected${loc}${ip}`;
  } else if (vpn.status === "stopped") {
    label.textContent = "VPN stopped";
  } else {
    label.textContent = "VPN connecting…";
  }
}

// ── Recordings list ──────────────────────────────────────────────────────────

function fmtTime(iso) {
  const d = new Date(iso);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function renderRecordings(recs) {
  const list  = $("recordings-list");
  const count = $("rec-count");

  count.textContent = recs.length;
  count.style.display = recs.length ? "" : "none";

  if (!recs.length) {
    list.innerHTML = '<p class="empty">No recordings yet.</p>';
    return;
  }

  // Preserve scroll position
  const scrollTop = list.scrollTop;

  list.innerHTML = recs.map(r => {
    const actions = [];

    if (r.status === "recording") {
      actions.push(`<button class="btn btn-sm btn-danger" onclick="stopRec('${r.id}')">Stop</button>`);
    }
    if (r.status === "failed" || r.status === "stopped") {
      actions.push(`<button class="btn btn-sm btn-primary" onclick="retryRec('${r.id}')">Retry</button>`);
    }
    if (r.status === "completed" || r.status === "stopped") {
      actions.push(`<button class="btn btn-sm btn-ghost" onclick="downloadRec('${r.id}')">Download</button>`);
    }
    actions.push(`<button class="btn btn-sm btn-ghost" onclick="deleteRec('${r.id}')">Delete</button>`);

    const progress = r.progress
      ? `<div class="rec-progress">${escHtml(r.progress)}</div>`
      : "";

    return `
      <div class="rec-item" data-id="${r.id}">
        <div class="rec-info">
          <div class="rec-name">${escHtml(r.filename)}</div>
          <div class="rec-meta">
            <span class="status-badge status-${r.status}">${r.status}</span>
            <span>${fmtTime(r.started)}</span>
          </div>
          ${progress}
        </div>
        <div class="rec-actions">${actions.join("")}</div>
      </div>`;
  }).join("");

  list.scrollTop = scrollTop;
}

function escHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// ── Polling ──────────────────────────────────────────────────────────────────

async function poll() {
  try {
    const res = await fetch("/api/status");
    if (!res.ok) return;
    state = await res.json();
    renderVPN(state.vpn);
    renderCookies(state.cookies_loaded);
    renderRecordings(state.recordings);
  } catch (_) {}
}

poll();
setInterval(poll, 2000);

// ── Actions ──────────────────────────────────────────────────────────────────

async function retryRec(id) {
  await fetch(`/api/record/retry/${id}`, { method: "POST" });
  poll();
}

async function stopRec(id) {
  await fetch(`/api/record/stop/${id}`, { method: "POST" });
  poll();
}

async function deleteRec(id) {
  if (!confirm("Delete this recording and its file?")) return;
  await fetch(`/api/recordings/${id}`, { method: "DELETE" });
  poll();
}

function downloadRec(id) {
  window.location = `/api/recordings/${id}/download`;
}

// ── Form ─────────────────────────────────────────────────────────────────────

$("record-form").addEventListener("submit", async e => {
  e.preventDefault();
  const btn = $("start-btn");
  btn.disabled = true;
  btn.textContent = "Starting…";

  try {
    const res = await fetch("/api/record/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url:        $("url").value.trim(),
        from_start: $("from-start").checked,
        filename:   $("filename").value.trim() || null,
      }),
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      alert("Error: " + (err.detail || "unknown error"));
    } else {
      $("url").value = "";
      $("filename").value = "";
      $("from-start").checked = false;
      poll();
    }
  } finally {
    btn.disabled = false;
    btn.textContent = "Start Recording";
  }
});
