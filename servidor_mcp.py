from dotenv import load_dotenv
load_dotenv()

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.utilities.types import File
mcp = FastMCP('Servidor MCP - Caso Trenes')

from criterio import CRITERIO_AFIRMACION

import re
import os
import json
import pdfplumber
from pathlib import Path
from functools import lru_cache


PATH_CORPUS = Path(os.getenv("PATH_CORPUS"))
SIZE_CONTEXTO = 150

def _build_corpus_index():
    """
    Construye el índice leyendo referencia.json de cada expediente, 
    prefijado por expediente para evitar colisiones 
    (DEM/DOC-1/CON se repiten en cada expediente).
    
    Resultado: {"C1_DEM": {...}, "C1_DOC-1": {...}, "C2_DEM": {...}, ...}
    """
    index = {}

    if not PATH_CORPUS.exists():
        return index

    for exp_folder in sorted(PATH_CORPUS.glob("C*")):
        if not exp_folder.is_dir():
            continue

        ref_path = exp_folder / "referencia" / "referencia.json"
        if not ref_path.exists():
            continue

        expediente_code = exp_folder.name.split("_")[0]  # "C1_Impresos_..." -> "C1"

        with open(ref_path, encoding="utf-8") as f:
            ref_data = json.load(f)

        for doc in ref_data["documentos"]:
            doc_id = f"{expediente_code}_{doc['id']}"  # "C1_DEM", "C1_DOC-1", "C1_CON"
            index[doc_id] = {
                "id": doc_id,
                "titulo": doc["titulo"],
                "tipo": doc["tipo"],
                "path": str(exp_folder / doc["path"]),
                "paginas": doc["paginas"],  # ya viene de referencia, no hace falta pdfplumber acá
            }

    return index

_DOCUMENT_INDEX = _build_corpus_index()


_PATRON_FOOTER = re.compile(r'[^\n]*\nDOCUMENTO SINTETICO[^\n]*\n?')

def _limpiar_header_footer(texto: str) -> str:
    """
    Remueve el bloque de 2 líneas que se repite al inicio de cada página
    del PDF (código de expediente + partes, y el disclaimer "DOCUMENTO
    SINTETICO...| N de M").
    """
    return _PATRON_FOOTER.sub('\n', texto)


@mcp.tool()
def listar_documentos() -> list[dict]:
    """
    Lista todos los documentos disponibles en el corpus.
    
    Devuelve una lista con metadata de cada documento:
    - id: identificador único
    - titulo: nombre del archivo
    - tipo: 'demanda' o 'prueba'
    - path: ruta al archivo
    - paginas: cantidad de páginas
    
    El agente usa esto para DESCUBRIR qué documentos existen.
    """
    return list(_DOCUMENT_INDEX.values())

    
@mcp.tool()
def leer_documento(id: str):
    """
    Lee el contenido completo de un documento por su id.
    
    Params:
        id: identificador del documento (ej: "C1_DEM", "C1_DOC-1", "C1_CON")
    
    Devuelve: la demanda como texto con marcadores de página; la prueba como
    PDF citable (para poder citar documento, página y fragmento).
    
    Error: si el id no existe, el archivo falta, el formato no es PDF o el PDF
    es ilegible, devuelve un error (is_error) con la causa.
    """
    if id not in _DOCUMENT_INDEX:
        raise ToolError(f"Documento no encontrado: {id}. Documentos disponibles: {list(_DOCUMENT_INDEX.keys())}")
    
    doc_info = _DOCUMENT_INDEX[id]
    doc_path = Path(doc_info['path'])
    
    if not doc_path.exists():
        raise ToolError(f"El archivo de {id} no existe: {doc_path.name}")
    if doc_path.suffix.lower() != '.pdf':
        raise ToolError(f"Formato no soportado para {id}: {doc_path.suffix}. Solo se leen PDF.")
    
    try:
        with pdfplumber.open(doc_path) as pdf:
            texto_completo = ''
            for page_num, page in enumerate(pdf.pages, start=1):
                texto_pagina = page.extract_text() or ""
                texto_completo += f"\n--- PÁGINA {page_num} ---\n{texto_pagina}"
    except Exception as e:
        raise ToolError(f"PDF ilegible en {id}: {str(e)}")
    
    if doc_info['tipo'].startswith('prueba'):   # 'prueba' y 'prueba_de_la_demandada'
        return File(data=doc_path.read_bytes(), format='pdf', name=id)
    return _limpiar_header_footer(texto_completo)


@lru_cache(maxsize=None)
def _paginas_limpias(path: str) -> tuple:
    """
    Texto limpio de cada página de un PDF. Se calcula una sola vez por documento.
    """
    with pdfplumber.open(Path(path)) as pdf:
        return tuple(_limpiar_header_footer(p.extract_text() or '') for p in pdf.pages)


@mcp.tool()
def buscar(termino: str, documento_id: str = '', limite: int = 10, expediente: str = '') -> dict:
    """
    Búsqueda léxica (no semántica) en la prueba documental del corpus.
    Solo busca en documentos de prueba: nunca en demandas ni contestaciones.
    - documento_id: acota la búsqueda a ese documento de prueba.
    - expediente: ej. 'C1'; acota a la prueba de ese expediente.
    """
    docs = {k: v for k, v in _DOCUMENT_INDEX.items() if v["tipo"].startswith("prueba")}
    if expediente:
        docs = {k: v for k, v in docs.items() if k.startswith(f"{expediente}_")}
    if documento_id:
        if documento_id not in docs:
            return {"error": f"documento_id no disponible para búsqueda: {documento_id} "
                             "(no existe, no es prueba o es de otro expediente)"}
        docs = {documento_id: docs[documento_id]}

    resultados, errores = [], []
    for doc_id, doc_info in docs.items():
        try:
            for page_num, texto_pagina in enumerate(_paginas_limpias(str(doc_info['path'])), start=1):
                if termino.lower() in texto_pagina.lower():
                    idx = texto_pagina.lower().find(termino.lower())
                    inicio = max(0, idx - SIZE_CONTEXTO)
                    fin = min(len(texto_pagina), idx + len(termino) + SIZE_CONTEXTO)
                    resultados.append({
                        "documento_id": doc_id,
                        "pagina": page_num,
                        "fragmento": texto_pagina[inicio:fin].strip()
                    })
        except Exception as e:
            errores.append({"documento_id": doc_id, "error": str(e)})

    return {
        "total": len(resultados),
        "mostrando": min(limite, len(resultados)),
        "coincidencias": resultados[:limite],
        "errores": errores,
    }


@mcp.tool()
def prueba_del_expediente(expediente: str) -> dict:
    """
    Texto limpio, página por página, de TODOS los documentos de prueba de un expediente
    (ej: 'C3'). La usa el código del verificador forzado (M6), no el modelo: con esto arma
    el prefijo cacheado y valida que cada fragmento citado esté en la página que dice.

    Devuelve: {"expediente": "C3", "documentos": [{"id", "titulo", "paginas": [texto_p1, ...]}]}
    """
    docs = [v for k, v in _DOCUMENT_INDEX.items()
            if k.startswith(f"{expediente}_") and v["tipo"].startswith("prueba")]
    if not docs:
        raise ToolError(f"No hay documentos de prueba para el expediente {expediente}")
    salida = []
    def _orden(d):                                    # DOC-2 antes que DOC-10
        m = re.search(r"(\d+)$", d["id"])
        return (int(m.group(1)) if m else 0, d["id"])

    for info in sorted(docs, key=_orden):
        try:
            paginas = list(_paginas_limpias(str(info["path"])))
        except Exception as e:
            raise ToolError(f"PDF ilegible en {info['id']}: {e}")
        salida.append({"id": info["id"], "titulo": info["titulo"], "paginas": paginas})
    return {"expediente": expediente, "documentos": salida}


@mcp.prompt()
def criterio_afirmacion_hecho() -> str:
    """
    Criterio para identificar y extraer afirmaciones de hecho.
    Usado en M2, extendido en M6 con estados.
    """
    return CRITERIO_AFIRMACION




if __name__ == '__main__':
    for info in _DOCUMENT_INDEX.values():
        try:
            _paginas_limpias(str(info['path']))
        except Exception:
            pass
    mcp.run()