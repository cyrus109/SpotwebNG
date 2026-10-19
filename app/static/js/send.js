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
  const btn = document.getElementById("btn-send");
  if (!btn) return;
  const msg = document.getElementById("send-msg");
  btn.addEventListener("click", async function () {
    btn.disabled = true;
    msg.textContent = "Sending…";
    try {
      const r = await fetch(btn.dataset.url, { method: "POST" });
      const j = await r.json();
      msg.textContent = (j.ok ? "✓ " : "✗ ") + (j.message || "");
    } catch (e) {
      msg.textContent = "✗ Request failed";
    }
    btn.disabled = false;
  });
})();
