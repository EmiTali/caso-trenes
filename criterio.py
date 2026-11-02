"""
Criterio del caso Trenes:
  - CRITERIO_AFIRMACION: qué cuenta como afirmación de hecho (lo sirve el servidor MCP como prompt).
  - ESTADOS y la tool forzada del veredicto (registrar_veredicto), con su validación.
"""
import re
import unicodedata
from typing import Literal

from pydantic import BaseModel, field_validator, model_validator


CRITERIO_AFIRMACION = """
        DEFINICIÓN: AFIRMACIÓN DE HECHO

        Son enunciados de eventos o hechos que la parte actora sostiene que sucedieron.

        INCLUIR:
        - Hechos específicos, verificables contra prueba (fechas, montos, documentos)
        - Numerados en sección "IV.- HECHOS"
        - Ejemplo: "La demandada recibió Orden de Compra N° 430011742 el 26/09/2025"

        EXCLUIR:
        - Argumentación jurídica o interpretación legal
        - Petitorio (lo que se pide al juez)
        - Citas de jurisprudencia
        - Considerandos sobre competencia
        - Defensas o contrarargumentos

        REGLA DE ORO: Si no puedes verificarlo directamente en un documento PDF de prueba, probablemente NO es una afirmación de hecho, es argumentación.
    """

# ------------------------------------------------------------------------------------
# ESTADOS: los tres del contrato, y cómo se traduce lo que registra el verificador.
# ------------------------------------------------------------------------------------
ESTADOS = ('respaldada', 'sin_respaldo', 'contradicha')
ESTADO_POR_RESULTADO = {
    'ENCONTRADO': 'respaldada',
    'CONTRADICE': 'contradicha',
    'SIN MATERIAL': 'sin_respaldo',
}
assert set(ESTADO_POR_RESULTADO.values()) == set(ESTADOS)


# ------------------------------------------------------------------------------------
# La tool forzada del veredicto. El orden nota → evidencia → estado es deliberado: con
# tool_choice forzado Haiku 5.5 no piensa antes de responder, así que el modelo escribe la
# justificación y elige la evidencia ANTES de decidir el estado.
# ------------------------------------------------------------------------------------
NOMBRE_TOOL = 'registrar_veredicto'
MAX_CITAS = 2

TOOL_REGISTRAR_VEREDICTO = {
    'name': NOMBRE_TOOL,
    'description': 'Registra el veredicto sobre la afirmación: nota, evidencia literal de la prueba y estado.',
    'strict': True,
    'input_schema': {
        'type': 'object',
        'properties': {
            'nota': {'type': 'string', 'description': '1 a 3 oraciones en español que justifican el estado'},
            'evidencia': {
                'type': 'array',
                'description': '1 o 2 fragmentos literales con ENCONTRADO o CONTRADICE; vacía con SIN MATERIAL',
                'items': {
                    'type': 'object',
                    'properties': {
                        'documento_id': {'type': 'string', 'description': 'Id del documento, ej: C3_DOC-1'},
                        'pagina': {'type': 'integer'},
                        'fragmento': {'type': 'string', 'description': 'Texto copiado literalmente de esa página'},
                    },
                    'required': ['documento_id', 'pagina', 'fragmento'],
                    'additionalProperties': False,
                },
            },
            'estado': {'type': 'string', 'enum': list(ESTADO_POR_RESULTADO)},
        },
        'required': ['nota', 'evidencia', 'estado'],
        'additionalProperties': False,
    },
}


# ------------------------------------------------------------------------------------
# Validación de lo que devuelve la tool (la validación contra el texto de la prueba
# vive en agente.py, porque necesita la prueba del expediente).
# ------------------------------------------------------------------------------------
VERSION_VALIDACION = '2'

_EQUIVALENCIAS = str.maketrans({'–': '-', '—': '-', '“': '"', '”': '"', '‘': "'", '’': "'"})
# A veces el modelo escribe un carácter no ASCII como la secuencia de escape literal (barra
# invertida + u + cuatro dígitos hex) en lugar del carácter. Es codificación, no contenido.
_ESCAPE_UNICODE = re.compile(r'\\u([0-9a-fA-F]{4})')


def normalizar(s: str) -> str:
    """
    Para comparar texto literal: minúsculas, sin espacios ni saltos de línea, guiones y comillas unificados.
    """
    s = unicodedata.normalize('NFKC', s).translate(_EQUIVALENCIAS).lower()
    return re.sub(r'\s+', '', s)


def desescapar(v):
    return _ESCAPE_UNICODE.sub(lambda m: chr(int(m.group(1), 16)), v) if isinstance(v, str) else v


class EvidenciaSalida(BaseModel):
    documento_id: str
    pagina: int
    fragmento: str

    @field_validator('fragmento', mode='before')
    @classmethod
    def _fragmento(cls, v):
        return desescapar(v)


class RegistroVeredicto(BaseModel):
    nota: str
    evidencia: list[EvidenciaSalida]
    estado: Literal['ENCONTRADO', 'CONTRADICE', 'SIN MATERIAL']

    @field_validator('nota', mode='before')
    @classmethod
    def _nota(cls, v):
        return desescapar(v)

    @field_validator('estado', mode='before')
    @classmethod
    def _normalizar_estado(cls, v):
        # El enum no garantiza mayúsculas después de un espacio ("SIN Material"): se normaliza.
        return re.sub(r'[\s_]+', ' ', str(v).strip().upper())

    @field_validator('evidencia')
    @classmethod
    def _sin_duplicados(cls, v):
        vistas, unicas = set(), []
        for e in v:
            clave = (e.documento_id, e.pagina, normalizar(e.fragmento))
            if clave not in vistas:
                vistas.add(clave)
                unicas.append(e)
        return unicas

    @model_validator(mode='after')
    def _coherencia(self):
        if not self.nota.strip():
            raise ValueError('La nota está vacía: explicá en 1 a 3 oraciones por qué decidiste el estado.')
        if self.estado == 'SIN MATERIAL' and self.evidencia:
            raise ValueError('Con SIN MATERIAL la evidencia va vacía.')
        if self.estado != 'SIN MATERIAL' and not self.evidencia:
            raise ValueError(f'Con {self.estado} tenés que citar al menos un fragmento literal de la prueba.')
        if len(self.evidencia) > MAX_CITAS:
            raise ValueError(f'Máximo {MAX_CITAS} fragmentos; mandaste {len(self.evidencia)}. Dejá solo los que deciden.')
        return self


# Guarda: el schema de la tool y el modelo pydantic tienen los mismos campos, EN EL MISMO ORDEN.
assert list(TOOL_REGISTRAR_VEREDICTO['input_schema']['properties']) == list(RegistroVeredicto.model_fields)