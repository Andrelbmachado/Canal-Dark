# Canal Dark 🎬🤖

**Um canal "dark" no YouTube operado por IA: a partir de um vídeo de referência, a IA baixa, corta, edita e publica — sem intervenção.**

> **Status: URGENTE / em planejamento.** O repositório tem a especificação e o plano v1; o código ainda não existe.
> - Especificação: [docs/superpowers/specs](docs/superpowers/specs/2026-10-16-canal-dark-design.md)
> - Plano v1: [docs/superpowers/plans](docs/superpowers/plans/2026-10-16-canal-dark-v1.md)

## Como vai funcionar (planejado)
1. Você passa **um vídeo de referência** (link).
2. A IA entende o formato (tema, ritmo, duração, estilo de legenda).
3. Busca/baixa material, corta os melhores trechos, gera narração e legendas (PT/EN).
4. Renderiza em 9:16 (Shorts) ou 16:9.
5. Publica no YouTube via API, com título, descrição e tags.

## Primeiro passo
**Criar o canal no YouTube** (conta Google dedicada, nome, identidade visual) e liberar a YouTube Data API. Ver **[PLANO.md](PLANO.md)**.


> Este README e o PLANO.md foram reorganizados em 21/10/2026 a partir da visão do André. Itens marcados como **planejado** ainda não existem no código.
