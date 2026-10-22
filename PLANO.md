# PLANO — Canal Dark

> Roteiro executável do projeto. Cada fase tem um **objetivo**, um checklist e um **critério de pronto** verificável.
> Detalhes técnicos de cada tarefa (interfaces, testes, commits) estão no [plano de implementação v1](docs/superpowers/plans/2026-10-16-canal-dark-v1.md); as decisões de produto, na [especificação](docs/superpowers/specs/2026-10-16-canal-dark-design.md).

**Situação em 21/10/2026:** especificação e plano v1 prontos. Fase 0 não iniciada. Nenhum código escrito.

| Fase | Objetivo | Quem | Situação |
|---|---|---|---|
| 0 | Canal e contas de API prontos | André | ⏳ a fazer |
| 1 | v1 funcionando em modo ensaio (sem publicar) | Claude Code | 🔜 planejado |
| 2 | Primeiro vídeo publicado de verdade e rotina diária | André + Claude Code | 🔜 planejado |
| 3 | Mais canais (IA, política) | André + Claude Code | 🔜 planejado |
| 4 | Ideias além da v1 (backlog) | — | 💡 não especificado |

---

## Fase 0 — Canal e contas (André)

**Objetivo:** ter tudo o que a v1 precisa para publicar, sem depender de código.

- [ ] Criar conta Google dedicada ao projeto (não usar a pessoal).
- [ ] Criar o canal **GTA 6** no YouTube: nome, @handle, avatar, banner, descrição.
- [ ] Verificar o canal por telefone (libera miniaturas personalizadas e vídeos longos).
- [ ] Google Cloud: criar projeto, ativar **YouTube Data API v3**, tela de consentimento OAuth em **"In production"** e credencial OAuth do tipo *Desktop app*.
- [ ] Pedir a **auditoria de conformidade da YouTube API** (projetos não auditados só conseguem enviar vídeos como privados). Começar já, porque a resposta demora.
- [ ] Instagram: converter a conta para **Profissional (Business ou Criador)**, criar o app na Meta for Developers com a permissão `instagram_business_content_publish`.
- [ ] Listar 5–10 fontes do canal GTA 6 (busca do Google News, IDs de canais do YouTube, subreddits, RSS) e 3 vídeos de referência do estilo desejado.
- [ ] Instalar no PC: Python 3.11+, FFmpeg com libass, `cloudflared`, Claude Code logado; opcional: Ollama + `ollama pull qwen2.5:7b`.

**Pronto quando:** canal criado e verificado, `client_secret.json` salvo **fora do repositório**, app da Meta criado, lista de fontes anotada e `ffmpeg -version`, `cloudflared --version` e `claude --version` funcionando no terminal.

---

## Fase 1 — v1 em modo ensaio (Claude Code)

**Objetivo:** `canaldark run --ensaio` capta, edita e "publica" em `dados/ensaio/` sem tocar em nenhuma conta real.

Executar as tarefas do [plano v1](docs/superpowers/plans/2026-10-16-canal-dark-v1.md) em ordem, com TDD e um commit por tarefa, numa branch `feat/v1`:

| # | Tarefa | Entrega verificável |
|---|---|---|
| 1 | Esqueleto, SQLite e máquina de estados | `pytest tests/test_db.py` passa |
| 2 | YAML de canais e catálogo de sons | `canais/gta6.yaml` carrega; pesos inválidos dão erro citando o arquivo |
| 3 | Barramento de eventos e log | progresso limitado a 1 evento/250 ms |
| 4 | Regras de conteúdo e janela do TSE | testes de borda 22/10 00:00 e 26/10 23:59 passam |
| 5 | Cérebro: `claude -p` com reserva no Ollama | sem Claude Code, cai para Ollama; sem os dois, vídeo vai para `erro` sem travar |
| 6 | Captação (fontes + triagem + yt-dlp) | feeds de exemplo lidos; só o melhor item de cada grupo entra |
| 7 | Edição (Whisper, Kokoro, layout, FFmpeg) | saída 1080x1920 H.264 + AAC; clipe vertical e clipe mudo não quebram |
| 8 | Publicação (agenda, YouTube, Instagram, túnel) | APIs mockadas; reenvio não republica onde já foi publicado |
| 9 | Pipeline, recuperação e `canaldark run` | teste ponta a ponta em `--ensaio` termina em `publicado` |
| 10 | Painel web de 3 colunas | card atravessa as 3 colunas; screenshot em `docs/painel.png` |
| 11 | Chat com Claude Code via MCP | ações que publicam ou descartam pedem confirmação |
| 12 | Guia de instalação e contas | `pytest -v` inteiro passa; `docs/contas.md` escrito |

**Pronto quando:** `pytest -v` todo verde e `canaldark run --ensaio` gera pelo menos 3 MP4 em `dados/ensaio/` a partir das fontes reais do canal GTA 6, assistidos e aprovados pelo André.

---

## Fase 2 — Primeiro vídeo real e rotina (André + Claude Code)

**Objetivo:** o canal GTA 6 publicando sozinho, com supervisão leve.

- [ ] `canaldark auth youtube gta6` e `canaldark auth instagram gta6` (tokens no keyring).
- [ ] Publicar **1 vídeo manualmente aprovado** pelo painel e conferir no YouTube e no Instagram (título, descrição, hashtags, selo de conteúdo sintético quando aplicável).
- [ ] Rodar 7 dias com `max_por_dia: 4`, olhando a fila "revisar" uma vez por dia.
- [ ] Anotar problemas (manchetes ruins, cortes, voz) e ajustar `persona_roteiro`, `estilo_manchete` e fontes no YAML.

**Pronto quando:** 7 dias seguidos com publicações automáticas, nenhum vídeo preso em estado "-ando", nenhum aviso de direitos autorais ou de "conteúdo reutilizado".

---

## Fase 3 — Mais canais (André + Claude Code)

- [ ] Canal **IA**: `canais/ia.yaml` (Tecnoblog, Canaltech, Olhar Digital, Google News "inteligência artificial").
- [ ] Canal **política**: `canais/politica.yaml` com `regras: politica` (só fontes jornalísticas, sem meme, opinião vai para "revisar"). Não ligar antes de 27/10/2026, fim da janela do TSE.
- [ ] Repetir a Fase 0 (canal + contas) para cada novo canal.

**Pronto quando:** cada canal novo passa pelos mesmos 7 dias da Fase 2.

---

## Fase 4 — Backlog (fora da v1, ainda não especificado)

Ideias anotadas para depois. Nenhuma entra sem antes virar uma seção da especificação.

- **Modo "vídeo de referência":** colar o link de um vídeo e a IA copiar o formato (ritmo, duração, estilo de legenda) para os próximos vídeos.
- **Formato 16:9** além dos Shorts 9:16.
- **Legendas/tradução PT↔EN** offline para canais em inglês.
- **Firecrawl** para puxar pautas de sites sem RSS.
- **Clipes gerados por IA** (ex.: Wan2GP) numa fila noturna; precisa de GPU forte.
- **Métricas do YouTube Analytics** para ajustar temas, títulos e horários.
- **Teste de títulos com público simulado** (ex.: MiroFish).

---

## Riscos e como a v1 lida com eles

| Risco | Mitigação |
|---|---|
| Política de "conteúdo reutilizado" do YouTube | Todo vídeo tem manchete, narração/legenda e fonte; regra checada antes do render |
| Direitos autorais dos clipes | Cortes curtos com transformação; fonte citada; fila "revisar" para casos duvidosos |
| Uploads presos como privados | Pedir a auditoria da YouTube API já na Fase 0 |
| Claude Code fora do ar ou no limite de uso | Reserva no Ollama; se ambos falharem, vídeo vai para `erro` e a fila continua |
| Eleições (TSE) | Pausa automática de 22 a 26/10/2026 em canais com `regras: politica` |
| Token do Instagram expira | Conta pausada e aviso "reconectar" no painel; vídeo volta para a fila |
| Limites das plataformas | YouTube até 100 uploads/dia por projeto; Instagram até 25 posts/24 h por conta |
