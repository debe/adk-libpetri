// The /petri page: edit a blueprint, see its net, prove it, replay runs.
"use strict";

const $ = (id) => document.getElementById(id);
const API = "/petri/api";

const state = {
  apps: [],
  app: null,
  file: null,
  dirty: false,
  viz: null,
  editor: null,
  net: null,          // last graph JSON
  view: null,         // {marking, fired, caption}
  selected: null,     // the <li> showing on the net
};

// -- HTTP ---------------------------------------------------------------------

async function api(method, path, body) {
  const res = await fetch(API + path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  let data;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) {
    const detail = data && data.detail ? data.detail : text || res.statusText;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data;
}

const enc = encodeURIComponent;
const filePath = (app, file) => `/apps/${enc(app)}/files/${file.split("/").map(enc).join("/")}`;

// -- the editor ------------------------------------------------------------------

function initEditor() {
  state.editor = CodeMirror.fromTextArea($("editor"), {
    mode: "yaml",
    lineNumbers: true,
    indentUnit: 2,
    tabSize: 2,
    lineWrapping: false,
    extraKeys: {
      Tab: (cm) => cm.replaceSelection("  "),
      "Cmd-S": () => save(),
      "Ctrl-S": () => save(),
    },
  });
  state.editor.on("change", () => { state.dirty = true; });
}

function setStatus(kind, html) {
  const el = $("status");
  el.className = "status" + (kind ? " " + kind : "");
  el.innerHTML = html;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function showCheck(report) {
  if (!report) { setStatus("", '<span class="muted">Saved. Not a blueprint, so nothing to check.</span>'); return; }
  if (report.ok) {
    const n = report.net;
    setStatus("ok", `✓ ${esc(n.name)}: ${n.places} places, ${n.transitions} transitions, ${n.subnets} subnets, ${n.claims} claims`);
  } else {
    const key = report.key_path ? `<span class="key">${esc(report.key_path)}</span>: ` : "";
    setStatus("bad", `${key}${esc(report.error)}`);
    jumpToKey(report.key_path);
  }
}

// Put the cursor on the line of the first segment of a YAML key path.
function jumpToKey(keyPath) {
  if (!keyPath) return;
  const parts = keyPath.split(/[.[\]]/).filter(Boolean);
  const cm = state.editor;
  let from = 0;
  for (const part of parts) {
    for (let i = from; i < cm.lineCount(); i++) {
      if (new RegExp(`^\\s*-?\\s*${part.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\s*:`).test(cm.getLine(i))) {
        from = i;
        break;
      }
    }
  }
  cm.setCursor({ line: from, ch: 0 });
  cm.scrollIntoView({ line: from, ch: 0 }, 80);
}

// -- apps and files ---------------------------------------------------------------

async function loadApps() {
  state.apps = await api("GET", "/apps");
  const sel = $("app");
  sel.innerHTML = state.apps.map((a) => `<option>${esc(a.name)}</option>`).join("");
  const want = new URLSearchParams(location.search).get("app");
  const first = state.apps.find((a) => a.name === want) ||
    state.apps.find((a) => a.files.some((f) => f.blueprint)) || state.apps[0];
  if (first) { sel.value = first.name; await pickApp(first.name); }
  else setStatus("bad", "No apps in this folder. Start the server in the folder that holds your agents.");
}

async function pickApp(name) {
  state.app = name;
  const app = state.apps.find((a) => a.name === name);
  const sel = $("file");
  sel.innerHTML = app.files
    .map((f) => `<option value="${esc(f.path)}">${esc(f.path)}${f.blueprint ? "  · net" : ""}</option>`)
    .join("");
  $("chat").href = `/dev-ui/?app=${enc(name)}`;
  const want = new URLSearchParams(location.search).get("file");
  const first = app.files.find((f) => f.path === want) ||
    app.files.find((f) => f.path === "root_agent.yaml" && f.blueprint) ||
    app.files.find((f) => f.blueprint) || app.files[0];
  if (first) { sel.value = first.path; await pickFile(first.path); }
  history.replaceState(null, "", `?app=${enc(name)}` + (first ? `&file=${enc(first.path)}` : ""));
  loadRuns();
}

async function pickFile(path) {
  state.file = path;
  const f = await api("GET", filePath(state.app, path));
  state.editor.setValue(f.content);
  state.editor.clearHistory();
  state.dirty = false;
  setStatus("", "");
  $("tab-proofs").innerHTML = '<p class="muted">Verify proves the file\'s <code>prove:</code> claims. A violated claim comes with a counterexample: step through it on the net.</p>';
  history.replaceState(null, "", `?app=${enc(state.app)}&file=${enc(path)}`);
  await check();
}

async function save() {
  if (!state.app || !state.file) return;
  await busy("save", async () => {
    const r = await api("PUT", filePath(state.app, state.file), { content: state.editor.getValue() });
    state.dirty = false;
    showCheck(r.check);
    if (!r.check || r.check.ok) await drawNet();
  });
}

async function check() {
  if (state.dirty) return save();
  await busy("check", async () => {
    const r = await api("POST", `/apps/${enc(state.app)}/check`, { file: state.file });
    showCheck(r);
    if (r.ok) await drawNet();
    else clearNet(r.error);
  }, true);
}

// -- the net ----------------------------------------------------------------------

async function drawNet(marking, fired, caption) {
  state.view = marking ? { marking, fired, caption } : null;
  const q = new URLSearchParams({ file: state.file });
  if (marking) q.set("marking", JSON.stringify(marking));
  if (fired) q.set("fired", fired);
  let r;
  try {
    r = await api("GET", `/apps/${enc(state.app)}/net?${q}`);
  } catch (e) {
    clearNet(e.message);
    return;
  }
  const sameNet = state.net && state.net.name === r.graph.name && state.netFile === state.file;
  state.net = r.graph;
  state.netFile = state.file;
  if (!state.viz) state.viz = await Viz.instance();
  const svg = state.viz.renderSVGElement(r.dot);
  const box = $("net");
  box.replaceChildren(svg);
  state.svgSize = { w: svg.width.baseVal.value, h: svg.height.baseVal.value };
  if (sameNet && state.zoom) applyZoom(); else fit();
  $("net-title").textContent = r.graph.name;
  $("net-caption").textContent = caption || "seed marking";
  $("reset").hidden = !marking;
}

function clearNet(message) {
  $("net").innerHTML = `<p class="muted">${esc(message || "No net to draw.")}</p>`;
  $("net-caption").textContent = "";
}

function markingText(m) {
  const parts = Object.entries(m || {}).filter(([, n]) => n > 0).map(([p, n]) => {
    const name = p.startsWith("inflight:") ? `${p.slice(9)} ▶` : p;
    return n === 1 ? name : `${name}×${n}`;
  });
  return parts.length ? parts.join(", ") : "empty";
}

// A proof splits a transition whose action takes time into its start and
// "complete:T", with an "inflight:T" place between.
function stepName(fired, before, after) {
  if (fired.startsWith("complete:")) return `${fired.slice(9)} completes`;
  const key = `inflight:${fired}`;
  if ((after[key] || 0) > (before[key] || 0)) return `${fired} starts`;
  return fired;
}

// A list of steps the user clicks through; each step redraws the net.
function stepList(steps) {
  const ul = document.createElement("ul");
  ul.className = "steps";
  steps.forEach((s, i) => {
    const li = document.createElement("li");
    li.innerHTML = `<span class="n">${i}</span><span class="t ${s.kind || ""}">${esc(s.label)}</span><span class="m">${esc(markingText(s.marking))}</span>`;
    li.addEventListener("click", () => {
      if (state.selected) state.selected.classList.remove("on");
      li.classList.add("on");
      state.selected = li;
      drawNet(s.marking, s.fired, s.caption);
    });
    ul.appendChild(li);
  });
  return ul;
}

// -- proofs -----------------------------------------------------------------------

async function verify() {
  if (state.dirty) await save();
  const k = parseInt($("k").value, 10);
  await busy("verify", async () => {
    $("tab-proofs").innerHTML = '<p class="muted">Proving…</p>';
    selectTab("proofs");
    const r = await api("POST", `/apps/${enc(state.app)}/verify`, { file: state.file, k: k > 0 ? k : null });
    renderProofs(r);
  });
}

function renderProofs(r) {
  const box = $("tab-proofs");
  box.innerHTML = "";
  if (r.error) {
    const key = r.key_path ? `<span class="key">${esc(r.key_path)}</span>: ` : "";
    box.innerHTML = `<div class="status bad">${key}${esc(r.error)}</div>`;
    return;
  }
  const head = document.createElement("p");
  head.className = "summary";
  head.innerHTML = `<strong>${r.proven}</strong> proven, <strong>${r.violated}</strong> violated, <strong>${r.unknown}</strong> unknown` +
    (r.z3 ? "" : ' <span class="muted">(no z3 binary: some claims stay unknown)</span>');
  box.appendChild(head);
  if (r.no_claims.length && !r.claims.length) {
    box.insertAdjacentHTML("beforeend", '<p class="muted">No claims under <code>prove:</code>. Add <code>deadlock_free</code> and a <code>place_bound</code> on <code>eventOut</code>.</p>');
  }
  for (const c of r.claims) {
    const el = document.createElement("div");
    el.className = "claim";
    el.innerHTML = `<div class="claim-head"><span class="badge ${c.verdict}">${c.verdict.toUpperCase()}</span><span class="label">${esc(c.label)}</span><span class="kind">${esc(c.kind)}${r.nets.length > 1 ? " · " + esc(c.net) : ""}</span></div>` +
      (c.scope ? `<div class="scope">under: ${esc(c.scope)}</div>` : "") +
      c.notes.map((n) => `<div class="note">note: ${esc(n)}</div>`).join("") +
      (c.reason ? `<div class="reason">${esc(c.reason)}</div>` : "");
    if (c.verdict === "violated" && c.markings.length) {
      const steps = c.markings.map((m, i) => {
        const name = i === 0 ? "start" : stepName(c.fires[i - 1], c.markings[i - 1], m);
        return {
          label: name,
          fired: i === 0 ? null : c.fires[i - 1],
          marking: m,
          caption: i === 0 ? "counterexample · start" : `counterexample · step ${i}: ${name}`,
        };
      });
      el.appendChild(stepList(steps));
    }
    box.appendChild(el);
  }
  const first = box.querySelector(".claim .steps li:last-child");
  if (first) first.click();
}

// -- runs -------------------------------------------------------------------------

async function loadRuns() {
  const box = $("runs");
  if (!state.app) return;
  let sessions;
  try {
    sessions = await api("GET", `/apps/${enc(state.app)}/sessions`);
  } catch (e) {
    box.innerHTML = `<p class="muted">${esc(e.message)}</p>`;
    return;
  }
  if (!sessions.length) {
    box.innerHTML = `<p class="muted">No runs yet. <a class="link" href="/dev-ui/?app=${enc(state.app)}" target="_blank" rel="noopener">Chat with the net in the dev UI</a>, then refresh.</p>`;
    return;
  }
  box.innerHTML = "";
  for (const s of sessions) {
    const el = document.createElement("div");
    el.className = "run";
    el.innerHTML = `<div class="run-head"><span class="sid" title="${esc(s.session)}">${esc(s.session)}</span><span class="muted small">${esc(s.user)}</span></div>`;
    el.querySelector(".run-head").addEventListener("click", () => openRun(el, s));
    box.appendChild(el);
  }
}

async function openRun(el, s) {
  const open = el.querySelector(".steps, .run-body");
  if (open) { el.querySelectorAll(".steps, .run-body").forEach((x) => x.remove()); return; }
  const t = await api("GET", `/apps/${enc(state.app)}/trace?user=${enc(s.user)}&session=${enc(s.session)}`);
  for (const [scope, trace] of Object.entries(t)) {
    const steps = trace.steps.map((st) => ({
      kind: st.kind,
      label: st.kind === "turn" ? "— turn —" : st.kind === "failed" ? `${st.transition} (failed)` : st.kind === "timed_out" ? `${st.transition} (timed out)` : st.transition,
      fired: st.kind === "turn" ? null : st.transition,
      marking: st.marking,
      caption: st.kind === "turn" ? `run · a turn's input arrived` : st.kind === "timed_out" ? `run · ${st.transition} timed out` : `run · ${st.transition} fired`,
    }));
    if (trace.dropped) {
      el.insertAdjacentHTML("beforeend", `<div class="run-body muted small" style="padding:0 10px 6px">${trace.dropped} earlier steps dropped</div>`);
    }
    if (Object.keys(t).length > 1) {
      el.insertAdjacentHTML("beforeend", `<div class="run-body muted small" style="padding:0 10px 6px">${esc(scope)}</div>`);
    }
    el.appendChild(stepList(steps));
  }
}

// -- zoom and pan -------------------------------------------------------------------

function applyZoom() {
  const svg = $("net").querySelector("svg");
  if (!svg || !state.zoom) return;
  const { k, x, y } = state.zoom;
  svg.style.transform = `translate(${x}px, ${y}px) scale(${k})`;
}

function fit() {
  const box = $("net");
  if (!state.svgSize) return;
  const pad = 16;
  const bw = box.clientWidth - pad * 2, bh = box.clientHeight - pad * 2;
  const k = Math.min(bw / state.svgSize.w, bh / state.svgSize.h, 1.5);
  state.zoom = {
    k,
    x: pad + (bw - state.svgSize.w * k) / 2,
    y: pad + Math.max(0, (bh - state.svgSize.h * k) / 2),
  };
  applyZoom();
}

function zoomAt(factor, cx, cy) {
  if (!state.zoom) return;
  const box = $("net").getBoundingClientRect();
  const px = cx === undefined ? box.width / 2 : cx - box.left;
  const py = cy === undefined ? box.height / 2 : cy - box.top;
  const z = state.zoom;
  const k = Math.min(8, Math.max(0.05, z.k * factor));
  state.zoom = { k, x: px - ((px - z.x) * k) / z.k, y: py - ((py - z.y) * k) / z.k };
  applyZoom();
}

function actualSize() {
  if (!state.zoom) return;
  zoomAt(1 / state.zoom.k);
}

function wireZoom() {
  const box = $("net");
  box.addEventListener("wheel", (e) => {
    e.preventDefault();
    zoomAt(Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.0015)), e.clientX, e.clientY);
  }, { passive: false });
  let drag = null;
  box.addEventListener("pointerdown", (e) => {
    if (!state.zoom || e.button !== 0) return;
    drag = { x: e.clientX, y: e.clientY, zx: state.zoom.x, zy: state.zoom.y };
    box.setPointerCapture(e.pointerId);
    box.classList.add("dragging");
  });
  box.addEventListener("pointermove", (e) => {
    if (!drag) return;
    state.zoom.x = drag.zx + e.clientX - drag.x;
    state.zoom.y = drag.zy + e.clientY - drag.y;
    applyZoom();
  });
  const end = () => { drag = null; box.classList.remove("dragging"); };
  box.addEventListener("pointerup", end);
  box.addEventListener("pointercancel", end);
  box.addEventListener("dblclick", fit);
  $("zoom-in").addEventListener("click", () => zoomAt(1.25));
  $("zoom-out").addEventListener("click", () => zoomAt(0.8));
  $("zoom-fit").addEventListener("click", fit);
  $("zoom-one").addEventListener("click", actualSize);
  $("focus").addEventListener("click", toggleFocus);
  window.addEventListener("resize", () => fit());
  document.addEventListener("keydown", (e) => {
    if (e.target.closest && e.target.closest(".CodeMirror, input, select, textarea")) return;
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === "+" || e.key === "=") zoomAt(1.25);
    else if (e.key === "-") zoomAt(0.8);
    else if (e.key === "0") fit();
    else if (e.key === "1") actualSize();
    else if (e.key === "f" || e.key === "F") toggleFocus();
    else return;
    e.preventDefault();
  });
}

function toggleFocus() {
  const on = document.body.classList.toggle("focus");
  $("focus").setAttribute("aria-pressed", String(on));
  requestAnimationFrame(() => { state.editor.refresh(); fit(); });
}

// -- chrome -----------------------------------------------------------------------

function selectTab(name) {
  document.querySelectorAll(".tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === name)));
  $("tab-proofs").hidden = name !== "proofs";
  $("tab-runs").hidden = name !== "runs";
  if (name === "runs") loadRuns();
}

async function busy(id, fn, quiet) {
  const b = $(id);
  b.disabled = true;
  try {
    await fn();
  } catch (e) {
    if (!quiet) setStatus("bad", esc(e.message));
    else setStatus("bad", esc(e.message));
  } finally {
    b.disabled = false;
  }
}

async function confirmLeave() {
  return !state.dirty || confirm("Discard unsaved changes?");
}

function wire() {
  $("app").addEventListener("change", async (e) => {
    if (await confirmLeave()) pickApp(e.target.value); else e.target.value = state.app;
  });
  $("file").addEventListener("change", async (e) => {
    if (await confirmLeave()) pickFile(e.target.value); else e.target.value = state.file;
  });
  $("save").addEventListener("click", save);
  $("check").addEventListener("click", check);
  $("verify").addEventListener("click", verify);
  $("reset").addEventListener("click", () => {
    if (state.selected) state.selected.classList.remove("on");
    state.selected = null;
    drawNet();
  });
  $("refresh-runs").addEventListener("click", loadRuns);
  document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => selectTab(b.dataset.tab)));
  window.addEventListener("beforeunload", (e) => { if (state.dirty) e.preventDefault(); });
}

initEditor();
wire();
wireZoom();
loadApps().catch((e) => setStatus("bad", esc(e.message)));
