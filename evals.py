from dotenv import load_dotenv
load_dotenv()

import os
import re
import glob
import json
import asyncio
import argparse
import unicodedata
from difflib import SequenceMatcher

from contrato import Item, Evidencia
from agente import Config
import orquestador as orq


def normalizar(texto: str) -> str:
    """
    Neutraliza diferencias de FORMATO (no de contenido): minúsculas, sin
    tildes, espacios/saltos de línea colapsados a uno solo. Nunca borra un
    espacio entre palabras distintas, solo colapsa espacios repetidos.
    """
    texto = texto.lower()
    texto = unicodedata.normalize('NFD', texto)
    texto = ''.join(c for c in texto if unicodedata.category(c) != 'Mn')
    texto = re.sub(r'\s+', ' ', texto)
    return texto.strip()


def textos_iguales(a: str, b: str) -> bool:
    """
    Igualdad exacta después de normalizar formato.
    """
    return normalizar(a) == normalizar(b)


def textos_similares(a: str, b: str) -> float:
    """
    Similitud normalizada de strings (0 a 1). Solo informativa: el eval no la usa para aprobar.
    """
    ratio = SequenceMatcher(None, normalizar(a), normalizar(b)).ratio()
    return ratio


def cargar_referencia(path_json: str, expediente_code: str) -> list[Item]:
    """
    Carga referencia.json y prefija los documento_id con el código de
    expediente, para que coincidan con los ids que expone el servidor MCP.
    """
    with open(path_json, encoding='utf-8') as f:
        data = json.load(f)

    items = []
    for item in data['items']:
        item['origen']['documento_id'] = f"{expediente_code}_{item['origen']['documento_id']}"
        items.append(Item(**item))
    return items


def cargar_evidencia_referencia(path_json: str, expediente_code: str) -> dict:
    """
    Evidencia esperada por ítem, tomada de los veredictos de la referencia:
    {'AF-01': {'evidencia': [Evidencia, ...], 'paginas_admitidas': [1, 2, 3, 4]}}
    Evidencia vacía = la referencia dice que ese ítem no tiene respaldo en la prueba.
    """
    with open(path_json, encoding='utf-8') as f:
        data = json.load(f)

    return {
        v['item_id']: {
            'evidencia': [
                Evidencia(documento_id=f"{expediente_code}_{e['documento_id']}",
                          pagina=e['pagina'], fragmento=e['fragmento'])
                for e in v['evidencia']
            ],
            'paginas_admitidas': v['paginas_admitidas'],
        }
        for v in data['veredictos']
    }


def evaluar_fuente(evidencias: dict, esperadas: dict, trazas: list[dict]) -> dict:
    """
    Acierto de fuente, en dos niveles:
      - estricto (ok):  documento esperado + página admitida + fragmento esperado.
      - doc_pag_ok:     documento esperado + página admitida (sin exigir el fragmento).
    Si la referencia no tiene respaldo para el ítem, acierta quien no cita nada
    (citar donde no hay respaldo es adivinar).
    """
    docs_leidos = {t['item_id']: t['docs_leidos'] for t in trazas}

    resultados = []
    for item_id, ref in esperadas.items():
        if item_id not in evidencias:   # ítem no verificado en esta corrida (ITEMS_A_VERIFICAR)
            continue
        citadas = evidencias[item_id]

        if not ref['evidencia']:
            ok = doc_pag_ok = not citadas
        else:
            doc_pag_ok = all(
                any(c.documento_id == e.documento_id and c.pagina in ref['paginas_admitidas']
                    for c in citadas)
                for e in ref['evidencia']
            )
            # El fragmento coincide si uno contiene al otro: el verificador puede citar una frase
            # que incluye el dato esperado, o solo el dato.
            ok = all(
                any(c.documento_id == e.documento_id and c.pagina in ref['paginas_admitidas']
                    and (normalizar(e.fragmento) in normalizar(c.fragmento)
                         or normalizar(c.fragmento) in normalizar(e.fragmento))
                    for c in citadas)
                for e in ref['evidencia']
            )

        resultados.append({
            'id': item_id,
            'ok': ok,
            'doc_pag_ok': doc_pag_ok,
            'motivo': f"citó {[(c.documento_id, c.pagina) for c in citadas]}, "
                      f"esperaba {[(e.documento_id, ref['paginas_admitidas']) for e in ref['evidencia']]}, "
                      f"docs leídos: {docs_leidos.get(item_id, [])}"
                      + ''.join(f"\n      citó {c.documento_id} p.{c.pagina}: {c.fragmento[:90]!r}" for c in citadas)
                      + ''.join(f"\n      esperado: {e.fragmento!r}" for e in ref['evidencia']),
        })

    aciertos = sum(1 for r in resultados if r['ok'])
    aciertos_dp = sum(1 for r in resultados if r['doc_pag_ok'])
    total = len(resultados)
    return {
        'porcentaje_acierto': round(100 * aciertos / total, 1) if total else 0.0,
        'aciertos': aciertos,
        'porcentaje_doc_pagina': round(100 * aciertos_dp / total, 1) if total else 0.0,
        'aciertos_doc_pagina': aciertos_dp,
        'total': total,
        'detalle': resultados,
    }


def cargar_estados_referencia(path_json: str) -> dict:
    """
    Estado esperado por ítem: {'AF-01': 'respaldada', ...}
    """
    with open(path_json, encoding='utf-8') as f:
        data = json.load(f)
    return {v['item_id']: v['estado'] for v in data['veredictos']}


def evaluar_estado(veredictos: dict, estados_ref: dict) -> dict:
    """
    Acierto de veredicto: el estado que devolvió el sistema contra el de la referencia.
    """
    resultados = []
    for item_id, esperado in estados_ref.items():
        if item_id not in veredictos:   # ítem no verificado en esta corrida (ITEMS_A_VERIFICAR)
            continue
        obtenido = veredictos[item_id].estado
        resultados.append({'id': item_id, 'ok': obtenido == esperado,
                           'motivo': f'obtuvo {obtenido}, esperaba {esperado}'})
    aciertos = sum(1 for r in resultados if r['ok'])
    total = len(resultados)
    return {'porcentaje_acierto': round(100 * aciertos / total, 1) if total else 0.0,
            'aciertos': aciertos, 'total': total, 'detalle': resultados}


async def correr_orquestador(expediente: str):
    """
    Corre el sistema completo sobre un expediente y devuelve (items, evidencias, trazas, veredictos).
    """
    estado = await orq.run_orquestador(f'{expediente}_DEM')
    items = list(estado['items'].values())
    evidencias = {item_id: v.evidencia for item_id, v in estado['veredictos'].items()}
    trazas = list(estado['trazas'].values())
    return items, evidencias, trazas, estado['veredictos']


def evaluar(items_extraidos: list[Item], items_referencia: list[Item]) -> dict:
    por_id = {item.id: item for item in items_extraidos}

    resultados = []
    for ref in items_referencia:
        extraido = por_id.get(ref.id)

        if extraido is None:
            resultados.append({'id': ref.id, 'ok': False, 'motivo': 'no extraído'})
            continue
        
        texto_ok = textos_iguales(extraido.texto, ref.texto)
        pagina_ok = extraido.origen.pagina == ref.origen.pagina
        doc_ok = extraido.origen.documento_id == ref.origen.documento_id
        
        if ref.titulo is None:
            titulo_ok = True  # por si la referencia no exige título para este ítem
        else:
            titulo_ok = extraido.titulo is not None and textos_iguales(extraido.titulo, ref.titulo)
        
        if texto_ok and pagina_ok and doc_ok and titulo_ok:
            resultados.append({'id': ref.id, 'ok': True})
        else:
            motivos = []
            if not texto_ok:
                ratio = textos_similares(extraido.texto, ref.texto)
                motivos.append(f"texto no coincide (similitud informativa: {ratio:.2f})")
            if not titulo_ok:
                motivos.append(f"título no coincide ('{extraido.titulo}' != '{ref.titulo}')")
            if not pagina_ok:
                motivos.append(f"página incorrecta ({extraido.origen.pagina} != {ref.origen.pagina})")
            if not doc_ok:
                motivos.append(f'documento_id incorrecto ({extraido.origen.documento_id} != {ref.origen.documento_id})')
            resultados.append({'id': ref.id, 'ok': False, 'motivo': ', '.join(motivos)})


    ids_extra = set(por_id.keys()) - {ref.id for ref in items_referencia}
    for id_extra in ids_extra:
        resultados.append({'id': id_extra, 'ok': False, 'motivo': 'extraído de más, no está en la referencia'})

    aciertos = sum(1 for r in resultados if r['ok'])
    total = len(resultados)

    return {
        'porcentaje_acierto': round(100 * aciertos / total, 1) if total else 0.0,
        'aciertos': aciertos,
        'total': total,
        'detalle': resultados,
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('expediente', nargs='?', default='C3')
    parser.add_argument('--modo', choices=['realtime', 'batch'], default='realtime',
                        help='batch: Batch API, mitad de precio por request, puede tardar')
    parser.add_argument('--effort', choices=['low', 'medium', 'high'], default=None,
                        help='sin el flag: la configuración congelada (medium)')
    args = parser.parse_args()
    expediente = args.expediente

    if expediente == 'C4':
        print('ATENCIÓN: C4 es la demanda apartada; ya se corrió una vez en la semana 6.')
        if input('¿Seguro? (escribí C4 para confirmar): ').strip() != 'C4':
            raise SystemExit('Cancelado.')

    orq.MODO = args.modo
    if args.effort:
        orq.CONFIG = Config(effort=args.effort)
    print(f'Config: modo={orq.MODO} effort={orq.CONFIG.effort or "default"}')

    rutas = glob.glob(f'corpus/{expediente}_*/referencia/referencia.json')
    if len(rutas) != 1:
        raise SystemExit(f'Esperaba 1 referencia para {expediente} y encontré {len(rutas)}: {rutas}')
    ruta_referencia = rutas[0]

    # --- Corrida ---
    items_extraidos, evidencias, trazas, veredictos = asyncio.run(correr_orquestador(expediente))
    items_referencia = cargar_referencia(ruta_referencia, expediente_code=expediente)

    resultado = evaluar(items_extraidos, items_referencia)
    fuente = evaluar_fuente(evidencias, cargar_evidencia_referencia(ruta_referencia, expediente), trazas)
    estados = evaluar_estado(veredictos, cargar_estados_referencia(ruta_referencia))

    print(f"\n=== EVAL ÍTEM (extracción): {resultado['porcentaje_acierto']}% ({resultado['aciertos']}/{resultado['total']}) ===")
    print(f"=== EVAL VEREDICTO (estado): {estados['porcentaje_acierto']}% ({estados['aciertos']}/{estados['total']}) ===")
    print(f"=== EVAL FUENTE (doc + página + fragmento): {fuente['porcentaje_acierto']}% ({fuente['aciertos']}/{fuente['total']}) ===")
    print(f"=== EVAL FUENTE (doc + página):              {fuente['porcentaje_doc_pagina']}% ({fuente['aciertos_doc_pagina']}/{fuente['total']}) ===\n")
    for r in resultado['detalle']:
        if not r['ok']:
            print(f"X ítem   - {r['id']}: {r['motivo']}")
    for r in estados['detalle']:
        if not r['ok']:
            print(f"X estado - {r['id']}: {r['motivo']}")
    for r in fuente['detalle']:
        if not r['ok']:
            print(f"X fuente - {r['id']}: {r['motivo']}")

    # --- Diagnóstico: costo, caché y traza de cada ítem con estado incorrecto ---
    por_id = trazas if isinstance(trazas, dict) else {t['item_id']: t for t in trazas}
    n = max(len(por_id), 1)
    costo = sum(t['costo'] for t in por_id.values())
    cr = sum(t.get('cache_read', 0) for t in por_id.values())
    cw = sum(t.get('cache_write', 0) for t in por_id.values())
    print('\n--- DIAGNÓSTICO ---')
    print(f'Referencia: {os.path.abspath(ruta_referencia)}')
    print(f'Costo verificadores: ${costo:.5f} ({len(por_id)} ítems, ${costo / n:.5f} por afirmación)')
    print(f'Caché: {cw:,} tokens escritos, {cr:,} leídos '
          f'({sum(1 for t in por_id.values() if t.get("cache_read", 0) > 0)}/{len(por_id)} ítems con lectura)')
    print(f'Reintentos: {[i for i, t in por_id.items() if t.get("reintentos")]}')
    print(f'Páginas/documentos corregidos: {[(i, t["correcciones"]) for i, t in por_id.items() if t.get("correcciones")]}')
    print(f'Reencolados del batch: {[(i, t["reencolado"]) for i, t in por_id.items() if t.get("reencolado")]}')
    if any(t.get('tramo_caro') for t in por_id.values()):
        print('ATENCIÓN: algún request pasó los 100.000 tokens de prompt: se cobró 5x.')
    for r in estados['detalle']:
        if not r['ok'] and r['id'] in por_id:
            t = por_id[r['id']]
            print(f"\n{r['id']}: veredicto={t.get('veredicto')} citas={t.get('citas')} "
                  f"docs={t.get('docs_leidos')} stop={t.get('stop_reason')} errores={t.get('errores_validacion', [])}")
            print(f"  respuesta: {t.get('respuesta', '')[:300]}")