from typing import Optional
from pydantic import BaseModel


class Documento(BaseModel):
    documento_id: str
    pagina: int


class Item(BaseModel):
    id: str
    titulo: Optional[str] = None
    grupo: Optional[str] = None
    texto: str
    origen: Documento


class Evidencia(BaseModel):
    documento_id: str
    pagina: int
    fragmento: str


class Veredicto(BaseModel):
    item_id: str
    estado: str
    evidencia: list[Evidencia]
    confianza: Optional[float] = None
    nota: str