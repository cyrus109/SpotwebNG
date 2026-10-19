// "Send to SABnzbd / black hole" button on the spot page.
(function () {
  // Block / trust / reset a poster (spot page and Settings -> Posters).
  document.querySelectorAll("[data-spotter]").forEach((el) => {
    el.addEventListener("click", async () => {
      if (el.dataset.confirm && !confirm(el.dataset.confirm)) return;
      const body = new URLSearchParams({ status: el.dataset.status });
      const r = await fetch("/spotter/" + encodeURIComponent(el.dataset.spotter) + "/status", { method: "POST", body });
      if (r.ok) location.reload();
      else alert((await r.json()).message || "Failed");
    });
  });
  const healthBtn = document.getElementById("btn-health");
  if (healthBtn) {
    healthBtn.addEventListener("click", async () => {
      const pill = document.getElementById("health-pill");
      pill.textContent = "checking…";
      healthBtn.disabled = true;
      try {
        const r = await (await fetch(healthBtn.dataset.url, { method: "POST", body: new URLSearchParams({ force: "1" }) })).json();
        const icon = { complete: "✓ complete", partial: "⚠ partial (" + r.pct + "%)", dead: "✗ dead (" + r.pct + "%)", unchecked: "not checked" }[r.label] || r.label;
        pill.textContent = r.error ? "✗ " + r.error : icon;
        pill.className = "pill " + (r.label === "complete" ? "sig-ok" : r.label === "dead" ? "sig-bad" : "");
      } catch (e) {
        pill.textContent = "✗ check failed";
      }
      healthBtn.disabled = false;
    });
  }
  const btn = document.getElementById("btn-send");
  if (!btn) return;
  const msg = document.getElementById("send-msg");
  btn.addEventListener("click", async function () {
    btn.disabled = true;
    msg.textContent = "Sending…";
    try {
      let r = await fetch(btn.dataset.url, { method: "POST" });
      let j = await r.json();
      if (!j.ok && j.blocked && confirm(j.message + ". Send it anyway?")) {
        msg.textContent = "Sending anyway…";
        r = await fetch(btn.dataset.url, { method: "POST", body: new URLSearchParams({ force: "1" }) });
        j = await r.json();
      }
      msg.textContent = (j.ok ? "✓ " : "✗ ") + (j.message || "");
    } catch (e) {
      msg.textContent = "✗ Request failed";
    }
    btn.disabled = false;
  });
})();
