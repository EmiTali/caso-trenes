"""
Comprueba que leer_documento devuelve un error (is_error) con la causa en los 4 casos que pide la consigna.
No toca tu corpus: arma uno temporal con archivos rotos y arranca el servidor apuntando a ese.
Correr desde la carpeta del proyecto (donde está servidor_mcp.py):  python scripts/diag_errores.py
"""
import os
import sys
import json
import asyncio
import tempfile
from pathlib import Path

from mcp.client.stdio import stdio_client
from mcp import ClientSession, StdioServerParameters


def armar_corpus_temporal(base: Path) -> None:
    carpeta = base / 'C9_Pruebas_de_error'
    (carpeta / 'prueba').mkdir(parents=True)
    (carpeta / 'referencia').mkdir()
    (carpeta / 'prueba' / 'corrupto.pdf').write_bytes(b'esto no es un pdf')
    (carpeta / 'prueba' / 'notas.txt').write_text('hola', encoding='utf-8')
    docs = [
        {'id': 'DOC-1', 'titulo': 'Archivo que no existe', 'tipo': 'prueba', 'path': 'prueba/no_existe.pdf', 'paginas': 1},
        {'id': 'DOC-2', 'titulo': 'PDF ilegible',          'tipo': 'prueba', 'path': 'prueba/corrupto.pdf',  'paginas': 1},
        {'id': 'DOC-3', 'titulo': 'Formato no soportado',  'tipo': 'prueba', 'path': 'prueba/notas.txt',     'paginas': 1},
    ]
    (carpeta / 'referencia' / 'referencia.json').write_text(
        json.dumps({'documentos': docs, 'items': []}), encoding='utf-8')


CASOS = {
    'id inexistente':       'C9_NOEXISTE',
    'archivo que no existe': 'C9_DOC-1',
    'PDF ilegible':         'C9_DOC-2',
    'formato no soportado': 'C9_DOC-3',
}


async def main(corpus: Path) -> bool:
    params = StdioServerParameters(command=sys.executable, args=['servidor_mcp.py'],
                                   env={**os.environ, 'PATH_CORPUS': str(corpus)})
    todo_ok = True
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for caso, doc_id in CASOS.items():
                res = await session.call_tool('leer_documento', {'id': doc_id})
                ok = bool(res.is_error)
                todo_ok = todo_ok and ok
                print(f"{'OK ' if ok else 'MAL'} {caso:22s} is_error={res.is_error} | {res.content[0].text[:85]}")
    return todo_ok


if __name__ == '__main__':
    with tempfile.TemporaryDirectory() as tmp:
        armar_corpus_temporal(Path(tmp))
        ok = asyncio.run(main(Path(tmp)))
    print('\n' + ('Los 4 casos devuelven error con la causa.' if ok else 'ATENCIÓN: algún caso no devolvió error.'))