"use strict";

const STAGES = ["captacao", "edicao", "publicacao"];
const MARK = { ok: "✓", erro: "✗", revisar: "revisar", fila: "na fila", andamento: "…" };
const $ = (sel, el = document) => el.querySelector(sel);
const cols = Object.fromEntries(STAGES.map((s) => [s, { el: $(`.coluna[data-stage="${s}"] .cards`), cards: new Map(), done: false, loading: false }]));
let state = { pausas: [] };

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || `erro ${res.status}`);
  return body;
}
const post = (path, data) => api(path, { method: "POST", body: JSON.stringify(data || {}) });
const channel = () => $("#canal").value || null;

// ---- colunas ---------------------------------------------------------------

function renderCard(stage, c) {
  const col = cols[stage];
  let el = col.cards.get(c.id);
  if (!el) {
    el = $("#tpl-card").content.firstElementChild.cloneNode(true);
    el.addEventListener("click", (ev) => { if (!ev.target.closest(".tentar")) openDetail(c.id); });
    $(".tentar", el).addEventListener("click", async () => {
      try { await post(`/api/videos/${c.id}/retry`); } catch (e) { logLine({ msg: e.message, level: "erro" }); }
    });
    col.cards.set(c.id, el);
  }
  el.className = `card ${c.status}`;
  el.dataset.id = c.id;
  $(".canal", el).textContent = c.canal + (c.formato ? ` · ${c.formato}` : "");
  $(".marca", el).textContent = MARK[c.status] || "";
  $(".titulo", el).textContent = c.manchete || c.titulo;
  $(".preenchido", el).style.width = `${c.progresso}%`;
  const err = $(".erro", el);
  err.hidden = !c.erro;
  err.textContent = c.erro || "";
  $(".tentar", el).hidden = c.status !== "erro";
  const img = $(".thumb", el);
  img.hidden = !c.thumb;
  if (c.thumb && img.getAttribute("src") !== c.thumb) img.src = c.thumb;
  return el;
}

function place(stage) {
  const col = cols[stage];
  const sorted = [...col.cards.entries()].sort((a, b) => b[0] - a[0]);
  sorted.forEach(([, el], i) => { if (col.el.children[i] !== el) col.el.insertBefore(el, col.el.children[i] || null); });
}

async function loadColumn(stage, { reset = false, more = false } = {}) {
  const col = cols[stage];
  if (col.loading || (more && col.done)) return;
  col.loading = true;
  try {
    const params = new URLSearchParams({ stage, limit: 30 });
    if (channel()) params.set("channel", channel());
    if (more && col.cards.size) params.set("before_id", Math.min(...col.cards.keys()));
    const items = await api(`/api/videos?${params}`);
    if (reset) { col.cards.clear(); col.el.replaceChildren(); col.done = false; }
    const seen = new Set(items.map((c) => c.id));
    if (!more) {  // a card that left this column (ex.: descartado) disappears on refresh
      const newest = items.length ? Math.min(...seen) : Infinity;
      for (const [id, el] of col.cards) if (id >= newest && !seen.has(id)) { el.remove(); col.cards.delete(id); }
    }
    items.forEach((c) => renderCard(stage, c));
    if (more && items.length < 30) col.done = true;
    place(stage);
  } catch (e) {
    logLine({ msg: `painel: ${e.message}`, level: "erro" });
  } finally {
    col.loading = false;
  }
}

let refreshTimer = null;
function refreshSoon() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(() => STAGES.forEach((s) => loadColumn(s)), 250);
}

STAGES.forEach((s) => cols[s].el.addEventListener("scroll", (ev) => {
  const el = ev.target;
  if (el.scrollTop + el.clientHeight > el.scrollHeight - 200) loadColumn(s, { more: true });
}));

function setProgress(id, pct) {
  for (const s of STAGES) {
    const el = cols[s].cards.get(id);
    if (el && el.classList.contains("andamento")) $(".preenchido", el).style.width = `${pct}%`;
  }
}

// ---- topo e pausas ---------------------------------------------------------

function isPaused(ch, stage) {
  return state.pausas.some((p) => p.channel === ch && p.stage === stage);
}

function paintPauses() {
  const all = isPaused(null, null);
  $("#pausar").textContent = all ? "Retomar tudo" : "Pausar tudo";
  $("#pausar").classList.toggle("ativo", all);
  document.querySelectorAll(".pausa-etapa").forEach((b) => {
    const on = isPaused(channel(), b.dataset.stage);
    b.textContent = on ? "Retomar" : "Pausar";
    b.classList.toggle("ativo", on);
  });
}

async function loadState() {
  state = await api("/api/state");
  const sel = $("#canal");
  if (sel.options.length === 1) {
    state.canais.forEach((c) => sel.add(new Option(c.nome, c.id)));
  }
  $("#ensaio").hidden = !state.ensaio;
  paintPauses();
}

async function togglePause(ch, stage) {
  const on = isPaused(ch, stage);
  await post(on ? "/api/resume" : "/api/pause", { channel: ch, stage });
  await loadState();
}

$("#pausar").addEventListener("click", () => togglePause(null, null));
document.querySelectorAll(".pausa-etapa").forEach((b) => b.addEventListener("click", () => togglePause(channel(), b.dataset.stage)));
$("#canal").addEventListener("change", () => { paintPauses(); STAGES.forEach((s) => loadColumn(s, { reset: true })); });
$("#buscar").addEventListener("click", async () => {
  const ids = channel() ? [channel()] : state.canais.map((c) => c.id);
  for (const id of ids) {
    try {
      const r = await post(`/api/capture-now?channel=${encodeURIComponent(id)}`);
      logLine({ msg: `[${id}] ${r.novos} pauta(s) nova(s)`, level: "info" });
    } catch (e) { logLine({ msg: e.message, level: "erro" }); }
  }
});

// ---- detalhe ---------------------------------------------------------------

function field(dl, label, value) {
  if (value === null || value === undefined || value === "") return;
  dl.append(Object.assign(document.createElement("dt"), { textContent: label }),
            Object.assign(document.createElement("dd"), { textContent: value }));
}

function button(label, fn) {
  const b = Object.assign(document.createElement("button"), { textContent: label, type: "button" });
  b.addEventListener("click", async () => {
    try { await fn(); $("#detalhe").close(); refreshSoon(); } catch (e) { alertInline(e.message); }
  });
  return b;
}

function alertInline(msg) {
  const p = Object.assign(document.createElement("p"), { className: "erro", textContent: msg });
  $("#detalhe-corpo").append(p);
}

async function openDetail(id) {
  const v = await api(`/api/videos/${id}`);
  const body = $("#detalhe-corpo");
  body.replaceChildren();
  const wrap = Object.assign(document.createElement("div"), { className: "detalhe" });
  const left = document.createElement("div");
  if (v.video) left.append(Object.assign(document.createElement("video"), { src: v.video, controls: true }));
  const right = document.createElement("div");
  right.append(Object.assign(document.createElement("h2"), { textContent: v.pacote?.manchete?.join(" ") || v.titulo_origem }));
  const dl = document.createElement("dl");
  field(dl, "Canal", v.canal);
  field(dl, "Estado", v.estado);
  field(dl, "Origem", v.url_origem);
  field(dl, "Fonte", v.pacote?.fonte || v.fonte);
  field(dl, "Formato", v.formato);
  field(dl, "Nota", v.nota);
  field(dl, "Roteiro", v.pacote?.roteiro);
  field(dl, "Diálogo", v.pacote?.dialogo?.map((l) => `${l.personagem}: ${l.fala}`).join("\n"));
  field(dl, "Título do post", v.pacote?.titulo_post);
  field(dl, "Motivo", v.erro);
  v.publicacoes.forEach((p) => field(dl, p.plataforma, p.url));
  right.append(dl);
  const actions = Object.assign(document.createElement("div"), { className: "botoes" });
  if (v.estado === "revisar") actions.append(button("Aprovar", () => post(`/api/videos/${id}/approve`)));
  if (v.estado === "erro") actions.append(button("Tentar de novo", () => post(`/api/videos/${id}/retry`)));
  if (v.estado === "editado") actions.append(button("Publicar agora", () => post(`/api/videos/${id}/publish-now`)));
  if (["revisar", "erro", "editado", "descoberto"].includes(v.estado)) {
    actions.append(button("Descartar", () => post(`/api/videos/${id}/discard`)));
  }
  right.append(actions);
  wrap.append(left, right);
  body.append(wrap);
  $("#detalhe").showModal();
}

// ---- log -------------------------------------------------------------------

function logLine(d, ts) {
  const ol = $("#log");
  const li = document.createElement("li");
  li.className = d.level || "info";
  const t = ts ? new Date(ts) : new Date();
  li.append(Object.assign(document.createElement("time"), { textContent: t.toLocaleTimeString("pt-BR") }),
            document.createTextNode(`${d.stage ? `[${d.stage}] ` : ""}${d.msg}`));
  const atBottom = ol.scrollTop + ol.clientHeight >= ol.scrollHeight - 20;
  ol.append(li);
  while (ol.children.length > 500) ol.firstElementChild.remove();
  if (atBottom) ol.scrollTop = ol.scrollHeight;
}

// ---- chat ------------------------------------------------------------------

function chatMsg(d) {
  const box = $("#chat-msgs");
  const div = Object.assign(document.createElement("div"), { className: `msg ${d.papel || "claude"}`, textContent: d.texto });
  if (d.confirmar?.length) {
    const row = Object.assign(document.createElement("div"), { className: "confirmar" });
    const ok = Object.assign(document.createElement("button"), { textContent: `Confirmar: ${d.confirmar.join(", ")}` });
    ok.addEventListener("click", () => { row.remove(); sendChat("Confirmado, pode executar.", d.confirmar); });
    const no = Object.assign(document.createElement("button"), { textContent: "Cancelar" });
    no.addEventListener("click", () => row.remove());
    row.append(ok, no);
    div.append(row);
  }
  box.append(div);
  box.scrollTop = box.scrollHeight;
}

async function sendChat(texto, liberar = []) {
  try { await post("/api/chat", { texto, liberar }); } catch (e) { chatMsg({ papel: "sistema", texto: e.message }); }
}

$("#chat-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const input = $("#chat-texto");
  const texto = input.value.trim();
  if (!texto) return;
  input.value = "";
  sendChat(texto);
});

// ---- tempo real ------------------------------------------------------------

let retry = 500;
function connect() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onopen = () => { retry = 500; $("#conexao").className = "conexao on"; $("#conexao").textContent = "ao vivo"; $("#log").replaceChildren(); refreshSoon(); };
  ws.onclose = () => {
    $("#conexao").className = "conexao off";
    $("#conexao").textContent = "reconectando…";
    setTimeout(connect, retry);
    retry = Math.min(retry * 2, 10000);
  };
  ws.onmessage = (msg) => {
    const ev = JSON.parse(msg.data);
    if (ev.type === "log") logLine(ev.data, ev.ts);
    else if (ev.type === "video.progresso") setProgress(ev.data.id, ev.data.pct);
    else if (ev.type === "video.estado" || ev.type === "video.removido") refreshSoon();
    else if (ev.type === "chat.mensagem") chatMsg(ev.data);
  };
}

loadState().then(() => STAGES.forEach((s) => loadColumn(s, { reset: true })));
connect();
