"""Entrypoint del servicio para Cloud Run (FastAPI) + interfaz gráfica.

Endpoints:
  GET  /               -> interfaz web (botón para generar actualización + análisis)
  GET  /health         -> healthcheck
  POST /run            -> ejecuta el pipeline; devuelve JSON con estadísticos
  GET  /report/latest  -> descarga el último reporte .xlsx desde GCS

La interfaz usa la paleta del dashboard (naranja/verde Nutresa). Como el servicio
es privado, se accede a la UI por `gcloud run services proxy` (localhost) o
habilitando acceso autenticado; las llamadas fetch son del mismo origen.
"""
from __future__ import annotations

import os
from dataclasses import asdict

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from .config import get_settings
from .pipeline import run_pipeline

app = FastAPI(title="Mantum Automatizador APU", version="2.0.0")


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.post("/run")
async def run(request: Request):
    settings = get_settings()
    # Permite override puntual de dry_run desde la UI.
    try:
        body = await request.json()
    except Exception:
        body = {}
    if isinstance(body, dict) and "dry_run" in body:
        os.environ["DRY_RUN"] = "true" if body["dry_run"] else "false"
        get_settings.cache_clear()
        settings = get_settings()
    try:
        result = run_pipeline(settings)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    status_code = 200 if not result.errors else 207
    return JSONResponse(status_code=status_code, content=asdict(result))


@app.get("/report/latest")
def report_latest():
    settings = get_settings()
    try:
        from google.cloud import storage

        client = storage.Client()
        blobs = [
            b for b in client.list_blobs(settings.gcs_bucket_name, prefix=settings.gcs_output_prefix)
            if b.name.endswith(".xlsx")
        ]
        if not blobs:
            raise HTTPException(status_code=404, detail="Aún no hay reportes generados.")
        latest = max(blobs, key=lambda b: b.updated)
        data = latest.download_as_bytes()
        fname = latest.name.split("/")[-1]
        return Response(
            content=data,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(_INDEX_HTML)


_INDEX_HTML = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Mántum · Automatizador APU</title>
<style>
  :root{
    --naranja:#F47920; --naranja-2:#F58220; --naranja-d:#D9641A;
    --verde:#7AB317; --verde-2:#8DC63F; --oliva:#A6A917; --amarillo:#F2B707;
    --gris-bg:#EDEFF1; --card:#FFFFFF; --tinta:#2B2F33; --gris-2:#6B7280;
  }
  *{box-sizing:border-box;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif}
  body{margin:0;background:var(--gris-bg);color:var(--tinta)}
  header{background:linear-gradient(90deg,var(--naranja),var(--naranja-2));
    color:#fff;padding:22px 28px;display:flex;align-items:center;gap:16px;
    box-shadow:0 2px 10px rgba(0,0,0,.12)}
  header .logo{width:42px;height:42px;border-radius:10px;background:#fff;
    display:flex;align-items:center;justify-content:center;color:var(--naranja);
    font-weight:800;font-size:20px}
  header h1{font-size:20px;margin:0;font-weight:700}
  header p{margin:0;opacity:.9;font-size:13px}
  .wrap{max-width:1180px;margin:0 auto;padding:26px}
  .hero{display:flex;justify-content:space-between;align-items:center;gap:20px;
    background:var(--card);border-radius:16px;padding:26px;
    box-shadow:0 4px 18px rgba(0,0,0,.06);margin-bottom:22px}
  .hero h2{margin:0 0 6px;font-size:22px}
  .hero p{margin:0;color:var(--gris-2);max-width:620px;font-size:14px}
  .controls{display:flex;flex-direction:column;gap:10px;align-items:flex-end}
  .toggle{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--gris-2)}
  .btn{border:0;cursor:pointer;border-radius:12px;padding:16px 26px;font-size:16px;
    font-weight:700;color:#fff;background:linear-gradient(90deg,var(--naranja),var(--naranja-d));
    box-shadow:0 6px 16px rgba(244,121,32,.35);transition:transform .08s,box-shadow .2s}
  .btn:hover{transform:translateY(-1px);box-shadow:0 8px 22px rgba(244,121,32,.45)}
  .btn:disabled{opacity:.55;cursor:not-allowed;transform:none}
  .btn.ghost{background:var(--card);color:var(--naranja-d);border:2px solid var(--naranja);
    box-shadow:none}
  .status{margin:18px 0;padding:16px 20px;border-radius:12px;background:#fff;
    border-left:6px solid var(--amarillo);display:none;align-items:center;gap:14px}
  .spinner{width:22px;height:22px;border:3px solid #eee;border-top-color:var(--naranja);
    border-radius:50%;animation:spin 1s linear infinite}
  @keyframes spin{to{transform:rotate(360deg)}}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:16px}
  .card{background:var(--card);border-radius:14px;padding:18px 20px;
    box-shadow:0 3px 14px rgba(0,0,0,.06);border-top:5px solid var(--verde)}
  .card.naranja{border-top-color:var(--naranja)}
  .card.amarillo{border-top-color:var(--amarillo)}
  .card.oliva{border-top-color:var(--oliva)}
  .card .label{font-size:12.5px;color:var(--gris-2);text-transform:uppercase;
    letter-spacing:.4px;margin-bottom:8px}
  .card .value{font-size:28px;font-weight:800}
  .card .sub{font-size:12px;color:var(--gris-2);margin-top:4px}
  .section{margin-top:24px}
  .section h3{font-size:16px;margin:0 0 12px;color:var(--naranja-d)}
  .tops{display:grid;grid-template-columns:1fr 1fr;gap:16px}
  .toplist{background:#fff;border-radius:14px;padding:16px 18px;box-shadow:0 3px 14px rgba(0,0,0,.06)}
  .toplist h4{margin:0 0 10px;font-size:14px}
  .toplist.cara h4{color:var(--naranja-d)} .toplist.barata h4{color:var(--verde)}
  .row{display:flex;justify-content:space-between;padding:7px 0;border-bottom:1px dashed #eee;font-size:14px}
  .row .pill{font-weight:700;padding:2px 9px;border-radius:20px;font-size:12px;color:#fff}
  .cara .pill{background:var(--naranja)} .barata .pill{background:var(--verde)}
  .concl{background:#fff;border-radius:14px;padding:18px 20px;box-shadow:0 3px 14px rgba(0,0,0,.06)}
  .concl li{margin:8px 0;font-size:14px;line-height:1.45}
  .err{border-left-color:#e23b3b !important}
  footer{padding:18px;text-align:center;color:var(--gris-2);font-size:12px}
  @media(max-width:720px){.hero{flex-direction:column;align-items:flex-start}
    .controls{align-items:flex-start}.tops{grid-template-columns:1fr}}
</style>
</head>
<body>
<header>
  <div class="logo">M</div>
  <div><h1>Mántum · Automatizador APU</h1>
  <p>Actualización de la base de APU de mantenimiento y análisis comparativo regional</p></div>
</header>

<div class="wrap">
  <div class="hero">
    <div>
      <h2>Generar actualización y análisis</h2>
      <p>Cruza el warehouse con las cotizaciones por región y el gasto real
      (Consolidado), aplica el IPC y produce un informe descargable con la fuente
      que refuta cada precio, outliers y comparativo por región.</p>
    </div>
    <div class="controls">
      <label class="toggle"><input type="checkbox" id="dry"/> Modo auditoría (no escribe el Sheet)</label>
      <button class="btn" id="go" onclick="run()">⚙️ Generar actualización y análisis</button>
    </div>
  </div>

  <div class="status" id="status">
    <div class="spinner" id="spin"></div>
    <div id="statusText">Procesando…</div>
  </div>

  <div id="results" style="display:none">
    <div id="diag" style="margin-bottom:14px"></div>
    <div class="grid" id="cards"></div>

    <div class="section">
      <h3>Análisis por categoría de costo</h3>
      <div class="tops" id="cats" style="grid-template-columns:repeat(3,1fr)"></div>
    </div>

    <div class="section">
      <h3>Regiones por nivel de precio</h3>
      <div class="tops">
        <div class="toplist cara"><h4>🔺 Top 5 más costosas</h4><div id="caras"></div></div>
        <div class="toplist barata"><h4>🔻 Top 5 más económicas</h4><div id="baratas"></div></div>
      </div>
    </div>

    <div class="section">
      <h3>Conclusiones</h3>
      <div class="concl"><ul id="concl"></ul></div>
    </div>

    <div class="section">
      <button class="btn" onclick="download()">⬇️ Descargar informe detallado (.xlsx)</button>
      <button class="btn ghost" onclick="location.reload()">↻ Nueva ejecución</button>
    </div>
  </div>
</div>

<footer>Grupo Nutresa · Mántum — generado automáticamente</footer>

<script>
const cop = n => '$'+(Number(n)||0).toLocaleString('es-CO');
function setStatus(txt, err=false, spin=true){
  const s=document.getElementById('status');
  s.style.display='flex'; s.classList.toggle('err',err);
  document.getElementById('spin').style.display=spin?'block':'none';
  document.getElementById('statusText').innerHTML=txt;
}
async function run(){
  const btn=document.getElementById('go'); btn.disabled=true;
  document.getElementById('results').style.display='none';
  const dry=document.getElementById('dry').checked;
  setStatus('Analizando fuentes, cruzando ítems y aplicando IPC… (puede tardar 1–3 min)');
  try{
    const r=await fetch('/run',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({dry_run:dry})});
    const d=await r.json();
    render(d);
    setStatus('✅ Listo. '+(d.dry_run?'(modo auditoría: no se escribió el Sheet)':'Warehouse actualizado.'),false,false);
  }catch(e){ setStatus('❌ Error: '+e.message,true,false); }
  btn.disabled=false;
}
function card(label,value,sub,cls){return `<div class="card ${cls||''}">
  <div class="label">${label}</div><div class="value">${value}</div>
  ${sub?`<div class="sub">${sub}</div>`:''}</div>`}
function render(d){
  const s=d.stats||{};
  // Diagnóstico: conteos crudos y errores (para detectar fuentes vacías).
  const diag=document.getElementById('diag');
  let dhtml='';
  const warn=(d.comparativos_filas||0)===0;
  dhtml+=`<div style="background:#fff;border-radius:10px;padding:12px 16px;border-left:6px solid ${warn?'#e23b3b':'#7AB317'};font-size:13px;color:#2B2F33">
    <b>Diagnóstico:</b> filas warehouse: ${d.rows_warehouse??'-'} ·
    insumos evaluados: ${d.insumos_evaluados??'-'} ·
    filas de fuentes (comparativos+consolidado): <b>${d.comparativos_filas??'-'}</b> ·
    celdas actualizadas: ${d.celdas_actualizadas??'-'}
    ${warn?'<br>⚠️ No se cargaron precios de fuentes: revisa que los archivos estén en gs://&lt;bucket&gt;/comparativos/ y /consolidado/.':''}
  </div>`;
  if((d.errors||[]).length){
    dhtml+=`<div style="background:#fff;border-radius:10px;padding:12px 16px;border-left:6px solid #e23b3b;font-size:13px;color:#a11;margin-top:8px">
      <b>Errores:</b><ul style="margin:6px 0 0;padding-left:18px">${d.errors.map(e=>`<li>${e}</li>`).join('')}</ul></div>`;
  }
  diag.innerHTML=dhtml;
  const cards=[
    card('Fuentes analizadas', s.fuentes_analizadas??0,'archivos de precios','naranja'),
    card('Registros de precio', (s.registros_precio??0).toLocaleString('es-CO'),'observaciones','oliva'),
    card('Regiones', (s.regiones_analizadas||[]).length,'con datos','amarillo'),
    card('Ítems cruzados', s.cruces_validos??0,'con fuente válida'),
    card('Más barato que IPC', s.items_mercado_mas_barato_que_ipc??0,'oportunidad de ahorro'),
    card('Más caro que IPC', s.items_mercado_mas_caro_que_ipc??0,'revisar','naranja'),
    card('Ahorro potencial', cop(s.ahorro_potencial_total),'vs. ajuste solo IPC'),
    card('Diferencia neta', cop(s.diferencia_neta_total),'mercado vs IPC','amarillo'),
  ].join('');
  document.getElementById('cards').innerHTML=cards;
  const rows=(arr,cls)=> (arr||[]).map(([r,i])=>
    `<div class="row"><span>${r}</span><span class="pill">${Number(i).toFixed(2)}</span></div>`).join('')
    || '<div class="row"><span>Sin datos suficientes</span></div>';
  document.getElementById('caras').innerHTML=rows(s.top5_regiones_mas_costosas);
  document.getElementById('baratas').innerHTML=rows(s.top5_regiones_mas_economicas);
  // Análisis por categoría: Material | Mano de obra | Viáticos.
  const pc=s.por_categoria||{};
  const catColors={'Material':'var(--verde)','Mano de obra':'var(--naranja)','Viáticos':'var(--amarillo)'};
  document.getElementById('cats').innerHTML=['Material','Mano de obra','Viáticos'].map(cat=>{
    const d=pc[cat]||{cruces:0,mas_barato_que_ipc:0,mas_caro_que_ipc:0,ahorro_potencial:0,diferencia_neta:0};
    return `<div class="toplist" style="border-top:5px solid ${catColors[cat]}">
      <h4 style="color:${catColors[cat]}">${cat}</h4>
      <div class="row"><span>Ítems cruzados</span><b>${d.cruces}</b></div>
      <div class="row"><span>Más barato que IPC</span><b>${d.mas_barato_que_ipc}</b></div>
      <div class="row"><span>Más caro que IPC</span><b>${d.mas_caro_que_ipc}</b></div>
      <div class="row"><span>Ahorro potencial</span><b>${cop(d.ahorro_potencial)}</b></div>
      <div class="row"><span>Diferencia neta</span><b>${cop(d.diferencia_neta)}</b></div>
    </div>`}).join('');
  // conclusiones: vienen del backend si las exponemos; si no, derivamos básicas.
  const cs=(d.stats&&d.stats._conclusiones)||[];
  document.getElementById('concl').innerHTML =
    (cs.length?cs:[
      `Se analizaron ${s.fuentes_analizadas??0} fuentes en ${(s.regiones_analizadas||[]).length} regiones.`,
      `${s.items_mercado_mas_barato_que_ipc??0} ítems se consiguen más baratos que aplicando solo IPC; ${s.items_mercado_mas_caro_que_ipc??0} están por encima.`,
      `Ahorro potencial: ${cop(s.ahorro_potencial_total)} · Diferencia neta: ${cop(s.diferencia_neta_total)}.`
    ]).map(t=>`<li>${t}</li>`).join('');
  document.getElementById('results').style.display='block';
}
function download(){ window.location='/report/latest'; }
</script>
</body>
</html>"""


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run("src.main:app", host="0.0.0.0", port=port)
