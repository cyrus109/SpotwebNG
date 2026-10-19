// Watchlist form: category preset -> tree, and a live preview of current matches.
(function () {
  const form = document.getElementById("watchForm");
  if (!form) return;
  const preset = document.getElementById("w-preset");
  const tree = document.getElementById("w-tree");
  const preview = document.getElementById("w-preview");
  let timer = null;

  async function refresh() {
    const params = new URLSearchParams({
      q: document.getElementById("w-query").value,
      tree: tree.value,
      min_gb: document.getElementById("w-min").value,
      max_gb: document.getElementById("w-max").value,
    });
    try {
      const r = await (await fetch("/watchlists/preview?" + params)).json();
      preview.textContent = r.count
        ? r.count.toLocaleString() + " spot(s) match today, e.g. " + r.titles.join(" · ")
        : "No spot matches today (the watch still catches future ones).";
    } catch (e) {
      preview.textContent = "";
    }
  }
  const later = () => { clearTimeout(timer); timer = setTimeout(refresh, 400); };
  preset.addEventListener("change", () => { tree.value = preset.value; later(); });
  form.addEventListener("input", later);
})();
