/* baas_platform Studio — sin dependencias. Habla con /dashboard/api/* */
"use strict";
const $ = (s, r = document) => r.querySelector(s);
const state = { token: sessionStorage.getItem("baas_admin_token"), projects: [], ref: localStorage.getItem("baas_ref"), view: "overview", range: "30d", data: null, table: null, offset: 0 };
const COLORS = ["#a6f12b", "#6f9a1c", "#4d6d14", "#7d8587", "#565c5e", "#3a3f41"];
const SERVICE_NAMES = { rest: "API REST", auth: "Auth", storage: "Storage", realtime: "Realtime", functions: "Functions", other: "Otros" };
const TITLES = {
  overview: ["Resumen", "Rendimiento, comportamiento e información operativa"],
  tables: ["Tablas", "Explora el esquema y los datos del proyecto"],
  sql: ["Editor SQL", "Ejecuta una sentencia con el rol service_role del proyecto"],
  project: ["Proyecto y claves", "Claves de API, usuarios, backups y zona de peligro"],
};

const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (n) => (n >= 1e6 ? (n / 1e6).toFixed(1) + " M" : n >= 1e4 ? (n / 1e3).toFixed(1) + " k" : Number(n).toLocaleString("es"));
const bytes = (n) => (n > 1e9 ? (n / 1e9).toFixed(2) + " GB" : n > 1e6 ? (n / 1e6).toFixed(1) + " MB" : n > 1e3 ? (n / 1e3).toFixed(1) + " KB" : n + " B");
function toast(msg, err = false) { const t = $("#toast"); t.textContent = msg; t.className = "toast show" + (err ? " err" : ""); clearTimeout(toast.t); toast.t = setTimeout(() => (t.className = "toast"), 3200); }

async function api(path, opts = {}) {
  const res = await fetch("/dashboard/api" + path, {
    method: opts.method || "GET",
    headers: { "Content-Type": "application/json", ...(state.token ? { Authorization: "Bearer " + state.token } : {}) },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (res.status === 401 && path !== "/login") { logout(); throw new Error("Sesión expirada"); }
  if (res.status === 204) return null;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data?.error?.message || `Error ${res.status}`);
  return data;
}

/* ---------- sesión ---------- */
function showLogin() { $("#login").hidden = false; $("#app").hidden = true; }
function logout() { state.token = null; sessionStorage.removeItem("baas_admin_token"); showLogin(); }
$("#logout").onclick = logout;
$("#login-form").onsubmit = async (e) => {
  e.preventDefault(); $("#login-error").textContent = "";
  try {
    const r = await api("/login", { method: "POST", body: { email: $("#login-email").value, password: $("#login-pass").value } });
    state.token = r.access_token; sessionStorage.setItem("baas_admin_token", r.access_token);
    $("#avatar").textContent = r.email.slice(0, 2).toUpperCase();
    await boot();
  } catch (err) { $("#login-error").textContent = err.message; }
};

/* ---------- proyectos ---------- */
async function boot() {
  $("#login").hidden = true; $("#app").hidden = false;
  state.projects = await api("/projects");
  if (!state.projects.find((p) => p.ref === state.ref)) state.ref = state.projects[0]?.ref || null;
  const sel = $("#project-select");
  sel.innerHTML = state.projects.map((p) => `<option value="${esc(p.ref)}">${esc(p.name)}</option>`).join("");
  sel.hidden = !state.projects.length; $("#range-select").hidden = state.view !== "overview";
  if (state.ref) sel.value = state.ref, localStorage.setItem("baas_ref", state.ref);
  $("#empty").hidden = !!state.projects.length; $("#content").hidden = !state.projects.length;
  if (state.ref) render();
}
$("#project-select").onchange = (e) => { state.ref = e.target.value; localStorage.setItem("baas_ref", state.ref); state.table = null; render(); };
$("#range-select").onchange = (e) => { state.range = e.target.value; render(); };
document.querySelectorAll(".rail-btn[data-view]").forEach((b) => (b.onclick = () => setView(b.dataset.view)));
function setView(v) {
  state.view = v;
  document.querySelectorAll(".rail-btn[data-view]").forEach((b) => b.classList.toggle("active", b.dataset.view === v));
  $("#view-title").textContent = TITLES[v][0]; $("#view-sub").textContent = TITLES[v][1];
  $("#range-select").hidden = v !== "overview"; render();
}
function openNewProject() { $("#project-error").textContent = ""; $("#project-name").value = ""; $("#dlg-project").showModal(); $("#project-name").focus(); }
$("#new-project").onclick = openNewProject; $("#empty-create").onclick = openNewProject;
$("#project-form").onsubmit = async (e) => {
  if (e.submitter?.value !== "ok") return;
  e.preventDefault();
  try {
    const p = await api("/projects", { method: "POST", body: { name: $("#project-name").value } });
    $("#dlg-project").close(); state.ref = p.ref; toast("Proyecto creado"); await boot(); setView("project");
  } catch (err) { $("#project-error").textContent = err.message; }
};

async function render() {
  if (!state.ref) return;
  const c = $("#content"); c.innerHTML = '<p class="muted">Cargando…</p>';
  try { await ({ overview: renderOverview, tables: renderTables, sql: renderSql, project: renderProject })[state.view](c); }
  catch (err) { c.innerHTML = `<div class="card"><p class="error">${esc(err.message)}</p></div>`; }
}

/* ---------- resumen ---------- */
async function renderOverview(c) {
  const d = (state.data = await api(`/projects/${state.ref}/overview?range=${state.range}`));
  const kpi = (k) => {
    const dp = k.delta_pct, cls = dp == null ? "flat" : dp < 0 ? "down" : "";
    const chip = dp == null ? "" : `<span class="chip ${cls}">${dp < 0 ? "▼" : "▲"} ${dp > 0 ? "+" : ""}${dp}%</span>`;
    return `<div class="card kpi"><div class="label">${esc(k.label)}</div><div class="value">${fmt(k.value)} ${chip}</div></div>`;
  };
  const total = d.series.reduce((a, p) => a + p.value, 0), maxV = Math.max(1, ...d.series.map((p) => p.value));
  const maxIdx = d.series.findIndex((p) => p.value === Math.max(...d.series.map((q) => q.value)));
  const bars = d.series.map((p, i) => `<div class="bar ${i === maxIdx && p.value > 0 ? "max" : ""}"><span class="tip">${fmt(p.value)}</span><div class="col" style="height:${Math.max(2, (p.value / maxV) * 100)}%"></div><span class="x">${d.series.length > 15 && i % 3 ? "" : esc(p.label)}</span></div>`).join("");
  const tabs = [["7d", "Semanal"], ["30d", "Mensual"], ["12m", "Anual"]].map(([k, l]) => `<button data-range="${k}" class="${state.range === k ? "active" : ""}">${l}</button>`).join("");

  const sTotal = d.services.reduce((a, s) => a + s.count, 0), R = 52, C = 2 * Math.PI * R;
  let acc = 0;
  const arcs = d.services.map((s, i) => { const len = (s.count / sTotal) * C, el = `<circle r="${R}" cx="70" cy="70" fill="none" stroke="${COLORS[i % COLORS.length]}" stroke-width="16" stroke-dasharray="${len} ${C - len}" stroke-dashoffset="${-acc}" transform="rotate(-90 70 70)"/>`; acc += len; return el; }).join("");
  const legend = d.services.map((s, i) => `<li><i class="dot" style="background:${COLORS[i % COLORS.length]}"></i>${esc(SERVICE_NAMES[s.service] || s.service)}<span class="n">${fmt(s.count)}</span></li>`).join("") || '<li class="muted">Sin tráfico todavía</li>';

  const hm = d.heatmap, cells = [`<span></span>`].concat(Array.from({ length: 24 }, (_, h) => `<span class="hh">${h % 6 === 0 ? String(h).padStart(2, "0") : ""}</span>`));
  hm.matrix.forEach((row, w) => { cells.push(`<span>${esc(hm.weekdays[w])}</span>`); row.forEach((n, h) => { const a = hm.max ? n / hm.max : 0, isPeak = hm.peak && hm.peak.weekday === hm.weekdays[w] && hm.peak.hour === h; cells.push(`<span class="cell ${isPeak ? "peak" : ""}" title="${esc(hm.weekdays[w])} ${h}:00 — ${n}" style="${n ? `background:rgba(166,241,43,${0.15 + a * 0.85})` : ""}"></span>`); }); });
  const filled = Math.round(hm.weekday_share / 10);
  const dashes = Array.from({ length: 10 }, (_, i) => `<i class="${i < filled ? "" : "off"}"></i>`).join("");

  c.innerHTML = `
    <div class="grid kpis">${d.kpis.map(kpi).join("")}</div>
    <div class="grid mid">
      <div class="card"><h2>Indicadores</h2><ul class="ledger">${d.ledger.map((l) => `<li><div class="l">${esc(l.label)}</div><div class="v">${esc(l.value)}</div></li>`).join("")}<li><div class="l">Conexiones realtime</div><div class="v">${d.realtime_connections}</div></li><li><div class="l">Storage</div><div class="v">${d.storage.objects} archivos · ${bytes(d.storage.bytes)}</div></li></ul></div>
      <div class="card"><div class="chart-head"><div><div class="muted">Peticiones a la API</div><div class="chart-total">${fmt(total)}</div></div><div class="tabs" id="tabs">${tabs}</div></div><div class="bars" role="img" aria-label="Peticiones por periodo">${bars}</div></div>
      <div class="card"><h2>Uso por servicio</h2><div class="donut-wrap"><svg width="140" height="140" viewBox="0 0 140 140" role="img" aria-label="Distribución por servicio"><circle r="${R}" cx="70" cy="70" fill="none" stroke="#1d2021" stroke-width="16"/>${arcs}</svg><div class="donut-center"><b>${fmt(sTotal)}</b><span>Total</span></div></div><ul class="legend">${legend}</ul><div class="note">${esc(d.kpis[1].label)}: <b>${fmt(d.kpis[1].value)}</b> de ${fmt(d.kpis[0].value)} usuarios en este periodo.</div></div>
    </div>
    <div class="grid bottom">
      <div class="card insight"><span class="pill">✧ Insight</span><p>${esc(d.insight)}</p><div class="dashes" aria-label="Tráfico en días laborables: ${hm.weekday_share}%">${dashes}</div></div>
      <div class="card"><div class="chart-head"><h2>Peticiones por día y hora (30 días)</h2></div><div class="heat">${cells.join("")}</div><div class="heat-stats"><div><span>Hora pico</span><b>${hm.peak ? esc(hm.peak.weekday) + " " + String(hm.peak.hour).padStart(2, "0") + ":00" : "—"}</b></div><div><span>Entre semana</span><b>${hm.weekday_share}%</b></div><div><span>Después de 18:00</span><b>${hm.after_18_share}%</b></div></div></div>
      <div class="card chat"><h2>Pregunta sobre estos datos</h2><p class="muted">Las respuestas usan las cifras de esta página.</p><div class="log" id="chat-log" aria-live="polite"></div><div class="chips">${["¿Cuál es la tasa de error?", "Resumir anomalías", "¿Qué servicio se usa más?"].map((q) => `<button type="button">${q}</button>`).join("")}</div><form id="chat-form"><input id="chat-input" placeholder="Pregunta por usuarios, errores, servicios…" aria-label="Pregunta"><button class="btn primary" aria-label="Enviar">➤</button></form></div>
    </div>`;
  $("#tabs").onclick = (e) => { const k = e.target.dataset?.range; if (k) { state.range = k; $("#range-select").value = k; render(); } };
  const ask = (q) => { const log = $("#chat-log"); log.insertAdjacentHTML("beforeend", `<div class="msg me">${esc(q)}</div><div class="msg">${esc(answer(q, d))}</div>`); log.scrollTop = log.scrollHeight; };
  c.querySelectorAll(".chips button").forEach((b) => (b.onclick = () => ask(b.textContent)));
  $("#chat-form").onsubmit = (e) => { e.preventDefault(); const i = $("#chat-input"); if (i.value.trim()) ask(i.value.trim()); i.value = ""; };
}

function answer(q, d) {
  q = q.toLowerCase();
  const kpi = (k) => d.kpis.find((x) => x.key === k), led = (t) => d.ledger.find((l) => l.label.toLowerCase().includes(t));
  if (/anomal|raro|pico|atípic/.test(q)) {
    const v = d.series.map((p) => p.value), mean = v.reduce((a, b) => a + b, 0) / (v.length || 1), sd = Math.sqrt(v.reduce((a, b) => a + (b - mean) ** 2, 0) / (v.length || 1));
    const out = d.series.filter((p) => sd > 0 && p.value > mean + 2 * sd);
    return out.length ? `Hay ${out.length} periodo(s) por encima de lo normal: ${out.map((p) => `${p.label} (${fmt(p.value)})`).join(", ")}. La media es ${fmt(Math.round(mean))}.` : "No veo anomalías: ningún periodo supera la media en más de dos desviaciones estándar.";
  }
  if (/error|fall/.test(q)) return `La tasa de error es ${led("error").value} sobre ${fmt(kpi("requests").value)} peticiones.`;
  if (/servicio|usa|api|tráfico|trafico/.test(q)) { const s = d.services[0]; return s ? `${SERVICE_NAMES[s.service] || s.service} es el más usado con ${fmt(s.count)} peticiones.` : "Todavía no hay tráfico registrado."; }
  if (/usuari|activ|nuevo/.test(q)) return `Hay ${fmt(kpi("total_users").value)} usuarios; ${fmt(kpi("active_users").value)} activos y ${fmt(kpi("new_users").value)} nuevos en el periodo.`;
  if (/latenc|lent|rápid|rapid/.test(q)) return `Latencia media ${led("media").value} y p95 ${led("p95").value}.`;
  if (/hora|cuándo|cuando/.test(q)) return d.heatmap.peak ? `El pico es ${d.heatmap.peak.weekday} a las ${String(d.heatmap.peak.hour).padStart(2, "0")}:00.` : "Aún no hay datos de actividad por hora.";
  return "Puedo responder sobre usuarios, errores, latencia, servicios, anomalías y horas pico con los datos visibles.";
}

/* ---------- tablas ---------- */
async function renderTables(c) {
  const tables = await api(`/projects/${state.ref}/tables`);
  if (!tables.length) { c.innerHTML = `<div class="card empty"><h2>Sin tablas</h2><p class="muted">Crea una en el editor SQL, por ejemplo:<br><code>CREATE TABLE tareas (id serial PRIMARY KEY, titulo text)</code></p></div>`; return; }
  if (!state.table || !tables.find((t) => t.name === state.table)) { state.table = tables[0].name; state.offset = 0; }
  const info = tables.find((t) => t.name === state.table), res = await api(`/projects/${state.ref}/tables/${encodeURIComponent(state.table)}/rows?limit=50&offset=${state.offset}`);
  const list = tables.map((t) => `<button data-t="${esc(t.name)}" class="${t.name === state.table ? "active" : ""}"><span>${esc(t.name)}</span>${t.kind === "view" ? '<span class="badge">vista</span>' : t.rls_enabled ? '<span class="badge">RLS</span>' : '<span class="badge warn" title="Sin Row Level Security: cualquiera con la clave anon puede leer y escribir">sin RLS</span>'}</button>`).join("");
  const head = res.columns.map((col) => `<th>${esc(col.name)} <span class="muted">${esc(col.data_type)}${col.is_primary_key ? " · PK" : ""}</span></th>`).join("");
  const body = res.rows.map((r) => `<tr>${res.columns.map((col) => `<td title="${esc(typeof r[col.name] === "object" ? JSON.stringify(r[col.name]) : r[col.name])}">${r[col.name] === null ? '<span class="muted">null</span>' : esc(typeof r[col.name] === "object" ? JSON.stringify(r[col.name]) : r[col.name])}</td>`).join("")}</tr>`).join("");
  c.innerHTML = `<div class="split"><div class="card"><h2>Tablas</h2><div class="tlist" id="tlist">${list}</div></div><div class="card"><div class="chart-head"><h2>${esc(state.table)} <span class="muted">${res.total} filas</span></h2><div class="row"><button class="btn small" id="prev" ${state.offset === 0 ? "disabled" : ""}>←</button><span class="muted">${res.total ? state.offset + 1 : 0}–${Math.min(state.offset + 50, res.total)}</span><button class="btn small" id="next" ${state.offset + 50 >= res.total ? "disabled" : ""}>→</button></div></div>${!info.rls_enabled && info.kind === "table" ? '<p class="note">⚠ Esta tabla no tiene RLS. Actívalo con <code>ALTER TABLE ' + esc(info.name) + " ENABLE ROW LEVEL SECURITY</code> y crea políticas.</p>" : ""}<div class="scroll" style="margin-top:12px"><table class="data"><thead><tr>${head}</tr></thead><tbody>${body || `<tr><td colspan="${res.columns.length}" class="muted">Sin filas</td></tr>`}</tbody></table></div></div></div>`;
  $("#tlist").onclick = (e) => { const b = e.target.closest("button"); if (b) { state.table = b.dataset.t; state.offset = 0; render(); } };
  $("#prev").onclick = () => { state.offset = Math.max(0, state.offset - 50); render(); };
  $("#next").onclick = () => { state.offset += 50; render(); };
}

/* ---------- SQL ---------- */
function renderSql(c) {
  c.innerHTML = `<div class="stack"><div class="card"><textarea id="sql" class="sql" spellcheck="false" aria-label="Consulta SQL">${esc(state.lastSql || "SELECT now();")}</textarea><div class="row end" style="margin-top:10px"><span class="muted grow">Ctrl/⌘ + Enter para ejecutar · una sola sentencia</span><button class="btn primary" id="run">Ejecutar</button></div></div><div class="card" id="sql-out"><p class="muted">El resultado aparecerá aquí.</p></div></div>`;
  const run = async () => {
    const out = $("#sql-out"); state.lastSql = $("#sql").value; out.innerHTML = '<p class="muted">Ejecutando…</p>';
    try {
      const r = await api(`/projects/${state.ref}/sql`, { method: "POST", body: { query: state.lastSql } });
      out.innerHTML = r.columns.length ? `<p class="muted">${r.row_count} fila(s)${r.truncated ? " (resultado truncado)" : ""}</p><div class="scroll"><table class="data"><thead><tr>${r.columns.map((x) => `<th>${esc(x)}</th>`).join("")}</tr></thead><tbody>${r.rows.map((row) => `<tr>${r.columns.map((x) => `<td>${esc(typeof row[x] === "object" && row[x] !== null ? JSON.stringify(row[x]) : row[x])}</td>`).join("")}</tr>`).join("")}</tbody></table></div>` : `<p>✔ Ejecutado. Filas afectadas: ${r.row_count}</p>`;
    } catch (err) { out.innerHTML = `<p class="error">${esc(err.message)}</p>`; }
  };
  $("#run").onclick = run; $("#sql").onkeydown = (e) => { if ((e.ctrlKey || e.metaKey) && e.key === "Enter") run(); };
}

/* ---------- proyecto y claves ---------- */
async function renderProject(c) {
  const [keys, users, backups] = await Promise.all([api(`/projects/${state.ref}/keys`), api(`/projects/${state.ref}/users?limit=20`), api(`/projects/${state.ref}/backups`)]);
  const row = (label, val) => `<div class="keyrow"><span>${label}</span><code title="${esc(val)}">${esc(val)}</code><button class="btn small" data-copy="${esc(val)}">Copiar</button></div>`;
  c.innerHTML = `<div class="stack">
    <div class="card"><h2>Claves de API</h2><p class="muted">Envía <code>apikey</code> en cada petición. La clave service_role salta RLS: nunca la expongas en el navegador.</p>${row("URL del proyecto", keys.url)}${row("Referencia", keys.ref)}${row("anon (pública)", keys.anon_key)}${row("service_role (secreta)", keys.service_role_key)}<div class="row end" style="margin-top:12px"><button class="btn danger" id="rotate">Rotar claves</button></div></div>
    <div class="card"><div class="chart-head"><h2>Usuarios (${users.length} más recientes)</h2></div><div class="scroll" style="margin-top:10px"><table class="data"><thead><tr><th>Email</th><th>Proveedor</th><th>Creado</th><th>Último acceso</th></tr></thead><tbody>${users.map((u) => `<tr><td>${esc(u.email)}</td><td>${esc(u.provider)}</td><td>${esc(new Date(u.created_at).toLocaleString("es"))}</td><td>${u.last_sign_in_at ? esc(new Date(u.last_sign_in_at).toLocaleString("es")) : "—"}</td></tr>`).join("") || '<tr><td colspan="4" class="muted">Sin usuarios</td></tr>'}</tbody></table></div></div>
    <div class="card"><div class="chart-head"><h2>Backups</h2><button class="btn" id="backup">Crear backup ahora</button></div><div class="scroll" style="margin-top:10px"><table class="data"><thead><tr><th>Inicio</th><th>Estado</th><th>Tamaño</th><th>Archivo / error</th></tr></thead><tbody>${backups.map((b) => `<tr><td>${esc(new Date(b.started_at).toLocaleString("es"))}</td><td>${esc(b.status)}</td><td>${b.size_bytes ? bytes(b.size_bytes) : "—"}</td><td title="${esc(b.error || b.file_path)}">${esc(b.error || b.file_path)}</td></tr>`).join("") || '<tr><td colspan="4" class="muted">Aún no hay backups</td></tr>'}</tbody></table></div></div>
    <div class="card"><h2>Zona de peligro</h2><p class="muted">Elimina el proyecto, su schema, usuarios y archivos. No se puede deshacer.</p><div class="row end"><button class="btn danger" id="delete">Eliminar proyecto</button></div></div></div>`;
  c.querySelectorAll("[data-copy]").forEach((b) => (b.onclick = () => navigator.clipboard.writeText(b.dataset.copy).then(() => toast("Copiado"), () => toast("No se pudo copiar", true))));
  $("#rotate").onclick = async () => { if (confirm("Se invalidarán TODAS las claves y sesiones de usuario del proyecto. ¿Continuar?")) { await api(`/projects/${state.ref}/keys/rotate`, { method: "POST" }); toast("Claves rotadas"); render(); } };
  $("#backup").onclick = async (e) => { e.target.disabled = true; try { const r = await api(`/projects/${state.ref}/backups`, { method: "POST" }); toast(r.status === "completed" ? "Backup creado" : "Backup fallido: " + r.error, r.status !== "completed"); } catch (err) { toast(err.message, true); } render(); };
  $("#delete").onclick = async () => { const p = state.projects.find((x) => x.ref === state.ref); if (prompt(`Escribe el nombre del proyecto para confirmar: ${p.name}`) === p.name) { await api(`/projects/${state.ref}`, { method: "DELETE" }); state.ref = null; toast("Proyecto eliminado"); await boot(); } };
}

/* ---------- arranque ---------- */
(async () => {
  if (!state.token) return showLogin();
  try { await boot(); } catch { showLogin(); }
})();
