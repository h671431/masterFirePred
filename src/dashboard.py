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


def collect() -> dict:
    con = db.connect(read_only=True)
    types = [r[0] for r in con.execute(
        "SELECT oppdragstype FROM brann GROUP BY 1 ORDER BY count(*) DESC").fetchall()]
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
    con.close()
    return {
        "types": types,
        "points": points,
        "perYear": per_year,
        "perMonth": per_month,
        "perHour": per_hour,
        "falskeKat": falske_kat,
        "perFylke": per_fylke,
        "stations": stations,
        "weather": weather,
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
main { display:grid; grid-template-columns: 300px 1fr; gap:16px; padding:16px 20px 24px; }
.panel { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:14px; }
#map { height: 640px; border-radius:10px; border:1px solid var(--line); }
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
@media (max-width: 820px) { main { grid-template-columns:1fr; padding:16px; } .charts { grid-template-columns:1fr; padding:0 16px 24px; } #map { height:460px; } }
</style>
</head>
<body>
<header>
  <h1>Brannhendelser og vær – foreløpig oversikt</h1>
  <p>BRIS-uttrekk 2016–2026 og MET-stasjoner. Lokal side – ikke del den, den inneholder koordinater for hendelser.</p>
</header>
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
<script>
const D = __DATA__;
const MONTHS = ["jan","feb","mar","apr","mai","jun","jul","aug","sep","okt","nov","des"];
const PALETTE = ["#c2410c","#2563eb","#059669","#a16207","#7c3aed","#db2777","#0891b2","#65a30d","#dc2626","#4f46e5","#0d9488","#ca8a04","#9333ea","#e11d48","#0284c7","#16a34a","#b45309","#6d28d9","#be123c","#475569"];
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
Chart.defaults.color = css("--muted"); Chart.defaults.borderColor = css("--line");

// --- kontroller ---
const typesDiv = document.getElementById("types");
const defaultOn = new Set(["Brann i bygning","Brann i skorstein"]);
D.types.forEach((t,i) => {
  const l = document.createElement("label");
  l.innerHTML = `<input type="checkbox" value="${i}" ${defaultOn.has(t)?"checked":""}><span class="swatch" style="background:${PALETTE[i%PALETTE.length]}"></span>${t}`;
  typesDiv.appendChild(l);
});
const years = [...new Set(D.points.map(p=>p[3]))].sort();
for (const id of ["y0","y1"]) { const s = document.getElementById(id); years.forEach(y => s.add(new Option(y,y))); }
document.getElementById("y1").value = years[years.length-1];
MONTHS.forEach((m,i) => document.getElementById("mnd").add(new Option(m,i+1)));

// --- kart ---
const map = L.map("map", { preferCanvas:true }).setView([64.5, 13], 5);
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
  const on = new Set([...typesDiv.querySelectorAll("input:checked")].map(e=>+e.value));
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
