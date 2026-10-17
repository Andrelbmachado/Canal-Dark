"""Modelos de dados compartilhados entre os módulos."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Literal
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, Field, field_validator, model_validator


class State(str, Enum):
    descoberto = "descoberto"
    baixando = "baixando"
    baixado = "baixado"
    enviando = "enviando"
    no_drive = "no_drive"
    separado = "separado"
    descendo = "descendo"
    pronto = "pronto"
    editando = "editando"
    revisar = "revisar"
    editado = "editado"
    publicando = "publicando"
    publicado = "publicado"
    arquivando = "arquivando"
    arquivado = "arquivado"
    erro = "erro"
    descartado = "descartado"


Tone = Literal["serio", "meme"]
Platform = Literal["youtube", "instagram", "facebook", "tiktok"]
PLATFORMS = ("youtube", "instagram", "facebook", "tiktok")


@dataclass(frozen=True)
class WordTiming:
    word: str
    start: float
    end: float


class VideoRow(BaseModel):
    id: int
    canal: str
    url_origem: str
    titulo_origem: str
    url_midia: str | None = None  # arquivo direto (Pexels/Pixabay)
    autor: str = ""  # já formatado: "@GaiaMemes" ou "Ana · Pexels"
    fonte: str = ""  # rótulo da fonte (SourceSpec.rotulo())
    fonte_autorizada: bool = False
    tom: Tone = "serio"
    exige_cc: bool = False
    estado: State
    etapa_progresso: float = 0
    erro: str | None = None
    fila_erro: State | None = None
    nota: float = 0
    duracao: float | None = None
    estilo: str | None = None
    manchete: str | None = None
    caminho_local: str | None = None
    caminho_capa: str | None = None
    caminho_final: str | None = None
    remoto_bruto: str | None = None
    remoto_final: str | None = None
    pacote: int | None = None
    destinos: str | None = None  # JSON list[{"plataforma","conta"}]; None = padrão do canal
    criado_em: str
    atualizado_em: str


# ---- Configuração -----------------------------------------------------------

_URL_SOURCES = ("youtube_canal", "tiktok_perfil", "instagram_perfil")


class SourceSpec(BaseModel):
    tipo: Literal["youtube_canal", "youtube_busca", "tiktok_perfil", "instagram_perfil", "pexels", "pixabay"]
    url: str | None = None
    busca: str | None = None
    so_creative_commons: bool = False
    tom: Tone = "serio"
    autorizado: bool = False

    @model_validator(mode="after")
    def _required_field(self):
        need = "url" if self.tipo in _URL_SOURCES else "busca"
        if not (getattr(self, need) or "").strip():
            raise ValueError(f"fonte {self.tipo} precisa do campo '{need}'")
        return self

    def rotulo(self) -> str:
        if self.tipo == "youtube_busca":
            return f"busca: {self.busca.strip()}"
        if self.tipo in ("pexels", "pixabay"):
            return f"{self.tipo.capitalize()}: {self.busca.strip()}"
        parts = [unquote(p) for p in urlsplit(self.url.strip()).path.split("/") if p]
        handles = [p for p in parts if p.startswith("@")]
        if handles:
            return handles[-1]
        if self.tipo == "instagram_perfil" and parts:  # instagram.com/<perfil>/ não usa @ no link
            return "@" + parts[0]
        return parts[-1] if parts else self.url.strip()


class ChannelConfig(BaseModel):
    id: str
    nome: str
    intervalo_captacao_min: int = 30
    captar_por_dia: int = 20
    estoque_max: int = 60
    fontes: list[SourceSpec]
    palavras_chave: list[str] = Field(default_factory=list)
    palavras_proibidas: list[str] = Field(default_factory=list)
    estilos: dict[Tone, dict[str, float]]
    manchete_padrao: list[str]
    texto_base: str = ""
    horarios: list[str]
    max_por_dia: int = 4
    contas: dict[Platform, str] = Field(default_factory=dict)
    hashtags: list[str] = Field(default_factory=list)
    regras: Literal["padrao", "politica"] = "padrao"
    pausa_eleitoral: bool = True
    revisar_antes_de_publicar: bool = False
    cookies_navegador: str | None = None
    tiktok_modo: Literal["rascunho", "publicar"] = "rascunho"

    @field_validator("estilos")
    @classmethod
    def _style_tables(cls, v):
        missing = [t for t in ("serio", "meme") if t not in v]
        if missing:
            raise ValueError(f"estilos precisa das tabelas serio e meme (falta: {', '.join(missing)})")
        for tone, table in v.items():
            total = sum(table.values())
            if abs(total - 1.0) > 0.01 + 1e-9:
                raise ValueError(f"os pesos de estilos.{tone} somam {total:.2f}, precisam somar 1.0")
        return v

    @field_validator("manchete_padrao")
    @classmethod
    def _fallback_headlines(cls, v):
        if not v or any(not s.strip() for s in v):
            raise ValueError("manchete_padrao precisa de pelo menos 1 frase, sem frases vazias")
        return v

    @field_validator("horarios")
    @classmethod
    def _times(cls, v):
        for t in v:
            m = re.fullmatch(r"(\d{2}):(\d{2})", t)
            if not m or int(m[1]) > 23 or int(m[2]) > 59:
                raise ValueError(f"horário inválido: {t}")
        return v


class DriveConfig(BaseModel):
    tipo: Literal["pasta", "rclone"] = "pasta"
    remote: str = "gdrive:"
    pasta: str = "CanalDark"
    pasta_local: str = "dados/drive"
    manter_originais: bool = False
    limite_uso: float = 0.9


class PlatformConfig(BaseModel):
    porta: int = 8001
    pacote: int = 15
    drive: DriveConfig = Field(default_factory=DriveConfig)
    tendencias_geo: str = "BR"


class StylePreset(BaseModel):
    nome: str
    rotulo: str
    tipo: Literal["faixa_cima", "faixa_dupla", "comentario", "borrado", "karaoke", "meme"]
    cor_faixa: str = "#FFFFFF"
    cor_texto: str = "#000000"
    alinhamento: Literal["centro", "esquerda"] = "centro"
    fonte_min: int = 56
    fonte_max: int = 76
    meme: str | None = None  # nome do memes/<meme>.yaml; obrigatório no tipo meme

    @model_validator(mode="after")
    def _meme_needs_template(self):
        if self.tipo == "meme" and not self.meme:
            raise ValueError("estilo do tipo meme precisa do campo 'meme'")
        return self


class MemeTemplate(BaseModel):
    nome: str
    clipe: str
    imagem_em: Literal["cima", "baixo"] = "cima"


# ---- Captação e publicação --------------------------------------------------


class Candidate(BaseModel):
    url: str
    titulo: str
    autor: str = ""
    fonte: str = ""
    duracao: float | None = None
    views: int | None = None
    publicado_em: datetime | None = None
    licenca: str | None = None
    direto: str | None = None
    capa: str | None = None


class Destination(BaseModel):
    plataforma: Platform
    conta: str
