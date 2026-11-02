"""
La corrida completa, un comando: extrae las afirmaciones de la demanda, las verifica contra la
prueba y deja los veredictos en salida/veredictos_<expediente>.json.

    python correr.py C3                    # tiempo real, configuración congelada
    python correr.py C3 --modo batch       # por Batch API (mitad de precio por request; tarda más)

Para medir contra la referencia, evals.py.
"""
import json
import asyncio
import argparse
from pathlib import Path

import orquestador as orq
from agente import Config

EXPEDIENTE_APARTADO = 'C4'


def main():
    parser = argparse.ArgumentParser(description='Corrida completa del caso Trenes')
    parser.add_argument('expediente', nargs='?', default='C3')
    parser.add_argument('--modo', choices=['realtime', 'batch'], default='realtime')
    parser.add_argument('--effort', choices=['low', 'medium', 'high'], default=None,
                        help='sin el flag: la configuración congelada (medium)')
    args = parser.parse_args()
    expediente = args.expediente

    if expediente == EXPEDIENTE_APARTADO:
        print(f'ATENCIÓN: {EXPEDIENTE_APARTADO} es la demanda apartada; ya se corrió una vez en la semana 6.')
        if input(f'¿Seguro? (escribí {EXPEDIENTE_APARTADO} para confirmar): ').strip() != EXPEDIENTE_APARTADO:
            raise SystemExit('Cancelado.')

    orq.MODO = args.modo
    if args.effort:
        orq.CONFIG = Config(effort=args.effort)
    print(f'Expediente {expediente} | modo {orq.MODO} | effort {orq.CONFIG.effort or "default"}')

    estado = asyncio.run(orq.run_orquestador(f'{expediente}_DEM'))
    orq.imprimir_resumen(estado)

    Path('salida').mkdir(exist_ok=True)
    ruta = Path('salida') / f'veredictos_{expediente}.json'
    items = estado.get('items', {})
    if isinstance(items, list):          # por si el orquestador los guarda como lista
        items = {i.id: i for i in items}
    datos = {
        'expediente': expediente,
        'config': {'modo': orq.MODO, 'effort': orq.CONFIG.effort or 'default'},
        'items': {k: v.model_dump() for k, v in items.items()},
        'veredictos': {k: v.model_dump() for k, v in estado['veredictos'].items()},
    }
    ruta.write_text(json.dumps(datos, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'\nVeredictos en {ruta}')


if __name__ == '__main__':
    main()