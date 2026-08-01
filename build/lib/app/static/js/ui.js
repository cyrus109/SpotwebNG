// Replace native <select> popups with styled menus. The OS draws the native
// list itself (white on Windows regardless of colour-scheme), so the only way
// to theme it is to render our own. The <select> stays as the source of truth.
(function () {
  document.querySelectorAll("select[data-ui-select]").forEach((native) => {
    const wrap = document.createElement("div");
    wrap.className = "ui-select";
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "ui-select-btn";
    const label = document.createElement("span");
    btn.appendChild(label);

    const menu = document.createElement("div");
    menu.className = "ui-select-menu";

    const sync = () => {
      label.textContent = native.options[native.selectedIndex].textContent;
      menu.querySelectorAll("button").forEach((b, i) =>
        b.classList.toggle("on", i === native.selectedIndex));
    };

    Array.from(native.options).forEach((opt, i) => {
      const item = document.createElement("button");
      item.type = "button";
      item.textContent = opt.textContent;
      item.addEventListener("click", () => {
        native.selectedIndex = i;
        sync();
        wrap.classList.remove("open");
        native.dispatchEvent(new Event("change", { bubbles: true }));
      });
      menu.appendChild(item);
    });

    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      document.querySelectorAll(".ui-select.open").forEach((o) => o !== wrap && o.classList.remove("open"));
      wrap.classList.toggle("open");
    });

    native.style.display = "none";
    native.parentNode.insertBefore(wrap, native);
    wrap.append(btn, menu, native);
    sync();
  });

  document.addEventListener("click", () =>
    document.querySelectorAll(".ui-select.open").forEach((o) => o.classList.remove("open")));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") document.querySelectorAll(".ui-select.open").forEach((o) => o.classList.remove("open"));
  });
})();
