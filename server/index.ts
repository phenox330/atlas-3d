import { Hono } from "hono";
import { serveStatic } from "hono/bun";
import { streamSSE } from "hono/streaming";
import { existsSync, mkdirSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { join, resolve, extname } from "node:path";
import { spawn, type ChildProcess } from "node:child_process";
import { WORKFLOW, PRESETS } from "./workflow";

const ROOT = resolve(import.meta.dir, "..");
const PROJECTS = join(ROOT, "projects");
const PIPELINE = join(ROOT, "pipeline");
const BLENDER = "/Applications/Blender.app/Contents/MacOS/Blender";
const PORT = Number(process.env.ATLAS_PORT ?? 4896);
mkdirSync(PROJECTS, { recursive: true });

const slugify = (s: string) =>
  s.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 48) || "secteur";
const readJSON = (p: string, fallback: any = null) => { try { return JSON.parse(readFileSync(p, "utf8")); } catch { return fallback; } };
const writeJSON = (p: string, d: any) => writeFileSync(p, JSON.stringify(d, null, 2));
const safe = (s: string) => /^[a-z0-9-]+$/.test(s);

// ------------------------------------------------------------------ jobs
type Job = {
  id: string; project: string; sector?: string; action: string; label: string;
  status: "running" | "ok" | "error" | "cancelled"; progress: number; step: string;
  started: string; ended?: string; log: string[]; proc?: ChildProcess; listeners: Set<(l: string) => void>;
};
const jobs = new Map<string, Job>();
let jobSeq = 0;

function jobView(j: Job) {
  const { proc, listeners, log, ...rest } = j;
  return { ...rest, logTail: log.filter(l => !l.startsWith("@@")).slice(-3) };
}

function persistJob(j: Job) {
  const dir = join(PROJECTS, j.project, "jobs");
  mkdirSync(dir, { recursive: true });
  writeFileSync(join(dir, `${j.id}.log`), j.log.join("\n"));
  const idxP = join(dir, "index.json");
  const idx = readJSON(idxP, []).filter((x: any) => x.id !== j.id);
  idx.unshift(jobView(j));
  writeJSON(idxP, idx.slice(0, 200));
}

function runJob(opts: {
  project: string; sector?: string; action: string; label: string; cmd: string; args: string[]; cwd: string;
  parse?: (line: string, j: Job) => string | null; onDone?: (j: Job) => void;
}): Job {
  const id = `${Date.now().toString(36)}-${++jobSeq}`;
  const j: Job = { id, project: opts.project, sector: opts.sector, action: opts.action, label: opts.label,
    status: "running", progress: 0, step: "démarrage", started: new Date().toISOString(), log: [], listeners: new Set() };
  jobs.set(id, j);
  const push = (line: string) => {
    j.log.push(line);
    for (const l of j.listeners) l(line);
  };
  push(`$ ${opts.cmd} ${opts.args.map(a => (a.length > 120 ? a.slice(0, 117) + "…" : a)).join(" ")}`);
  const proc = spawn(opts.cmd, opts.args, { cwd: opts.cwd, env: { ...process.env, PYTHONUNBUFFERED: "1" }, stdio: ["ignore", "pipe", "pipe"] });
  j.proc = proc;
  let buf = "";
  const onData = (d: Buffer) => {
    buf += d.toString();
    const lines = buf.split("\n");
    buf = lines.pop() ?? "";
    for (const raw of lines) {
      if (!raw.trim()) continue;
      const m = raw.match(/^@@PROGRESS ([\d.]+) (.*)$/);
      if (m) { j.progress = Number(m[1]); j.step = m[2]; for (const l of j.listeners) l(`@@PROGRESS ${m[1]} ${m[2]}`); continue; }
      const out = opts.parse ? opts.parse(raw, j) : raw;
      if (out) push(out);
    }
  };
  proc.stdout.on("data", onData);
  proc.stderr.on("data", onData);
  proc.on("close", (code) => {
    if (buf.trim()) push(buf);
    if (j.status === "running") j.status = code === 0 ? "ok" : "error";
    if (j.status === "ok") j.progress = 1;
    j.ended = new Date().toISOString();
    j.step = j.status === "ok" ? "terminé" : j.status;
    push(`@@END ${j.status} (code ${code})`);
    opts.onDone?.(j);
    persistJob(j);
  });
  proc.on("error", (e) => { push(`ERREUR : ${e.message}`); j.status = "error"; });
  persistJob(j);
  return j;
}

// ------------------------------------------------------------------ projets / secteurs
function sectorStatus(dir: string) {
  const raw = join(dir, "raw"), b = join(dir, "build");
  const rd = join(b, "hq", "renders");
  return {
    hq: existsSync(join(b, "hq", "arcachon_hq.blend")),
    facades: existsSync(join(b, "hq", "facades.json")),
    renders: existsSync(rd) ? readdirSync(rd).filter(f => f.endsWith(".png")).sort() : [],
    fetched: existsSync(join(raw, "mnt.npy")),
    built: existsSync(join(b, "sector.glb")),
    blender: existsSync(join(b, "sector.blend")),
    unreal: existsSync(join(b, "unreal", "README_UNREAL.md")),
    preview: existsSync(join(b, "preview_blender.png")),
  };
}

function listSectors(p: string) {
  const dir = join(PROJECTS, p, "sectors");
  if (!existsSync(dir)) return [];
  return readdirSync(dir).filter(s => existsSync(join(dir, s, "sector.json"))).map(s => {
    const sd = join(dir, s);
    return { slug: s, ...readJSON(join(sd, "sector.json")), status: sectorStatus(sd),
      manifest: readJSON(join(sd, "build", "manifest.json")), meta: readJSON(join(sd, "raw", "meta.json")) };
  });
}

function loadProject(p: string) {
  const pj = readJSON(join(PROJECTS, p, "project.json"));
  if (!pj) return null;
  return { ...pj, sectors: listSectors(p) };
}

// ------------------------------------------------------------------ env
function envCheck() {
  const which = (bin: string) => {
    try { return Bun.spawnSync(["which", bin]).stdout.toString().trim() || null; } catch { return null; }
  };
  const ueRoot = "/Users/Shared/Epic Games";
  const ue = existsSync(ueRoot) ? readdirSync(ueRoot).filter(d => d.startsWith("UE_")).sort() : [];
  let disk = "";
  try { disk = Bun.spawnSync(["/bin/df", "-h", ROOT]).stdout.toString().split("\n")[1]?.split(/\s+/)[3] ?? ""; } catch {}
  const chip = Bun.spawnSync(["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"]).stdout.toString().trim();
  const mem = Math.round(Number(Bun.spawnSync(["/usr/sbin/sysctl", "-n", "hw.memsize"]).stdout.toString()) / 2 ** 30);
  return {
    chip, mem_gb: mem, disk_free: disk,
    tools: [
      { name: "uv (pipeline Python)", ok: !!which("uv"), detail: which("uv") },
      { name: "Blender", ok: existsSync(BLENDER), detail: existsSync(BLENDER) ? Bun.spawnSync([BLENDER, "--version"]).stdout.toString().split("\n")[0] : "Installer depuis blender.org" },
      { name: "Unreal Engine", ok: ue.length > 0, detail: ue.length ? ue.join(", ") + " · rendus Lumen via Movie Render Queue" : (existsSync("/Applications/Epic Games Launcher.app") ? "Epic Games Launcher installé : connecte-toi puis Unreal Engine > Installer 5.8 (~60 Go). Il faut aussi Xcode + Metal Toolchain." : "Non installé : brew install --cask epic-games") + " Projet prêt dans unreal/." },
      { name: "Claude Code", ok: !!which("claude"), detail: which("claude") },
      { name: "IA photo → 3D locale", ok: false, detail: "Pas branchée : TRELLIS.2 exige un GPU NVIDIA 24 Go. Piste Mac : Hunyuan3D-2mini / SF3D sur MPS, à tester." },
    ],
  };
}

// ------------------------------------------------------------------ app
const app = new Hono();

app.get("/api/env", c => c.json(envCheck()));
app.get("/api/workflow", c => c.json(WORKFLOW));

app.get("/api/projects", c => {
  const ps = readdirSync(PROJECTS).filter(p => existsSync(join(PROJECTS, p, "project.json")))
    .map(p => { const pr = loadProject(p)!; return { slug: p, name: pr.name, sectors: pr.sectors.length,
      built: pr.sectors.filter((s: any) => s.status.built).length,
      done: Object.values(pr.workflow ?? {}).filter((w: any) => w.status === "fait").length, total: WORKFLOW.length }; });
  return c.json(ps);
});

app.post("/api/projects", async c => {
  const { name, preset } = await c.req.json();
  if (!name) return c.json({ error: "nom requis" }, 400);
  const slug = slugify(name);
  const dir = join(PROJECTS, slug);
  if (existsSync(join(dir, "project.json"))) return c.json({ error: "existe déjà", slug }, 409);
  mkdirSync(join(dir, "sectors"), { recursive: true });
  const presets = PRESETS[preset] ?? [];
  writeJSON(join(dir, "project.json"), { name, slug, preset: preset ?? null, created: new Date().toISOString(),
    center: presets[0]?.center ?? [-1.17, 44.66], workflow: {} });
  return c.json({ slug });
});

app.get("/api/projects/:p", c => {
  const p = c.req.param("p");
  if (!safe(p)) return c.notFound();
  const pr = loadProject(p);
  if (!pr) return c.notFound();
  const jobsIdx = readJSON(join(PROJECTS, p, "jobs", "index.json"), []);
  const live = [...jobs.values()].filter(j => j.project === p).map(jobView);
  const merged = [...live, ...jobsIdx.filter((x: any) => !live.find(l => l.id === x.id))].slice(0, 40);
  return c.json({ ...pr, workflowDef: WORKFLOW, presets: PRESETS[pr.preset] ?? Object.values(PRESETS).flat(), jobs: merged });
});

app.patch("/api/projects/:p/workflow/:step", async c => {
  const { p, step } = c.req.param();
  const pj = readJSON(join(PROJECTS, p, "project.json"));
  if (!pj || !WORKFLOW.find(w => w.id === step)) return c.notFound();
  pj.workflow ??= {};
  pj.workflow[step] = { ...(pj.workflow[step] ?? {}), ...(await c.req.json()), updated: new Date().toISOString() };
  writeJSON(join(PROJECTS, p, "project.json"), pj);
  return c.json(pj.workflow[step]);
});

app.post("/api/projects/:p/sectors", async c => {
  const p = c.req.param("p");
  if (!safe(p) || !existsSync(join(PROJECTS, p, "project.json"))) return c.notFound();
  const b = await c.req.json();
  const size = Math.min(Math.max(Number(b.size_m) || 500, 100), 5000);
  if (!Array.isArray(b.center) || b.center.length !== 2) return c.json({ error: "center [lon, lat] requis" }, 400);
  let slug = slugify(b.name || "secteur");
  const base = join(PROJECTS, p, "sectors");
  let i = 2; while (existsSync(join(base, slug))) slug = `${slugify(b.name)}-${i++}`;
  mkdirSync(join(base, slug), { recursive: true });
  // résolution auto : rester sous ~4k px de MNT et 8k d'ortho
  const mnt = b.mnt_res_m ?? (size <= 1000 ? 1 : size <= 2500 ? 2 : 5);
  const ortho = b.ortho_res_m ?? Math.max(0.2, Math.round((size / 8000) * 100) / 100);
  writeJSON(join(base, slug, "sector.json"), { name: b.name || slug, center: b.center.map(Number), size_m: size,
    mnt_res_m: mnt, ortho_res_m: ortho, water_level_m: b.water_level_m ?? 0 });
  return c.json({ slug });
});

app.delete("/api/projects/:p/sectors/:s", c => {
  const { p, s } = c.req.param();
  if (!safe(p) || !safe(s)) return c.notFound();
  const dir = join(PROJECTS, p, "sectors", s);
  if (!existsSync(join(dir, "sector.json"))) return c.notFound();
  if ([...jobs.values()].some(j => j.project === p && j.sector === s && j.status === "running"))
    return c.json({ error: "tâche en cours sur ce secteur" }, 409);
  rmSync(dir, { recursive: true, force: true });
  return c.json({ ok: true });
});

app.get("/api/projects/:p/sectors/:s/photos", c => {
  const { p, s } = c.req.param();
  if (!safe(p) || !safe(s)) return c.notFound();
  const d = join(PROJECTS, p, "sectors", s);
  return c.json({ photos: readJSON(join(d, "raw", "panoramax.json"), []), selection: readJSON(join(d, "photos-selection.json"), []) });
});

app.put("/api/projects/:p/sectors/:s/photos", async c => {
  const { p, s } = c.req.param();
  if (!safe(p) || !safe(s)) return c.notFound();
  const sel = await c.req.json();
  writeJSON(join(PROJECTS, p, "sectors", s, "photos-selection.json"), sel);
  return c.json({ ok: true, count: sel.length });
});

app.post("/api/projects/:p/sectors/:s/run", async c => {
  const { p, s } = c.req.param();
  if (!safe(p) || !safe(s)) return c.notFound();
  const sd = join(PROJECTS, p, "sectors", s);
  if (!existsSync(join(sd, "sector.json"))) return c.notFound();
  if ([...jobs.values()].some(j => j.project === p && j.sector === s && j.status === "running"))
    return c.json({ error: "une tâche tourne déjà sur ce secteur" }, 409);
  const { action } = await c.req.json();
  const name = readJSON(join(sd, "sector.json")).name;
  let job: Job;
  switch (action) {
    case "fetch": case "build": case "all":
      job = runJob({ project: p, sector: s, action, label: `${({ fetch: "Données", build: "Construction", all: "Données + construction" } as any)[action]} — ${name}`,
        cmd: "uv", args: ["run", "atlas.py", action, sd], cwd: PIPELINE });
      break;
    case "refetch":
      job = runJob({ project: p, sector: s, action, label: `Retéléchargement complet — ${name}`,
        cmd: "uv", args: ["run", "atlas.py", "all", sd, "--force"], cwd: PIPELINE });
      break;
    case "blender":
      if (!existsSync(join(sd, "build", "sector.glb"))) return c.json({ error: "construis le secteur d'abord" }, 400);
      job = runJob({ project: p, sector: s, action, label: `Blender (scène + rendu) — ${name}`,
        cmd: BLENDER, args: ["-b", "-P", join(PIPELINE, "blender_sector.py"), "--", join(sd, "build")], cwd: PIPELINE,
        parse: l => (/^(Fra:|Saved:|ATLAS|Error|Traceback|  File|\w+Error)/.test(l) ? l : null) });
      break;
    case "unreal":
      if (!existsSync(join(sd, "build", "manifest.json"))) return c.json({ error: "construis le secteur d'abord" }, 400);
      {
        const hqBlend = join(sd, "build", "hq", "arcachon_hq.blend");
        const chain = existsSync(hqBlend)
          ? `uv run unreal_export.py "${sd}" && "${BLENDER}" -b "${hqBlend}" -P export_ue.py -- "${join(sd, "build", "hq", "unreal")}" && uv run ue_project.py "${sd}"`
          : `uv run unreal_export.py "${sd}"`;
        job = runJob({ project: p, sector: s, action, label: `Paquet Unreal — ${name}`, cmd: "bash", args: ["-c", chain], cwd: PIPELINE,
          parse: l => (/^(ATLAS|Error|Traceback|  File|\w+Error|Lance)/.test(l) ? l : null) });
      }
      break;
    case "ue-render":
      if (!existsSync(join(sd, "build", "hq", "unreal", "arcachon_geo.glb"))) return c.json({ error: "lance d'abord Rendu HQ puis Paquet Unreal" }, 400);
      job = runJob({ project: p, sector: s, action, label: `Rendu Unreal (Lumen) — ${name}`,
        cmd: "bash", args: [join(PIPELINE, "ue_render.sh"), sd, "2560x1440"], cwd: PIPELINE,
        parse: l => (/^(\[Atlas\]|@@|ATLAS|Error|Traceback)/.test(l) ? l : null) });
      break;
    case "hq":
      if (!existsSync(join(sd, "build", "sector.glb"))) return c.json({ error: "construis le secteur d'abord" }, 400);
      job = runJob({ project: p, sector: s, action, label: `Rendu haute qualité (Cycles) — ${name}`,
        cmd: "bash", args: ["-c", `uv run hq_prepare.py "${sd}" && "${BLENDER}" -b -P blender_hq.py -- "${sd}" --samples=320 --res=2560x1440`], cwd: PIPELINE,
        parse: l => (/^(\[|\[hq|@@|ATLAS|Error|Traceback|  File|\w+Error)/.test(l) ? l : null) });
      break;
    case "facades":
      if (!existsSync(join(sd, "raw", "batiments.geojson"))) return c.json({ error: "télécharge les données d'abord" }, 400);
      job = runJob({ project: p, sector: s, action, label: `Façades depuis les photos de rue (Panoramax + vision) — ${name}`,
        cmd: "bash", args: ["-c", `uv run python -c "import sys;sys.path.insert(0,'.');from atlas import Sector,fetch_panoramax;from pathlib import Path;fetch_panoramax(Sector(Path(sys.argv[1])))" "${sd}" && uv run facades.py "${sd}" --max 250 --workers 6`], cwd: PIPELINE,
        parse: l => (/^(\[|@@|Error|Traceback|  File|\w+Error)/.test(l) ? l : null) });
      break;
    case "open-blender":
      spawn("open", ["-a", "Blender", join(sd, "build", "sector.blend")], { detached: true });
      return c.json({ ok: true });
    case "open-folder":
      spawn("open", [sd], { detached: true });
      return c.json({ ok: true });
    default:
      return c.json({ error: "action inconnue" }, 400);
  }
  return c.json(jobView(job));
});

// Agent Claude Code : exécute une consigne dans le dossier Atlas avec le contexte du projet/secteur.
app.post("/api/projects/:p/claude", async c => {
  const p = c.req.param("p");
  if (!safe(p)) return c.notFound();
  const { prompt, sector, resume, stepId } = await c.req.json();
  if (!prompt?.trim()) return c.json({ error: "consigne vide" }, 400);
  const pj = readJSON(join(PROJECTS, p, "project.json"));
  const ctx = [
    `Contexte Atlas : projet « ${pj.name} » (dossier projects/${p}).`,
    sector ? `Secteur courant : projects/${p}/sectors/${sector} (sector.json, raw/, build/manifest.json).` : "",
    "Lis CLAUDE.md à la racine avant d'agir. Teste ce que tu écris (pipeline : cd pipeline && uv run …, Blender headless : /Applications/Blender.app/Contents/MacOS/Blender -b). Réponds en français, termine par un résumé court de ce qui a changé.",
  ].filter(Boolean).join("\n");
  const args = ["-p", `${ctx}\n\nConsigne :\n${prompt}`, "--output-format", "stream-json", "--verbose",
    "--permission-mode", "bypassPermissions"];
  if (resume && pj.claudeSession) args.push("--resume", pj.claudeSession);
  const job = runJob({
    project: p, sector, action: "claude", label: `Claude — ${prompt.slice(0, 60)}${prompt.length > 60 ? "…" : ""}`,
    cmd: "claude", args, cwd: ROOT,
    parse: (line, j) => {
      let ev: any; try { ev = JSON.parse(line); } catch { return line; }
      if (ev.type === "system" && ev.session_id) { (j as any).session = ev.session_id; return null; }
      if (ev.type === "assistant") {
        const out: string[] = [];
        for (const b of ev.message?.content ?? []) {
          if (b.type === "text" && b.text.trim()) out.push(b.text.trim());
          if (b.type === "tool_use") {
            const i = b.input ?? {};
            out.push(`→ ${b.name} ${i.command ?? i.file_path ?? i.pattern ?? i.description ?? ""}`.slice(0, 200));
          }
        }
        j.step = "travail en cours";
        return out.join("\n") || null;
      }
      if (ev.type === "result") {
        (j as any).session = ev.session_id ?? (j as any).session;
        if (ev.is_error) j.status = "error";
        return `── terminé en ${Math.round((ev.duration_ms ?? 0) / 1000)} s${ev.total_cost_usd ? ` · ${ev.total_cost_usd.toFixed(2)} $ équiv.` : ""}`;
      }
      return null;
    },
    onDone: j => {
      const cur = readJSON(join(PROJECTS, p, "project.json"));
      if ((j as any).session) cur.claudeSession = (j as any).session;
      if (stepId && cur.workflow) {
        cur.workflow[stepId] = { ...(cur.workflow[stepId] ?? {}), lastClaudeJob: j.id, status: cur.workflow[stepId]?.status ?? "en cours" };
      }
      writeJSON(join(PROJECTS, p, "project.json"), cur);
    },
  });
  return c.json(jobView(job));
});

app.get("/api/jobs/:id", c => {
  const id = c.req.param("id");
  const j = jobs.get(id);
  if (j) return c.json({ ...jobView(j), log: j.log });
  // job terminé lors d'une session précédente
  for (const p of readdirSync(PROJECTS)) {
    const lp = join(PROJECTS, p, "jobs", `${id}.log`);
    if (existsSync(lp)) {
      const meta = readJSON(join(PROJECTS, p, "jobs", "index.json"), []).find((x: any) => x.id === id);
      return c.json({ ...meta, log: readFileSync(lp, "utf8").split("\n") });
    }
  }
  return c.notFound();
});

app.get("/api/jobs/:id/stream", c => {
  const j = jobs.get(c.req.param("id"));
  if (!j) return c.notFound();
  return streamSSE(c, async stream => {
    for (const l of j.log) await stream.writeSSE({ data: l });
    if (j.status !== "running") return;
    await new Promise<void>(res => {
      const fn = (l: string) => { stream.writeSSE({ data: l }); if (l.startsWith("@@END")) { j.listeners.delete(fn); res(); } };
      j.listeners.add(fn);
      stream.onAbort(() => { j.listeners.delete(fn); res(); });
    });
  });
});

app.post("/api/jobs/:id/cancel", c => {
  const j = jobs.get(c.req.param("id"));
  if (!j || j.status !== "running") return c.json({ error: "pas en cours" }, 400);
  j.status = "cancelled";
  j.proc?.kill("SIGTERM");
  return c.json({ ok: true });
});

// fichiers d'un secteur (glb, png, json)
const MIME: Record<string, string> = { ".glb": "model/gltf-binary", ".png": "image/png", ".jpg": "image/jpeg", ".json": "application/json", ".md": "text/markdown; charset=utf-8", ".py": "text/plain; charset=utf-8" };
app.get("/files/:p/:s/*", c => {
  const { p, s } = c.req.param();
  if (!safe(p) || !safe(s)) return c.notFound();
  const rel = decodeURIComponent(c.req.path.split(`/files/${p}/${s}/`)[1] ?? "");
  const base = join(PROJECTS, p, "sectors", s);
  const f = resolve(base, rel);
  if (!f.startsWith(base) || !existsSync(f) || statSync(f).isDirectory()) return c.notFound();
  return new Response(Bun.file(f), { headers: { "content-type": MIME[extname(f)] ?? "application/octet-stream", "cache-control": "no-cache" } });
});

app.use("/vendor/three/*", serveStatic({ root: "./node_modules/three", rewriteRequestPath: p => p.replace("/vendor/three", "") }));
app.use("/vendor/leaflet/*", serveStatic({ root: "./node_modules/leaflet/dist", rewriteRequestPath: p => p.replace("/vendor/leaflet", "") }));
app.use("/*", serveStatic({ root: "./web" }));
app.get("*", serveStatic({ path: "./web/index.html" }));

console.log(`Atlas sur http://127.0.0.1:${PORT}`);
export default { port: PORT, hostname: "127.0.0.1", fetch: app.fetch, idleTimeout: 0 };
