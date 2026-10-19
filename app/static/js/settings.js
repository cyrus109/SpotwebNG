// Settings page behaviour: dirty tracking for the sticky save bar, a live
// connection test, and copy-to-clipboard for API keys.
(function () {
  const form = document.querySelector('form[action="/settings"]');
  const bar = document.getElementById("saveBar");

  // --- sticky save bar -------------------------------------------------
  // The form spans several grid cards, so a Save button inside one card can
  // render in a different column from the field being edited.
  if (form && bar) {
    const initial = new URLSearchParams(new FormData(form)).toString();
    const check = () => {
      const now = new URLSearchParams(new FormData(form)).toString();
      bar.hidden = now === initial;
    };
    form.addEventListener("input", check);
    form.addEventListener("change", check);
    const discard = document.getElementById("discardBtn");
    if (discard) discard.addEventListener("click", () => location.reload());
    window.addEventListener("beforeunload", (e) => {
      if (!bar.hidden) { e.preventDefault(); e.returnValue = ""; }
    });
  }

  // --- connection test --------------------------------------------------
  const testBtn = document.getElementById("testConn");
  const pill = document.getElementById("connPill");
  if (testBtn && pill) {
    testBtn.addEventListener("click", async () => {
      testBtn.disabled = true;
      pill.dataset.state = "busy";
      pill.textContent = "Testing…";
      try {
        const resp = await fetch("/settings/test", { method: "POST" });
        const r = await resp.json();
        pill.dataset.state = r.ok ? "ok" : "bad";
        pill.textContent = r.ok ? "Connected" : "Failed";
        pill.title = r.message || "";
      } catch (err) {
        pill.dataset.state = "bad";
        pill.textContent = "Failed";
        pill.title = String(err);
      } finally {
        testBtn.disabled = false;
      }
    });
  }

  // --- SABnzbd test -----------------------------------------------------
  const sabBtn = document.getElementById("sabTest");
  if (sabBtn) {
    sabBtn.addEventListener("click", async () => {
      const msg = document.getElementById("sabTestMsg");
      sabBtn.disabled = true;
      msg.textContent = "Testing…";
      try {
        const body = new URLSearchParams({
          url: document.getElementById("sab_url").value,
          apikey: document.getElementById("sab_apikey").value,
        });
        const r = await (await fetch("/settings/sab/test", { method: "POST", body })).json();
        msg.textContent = (r.ok ? "✓ " : "✗ ") + (r.message || "");
        const list = document.getElementById("sabCats");
        if (r.ok && r.categories && list) {
          list.replaceChildren(...r.categories.map((c) => Object.assign(document.createElement("option"), { value: c })));
        }
      } catch (e) {
        msg.textContent = "✗ Request failed";
      }
      sabBtn.disabled = false;
    });
  }

  // --- Radarr / Sonarr test: fills the profile and root-folder choices -------
  document.querySelectorAll("[data-arr-test]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const kind = btn.dataset.arrTest;
      const msg = document.getElementById(kind + "TestMsg");
      btn.disabled = true;
      msg.textContent = "Testing…";
      try {
        const body = new URLSearchParams({
          kind,
          url: document.getElementById(kind + "_url").value,
          apikey: document.getElementById(kind + "_apikey").value,
        });
        const r = await (await fetch("/settings/arr/test", { method: "POST", body })).json();
        msg.textContent = (r.ok ? "✓ " : "✗ ") + (r.message || "");
        if (r.ok) {
          const fill = (id, options) => {
            const sel = document.getElementById(id);
            const keep = sel.value;
            sel.replaceChildren(...options.map(([value, text]) => Object.assign(document.createElement("option"), { value, textContent: text, selected: String(value) === keep })));
            sel.dispatchEvent(new Event("change", { bubbles: true }));
          };
          fill(kind + "_profile_id", r.profiles.map((p) => [p.id, p.name]));
          fill(kind + "_root", r.roots.map((x) => [x.path, x.path]));
        }
      } catch (e) {
        msg.textContent = "✗ Request failed";
      }
      btn.disabled = false;
    });
  });

  // --- notification test ------------------------------------------------
  const nBtn = document.getElementById("notifyTest");
  if (nBtn) {
    nBtn.addEventListener("click", async () => {
      const msg = document.getElementById("notifyTestMsg");
      nBtn.disabled = true;
      msg.textContent = "Sending…";
      try {
        const r = await (await fetch("/settings/notify/test", { method: "POST" })).json();
        msg.textContent = (r.ok ? "✓ " : "✗ ") + (r.message || "");
      } catch (e) {
        msg.textContent = "✗ Request failed";
      }
      nBtn.disabled = false;
    });
  }

  // --- copy API key -----------------------------------------------------
  // navigator.clipboard only exists in a secure context — this app is served
  // over plain HTTP on a LAN, so fall back to a hidden textarea + execCommand.
  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise((resolve, reject) => {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.cssText = "position:fixed;top:-1000px;opacity:0";
      document.body.appendChild(ta);
      ta.select();
      ta.setSelectionRange(0, text.length);
      const ok = document.execCommand && document.execCommand("copy");
      document.body.removeChild(ta);
      ok ? resolve() : reject(new Error("copy blocked"));
    });
  }

  document.querySelectorAll("[data-copy]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const was = btn.textContent;
      try {
        await copyText(btn.dataset.copy);
        btn.textContent = "Copied";
        btn.classList.add("done");
      } catch {
        btn.textContent = "Press \u2318C";
        const code = btn.closest(".keyrow")?.querySelector(".keyrow-key");
        if (code) getSelection().selectAllChildren(code);
      }
      setTimeout(() => { btn.textContent = was; btn.classList.remove("done"); }, 1600);
    });
  });
})();

// --- section filtering ------------------------------------------------
// The rail used to be anchor links; on a page where every card is visible,
// scrolling to one is pointless. Clicking a section now shows only that card.
(function () {
  const rail = document.querySelectorAll(".rail-link[data-section]");
  const cards = document.querySelectorAll(".settings-wrap .card");
  if (!rail.length || !cards.length) return;

  function show(section) {
    const all = !section || section === "all";
    cards.forEach((c) => { c.hidden = !all && c.id !== section; });
    rail.forEach((a) => a.classList.toggle("on", a.dataset.section === (all ? "all" : section)));
    // The save bar belongs to a form spanning several cards, so it must stay
    // reachable even when only one of those cards is on screen.
  }

  function fromHash() {
    show((location.hash || "#all").slice(1));
  }

  rail.forEach((a) =>
    a.addEventListener("click", () => {
      // let the hash change, then filter (keeps the URL shareable)
      setTimeout(fromHash, 0);
    }));
  window.addEventListener("hashchange", fromHash);
  fromHash();
})();
