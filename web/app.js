import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const api = async (url, opts = {}) => {
  const r = await fetch(url, { headers: { "content-type": "application/json" }, ...opts,
    body: opts.body && typeof opts.body !== "string" ? JSON.stringify(opts.body) : opts.body });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.error || r.statusText);
  return d;
};
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const LS = { get: k => { try { return localStorage.getItem(k); } catch { return null; } }, set: (k, v) => { try { localStorage.setItem(k, v); } catch {} } };

const state = { project: null, data: null, sector: null, tab: LS.get("atlas.tab") || "carte", streams: new Map() };

// ------------------------------------------------------------ sidebar
async function loadEnv() {
  const e = await api("/api/env");
  $("#env").innerHTML = `<div><span class="detail">${esc(e.chip)} · ${e.mem_gb} Go · ${esc(e.disk_free)} libres</span></div>` +
    e.tools.map(t => `<div><span class="dot" style="background:${t.ok ? "var(--ok)" : "var(--warn)"}"></span><span>${esc(t.name)}<br><span class="detail">${esc(t.detail ?? "")}</span></span></div>`).join("");
}

async function loadProjects() {
  const ps = await api("/api/projects");
  $("#projects").innerHTML = ps.map(p => `<a href="#${p.slug}" data-p="${p.slug}" class="${p.slug === state.project ? "on" : ""}">${esc(p.name)}<small>${p.sectors} secteur(s) · ${p.built} construit(s) · workflow ${p.done}/${p.total}</small></a>`).join("") || `<p class="muted">Aucun projet.</p>`;
  if (!state.project && ps[0]) openProject(location.hash.slice(1) || ps[0].slug);
}
$("#projects").addEventListener("click", e => { const a = e.target.closest("a"); if (a) { e.preventDefault(); openProject(a.dataset.p); } });
$("#newProject").onclick = () => $("#dlgProject").showModal();
$("#projectForm").addEventListener("submit", async e => {
  if (e.submitter?.value !== "ok") return;
  const f = new FormData(e.target);
  try { const r = await api("/api/projects", { method: "POST", body: { name: f.get("name"), preset: f.get("preset") } }); await loadProjects(); openProject(r.slug); }
  catch (err) { alert(err.message); }
});

// ------------------------------------------------------------ projet
async function openProject(slug, keepSector = false) {
  state.project = slug;
  location.hash = slug;
  state.data = await api(`/api/projects/${slug}`);
  $$("#projects a").forEach(a => a.classList.toggle("on", a.dataset.p === slug));
  const d = state.data;
  $("#pname").textContent = d.name;
  const built = d.sectors.filter(s => s.status.built).length;
  const done = Object.values(d.workflow || {}).filter(w => w.status === "fait").length;
  $("#pstats").textContent = `${d.sectors.length} secteur(s) · ${built} construit(s) · workflow ${done}/${d.workflowDef.length}`;
  if (!keepSector || !d.sectors.find(s => s.slug === state.sector)) state.sector = d.sectors[0]?.slug ?? null;
  renderMap(); renderSectorList(); renderPresets(); renderWorkflow(); renderJobs(); fillSectorSelects();
  if (state.tab === "secteur") renderSector();
  // reprendre le suivi des tâches en cours
  d.jobs.filter(j => j.status === "running").forEach(j => follow(j.id));
}

async function refresh() { if (state.project) await openProject(state.project, true); loadProjects(); }

// ------------------------------------------------------------ onglets
function setTab(t) {
  state.tab = t; LS.set("atlas.tab", t);
  $$("#tabs button").forEach(b => b.classList.toggle("on", b.dataset.tab === t));
  $$(".tab").forEach(s => s.classList.toggle("on", s.id === `tab-${t}`));
  if (t === "carte") setTimeout(() => map?.invalidateSize(), 50);
  if (t === "secteur") renderSector();
}
$("#tabs").addEventListener("click", e => { const b = e.target.closest("button"); if (b) setTab(b.dataset.tab); });

// ------------------------------------------------------------ carte
let map, sectorLayer, draftMarker;
const IGN = (layer, fmt = "image/jpeg") => `https://data.geopf.fr/wmts?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0&LAYER=${layer}&STYLE=normal&TILEMATRIXSET=PM&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}&FORMAT=${fmt}`;
function initMap() {
  map = L.map("map", { zoomControl: true }).setView([44.66, -1.17], 12);
  const ortho = L.tileLayer(IGN("ORTHOIMAGERY.ORTHOPHOTOS"), { maxZoom: 20, maxNativeZoom: 19, attribution: "© IGN" });
  const plan = L.tileLayer(IGN("GEOGRAPHICALGRIDSYSTEMS.PLANIGNV2", "image/png"), { maxZoom: 19, attribution: "© IGN" });
  ortho.addTo(map);
  L.control.layers({ "Photo aérienne IGN": ortho, "Plan IGN": plan }).addTo(map);
  sectorLayer = L.layerGroup().addTo(map);
  map.on("click", e => {
    const f = $("#sectorForm");
    f.lon.value = e.latlng.lng.toFixed(5); f.lat.value = e.latlng.lat.toFixed(5);
    drawDraft();
  });
  ["lon", "lat", "size_m"].forEach(n => $("#sectorForm")[n].addEventListener("input", drawDraft));
}
function squareBounds([lon, lat], size) {
  const dLat = size / 2 / 111320, dLon = size / 2 / (111320 * Math.cos(lat * Math.PI / 180));
  return [[lat - dLat, lon - dLon], [lat + dLat, lon + dLon]];
}
function drawDraft() {
  const f = $("#sectorForm");
  const lon = parseFloat(f.lon.value), lat = parseFloat(f.lat.value);
  if (draftMarker) draftMarker.remove();
  if (isNaN(lon) || isNaN(lat)) return;
  draftMarker = L.rectangle(squareBounds([lon, lat], +f.size_m.value), { color: "#e8c98f", weight: 2, dashArray: "6 4", fill: false }).addTo(map);
}
function renderMap() {
  if (!map) initMap();
  sectorLayer.clearLayers();
  const d = state.data;
  const bounds = [];
  for (const s of d.sectors) {
    const b = squareBounds(s.center, s.size_m);
    bounds.push(...b);
    const col = s.status.built ? "#6fbf73" : s.status.fetched ? "#e0a24a" : "#8b93a0";
    L.rectangle(b, { color: col, weight: 2, fillOpacity: s.slug === state.sector ? .25 : .08 })
      .bindTooltip(s.name, { permanent: true, direction: "center", className: "sector-label" })
      .on("click", ev => { L.DomEvent.stopPropagation(ev); state.sector = s.slug; setTab("secteur"); })
      .addTo(sectorLayer);
  }
  if (bounds.length) map.fitBounds(bounds, { padding: [60, 60], maxZoom: 15 });
  else if (d.center) map.setView([d.center[1], d.center[0]], 12);
}
function renderPresets() {
  const have = new Set(state.data.sectors.map(s => s.name));
  $("#presets").innerHTML = state.data.presets.map((p, i) => `<button data-i="${i}" class="${have.has(p.name) ? "has" : ""}" title="${p.size_m} m">${esc(p.name)}</button>`).join("");
}
$("#presets").addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b) return;
  const p = state.data.presets[b.dataset.i];
  const f = $("#sectorForm");
  f.name.value = p.name; f.lon.value = p.center[0]; f.lat.value = p.center[1];
  if (![...f.size_m.options].some(o => +o.value === p.size_m)) f.size_m.add(new Option(`${p.size_m} m`, p.size_m));
  f.size_m.value = p.size_m;
  drawDraft();
  map.fitBounds(squareBounds(p.center, p.size_m), { padding: [80, 80] });
});
$("#sectorForm").addEventListener("submit", async e => {
  e.preventDefault();
  const f = e.target;
  try {
    const r = await api(`/api/projects/${state.project}/sectors`, { method: "POST",
      body: { name: f.name.value, center: [+f.lon.value, +f.lat.value], size_m: +f.size_m.value } });
    draftMarker?.remove(); f.reset();
    state.sector = r.slug;
    await refresh();
  } catch (err) { alert(err.message); }
});
function renderSectorList() {
  $("#sectorList").innerHTML = state.data.sectors.map(s => `<div class="s" data-s="${s.slug}"><span>${esc(s.name)}<br><small class="muted">${s.size_m} m</small></span><span class="badges">
    <span class="b ${s.status.fetched ? "ok" : ""}">données</span><span class="b ${s.status.built ? "ok" : ""}">3D</span><span class="b ${s.status.blender ? "ok" : ""}">blend</span><span class="b ${s.status.unreal ? "ok" : ""}">UE</span></span></div>`).join("") || `<p class="muted">Aucun secteur.</p>`;
}
$("#sectorList").addEventListener("click", e => { const s = e.target.closest(".s"); if (s) { state.sector = s.dataset.s; setTab("secteur"); } });

// ------------------------------------------------------------ secteur
const STEPS = [
  { action: "fetch", label: "Données", done: s => s.status.fetched },
  { action: "build", label: "Construire 3D", done: s => s.status.built },
  { action: "blender", label: "Blender", done: s => s.status.blender },
  { action: "facades", label: "Façades (photos)", done: s => s.status.facades },
  { action: "hq", label: "Rendu HQ", done: s => s.status.hq },
  { action: "unreal", label: "Paquet Unreal", done: s => s.status.unreal },
  { action: "ue-render", label: "Rendu Unreal", done: s => (s.status.renders || []).some(f => f.startsWith("unreal_")) },
];
function fillSectorSelects() {
  const opts = state.data.sectors.map(s => `<option value="${s.slug}">${esc(s.name)}</option>`).join("");
  $("#sectorPick").innerHTML = opts;
  const cur = $("#agentSector").value;
  $("#agentSector").innerHTML = `<option value="">(aucun)</option>` + opts;
  $("#agentSector").value = cur || state.sector || "";
}
$("#sectorPick").onchange = e => { state.sector = e.target.value; renderSector(); renderMap(); };

function curSector() { return state.data?.sectors.find(s => s.slug === state.sector); }
let lastViewerKey = null;
function renderSector() {
  const s = curSector();
  $("#sectorEmpty").hidden = !!s; $("#sectorView").hidden = !s;
  if (!s) return;
  $("#sectorPick").value = s.slug;
  $("#sectorInfo").textContent = `${s.size_m} m · centre ${s.center[1].toFixed(4)}, ${s.center[0].toFixed(4)} · MNT ${s.mnt_res_m} m · ortho ${s.ortho_res_m} m`;
  const running = state.data.jobs.find(j => j.sector === s.slug && j.status === "running");
  $("#pipeline").innerHTML = STEPS.map((st, i) => `<button data-a="${st.action}" class="${st.done(s) ? "done" : ""}" ${running ? "disabled" : ""}><span class="n">${st.done(s) ? "✓" : i + 1}</span>${st.label}</button>`).join("") +
    `<button data-a="all" class="primary" ${running ? "disabled" : ""}>Tout lancer</button><button data-a="refetch" class="ghost sm" ${running ? "disabled" : ""} title="Retélécharger toutes les sources">↻</button>`;
  const m = s.manifest, meta = s.meta || {};
  const rows = [
    ["Relief", meta.mnt_source || (s.status.fetched ? "ok" : "—")],
    ["Bâtiments BD TOPO", m ? `${m.buildings.total - m.buildings.ignores} (${m.buildings.sans_hauteur} hauteur estimée)` : meta["Bâti BD TOPO"] || "—"],
    ["Routes OSM", m ? m.roads : meta.OpenStreetMap || "—"],
    ["Photos Panoramax", meta.Panoramax || "—"],
    ["Altitudes", m ? `${m.heightmap.zmin_m.toFixed(1)} → ${m.heightmap.zmax_m.toFixed(1)} m` : "—"],
    ["Construit le", m ? m.built_at.replace("T", " ") : "—"],
  ];
  $("#sectorData").innerHTML = rows.map(([k, v]) => `<div><span>${k}</span><span>${esc(v)}</span></div>`).join("");
  renderSectorJob();
  const key = `${s.slug}:${m?.built_at}`;
  if (key !== lastViewerKey) { lastViewerKey = key; loadViewer(s); loadBlender(s); loadUnreal(s); loadPhotos(s); }
}
function renderSectorJob() {
  const s = curSector(); if (!s) return;
  const j = state.data.jobs.find(j => j.sector === s.slug && j.status === "running") || state.data.jobs.find(j => j.sector === s.slug);
  if (!j) { $("#sectorJob").innerHTML = "Aucune."; return; }
  $("#sectorJob").innerHTML = `<div>${esc(j.label)}</div><div class="st ${j.status}">${j.status} · ${esc(j.step || "")}</div>
    <div class="progress"><i style="width:${Math.round((j.progress || 0) * 100)}%"></i></div>
    <div class="muted" style="font-size:11px;white-space:pre-wrap">${esc((j.logTail || []).join("\n"))}</div>
    ${j.status === "running" ? `<button class="ghost sm" data-cancel="${j.id}">Annuler</button>` : `<button class="ghost sm" data-log="${j.id}">Voir le journal</button>`}`;
}
$("#sectorJob").addEventListener("click", async e => {
  const c = e.target.dataset.cancel, l = e.target.dataset.log;
  if (c) await api(`/api/jobs/${c}/cancel`, { method: "POST" });
  if (l) { setTab("journal"); showJob(l); }
});
$("#pipeline").addEventListener("click", async e => {
  const b = e.target.closest("button"); if (!b) return;
  try { const j = await api(`/api/projects/${state.project}/sectors/${state.sector}/run`, { method: "POST", body: { action: b.dataset.a } });
    state.data.jobs.unshift(j); renderSector(); follow(j.id); }
  catch (err) { alert(err.message); }
});
$("#openFolder").onclick = () => api(`/api/projects/${state.project}/sectors/${state.sector}/run`, { method: "POST", body: { action: "open-folder" } });
$("#openBlender").onclick = () => api(`/api/projects/${state.project}/sectors/${state.sector}/run`, { method: "POST", body: { action: "open-blender" } }).catch(e => alert(e.message));
$("#delSector").onclick = async () => {
  const s = curSector();
  if (!confirm(`Supprimer définitivement le secteur « ${s.name} » et ses fichiers ?`)) return;
  try { await api(`/api/projects/${state.project}/sectors/${s.slug}`, { method: "DELETE" }); state.sector = null; await refresh(); setTab("carte"); }
  catch (err) { alert(err.message); }
};
$(".vtabs").addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b) return;
  $$(".vtabs button").forEach(x => x.classList.toggle("on", x === b));
  $$(".vpane").forEach(p => p.classList.toggle("on", p.id === `v-${b.dataset.v}`));
  if (b.dataset.v === "3d") resizeViewer();
});

// ------------------------------------------------------------ visionneuse 3D
let renderer, scene, camera, controls, current;
function initViewer() {
  const el = $("#viewer");
  renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.shadowMap.enabled = true;
  el.appendChild(renderer.domElement);
  scene = new THREE.Scene();
  scene.background = new THREE.Color(0x9cc3dc);
  scene.fog = new THREE.Fog(0x9cc3dc, 800, 6000);
  camera = new THREE.PerspectiveCamera(50, 1, 0.5, 20000);
  controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.maxPolarAngle = Math.PI / 2.05;
  scene.add(new THREE.HemisphereLight(0xdbeaff, 0x7a6a50, 1.2));
  const sun = new THREE.DirectionalLight(0xfff1dc, 2.2);
  sun.position.set(-400, 600, 300); sun.castShadow = true;
  sun.shadow.mapSize.set(4096, 4096);
  Object.assign(sun.shadow.camera, { left: -1200, right: 1200, top: 1200, bottom: -1200, far: 4000 });
  scene.add(sun, sun.target);
  scene.userData.sun = sun;
  new ResizeObserver(resizeViewer).observe(el);
  (function loop() { requestAnimationFrame(loop); controls.update(); renderer.render(scene, camera); })();
}
function resizeViewer() {
  const el = $("#viewer"); if (!renderer || !el.clientWidth) return;
  renderer.setSize(el.clientWidth, el.clientHeight); camera.aspect = el.clientWidth / el.clientHeight; camera.updateProjectionMatrix();
}
function loadViewer(s) {
  if (!renderer) initViewer();
  if (current) { scene.remove(current); current = null; }
  if (!s.status.built) { $("#viewerHint").textContent = "Pas encore construit : lance « Données » puis « Construire 3D »."; return; }
  $("#viewerHint").textContent = "Chargement…";
  // scène HQ (toits LiDAR, façades relevées, arbres) si « Rendu HQ » a tourné, sinon le blockout
  const url = s.status.viewer ? `/files/${state.project}/${s.slug}/build/hq/viewer.glb?t=${s.status.viewer}`
                              : `/files/${state.project}/${s.slug}/build/sector.glb?t=${s.manifest.built_at}`;
  new GLTFLoader().load(url, g => {
    current = g.scene;
    current.traverse(o => { if (o.isMesh) { o.castShadow = o.receiveShadow = true;
      if (o.material?.transparent) { o.material.depthWrite = false; o.castShadow = false; } } });
    scene.add(current);
    const size = s.size_m;
    const sun = scene.userData.sun;
    sun.target.position.set(size / 2, 0, -size / 2);
    sun.position.set(size / 2 - size * .6, size * 1.2, -size / 2 + size * .5);
    Object.assign(sun.shadow.camera, { left: -size, right: size, top: size, bottom: -size, far: size * 4 });
    sun.shadow.camera.updateProjectionMatrix();
    controls.target.set(size / 2, 0, -size / 2);
    camera.position.set(size * -0.1, size * 0.55, size * 0.35);
    camera.far = size * 20; camera.updateProjectionMatrix();
    scene.fog.near = size * 1.5; scene.fog.far = size * 8;
    resizeViewer();
    $("#viewerHint").textContent = (s.status.viewer ? "Scène HQ (aperçu web : murs percés, vitrages, toits relevés ; sans menuiseries, textures ni voitures : voir l'onglet Rendus pour le rendu final) · " : "Blockout · ")
      + "Clic gauche : tourner · droit : déplacer · molette : zoom";
  }, undefined, err => { $("#viewerHint").textContent = "Erreur de chargement : " + err.message; });
}
function loadBlender(s) {
  const hq = s.status.renders || [];
  $("#blenderImg").hidden = !s.status.preview || hq.length > 0; $("#blenderEmpty").hidden = s.status.preview || hq.length > 0;
  if (s.status.preview) $("#blenderImg").src = `/files/${state.project}/${s.slug}/build/preview_blender.png?t=${Date.now()}`;
  $("#hqRenders").innerHTML = hq.map(f => `<figure><a href="/files/${state.project}/${s.slug}/build/hq/renders/${f}" target="_blank"><img loading="lazy" src="/files/${state.project}/${s.slug}/build/hq/renders/${f}?t=${Date.now()}"></a><figcaption>${esc(f.replace(".png", "").replaceAll("_", " "))}</figcaption></figure>`).join("");
}
async function loadUnreal(s) {
  if (!s.status.unreal) { $("#unrealDoc").textContent = "Lance « Rendu HQ » puis « Paquet Unreal » : il génère le projet Unreal 5.8 du secteur (géométrie, matériaux, arbres, caméras).\nEnsuite « Rendu Unreal » produit les images Lumen (onglet Rendus, préfixe unreal_)."; return; }
  $("#unrealDoc").textContent = await (await fetch(`/files/${state.project}/${s.slug}/build/unreal/README_UNREAL.md`)).text();
}

// ------------------------------------------------------------ photos Panoramax
let photoSel = new Set();
async function loadPhotos(s) {
  const d = await api(`/api/projects/${state.project}/sectors/${s.slug}/photos`);
  photoSel = new Set(d.selection.map(p => p.id));
  $("#photoCount").textContent = d.photos.length ? `${d.photos.length} photos de rue (Panoramax, licence libre) — clique pour sélectionner les objets à passer en 3D` : "Aucune photo (lance « Données »).";
  $("#photos").innerHTML = d.photos.slice(0, 400).map(p => `<figure data-id="${p.id}" class="${photoSel.has(p.id) ? "sel" : ""}"><img loading="lazy" src="${esc(p.thumb)}" alt=""><figcaption>${(p.date || "").slice(0, 10)}</figcaption></figure>`).join("");
  $("#photos").dataset.all = JSON.stringify(d.photos);
}
$("#photos").addEventListener("click", e => {
  const f = e.target.closest("figure"); if (!f) return;
  const id = f.dataset.id;
  photoSel.has(id) ? photoSel.delete(id) : photoSel.add(id);
  f.classList.toggle("sel");
});
$("#photos").addEventListener("dblclick", e => {
  const f = e.target.closest("figure"); if (!f) return;
  const p = JSON.parse($("#photos").dataset.all).find(x => x.id === f.dataset.id);
  if (p?.hd) window.open(p.hd, "_blank");
});
$("#savePhotos").onclick = async () => {
  const all = JSON.parse($("#photos").dataset.all || "[]");
  const r = await api(`/api/projects/${state.project}/sectors/${state.sector}/photos`, { method: "PUT", body: all.filter(p => photoSel.has(p.id)) });
  $("#photoCount").textContent = `${r.count} photo(s) sélectionnée(s) enregistrée(s) dans photos-selection.json`;
};

// ------------------------------------------------------------ workflow
function renderWorkflow() {
  const d = state.data, wf = d.workflow || {};
  const phases = [...new Set(d.workflowDef.map(s => s.phase))];
  $("#workflow").innerHTML = phases.map(ph => `<div class="phase"><h4>${esc(ph)}</h4>${d.workflowDef.filter(s => s.phase === ph).map(s => {
    const w = wf[s.id] || {}, st = w.status || "à faire";
    return `<div class="step ${st === "fait" ? "fait" : ""}" data-id="${s.id}">
      <div><h5><span class="kind ${s.kind}">${s.kind}</span>${esc(s.title)}</h5><p>${esc(s.desc)}</p><div class="tool">Outil : ${esc(s.tool)}</div>
      ${s.prompt ? `<textarea rows="3" data-prompt>${esc(w.prompt || s.prompt)}</textarea>` : ""}
      <textarea rows="1" data-notes placeholder="Notes…">${esc(w.notes || "")}</textarea></div>
      <div class="ctrl"><select data-status>${["à faire", "en cours", "fait"].map(o => `<option ${o === st ? "selected" : ""}>${o}</option>`).join("")}</select>
      ${s.kind === "auto" ? `<button class="ghost sm" data-go="secteur">Ouvrir le secteur</button>` : ""}
      ${s.prompt ? `<button class="primary sm" data-claude>Lancer avec Claude</button>` : ""}
      ${w.lastClaudeJob ? `<button class="ghost sm" data-log="${w.lastClaudeJob}">Dernier passage</button>` : ""}</div></div>`;
  }).join("")}</div>`).join("");
}
$("#workflow").addEventListener("change", async e => {
  const step = e.target.closest(".step"); if (!step) return;
  const body = {};
  if (e.target.matches("[data-status]")) body.status = e.target.value;
  if (e.target.matches("[data-notes]")) body.notes = e.target.value;
  if (e.target.matches("[data-prompt]")) body.prompt = e.target.value;
  await api(`/api/projects/${state.project}/workflow/${step.dataset.id}`, { method: "PATCH", body });
  if (body.status) { state.data.workflow[step.dataset.id] = { ...(state.data.workflow[step.dataset.id] || {}), ...body }; renderWorkflow(); loadProjects(); }
});
$("#workflow").addEventListener("click", async e => {
  const step = e.target.closest(".step"); if (!step) return;
  if (e.target.dataset.go) setTab(e.target.dataset.go);
  if (e.target.dataset.log) { setTab("journal"); showJob(e.target.dataset.log); }
  if (e.target.matches("[data-claude]")) {
    const prompt = $("[data-prompt]", step).value;
    if (!state.sector && !confirm("Aucun secteur sélectionné : lancer quand même ?")) return;
    $("#agentPrompt").value = prompt;
    await runClaude(prompt, step.dataset.id);
  }
});

// ------------------------------------------------------------ agent
async function runClaude(prompt, stepId) {
  try {
    const j = await api(`/api/projects/${state.project}/claude`, { method: "POST",
      body: { prompt, sector: $("#agentSector").value || state.sector, resume: $("#agentResume").checked, stepId } });
    setTab("agent");
    $("#agentLog").textContent = "";
    state.data.jobs.unshift(j);
    follow(j.id, $("#agentLog"));
  } catch (err) { alert(err.message); }
}
$("#agentRun").onclick = () => { const p = $("#agentPrompt").value.trim(); if (p) runClaude(p); };

// ------------------------------------------------------------ journal + suivi temps réel
function renderJobs() {
  $("#jobs").innerHTML = state.data.jobs.map(j => `<div class="job" data-id="${j.id}"><div class="st ${j.status}">${j.status}</div>${esc(j.label)}<div class="muted">${new Date(j.started).toLocaleString("fr-FR")}</div></div>`).join("") || `<p class="muted">Aucune tâche.</p>`;
}
$("#jobs").addEventListener("click", e => { const j = e.target.closest(".job"); if (j) showJob(j.dataset.id); });
async function showJob(id) {
  $$(".job").forEach(x => x.classList.toggle("on", x.dataset.id === id));
  const j = await api(`/api/jobs/${id}`);
  $("#jobLog").textContent = (j.log || []).filter(l => !l.startsWith("@@")).join("\n");
  if (j.status === "running") follow(id, $("#jobLog"), true);
}
function follow(id, pre, append = false) {
  const key = id + (pre ? pre.id : "");
  if (state.streams.has(key)) return;
  const es = new EventSource(`/api/jobs/${id}/stream`);
  state.streams.set(key, es);
  let skip = append ? Infinity : 0, seen = 0;
  if (append) skip = pre.textContent.split("\n").length;
  es.onmessage = ev => {
    const l = ev.data;
    const job = state.data?.jobs.find(j => j.id === id);
    if (l.startsWith("@@PROGRESS")) {
      const [, p, ...st] = l.split(" ");
      if (job) { job.progress = +p; job.step = st.join(" "); if (state.tab === "secteur") renderSectorJob(); }
      return;
    }
    if (l.startsWith("@@END")) {
      es.close(); state.streams.delete(key);
      if (job) job.status = l.split(" ")[1];
      refresh();
      return;
    }
    seen++;
    if (job) { job.logTail = [...(job.logTail || []), l].slice(-3); if (state.tab === "secteur") renderSectorJob(); }
    if (pre && seen > (append ? skip : 0)) { pre.textContent += l + "\n"; pre.scrollTop = pre.scrollHeight; }
  };
  es.onerror = () => { es.close(); state.streams.delete(key); };
}

// ------------------------------------------------------------ démarrage
setTab(state.tab);
loadEnv();
loadProjects();
