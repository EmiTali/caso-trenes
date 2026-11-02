"""
Interfaz de demo del caso Trenes.

    streamlit run app.py

- Barra lateral: correr un expediente del corpus (llama a correr.py) y elegir qué resultado ver.
- Panel principal: por afirmación, el veredicto, el porqué y la cita exacta en la prueba.
- Opcional: comparar contra la referencia del expediente.

Lee los resultados de salida/veredictos_<exp>.json. Acepta el formato nuevo de correr.py
({'expediente', 'config', 'items', 'veredictos'}) y el viejo ({id: veredicto}).
"""
import glob
import json
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

SALIDA = Path('salida')
EXPEDIENTE_APARTADO = 'C4'  # correr.py pide confirmación por teclado: no se lanza desde la interfaz
ESTILO = {
    'respaldada': ('🟢', 'Respaldada'),
    'contradicha': ('🔴', 'Contradicha'),
    'sin_respaldo': ('🟡', 'Sin respaldo'),
}

st.set_page_config(page_title='Verificador de demandas', page_icon='⚖️', layout='wide')


# ------------------------------------------------------------------ datos
def etiqueta(estado: str) -> str:
    icono, nombre = ESTILO.get(estado, ('⚪', estado))
    return f'{icono} {nombre}'


@st.cache_data
def cargar_resultado(ruta: str, mtime: float) -> dict:
    """
    mtime entra en la clave de la caché: si correr.py reescribe el archivo, se vuelve a leer.
    """
    datos = json.loads(Path(ruta).read_text(encoding='utf-8'))
    if 'veredictos' not in datos:  # formato viejo: solo {id: veredicto}
        datos = {'veredictos': datos, 'items': {}}
    return datos


@st.cache_data
def cargar_referencia(expediente: str) -> dict:
    """
    {item_id: estado esperado}, con la misma función que usan evals.py y grid.py.
    """
    rutas = glob.glob(f'corpus/{expediente}_*/referencia/referencia.json')
    if len(rutas) != 1:
        return {}
    try:
        from evals import cargar_estados_referencia
        return cargar_estados_referencia(rutas[0])
    except Exception:
        return {}


def expedientes_en_corpus() -> list[str]:
    return sorted({Path(p).name.split('_')[0] for p in glob.glob('corpus/C*_*')})


def resultados_disponibles() -> dict[str, Path]:
    return {p.stem.removeprefix('veredictos_'): p for p in sorted(SALIDA.glob('veredictos_*.json'))}


# ------------------------------------------------------------------ corrida
def correr_expediente(expediente: str, modo: str) -> bool:
    """
    Lanza correr.py y muestra su salida en vivo.
    """
    env = {**os.environ, 'PYTHONIOENCODING': 'utf-8', 'PYTHONUNBUFFERED': '1'}
    cmd = [sys.executable, 'correr.py', expediente, '--modo', modo]
    with st.status(f'Verificando el expediente {expediente}...', expanded=True) as status:
        log = st.empty()
        lineas = []
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, text=True, encoding='utf-8',
                                errors='replace', env=env)
        for linea in proc.stdout:
            lineas.append(linea.rstrip())
            log.code('\n'.join(lineas[-25:]), language=None)
        proc.wait()
        if proc.returncode == 0:
            status.update(label=f'Expediente {expediente} verificado', state='complete', expanded=False)
        else:
            status.update(label=f'Falló la corrida de {expediente} (código {proc.returncode})', state='error')
    return proc.returncode == 0


# ------------------------------------------------------------------ barra lateral
with st.sidebar:
    st.header('Correr un expediente')
    corribles = [e for e in expedientes_en_corpus() if e != EXPEDIENTE_APARTADO]
    exp_correr = st.selectbox('Expediente del corpus', corribles) if corribles else None
    modo = st.radio('Modo', ['realtime', 'batch'], horizontal=True,
                    help='Batch es por Batch API: más barato con volumen, pero tarda minutos.')
    lanzar = st.button('▶ Verificar', disabled=exp_correr is None, use_container_width=True)
    st.divider()

st.title('⚖️ Verificador de afirmaciones de hecho')
st.caption('Para cada afirmación de la demanda: un veredicto y la cita exacta en la prueba documental.')

if lanzar and correr_expediente(exp_correr, modo):
    st.session_state['ver'] = exp_correr

disponibles = resultados_disponibles()
if not disponibles:
    st.info('Todavía no hay resultados en salida/. Corré un expediente desde la barra lateral.')
    st.stop()

with st.sidebar:
    st.header('Resultados')
    opciones = list(disponibles)
    elegido = st.session_state.get('ver')
    exp = st.selectbox('Ver expediente', opciones,
                       index=opciones.index(elegido) if elegido in opciones else 0)
    comparar = st.toggle('Comparar con la referencia')

# ------------------------------------------------------------------ resultados
ruta = disponibles[exp]
datos = cargar_resultado(str(ruta), ruta.stat().st_mtime)
veredictos = datos['veredictos']
items = datos.get('items', {})
referencia = cargar_referencia(exp) if comparar else {}

st.subheader(f'Expediente {exp}')
if datos.get('config'):
    cfg = datos['config']
    st.caption(f"Modo {cfg.get('modo', '?')} · effort {cfg.get('effort', '?')}")

conteo = pd.Series([v['estado'] for v in veredictos.values()]).value_counts()
columnas = st.columns(4)
columnas[0].metric('Afirmaciones', len(veredictos))
for col, estado in zip(columnas[1:], ESTILO):
    col.metric(etiqueta(estado), int(conteo.get(estado, 0)))

if comparar and not referencia:
    st.warning('No encontré la referencia de este expediente.')
elif referencia:
    ok = sum(referencia.get(i) == v['estado'] for i, v in veredictos.items())
    st.success(f'Coincide con la referencia en {ok} de {len(veredictos)} afirmaciones.')

filtro = st.multiselect('Mostrar', list(ESTILO), default=list(ESTILO), format_func=etiqueta)

for item_id in sorted(veredictos):
    v = veredictos[item_id]
    if v['estado'] not in filtro:
        continue
    item = items.get(item_id, {})
    titulo = item.get('titulo') or item.get('texto', '')[:90]
    marca = ''
    if referencia:
        esperado = referencia.get(item_id, '?')
        marca = '  ✅' if esperado == v['estado'] else f'  ❌ referencia: {esperado}'

    encabezado = f"{etiqueta(v['estado'])} · {item_id}" + (f' · {titulo}' if titulo else '')
    with st.expander(f'{encabezado}{marca}'):
        if item.get('texto'):
            pagina = item.get('origen', {}).get('pagina', '?')
            st.markdown(f'**Afirmación** (demanda, pág. {pagina})')
            st.markdown(f"> {item['texto']}")
        if v.get('nota'):
            st.markdown(f"**Por qué:** {v['nota']}")
        if v.get('evidencia'):
            st.markdown('**Evidencia**')
            for ev in v['evidencia']:
                st.markdown(f"📄 `{ev['documento_id']}` · pág. {ev['pagina']}")
                st.info(ev['fragmento'])
        else:
            st.caption('Sin cita: la prueba no tiene material sobre este hecho.')

st.download_button('Descargar resultados (JSON)', ruta.read_bytes(), file_name=ruta.name,
                   mime='application/json')