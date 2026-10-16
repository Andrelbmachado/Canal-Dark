"""O "cérebro": Claude Code local (claude -p) com Ollama local de reserva. Nada pago."""
from __future__ import annotations

import asyncio
import json
import os
from email.utils import parsedate_to_datetime
from typing import TypeVar

import httpx
from pydantic import BaseModel, Field, ValidationError

from .models import ChannelConfig, Package, Sound, SourceItem, TriageResult
from .rules import prompt_rules

T = TypeVar("T", bound=BaseModel)
LIMIT_MARKERS = ("usage limit", "rate limit", "limit reached", "quota")


class BrainError(Exception):
    pass


class EngineUnavailable(Exception):
    pass


def extract_json(text: str) -> dict:
    """Primeiro objeto JSON completo dentro do texto (tolera cercas ``` e texto em volta)."""
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch == "{":
            try:
                obj, _ = decoder.raw_decode(text[i:])
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                return obj
    raise ValueError("nenhum objeto JSON na resposta")


async def _claude(prompt: str, timeout: float) -> str:
    binary = os.environ.get("CANALDARK_CLAUDE_BIN", "claude")
    try:
        proc = await asyncio.create_subprocess_exec(
            binary, "-p", prompt, "--output-format", "json",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, PermissionError) as e:
        raise EngineUnavailable(f"claude não encontrado ({binary})") from e
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError as e:
        proc.kill()
        raise EngineUnavailable("claude: tempo esgotado") from e
    text_err = err.decode(errors="replace")
    if proc.returncode != 0:
        kind = "limite de uso" if any(m in text_err.lower() for m in LIMIT_MARKERS) else "falhou"
        raise EngineUnavailable(f"claude {kind}: {text_err.strip()[:200]}")
    raw = out.decode(errors="replace")
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if envelope.get("is_error"):
        raise EngineUnavailable(f"claude erro: {str(envelope.get('result'))[:200]}")
    return str(envelope.get("result", raw))


async def _ollama(prompt: str, timeout: float) -> str:
    url = os.environ.get("CANALDARK_OLLAMA_URL", "http://localhost:11434") + "/api/generate"
    model = os.environ.get("CANALDARK_OLLAMA_MODEL", "qwen2.5:7b")
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(url, json={"model": model, "prompt": prompt, "format": "json", "stream": False})
            r.raise_for_status()
            return r.json()["response"]
    except (httpx.HTTPError, KeyError, ValueError) as e:
        raise EngineUnavailable(f"ollama indisponível: {e}") from e


ENGINES = (("claude", _claude), ("ollama", _ollama))


async def ask_json(prompt: str, schema: type[T], *, timeout: float = 180) -> T:
    """Pede um JSON que valide em `schema`. Tenta claude, depois ollama; repete uma vez se o JSON vier inválido."""
    reasons: list[str] = []
    for name, engine in ENGINES:
        current = prompt
        for attempt in range(2):
            try:
                text = await engine(current, timeout)
            except EngineUnavailable as e:
                reasons.append(f"{name}: {e}")
                break
            try:
                return schema.model_validate(extract_json(text))
            except (ValueError, ValidationError) as e:
                if attempt == 1:
                    reasons.append(f"{name}: JSON inválido duas vezes ({str(e)[:150]})")
                    break
                current = (f"{prompt}\n\nSua resposta anterior era JSON inválido para o schema: {str(e)[:500]}\n"
                           "Responda apenas com o objeto JSON corrigido.")
    raise BrainError("cérebro indisponível — " + " | ".join(reasons))


def _schema_hint(schema: type[BaseModel]) -> str:
    return json.dumps(schema.model_json_schema(), ensure_ascii=False)


async def make_package(cfg: ChannelConfig, title: str, description: str, transcript: str,
                       recent_headlines: list[str], sounds: list[Sound], *,
                       has_video: bool = True, instruction: str | None = None) -> Package:
    sound_lines = "\n".join(f"- {s.id}: {s.quando_usar} (momento: {s.momento})" for s in sounds) or "(nenhum)"
    formats = ", ".join(f"{k} {v:.0%}" for k, v in cfg.formatos.items() if v > 0)
    prompt = f"""Você edita Shorts/Reels do canal "{cfg.nome}" em português do Brasil.
Persona do roteiro: {cfg.persona_roteiro}
Estilo da manchete: {cfg.estilo_manchete}
Pesos de formato do canal: {formats}. {"Não há vídeo: use o formato dialogo." if not has_video else ""}
Personagens do formato dialogo: {", ".join(cfg.personagens) or "narrador"}

{prompt_rules(cfg)}

Notícia/clipe:
Título: {title}
Descrição: {description[:2000]}
Transcrição do clipe: {transcript[:3000] or "(sem fala)"}

Últimas manchetes do canal (não repita a estrutura): {json.dumps(recent_headlines[-20:], ensure_ascii=False)}
Sons disponíveis:
{sound_lines}
{f"Instrução do editor humano: {instruction}" if instruction else ""}

Escreva a manchete já quebrada em linhas de até 22 caracteres. Roteiro de 15 a 40 s falado (formato noticia),
diálogo de 30 a 60 s (formato dialogo) ou nada (meme). Responda SOMENTE com um objeto JSON neste schema:
{_schema_hint(Package)}"""
    return await ask_json(prompt, Package, timeout=PACKAGE_TIMEOUT)


PACKAGE_TIMEOUT = 360  # a 60 s dialogue script can take minutes on `claude -p`
TRIAGE_MAX = 30          # bigger prompts made `claude -p` take 5+ min on a 140-item Google News feed
TRIAGE_TIMEOUT = 420


class _TriageItem(BaseModel):
    id: int
    nota: int = Field(ge=0, le=10)
    grupo: str


class _TriageIds(BaseModel):
    itens: list[_TriageItem]


def _newest_first(items: list[SourceItem]) -> list[SourceItem]:
    def ts(i: SourceItem) -> float:
        try:
            return parsedate_to_datetime(i.published).timestamp() if i.published else 0.0
        except (TypeError, ValueError):
            return 0.0
    return sorted(items, key=ts, reverse=True)


async def triage(cfg: ChannelConfig, items: list[SourceItem]) -> list[TriageResult]:
    if not items:
        return []
    batch = _newest_first(items)[:TRIAGE_MAX]
    listing = "\n".join(f"- id: {n}\n  título: {i.title}\n  resumo: {i.summary[:200]}"
                        for n, i in enumerate(batch, 1))
    prompt = f"""Você faz a triagem de pauta do canal "{cfg.nome}" (público brasileiro).
Para cada item, dê uma nota de 0 a 10 de interesse para um Short hoje e um "grupo" curto que seja igual
para itens que falam do mesmo fato. Itens fora do tema do canal recebem nota 0.
{listing}
Responda SOMENTE com JSON no schema: {_schema_hint(_TriageIds)}"""
    resp = await ask_json(prompt, _TriageIds, timeout=TRIAGE_TIMEOUT)
    return [TriageResult(url=batch[r.id - 1].url, nota=r.nota, grupo=r.grupo)
            for r in resp.itens if 1 <= r.id <= len(batch)]
