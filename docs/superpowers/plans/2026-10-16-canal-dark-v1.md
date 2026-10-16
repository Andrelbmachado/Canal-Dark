# Canal Dark v1 — Plano de implementação

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Objetivo:** um programa Python local que capta notícias/clipes, gera Shorts 9:16 com valor original (manchete, narração, legendas, fonte) e publica sozinho no YouTube e Instagram, com painel web de 3 colunas, log e chat.

**Arquitetura:** um processo asyncio com quatro módulos (`capture`, `edit`, `publish`, `web`) que só conversam pelo SQLite: cada etapa pega vídeos num estado de entrada e grava o de saída. O "cérebro" (manchete, roteiro, triagem, chat) é o Claude Code do PC do usuário chamado por `claude -p`, com Ollama local de reserva. O painel recebe eventos por WebSocket.

**Tech stack:** Python 3.11+, FastAPI + uvicorn, aiosqlite, pydantic v2, PyYAML, feedparser, httpx, yt-dlp (lib), faster-whisper, kokoro (TTS), FFmpeg/ffprobe (subprocess), google-api-python-client + google-auth-oauthlib, keyring, mcp (SDK Python, servidor stdio), pytest + pytest-asyncio. Frontend: HTML/CSS/JS puro, sem build.

**Spec:** [Especificação do Canal Dark](https://claude.ai/code/artifact/96c2ba3f-0e57-49ad-9ccf-0bb442d383ed) (resumo e link em `docs/superpowers/specs/2026-10-16-canal-dark-design.md`). Pesquisa: [Canais dark automatizados](https://claude.ai/code/artifact/2e0af1f9-8995-424a-a758-0d27fa346644).

## Global Constraints

- **Nada pago:** nenhuma API paga, SaaS ou assinatura. Nenhuma dependência que exija chave paga. O cérebro é `claude -p` (plano do usuário) com fallback Ollama.
- Roda local (macOS ou Linux), Python 3.11+, FFmpeg com libass. Comando único: `canaldark run` sobe as três etapas e o painel em `localhost:8000`.
- Estados do vídeo (valores exatos, em português): `descoberto`, `baixando`, `baixado`, `editando`, `editado`, `revisar`, `publicando`, `publicado`, `erro`.
- Saída de vídeo: MP4 H.264 + AAC, 1080x1920, `+faststart`. Barras brancas em cima e embaixo; manchete preta na barra de cima, até 60 caracteres, quebrada em linhas.
- Formatos (valores exatos): `noticia` (15–40 s), `meme` (7–15 s), `dialogo` (30–60 s).
- Concorrência padrão: 2 downloads, 1 render, 1 upload simultâneos.
- Fuso de todas as regras de calendário: `America/Sao_Paulo`. Janela TSE do 2º turno: de 2026-10-22 00:00 a 2026-10-26 23:59 (72 h antes a 24 h depois do turno de 25/10), aplicada só a canais com `regras: politica`.
- Limites de publicação: YouTube até 100 uploads/dia por projeto; Instagram no máximo 25 posts/24 h por conta (limite próprio, abaixo dos 100 da Meta).
- Segredos (tokens OAuth) ficam no keyring do sistema, nunca no repositório nem no SQLite.
- Textos do painel e das mensagens em português do Brasil. Código, nomes de função e commits em inglês.

## Review Focus

1. **Clipe já vertical ou quadrado:** a barra de cima nunca pode ter menos de 420 px; um clipe 9:16 deve ser reduzido para caber na caixa 1080x1080, nunca esticado. → teste em Task 7.
2. **Clipe sem trilha de áudio** (GIF convertido, vídeo mudo): o render não pode quebrar; a narração vira a única trilha. → teste em Task 7.
3. **Claude Code ausente, deslogado ou no limite de uso:** o cérebro deve cair para o Ollama e, se o Ollama também falhar, mandar o vídeo para `erro` com mensagem clara, sem travar a fila. → testes em Task 5.
4. **Manchete com acento, emoji ou palavra maior que a linha:** a quebra não pode cortar palavra no meio nem perder caracteres; emoji é removido antes do drawtext. → teste em Task 7.
5. **Programa fechado no meio de um download/render/upload:** ao reabrir, nada fica preso em estado "-ando". → teste em Task 9.

---

## Estrutura de arquivos

```text
pyproject.toml                 dependências, entry point canaldark = canaldark.cli:main
canais/gta6.yaml               canal de exemplo (spec, seção Configuração)
sons/catalogo.yaml             catálogo de efeitos (vazio no início, com exemplo comentado)
personagens/                   imagens dos personagens do formato dialogo
fontes/                        Montserrat-ExtraBold.ttf (OFL)
canaldark/
  cli.py                       argparse: run [--ensaio], auth youtube|instagram <conta>
  db.py                        schema, conexões, transições de estado
  models.py                    pydantic: ChannelConfig, Sound, Package, VideoRow
  config.py                    carrega canais/*.yaml e sons/catalogo.yaml
  events.py                    EventBus (pub/sub asyncio) + grava tabela eventos
  rules.py                     regras de conteúdo e calendário
  brain.py                     claude -p / Ollama, validação de JSON
  capture/sources.py           leitores de fonte -> SourceItem
  capture/download.py          yt-dlp com progresso
  capture/stage.py             ciclo de captação
  edit/transcribe.py           faster-whisper
  edit/tts.py                  Kokoro -> wav + tempos por palavra
  edit/layout.py               altura da barra, quebra da manchete, ASS
  edit/render.py               montagem do comando FFmpeg e progresso
  edit/stage.py                orquestra a edição de um vídeo
  publish/schedule.py          próximo horário livre por canal
  publish/youtube.py           upload resumível
  publish/instagram.py         Reels via URL pública
  publish/tunnel.py            cloudflared quick tunnel servindo dados/finais
  publish/stage.py             fila de publicação
  pipeline.py                  workers, concorrência, retry, recuperação
  web/app.py                   FastAPI, REST, WebSocket
  web/static/index.html|app.js|style.css
  chat/mcp_server.py           servidor MCP stdio com as ferramentas do chat
  chat/bridge.py               ponte painel <-> claude -p --resume
tests/                         um arquivo por módulo + tests/fixtures/clip10s.mp4 (gerado)
```

---

### Task 1: Esqueleto, banco e transições de estado

**Files:**
- Create: `pyproject.toml`, `canaldark/__init__.py`, `canaldark/cli.py`, `canaldark/db.py`, `canaldark/models.py`
- Test: `tests/test_db.py`, `tests/conftest.py`

**Interfaces:**
- Produces:
  - `State` (enum str) com os 9 estados das Global Constraints.
  - `ALLOWED: dict[State, set[State]]` — `descoberto→{baixando,erro}`, `baixando→{baixado,erro,descoberto}`, `baixado→{editando,erro}`, `editando→{editado,revisar,erro,baixado}`, `revisar→{editado,erro}`, `editado→{publicando,erro}`, `publicando→{publicado,erro,editado}`, `erro→{descoberto,baixado,editado}` (tentar de novo volta ao estado de entrada da etapa que falhou).
  - `async def open_db(path: Path) -> aiosqlite.Connection` (cria schema se faltar; WAL).
  - `async def insert_video(db, channel: str, url: str, title: str, source: str) -> int | None` (None se `vistos` já tem o hash da URL normalizada).
  - `async def transition(db, video_id: int, to: State, *, error: str | None = None) -> None` — levanta `InvalidTransition` se `to` não está em `ALLOWED[atual]`.
  - `async def claim(db, from_state: State, to_state: State, channel: str | None = None) -> VideoRow | None` — pega o mais antigo atomicamente (`UPDATE ... RETURNING`).
  - `async def set_progress(db, video_id: int, pct: float) -> None`.
  - `async def update_fields(db, video_id: int, **fields) -> None` (colunas da tabela `videos`).
  - Tabelas exatamente como na spec: `videos`, `publicacoes`, `vistos`, `eventos`.
  - `normalize_url(url: str) -> str` (minúsculas no host, remove utm_*, fragmento e barra final).

- [ ] **Step 1: Testes que falham**

```python
async def test_insert_dedup(db):
    a = await insert_video(db, "gta6", "https://x.com/a?utm_source=t", "T", "rss")
    b = await insert_video(db, "gta6", "https://X.com/a/", "T", "rss")
    assert a is not None and b is None

async def test_invalid_transition(db):
    vid = await insert_video(db, "gta6", "https://x.com/b", "T", "rss")
    with pytest.raises(InvalidTransition):
        await transition(db, vid, State.publicado)

async def test_claim_moves_oldest(db):
    v1 = await insert_video(db, "gta6", "https://x.com/1", "1", "rss")
    await insert_video(db, "gta6", "https://x.com/2", "2", "rss")
    row = await claim(db, State.descoberto, State.baixando)
    assert row.id == v1 and row.estado == State.baixando
```

- [ ] **Step 2:** `pytest tests/test_db.py -v` → FAIL (módulo inexistente).
- [ ] **Step 3:** Implementar `db.py`/`models.py` com as interfaces acima; `cli.py` com subcomando `run` ainda vazio. `pyproject.toml` com todas as dependências do tech stack e `asyncio_mode = "auto"`.
- [ ] **Step 4:** `pytest tests/test_db.py -v` → PASS.
- [ ] **Step 5:** `git commit -m "feat: project skeleton, sqlite schema and state machine"`

---

### Task 2: Configuração de canais e catálogo de sons

**Files:**
- Create: `canaldark/config.py`, `canais/gta6.yaml`, `sons/catalogo.yaml`
- Modify: `canaldark/models.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces:
  - `ChannelConfig` (pydantic) com os campos do YAML da spec: `id, nome, idioma, intervalo_captacao_min=30, nota_minima=6, max_por_dia=4, fontes: list[SourceSpec], formatos: dict[Literal["noticia","meme","dialogo"], float], estilo_manchete, persona_roteiro, voz: VoiceSpec, personagens: list[str], horarios: list[str "HH:MM"], contas: dict[str,str], regras: Literal["padrao","politica"]="padrao", regras_extras: list[str]=[], cookies_navegador: str | None = None`.
  - `SourceSpec`: `tipo: Literal["google_news","youtube_canal","reddit","rss"]` + `busca|id|sub|url` conforme o tipo (validador exige o campo certo).
  - `Sound`: `id, arquivo, quando_usar, momento: Literal["inicio","final","antes_revelacao"]`.
  - `def load_channels(dir: Path) -> dict[str, ChannelConfig]`; `def load_sounds(path: Path) -> list[Sound]`.
  - Validações: pesos de `formatos` somam 1.0 (±0.01); `horarios` válidos; erro de validação cita o arquivo.

- [ ] **Step 1: Testes que falham** — `test_load_gta6_example` (carrega `canais/gta6.yaml` do repo, `cfg.formatos["noticia"] == 0.6`, `cfg.regras == "padrao"`); `test_weights_must_sum_to_one` (YAML com 0.5/0.2/0.2 → `ValidationError` mencionando o nome do arquivo); `test_google_news_requires_busca`.
- [ ] **Step 2:** rodar → FAIL.
- [ ] **Step 3:** Implementar; `canais/gta6.yaml` é o exemplo da spec (seção "Configuração de um canal"), sem a linha de API paga.
- [ ] **Step 4:** rodar → PASS.
- [ ] **Step 5:** `git commit -m "feat: channel yaml config and sound catalog"`

---

### Task 3: Barramento de eventos e log

**Files:**
- Create: `canaldark/events.py`
- Test: `tests/test_events.py`

**Interfaces:**
- Produces:
  - `class EventBus` com `async def publish(self, type: str, data: dict) -> None` (grava em `eventos` e entrega a todos os assinantes) e `def subscribe(self) -> AsyncIterator[dict]` (fila por assinante, tamanho 1000, descarta o mais antigo se cheia).
  - Tipos exatos: `video.criado`, `video.progresso`, `video.estado`, `log`, `chat.mensagem`.
  - `async def log(self, stage: Literal["captacao","edicao","publicacao","sistema","chat"], msg: str, level: Literal["info","aviso","erro"]="info", video_id: int | None = None)`.
  - `video.progresso` é limitado a no máximo 1 evento a cada 250 ms por vídeo (throttle), sempre emitindo o 100%.

- [ ] **Step 1: Testes** — `test_subscriber_receives_event`; `test_progress_throttled` (50 chamadas em <250 ms → no máximo 2 eventos, o último com `pct == 100`); `test_events_persisted`.
- [ ] **Step 2:** FAIL. **Step 3:** implementar. **Step 4:** PASS.
- [ ] **Step 5:** `git commit -m "feat: event bus with throttled progress and persistent log"`

---

### Task 4: Regras de conteúdo e calendário

**Files:**
- Create: `canaldark/rules.py`
- Test: `tests/test_rules.py`

**Interfaces:**
- Consumes: `ChannelConfig` (Task 2), `Package` (Task 5 define; aqui use o modelo de `models.py`, criado nesta task).
- Produces:
  - `Package` (pydantic, em `models.py`): `formato, manchete: list[str]` (linhas), `roteiro: str | None`, `dialogo: list[DialogLine] | None` (`personagem, fala`), `fonte: str`, `som: str | None`, `corte: Cut | None` (`inicio, fim` em segundos), `titulo_post, descricao_post, hashtags: list[str], risco: Literal["ok","revisar"], motivo_risco: str | None`.
  - `def in_tse_blackout(now: datetime) -> bool` (janela das Global Constraints, timezone-aware).
  - `def prompt_rules(cfg: ChannelConfig) -> str` — texto das regras para o cérebro (tabela da seção Regras da spec + `regras_extras`).
  - `def check(pkg: Package, cfg: ChannelConfig, recent_formats: list[str]) -> list[str]` — lista de violações; vazia = pode publicar. Verifica: manchete ≤ 60 caracteres somando linhas; `fonte` não vazia; `formato == "meme"` proibido se já há 1 meme nos últimos 2 itens de `recent_formats`; política: formato `meme` proibido, `dialogo` sempre vira violação "revisar opinião/sátira", `risco == "revisar"` sempre violação.
  - `def publish_allowed(cfg: ChannelConfig, now: datetime) -> bool` — False para `regras == "politica"` dentro da janela TSE.

- [ ] **Step 1: Testes**

```python
SP = ZoneInfo("America/Sao_Paulo")
def test_blackout_edges():
    assert not in_tse_blackout(datetime(2026,10,21,23,59,tzinfo=SP))
    assert in_tse_blackout(datetime(2026,10,22,0,0,tzinfo=SP))
    assert in_tse_blackout(datetime(2026,10,26,23,59,tzinfo=SP))
    assert not in_tse_blackout(datetime(2026,10,27,0,0,tzinfo=SP))

def test_meme_ratio(pkg_meme, gta_cfg):
    assert check(pkg_meme, gta_cfg, ["noticia", "meme"])  # violação
    assert not check(pkg_meme, gta_cfg, ["noticia", "noticia"])

def test_politics_blocks_publish_in_window(pol_cfg):
    assert not publish_allowed(pol_cfg, datetime(2026,10,24,12,tzinfo=SP))
```

- [ ] **Step 2:** FAIL. **Step 3:** implementar. **Step 4:** PASS.
- [ ] **Step 5:** `git commit -m "feat: content rules and TSE blackout window"`

---

### Task 5: Cérebro (Claude Code local + Ollama)

**Files:**
- Create: `canaldark/brain.py`
- Test: `tests/test_brain.py`

**Interfaces:**
- Consumes: `Package`, `prompt_rules` (Task 4).
- Produces:
  - `class BrainError(Exception)`.
  - `async def ask_json(prompt: str, schema: type[BaseModel], *, timeout: float = 180) -> BaseModel` — tenta `claude -p <prompt> --output-format json` (lê o campo `result`, extrai o primeiro objeto JSON e valida com `schema`); se o binário falta, sai com código ≠ 0, a saída menciona limite de uso, ou estoura o timeout → tenta Ollama (`POST http://localhost:11434/api/generate`, `format: "json"`, modelo de `CANALDARK_OLLAMA_MODEL`, padrão `qwen2.5:7b`). JSON inválido em qualquer motor: repete uma vez com a mensagem de validação anexada. Tudo falhou → `BrainError` com o motivo de cada motor.
  - `async def make_package(cfg, title, description, transcript, recent_headlines: list[str], sounds: list[Sound]) -> Package`.
  - `async def triage(cfg, items: list[SourceItem]) -> list[TriageResult]` (`url, nota: int 0–10, grupo: str`) — agrupa notícias do mesmo fato.
  - O executável é configurável por `CANALDARK_CLAUDE_BIN` (padrão `claude`), para os testes usarem um script falso.

- [ ] **Step 1: Testes** (com `tests/fakes/fake_claude.sh` e `respx` para o Ollama)
  - `test_uses_claude_when_available` — fake devolve `{"result": "<json do pacote>"}` → `Package` válido, Ollama não chamado.
  - `test_falls_back_to_ollama_when_claude_missing` — `CANALDARK_CLAUDE_BIN=/nao/existe`.
  - `test_falls_back_on_usage_limit` — fake sai com código 1 e "usage limit" no stderr.
  - `test_retries_once_on_invalid_json` — primeira resposta inválida, segunda válida → sucesso com 2 chamadas.
  - `test_brain_error_when_both_fail` — mensagem contém "claude" e "ollama".
- [ ] **Step 2:** FAIL. **Step 3:** implementar com `asyncio.create_subprocess_exec` (nunca shell). **Step 4:** PASS.
- [ ] **Step 5:** `git commit -m "feat: brain via local claude code with ollama fallback"`

---

### Task 6: Captação

**Files:**
- Create: `canaldark/capture/sources.py`, `canaldark/capture/download.py`, `canaldark/capture/stage.py`
- Test: `tests/test_capture.py`, `tests/fixtures/feeds/*.xml`

**Interfaces:**
- Consumes: `insert_video`, `claim`, `transition`, `set_progress`, `update_fields` (Task 1), `EventBus` (Task 3), `triage` (Task 5).
- Produces:
  - `SourceItem` (dataclass): `url, title, summary, published, source_name, video_url: str | None`.
  - `async def read_source(spec: SourceSpec, client: httpx.AsyncClient) -> list[SourceItem]` — URLs: Google News `https://news.google.com/rss/search?q=<busca>&hl=pt-BR&gl=BR&ceid=BR:pt-419`; YouTube `https://www.youtube.com/feeds/videos.xml?channel_id=<id>` (video_url = link); Reddit `https://www.reddit.com/r/<sub>/.rss`; `rss` genérico.
  - `async def capture_cycle(db, bus, cfg) -> int` — lê todas as fontes, chama `triage`, insere os de `nota >= cfg.nota_minima` (um por `grupo`, o de maior nota), respeitando `max_por_dia`; retorna quantos inseriu. Item sem `video_url` é inserido já com `formato = "dialogo"` e vai direto para `baixado` (não há download).
  - `async def download(row: VideoRow, cfg, out_dir: Path, on_progress: Callable[[float], Awaitable[None]]) -> Path` — yt-dlp em thread (`asyncio.to_thread`), opções: `format "bv*[height<=1080]+ba/b[height<=1080]"`, `merge_output_format mp4`, `writeinfojson True`, `download_archive dados/archive.txt`, `cookiesfrombrowser` se o canal configurar; hook de progresso convertido para 0–100. 3 tentativas com espera 5 s, 20 s, 60 s.
  - `async def download_worker(db, bus, cfgs)` — loop: `claim(descoberto→baixando)` → `download` → `update_fields(caminho_bruto=...)` → `transition(baixado)`; falha final → `erro`.

- [ ] **Step 1: Testes** — `test_parse_google_news_fixture`; `test_parse_youtube_feed_sets_video_url`; `test_cycle_keeps_best_of_group` (triage falso agrupando 2 itens; só 1 inserido); `test_item_without_video_goes_to_dialogo`; `test_download_progress_reaches_100` (yt-dlp mockado chamando o hook com downloaded/total).
- [ ] **Step 2:** FAIL. **Step 3:** implementar. **Step 4:** PASS.
- [ ] **Step 5:** `git commit -m "feat: capture sources, triage and yt-dlp download"`

---

### Task 7: Edição (transcrição, voz, layout, render)

**Files:**
- Create: `canaldark/edit/transcribe.py`, `edit/tts.py`, `edit/layout.py`, `edit/render.py`, `edit/stage.py`, `fontes/Montserrat-ExtraBold.ttf`
- Test: `tests/test_layout.py`, `tests/test_render.py`, `tests/test_edit_stage.py`, `tests/fixtures/make_clip.sh`

**Interfaces:**
- Consumes: `make_package` (Task 5), `check` (Task 4), estado/eventos (Tasks 1, 3).
- Produces:
  - `def bar_height(src_w: int, src_h: int) -> int` — vídeo encaixado numa caixa 1080x1080 (`decrease`); barra = `(1920 - altura_encaixada) // 2`; nunca < 420.
  - `def wrap_headline(text: str, max_chars: int = 22) -> list[str]` — remove emoji, quebra por palavra (`textwrap`, `break_long_words=False`), no máximo 3 linhas; se passar de 3, reduz e sinaliza via `ValueError` para o cérebro refazer.
  - `def font_size_for(lines: list[str], bar: int) -> int` — começa em 76, desce de 4 em 4 até caber em `bar - 60` px de altura (altura de linha ≈ 1.25 × tamanho), mínimo 44.
  - `def build_ass(words: list[WordTiming], style: Literal["palavra"]) -> str` — legendas ASS brancas com contorno, 1–3 palavras por evento.
  - `async def transcribe(path: Path) -> Transcript` (`text`, `words: list[WordTiming]`), faster-whisper `small`, `int8`, idioma `pt`. Sem trilha de áudio → `Transcript("", [])`.
  - `async def synthesize(text: str, voice: str, out: Path) -> list[WordTiming]` (Kokoro; tempos por palavra estimados por duração de sílabas quando o motor não fornecer).
  - `def build_ffmpeg_cmd(spec: RenderSpec) -> list[str]` — `RenderSpec`: `src: Path | None` (None no formato dialogo), `cut, headline_lines, font, font_size, bar, bottom_text, ass_path, narration: Path | None, sound: Path | None, sound_at: float | None, out: Path, image_layers: list[ImageLayer]` (dialogo). Filtros conforme a receita testada da pesquisa (scale 1080:1080 decrease, pad 1080:1920 branco, drawtext com `textfile`, `text_align=C`, `y=(bar-text_h)/2`, barra de baixo `y=h-200`, `subtitles`), mixagem `amix` com o áudio original a -14 dB sob narração (sidechain simples por volume fixo na v1), `-progress pipe:1`.
  - `async def render(spec: RenderSpec, on_progress) -> Path` — lê `out_time_ms` do pipe e converte em % pela duração alvo.
  - `async def edit_video(db, bus, cfgs, sounds, row) -> State` — fluxo da seção Edição da spec: transcrever → pacote (com as últimas 20 manchetes do canal) → `check` → voz → render → `editado`, ou `revisar` se `check` retornar violações (grava-as em `erro`) ou `risco == "revisar"`.
  - `async def edit_worker(...)` — `claim(baixado→editando)`.

- [ ] **Step 1: Testes de layout**

```python
def test_bar_for_16x9(): assert bar_height(1920, 1080) == 656
def test_bar_for_vertical_never_below_420(): assert bar_height(1080, 1920) == 420
def test_wrap_keeps_words_and_accents():
    lines = wrap_headline("Rockstar confirma: GTA 6 terá ação no Brasil? 🔥")
    assert "".join(lines).replace(" ", "") == "Rockstarconfirma:GTA6teráaçãonoBrasil?"
    assert all(len(l) <= 22 for l in lines)
```

- [ ] **Step 2: Testes de render** (geram `clip10s.mp4` 1280x720 com `ffmpeg -f lavfi testsrc` + `sine`, e `mute.mp4` sem áudio)
  - `test_render_output_is_1080x1920_with_audio` — ffprobe confere 1080x1920, h264, faixa aac.
  - `test_render_vertical_source` — fonte 1080x1920 → saída 1080x1920 sem erro.
  - `test_render_without_source_audio` — `mute.mp4` + narração → sucesso, 1 faixa de áudio.
  - `test_progress_reaches_100`.
- [ ] **Step 3: Teste do estágio** — `test_violation_goes_to_revisar` (cérebro falso devolvendo `risco="revisar"` → estado `revisar`, nada renderizado).
- [ ] **Step 4:** FAIL. **Step 5:** implementar. **Step 6:** PASS (`pytest tests/test_layout.py tests/test_render.py tests/test_edit_stage.py -v`).
- [ ] **Step 7:** `git commit -m "feat: edit stage with whisper, kokoro and ffmpeg 9:16 template"`

---

### Task 8: Publicação (agenda, YouTube, Instagram, túnel)

**Files:**
- Create: `canaldark/publish/schedule.py`, `publish/youtube.py`, `publish/instagram.py`, `publish/tunnel.py`, `publish/stage.py`
- Modify: `canaldark/cli.py` (subcomandos `auth youtube <conta>` e `auth instagram <conta>`)
- Test: `tests/test_schedule.py`, `tests/test_publish.py`

**Interfaces:**
- Consumes: `publish_allowed` (Task 4), estado/eventos.
- Produces:
  - `def next_slot(cfg, now: datetime, published_today: int, urgent: bool) -> datetime | None` — próximo `horarios` livre hoje ou amanhã; `None` se `max_por_dia` atingido hoje e amanhã sem slot (não acontece com config válida); `urgent=True` (nota ≥ 9) devolve `now`.
  - `class Uploader(Protocol)`: `async def upload(self, video: Path, title: str, description: str, tags: list[str], on_progress) -> PublishResult` (`id_externo, url`).
  - `YouTubeUploader(account: str)` — credenciais do keyring (`canaldark:youtube:<conta>`), `videos().insert(part="snippet,status", media_body=MediaFileUpload(resumable=True, chunksize=8MB))`, `privacyStatus="public"`, `selfDeclaredMadeForKids=False`, `containsSyntheticMedia` quando o pacote tiver voz/imagem sintética realista; progresso por `next_chunk()`. Token inválido → `AuthExpired`.
  - `InstagramUploader(account: str)` — token do keyring (`canaldark:instagram:<conta>`), `POST /{ig-user-id}/media` com `media_type=REELS`, `video_url` do túnel, polling de `status_code` até `FINISHED` (a cada 5 s, timeout 10 min), `POST /media_publish`. Progresso: 0–50 envio/processamento, 100 publicado. Respeita 25 posts/24 h contando `publicacoes`.
  - `class Tunnel` — `async def start(self, directory: Path) -> str` sobe `python -m http.server` local numa porta livre + `cloudflared tunnel --url http://localhost:<porta>` e devolve a URL `trycloudflare.com` lida do stderr; `async def stop()`. Só ativo enquanto houver Instagram pendente.
  - `async def publish_worker(db, bus, cfgs, uploaders)` — `claim(editado→publicando)` apenas quando `next_slot <= agora` e `publish_allowed`; publica nas plataformas do `contas` do canal; grava cada uma em `publicacoes`; plataforma já publicada não é reenviada no tentar de novo; `AuthExpired` → pausa a conta, evento `log` nível erro com "reconectar", vídeo volta para `editado`.
  - `--ensaio`: uploaders substituídos por `DryRunUploader` que copia o MP4 para `dados/ensaio/` e devolve `url` local.

- [ ] **Step 1: Testes** — `test_next_slot_skips_past_times`; `test_urgent_publishes_now`; `test_retry_skips_platform_already_published` (YouTube ok + Instagram falha → tentar de novo chama só Instagram); `test_auth_expired_returns_to_editado`; `test_instagram_daily_cap_25`; `test_politics_not_claimed_in_blackout`.
- [ ] **Step 2:** FAIL. **Step 3:** implementar (Google API e Graph API mockados nos testes). **Step 4:** PASS.
- [ ] **Step 5:** `git commit -m "feat: publish stage for youtube and instagram with schedule and tunnel"`

---

### Task 9: Pipeline, recuperação e comando run

**Files:**
- Create: `canaldark/pipeline.py`
- Modify: `canaldark/cli.py`
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: workers das Tasks 6–8, `EventBus`, `load_channels`, `load_sounds`.
- Produces:
  - `async def recover(db) -> int` — na inicialização: `baixando→descoberto`, `editando→baixado`, `publicando→editado`; zera progresso; retorna quantos recuperou e loga.
  - `class Pipeline` com `start()`, `stop()`, `pause(channel: str | None, stage: str | None, until: datetime | None)`, `resume(channel, stage)`, `paused: dict` — workers checam pausa antes de cada `claim`. Limites de concorrência das Global Constraints via `asyncio.Semaphore`.
  - Agendador de captação por canal no intervalo `intervalo_captacao_min` + `async def capture_now(channel: str, query: str | None)`.
  - `canaldark run [--ensaio] [--porta 8000]` — abre DB em `dados/canaldark.db`, `recover`, inicia pipeline e uvicorn no mesmo loop.

- [ ] **Step 1: Testes** — `test_recover_resets_in_progress_states` (3 vídeos em `baixando/editando/publicando` → voltam para `descoberto/baixado/editado`, progresso 0); `test_paused_stage_does_not_claim`; `test_end_to_end_dry_run` (fonte falsa com o `clip10s.mp4`, cérebro falso, `--ensaio`: um vídeo termina em `publicado` com arquivo em `dados/ensaio/`, e o bus emitiu `video.estado` para cada transição).
- [ ] **Step 2:** FAIL. **Step 3:** implementar. **Step 4:** PASS.
- [ ] **Step 5:** `git commit -m "feat: pipeline runner, crash recovery and run command"`

---

### Task 10: Painel web

**Files:**
- Create: `canaldark/web/app.py`, `web/static/index.html`, `web/static/app.js`, `web/static/style.css`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `Pipeline`, `EventBus`, DB.
- Produces (REST, JSON):
  - `GET /api/videos?stage=captacao|edicao|publicacao&channel=&before_id=&limit=30` — cards da coluna (mais novos primeiro, paginação por `before_id`). Coluna de um vídeo: captação = estados até `baixado`; edição = `editando`, `editado`, `revisar` e os que passaram por ela; publicação = `publicando`, `publicado`. Um vídeo aparece em todas as colunas por onde passou, com ✓ nas concluídas (histórico).
  - `GET /api/videos/{id}` — detalhe com pacote, caminhos, publicações, erro.
  - `GET /media/{id}/final.mp4` e `/media/{id}/thumb.jpg` (thumb gerada no fim do render, frame 0).
  - `POST /api/videos/{id}/retry`, `/approve`, `/discard`, `/publish-now`.
  - `POST /api/pause` e `/api/resume` (`channel?`, `stage?`, `until?`).
  - `WS /ws` — envia os eventos do bus; no conectar, envia os últimos 200 `log`.
- Frontend: layout da seção Painel da spec (topo com seletor e pausas, 3 colunas, rodapé com log à esquerda e chat à direita), tema escuro, cards com barra de progresso, ✓ verde / ✗ vermelho + botão "Tentar de novo", modal de detalhe com player e botões Aprovar/Descartar para `revisar`. Rolagem infinita por `before_id`. Sem frameworks.

- [ ] **Step 1: Testes** (FastAPI TestClient) — `test_columns_show_history` (vídeo `publicado` aparece nas três colunas com `done: true`); `test_retry_from_erro`; `test_ws_streams_events`; `test_static_index_served`.
- [ ] **Step 2:** FAIL. **Step 3:** implementar. **Step 4:** PASS.
- [ ] **Step 5:** Verificação manual: `canaldark run --ensaio` com a fonte de teste; abrir `localhost:8000` e ver um card atravessar as 3 colunas com barras e ✓. Capturar um screenshot (Playwright) em `docs/painel.png`.
- [ ] **Step 6:** `git commit -m "feat: realtime three-column web panel"`

---

### Task 11: Chat com o Claude Code

**Files:**
- Create: `canaldark/chat/mcp_server.py`, `canaldark/chat/bridge.py`
- Modify: `canaldark/web/app.py` (`POST /api/chat`), `web/static/app.js`
- Test: `tests/test_chat.py`

**Interfaces:**
- Consumes: as mesmas funções da Task 10 (via HTTP local para o servidor MCP, que roda como processo separado iniciado pelo `claude`).
- Produces:
  - Servidor MCP stdio `canaldark-mcp` com as ferramentas da tabela da spec: `buscar_agora(canal, consulta?)`, `refazer(video_id, campo: "manchete"|"roteiro"|"formato", instrucao)`, `pausar(canal?, etapa?, ate?)`, `retomar(canal?, etapa?)`, `aprovar(video_id)`, `descartar(video_id)`, `publicar_agora(video_id)`, `status(canal?)`, `adicionar_fonte(canal, url)`. Cada uma chama `http://localhost:<porta>/api/...`.
  - `refazer` grava a instrução, volta o vídeo para `baixado` e o editor inclui a instrução no prompt do pacote.
  - `class ChatBridge` — `async def send(self, text: str) -> AsyncIterator[str]` roda `claude -p <texto> --output-format stream-json --mcp-config dados/mcp.json --allowedTools "mcp__canaldark__status,mcp__canaldark__buscar_agora,mcp__canaldark__pausar,mcp__canaldark__retomar,mcp__canaldark__refazer"` com `--resume <session_id>` a partir da segunda mensagem; ferramentas que publicam ou descartam (`aprovar`, `descartar`, `publicar_agora`, `adicionar_fonte`) não estão em `allowedTools`: quando o Claude pedir, o painel mostra um botão de confirmação e só então reenvia com a ferramenta liberada. Respostas viram eventos `chat.mensagem`.
  - Sem Claude Code instalado: o chat mostra "Claude Code não encontrado neste computador" e o resto do painel segue funcionando (sem fallback para Ollama no chat na v1).

- [ ] **Step 1: Testes** — `test_mcp_tools_listed` (as 9 ferramentas com os nomes exatos); `test_status_tool_calls_api` (respx); `test_bridge_resumes_session` (claude falso registra argumentos; 2ª chamada contém `--resume <id>` vindo do `session_id` da 1ª); `test_destructive_tool_requires_confirmation`; `test_chat_without_claude_reports_clearly`.
- [ ] **Step 2:** FAIL. **Step 3:** implementar. **Step 4:** PASS.
- [ ] **Step 5:** `git commit -m "feat: panel chat powered by local claude code and mcp tools"`

---

### Task 12: Documentação de instalação e configuração das contas

**Files:**
- Create: `README.md`, `docs/contas.md`, `canais/ia.yaml`, `canais/politica.yaml`
- Test: `tests/test_config.py` (adicionar `test_all_bundled_channels_load`)

**Interfaces:**
- Consumes: tudo.
- Produces:
  - `README.md` em português: requisitos (Python, FFmpeg, cloudflared, Claude Code logado, Ollama opcional com `ollama pull qwen2.5:7b`), instalação (`pip install -e .`), `canaldark run --ensaio` primeiro, depois `canaldark auth youtube gta6` / `canaldark auth instagram gta6` e `canaldark run`.
  - `docs/contas.md`: passo a passo gratuito para criar o projeto no Google Cloud (YouTube Data API v3, OAuth "In production"), o app Meta com `instagram_business_content_publish` e a conta Business; avisos da pesquisa (tokens de 7 dias em modo Testing, limite de 25 posts/24 h configurado).
  - `canais/ia.yaml` (fontes Tecnoblog/Canaltech/Olhar Digital RSS + Google News "inteligência artificial") e `canais/politica.yaml` (`regras: politica`, pesos `noticia: 0.85, dialogo: 0.15, meme: 0.0`, fontes G1/Folha/Poder360/UOL RSS).
- [ ] **Step 1:** teste `test_all_bundled_channels_load` → FAIL (arquivos faltam). **Step 2:** escrever arquivos. **Step 3:** PASS.
- [ ] **Step 4:** rodar a suíte inteira `pytest -v` → tudo PASS.
- [ ] **Step 5:** `git commit -m "docs: setup guide, account guide and bundled channels"`
