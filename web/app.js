"use strict";
/* Resume Screener front end. Resume text is untrusted: everything interpolated into HTML goes through esc(). */

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const state = {
  cfg: null, jd: "", rubric: [],
  batch: null, job: null, poll: null,
  results: [], used: [],             // results + the rubric they were scored with
  sort: { key: "rank", dir: 1 },
  f: { min: 0, q: "", th: 70, status: new Set(["scored"]) },
  limit: 200, view: [],
};
const STORE = "screener.v1";
const PAGE = 200;

/* ---------------------------------------------------------------- helpers */
async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch { /* not json */ }
    throw new Error(msg);
  }
  return r.json();
}
function banner(msg, ok = false) {
  const b = $("#banner");
  b.textContent = msg || "";
  b.hidden = !msg;
  b.className = "banner" + (ok ? " ok" : "");
}
// Edits are kept in the browser, tagged with the code defaults they started from (`base`), so a change to the
// defaults in code (rubric.py / default_jd.txt) replaces stale browser edits on the next load.
const baseOf = (cfg) => JSON.stringify({ jd: cfg.jd, rubric: cfg.rubric });
function save() {
  try { localStorage.setItem(STORE, JSON.stringify({ jd: state.jd, rubric: state.rubric, base: baseOf(state.cfg) })); } catch { /* storage blocked */ }
}
function load() {
  try { return JSON.parse(localStorage.getItem(STORE) || "null"); } catch { return null; }
}
const num = (v) => (Number.isFinite(+v) ? +v : 0);
const total = (rub) => rub.reduce((a, c) => a + Math.max(num(c.weight), 0), 0);

/* ---------------------------------------------------------------- the ruler */
function ruler(items, cls = "") {
  const segs = items.map((c) => {
    const w = Math.max(num(c.weight), 0);
    const fill = c.pts == null ? 100 : w ? Math.min(100, (c.pts / w) * 100) : 0;
    return `<span class="seg" style="--w:${w};--fill:${fill}%" title="${esc(c.name)}${c.pts == null ? ` · ${w}` : ` · ${c.pts}/${Math.floor(w)}`}"><i></i></span>`;
  });
  return `<div class="ruler ${cls}" aria-hidden="true">${segs.join("")}</div>`;
}

/* ---------------------------------------------------------------- tabs */
function showTab(name) {
  for (const t of ["brief", "screen", "results"]) {
    $("#t-" + t).setAttribute("aria-selected", String(t === name));
    $("#tab-" + t).hidden = t !== name;
  }
}
$$(".tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.id.slice(2))));
$(".tabs").addEventListener("keydown", (e) => {
  const tabs = $$(".tabs button:not(:disabled)");
  const i = tabs.indexOf(document.activeElement);
  const fwd = ["ArrowRight", "ArrowDown"].includes(e.key), back = ["ArrowLeft", "ArrowUp"].includes(e.key);
  if (i < 0 || !(fwd || back)) return;
  e.preventDefault();
  const n = tabs[(i + (fwd ? 1 : tabs.length - 1)) % tabs.length];
  n.focus(); n.click();
});

/* ---------------------------------------------------------------- job & rubric */
function renderWeightRuler() {
  const t = total(state.rubric);
  const n = state.rubric.filter((c) => c.name.trim()).length;
  $("#ruler-title").textContent = `${n} criteria · ${t % 1 ? t.toFixed(1) : t} points`;
  $("#weight-ruler").outerHTML = ruler(state.rubric.filter((c) => c.name.trim()), "big").replace('class="ruler', 'id="weight-ruler" class="ruler');
  $("#weight-legend").innerHTML = state.rubric.filter((c) => c.name.trim())
    .map((c) => `<li>${esc(c.name)}<b>${esc(c.weight)}</b></li>`).join("");
  $("#weight-note").textContent = Math.abs(t - 100) < 0.01 ? "" : `Weights add up to ${t}, not 100. The final score is out of ${t}.`;
}

function renderCriteria() {
  const rows = state.rubric.map((c, i) => `
    <tr data-i="${i}">
      <td><textarea data-f="name" rows="1" aria-label="Criterion name">${esc(c.name)}</textarea></td>
      <td><textarea class="k" data-f="key" rows="1" aria-label="Key, a unique id">${esc(c.key)}</textarea></td>
      <td><input type="number" min="0" max="100" step="1" data-f="weight" value="${esc(c.weight)}" aria-label="Weight for ${esc(c.name || "criterion")}"></td>
      <td><textarea data-f="description" rows="2" aria-label="What earns a high score for ${esc(c.name || "criterion")}">${esc(c.description)}</textarea></td>
      <td><button class="btn link sm" type="button" data-del="${i}" aria-label="Remove ${esc(c.name || "criterion")}">Remove</button></td>
    </tr>`).join("");
  $("#criteria").innerHTML = `<table class="rubric-table">
    <thead><tr><th scope="col">Criterion</th><th scope="col">Key</th><th scope="col">Weight</th><th scope="col">What earns a high score</th><th scope="col"><span class="sr-only">Actions</span></th></tr></thead>
    <tbody>${rows || '<tr><td colspan="5" class="muted">No criteria yet. Add one below.</td></tr>'}</tbody></table>`;
  $$("#criteria textarea").forEach(grow);
  renderWeightRuler();
}
function grow(t) { t.style.height = "auto"; t.style.height = t.scrollHeight + 2 + "px"; }
$("#criteria").addEventListener("input", (e) => {
  const row = e.target.closest("tr[data-i]");
  if (!row || !e.target.dataset.f) return;
  const c = state.rubric[+row.dataset.i];
  c[e.target.dataset.f] = e.target.dataset.f === "weight" ? num(e.target.value) : e.target.value;
  if (e.target.tagName === "TEXTAREA") grow(e.target);
  renderWeightRuler(); save();
});
$("#criteria").addEventListener("click", (e) => {
  const d = e.target.closest("[data-del]");
  if (!d) return;
  state.rubric.splice(+d.dataset.del, 1);
  renderCriteria(); save();
});
$("#add-criterion").addEventListener("click", () => {
  let n = state.rubric.length + 1;
  while (state.rubric.some((c) => c.key === "c" + n)) n++;
  state.rubric.push({ key: "c" + n, name: "", weight: 5, description: "" });
  renderCriteria(); save();
  $$("#criteria [data-f=name]").at(-1).focus();
});
$("#jd").addEventListener("input", (e) => { state.jd = e.target.value; save(); });

$("#jd-file").addEventListener("change", async (e) => {
  const f = e.target.files[0];
  e.target.value = "";
  if (!f) return;
  const fd = new FormData(); fd.append("file", f);
  try {
    const { text } = await api("/api/jd", { method: "POST", body: fd });
    state.jd = text; $("#jd").value = text; save(); banner("Job description loaded.", true);
  } catch (err) { banner(err.message); }
});

$("#gen-rubric").addEventListener("click", async (e) => {
  const btn = e.currentTarget; btn.disabled = true; btn.textContent = "Drafting…";
  try {
    const { criteria } = await api("/api/rubric/generate", {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ jd: state.jd }),
    });
    state.rubric = criteria; renderCriteria(); save(); banner("Criteria drafted from the job description. Review the weights.", true);
  } catch (err) { banner(err.message); }
  btn.disabled = false; btn.textContent = "Draft from job description";
});
$("#reset-rubric").addEventListener("click", () => {
  state.jd = state.cfg.jd; state.rubric = structuredClone(state.cfg.rubric);
  $("#jd").value = state.jd; renderCriteria(); save(); banner("Reset to the default job description and rubric.", true);
});

/* ---------------------------------------------------------------- upload */
const CHUNK = 100;
async function uploadFiles(files) {
  banner("");
  files = [...files];
  if (!files.length) return;
  try {
    for (let i = 0; i < files.length; i += CHUNK) {
      $("#upload-status").textContent = `Reading resumes… ${Math.min(i + CHUNK, files.length)} of ${files.length} files`;
      const fd = new FormData();
      files.slice(i, i + CHUNK).forEach((f) => fd.append("files", f));
      if (state.batch) fd.append("batch", state.batch.batch);
      state.batch = await api("/api/resumes", { method: "POST", body: fd });
    }
    $("#upload-status").textContent = "";
    renderBatch();
  } catch (err) {
    $("#upload-status").textContent = "";
    banner("Upload failed: " + err.message);
  }
}
function renderBatch() {
  const b = state.batch;
  $("#batch").hidden = !b;
  if (!b) return;
  $("#b-found").textContent = b.found; $("#b-readable").textContent = b.readable;
  $("#b-unreadable").textContent = b.unreadable; $("#b-dups").textContent = b.duplicates;
  $("#b-bad").hidden = !b.bad.length;
  $("#b-bad summary").textContent = `${b.bad.length} file(s) need manual review`;
  $("#b-bad-list").innerHTML = b.bad.map((x) => `<li><b>${esc(x.file)}</b>: ${esc(x.problem)}</li>`).join("");
  $("#b-estimate").textContent = `About ${b.readable * state.cfg.samples} AI calls (${state.cfg.samples} per resume; cached resumes are free).`;
  const ok = b.readable > 0 && state.cfg.ready;
  $("#run").disabled = !ok;
  if (b.readable > 0 && !state.cfg.ready) banner("Set LLM_API_KEY in .env, restart the server, then screen.");
}
const drop = $("#drop"), picker = $("#resume-files");
drop.addEventListener("click", () => picker.click());
drop.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); picker.click(); } });
picker.addEventListener("change", () => { uploadFiles(picker.files); picker.value = ""; });
["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (e) => uploadFiles(e.dataTransfer.files));
$("#conc").addEventListener("input", (e) => { $("#conc-out").textContent = e.target.value; });
$("#clear-batch").addEventListener("click", async () => {
  if (state.batch) await fetch("/api/resumes/" + state.batch.batch, { method: "DELETE" });
  state.batch = null; renderBatch();
});

/* ---------------------------------------------------------------- run + progress */
$("#run").addEventListener("click", async () => {
  banner("");
  try {
    const { job } = await api("/api/screen", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ batch: state.batch.batch, jd: state.jd, rubric: state.rubric, concurrency: +$("#conc").value }),
    });
    state.job = job;
    $("#run").disabled = true; $("#progress").hidden = false; $("#stop").disabled = false;
    state.poll = setInterval(pollJob, 700); pollJob();
  } catch (err) { banner("Fix this first: " + err.message); }
});
$("#stop").addEventListener("click", () => { fetch(`/api/jobs/${state.job}/stop`, { method: "POST" }); $("#stop").disabled = true; });

async function pollJob() {
  let j;
  try { j = await api("/api/jobs/" + state.job); } catch (err) { clearInterval(state.poll); banner(err.message); return; }
  const pct = j.total ? Math.round((j.done / j.total) * 100) : 0;
  const rate = j.elapsed > 0 ? j.done / j.elapsed : 0;
  const eta = rate ? (j.total - j.done) / rate / 60 : 0;
  $("#bar-fill").style.width = pct + "%";
  $(".bar").setAttribute("aria-valuenow", pct);
  $("#progress-text").textContent = `${j.done}/${j.total} screened · ${rate.toFixed(1)}/s` + (j.status === "running" ? ` · about ${eta.toFixed(1)} min left` : "");
  const c = j.counts;
  $("#progress-counts").textContent = `${c.scored} scored (${c.cached} from cache) · ${c.error} errors · ${c.unreadable} unreadable`;
  if (j.status === "running") return;
  clearInterval(state.poll);
  $("#run").disabled = false;
  if (j.status === "error") { banner("Screening failed: " + j.error); return; }
  state.results = j.results; state.used = j.rubric;
  const errs = j.results.filter((r) => r.status === "error");
  $("#run-meta").textContent = `Sorted by total score · ${j.model} · run time ${(j.elapsed / 60).toFixed(1)} min` + (j.status === "stopped" ? " · stopped early" : "");
  $("#t-results").disabled = false;
  $("#progress").hidden = true;
  initResults();
  showTab("results");
  if (errs.length) banner(`${errs.length} resume(s) failed (for example: ${errs[0].error.slice(0, 160)}). Run again to retry only those; successful scores are cached.`);
}

/* ---------------------------------------------------------------- results */
function initResults() {
  $("#results-empty").hidden = true; $("#results-body").hidden = false; $("#exports").hidden = false;
  $("#dl-xlsx").href = `/api/jobs/${state.job}/export.xlsx`;
  $("#dl-csv").href = `/api/jobs/${state.job}/export.csv`;
  state.limit = PAGE;
  renderHead(); renderResults();
}
const sortVal = (r, key) => {
  if (key === "rank") return r.rank ?? 1e9;
  if (key === "score") return r.score ?? -1;
  if (key === "name") return (r.candidate_name || r.filename).toLowerCase();
  if (key === "status") return r.status;
  return r.criteria?.[key]?.score ?? -1;
};
function renderHead() {
  const s = state.sort;
  const th = (key, label, extra = "") => {
    const aria = s.key === key ? ` aria-sort="${s.dir > 0 ? "ascending" : "descending"}"` : "";
    return `<th data-k="${esc(key)}"${aria} tabindex="0" scope="col">${label}${extra}</th>`;
  };
  $("#thead").innerHTML = "<tr>" + th("rank", "Rank") + th("name", "Candidate") + th("score", "Score")
    + state.used.map((c) => th(c.key, esc(c.name), ` <span class="w">/${Math.floor(c.weight)}</span>`)).join("")
    + '<th scope="col" style="cursor:default">Summary</th>' + th("status", "Status") + "</tr>";
}
function applyFilters() {
  const { min, q, status } = state.f;
  const needle = q.trim().toLowerCase();
  let rows = state.results.filter((r) => status.has(r.status) && (r.status !== "scored" || (r.score ?? 0) >= min));
  if (needle) {
    rows = rows.filter((r) => [r.candidate_name, r.filename, r.summary, r.role, (r.strengths || []).join(" ")]
      .join(" ").toLowerCase().includes(needle));
  }
  const { key, dir } = state.sort;
  rows = rows.map((r) => [sortVal(r, key), r]).sort((a, b) => {
    const [x, y] = [a[0], b[0]];
    const c = x < y ? -1 : x > y ? 1 : 0;
    // score columns read best high-to-low when first clicked: dir stores the user's choice
    return c * dir || (a[1].rank ?? 1e9) - (b[1].rank ?? 1e9);
  }).map((x) => x[1]);
  return rows;
}
function renderResults() {
  const scored = state.results.filter((r) => r.status === "scored");
  const scores = scored.map((r) => r.score).sort((a, b) => a - b);
  const med = scores.length ? (scores[(scores.length - 1) >> 1] + scores[scores.length >> 1]) / 2 : null;
  const th = state.f.th;
  $("#kpis").innerHTML = [
    ["Scored", scored.length], ["Top score", scores.length ? scores.at(-1) : "–"],
    ["Median", med == null ? "–" : med], [`At or above ${th}`, scores.filter((s) => s >= th).length],
    ["Not scored", state.results.length - scored.length],
  ].map(([k, v]) => `<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join("");

  state.view = applyFilters();
  const shown = state.view.slice(0, state.limit);
  $("#tbody").innerHTML = shown.map((r, i) => rowHTML(r, i)).join("") ||
    `<tr><td colspan="${state.used.length + 5}" class="muted">No candidates match these filters.</td></tr>`;
  $("#table-foot").textContent = `Showing ${shown.length} of ${state.view.length} (${state.results.length} total)`;
  $("#more").hidden = shown.length >= state.view.length;

  const bins = Array(10).fill(0);
  scores.forEach((s) => { bins[Math.min(9, Math.floor(s / 10))]++; });
  const mx = Math.max(...bins, 1);
  $("#dist").innerHTML = bins.map((n, i) => `<div style="height:${(n / mx) * 100}%" title="${i * 10}–${i * 10 + 10}: ${n}"><span>${n || ""}</span><em>${i * 10}</em></div>`).join("");
}
const band = (s) => (s >= state.f.th ? "hi" : s >= state.f.th * 0.75 ? "mid" : "lo");
const fmt = (t) => (t % 1 ? t.toFixed(1) : t);
function rowHTML(r, i) {
  const name = esc(r.candidate_name || r.filename);
  const flag = r.red_flags?.length ? `<span class="tag flag" title="${esc(r.red_flags.join("; "))}">Red flag</span>` : "";
  const cand = `<td class="cand"><b>${name}${flag}</b><span>${esc(r.filename)}</span></td>`;
  if (r.status !== "scored") {
    return `<tr tabindex="0" data-i="${i}"><td class="rank">–</td>${cand}<td class="score">–</td>`
      + `${state.used.map(() => "<td></td>").join("")}<td class="sum"><span class="clamp">${esc(r.error)}</span></td><td><span class="tag ${r.status === "duplicate" ? "" : "bad"}">${esc(r.status)}</span></td></tr>`;
  }
  const items = state.used.map((c) => ({ ...c, pts: r.criteria[c.key]?.score ?? 0 }));
  const cells = items.map((c) => `<td class="num${c.pts ? "" : " zero"}">${c.pts}/${Math.floor(c.weight)}</td>`).join("");
  return `<tr tabindex="0" data-i="${i}"><td class="rank">#${r.rank}</td>${cand}`
    + `<td class="score"><span class="pill ${band(r.score)}">${r.score}</span>${ruler(items, "mini")}</td>${cells}`
    + `<td class="sum"><span class="clamp">${esc(r.summary)}</span></td><td><span class="tag ok">Scored</span></td></tr>`;
}

$("#thead").addEventListener("click", (e) => {
  const th = e.target.closest("th[data-k]");
  if (!th) return;
  const k = th.dataset.k;
  state.sort = { key: k, dir: state.sort.key === k ? -state.sort.dir : (k === "rank" || k === "name" || k === "status" ? 1 : -1) };
  renderHead(); renderResults();
});
$("#thead").addEventListener("keydown", (e) => { if (e.key === "Enter") e.target.click(); });
$("#tbody").addEventListener("click", (e) => { const tr = e.target.closest("tr[data-i]"); if (tr) openDetail(+tr.dataset.i); });
$("#tbody").addEventListener("keydown", (e) => { if (e.key === "Enter") { const tr = e.target.closest("tr[data-i]"); if (tr) openDetail(+tr.dataset.i); } });
$("#more").addEventListener("click", () => { state.limit += PAGE; renderResults(); });
$("#f-min").addEventListener("input", (e) => { state.f.min = +e.target.value; $("#min-out").textContent = e.target.value; renderResults(); });
$("#f-q").addEventListener("input", (e) => { state.f.q = e.target.value; state.limit = PAGE; renderResults(); });
$("#f-th").addEventListener("input", (e) => { state.f.th = num(e.target.value); renderResults(); });
$$(".statuses input").forEach((cb) => cb.addEventListener("change", () => {
  state.f.status = new Set($$(".statuses input:checked").map((x) => x.value)); state.limit = PAGE; renderResults();
}));

/* ---------------------------------------------------------------- candidate detail */
let cur = 0;
function openDetail(i) {
  cur = i;
  const r = state.view[i];
  if (!r) return;
  const dlg = $("#detail");
  const list = (arr) => (arr?.length ? `<ul>${arr.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : "");
  const items = state.used.map((c) => ({ ...c, pts: r.criteria?.[c.key]?.score ?? 0, ev: r.criteria?.[c.key]?.evidence || "" }));
  const meta = [r.role, r.email, r.phone, r.filename].filter(Boolean).map(esc).join(" · ");
  const body = r.status === "scored" ? `
    <div class="score-box">
      <div>
        <div class="big-score">${r.score}<small> / ${fmt(total(state.used))}</small></div>
        ${ruler(items, "big")}
      </div>
      <span class="pill ${band(r.score)}">${r.score >= state.f.th ? "Shortlist" : "Below threshold"}</span>
    </div>
    <p class="summary">${esc(r.summary)}</p>
    ${r.red_flags?.length ? `<div class="flagbox"><b>Red flags</b>${list(r.red_flags)}</div>` : ""}
    <div class="two-col">
      ${r.strengths?.length ? `<div><h3>Strengths</h3>${list(r.strengths)}</div>` : ""}
      ${r.gaps?.length ? `<div><h3>Gaps</h3>${list(r.gaps)}</div>` : ""}
    </div>
    <h3>Score breakdown</h3>
    <div class="break">${items.map((c) => `
      <div><div class="head"><span>${esc(c.name)}</span><span class="pts">${c.pts}/${Math.floor(c.weight)}</span></div>
      <div class="ruler" aria-hidden="true"><span class="seg" style="--w:1;--fill:${c.weight ? Math.min(100, (c.pts / c.weight) * 100) : 0}%"><i></i></span></div>
      <p>${esc(c.ev) || "No evidence cited."}</p></div>`).join("")}
    </div>`
    : `<p class="summary"><span class="tag bad">${esc(r.status)}</span></p><p>${esc(r.error)}</p>`;
  dlg.innerHTML = `<div class="sheet">
    <div class="sheet-top">
      <span class="mono">${r.rank ? "Rank #" + r.rank : esc(r.status)} · ${i + 1} of ${state.view.length}</span>
      <span class="actions">
        <button class="btn secondary sm" type="button" id="d-prev" ${i === 0 ? "disabled" : ""}>Previous</button>
        <button class="btn secondary sm" type="button" id="d-next" ${i >= state.view.length - 1 ? "disabled" : ""}>Next</button>
        <button class="btn secondary sm" type="button" id="d-close">Close</button>
      </span>
    </div>
    <div class="sheet-body">
      <h2 id="d-title">${esc(r.candidate_name || r.filename)}</h2>
      <p class="sub">${meta}</p>${body}
    </div></div>`;
  if (!dlg.open) dlg.showModal();
  dlg.scrollTop = 0;
  $("#d-close").onclick = () => dlg.close();
  $("#d-prev").onclick = () => openDetail(cur - 1);
  $("#d-next").onclick = () => openDetail(cur + 1);
}
$("#detail").addEventListener("click", (e) => { if (e.target === e.currentTarget) e.currentTarget.close(); });

/* ---------------------------------------------------------------- cache + boot */
$("#clear-cache").addEventListener("click", async () => {
  try { await api("/api/cache/clear", { method: "POST" }); banner("Score cache cleared. The next run re-scores every resume.", true); }
  catch (err) { banner(err.message); }
});

(async function boot() {
  try {
    state.cfg = await api("/api/config");
  } catch (err) { banner("Could not reach the server: " + err.message); return; }
  let saved = load();
  const stale = saved && saved.base !== baseOf(state.cfg);
  if (stale) {
    saved = null;
    try { localStorage.removeItem(STORE); } catch { /* storage blocked */ }
  }
  state.jd = saved?.jd ?? state.cfg.jd;
  state.rubric = saved?.rubric?.length ? saved.rubric : structuredClone(state.cfg.rubric);
  if (stale) banner("The default job description or rubric changed in the code, so it has been reloaded.", true);
  $("#jd").value = state.jd;
  $("#gen-rubric").disabled = !state.cfg.can_generate;
  const cc = state.cfg.concurrency, conc = $("#conc");
  if (cc) { conc.min = cc.min; conc.max = cc.max; conc.value = cc.default; $("#conc-out").textContent = cc.default; }
  const chip = $("#model-chip");
  chip.hidden = false;
  chip.innerHTML = `<span class="k">Model</span><span class="v">${esc(state.cfg.model)}</span><span class="s">${esc(state.cfg.label)} · ${esc(state.cfg.samples)}× per resume</span>`;
  renderCriteria();
  if (!state.cfg.ready) banner("No API key found. Set LLM_API_KEY in .env and restart the server to enable screening.");
})();
