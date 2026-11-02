import json
import time
import asyncio
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import stdio_client
from agente import (
    client, MODELO, THINKING, SERVER_PARAMS, ITEMS_A_VERIFICAR, CONFIG_CONGELADA,
    costo_usd, extraer_items, verificar_items as verificar_items_agente
)

DEMANDA_ID = 'C1_DEM'
MAX_ITERACIONES = 5
MAX_PARALELOS = 5
COMPARAR_SECUENCIAL = False # True: verifica también en secuencia para medir el tiempo (gasta el doble)

# Cómo se verifica. correr.py y evals.py los pisan con los argumentos de línea de comandos.
MODO = 'realtime'            # 'realtime' | 'batch'
CONFIG = CONFIG_CONGELADA    # effort, max_tokens

SYSTEM_ORQUESTADOR = """
    Sos el orquestador del sistema Trenes, que verifica si cada afirmación de
    hecho de una demanda tiene respaldo en la prueba documental del expediente.
    
    ## Tu rol
    No leés documentos ni juzgás afirmaciones vos mismo: delegás en sub-agentes.
    - delegar_extraccion: el extractor lee la demanda y te devuelve los ids de sus afirmaciones.
    - delegar_verificacion: verificadores (uno por afirmación, en paralelo) las contrastan con la prueba.
    
    ## Reglas
    - Pedí primero la extracción de la demanda indicada.
    - Con los ids que te devuelva, pedí la verificación de todos en UNA sola delegación.
    - Si la verificación informa afirmaciones fallidas, reintentá solo esas, una vez.
    
    ## Cuándo terminás
    Cuando la verificación esté hecha, respondé UNA oración con el total verificado y el conteo
    por estado. No repitas el detalle de cada afirmación: queda guardado aparte.
    Respondé siempre en español.
"""

TOOLS_ORQUESTADOR = [
    {
        'name': 'delegar_extraccion',
        'description': ('Delega al sub-agente extractor la lectura de una demanda. Devuelve los ids '
                        'de sus afirmaciones de hecho. Usala primero.'),
        'input_schema': {
            'type': 'object',
            'properties': {
                'demanda_id': {
                    'type': 'string', 
                    'description': 'Id de la demanda, ej: C1_DEM'
                }
            },
            'required': ['demanda_id']
        }
    },
    {
        'name': 'delegar_verificacion',
        'description': ('Delega a verificadores (uno por afirmación, en paralelo) la verificación de '
                        'las afirmaciones indicadas contra la prueba. Usala cuando ya tengas los ids.'),
        'input_schema': {
            'type': 'object',
            'properties': {
                'item_ids': {
                    'type': 'array', 
                    'items': {'type': 'string'},
                    'description': 'Ids de las afirmaciones, ej: ["AF-01", "AF-02"]'
                }
            },
            'required': ['item_ids']
        }
    }
]


async def verificar_items(session, items: list, expediente: str, paralelo: bool = True) -> list:
    """
    Fan-out: un verificador por ítem. Devuelve una lista alineada con `items`; cada elemento es
    (veredicto, traza) o la excepción si ese verificador falló, para que un error no tire abajo al resto.
    """
    # Secuencial = un verificador a la vez (para medir el paralelo, como en M4).
    return await verificar_items_agente(session, items, expediente, CONFIG, modo=MODO,
                                        max_paralelos=MAX_PARALELOS if paralelo else 1)


async def medir(corutina):
    t0 = time.perf_counter()
    resultado = await corutina
    return resultado, time.perf_counter() - t0
 
 
async def delegar_extraccion(session, estado: dict, argumentos: dict):
    demanda_id = argumentos['demanda_id']
    items, costo = await extraer_items(session, demanda_id)
    estado['costo'] += costo
    if not items:
        return 'El extractor no devolvió afirmaciones.', True
    estado['items'] = {item.id: item for item in items}
    estado['expediente'] = demanda_id.split('_')[0]
    # Al orquestador solo vuelven los ids: el texto de las afirmaciones queda en `estado`.
    return json.dumps({'total': len(items), 'ids': list(estado['items'])}), False
 
 
async def delegar_verificacion(session, estado: dict, argumentos: dict):
    if not estado['items']:
        return 'Todavía no hay afirmaciones: pedí primero la extracción.', True
    ids = [i for i in argumentos['item_ids'] if i in estado['items']]
    if ITEMS_A_VERIFICAR:
        ids = [i for i in ids if i in ITEMS_A_VERIFICAR]
    if not ids:
        return 'Ninguno de esos ids corresponde a una afirmación extraída.', True
    items = [estado['items'][i] for i in ids]
 
    if COMPARAR_SECUENCIAL:
        resultados_seq, estado['tiempos']['secuencial'] = await medir(
            verificar_items(session, items, estado['expediente'], paralelo=False))
        estado['costo'] += sum(r[1]['costo'] for r in resultados_seq if not isinstance(r, Exception))
    resultados, estado['tiempos']['paralelo'] = await medir(
        verificar_items(session, items, estado['expediente'], paralelo=True))
 
    # Fan-in: se consolida en `estado`.
    fallidas = []
    for item, resultado in zip(items, resultados):
        if isinstance(resultado, Exception):
            print(f'  [verificador] {item.id} falló: {type(resultado).__name__}: {resultado}')
            fallidas.append(item.id)
            continue
        veredicto, traza = resultado
        estado['veredictos'][item.id] = veredicto
        estado['trazas'][item.id] = traza
        estado['costo'] += traza['costo']
 
    conteo = {}
    for veredicto in estado['veredictos'].values():
        conteo[veredicto.estado] = conteo.get(veredicto.estado, 0) + 1
    return json.dumps({'verificadas': len(estado['veredictos']), 'por_estado': conteo,
                       'fallidas': fallidas}), False


async def despachar_a_subagente(session, estado: dict, nombre_tool: str, argumentos: dict):
    """
    Mapea cada tool 'meta' del orquestador al sub-agente correspondiente.
    """
    if nombre_tool == 'delegar_extraccion':
        return await delegar_extraccion(session, estado, argumentos)
    if nombre_tool == 'delegar_verificacion':
        return await delegar_verificacion(session, estado, argumentos)
    return f"Error: sub-agente '{nombre_tool}' no reconocido", True
 
 
async def run_orquestador(demanda_id: str = DEMANDA_ID, max_iteraciones: int = MAX_ITERACIONES) -> dict:
    estado = {'items': {}, 'expediente': None, 'veredictos': {}, 'trazas': {}, 'tiempos': {}, 'costo': 0.0}
    mensajes = [{'role': 'user',
                 'content': f'Verificá las afirmaciones de hecho de la demanda {demanda_id} contra su prueba.'}]
 
    async with stdio_client(SERVER_PARAMS) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
 
            for iteracion in range(max_iteraciones):
                response = await client.messages.create(
                    model=MODELO,
                    max_tokens=1024,
                    system=SYSTEM_ORQUESTADOR,
                    tools=TOOLS_ORQUESTADOR,
                    messages=mensajes,
                    thinking=THINKING
                )
                estado['costo'] += costo_usd(response.usage)
                mensajes.append({'role': 'assistant', 'content': response.content})
 
                if response.stop_reason == 'end_turn':
                    print(f"\n[orquestador] {''.join(b.text for b in response.content if b.type == 'text')}")
                    return estado
                elif response.stop_reason == 'tool_use':
                    tool_results = []
                    for block in response.content:
                        if block.type == 'tool_use':
                            print(f'[orquestador → {block.name}] {json.dumps(block.input, ensure_ascii=False)[:100]}')
                            resultado, es_error = await despachar_a_subagente(session, estado, block.name, block.input)
                            tool_results.append({'type': 'tool_result', 'tool_use_id': block.id,
                                                 'content': resultado, 'is_error': es_error})
                    mensajes.append({'role': 'user', 'content': tool_results})
                else:
                    print(f'[orquestador] stop_reason inesperado: {response.stop_reason}')
                    return estado
 
    print(f'\n[orquestador] Límite de {max_iteraciones} iteraciones alcanzado sin terminar.')
    return estado


def imprimir_resumen(estado: dict):
    print('\n=== RESULTADO ===')
    for item_id, veredicto in estado['veredictos'].items():
        traza = estado['trazas'][item_id]
        citas = ', '.join(f'{e.documento_id} p.{e.pagina}' for e in veredicto.evidencia) or '-'
        print(f"{item_id}: {veredicto.estado:<12} | citas: {citas} | reintentos: {traza.get('reintentos', 0)} "
              f"| ${traza['costo']:.5f} | {traza['segundos']:.1f} s")
    n = len(estado['veredictos'])
    if not n:
        print('No se verificó ninguna afirmación.')
        return
    costo_verificadores = sum(traza['costo'] for traza in estado['trazas'].values())    
    print(f"\nCosto total de la corrida: ${estado['costo']:.5f} USD")
    print(f'Costo por afirmación (solo verificador): ${costo_verificadores / n:.5f} USD')
    suma = sum(t['segundos'] for t in estado['trazas'].values())
    print(f"Suma de tiempos individuales: {suma:.1f} s | tiempo real (paralelo): {estado['tiempos']['paralelo']:.1f} s")
    for modo, segundos in estado['tiempos'].items():
        print(f'Tiempo {modo}: {segundos:.2f} s')
    if len(estado['tiempos']) == 2:
        print(f"Mejora del paralelo: {estado['tiempos']['secuencial'] / estado['tiempos']['paralelo']:.2f}x")


if __name__ == '__main__':
    import correr      # la corrida completa vive en correr.py
    correr.main()