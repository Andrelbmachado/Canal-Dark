"""Modelos de dados compartilhados entre os módulos."""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel


class State(str, Enum):
    descoberto = "descoberto"
    baixando = "baixando"
    baixado = "baixado"
    editando = "editando"
    editado = "editado"
    revisar = "revisar"
    publicando = "publicando"
    publicado = "publicado"
    erro = "erro"


class VideoRow(BaseModel):
    id: int
    canal: str
    url_origem: str
    titulo_origem: str
    fonte: str
    estado: State
    etapa_progresso: float = 0
    formato: str | None = None
    pacote_json: str | None = None
    caminho_bruto: str | None = None
    caminho_final: str | None = None
    erro: str | None = None
    etapa_erro: str | None = None
    nota: int | None = None
    instrucao: str | None = None
    criado_em: str
    atualizado_em: str
