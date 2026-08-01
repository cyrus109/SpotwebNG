// Advanced-filter drawer + mobile rail. Every option is a real link, so
// filtering works with JS disabled; this only adds the panel behaviour.
(function () {
  const drawer = document.getElementById("drawer");
  const scrim = document.getElementById("scrim");
  const rail = document.getElementById("rail");
  if (!drawer) return;

  let lastFocus = null;

  function openDrawer(dim) {
    lastFocus = document.activeElement;
    scrim.hidden = false;
    requestAnimationFrame(() => drawer.classList.add("open"));
    drawer.setAttribute("aria-hidden", "false");
    if (dim) selectDim(dim);
    const search = document.getElementById("optSearch");
    if (search) setTimeout(() => search.focus(), 320);
  }

  function closeDrawer() {
    drawer.classList.remove("open");
    drawer.setAttribute("aria-hidden", "true");
    setTimeout(() => { scrim.hidden = true; }, 320);
    if (lastFocus) lastFocus.focus();
  }

  function selectDim(letter) {
    document.querySelectorAll("[data-dim-tab]").forEach((t) =>
      t.classList.toggle("on", t.dataset.dimTab === letter));
    document.querySelectorAll("[data-dim-panel]").forEach((p) =>
      p.classList.toggle("hidden", p.dataset.dimPanel !== letter));
  }

  document.querySelectorAll("[data-open-drawer]").forEach((b) =>
    b.addEventListener("click", () => openDrawer(b.dataset.dim)));
  document.querySelectorAll("[data-dim-tab]").forEach((t) =>
    t.addEventListener("click", () => selectDim(t.dataset.dimTab)));

  const close = document.getElementById("closeDrawer");
  const apply = document.getElementById("applyDrawer");
  if (close) close.addEventListener("click", closeDrawer);
  if (apply) apply.addEventListener("click", closeDrawer);
  scrim.addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { closeDrawer(); rail && rail.classList.remove("open"); }
  });

  // Filter the visible options as you type — the 79 movie genres need it.
  const search = document.getElementById("optSearch");
  if (search) {
    search.addEventListener("input", () => {
      const q = search.value.trim().toLowerCase();
      document.querySelectorAll(".dim-panel").forEach((panel) => {
        panel.querySelectorAll(".opt").forEach((o) => {
          o.classList.toggle("hidden", !!q && !o.dataset.name.includes(q));
        });
      });
    });
  }

  const toggle = document.getElementById("railToggle");
  if (toggle && rail) toggle.addEventListener("click", () => rail.classList.toggle("open"));
})();
