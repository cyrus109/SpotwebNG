// "Send to SABnzbd / black hole" button on the spot page.
(function () {
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
