// Live retrieval status (settings card + browse pill) and database import.
(function () {
  const box = document.getElementById("retrievalStatus");
  const pill = document.getElementById("retrievalPill");
  if (!box && !pill) return;
  const url = (box || pill).dataset.url;
  const day = (s) => (s ? String(s).slice(0, 10) : null);
  let timer = null;

  const schedule = (ms) => {
    clearTimeout(timer);
    timer = setTimeout(tick, ms);
  };

  function liveText(live) {
    let text = live.phase || "Working";
    if (live.percent != null) text += " — " + live.percent.toFixed(1) + "%";
    if (live.detail) text += " · " + live.detail;
    return text;
  }

  function render(d) {
    const live = d.live || {};
    const st = d.status || {};
    if (pill) {
      pill.hidden = !live.running;
      pill.textContent = live.running ? "⟳ " + liveText(live) : "";
    }
    if (!box) return;
    const line = document.getElementById("rsLine");
    const wrap = document.getElementById("rsBarWrap");
    const bar = document.getElementById("rsBar");
    const range = document.getElementById("rsRange");
    if (live.running) {
      line.textContent = liveText(live);
    } else {
      line.textContent = st.last_run ? "Idle · last check " + day(st.last_run) : "No retrieval has run yet.";
    }
    const pct = live.running && live.percent != null ? live.percent : d.history_percent;
    wrap.hidden = pct == null;
    if (pct != null) bar.style.width = Math.max(0, Math.min(100, pct)) + "%";
    const parts = [];
    if (st.oldest_date) parts.push("Oldest article on your provider: " + day(st.oldest_date));
    if (d.retrieve_days && d.effective_oldest && day(d.effective_oldest) !== day(st.oldest_date))
      parts.push("a full history stops at " + day(d.effective_oldest) + " (" + d.retrieve_days + " days)");
    if (st.floor_date) parts.push("reached " + day(st.floor_date));
    if (!st.oldest_date) parts.push("The provider's oldest date is shown after the first retrieval.");
    const running = (d.jobs || []).filter((j) => j.status !== "done");
    if (running.length) parts.push("background jobs: " + running.map((j) => j.description + " (" + j.status + (j.processed ? ", " + j.processed.toLocaleString() : "") + ")").join(", "));
    if (d.backups && d.backups.count) parts.push(d.backups.count + " backup(s), latest " + day(d.backups.latest));
    const sg = d.signatures;
    if (sg && sg.verified + sg.failed > 0) {
      const pct = Math.round((100 * sg.verified) / (sg.verified + sg.failed));
      parts.push("signatures: " + pct + "% of signed spots verify (" + sg.failed.toLocaleString() + " failed)");
    }
    if (d.version) parts.push("v" + d.version);
    const imp = st.last_import;
    if (imp) {
      const n = imp.added ? Object.values(imp.added).reduce((a, b) => a + b, 0) : 0;
      parts.push(imp.ok ? "last import: " + n.toLocaleString() + " rows added" : "last import failed: " + imp.error);
    }
    range.textContent = parts.join(" · ");
    const st2 = d.storage;
    const store = document.getElementById("rsStorage");
    if (st2 && store) {
      const mb = (n) => (n >= 1e9 ? (n / 1e9).toFixed(1) + " GB" : (n / 1e6).toFixed(n < 1e7 ? 1 : 0) + " MB");
      const bits = [];
      if (st2.database_bytes != null) bits.push("database " + mb(st2.database_bytes));
      bits.push("covers " + mb(st2.covers_bytes), "NZB cache " + mb(st2.nzb_bytes));
      bits.push("backups " + st2.backups.count + " (" + mb(st2.backups.bytes) + ")");
      bits.push("free on disk " + mb(st2.free_bytes) + " of " + mb(st2.total_bytes));
      bits.push("next export ≈ " + mb(st2.export_estimate_bytes));
      store.textContent = "Storage: " + bits.join(" · ") + (st2.warning ? "  ⚠ " + st2.warning : "");
      store.style.color = st2.warning ? "#d9534f" : "";
    }
  }

  async function tick() {
    let data;
    try {
      const r = await fetch(url, { headers: { Accept: "application/json" } });
      if (!r.ok || !(r.headers.get("content-type") || "").includes("json")) return; // viewers: stop quietly
      data = await r.json();
    } catch (e) {
      return schedule(10000);
    }
    render(data);
    schedule(data.live && data.live.running ? 2000 : 15000);
  }

  const importBtn = document.getElementById("importBtn");
  if (importBtn) {
    importBtn.addEventListener("click", async function () {
      const input = document.getElementById("importFile");
      const msg = document.getElementById("importMsg");
      if (!input.files.length) {
        msg.textContent = "Choose an export file first.";
        return;
      }
      importBtn.disabled = true;
      msg.textContent = "Uploading…";
      const form = new FormData();
      form.append("file", input.files[0]);
      try {
        const r = await fetch("/settings/import", { method: "POST", body: form });
        const j = await r.json();
        msg.textContent = j.ok ? "Import started — progress is shown above." : j.message || "Import refused.";
        if (j.ok) schedule(500);
      } catch (e) {
        msg.textContent = "Upload failed: " + e;
      }
      importBtn.disabled = false;
    });
  }

  const maintBtn = document.getElementById("maintBtn");
  if (maintBtn) {
    maintBtn.addEventListener("click", async function () {
      const action = document.getElementById("maintAction").value;
      const msg = document.getElementById("maintMsg");
      const form = new URLSearchParams({ action });
      if (action === "wipe" || action === "wipe-comments" || action === "forget-downloads") {
        const typed = prompt("This cannot be undone. Type DELETE to continue.");
        if (typed !== "DELETE") return;
        form.set("confirm", typed);
      } else if (!confirm("Run this maintenance action?")) {
        return;
      }
      maintBtn.disabled = true;
      msg.textContent = "Working…";
      try {
        const j = await (await fetch("/settings/maintenance", { method: "POST", body: form })).json();
        msg.textContent = (j.ok ? "✓ " : "✗ ") + j.message;
      } catch (e) {
        msg.textContent = "✗ Request failed";
      }
      maintBtn.disabled = false;
      schedule(500);
    });
  }

  tick();
})();
