// CSRF: every unsafe fetch() carries the session's token as a header, and every
// POST form gets it as a hidden field just before it is submitted.
(function () {
  const meta = document.querySelector('meta[name="csrf-token"]');
  const token = meta ? meta.content : "";
  if (!token) return;
  const nativeFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    init = init || {};
    const method = (init.method || (input && input.method) || "GET").toUpperCase();
    if (method !== "GET" && method !== "HEAD") {
      const headers = new Headers(init.headers || (input && input.headers) || {});
      headers.set("X-CSRF-Token", token);
      init = Object.assign({}, init, { headers });
    }
    return nativeFetch(input, init);
  };
  const addField = (form) => {
    if ((form.method || "").toLowerCase() !== "post" || form.querySelector('input[name="csrf_token"]')) return;
    const field = document.createElement("input");
    field.type = "hidden";
    field.name = "csrf_token";
    field.value = token;
    form.appendChild(field);
  };
  document.addEventListener("submit", (e) => addField(e.target), true);
  document.querySelectorAll("form").forEach(addField);
})();

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
