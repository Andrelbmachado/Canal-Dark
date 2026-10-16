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


# ---- Configuração de canais -------------------------------------------------

import re
from typing import Literal

from pydantic import Field, field_validator, model_validator

FormatName = Literal["noticia", "meme", "dialogo"]


class SourceSpec(BaseModel):
    tipo: Literal["google_news", "youtube_canal", "reddit", "rss"]
    busca: str | None = None
    id: str | None = None
    sub: str | None = None
    url: str | None = None

    @model_validator(mode="after")
    def _required_field(self):
        need = {"google_news": "busca", "youtube_canal": "id", "reddit": "sub", "rss": "url"}[self.tipo]
        if not getattr(self, need):
            raise ValueError(f"fonte {self.tipo} precisa do campo '{need}'")
        return self


class VoiceSpec(BaseModel):
    motor: Literal["kokoro", "piper"] = "kokoro"
    id: str


class ChannelConfig(BaseModel):
    id: str
    nome: str
    idioma: str = "pt-BR"
    intervalo_captacao_min: int = 30
    nota_minima: int = 6
    max_por_dia: int = 4
    fontes: list[SourceSpec]
    formatos: dict[FormatName, float]
    estilo_manchete: str
    persona_roteiro: str
    voz: VoiceSpec
    personagens: list[str] = Field(default_factory=list)
    horarios: list[str]
    contas: dict[str, str]
    regras: Literal["padrao", "politica"] = "padrao"
    regras_extras: list[str] = Field(default_factory=list)
    cookies_navegador: str | None = None

    @field_validator("formatos")
    @classmethod
    def _weights(cls, v):
        if abs(sum(v.values()) - 1.0) > 0.01:
            raise ValueError(f"os pesos de formatos somam {sum(v.values()):.2f}, precisam somar 1.0")
        return v

    @field_validator("horarios")
    @classmethod
    def _times(cls, v):
        for t in v:
            m = re.fullmatch(r"(\d{2}):(\d{2})", t)
            if not m or int(m[1]) > 23 or int(m[2]) > 59:
                raise ValueError(f"horário inválido: {t}")
        return v


class Sound(BaseModel):
    id: str
    arquivo: str
    quando_usar: str
    momento: Literal["inicio", "final", "antes_revelacao"]


# ---- Pacote gerado pelo cérebro --------------------------------------------


class DialogLine(BaseModel):
    personagem: str
    fala: str


class Cut(BaseModel):
    inicio: float = 0
    fim: float | None = None


class Package(BaseModel):
    formato: FormatName
    manchete: list[str]
    roteiro: str | None = None
    dialogo: list[DialogLine] | None = None
    fonte: str
    som: str | None = None
    corte: Cut | None = None
    titulo_post: str
    descricao_post: str
    hashtags: list[str] = Field(default_factory=list)
    risco: Literal["ok", "revisar"] = "ok"
    motivo_risco: str | None = None
    sintetico_realista: bool = False


# ---- Captação ---------------------------------------------------------------


class SourceItem(BaseModel):
    url: str
    title: str
    summary: str = ""
    published: str | None = None
    source_name: str
    video_url: str | None = None


class TriageResult(BaseModel):
    url: str
    nota: int = Field(ge=0, le=10)
    grupo: str


class TriageResponse(BaseModel):
    itens: list[TriageResult]
