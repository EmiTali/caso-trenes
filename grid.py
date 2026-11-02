"""
GRID de configuraciones del verificador (M6).

    python grid.py                                   # C1 C2 C3, todas las configs, 3 repeticiones
    python grid.py --configs forzado_low forzado_high --reps 2
    python grid.py --expedientes C5 C6 --configs forzado_medium   # chequeo de generalización
    python grid.py --resumen                         # solo muestra la tabla con lo ya corrido (gratis)

- Mide SOLO el verificador: la extracción se hace una vez por expediente y queda en
  cache/extraccion_CX.json (legible, un archivo por expediente). Si cambia el prompt o el schema
  del extractor, la huella no coincide y se re-extrae sola. --reextraer la fuerza.
- Registra una fila por corrida en salida/grid.csv y una por afirmación en salida/grid_items.csv
  (se abren en Excel). Es reanudable: lo ya corrido con el mismo prompt no se vuelve a correr.
- Cada fila lleva la huella del prompt del verificador: si cambiás el prompt, las corridas viejas
  quedan en el CSV pero no se mezclan en el resumen.
- Nunca corre C4: la demanda apartada se abre una sola vez, con evals.py y la config congelada.
"""
import re
import csv
import json
import time
import asyncio
import hashlib
import argparse
from pathlib import Path
from datetime import datetime
from collections import defaultdict
from difflib import SequenceMatcher

import anthropic
from mcp import ClientSession
from mcp.client.stdio import stdio_client

from contrato import Item
from agente import (SERVER_PARAMS, MODELO, THINKING, SYSTEM_PROMPT_EXTRACTOR, SCHEMA_EXTRACTOR,
                    SYSTEM_PROMPT_VERIFICADOR, Config, extraer_items, cargar_prueba, verificar_items)
from criterio import TOOL_REGISTRAR_VEREDICTO, VERSION_VALIDACION
from evals import cargar_estados_referencia, cargar_evidencia_referencia, evaluar_estado, evaluar_fuente

# Configuraciones a comparar. forzado_medium es el default del modelo (sin el flag de effort).
CONFIGS = {
    'forzado_low':     Config(effort='low'),
    'forzado_medium':  Config(effort='medium'),
    'forzado_high':    Config(effort='high'),
}
EXPEDIENTE_APARTADO = 'C4'
EMPATE_PP = 2.0           # dos configs "empatan" si su % de estado difiere en hasta estos puntos

DIR_SALIDA = Path('salida')
CSV_CORRIDAS = DIR_SALIDA / 'grid.csv'
CSV_ITEMS = DIR_SALIDA / 'grid_items.csv'
DIR_CACHE = Path('cache')

COLUMNAS_CORRIDA = ['fecha', 'config', 'huella_prompt', 'rep', 'expediente', 'effort', 'thinking',
                    'total', 'aciertos_estado', 'pct_estado', 'aciertos_doc_pag', 'pct_doc_pag',
                    'aciertos_fragmento', 'fallidas', 'costo_verificador', 'costo_por_afirmacion',
                    'cache_leido', 'cache_escrito', 'tokens_salida', 'reintentos', 'segundos', 'ids_mal']
COLUMNAS_ITEM = ['config', 'huella_prompt', 'rep', 'expediente', 'item_id', 'esperado', 'obtenido',
                 'ok_estado', 'ok_doc_pag', 'ok_fragmento', 'citas', 'costo', 'reintentos', 'error', 'nota']


def _huella(*partes) -> str:
    return hashlib.sha256('\x1f'.join(partes).encode('utf-8')).hexdigest()[:10]


# Huella de todo lo que define al verificador: prompt, tool, modelo y versión de la validación.
HUELLA_PROMPT = _huella(SYSTEM_PROMPT_VERIFICADOR, json.dumps(TOOL_REGISTRAR_VEREDICTO, sort_keys=True), MODELO or '',
                        VERSION_VALIDACION)
HUELLA_EXTRACTOR = _huella(SYSTEM_PROMPT_EXTRACTOR, json.dumps(SCHEMA_EXTRACTOR, sort_keys=True),
                           json.dumps(THINKING), MODELO or '')


# ------------------------------------------------------------------ extracción cacheada
class ExtraccionDesalineada(Exception):
    """Los ítems extraídos no se corresponden 1 a 1 con los de la referencia."""


def _texto_normalizado(s: str) -> str:
    return re.sub(r'\s+', ' ', s.lower()).strip()


def chequear_alineacion(items, expediente: str, umbral: float = 0.9) -> None:
    """
    El grid compara el veredicto del ítem AF-07 extraído con el veredicto AF-07 de la referencia.
    Si el extractor se salteó una afirmación, los ids se corren y se compara contra el veredicto de
    OTRA afirmación. Esto no mide la calidad de la extracción (eso es evals.py): solo que cada id
    extraído sea la misma afirmación que en la referencia (texto casi igual).
    """
    with open(ruta_referencia(expediente), encoding='utf-8') as f:
        ref = {i['id']: i['texto'] for i in json.load(f)['items']}
    extraidos = {i.id: i.texto for i in items}
    problemas = [f'{iid}: falta en la extracción' for iid in ref if iid not in extraidos]
    problemas += [f'{iid}: sobra (no está en la referencia)' for iid in extraidos if iid not in ref]
    for iid in ref.keys() & extraidos.keys():
        ratio = SequenceMatcher(None, _texto_normalizado(ref[iid]), _texto_normalizado(extraidos[iid])).ratio()
        if ratio < umbral:
            problemas.append(f'{iid}: el texto extraído no es esa afirmación (similitud {ratio:.2f}): '
                             f'"{extraidos[iid][:70]}…"')
    if problemas:
        raise ExtraccionDesalineada(
            f'La extracción de {expediente} no está alineada con la referencia ({len(extraidos)} ítems '
            f'extraídos, {len(ref)} en la referencia):\n    ' + '\n    '.join(problemas) +
            f'\n  Re-extraé: python grid.py --expedientes {expediente} --reextraer')


async def items_del_expediente(session, expediente: str, reextraer: bool):
    """Devuelve (items, costo). Reutiliza cache/extraccion_CX.json si la huella del extractor coincide."""
    ruta = DIR_CACHE / f'extraccion_{expediente}.json'
    if ruta.exists() and not reextraer:
        datos = json.loads(ruta.read_text(encoding='utf-8'))
        if datos.get('huella') == HUELLA_EXTRACTOR:
            print(f'  [{expediente}] extracción desde {ruta} ({len(datos["items"])} ítems, sin llamar a la API)')
            items = [Item(**i) for i in datos['items']]
            chequear_alineacion(items, expediente)
            return items, 0.0
        print(f'  [{expediente}] el prompt o el schema del extractor cambió: re-extraigo')
    items, costo = await extraer_items(session, f'{expediente}_DEM')
    if not items:
        raise RuntimeError(f'El extractor no devolvió ítems para {expediente}')
    chequear_alineacion(items, expediente)        # si falla, no se guarda en la caché
    DIR_CACHE.mkdir(exist_ok=True)
    ruta.write_text(json.dumps({'expediente': expediente, 'huella': HUELLA_EXTRACTOR, 'modelo': MODELO,
                                'fecha': datetime.now().isoformat(timespec='seconds'), 'costo_usd': costo,
                                'items': [i.model_dump() for i in items]},
                               ensure_ascii=False, indent=2), encoding='utf-8')
    return items, costo


def ruta_referencia(expediente: str) -> str:
    rutas = list(Path('corpus').glob(f'{expediente}_*/referencia/referencia.json'))
    if len(rutas) != 1:
        raise SystemExit(f'Esperaba 1 referencia para {expediente} y encontré {len(rutas)}: {rutas}')
    return str(rutas[0])


# ------------------------------------------------------------------ registro en CSV
def agregar_csv(ruta: Path, columnas: list, filas: list[dict]) -> None:
    DIR_SALIDA.mkdir(exist_ok=True)
    nuevo = not ruta.exists()
    # Formato Excel en español: utf-8-sig (tildes), ';' como separador y ',' como decimal.
    filas = [{k: (str(v).replace('.', ',') if isinstance(v, float) else v) for k, v in f.items()} for f in filas]
    with open(ruta, 'a', encoding='utf-8-sig' if nuevo else 'utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=columnas, delimiter=';')
        if nuevo:
            w.writeheader()
        w.writerows(filas)


def leer_corridas() -> list[dict]:
    if not CSV_CORRIDAS.exists():
        return []
    with open(CSV_CORRIDAS, encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f, delimiter=';'))


def _num(x) -> float:
    return float(str(x).replace(',', '.')) if x not in (None, '') else 0.0


# ------------------------------------------------------------------ una corrida
async def correr_una(session, nombre: str, cfg: Config, rep: int, expediente: str, items, prueba, ref: str):
    t0 = time.perf_counter()
    resultados = await verificar_items(session, items, expediente, cfg, modo='realtime', prueba=prueba)

    veredictos, trazas, errores = {}, {}, {}
    for item, r in zip(items, resultados):
        if isinstance(r, Exception):
            # Un error de la API (límite de uso, 429/5xx tras los reintentos, conexión) no es una
            # medición del verificador: la corrida no se registra y el grid se detiene.
            if isinstance(r, anthropic.APIError):
                raise r
            errores[item.id] = f'{type(r).__name__}: {r}'[:200]   # el modelo falló: cuenta como error
        else:
            veredictos[item.id], trazas[item.id] = r

    estados_ref = cargar_estados_referencia(ref)
    est = {d['id']: d for d in evaluar_estado(veredictos, estados_ref)['detalle']}
    fue = {d['id']: d for d in evaluar_fuente({k: v.evidencia for k, v in veredictos.items()},
                                              cargar_evidencia_referencia(ref, expediente),
                                              list(trazas.values()))['detalle']}

    filas_items = []
    for item in items:
        t = trazas.get(item.id, {})
        filas_items.append({
            'config': nombre, 'huella_prompt': HUELLA_PROMPT, 'rep': rep, 'expediente': expediente,
            'item_id': item.id, 'esperado': estados_ref.get(item.id, ''),
            'obtenido': veredictos[item.id].estado if item.id in veredictos else 'FALLIDA',
            # Una afirmación fallida cuenta como error: si no, el % se inflaría al sacarla del total.
            'ok_estado': int(est.get(item.id, {}).get('ok', False)),
            'ok_doc_pag': int(fue.get(item.id, {}).get('doc_pag_ok', False)),
            'ok_fragmento': int(fue.get(item.id, {}).get('ok', False)),
            'citas': t.get('citas', 0), 'costo': round(t.get('costo', 0.0), 7),
            'reintentos': t.get('reintentos', 0), 'error': errores.get(item.id, ''),
            # La justificación del modelo: para entender un error sin volver a pagar la corrida.
            'nota': ' '.join(t.get('respuesta', '').split())[:400],
        })

    total = len(items)
    costo = sum(t['costo'] for t in trazas.values())
    ok_est = sum(f['ok_estado'] for f in filas_items)
    ok_dp = sum(f['ok_doc_pag'] for f in filas_items)
    fila = {
        'fecha': datetime.now().isoformat(timespec='seconds'), 'config': nombre, 'huella_prompt': HUELLA_PROMPT,
        'rep': rep, 'expediente': expediente, 'effort': cfg.effort or 'default',
        'thinking': 0,      # la tool forzada no piensa; la columna queda por compatibilidad con el CSV
        'total': total, 'aciertos_estado': ok_est, 'pct_estado': round(100 * ok_est / total, 1),
        'aciertos_doc_pag': ok_dp, 'pct_doc_pag': round(100 * ok_dp / total, 1),
        'aciertos_fragmento': sum(f['ok_fragmento'] for f in filas_items), 'fallidas': len(errores),
        'costo_verificador': round(costo, 6), 'costo_por_afirmacion': round(costo / total, 7),
        'cache_leido': sum(t.get('cache_read', 0) for t in trazas.values()),
        'cache_escrito': sum(t.get('cache_write', 0) for t in trazas.values()),
        'tokens_salida': sum(t.get('tokens_out', 0) for t in trazas.values()),
        'reintentos': sum(t.get('reintentos', 0) for t in trazas.values()),
        'segundos': round(time.perf_counter() - t0, 1),
        'ids_mal': ' '.join(f['item_id'] for f in filas_items if not f['ok_estado']),
    }
    agregar_csv(CSV_CORRIDAS, COLUMNAS_CORRIDA, [fila])
    agregar_csv(CSV_ITEMS, COLUMNAS_ITEM, filas_items)
    print(f"  {nombre:<16} rep {rep}  {expediente}: estado {ok_est}/{total} ({fila['pct_estado']}%)  "
          f"doc+pág {ok_dp}/{total}  ${fila['costo_por_afirmacion']:.5f}/afirmación  "
          f"caché leído {fila['cache_leido']:,}  {fila['segundos']}s  mal: {fila['ids_mal'] or '-'}")
    return fila


# ------------------------------------------------------------------ resumen
def resumen(todas: bool = False) -> None:
    filas = [f for f in leer_corridas() if todas or f['huella_prompt'] == HUELLA_PROMPT]
    if not filas:
        print('No hay corridas registradas con el prompt actual.')
        return
    por_cfg = defaultdict(list)
    for f in filas:
        por_cfg[f['config']].append(f)

    tabla = []
    for cfg, fs in por_cfg.items():
        total = sum(int(f['total']) for f in fs)
        pcts = [_num(f['pct_estado']) for f in fs]
        tabla.append({
            'config': cfg, 'corridas': len(fs),
            'expedientes': ' '.join(sorted({f['expediente'] for f in fs})),
            'pct_estado': 100 * sum(int(f['aciertos_estado']) for f in fs) / total,
            'rango': f'{min(pcts):.0f}-{max(pcts):.0f}',
            'pct_doc_pag': 100 * sum(int(f['aciertos_doc_pag']) for f in fs) / total,
            'costo_af': sum(_num(f['costo_verificador']) for f in fs) / total,
            'fallidas': sum(int(f['fallidas']) for f in fs),
            'seg': sum(_num(f['segundos']) for f in fs) / len(fs),
        })
    tabla.sort(key=lambda r: (-r['pct_estado'], r['costo_af']))
    mejor = tabla[0]['pct_estado']
    empatadas = [r for r in tabla if r['pct_estado'] >= mejor - EMPATE_PP]
    ganadora = min(empatadas, key=lambda r: r['costo_af'])['config']

    print(f"\n=== GRID (prompt {HUELLA_PROMPT if not todas else 'todos'}) — % sobre todas las afirmaciones de todas las corridas ===")
    print(f"{'config':<18}{'corridas':>9}  {'expedientes':<15}{'% estado':>9}{'rango':>9}{'% doc+pág':>11}"
          f"{'$/afirm.':>11}{'fallidas':>10}{'s/corrida':>11}")
    for r in tabla:
        marca = '  <- ganadora' if r['config'] == ganadora else ('  (empata)' if r in empatadas else '')
        print(f"{r['config']:<18}{r['corridas']:>9}  {r['expedientes']:<15}{r['pct_estado']:>8.1f}%{r['rango']:>9}"
              f"{r['pct_doc_pag']:>10.1f}%{r['costo_af']:>11.5f}{r['fallidas']:>10}{r['seg']:>11.1f}{marca}")
    print(f'\nRegla: entre las configs a {EMPATE_PP} puntos o menos de la mejor en % de estado, gana la más barata.')
    if len({r['corridas'] for r in tabla}) > 1:
        print('ATENCIÓN: las configs no tienen la misma cantidad de corridas: la comparación no es pareja.')

    # Afirmaciones que fallan o que cambian de estado entre repeticiones de la misma config.
    items = [f for f in leer_csv_items() if todas or f['huella_prompt'] == HUELLA_PROMPT]
    obtenidos = defaultdict(lambda: defaultdict(list))
    for f in items:
        obtenidos[f['config']][(f['expediente'], f['item_id'])].append((f['obtenido'], f['esperado']))
    print('\nAfirmaciones con errores (expediente/ítem fallas/repeticiones [obtenido≠esperado]; '
          '* = cambia entre repeticiones). La nota de cada una está en grid_items.csv:')
    for cfg in [r['config'] for r in tabla]:
        malos = []
        for (exp, iid), obs in sorted(obtenidos[cfg].items()):
            mal = [o for o, e in obs if o != e]
            if mal:
                malos.append(f"{exp}/{iid} {len(mal)}/{len(obs)} [{'/'.join(sorted(set(mal)))}≠{obs[0][1]}]"
                             f"{'*' if len({o for o, _ in obs}) > 1 else ''}")
        print(f"  {cfg:<18}{', '.join(malos) or '-'}")


def leer_csv_items() -> list[dict]:
    if not CSV_ITEMS.exists():
        return []
    with open(CSV_ITEMS, encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f, delimiter=';'))


# ------------------------------------------------------------------ main
async def main(args):
    hechas = {(f['config'], int(f['rep']), f['expediente']) for f in leer_corridas()
              if f['huella_prompt'] == HUELLA_PROMPT}
    pendientes = [(c, r, e) for e in args.expedientes for c in args.configs for r in range(1, args.reps + 1)
                  if (c, r, e) not in hechas]
    print(f'Prompt del verificador: {HUELLA_PROMPT}. Corridas pendientes: {len(pendientes)} '
          f'(ya registradas: {len(hechas)})')
    if not pendientes:
        return

    async with stdio_client(SERVER_PARAMS) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            costo_extraccion = 0.0
            try:
                for expediente in args.expedientes:
                    del_exp = [p for p in pendientes if p[2] == expediente]
                    if not del_exp:
                        continue
                    items, costo = await items_del_expediente(session, expediente, args.reextraer)
                    costo_extraccion += costo
                    prueba = await cargar_prueba(session, expediente)   # una vez por expediente
                    ref = ruta_referencia(expediente)
                    for nombre, rep, _ in del_exp:
                        await correr_una(session, nombre, CONFIGS[nombre], rep, expediente, items, prueba, ref)
            except anthropic.APIError as e:
                print(f'\nGRID DETENIDO por un error de la API ({type(e).__name__}): {e}\n'
                      'La corrida en curso NO se registró. Lo ya registrado queda; al relanzar sigue desde ahí.')
            except ExtraccionDesalineada as e:
                print(f'\nGRID DETENIDO: {e}')
            if costo_extraccion:
                print(f'\nExtracción (una vez, queda en {DIR_CACHE}/): ${costo_extraccion:.5f}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Grid de configuraciones del verificador')
    parser.add_argument('--expedientes', nargs='+', default=['C1', 'C2', 'C3'])
    parser.add_argument('--configs', nargs='+', default=list(CONFIGS), choices=list(CONFIGS))
    parser.add_argument('--reps', type=int, default=3)
    parser.add_argument('--reextraer', action='store_true', help='ignora cache/extraccion_CX.json')
    parser.add_argument('--resumen', action='store_true', help='solo muestra la tabla (no llama a la API)')
    parser.add_argument('--todas', action='store_true', help='en el resumen, incluye corridas con prompts viejos')
    args = parser.parse_args()

    if EXPEDIENTE_APARTADO in args.expedientes:
        raise SystemExit(f'{EXPEDIENTE_APARTADO} es la demanda apartada: no entra al grid. '
                         'Se corre una sola vez con evals.py y la config congelada.')
    if not args.resumen:
        asyncio.run(main(args))
    resumen(args.todas)