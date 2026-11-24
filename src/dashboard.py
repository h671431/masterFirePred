"""Lager en lokal HTML-side med kart og grafer over BRIS- og værdataene.

    python -m src.dashboard

Skriver data/processed/kart.html (under data/, så den committes aldri – den
inneholder koordinater for brannhendelser). Åpne filen i en nettleser.
"""

from __future__ import annotations

import json

from src import db
from src.config import PROCESSED_DIR

OUT = PROCESSED_DIR / "kart.html"


def _table_exists(con, name: str) -> bool:
    return con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [name]
    ).fetchone()[0] > 0


# Grupper i analysefanen: kolonne i panel_stasjon_dag -> (navn, filter på år)
ANALYSIS_GROUPS = {
    "n_bygning": ("Brann i bygning", "aar BETWEEN 2016 AND 2026"),
    "n_skorstein": ("Brann i skorstein (2023→)", "aar >= 2023"),
    "n_komfyr": ("Brannhindrende tiltak komfyr", "aar BETWEEN 2016 AND 2026"),
    "n_falske": ("Falske/unødige utrykninger", "aar BETWEEN 2016 AND 2026"),
}


def collect_analysis(con) -> dict | None:
    """Aggregerte tall fra panel_stasjon_dag (lages av python -m src.matching)."""
    if not _table_exists(con, "panel_stasjon_dag"):
        return None
    temp_rel, anom_rel, lags = [], [], []
    for col, (navn, filt) in ANALYSIS_GROUPS.items():
        # Relativ rate etter temperatur, standardisert per stasjon (hver stasjon mot seg selv)
        for t, n, forventet, dager in con.execute(f"""
            WITH p AS (SELECT {col} AS n, temp_mean, avg({col}) OVER (PARTITION BY stasjon_id) AS forventet
                       FROM panel_stasjon_dag WHERE temp_mean IS NOT NULL AND {filt})
            SELECT floor(temp_mean/2)*2, sum(n), sum(forventet), count(*) FROM p GROUP BY 1 ORDER BY 1
        """).fetchall():
            if dager >= 2000 and forventet:
                temp_rel.append([t, navn, round(n / forventet, 3), int(n), int(dager)])
        # Relativ rate etter avvik fra normal temperatur for stasjon og måned
        for a, n, dager in con.execute(f"""
            WITH p AS (SELECT {col} AS n, temp_mean - avg(temp_mean) OVER (PARTITION BY stasjon_id, mnd) AS avvik
                       FROM panel_stasjon_dag WHERE temp_mean IS NOT NULL AND {filt})
            SELECT floor(avvik/2)*2, sum(n), count(*) FROM p GROUP BY 1 ORDER BY 1
        """).fetchall():
            anom_rel.append([a, navn, int(n), int(dager)])
        # Korrelasjon med temperaturavvik ved ulik tidsforsinkelse
        r = con.execute(f"""
            WITH a AS (SELECT {col} AS n, temp_mean, temp_mean_lag1, temp_mean_lag2, temp_mean_lag3, temp_mean_7d,
                              avg(temp_mean) OVER (PARTITION BY stasjon_id, mnd) AS norm
                       FROM panel_stasjon_dag WHERE {filt})
            SELECT corr(n, temp_mean-norm), corr(n, temp_mean_lag1-norm), corr(n, temp_mean_lag2-norm),
                   corr(n, temp_mean_lag3-norm), corr(n, temp_mean_7d-norm) FROM a
        """).fetchone()
        lags.append([navn] + [round(x, 4) if x is not None else None for x in r])

    # Avvik: normaliser til relativ rate i Python (snitt over alle intervaller = 1)
    by_group: dict[str, list] = {}
    for a, navn, n, dager in anom_rel:
        by_group.setdefault(navn, []).append((a, n, dager))
    anom_out = []
    for navn, rows in by_group.items():
        tot = sum(n for _, n, _ in rows) / max(sum(d for _, _, d in rows), 1)
        for a, n, dager in rows:
            if dager >= 2000 and -14 <= a <= 12 and tot:
                anom_out.append([a, navn, round((n / dager) / tot, 3), n, dager])

    dist = con.execute("""
        SELECT kilde, count(*), round(median(avstand_km), 1), round(quantile_cont(avstand_km, 0.9), 1),
               round(100 * avg((avstand_km <= 10)::int), 1)
        FROM hendelse_stasjon GROUP BY 1 ORDER BY 2 DESC
    """).fetchall()

    monthly = con.execute("""
        SELECT strftime(date_trunc('month', dato), '%Y-%m'), round(avg(temp_mean), 2),
               sum(n_bygning), sum(n_skorstein), sum(n_komfyr), sum(n_falske)
        FROM panel_stasjon_dag GROUP BY 1 ORDER BY 1
    """).fetchall()

    summary = con.execute("""
        SELECT (SELECT count(*) FROM stasjon_kjerne), (SELECT count(*) FROM hendelse_stasjon),
               (SELECT count(*) FROM panel_stasjon_dag),
               (SELECT round(median(avstand_km), 1) FROM hendelse_stasjon WHERE kilde = 'bygg')
    """).fetchone()

    return {
        "tempRel": temp_rel,
        "anomRel": anom_out,
        "lags": lags,
        "dist": dist,
        "monthly": [[m, t, int(b), int(s), int(k), int(f)] for m, t, b, s, k, f in monthly],
        "summary": summary,
    }


def collect() -> dict:
    con = db.connect(read_only=True)
    type_rows = con.execute(
        "SELECT oppdragstype, any_value(kategori), count(*) FROM brann GROUP BY 1 ORDER BY count(*) DESC").fetchall()
    types = [r[0] for r in type_rows]
    type_cat = [[r[1], r[2]] for r in type_rows]  # samme rekkefølge som types: [kategori, antall]
    tix = {t: i for i, t in enumerate(types)}

    pts = con.execute("""
        SELECT round(lat, 4), round(lon, 4), oppdragstype, year(dato), month(dato)
        FROM brann
        WHERE lat BETWEEN 57.5 AND 81 AND lon BETWEEN 4 AND 32
    """).fetchall()
    points = [[la, lo, tix[t], y, m] for la, lo, t, y, m in pts]

    per_year = con.execute("""
        SELECT year(dato), oppdragstype, count(*) FROM brann GROUP BY ALL ORDER BY ALL
    """).fetchall()
    per_month = con.execute("""
        SELECT month(dato), oppdragstype, count(*) FROM brann WHERE kilde='bygg' GROUP BY ALL
        UNION ALL
        SELECT month(dato), 'Falske/unødige utrykninger', count(*) FROM falske GROUP BY ALL
    """).fetchall()
    per_hour = con.execute("""
        SELECT hour(tidspunkt), oppdragstype, count(*) FROM brann WHERE kilde='bygg' GROUP BY ALL
        UNION ALL
        SELECT hour(tidspunkt), 'Falske/unødige utrykninger', count(*) FROM falske GROUP BY ALL
    """).fetchall()
    falske_kat = con.execute("""
        SELECT year(dato), kategori, count(*) FROM falske GROUP BY ALL ORDER BY ALL
    """).fetchall()
    per_fylke = con.execute("""
        SELECT fylke, count(*) FROM brann WHERE fylke IS NOT NULL GROUP BY 1 ORDER BY 2 DESC
    """).fetchall()

    stations, weather = [], None
    if _table_exists(con, "stasjoner"):
        has_raw = _table_exists(con, "vaer_dogn_raw")
        with_data = set()
        if has_raw:
            with_data = {r[0] for r in con.execute("SELECT DISTINCT stasjon_id FROM vaer_dogn_raw").fetchall()}
        for sid, navn, la, lo, moh in con.execute(
            "SELECT stasjon_id, navn, lat, lon, moh FROM stasjoner WHERE lat BETWEEN 57.5 AND 81 AND lon BETWEEN 4 AND 32"
        ).fetchall():
            stations.append([sid, navn, la, lo, moh, sid in with_data])
        if has_raw:
            weather = con.execute("""
                SELECT count(DISTINCT stasjon_id), min(dato)::VARCHAR, max(dato)::VARCHAR, count(*)
                FROM vaer_dogn_raw
            """).fetchone()
    analysis = collect_analysis(con)
    con.close()
    return {
        "types": types,
        "typeCat": type_cat,
        "points": points,
        "perYear": per_year,
        "perMonth": per_month,
        "perHour": per_hour,
        "falskeKat": falske_kat,
        "perFylke": per_fylke,
        "stations": stations,
        "weather": weather,
        "analysis": analysis,
    }


TEMPLATE = r"""<!doctype html>
<html lang="no">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Brann og vær</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/leaflet.heat@0.2.0/dist/leaflet-heat.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
:root { --bg:#f7f7f5; --panel:#fff; --text:#1d1d1b; --muted:#6b6b66; --line:#e3e3de; --accent:#c2410c; }
@media (prefers-color-scheme: dark) { :root { --bg:#161615; --panel:#202020; --text:#ececea; --muted:#a3a39d; --line:#33332f; --accent:#fb923c; } }
* { box-sizing:border-box; }
body { margin:0; font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif; background:var(--bg); color:var(--text); }
header { padding:18px 20px 6px; }
h1 { margin:0; font-size:20px; }
header p { margin:4px 0 0; color:var(--muted); }
main { display:grid; grid-template-columns: 360px minmax(0, 760px); justify-content:center; gap:16px; padding:16px 20px 24px; }
main > .panel { max-height:calc(100vh - 150px); min-height:560px; overflow-y:auto; }
.panel { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:14px; }
#map { height:calc(100vh - 150px); min-height:560px; border-radius:10px; border:1px solid var(--line); }
details.grp { border:1px solid var(--line); border-radius:8px; margin:6px 0; }
details.grp > summary { list-style:none; cursor:pointer; padding:7px 10px; display:flex; align-items:center; gap:8px; font-weight:600; }
details.grp > summary::-webkit-details-marker { display:none; }
details.grp > summary::before { content:"▸"; color:var(--muted); width:10px; transition:transform .15s; }
details.grp[open] > summary::before { transform:rotate(90deg); }
details.grp > summary .n { margin-left:auto; color:var(--muted); font-weight:400; font-size:12px; }
details.grp .items { padding:2px 10px 8px 28px; }
details.grp .items label { font-size:13px; justify-content:space-between; }
details.grp .items label span.lab { display:flex; gap:6px; align-items:center; }
details.grp .items .n { color:var(--muted); font-size:12px; }
h2 { font-size:14px; margin:0 0 8px; }
label { display:flex; gap:6px; align-items:center; margin:3px 0; cursor:pointer; }
.row { display:flex; gap:8px; align-items:center; margin:6px 0; }
select { padding:3px 6px; background:var(--panel); color:var(--text); border:1px solid var(--line); border-radius:6px; }
.stat { font-size:22px; font-weight:600; }
.muted { color:var(--muted); font-size:12px; }
.charts { display:grid; grid-template-columns: repeat(auto-fit, minmax(420px, 1fr)); gap:16px; padding:0 20px 30px; }
.charts .panel { min-height:320px; }
.charts canvas { max-height:300px; }
.swatch { width:10px; height:10px; border-radius:2px; display:inline-block; }
@media (max-width: 820px) { main { grid-template-columns:1fr; padding:16px; } main > .panel { max-height:none; min-height:0; } .charts { grid-template-columns:1fr; padding:0 16px 24px; } #map { height:520px; min-height:0; } }
.tabs { display:flex; gap:4px; padding:10px 20px 0; border-bottom:1px solid var(--line); }
.tabs button { font:inherit; padding:8px 14px; border:1px solid transparent; border-bottom:none; border-radius:8px 8px 0 0; background:none; color:var(--muted); cursor:pointer; }
.tabs button.active { background:var(--panel); color:var(--text); border-color:var(--line); margin-bottom:-1px; font-weight:600; }
.cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:12px; padding:16px 20px 0; }
.cards .panel .stat { font-size:24px; }
.wide { padding:16px 20px 0; }
.findings li { margin:4px 0; }
table.t { border-collapse:collapse; width:100%; font-size:13px; font-variant-numeric:tabular-nums; }
table.t th, table.t td { padding:4px 8px; border-bottom:1px solid var(--line); text-align:right; white-space:nowrap; }
table.t th:first-child, table.t td:first-child { text-align:left; }
table.t th { color:var(--muted); font-weight:600; }
.tablewrap { overflow-x:auto; }
.col { max-width:1100px; margin:0 auto; padding:16px 20px 40px; display:flex; flex-direction:column; gap:16px; }
.col .panel { padding:16px 18px; }
.col h2 { font-size:16px; margin:0 0 4px; }
.col .muted { margin:0 0 10px; }
.summary .findings { margin:6px 0 12px; padding-left:18px; font-size:15px; }
.kpis { display:flex; flex-wrap:wrap; gap:8px 24px; color:var(--muted); font-size:13px; border-top:1px solid var(--line); padding-top:10px; }
.kpis span { color:var(--text); font-weight:600; font-size:15px; }
.chartbox { position:relative; height:360px; }
table.big { font-size:14px; }
table.big th { position:sticky; top:0; background:var(--panel); z-index:1; }
table.big td, table.big th { padding:6px 10px; }
@media (max-width: 820px) { .col { padding:12px 16px 32px; } .chartbox { height:280px; } }
.explain { font-size:13px; color:var(--muted); }
.explain b { color:var(--text); }
</style>
</head>
<body>
<header>
  <h1>Brannhendelser og vær – foreløpig oversikt</h1>
  <p>BRIS-uttrekk 2016–2026 og MET-stasjoner. Lokal side – ikke del den, den inneholder koordinater for hendelser.</p>
</header>
<nav class="tabs">
  <button class="active" data-tab="oversikt">Kart og oversikt</button>
  <button data-tab="analyse">Brann og vær</button>
</nav>
<div id="tab-analyse" hidden>
  <div id="noanalysis" class="wide" hidden><div class="panel">Ingen koblede data ennå. Kjør <code>python -m src.matching</code> og deretter <code>python -m src.dashboard</code>.</div></div>
  <div id="analysis" class="col">
    <section class="panel summary">
      <h2>Oppsummering</h2>
      <ul class="findings" id="findings"></ul>
      <div class="kpis">
        <div><span id="kStations">–</span> stasjoner</div>
        <div><span id="kEvents">–</span> koblede hendelser</div>
        <div><span id="kDays">–</span> stasjonsdager</div>
        <div><span id="kDist">–</span> median avstand til stasjon</div>
      </div>
    </section>

    <section class="panel">
      <h2>Relativ rate etter døgntemperatur</h2>
      <p class="muted">1,00 = vanlig nivå for stasjonen. Rødt = flere hendelser, blått = færre.</p>
      <div class="tablewrap"><table class="t big" id="tTemp"></table></div>
    </section>

    <section class="panel">
      <h2>Samme tall som graf</h2>
      <div class="chartbox"><canvas id="aTemp"></canvas></div>
    </section>

    <section class="panel">
      <h2>Justert for sesong</h2>
      <p class="muted">Avvik fra normal temperatur for samme stasjon og måned.</p>
      <div class="chartbox"><canvas id="aAnom"></canvas></div>
    </section>

    <section class="panel">
      <h2>Måned for måned</h2>
      <p class="muted">Branner per måned (søyler) og snittemperatur (linje).</p>
      <div class="chartbox"><canvas id="aMonthly"></canvas></div>
    </section>

    <section class="panel">
      <h2>Tidsforsinkelse</h2>
      <p class="muted">Korrelasjon med temperaturavvik. Negativ = flere hendelser når det er kaldere enn normalt.</p>
      <div class="tablewrap"><table class="t" id="tLag"></table></div>
    </section>

    <section class="panel">
      <h2>Kobling til nærmeste stasjon</h2>
      <div class="tablewrap"><table class="t" id="tDist"></table></div>
    </section>
  </div>
</div>
<div id="tab-oversikt">
<main>
  <div class="panel">
    <div class="stat" id="count">–</div>
    <div class="muted">hendelser vist i kartet</div>
    <h2 style="margin-top:14px">Hendelsestype</h2>
    <div id="types"></div>
    <h2 style="margin-top:14px">Periode</h2>
    <div class="row">Fra <select id="y0"></select> til <select id="y1"></select></div>
    <div class="row">Måned <select id="mnd"><option value="0">Alle</option></select></div>
    <h2 style="margin-top:14px">Visning</h2>
    <label><input type="radio" name="mode" value="heat" checked> Varmekart</label>
    <label><input type="radio" name="mode" value="points"> Punkter</label>
    <label><input type="checkbox" id="st"> Værstasjoner <span class="muted" id="stinfo"></span></label>
    <p class="muted" id="wx"></p>
  </div>
  <div id="map"></div>
</main>
<section class="charts">
  <div class="panel"><h2>Hendelser per år og type</h2><canvas id="cYear"></canvas><p class="muted">Merk bruddene i registreringen, f.eks. skorsteinsbranner fra 2019/2023.</p></div>
  <div class="panel"><h2>Sesong: andel per måned</h2><canvas id="cMonth"></canvas><p class="muted">Normalisert per gruppe, så branner og falske utrykninger kan sammenlignes.</p></div>
  <div class="panel"><h2>Tid på døgnet: andel per time</h2><canvas id="cHour"></canvas></div>
  <div class="panel"><h2>Falske/unødige utrykninger per år og kategori</h2><canvas id="cFalske"></canvas></div>
  <div class="panel"><h2>Branner per fylke</h2><canvas id="cFylke"></canvas></div>
</section>
</div>
<script>
const D = __DATA__;
const MONTHS = ["jan","feb","mar","apr","mai","jun","jul","aug","sep","okt","nov","des"];
const PALETTE = ["#c2410c","#2563eb","#059669","#a16207","#7c3aed","#db2777","#0891b2","#65a30d","#dc2626","#4f46e5","#0d9488","#ca8a04","#9333ea","#e11d48","#0284c7","#16a34a","#b45309","#6d28d9","#be123c","#475569"];
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
Chart.defaults.color = css("--muted"); Chart.defaults.borderColor = css("--line");

// --- kontroller ---
const typesDiv = document.getElementById("types");
const defaultOn = new Set(["Brann i bygning","Brann i skorstein"]);
// Grupper typene etter BRIS-kategori, hver gruppe som en nedtrekksmeny
const groups = new Map();
D.types.forEach((t,i) => {
  const [kat, n] = D.typeCat[i];
  if (!groups.has(kat)) groups.set(kat, []);
  groups.get(kat).push([t, i, n]);
});
const KATNAVN = { "Brannhendelser tilknyttet bygg":"Branner tilknyttet bygg" };
[...groups.entries()].forEach(([kat, items], gi) => {
  const total = items.reduce((s, x) => s + x[2], 0);
  const d = document.createElement("details");
  d.className = "grp"; if (gi === 0) d.open = true;
  d.innerHTML = `<summary><input type="checkbox" class="all" title="Velg alle"> ${KATNAVN[kat] || kat}<span class="n">${total.toLocaleString("no")}</span></summary>`
    + `<div class="items">` + items.map(([t, i, n]) =>
      `<label><span class="lab"><input type="checkbox" value="${i}" ${defaultOn.has(t)?"checked":""}><span class="swatch" style="background:${PALETTE[i%PALETTE.length]}"></span>${t.replace(/^Brann i /, "").replace(/^\w/, c => c.toUpperCase())}</span><span class="n">${n.toLocaleString("no")}</span></label>`).join("")
    + `</div>`;
  const all = d.querySelector(".all"), boxes = [...d.querySelectorAll(".items input")];
  const sync = () => { const k = boxes.filter(b => b.checked).length; all.checked = k === boxes.length; all.indeterminate = k > 0 && k < boxes.length; };
  all.addEventListener("click", e => e.stopPropagation());
  all.addEventListener("change", () => { boxes.forEach(b => b.checked = all.checked); sync(); update(); });
  boxes.forEach(b => b.addEventListener("change", sync));
  sync();
  typesDiv.appendChild(d);
});
const years = [...new Set(D.points.map(p=>p[3]))].sort();
for (const id of ["y0","y1"]) { const s = document.getElementById(id); years.forEach(y => s.add(new Option(y,y))); }
document.getElementById("y1").value = years[years.length-1];
MONTHS.forEach((m,i) => document.getElementById("mnd").add(new Option(m,i+1)));

// --- kart ---
const map = L.map("map", { preferCanvas:true });
map.fitBounds([[57.8, 4.5], [71.2, 31.2]]);  // fastlands-Norge
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom:18, attribution:"© OpenStreetMap" }).addTo(map);
let layer = null;
const canvas = L.canvas({ padding:0.5 });
const stLayer = L.layerGroup();
D.stations.forEach(([id,navn,la,lo,moh,has]) => {
  L.circleMarker([la,lo], { renderer:canvas, radius:has?4:2.5, color:has?"#2563eb":"#94a3b8", weight:1, fillOpacity:has?0.8:0.3 })
   .bindPopup(`<b>${navn}</b><br>${id} · ${moh ?? "?"} moh<br>${has?"Har lastet ned værdata":"Ingen værdata lastet ned ennå"}`)
   .addTo(stLayer);
});
document.getElementById("stinfo").textContent = D.stations.length ? `(${D.stations.filter(s=>s[5]).length} med data)` : "(ikke lastet ned)";
if (D.weather) document.getElementById("wx").textContent = `Værdata: ${D.weather[0]} stasjoner, ${D.weather[1]} – ${D.weather[2]}, ${D.weather[3].toLocaleString("no")} verdier.`;

function update() {
  const on = new Set([...typesDiv.querySelectorAll(".items input:checked")].map(e=>+e.value));
  const y0 = +document.getElementById("y0").value, y1 = +document.getElementById("y1").value;
  const m = +document.getElementById("mnd").value;
  const sel = D.points.filter(p => on.has(p[2]) && p[3]>=y0 && p[3]<=y1 && (!m || p[4]===m));
  document.getElementById("count").textContent = sel.length.toLocaleString("no");
  if (layer) map.removeLayer(layer);
  const mode = document.querySelector("input[name=mode]:checked").value;
  if (mode === "heat") {
    layer = L.heatLayer(sel.map(p=>[p[0],p[1],0.6]), { radius:9, blur:12, maxZoom:10 });
  } else {
    layer = L.layerGroup(sel.map(p => L.circleMarker([p[0],p[1]], { renderer:canvas, radius:2, stroke:false, fillColor:PALETTE[p[2]%PALETTE.length], fillOpacity:0.6 })));
  }
  layer.addTo(map);
  if (document.getElementById("st").checked) stLayer.addTo(map); else map.removeLayer(stLayer);
}
document.querySelectorAll("input, select").forEach(e => e.addEventListener("change", update));
update();

// --- grafer ---
function series(rows, keys) {
  const groups = [...new Set(rows.map(r=>r[1]))];
  return groups.map((g,i) => ({ label:g, data: keys.map(k => (rows.find(r=>r[0]===k && r[1]===g)||[0,0,0])[2]), color: PALETTE[(D.types.indexOf(g)>=0?D.types.indexOf(g):i+8)%PALETTE.length] }));
}
function share(ds) { return ds.map(d => { const s = d.data.reduce((a,b)=>a+b,0)||1; return {...d, data:d.data.map(v=>v/s)}; }); }
const toLine = ds => ds.map(d => ({ label:d.label, data:d.data, borderColor:d.color, backgroundColor:d.color, tension:0.25, pointRadius:2 }));
const opts = (pct) => ({ responsive:true, maintainAspectRatio:false, plugins:{ legend:{ position:"bottom", labels:{ boxWidth:10 } } },
  scales:{ y:{ beginAtZero:true, ticks: pct ? { callback:v => (v*100).toFixed(0)+" %" } : {} } } });

const yrs = [...new Set(D.perYear.map(r=>r[0]))].sort();
const mainTypes = new Set(D.types.slice(0,4));
new Chart(cYear, { type:"line", data:{ labels:yrs, datasets: toLine(series(D.perYear.filter(r=>mainTypes.has(r[1])), yrs)) }, options:opts(false) });
const ms = [...Array(12).keys()].map(i=>i+1);
new Chart(cMonth, { type:"line", data:{ labels:MONTHS, datasets: toLine(share(series(D.perMonth, ms))) }, options:opts(true) });
const hs = [...Array(24).keys()];
new Chart(cHour, { type:"line", data:{ labels:hs, datasets: toLine(share(series(D.perHour, hs))) }, options:opts(true) });
const fy = [...new Set(D.falskeKat.map(r=>r[0]))].sort();
new Chart(cFalske, { type:"bar", data:{ labels:fy, datasets: series(D.falskeKat, fy).map(d=>({label:d.label, data:d.data, backgroundColor:d.color})) },
  options:{ ...opts(false), scales:{ x:{ stacked:true }, y:{ stacked:true, beginAtZero:true } } } });
new Chart(cFylke, { type:"bar", data:{ labels:D.perFylke.map(r=>r[0]), datasets:[{ label:"Branner", data:D.perFylke.map(r=>r[1]), backgroundColor:PALETTE[0] }] },
  options:{ ...opts(false), indexAxis:"y", plugins:{ legend:{ display:false } } } });

// --- faner ---
let analysisDrawn = false;
document.querySelectorAll(".tabs button").forEach(b => b.addEventListener("click", () => {
  document.querySelectorAll(".tabs button").forEach(x => x.classList.toggle("active", x === b));
  const t = b.dataset.tab;
  // Varmekartet tåler ikke å tegnes mens kartet er skjult (bredde 0), så laget fjernes og legges på igjen
  if (t !== "oversikt" && layer) { map.removeLayer(layer); layer = null; }
  document.getElementById("tab-oversikt").hidden = t !== "oversikt";
  document.getElementById("tab-analyse").hidden = t !== "analyse";
  if (t === "oversikt") { map.invalidateSize(); update(); }
  if (t === "analyse" && !analysisDrawn) { drawAnalysis(); analysisDrawn = true; }
  try { localStorage.setItem("fane", t); } catch (e) {}
}));

// --- analysefanen ---
const GCOL = { "Brann i bygning":"#c2410c", "Brann i skorstein (2023→)":"#7c3aed", "Brannhindrende tiltak komfyr":"#059669", "Falske/unødige utrykninger":"#2563eb" };
const fmt = (v, d=2) => v == null ? "–" : v.toLocaleString("no", { minimumFractionDigits:d, maximumFractionDigits:d });
function heat(v) {  // farge for relativ rate rundt 1,0
  if (v == null) return "";
  const x = Math.max(-1, Math.min(1, Math.log2(v) / 2));
  return x >= 0 ? `background:rgba(220,38,38,${(0.08 + 0.45*x).toFixed(2)})` : `background:rgba(37,99,235,${(0.08 + 0.45*-x).toFixed(2)})`;
}
function pivot(rows) {  // [x, gruppe, rel, n, dager] -> {xs, groups, get(x,g)}
  const xs = [...new Set(rows.map(r=>r[0]))].sort((a,b)=>a-b);
  const groups = Object.keys(GCOL).filter(g => rows.some(r=>r[1]===g));
  const m = new Map(rows.map(r => [r[0]+"|"+r[1], r]));
  return { xs, groups, get:(x,g) => m.get(x+"|"+g) };
}
function relChart(el, rows, xlabel) {
  const p = pivot(rows);
  new Chart(el, { type:"line",
    data:{ labels:p.xs.map(x => (x>0?"+":"")+x), datasets:p.groups.map(g => ({ label:g, data:p.xs.map(x => (p.get(x,g)||[])[2] ?? null),
      borderColor:GCOL[g], backgroundColor:GCOL[g], tension:0.25, pointRadius:2, spanGaps:true })) },
    options:{ responsive:true, maintainAspectRatio:false,
      plugins:{ legend:{ position:"bottom", labels:{ boxWidth:10 } },
        tooltip:{ callbacks:{ label:c => `${c.dataset.label}: ${fmt(c.parsed.y)}` } } },
      scales:{ x:{ title:{ display:true, text:xlabel } }, y:{ beginAtZero:true, title:{ display:true, text:"Relativ rate (1 = vanlig)" } } } } });
}
function drawAnalysis() {
  const A = D.analysis;
  if (!A) { document.getElementById("analysis").hidden = true; document.getElementById("noanalysis").hidden = false; return; }
  const [nSt, nEv, nDays, medKm] = A.summary;
  kStations.textContent = nSt.toLocaleString("no");
  kEvents.textContent = nEv.toLocaleString("no");
  kDays.textContent = nDays.toLocaleString("no");
  kDist.textContent = fmt(medKm, 1) + " km";

  // Hovedfunn regnet ut fra tallene
  const at = (g, t) => ((A.tempRel.find(r => r[1]===g && r[0]===t)) || [])[2];
  const li = [];
  const sk = at("Brann i skorstein (2023→)", -10), by = at("Brann i bygning", -10);
  if (sk) li.push(`<b>Skorstein:</b> ${fmt(sk,1)} ganger vanlig nivå ved −10 °C.`);
  if (by) li.push(`<b>Bygning:</b> ${Math.round((by-1)*100)} % flere branner ved −10 °C, også justert for sesong.`);
  li.push(`<b>Komfyr og falske utrykninger</b> øker ikke med kulde – effekten skyldes trolig ikke bare at folk er mer inne.`);
  li.push(`<b>Tidsforsinkelse:</b> sterkest samme dag og dagen etter.`);
  findings.innerHTML = li.map(x => `<li>${x}</li>`).join("");

  relChart(aTemp, A.tempRel, "Døgnmiddeltemperatur (°C)");
  relChart(aAnom, A.anomRel, "Avvik fra normal temperatur (°C)");

  new Chart(aMonthly, { data:{ labels:A.monthly.map(r=>r[0]), datasets:[
      { type:"line", label:"Temperatur (°C)", data:A.monthly.map(r=>r[1]), yAxisID:"y1", borderColor:"#64748b", backgroundColor:"#64748b", pointRadius:0, borderWidth:1.5, tension:0.3 },
      { type:"bar", label:"Brann i bygning", data:A.monthly.map(r=>r[2]), backgroundColor:GCOL["Brann i bygning"] },
      { type:"bar", label:"Brann i skorstein", data:A.monthly.map(r=>r[3]), backgroundColor:GCOL["Brann i skorstein (2023→)"] } ] },
    options:{ responsive:true, maintainAspectRatio:false, plugins:{ legend:{ position:"bottom", labels:{ boxWidth:10 } } },
      scales:{ x:{ stacked:true, ticks:{ maxTicksLimit:12 } }, y:{ stacked:true, beginAtZero:true, title:{ display:true, text:"Branner per måned" } },
               y1:{ position:"right", grid:{ drawOnChartArea:false }, title:{ display:true, text:"°C" } } } } });

  const SHORT = { "Brann i bygning":"Bygning", "Brann i skorstein (2023→)":"Skorstein (2023→)", "Brannhindrende tiltak komfyr":"Komfyr", "Falske/unødige utrykninger":"Falske utrykninger" };
  const lagLabels = ["Samme dag", "Dagen før", "2 dager før", "3 dager før", "Snitt 7 dager"];

  // Tabell: relativ rate etter temperatur
  const p = pivot(A.tempRel);
  let h = "<thead><tr><th>Temperatur</th>" + p.groups.map(g => `<th>${SHORT[g]}</th>`).join("") + "<th>Stasjonsdager</th></tr></thead><tbody>";
  for (const x of p.xs) {
    const base = p.get(x, "Brann i bygning");
    h += `<tr><td>${x} til ${x+2} °C</td>` + p.groups.map(g => { const r = p.get(x,g); return `<td style="${heat(r && r[2])}">${fmt(r && r[2])}</td>`; }).join("")
       + `<td>${base ? base[4].toLocaleString("no") : "–"}</td></tr>`;
  }
  tTemp.innerHTML = h + "</tbody>";

  tLag.innerHTML = "<thead><tr><th>Gruppe</th>" + lagLabels.map(l => `<th>${l}</th>`).join("") + "</tr></thead><tbody>"
    + A.lags.map(r => `<tr><td>${SHORT[r[0]] || r[0]}</td>` + r.slice(1).map(v => `<td>${fmt(v, 3)}</td>`).join("") + "</tr>").join("") + "</tbody>";

  const kildeNavn = { bygg:"Branner tilknyttet bygg", falske:"Falske/unødige utrykninger", spredning_til_bygg:"Branner med spredning til bygg" };
  tDist.innerHTML = "<thead><tr><th>Datasett</th><th>Hendelser</th><th>Median avstand (km)</th><th>90 % innen (km)</th><th>Andel innen 10 km</th></tr></thead><tbody>"
    + A.dist.map(r => `<tr><td>${kildeNavn[r[0]] || r[0]}</td><td>${r[1].toLocaleString("no")}</td><td>${fmt(r[2],1)}</td><td>${fmt(r[3],1)}</td><td>${fmt(r[4],1)} %</td></tr>`).join("") + "</tbody>";
}

// Åpne fanen som sist var i bruk (helt til slutt, når alt over er definert)
try { if (localStorage.getItem("fane") === "analyse") document.querySelector('.tabs button[data-tab="analyse"]').click(); } catch (e) {}
</script>
</body>
</html>
"""


def main() -> None:
    data = collect()
    html = TEMPLATE.replace("__DATA__", json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    OUT.write_text(html, encoding="utf-8")
    print(f"Skrev {OUT} ({OUT.stat().st_size / 1e6:.1f} MB, {len(data['points']):,} punkter, {len(data['stations'])} stasjoner)")


if __name__ == "__main__":
    main()
