"use strict";
// Plain DOM, no build step. Text from sites (titles, errors) is only ever set
// through textContent, never innerHTML.

const $ = (sel) => document.querySelector(sel);
const state = {
  csrf: null,
  user: null,
  tab: "download",
  analysis: null,
  selected: new Set(),
  jobs: [],
  lib: { files: [], total: 0, offset: 0, selectMode: false, selected: new Set() },
  pollTimer: null,
};
const PENDING_KEY = "mediadl-pending-delivery";
const ICONS = { video: "▶", audio: "♪", image: "▣", mixed: "◫", other: "•" };

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "text") el.textContent = value;
    else if (key === "width") el.style.width = value;  // CSSOM: allowed by the CSP, unlike style=""
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (value === true) el.setAttribute(key, "");
    else el.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

function toast(message, ms = 3500) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.remove("hidden");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => el.classList.add("hidden"), ms);
}

class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

async function api(path, { method = "GET", body, form } = {}) {
  const opts = { method, headers: {}, credentials: "same-origin" };
  if (method !== "GET" && state.csrf) opts.headers["X-CSRF-Token"] = state.csrf;
  if (form) opts.body = form;
  else if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  let data = null;
  const text = await res.text();
  try { data = text ? JSON.parse(text) : null; } catch { data = { detail: text }; }
  if (res.status === 401 && path !== "api/login") {
    showLogin();
    throw new ApiError(401, "Signed out");
  }
  if (!res.ok) {
    let detail = data && data.detail;
    if (Array.isArray(detail)) detail = detail.map((d) => d.msg || JSON.stringify(d)).join("\n");
    throw new ApiError(res.status, detail || `HTTP ${res.status}`);
  }
  return data;
}

function fmtBytes(n) {
  if (n === null || n === undefined) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i && n < 10 ? 1 : 0)} ${units[i]}`;
}
function fmtDuration(s) {
  if (!s) return "";
  s = Math.round(s);
  const hh = Math.floor(s / 3600), mm = Math.floor((s % 3600) / 60), ss = s % 60;
  return (hh ? `${hh}:${String(mm).padStart(2, "0")}` : `${mm}`) + `:${String(ss).padStart(2, "0")}`;
}
function fmtDate(ts) {
  return new Date(ts * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}
const fileUrl = (path, dl) => `api/file?path=${encodeURIComponent(path)}${dl ? "&dl=1" : ""}`;
const thumbUrl = (path) => `api/thumb?path=${encodeURIComponent(path)}`;

// ---------------------------------------------------------------- auth

function showLogin() {
  state.csrf = null;
  $("#app-view").classList.add("hidden");
  $("#login-view").classList.remove("hidden");
  stopPolling();
  setTimeout(() => $("#login-user").focus(), 50);
}

function showApp() {
  $("#login-view").classList.add("hidden");
  $("#app-view").classList.remove("hidden");
  $("#me-user").textContent = state.user;
  switchTab(location.hash.slice(1) || "download");
  startPolling();
}

$("#login-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const err = $("#login-error");
  err.textContent = "";
  const button = ev.submitter || $("#login-form button");
  button.disabled = true;
  try {
    const data = await api("api/login", {
      method: "POST",
      body: { username: $("#login-user").value.trim(), password: $("#login-pass").value },
    });
    state.csrf = data.csrf;
    state.user = data.user;
    $("#login-pass").value = "";
    showApp();
  } catch (e) {
    err.textContent = e.message;
  } finally {
    button.disabled = false;
  }
});

$("#logout").addEventListener("click", async () => {
  try { await api("api/logout", { method: "POST" }); } catch {}
  showLogin();
});

// ---------------------------------------------------------------- tabs

function switchTab(name) {
  if (!["download", "queue", "library", "settings"].includes(name)) name = "download";
  state.tab = name;
  for (const btn of document.querySelectorAll("#tabs button")) btn.classList.toggle("active", btn.dataset.tab === name);
  for (const sec of document.querySelectorAll(".tab")) sec.classList.toggle("hidden", sec.id !== `tab-${name}`);
  if (location.hash !== `#${name}`) history.replaceState(null, "", `#${name}`);
  window.scrollTo(0, 0);
  if (name === "queue") refreshJobs();
  if (name === "library") loadLibrary(true);
  if (name === "settings") { loadCookies(); loadEngines(); }
}
for (const btn of document.querySelectorAll("#tabs button")) btn.addEventListener("click", () => switchTab(btn.dataset.tab));

// ------------------------------------------------------------- analyze

$("#analyze-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  analyze($("#engine").value);
});

async function analyze(engine) {
  const url = $("#url").value.trim();
  if (!url) return;
  const status = $("#analyze-status");
  const button = $("#analyze-btn");
  button.disabled = true;
  $("#result").classList.add("hidden");
  status.replaceChildren(h("div", { class: "card" }, h("span", { class: "spinner" }), " Reading the link… (can take up to a minute)"));
  try {
    const data = await api("api/analyze", { method: "POST", body: { url, engine } });
    status.replaceChildren();
    renderAnalysis(data);
  } catch (e) {
    if (e.status === 401) return;
    status.replaceChildren(h("div", { class: "card error-box" },
      h("strong", { text: "Could not read this link" }),
      h("pre", { class: "error-text", text: e.message })));
  } finally {
    button.disabled = false;
  }
}

function renderAnalysis(a) {
  state.analysis = a;
  state.selected = new Set(a.entries.filter((e) => !e.error).map((e) => e.idx));
  $("#r-title").textContent = a.title || a.url;
  const meta = [a.platform, a.uploader, `${a.entries.length} item${a.entries.length === 1 ? "" : "s"}`, `engine: ${a.engine}`];
  if (a.has_cookies) meta.push("cookies: yes");
  $("#r-meta").textContent = meta.filter(Boolean).join(" · ");
  const other = a.engine === "yt-dlp" ? "gallery-dl" : "yt-dlp";
  const sw = $("#r-switch");
  sw.textContent = `Try ${other}`;
  sw.onclick = () => { $("#engine").value = other; analyze(other); };
  $("#r-note").textContent = a.note || "";
  $("#r-note").classList.toggle("hidden", !a.note);
  $("#r-fallback").classList.toggle("hidden", !a.fallback_error);
  $("#r-fallback-text").textContent = a.fallback_error || "";

  const multi = a.entries.length > 1;
  $("#r-select").classList.toggle("hidden", !multi);
  const grid = $("#r-grid");
  grid.replaceChildren();
  if (multi) {
    for (const e of a.entries) grid.append(entryTile(a, e));
    updateSelCount();
  }
  renderFormats(a);
  $("#result").classList.remove("hidden");
}

function entryTile(a, e) {
  const box = h("input", { type: "checkbox", checked: true, "aria-label": `Select ${e.title}` });
  const thumb = h("div", { class: "thumb", text: ICONS[e.media] || "•" });
  if (e.thumb) {
    const img = h("img", { loading: "lazy", alt: "", src: `api/analysis/${a.id}/thumb/${e.idx}` });
    img.addEventListener("error", () => img.remove());
    img.addEventListener("load", () => { thumb.firstChild && thumb.firstChild.nodeType === 3 && thumb.firstChild.remove(); });
    thumb.append(img);
  }
  const tag = h("span", { class: "tag", text: e.duration ? fmtDuration(e.duration) : `#${e.idx}` });
  const caption = h("div", { class: "cap", text: e.title });
  if (e.error) {
    // The engine cannot download this one (DRM, removed, …): show why, never select it.
    const tile = h("div", { class: "tile unavailable", title: e.error }, thumb, tag, caption,
      h("div", { class: "sub error-text", text: e.error }));
    tile._toggle = () => {};
    return tile;
  }
  const tile = h("div", { class: "tile selected" }, thumb, box, tag, caption);
  const toggle = (on) => {
    if (on) state.selected.add(e.idx); else state.selected.delete(e.idx);
    box.checked = on;
    tile.classList.toggle("selected", on);
    updateSelCount();
  };
  tile.addEventListener("click", (ev) => { if (ev.target !== box) toggle(!state.selected.has(e.idx)); });
  box.addEventListener("change", () => toggle(box.checked));
  tile._toggle = toggle;
  return tile;
}

function updateSelCount() {
  const a = state.analysis;
  const blocked = a.entries.filter((e) => e.error).length;
  $("#sel-count").textContent = `${state.selected.size} of ${a.entries.length} selected` + (blocked ? ` · ${blocked} unavailable` : "");
  $("#download-btn").disabled = state.selected.size === 0;
}
$("#sel-all").addEventListener("click", () => { for (const t of $("#r-grid").children) t._toggle(true); });
$("#sel-none").addEventListener("click", () => { for (const t of $("#r-grid").children) t._toggle(false); });

function renderFormats(a) {
  const box = $("#r-formats");
  box.replaceChildren(h("legend", { class: "muted", text: "Format" }));
  if (a.engine === "gallery-dl") {
    box.append(formatOption("files", "Original files (as published)", null, true));
    return;
  }
  const hasVideo = a.media !== "audio";
  if (hasVideo) {
    const res = h("select", { id: "fmt-height", "aria-label": "Resolution" },
      h("option", { value: "", text: "Best available" }),
      a.heights.map((ht) => h("option", { value: ht, text: a.heights_exact ? `${ht}p` : `up to ${ht}p` })));
    const compat = h("label", { class: "check sub-option" },
      h("input", { type: "checkbox", id: "fmt-compat", checked: true }),
      "H.264 (plays on every device; YouTube often has higher resolutions only in AV1/VP9)");
    box.append(formatOption("video", "Video · MP4", res, true), compat);
  }
  if (a.has_audio === false) {
    box.append(h("p", { class: "note", text: "This video has no audio track, so audio formats are not offered." }));
    return;
  }
  const br = h("select", { id: "fmt-bitrate", "aria-label": "MP3 bitrate" },
    [320, 256, 192, 160, 128, 96].map((b) => h("option", { value: b, text: `${b} kbps`, selected: b === 192 })));
  box.append(formatOption("mp3", "Audio · MP3", br, !hasVideo));
  box.append(formatOption("m4a", "Audio · M4A", null, false));
  box.append(formatOption("original", "Audio · original stream (no re-encoding)", null, false));
}

function formatOption(value, text, extra, checked) {
  const radio = h("input", { type: "radio", name: "fmt", value, checked });
  return h("div", { class: "format-option" }, h("label", {}, radio, text), extra);
}

function chosenFormat() {
  const mode = (document.querySelector("input[name=fmt]:checked") || {}).value;
  if (mode === "video") return { mode, height: $("#fmt-height").value || null, compat: $("#fmt-compat").checked };
  if (mode === "mp3") return { mode, bitrate: Number($("#fmt-bitrate").value) };
  return { mode };
}

$("#download-btn").addEventListener("click", async () => {
  const a = state.analysis;
  if (!a) return;
  const button = $("#download-btn");
  button.disabled = true;
  try {
    const selected = a.entries.length > 1 ? [...state.selected] : [a.entries[0].idx];
    const deliver = $("#deliver").checked;
    const job = await api("api/jobs", {
      method: "POST",
      body: { analysis_id: a.id, selected, format: chosenFormat(), deliver },
    });
    if (deliver) { const p = pendingDeliveries(); p.add(job.id); setPendingDeliveries(p); }
    toast("Added to the queue");
    $("#result").classList.add("hidden");
    $("#url").value = "";
    state.analysis = null;
    switchTab("queue");
  } catch (e) {
    if (e.status !== 401) toast(e.message, 6000);
  } finally {
    button.disabled = false;
  }
});

// ---------------------------------------------------------------- queue

// Jobs this browser asked to receive ("send to this device"). Kept in
// localStorage so a reload while downloading does not lose them; other
// devices never see them, so they never auto-download someone else's job.
function pendingDeliveries() {
  try { return new Set(JSON.parse(localStorage.getItem(PENDING_KEY) || "[]")); } catch { return new Set(); }
}
function setPendingDeliveries(set) {
  try { localStorage.setItem(PENDING_KEY, JSON.stringify([...set].slice(-50))); } catch {}
}

function saveJobToDevice(job) {
  const files = job.items.flatMap((i) => i.files);
  if (!files.length) return;
  const a = h("a", { href: files.length === 1 ? fileUrl(files[0], true) : `api/jobs/${job.id}/zip`, download: "" });
  document.body.append(a);
  a.click();
  a.remove();
}

async function refreshJobs() {
  try {
    const data = await api("api/jobs");
    state.jobs = data.jobs;
  } catch (e) {
    return;
  }
  const active = state.jobs.filter((j) => j.status === "running" || j.status === "queued").length;
  const badge = $("#queue-badge");
  badge.textContent = active;
  badge.classList.toggle("hidden", !active);

  const pending = pendingDeliveries();
  for (const job of state.jobs) {
    if (!pending.has(job.id)) continue;
    if (["error", "canceled"].includes(job.status)) { pending.delete(job.id); setPendingDeliveries(pending); }
    if (job.status === "done" || job.status === "partial") {
      pending.delete(job.id);
      setPendingDeliveries(pending);
      saveJobToDevice(job);
      toast(`Sending "${job.title || "download"}" to this device`);
    }
  }
  if (state.tab === "queue") renderJobs();
}

function renderJobs() {
  const list = $("#jobs");
  if (!state.jobs.length) {
    list.replaceChildren(h("p", { class: "muted", text: "Nothing here yet." }));
    return;
  }
  // Keep open <details> and scroll position stable across refreshes.
  const open = new Set([...list.querySelectorAll("details[open]")].map((d) => d.dataset.key));
  list.replaceChildren(...state.jobs.map((job) => renderJob(job, open)));
}

function renderJob(job, open) {
  const doneFiles = job.items.flatMap((i) => i.files);
  const counts = job.items.reduce((acc, i) => ((acc[i.status] = (acc[i.status] || 0) + 1), acc), {});
  const summary = Object.entries(counts).map(([k, v]) => `${v} ${k}`).join(", ");
  const actions = [];
  if (job.status === "running" || job.status === "queued") {
    actions.push(h("button", { type: "button", text: "Cancel", onclick: () => jobAction(job.id, "cancel") }));
  }
  if (job.items.some((i) => i.status === "error" || i.status === "canceled")) {
    actions.push(h("button", { type: "button", text: "Retry failed", onclick: () => jobAction(job.id, "retry") }));
  }
  if (doneFiles.length) {
    actions.push(h("button", { type: "button", class: "primary", text: doneFiles.length === 1 ? "Save to device" : `Save ${doneFiles.length} files (ZIP)`, onclick: () => saveJobToDevice(job) }));
  }
  const items = job.items.map((item) => {
    const pct = item.status === "done" ? 100 : Math.round(item.progress || 0);
    const right = item.status === "running"
      ? (item.total ? `${pct}% · ${fmtBytes(item.downloaded)} / ${fmtBytes(item.total)}` : (item.downloaded ? fmtBytes(item.downloaded) : "starting…"))
      : item.status;
    return h("div", { class: `item ${item.status}` },
      h("div", { class: "item-line" },
        h("span", { class: "item-title", text: `${job.items.length > 1 ? `#${item.idx} ` : ""}${item.title || ""}` }),
        h("span", { class: "muted", text: right })),
      h("div", { class: "bar" }, h("div", { width: `${pct}%` })),
      item.error ? h("pre", { text: item.error }) : null,
      item.files.length ? h("ul", { class: "files" }, item.files.map((f) => h("li", {}, h("a", { href: fileUrl(f, true), text: f })))) : null);
  });
  const many = job.items.length > 3;
  const details = h("details", { "data-key": job.id, open: !many || open.has(String(job.id)) || job.status === "running" },
    h("summary", { text: `${job.items.length} item${job.items.length === 1 ? "" : "s"}: ${summary}` }), items);
  return h("div", { class: "card job" },
    h("div", { class: "job-head" },
      h("div", { class: "grow" },
        h("div", { class: "job-title", text: job.title || job.url }),
        h("div", { class: "job-meta", text: `${job.platform} · ${job.engine} · ${job.format} · ${fmtDate(job.created)}${job.deliver ? " · send to device" : ""}` }),
        h("div", { class: "job-meta" }, h("a", { href: job.url, target: "_blank", rel: "noopener noreferrer", text: job.url }))),
      h("span", { class: `status ${job.status}`, text: job.status })),
    details,
    actions.length ? h("div", { class: "job-actions" }, actions) : null);
}

async function jobAction(id, action) {
  try {
    await api(`api/jobs/${id}/${action}`, { method: "POST" });
    refreshJobs();
  } catch (e) { if (e.status !== 401) toast(e.message, 6000); }
}

$("#clear-jobs").addEventListener("click", async () => {
  if (!confirm("Remove finished, failed and canceled jobs from the history? Downloaded files stay in the library.")) return;
  try {
    const r = await api("api/jobs/clear", { method: "POST" });
    toast(`Removed ${r.removed} job(s)`);
    refreshJobs();
  } catch (e) { if (e.status !== 401) toast(e.message, 6000); }
});

function startPolling() {
  stopPolling();
  const tick = async () => {
    await refreshJobs();
    const active = state.jobs.some((j) => j.status === "running" || j.status === "queued");
    state.pollTimer = setTimeout(tick, active ? 1500 : 8000);
  };
  tick();
}
function stopPolling() {
  clearTimeout(state.pollTimer);
  state.pollTimer = null;
}

// -------------------------------------------------------------- library

let searchTimer = null;
$("#lib-platform").addEventListener("change", () => loadLibrary(true));
$("#lib-kind").addEventListener("change", () => loadLibrary(true));
$("#lib-search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => loadLibrary(true), 300);
});
$("#lib-more").addEventListener("click", () => loadLibrary(false));

async function loadLibrary(reset) {
  const lib = state.lib;
  if (reset) { lib.offset = 0; lib.files = []; lib.selected.clear(); }
  const params = new URLSearchParams({
    platform: $("#lib-platform").value, kind: $("#lib-kind").value, q: $("#lib-search").value.trim(),
    offset: lib.offset, limit: 120,
  });
  let data;
  try { data = await api(`api/library?${params}`); } catch (e) { if (e.status !== 401) toast(e.message, 6000); return; }
  lib.files = lib.files.concat(data.files);
  lib.total = data.total;
  lib.offset = lib.files.length;
  const sel = $("#lib-platform");
  const current = sel.value;
  sel.replaceChildren(h("option", { value: "", text: "All platforms" }),
    data.platforms.map((p) => h("option", { value: p, text: p, selected: p === current })));
  renderLibrary(reset);
}

function renderLibrary(reset) {
  const lib = state.lib;
  const grid = $("#lib-grid");
  if (reset) grid.replaceChildren();
  for (const f of lib.files.slice(grid.children.length)) grid.append(libTile(f));
  if (!lib.files.length) grid.replaceChildren(h("p", { class: "muted", text: "No files." }));
  $("#lib-count").textContent = `${lib.files.length} of ${lib.total}`;
  $("#lib-more").classList.toggle("hidden", lib.files.length >= lib.total);
  updateLibSelection();
}

function libTile(f) {
  const lib = state.lib;
  const thumb = h("div", { class: "thumb", text: ICONS[f.kind] || "•" });
  if (f.kind !== "other") {
    const img = h("img", { loading: "lazy", alt: "", src: thumbUrl(f.path) });
    img.addEventListener("error", () => img.remove());
    img.addEventListener("load", () => { thumb.firstChild && thumb.firstChild.nodeType === 3 && thumb.firstChild.remove(); });
    thumb.append(img);
  }
  const box = h("input", { type: "checkbox", class: lib.selectMode ? "" : "hidden", checked: lib.selected.has(f.path), "aria-label": `Select ${f.name}` });
  const tile = h("div", { class: `tile${lib.selected.has(f.path) ? " selected" : ""}` }, thumb, box,
    h("span", { class: "tag", text: f.kind }),
    h("div", { class: "cap", text: f.name }),
    h("div", { class: "sub", text: `${fmtBytes(f.size)} · ${f.platform || "—"}` }));
  const toggle = (on) => {
    if (on) lib.selected.add(f.path); else lib.selected.delete(f.path);
    box.checked = on;
    tile.classList.toggle("selected", on);
    updateLibSelection();
  };
  tile._toggle = toggle;
  tile._box = box;
  tile.addEventListener("click", (ev) => {
    if (lib.selectMode) { if (ev.target !== box) toggle(!lib.selected.has(f.path)); }
    else openPreview(f);
  });
  box.addEventListener("change", () => toggle(box.checked));
  return tile;
}

function updateLibSelection() {
  const lib = state.lib;
  $("#lib-select-mode").textContent = lib.selectMode ? "Done" : "Select";
  $("#lib-selection").classList.toggle("hidden", !lib.selectMode);
  $("#lib-zip").textContent = `Download ZIP (${lib.selected.size})`;
  $("#lib-delete").textContent = `Delete (${lib.selected.size})`;
  $("#lib-zip").disabled = $("#lib-delete").disabled = lib.selected.size === 0;
}

$("#lib-select-mode").addEventListener("click", () => {
  const lib = state.lib;
  lib.selectMode = !lib.selectMode;
  if (!lib.selectMode) lib.selected.clear();
  for (const tile of $("#lib-grid").children) {
    if (!tile._box) continue;
    tile._box.classList.toggle("hidden", !lib.selectMode);
    if (!lib.selectMode) tile._toggle(false);
  }
  updateLibSelection();
});
$("#lib-sel-all").addEventListener("click", () => { for (const t of $("#lib-grid").children) t._toggle && t._toggle(true); });

$("#lib-zip").addEventListener("click", () => {
  const paths = [...state.lib.selected];
  if (!paths.length) return;
  if (paths.length === 1) { location.href = fileUrl(paths[0], true); return; }
  const form = $("#zip-form");
  form.replaceChildren(h("input", { type: "hidden", name: "csrf", value: state.csrf }),
    paths.map((p) => h("input", { type: "hidden", name: "paths", value: p })));
  form.submit();
});

async function deleteFiles(paths) {
  const label = paths.length === 1 ? `"${paths[0]}"` : `${paths.length} files`;
  if (!confirm(`Delete ${label} from the Pi? This cannot be undone.`)) return false;
  try {
    const r = await api("api/library/delete", { method: "POST", body: { paths } });
    toast(r.errors.length ? `Deleted ${r.deleted}, failed: ${r.errors.join("; ")}` : `Deleted ${r.deleted} file(s)`, 6000);
    loadLibrary(true);
    return true;
  } catch (e) { if (e.status !== 401) toast(e.message, 6000); return false; }
}
$("#lib-delete").addEventListener("click", () => deleteFiles([...state.lib.selected]));

function openPreview(f) {
  const body = $("#modal-body");
  const src = fileUrl(f.path, false);
  let media;
  if (f.kind === "image") media = h("img", { src, alt: f.name });
  else if (f.kind === "video") media = h("video", { src, controls: true, playsinline: true, preload: "metadata" });
  else if (f.kind === "audio") media = h("audio", { src, controls: true, preload: "metadata" });
  else media = h("div", { class: "plain", text: "No preview for this file type." });
  body.replaceChildren(media);
  $("#modal-title").textContent = f.path;
  $("#modal-download").href = fileUrl(f.path, true);
  $("#modal-delete").onclick = async () => { if (await deleteFiles([f.path])) closeModal(); };
  $("#modal").classList.remove("hidden");
}
function closeModal() {
  $("#modal").classList.add("hidden");
  $("#modal-body").replaceChildren();  // stops playback
}
$("#modal-close").addEventListener("click", closeModal);
$("#modal").addEventListener("click", (ev) => { if (ev.target.id === "modal") closeModal(); });
document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") closeModal(); });

// ------------------------------------------------------------- settings

async function loadCookies() {
  const list = $("#cookie-list");
  let data;
  try { data = await api("api/cookies"); } catch { return; }
  if (!data.cookies.length) {
    list.replaceChildren(h("p", { class: "muted", text: "No cookies uploaded." }));
    return;
  }
  list.replaceChildren(...data.cookies.map((c) => h("div", { class: "cookie-row" },
    h("strong", { class: "grow", text: c.platform }),
    h("span", { class: "muted", text: `${c.entries} cookies · ${fmtDate(c.updated)}` }),
    h("button", { type: "button", class: "danger", text: "Delete", onclick: async () => {
      if (!confirm(`Delete cookies for ${c.platform}?`)) return;
      try { await api(`api/cookies/${encodeURIComponent(c.platform)}`, { method: "DELETE" }); toast("Cookies deleted"); loadCookies(); }
      catch (e) { if (e.status !== 401) toast(e.message, 6000); }
    } }))));
}

$("#cookie-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const platform = $("#cookie-platform").value.trim().toLowerCase();
  const file = $("#cookie-file").files[0];
  if (!file) return;
  if (!confirm(`Upload ${file.name} as cookies for "${platform}"? Existing cookies for it will be replaced.`)) return;
  const form = new FormData();
  form.append("platform", platform);
  form.append("file", file);
  try {
    const r = await api("api/cookies", { method: "POST", form });
    toast(`Saved ${r.entries} cookies for ${r.platform}`);
    ev.target.reset();
    loadCookies();
  } catch (e) { if (e.status !== 401) toast(e.message, 8000); }
});

function renderVersions(versions) {
  $("#engine-versions").replaceChildren(...Object.entries(versions).flatMap(([k, v]) => [h("dt", { text: k }), h("dd", { text: v })]));
}

async function loadEngines() {
  $("#engine-versions").replaceChildren(h("dt", { text: "…" }), h("dd", { text: "checking versions" }));
  try { renderVersions((await api("api/engines")).versions); } catch {}
}

$("#engines-update").addEventListener("click", async () => {
  if (!confirm("Update yt-dlp and gallery-dl now? New downloads wait until it finishes.")) return;
  const button = $("#engines-update");
  const out = $("#engines-output");
  button.disabled = true;
  out.classList.remove("hidden");
  out.textContent = "Updating… this can take a few minutes on the Pi.";
  try {
    const r = await api("api/engines/update", { method: "POST" });
    out.textContent = (r.ok ? "Update finished.\n\n" : "Update FAILED.\n\n") + r.output;
    renderVersions(r.versions);
  } catch (e) {
    out.textContent = e.message;
  } finally {
    button.disabled = false;
  }
});

// ---------------------------------------------------------------- start

(async () => {
  try {
    const me = await api("api/me");
    state.csrf = me.csrf;
    state.user = me.user;
    showApp();
  } catch {
    showLogin();
  }
})();
