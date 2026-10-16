"""Regras de conteúdo (prompt + checagem em código) e calendário eleitoral."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .models import ChannelConfig, Package

SP = ZoneInfo("America/Sao_Paulo")
# 72 h antes a 24 h depois do 2º turno (25/10/2026), em dias inteiros de São Paulo
TSE_BLACKOUTS = [(datetime(2026, 10, 22, 0, 0, tzinfo=SP), datetime(2026, 10, 27, 0, 0, tzinfo=SP))]
MAX_HEADLINE = 60

COMMON_RULES = """Regras obrigatórias:
- Todo vídeo precisa de valor original: manchete própria, narração ou legendas que contam a história, e a fonte.
- Proibido: material vazado, vídeo de IA imitando jogo ou pessoa real, música inteira com direitos autorais, marca d'água de outra plataforma.
- Manchete com no máximo 60 caracteres no total, sem mentir sobre o conteúdo.
- Meme no máximo 1 a cada 3 vídeos.
- Se tiver qualquer dúvida sobre direitos, veracidade ou tom, marque risco = "revisar" e explique em motivo_risco."""

POLITICS_RULES = """Regras extras deste canal (política, eleições 2026):
- Só fontes jornalísticas e perfis oficiais; cite o veículo em "fonte".
- Nunca crie voz, imagem ou fala sintética de candidato.
- Formato meme proibido. Formato dialogo (opinião ou sátira) sempre com risco = "revisar".
- Tom factual, sem ofensa, sem afirmar fatos não confirmados."""


def in_tse_blackout(now: datetime) -> bool:
    local = now.astimezone(SP)
    return any(start <= local < end for start, end in TSE_BLACKOUTS)


def publish_allowed(cfg: ChannelConfig, now: datetime) -> bool:
    return not (cfg.regras == "politica" and in_tse_blackout(now))


def prompt_rules(cfg: ChannelConfig) -> str:
    parts = [COMMON_RULES]
    if cfg.regras == "politica":
        parts.append(POLITICS_RULES)
    if cfg.regras_extras:
        parts.append("Regras do canal:\n" + "\n".join(f"- {r}" for r in cfg.regras_extras))
    return "\n\n".join(parts)


def check(pkg: Package, cfg: ChannelConfig, recent_formats: list[str]) -> list[str]:
    v: list[str] = []
    if sum(len(line) for line in pkg.manchete) > MAX_HEADLINE:
        v.append(f"manchete com mais de {MAX_HEADLINE} caracteres")
    if not pkg.manchete or not any(line.strip() for line in pkg.manchete):
        v.append("manchete vazia")
    if not pkg.fonte.strip():
        v.append("sem fonte")
    if pkg.formato == "meme" and "meme" in recent_formats[-2:]:
        v.append("meme demais: no máximo 1 a cada 3 vídeos")
    if pkg.formato == "noticia" and not (pkg.roteiro or "").strip():
        v.append("notícia sem roteiro de narração")
    if pkg.formato == "dialogo" and not pkg.dialogo:
        v.append("diálogo sem falas")
    if pkg.risco == "revisar":
        v.append(f"marcado para revisar: {pkg.motivo_risco or 'sem motivo'}")
    if cfg.regras == "politica":
        if pkg.formato == "meme":
            v.append("política: meme proibido")
        if pkg.formato == "dialogo":
            v.append("política: opinião/sátira precisa de revisão")
        if pkg.sintetico_realista:
            v.append("política: conteúdo sintético realista proibido")
    return v
