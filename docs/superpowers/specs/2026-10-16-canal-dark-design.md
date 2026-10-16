# Especificação do Canal Dark

A especificação completa e atualizada é o documento colaborativo
[Especificação do Canal Dark](https://claude.ai/code/artifact/96c2ba3f-0e57-49ad-9ccf-0bb442d383ed),
aprovada em 16/10/2026. A pesquisa que embasa as decisões está em
[Canais dark automatizados](https://claude.ai/code/artifact/2e0af1f9-8995-424a-a758-0d27fa346644).

Resumo das decisões:

- Programa Python local (`canaldark run`), painel em `localhost:8000`.
- Pipeline em 3 etapas ligadas por SQLite: captação (RSS + yt-dlp) → edição
  (faster-whisper + cérebro + Kokoro + FFmpeg) → publicação (APIs gratuitas do
  YouTube e do Instagram).
- **Nada pago.** O cérebro é o Claude Code do PC do usuário (`claude -p`), com
  Ollama local de reserva. Instagram busca o vídeo por Cloudflare Tunnel gratuito.
- Formatos: notícia narrada, meme e explicação em diálogo, sempre com valor
  original (manchete, narração/legenda, fonte) para não cair em "reused content".
- Painel com 3 colunas (captação, edição, publicação), barras de progresso,
  histórico com ✓, log ao vivo e chat com o Claude Code via MCP local.
- Canal de política: só fontes jornalísticas, sem IA de candidato, pausa
  automática na janela do TSE (22 a 26/10/2026) e fila "revisar".
- Primeiro canal: GTA 6; IA e política entram só com YAML.
