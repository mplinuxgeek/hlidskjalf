/* hlidskjalf themes: presets + user-defined themes kept in localStorage.
   Loaded in <head> so the saved theme is applied before first paint. A theme is a
   flat map of CSS custom property name (without "--") to a #rrggbb colour; every
   other colour in style.css derives from these. Fires "hl-theme" on window after
   each change so app.js can re-read colours and rebuild its charts. */
"use strict";
(() => {
  // [key, label, group]
  const FIELDS = [
    ["bg", "Background", "Base"], ["surface-solid", "Cards", "Base"],
    ["text", "Text", "Base"], ["text-2", "Secondary text", "Base"], ["muted", "Muted text", "Base"],
    ["a-cpu", "CPU / brand", "Section accents"], ["a-mem", "Memory", "Section accents"], ["a-gpu", "GPU", "Section accents"],
    ["a-temp", "Temperature", "Section accents"], ["a-ups", "UPS", "Section accents"], ["a-net", "Network", "Section accents"],
    ["a-disk", "Storage", "Section accents"], ["a-ct", "Containers", "Section accents"], ["a-vm", "VMs", "Section accents"],
    ["s1", "Series 1", "Chart series"], ["s2", "Series 2", "Chart series"], ["s3", "Series 3", "Chart series"],
    ["s4", "Series 4", "Chart series"], ["s5", "Series 5", "Chart series"], ["s6", "Series 6", "Chart series"],
    ["ok", "OK", "Status"], ["warn", "Warning", "Status"], ["bad", "Critical", "Status"],
  ];
  const DARK_ACC = {
    "a-cpu": "#5b9cff", "a-mem": "#34d399", "a-gpu": "#e879f9", "a-temp": "#ff8a4c", "a-ups": "#a3e635",
    "a-net": "#22d3ee", "a-disk": "#fbbf24", "a-ct": "#f472b6", "a-vm": "#a78bfa",
    s1: "#3987e5", s2: "#d95926", s3: "#199e70", s4: "#c98500", s5: "#d55181", s6: "#9085e9",
    ok: "#34d399", warn: "#fbbf24", bad: "#f87171",
  };
  const MID_DARK_SERIES = { s1: "#4d93ea", s2: "#e2683a", s3: "#22ab7c", s4: "#c98500", s5: "#de6590", s6: "#9085e9" };
  const LIGHT_ACC = {
    "a-cpu": "#2563eb", "a-mem": "#059669", "a-gpu": "#c026d3", "a-temp": "#d9480f", "a-ups": "#4d7c0f",
    "a-net": "#0e7490", "a-disk": "#a16207", "a-ct": "#db2777", "a-vm": "#7c3aed",
    // series validated for CVD separation and 3:1 contrast on a white card
    s1: "#2a78d6", s2: "#c94f1f", s3: "#0f8a78", s4: "#b87a00", s5: "#c2416f", s6: "#6250d6",
    ok: "#059669", warn: "#b45309", bad: "#dc2626",
  };
  const MID_LIGHT = {
    "a-cpu": "#1d4ed8", "a-mem": "#047857", "a-gpu": "#a21caf", "a-temp": "#c2410c", "a-ups": "#3f6212",
    "a-net": "#155e75", "a-disk": "#854d0e", "a-ct": "#be185d", "a-vm": "#6d28d9", s4: "#a36a00",
  };
  const DEFAULT = "Midnight blue";
  const PRESETS = {
    "Midnight blue": { bg: "#0a0d16", "surface-solid": "#121726", text: "#eef0f6", "text-2": "#a9afc3", muted: "#6d7490", ...DARK_ACC },
    "Graphite": { bg: "#101113", "surface-solid": "#1a1b1e", text: "#ededf0", "text-2": "#a9abb2", muted: "#73757d", ...DARK_ACC },
    "OLED black": { bg: "#000000", "surface-solid": "#0c0c0d", text: "#f2f2f3", "text-2": "#a6a7ad", muted: "#6e7078", ...DARK_ACC },
    "Nord": {
      ...DARK_ACC, bg: "#242933", "surface-solid": "#2e3440", text: "#eceff4", "text-2": "#c0c8d8", muted: "#7b88a1",
      "a-cpu": "#81a1c1", "a-mem": "#a3be8c", "a-gpu": "#b48ead", "a-temp": "#d08770", "a-ups": "#8fbcbb",
      "a-net": "#88c0d0", "a-disk": "#ebcb8b", "a-ct": "#d98ba5", "a-vm": "#9a8fc9",
      ok: "#a3be8c", warn: "#ebcb8b", bad: "#bf616a",
    },
    "Tokyo Night": {
      ...DARK_ACC, bg: "#16161e", "surface-solid": "#1f2335", text: "#c0caf5", "text-2": "#a9b1d6", muted: "#6b739c",
      "a-cpu": "#7aa2f7", "a-mem": "#9ece6a", "a-gpu": "#bb9af7", "a-temp": "#ff9e64", "a-ups": "#73daca",
      "a-net": "#7dcfff", "a-disk": "#e0af68", "a-ct": "#ff8fc7", "a-vm": "#9d7cd8",
      ok: "#9ece6a", warn: "#e0af68", bad: "#f7768e",
    },
    "Dracula": {
      ...DARK_ACC, bg: "#1e1f29", "surface-solid": "#282a36", text: "#f8f8f2", "text-2": "#c3c5d6", muted: "#7c86b3",
      "a-cpu": "#bd93f9", "a-mem": "#50fa7b", "a-gpu": "#ff79c6", "a-temp": "#ffb86c", "a-ups": "#b8f171",
      "a-net": "#8be9fd", "a-disk": "#f1fa8c", "a-ct": "#ff6e9c", "a-vm": "#9aa5ff",
      ok: "#50fa7b", warn: "#f1fa8c", bad: "#ff5555",
    },
    "Gruvbox": {
      ...DARK_ACC, bg: "#1d2021", "surface-solid": "#282828", text: "#ebdbb2", "text-2": "#bdae93", muted: "#928374",
      "a-cpu": "#83a598", "a-mem": "#b8bb26", "a-gpu": "#d3869b", "a-temp": "#fe8019", "a-ups": "#8ec07c",
      "a-net": "#7fb7c9", "a-disk": "#fabd2f", "a-ct": "#e78a9f", "a-vm": "#b39ddb",
      ok: "#b8bb26", warn: "#fabd2f", bad: "#fb4934",
    },
    "Solarized dark": {
      ...DARK_ACC, bg: "#002b36", "surface-solid": "#073642", text: "#eee8d5", "text-2": "#93a1a1", muted: "#6f858c",
      "a-cpu": "#268bd2", "a-mem": "#859900", "a-gpu": "#d33682", "a-temp": "#cb4b16", "a-ups": "#9ab000",
      "a-net": "#2aa198", "a-disk": "#b58900", "a-ct": "#e0609a", "a-vm": "#6c71c4",
      ok: "#859900", warn: "#b58900", bad: "#dc322f",
    },
    // mid-tone, dark side: series lifted to keep 3:1 contrast on the lighter cards
    "Slate": { ...DARK_ACC, ...MID_DARK_SERIES, bg: "#2a313b", "surface-solid": "#343c48", text: "#eef1f5", "text-2": "#b9c1cd", muted: "#8691a1" },
    "Dusk": { ...DARK_ACC, ...MID_DARK_SERIES, bg: "#302c40", "surface-solid": "#3b364e", text: "#f0edf7", "text-2": "#c0b9d2", muted: "#8f87a8" },
    "Pine": { ...DARK_ACC, ...MID_DARK_SERIES, bg: "#2b3533", "surface-solid": "#35403e", text: "#edf2f0", "text-2": "#b7c4c0", muted: "#84948f" },
    // mid-tone, light side: Light palette with accents and series 4 darkened for the greyer cards
    "Mist": { ...LIGHT_ACC, ...MID_LIGHT, bg: "#c9ced6", "surface-solid": "#dde1e7", text: "#171b24", "text-2": "#454c5c", muted: "#687083" },
    "Sand": { ...LIGHT_ACC, ...MID_LIGHT, bg: "#d6ccb8", "surface-solid": "#e8e0d0", text: "#2a241a", "text-2": "#584f3f", muted: "#7d7260" },
    "Sage": { ...LIGHT_ACC, ...MID_LIGHT, bg: "#bcc7b8", "surface-solid": "#d3dccf", text: "#1a221b", "text-2": "#46524a", muted: "#66736a" },
    "Light": { ...LIGHT_ACC, bg: "#f1f3f7", "surface-solid": "#ffffff", text: "#161a24", "text-2": "#4a5166", muted: "#737a8f" },
    "Solarized light": {
      bg: "#eee8d5", "surface-solid": "#fdf6e3", text: "#073642", "text-2": "#586e75", muted: "#7b8b8d",
      "a-cpu": "#268bd2", "a-mem": "#6c7c00", "a-gpu": "#d33682", "a-temp": "#cb4b16", "a-ups": "#4f8a10",
      "a-net": "#1f8a82", "a-disk": "#9a7500", "a-ct": "#c2416f", "a-vm": "#6c71c4",
      s1: "#2a78d6", s2: "#c94f1f", s3: "#0f8a78", s4: "#b87a00", s5: "#c2416f", s6: "#6250d6",
      ok: "#6c7c00", warn: "#9a7500", bad: "#dc322f",
    },
  };

  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); return true; } catch (e) { return false; } },
  };
  const HEX = /^#[0-9a-f]{6}$/i;
  // stored data is untrusted: keep only known keys with #rrggbb values, fill the rest from the default
  const clean = (c) => {
    const out = { ...PRESETS[DEFAULT] };
    for (const [k] of FIELDS) if (c && typeof c[k] === "string" && HEX.test(c[k])) out[k] = c[k].toLowerCase();
    return out;
  };
  function loadCustom() {
    const out = {};
    try {
      const j = JSON.parse(store.get("hl-themes") || "{}");
      if (j && typeof j === "object") for (const [n, c] of Object.entries(j)) if (!PRESETS[n] && n.length <= 32) out[n] = clean(c);
    } catch (e) { /* corrupt: start empty */ }
    return out;
  }
  let custom = loadCustom();
  const all = () => ({ ...PRESETS, ...custom });
  let cur = store.get("hl-theme");
  if (!all()[cur]) cur = DEFAULT;

  const root = document.documentElement;
  function lum(hex) {
    const n = parseInt(hex.slice(1), 16);
    const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
    return 0.2126 * f(n >> 16) + 0.7152 * f(n >> 8 & 255) + 0.0722 * f(n & 255);
  }
  function apply(c) {
    for (const [k] of FIELDS) root.style.setProperty("--" + k, c[k]);
    const scheme = lum(c.bg) > 0.4 ? "light" : "dark";
    root.dataset.scheme = scheme;
    document.querySelector('meta[name="color-scheme"]')?.setAttribute("content", scheme);
    document.querySelector('meta[name="theme-color"]')?.setAttribute("content", c.bg);
    window.dispatchEvent(new Event("hl-theme"));
  }
  apply(all()[cur]);

  /* ---------------- drawer ---------------- */
  const el = (tag, props = {}, kids = []) => {
    const e = Object.assign(document.createElement(tag), props);
    for (const k of kids) e.append(k);
    return e;
  };
  let drawer, draft, dirty = false;

  function select(name) {
    cur = name; store.set("hl-theme", name);
    draft = { ...all()[name] }; dirty = false;
    apply(draft);
    render();
  }
  function msg(text, err) { const m = drawer.querySelector(".th-msg"); m.textContent = text; m.classList.toggle("err", !!err); }

  function render() {
    const list = drawer.querySelector(".th-list");
    list.replaceChildren(...Object.entries(all()).map(([name, c]) => {
      const sw = el("span", { className: "th-sw" }, ["bg", "surface-solid", "text", "a-cpu", "a-ct"].map((k) => { const i = el("i"); i.style.background = c[k]; return i; }));
      const pick = el("button", { type: "button", className: "th-pick" + (name === cur ? " on" : "") }, [sw, el("span", { textContent: name })]);
      pick.setAttribute("aria-pressed", name === cur);
      pick.addEventListener("click", () => { select(name); msg(""); });
      const row = el("div", { className: "th-item" }, [pick]);
      if (custom[name]) {
        const del = el("button", { type: "button", className: "th-btn", textContent: "Delete" });
        del.addEventListener("click", () => {
          if (!del.classList.contains("arm")) { del.classList.add("arm"); del.textContent = "Sure?"; return; }
          delete custom[name]; store.set("hl-themes", JSON.stringify(custom));
          if (cur === name) select(DEFAULT); else render();
          msg(`Deleted “${name}”.`);
        });
        row.append(del);
      }
      return row;
    }));
    for (const inp of drawer.querySelectorAll('input[type="color"]')) inp.value = draft[inp.dataset.k];
    drawer.querySelector(".th-name").value = custom[cur] ? cur : "";
  }

  function build() {
    const close = el("button", { type: "button", className: "icon-btn", textContent: "✕", title: "Close" });
    close.addEventListener("click", toggle);
    const kids = [
      el("header", {}, [el("h2", { textContent: "Theme" }), close]),
      el("div", { className: "th-list" }),
      el("h3", { textContent: "Customise" }),
      el("p", { textContent: "Change any colour of the selected theme, then save it under a new name." }),
    ];
    for (const g of [...new Set(FIELDS.map((f) => f[2]))]) {
      kids.push(el("h3", { textContent: g }), el("div", { className: "th-grid" }, FIELDS.filter((f) => f[2] === g).map(([k, label]) => {
        const inp = el("input", { type: "color" });
        inp.dataset.k = k;
        inp.addEventListener("input", () => { draft[k] = inp.value.toLowerCase(); dirty = true; apply(draft); msg("Unsaved changes."); });
        return el("label", {}, [inp, el("span", { textContent: label })]);
      })));
    }
    const name = el("input", { type: "text", className: "th-name", placeholder: "Theme name", maxLength: 32 });
    name.setAttribute("aria-label", "Theme name");
    const save = el("button", { type: "button", className: "th-btn pri", textContent: "Save" });
    const doSave = () => {
      const n = name.value.trim();
      if (!n) return msg("Enter a name for the theme.", true);
      if (PRESETS[n]) return msg("That name belongs to a preset. Pick another.", true);
      const next = { ...custom, [n]: clean(draft) };
      if (!store.set("hl-themes", JSON.stringify(next))) return msg("Browser storage is unavailable; theme not saved.", true);
      custom = next;
      select(n); msg(`Saved “${n}”.`);
    };
    save.addEventListener("click", doSave);
    name.addEventListener("keydown", (e) => { if (e.key === "Enter") doSave(); });
    const reset = el("button", { type: "button", className: "th-btn", textContent: "Reset" });
    reset.addEventListener("click", () => { select(cur); msg(""); });
    kids.push(el("div", { className: "th-save" }, [name, save, reset]), el("p", { className: "th-msg" }),
      el("p", { textContent: "Themes are stored in this browser, per host address." }));
    drawer = el("aside", { className: "theme-drawer", id: "theme-drawer", hidden: true }, kids);
    drawer.setAttribute("aria-label", "Theme");
    document.body.append(drawer);
  }

  function toggle() {
    if (!drawer) build();
    const open = drawer.hidden;
    if (open) { draft = { ...all()[cur] }; dirty = false; render(); msg(""); }
    else if (dirty) apply(all()[cur]);   // closing discards an unsaved preview
    drawer.hidden = !open;
    document.body.classList.toggle("theming", open);
    document.getElementById("theme-btn")?.setAttribute("aria-expanded", open);
  }

  document.addEventListener("DOMContentLoaded", () => {
    document.getElementById("theme-btn")?.addEventListener("click", toggle);
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && drawer && !drawer.hidden) toggle(); });
  });
})();
