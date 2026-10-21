# PLANO — Canal Dark (urgente)

## Fase 0 — Criar o canal (André)
- [ ] Conta Google dedicada + canal do YouTube (nome, nicho, avatar, banner).
- [ ] Verificar o canal (telefone) para liberar vídeos > 15 min e miniaturas.
- [ ] Projeto no Google Cloud com YouTube Data API v3 + OAuth (token de upload).
- [ ] Definir nicho e 3 vídeos de referência.

## Fase 1 — Pipeline mínimo (1 vídeo por dia)
- [ ] `ingest`: baixar vídeo de referência e materiais (yt-dlp), respeitando direitos/licenças.
- [ ] `analisar`: transcrição (faster-whisper) + frames a cada 2 s para extrair formato e ganchos.
- [ ] `roteiro`: prompt estruturado (#, ###, [ ], tabelas) gera roteiro e títulos.
- [ ] `cortar/editar`: ffmpeg (cortes, 9:16, legendas queimadas).
- [ ] `narrar`: TTS local (Piper) — voz PT-BR.
- [ ] `publicar`: upload via API, agendado.

## Fase 2 — Escala
- [ ] Legendas/tradução PT↔EN offline (Hy-MT2).
- [ ] Firecrawl para puxar pautas de sites sem RSS.
- [ ] Fila noturna: Claude escreve N prompts de cena → Wan2GP gera clipes (precisa de GPU forte; opcional).
- [ ] Painel web com status da fila.

## Fase 3 — Otimização
- [ ] Ler métricas (YouTube Analytics) e ajustar temas/títulos.
- [ ] Testes de público simulado (MiroFish) para títulos — opcional.

## Riscos
- Direitos autorais e política de "conteúdo reutilizado" do YouTube: usar material licenciado/transformado.
