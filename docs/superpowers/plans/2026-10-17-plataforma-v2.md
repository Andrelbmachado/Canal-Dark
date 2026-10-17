# Canal Dark v2 — Plano de implementação

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Objetivo:** transformar o Canal Dark numa plataforma local de 3 abas (porta 8001), só com scripts. Ela capta Shorts com crédito, guarda no Google Drive, baixa pacotes de 15 por canal, edita em 7 estilos, publica sozinha em YouTube, Instagram, Facebook e TikTok, devolve o final ao Drive e apaga a cópia local.

**Arquitetura:** um processo asyncio. Cada etapa é um trabalhador que tira um vídeo de um estado-fila do SQLite, trabalha nele e o grava no estado seguinte. Todos recebem um `Ctx`, que guarda banco, eventos, configs, Drive e uploaders. O Drive é uma interface com duas implementações: pasta local (o padrão) e rclone. O painel é FastAPI com HTML/JS puro e WebSocket. Nenhuma chamada a LLM.

**Tech stack:**
- Backend: Python 3.11+, FastAPI + uvicorn[standard], aiosqlite, pydantic v2, PyYAML, httpx, feedparser (RSS do Google Trends), yt-dlp (lib), Pillow, keyring, google-api-python-client + google-auth-oauthlib.
- Ferramentas externas: FFmpeg/ffprobe com libass (subprocess), rclone (subprocess, opcional).
- Opcional: faster-whisper, no extra `[ml]`.
- Testes: pytest + pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-10-17-plataforma-3-abas-design.md` (aprovada em 17/10). A pesquisa de apoio está em `/mnt/project-files/canal-dark/pesquisa/` (`formatos-e-memes.md`, `automacao-e-canais.md`).

## Global Constraints

- **Nada pago e nenhuma IA generativa.** Nenhuma chamada a `claude`, Ollama ou API paga. A única exceção é o faster-whisper local, opcional, só no estilo `karaoke`.
- Textos de interface, logs, erros e docs em português do Brasil. Código, nomes de função e commits em inglês.
- Estados (valores exatos): `descoberto`, `baixando`, `baixado`, `enviando`, `no_drive`, `separado`, `descendo`, `pronto`, `editando`, `revisar`, `editado`, `publicando`, `publicado`, `arquivando`, `arquivado`, `erro`, `descartado`.
- Nomes de etapa, usados na pausa e em `ctx.channels_for(etapa)`: `captacao`, `edicao`, `publicacao`, como no v1. Todo trabalhador só pega vídeos dos canais que `ctx.channels_for` devolve para a sua aba: download e envio ao Drive em `captacao`; pacote, descida e edição em `edicao`; publicação e arquivamento em `publicacao`.
- Saída de vídeo: 1080×1920, 30 fps, H.264 (libx264, yuv420p) + AAC 48 kHz, `-movflags +faststart`, no máximo 180 s.
- Porta padrão 8001. O pacote tem 15 vídeos por canal. `estoque_max` padrão é 60. A captação pausa com o Drive em 90% ou mais (`limite_uso: 0.9`).
- Limites de publicação: Instagram 25 em 24 h por conta, Facebook 30 em 24 h por Página, TikTok 15 em 24 h por conta, YouTube 90 por dia somando todas as contas.
- Nomes no Drive: `<canal>/1-captados/<id>.mp4` e `<id>.info.json`, `<canal>/2-em-edicao/<id>.mp4`, `<canal>/3-publicados/AAAA-MM-DD/<id>.mp4`, `<canal>/4-originais-usados/<id>.mp4`.
- Fuso das regras de calendário: `America/Sao_Paulo`. A pausa eleitoral vai de 2026-10-22 00:00 a 2026-10-27 00:00 e vale só com `regras: politica` e `pausa_eleitoral: true`.
- Segredos ficam no keyring (serviço `canaldark`, chave `plataforma:conta`), nunca no repo nem no SQLite. As chaves do Pexels e do Pixabay usam `pexels:chave` e `pixabay:chave`, ou as variáveis `PEXELS_API_KEY` e `PIXABAY_API_KEY`.
- **Testes sem rede:** use `httpx.MockTransport`, um extrator falso do yt-dlp, um runner falso do rclone e o `FakeDrive`. O FFmpeg real pode rodar nos testes, com clipes gerados por `lavfi`.
- Todo commit termina com:
  ```
  Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01EAGCGqC4o3Bxhc2ALjRD3u
  ```
- **Arquivos congelados depois da Task 1:**
  - `canaldark/models.py`, `canaldark/db.py`, `canaldark/config.py` e `canaldark/context.py`;
  - a parte de protocolo de `canaldark/drive.py`;
  - `tests/conftest.py` e `tests/fakes/`.

  As tasks das ondas 1 e 2 não editam esses arquivos. Se faltar algo, definam localmente no próprio módulo e anotem no relatório final.

## Review Focus

1. **O pacote nunca trava:** dentro de um pacote, um vídeo em `erro` ou `descartado` não segura o próximo pacote; um em `revisar` segura, de propósito. → teste em Task 3.
2. **Queda no meio de uma transferência** (`enviando`, `descendo`, `arquivando`): ao reiniciar, o vídeo volta para a fila e a nova tentativa não duplica nem perde arquivo. O `enviar` aceita sobrescrever no destino, e quem chama `enviar` grava o destino no banco antes, para a nova tentativa saber que o arquivo já subiu. → testes em Task 1 (recover), Task 3 (sobrescrever), Task 4 (`upload_one` retomado) e Task 5 (`archive_video` retomado).
3. **Títulos estranhos:** só hashtags, só emoji, 300 caracteres, tudo em maiúsculas, sufixo "| Canal". O texto cai para `manchete_padrao` e o Pillow nunca estoura a caixa. → testes em Task 2.
4. **Nenhuma conta conectada fora do ensaio:** o canal não publica e nada é arquivado. Vale também para um vídeo cujos destinos trocados na aba estão todos desconectados. → testes em Task 5.
5. **Fontes de formato estranho:** 16:9, 9:16, quadrado, sem trilha de áudio, mais longo que 180 s. Todo estilo sai em 1080×1920 com trilha de áudio. → testes em Task 2.

---

## Estrutura de arquivos (v2)

```text
plataforma.yaml                      config global (porta, pacote, drive)
canais/{politica,tecnologia,jogos,bebes_gatos}.yaml
estilos/{faixa_cima,faixa_dupla,comentario,borrado,karaoke,meme_shrek,meme_dicaprio}.yaml
memes/{shrek,dicaprio}.yaml + memes/LEIAME.md   (clipes .mp4 colocados pelo usuário)
canaldark/
  models.py      State, VideoRow, configs, Candidate, Destination, WordTiming      (T1)
  db.py          schema v2, máquina de estados, claim, recover, helpers             (T1)
  config.py      loaders + replace_sources                                         (T1)
  context.py     Ctx                                                               (T1)
  drive.py       protocolo (T1) + PastaDrive, RcloneDrive, make_drive (T3)
  rules.py       pausa eleitoral                                                   (T1)
  events.py      sem mudança
  manchete.py    texto da faixa a partir do título                                 (T2)
  pack.py        pacotes de 15 + descida do Drive                                  (T3)
  capture/{sources,trends,scoring,download,stage}.py                               (T4)
  edit/{cards,styles,render,layout,transcribe,stage}.py                            (T2)
  publish/{base,youtube,instagram,facebook,tiktok,schedule,stage}.py               (T5)
  pipeline.py, cli.py, service.py                                                  (T6)
  web/app.py + web/static/{index.html,app.js,style.css}                            (T7)
README.md, docs/contas.md                                                          (T8)
```

**Ordem de execução:**
- Onda 0: T1.
- Onda 1, em paralelo: T2, T3, T4, T5.
- Onda 2, em paralelo: T6, T7, T8.
- No fim, revisão do branch inteiro.

---

### Task 1: Fundação v2 (modelos, banco, configs, contexto, limpeza)

**Files:**
- Rewrite: `canaldark/models.py`, `canaldark/db.py`, `canaldark/config.py`, `canaldark/rules.py`
- Create: `canaldark/context.py`, `canaldark/drive.py` (só o protocolo), `tests/fakes/drive.py`
- Create: `plataforma.yaml`, `canais/{politica,tecnologia,jogos,bebes_gatos}.yaml`, `estilos/*.yaml` (7), `memes/shrek.yaml`, `memes/dicaprio.yaml`, `memes/LEIAME.md`
- Delete:
  - módulos: `canaldark/brain.py`, `canaldark/chat/`, `canaldark/edit/tts.py`;
  - configs e dados: `canais/gta6.yaml`, `canais/ia.yaml`, `sons/`, `tests/fakes/fake_claude.py`, `tests/fixtures/feeds/`;
  - testes reescritos por outras tasks: `tests/test_brain.py`, `tests/test_chat.py`, `tests/test_capture.py`, `tests/test_edit_stage.py`, `tests/test_pipeline.py`, `tests/test_web.py`, `tests/test_publish.py`, `tests/test_render.py`, `tests/test_layout.py`.
- Modify:
  - `canaldark/edit/layout.py` e `canaldark/edit/transcribe.py`: só a linha de import de `WordTiming`, que agora vem de `canaldark.models`;
  - `pyproject.toml`: tira `mcp`, `kokoro`, `soundfile` e o script `canaldark-mcp`; põe `pillow`; `[ml] = ["faster-whisper"]`;
  - `tests/conftest.py`, `tests/test_schedule.py`: fixture do `ChannelConfig` v2.
- Test: `tests/test_db.py`, `tests/test_config.py`, `tests/test_rules.py`, `tests/test_fakes.py`

**Interfaces:**
- Consumes: nada.
- Produces (todas as outras tasks dependem disto):

```python
# models.py
class State(str, Enum): ...                     # os 17 valores das Global Constraints, nessa ordem
Tone = Literal["serio", "meme"]
Platform = Literal["youtube", "instagram", "facebook", "tiktok"]
PLATFORMS = ("youtube", "instagram", "facebook", "tiktok")

@dataclass(frozen=True)
class WordTiming: word: str; start: float; end: float

class VideoRow(BaseModel):
    id: int; canal: str; url_origem: str; titulo_origem: str
    url_midia: str | None = None                 # arquivo direto (Pexels/Pixabay)
    autor: str = ""                              # já formatado: "@GaiaMemes" ou "Ana · Pexels"
    fonte: str = ""                              # rótulo da fonte (SourceSpec.rotulo())
    fonte_autorizada: bool = False; tom: Tone = "serio"; exige_cc: bool = False
    estado: State; etapa_progresso: float = 0
    erro: str | None = None; fila_erro: State | None = None
    nota: float = 0; duracao: float | None = None
    estilo: str | None = None; manchete: str | None = None
    caminho_local: str | None = None; caminho_capa: str | None = None; caminho_final: str | None = None
    remoto_bruto: str | None = None; remoto_final: str | None = None
    pacote: int | None = None
    destinos: str | None = None                  # JSON list[{"plataforma","conta"}]; None = padrão do canal
    criado_em: str; atualizado_em: str

class SourceSpec(BaseModel):
    tipo: Literal["youtube_canal", "youtube_busca", "tiktok_perfil", "instagram_perfil", "pexels", "pixabay"]
    url: str | None = None; busca: str | None = None
    so_creative_commons: bool = False; tom: Tone = "serio"; autorizado: bool = False
    def rotulo(self) -> str   # "@GaiaMemes" (último segmento da url que começa com @), "busca: meme gta 6", "Pexels: cute cat", "Pixabay: baby"
    # validator: url obrigatória em youtube_canal/tiktok_perfil/instagram_perfil; busca obrigatória em youtube_busca/pexels/pixabay

class ChannelConfig(BaseModel):
    id: str; nome: str
    intervalo_captacao_min: int = 30; captar_por_dia: int = 20; estoque_max: int = 60
    fontes: list[SourceSpec]
    palavras_chave: list[str] = []; palavras_proibidas: list[str] = []
    estilos: dict[Tone, dict[str, float]]        # validator: cada tabela soma 1.0 (±0.01), ambas presentes
    manchete_padrao: list[str]                   # validator: pelo menos 1
    texto_base: str = ""
    horarios: list[str]; max_por_dia: int = 4    # validator de HH:MM do v1
    contas: dict[Platform, str] = {}
    hashtags: list[str] = []
    regras: Literal["padrao", "politica"] = "padrao"; pausa_eleitoral: bool = True
    revisar_antes_de_publicar: bool = False
    cookies_navegador: str | None = None
    tiktok_modo: Literal["rascunho", "publicar"] = "rascunho"

class DriveConfig(BaseModel):
    tipo: Literal["pasta", "rclone"] = "pasta"; remote: str = "gdrive:"; pasta: str = "CanalDark"
    pasta_local: str = "dados/drive"; manter_originais: bool = False; limite_uso: float = 0.9

class PlatformConfig(BaseModel):
    porta: int = 8001; pacote: int = 15; drive: DriveConfig = DriveConfig(); tendencias_geo: str = "BR"

class StylePreset(BaseModel):
    nome: str; rotulo: str
    tipo: Literal["faixa_cima", "faixa_dupla", "comentario", "borrado", "karaoke", "meme"]
    cor_faixa: str = "#FFFFFF"; cor_texto: str = "#000000"
    alinhamento: Literal["centro", "esquerda"] = "centro"
    fonte_min: int = 56; fonte_max: int = 76
    meme: str | None = None                      # validator: obrigatório quando tipo == "meme"

class MemeTemplate(BaseModel):
    nome: str; clipe: str; imagem_em: Literal["cima", "baixo"] = "cima"

class Candidate(BaseModel):
    url: str; titulo: str; autor: str = ""; fonte: str = ""
    duracao: float | None = None; views: int | None = None; publicado_em: datetime | None = None
    licenca: str | None = None; direto: str | None = None; capa: str | None = None

class Destination(BaseModel): plataforma: Platform; conta: str

# db.py
WORKING: dict[State, State] = {baixando: descoberto, enviando: baixado, descendo: separado,
                               editando: pronto, publicando: editado, arquivando: publicado}
ALLOWED: dict[State, set[State]]                 # tabela abaixo
TAB_STATES = {"captacao": (descoberto, baixando, baixado, enviando, no_drive),
              "edicao": (separado, descendo, pronto, editando, revisar, editado),
              "publicacao": (editado, publicando, publicado, arquivando, arquivado)}
TAB_OF_QUEUE = {descoberto: "captacao", baixado: "captacao", separado: "edicao", pronto: "edicao",
                editado: "publicacao", publicado: "publicacao"}
PACK_STATES = (separado, descendo, pronto, editando, revisar, editado, publicando, publicado, arquivando)
class InvalidTransition(Exception)
def now_iso() -> str; def normalize_url(url) -> str; def url_hash(url) -> str        # do v1
async def open_db(path: Path) -> aiosqlite.Connection
async def get_video(db, video_id: int) -> VideoRow | None
async def seen(db, url: str) -> bool
async def insert_video(db, channel: str, url: str, title: str, **fields) -> int | None   # dedupe por url_hash em `vistos`; estado descoberto
async def transition(db, video_id: int, to: State, *, error: str | None = None) -> None
async def claim(db, from_state: State, to_state: State, *, channels: list[str] | None = None,
                video_id: int | None = None, order: str = "id") -> VideoRow | None
async def update_fields(db, video_id: int, **fields) -> None
async def set_progress(db, video_id: int, pct: float) -> None
async def count_states(db, channel: str, states: Iterable[State]) -> int
async def recent_styles(db, channel: str, limit: int = 2) -> list[str]   # estilo dos últimos vídeos em editado..arquivado, mais recente primeiro (atualizado_em DESC, id DESC)
async def recover(db) -> int                                            # aplica WORKING; zera etapa_progresso

# config.py
def load_channels(directory: Path) -> dict[str, ChannelConfig]   # erro de validação cita o arquivo
def load_platform(path: Path) -> PlatformConfig                   # arquivo ausente → padrões
def load_styles(directory: Path) -> dict[str, StylePreset]        # nome = stem do arquivo
def load_memes(directory: Path) -> dict[str, MemeTemplate]        # nome = stem do arquivo
def replace_sources(path: Path, fontes: list[SourceSpec]) -> ChannelConfig   # reescreve só o bloco `fontes:`; valida recarregando; se falhar, restaura o texto e relança

# context.py
@dataclass
class Ctx:
    db: aiosqlite.Connection; bus: EventBus; cfgs: dict[str, ChannelConfig]; plat: PlatformConfig
    drive: Drive; root: Path
    styles: dict[str, StylePreset] = field(default_factory=dict)
    memes: dict[str, MemeTemplate] = field(default_factory=dict)
    dry_run: bool = False
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    active: Callable[[str], list[str]] | None = None          # etapa → canais não pausados
    get_uploader: Callable[[str, str], Any] | None = None
    keys: dict[str, str] = field(default_factory=dict)          # {"pexels": ..., "pixabay": ...}
    paused_accounts: set[tuple[str, str]] = field(default_factory=set)        # (plataforma, conta) com token vencido
    cooldown: dict[tuple[str, str], datetime] = field(default_factory=dict)   # (plataforma, conta) → só tenta de novo depois disso
    @property
    def data(self) -> Path: return self.root / "dados"
    def channels_for(self, stage: str) -> list[str]: return self.active(stage) if self.active else list(self.cfgs)

# drive.py (protocolo)
CAPTADOS, EM_EDICAO, PUBLICADOS, USADOS = "1-captados", "2-em-edicao", "3-publicados", "4-originais-usados"
class DriveError(Exception)
@dataclass
class DriveUsage:
    usado: int; total: int | None
    @property
    def fracao(self) -> float | None: return self.usado / self.total if self.total else None
class Drive(Protocol):
    async def enviar(self, local: Path, pasta: str) -> str: ...      # move; apaga o local só depois do sucesso; devolve "pasta/<nome>"
    async def baixar(self, remoto: str, pasta_local: Path) -> Path: ...
    async def mover(self, remoto: str, pasta: str) -> str: ...
    async def apagar(self, remoto: str) -> None: ...                 # arquivo ausente não é erro
    async def uso(self) -> DriveUsage | None: ...
def pasta(canal: str, sub: str, *extra: str) -> str: return "/".join((canal, sub, *extra))

# rules.py
SP = ZoneInfo("America/Sao_Paulo"); TSE_BLACKOUTS = [(datetime(2026,10,22,tzinfo=SP), datetime(2026,10,27,tzinfo=SP))]
def in_tse_blackout(now: datetime) -> bool
def publish_allowed(cfg: ChannelConfig, now: datetime) -> bool   # False só se politica + pausa_eleitoral + janela

# tests/fakes/drive.py
class FakeDrive:                                 # implementa Drive em memória: files: dict[str, bytes]
    fail: dict[str, Exception]                   # nome do método → exceção a lançar uma vez
    usage: DriveUsage | None = DriveUsage(0, 15 * 1024**3)
# tests/conftest.py — fixtures
db                                     # como no v1
cfg_factory(**over) -> ChannelConfig    # canal "teste" com 1 fonte youtube_busca, estilos serio/meme, horarios ["09:00"], contas das 4 redes = "teste"
ctx(tmp_path, db) -> Ctx                # root=tmp_path, com cópias de estilos/ e memes/*.yaml do repo (a FONT continua vindo de fontes/ do pacote); FakeDrive; cfgs={"teste": cfg_factory()}; plat=PlatformConfig(); dry_run=True
```

**Tabela ALLOWED** (origem → destinos permitidos):

| De | Para |
|---|---|
| descoberto | baixando, erro, descartado |
| baixando | baixado, descoberto, erro |
| baixado | enviando, erro, descartado |
| enviando | no_drive, baixado, erro |
| no_drive | separado, descartado |
| separado | descendo, erro, descartado |
| descendo | pronto, separado, erro |
| pronto | editando, descartado |
| editando | editado, revisar, pronto, erro |
| revisar | editado, pronto, descartado |
| editado | publicando, pronto, descartado |
| publicando | publicado, editado, erro |
| publicado | arquivando |
| arquivando | arquivado, publicado, erro |
| erro | descoberto, baixado, separado, pronto, editado, publicado, descartado |
| arquivado, descartado | (nenhum) |

**Regras do `transition`:**
- Indo para `erro`, grava `erro` e também `fila_erro = WORKING.get(atual, atual)`.
- Saindo de `erro`, limpa `erro`, `fila_erro` e `etapa_progresso`.

**Schema:** a tabela `videos` tem as colunas de `VideoRow` (bools como INTEGER, `nota` REAL). A tabela `publicacoes` tem:
- `id`, `video_id`, `plataforma`, `conta`, `id_externo`, `url`;
- `status TEXT NOT NULL DEFAULT 'publicado'`, com valores `publicado`, `rascunho` ou `pulado`;
- `motivo`, `publicado_em`;
- `UNIQUE(video_id, plataforma)`.

`vistos` e `eventos` ficam como no v1. Os índices são `idx_videos_estado(estado, id)` e `idx_videos_canal(canal, estado)`.

**Conteúdo das configs** (valores exatos):
- `plataforma.yaml`: `porta: 8001`, `pacote: 15`, `drive: {tipo: pasta, remote: "gdrive:", pasta: CanalDark, pasta_local: dados/drive, manter_originais: false, limite_uso: 0.9}`, `tendencias_geo: BR`, com comentários em PT-BR explicando como trocar para rclone.
- Em todos os canais: `captar_por_dia: 20`, `estoque_max: 60` e `contas` das 4 redes com o id do canal.
- **`canais/politica.yaml`:**
  - básicos: `nome: "Eleições 2026"`, `regras: politica`, `pausa_eleitoral: true`;
  - fontes: `youtube_canal https://www.youtube.com/@investigapinhel` (tom serio, autorizado false, com o comentário "canal provisório, mude na aba Captação"), `youtube_busca "Lula Flávio Bolsonaro"` (serio) e `youtube_busca "meme Lula Bolsonaro"` (meme);
  - `palavras_chave: [Lula, Flávio Bolsonaro, Bolsonaro, eleição, eleições, debate, segundo turno, pesquisa, TSE]`;
  - estilos: serio `{faixa_cima: 0.5, faixa_dupla: 0.3, karaoke: 0.2}`, meme `{comentario: 0.4, meme_shrek: 0.3, meme_dicaprio: 0.3}`;
  - `manchete_padrao: ["Olha o que aconteceu na eleição", "Você viu isso?", "Isso está dando o que falar"]`;
  - `texto_base: "Siga para acompanhar as eleições"`;
  - `horarios: ["07:30", "12:30", "19:00", "22:00"]`, `max_por_dia: 4`;
  - `hashtags: ["#eleicoes2026", "#politica", "#lula", "#bolsonaro"]`.
- **`canais/tecnologia.yaml`:**
  - nome "Tecnologia Hoje";
  - fontes: `youtube_canal https://www.youtube.com/@AlmanaqueSOS` (serio, provisório), `youtube_busca "tecnologia IA celular"` (serio) e `youtube_busca "meme tecnologia"` (meme);
  - `palavras_chave: [IA, inteligência artificial, ChatGPT, iPhone, Samsung, Android, Apple, Google, celular]`;
  - mesmos pesos de estilo;
  - `manchete_padrao: ["Olha essa tecnologia", "Você sabia disso?", "Isso vai mudar tudo"]`, `texto_base: "Siga para mais tecnologia"`;
  - `horarios: ["08:00", "11:30", "18:30", "21:30"]`;
  - `hashtags: ["#tecnologia", "#ia", "#tech"]`.
- **`canais/jogos.yaml`:**
  - nome "Jogos e GTA 6";
  - fontes: `youtube_canal https://www.youtube.com/@GaiaMemes` (meme, provisório), `youtube_busca "GTA 6"` (serio) e `youtube_busca "meme games"` (meme);
  - `palavras_chave: [GTA 6, GTA VI, Rockstar, PlayStation, Xbox, Nintendo, Minecraft, Fortnite]`;
  - `palavras_proibidas: [leak, vazado, vazamento]`;
  - mesmos pesos;
  - `manchete_padrao: ["Quem joga vai entender", "Olha isso no jogo", "Gamers, vocês viram?"]`, `texto_base: "Siga para mais games"`;
  - `horarios: ["09:00", "12:00", "18:00", "21:00"]`;
  - `hashtags: ["#gta6", "#games", "#gamer"]`.
- **`canais/bebes_gatos.yaml`:**
  - nome "Bebês e Gatos";
  - fontes: `pexels "cute cat"` (serio), `pexels "baby"` (serio), `pixabay "kitten"` (serio) e `youtube_busca "gatos fofos"` (meme, `so_creative_commons: true`);
  - `palavras_chave: [gato, gatinho, bebê, filhote, fofo]`;
  - estilos: serio `{faixa_cima: 0.6, borrado: 0.4}`, meme `{comentario: 0.5, faixa_cima: 0.5}`;
  - `manchete_padrao: ["Olha que fofura", "Impossível não sorrir", "Quem aí tem um assim?"]`, `texto_base: "Siga para mais fofura"`;
  - `horarios: ["10:00", "15:00", "20:00"]`, `max_por_dia: 3`;
  - `hashtags: ["#gatos", "#bebes", "#fofura"]`.
- **`estilos/*.yaml`:**
  - `faixa_cima`, rótulo "Faixa em cima", alinhamento centro, fonte 60–72;
  - `faixa_dupla`, "Faixa dupla";
  - `comentario`, "Comentário";
  - `borrado`, "Fundo borrado", `cor_texto: "#FFFFFF"`;
  - `karaoke`, "Legenda karaokê";
  - `meme_shrek`, tipo meme, `meme: shrek`, "Meme do Shrek";
  - `meme_dicaprio`, tipo meme, `meme: dicaprio`, "Meme do DiCaprio".
- `memes/shrek.yaml`: `{clipe: shrek.mp4, imagem_em: cima}`. `memes/dicaprio.yaml`: `{clipe: dicaprio.mp4, imagem_em: cima}`.
- `memes/LEIAME.md`, em PT-BR: onde pôr os clipes limpos (sem texto), duração de 3 a 10 s, e um comando de exemplo com yt-dlp + ffmpeg para recortar um trecho.

- [ ] **Step 1: Escrever os testes que falham**

```python
# tests/test_db.py
async def test_insert_dedup_and_defaults(db):
    vid = await insert_video(db, "teste", "https://youtu.be/x?utm_source=a", "T", autor="@a", nota=3.5)
    assert vid and await insert_video(db, "teste", "https://youtu.be/x", "T") is None
    row = await get_video(db, vid)
    assert row.estado == State.descoberto and row.nota == 3.5 and row.autor == "@a"

async def test_happy_path_transitions(db): ...   # percorre descoberto → … → arquivado sem erro
async def test_invalid_transition_raises(db): ...  # descoberto → editado: InvalidTransition
async def test_error_records_queue_and_retry_returns(db):
    # pronto → editando → erro
    assert row.fila_erro == State.pronto and row.erro == "falhou"
    # erro → pronto
    assert row.erro is None and row.fila_erro is None and row.etapa_progresso == 0
async def test_error_from_queue_state_keeps_it(db): ...  # separado → erro: fila_erro == separado
async def test_recover_resets_every_working_state(db):
    assert await recover(db) == 6   # um vídeo em cada chave de WORKING, cada um volta para WORKING[k]
async def test_claim_respects_channels_and_order(db): ...  # channels=["a"] ignora "b"; order="nota DESC, id" pega a maior nota
async def test_count_states_and_recent_styles(db): ...     # recent_styles devolve ["borrado", "faixa_cima"] na ordem certa

# tests/test_config.py
def test_repo_channels_load():
    cfgs = load_channels(Path("canais"))
    assert set(cfgs) == {"politica", "tecnologia", "jogos", "bebes_gatos"}
    assert all(set(c.contas) == set(PLATFORMS) for c in cfgs.values())
def test_style_weights_must_sum_to_one(tmp_path): ...      # ValueError com o nome do arquivo
def test_source_requires_field_and_label():
    with pytest.raises(ValidationError): SourceSpec(tipo="youtube_canal")
    assert SourceSpec(tipo="youtube_canal", url="https://www.youtube.com/@GaiaMemes").rotulo() == "@GaiaMemes"
    assert SourceSpec(tipo="youtube_busca", busca="meme gta 6").rotulo() == "busca: meme gta 6"
def test_platform_defaults_when_missing(tmp_path):
    p = load_platform(tmp_path / "nao.yaml"); assert (p.porta, p.pacote, p.drive.tipo) == (8001, 15, "pasta")
def test_repo_platform_styles_memes():
    assert load_platform(Path("plataforma.yaml")).porta == 8001
    styles = load_styles(Path("estilos")); assert len(styles) == 7 and styles["meme_shrek"].meme == "shrek"
    assert load_memes(Path("memes"))["dicaprio"].clipe == "dicaprio.mp4"
def test_replace_sources_keeps_comments(tmp_path): ...     # comentário antes e depois de `fontes:` continua; recarrega com 1 fonte
def test_replace_sources_restores_on_failure(tmp_path): ... # arquivo com horario inválido fora do bloco → relança e o texto fica igual

# tests/test_rules.py
def test_blackout_only_for_politics_with_flag(): ...       # politica+pausa na janela → False; pausa_eleitoral=False → True; padrao → True

# tests/test_fakes.py
async def test_fake_drive_roundtrip(tmp_path): ...         # enviar apaga o local; baixar recria; fail["mover"] lança uma vez
```

- [ ] **Step 2: Rodar e confirmar a falha** — `.venv/bin/python -m pytest tests/test_db.py tests/test_config.py tests/test_rules.py tests/test_fakes.py -q`. Esperado: falhas de import ou de atributo.
- [ ] **Step 3: Implementar.** Rodar `.venv/bin/python -m pip install -e ".[dev]"` depois de mudar o `pyproject.toml`, para o Pillow entrar no venv. Reescrever os módulos, criar as configs e apagar o que a lista manda. Em `replace_sources`, localizar a linha `fontes:` no nível 0 e o fim do bloco (a próxima linha no nível 0 que não é comentário nem vazia), e trocar só esse trecho por `yaml.safe_dump({"fontes": [...]}, allow_unicode=True, sort_keys=False)`, gravando cada fonte só com os campos diferentes do padrão.
- [ ] **Step 4: Rodar e confirmar que passa** — o mesmo comando, mais `tests/test_schedule.py tests/test_events.py`. Esperado: tudo verde.
- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat: v2 foundation (states, schema, configs, context, drive protocol)"` com os trailers.

---

### Task 2: Edição (texto da faixa, cartões, estilos, render e etapa)

**Files:**
- Create: `canaldark/manchete.py`, `canaldark/edit/cards.py`, `canaldark/edit/styles.py`
- Rewrite: `canaldark/edit/render.py`, `canaldark/edit/stage.py`
- Modify: `canaldark/edit/layout.py`: mantém `FONT`, `FONT_NAME`, `_EMOJI`, `strip_emoji` e `build_ass`; `build_ass(words, *, margin_v, destaque="&H0000E0FF&")` passa a destacar a palavra atual em amarelo com override `{\c...}`; o resto sai.
- Test: `tests/test_manchete.py`, `tests/test_cards.py`, `tests/test_styles.py`, `tests/test_edit_stage.py`

**Interfaces:**
- Consumes: T1 (`Ctx`, `VideoRow`, `StylePreset`, `MemeTemplate`, `WordTiming`, `db.transition`/`update_fields`/`recent_styles`/`claim`).
- Produces:

```python
# manchete.py
MAX_CHARS = 90
JUNK_WORDS = {"shorts", "short", "fyp", "foryou", "foryoupage", "viral", "reels", "tiktok"}
def clean_title(title: str) -> str
def headline_for(title: str, fallbacks: list[str], counter: int) -> str   # clean_title(title); se ele tiver menos de 3 letras (str.isalpha), fallbacks[counter % len(fallbacks)]

# edit/render.py
W, H, FPS = 1080, 1920, 30
class RenderError(Exception)
@dataclass
class MediaInfo: width: int; height: int; duration: float; has_audio: bool
@dataclass
class RenderJob: cmd: list[str]; duration: float; out: Path
async def probe(path: Path) -> MediaInfo                               # do v1
async def run_ffmpeg(job: RenderJob, on_progress: Callable[[float], Awaitable[None]]) -> Path
async def make_thumb(video: Path, out: Path, at: float = 0.5) -> Path  # do v1 (270 px)
async def extract_frame(video: Path, out: Path, at: float = 1.0) -> Path

# edit/cards.py (Pillow; todo texto passa por strip_emoji)
def fit_text(text: str, font: Path, box: tuple[int, int], size_range: tuple[int, int],
             max_lines: int = 4) -> tuple[int, list[str]]
def text_image(text: str, font: Path, box: tuple[int, int], *, color: str, size_range: tuple[int, int],
               align: str = "centro", stroke: int = 0, stroke_color: str = "#000000") -> Image.Image   # RGBA, exatamente do tamanho de box
def bar_image(width: int, height: int, color: str, text: str, font: Path, *, text_color: str,
              size_range: tuple[int, int], align: str = "centro", credit: str = "",
              credit_color: str = "#666666") -> Image.Image
def comment_card(text: str, handle: str, font: Path, *, width: int = 960,
                 video_size: tuple[int, int]) -> tuple[Image.Image, tuple[int, int, int, int]]   # cartão branco arredondado: avatar genérico (círculo cinza com silhueta), "@autor" em negrito, o texto e embaixo o buraco transparente de cantos arredondados no formato do vídeo; devolve (x, y, w, h) do buraco

# edit/styles.py
FALLBACK_STYLE = "faixa_cima"; MAX_SECONDS = 180.0
def available_styles(styles: dict[str, StylePreset], memes: dict[str, MemeTemplate],
                     memes_dir: Path) -> dict[str, str | None]   # nome → None (disponível) ou motivo "falta o clipe memes/shrek.mp4"
def choose_style(weights: dict[str, float], recent: list[str], available: set[str], rng: random.Random) -> str
@dataclass
class StyleInputs:
    src: Path; info: MediaInfo; text: str; credit: str; work: Path
    capa: Path | None = None; meme_clip: Path | None = None; meme: MemeTemplate | None = None
    texto_base: str = ""; words: list[WordTiming] = field(default_factory=list); font: Path = FONT
def prepare_job(preset: StylePreset, inp: StyleInputs) -> RenderJob   # grava os PNG/ASS em inp.work e devolve o comando ffmpeg

# edit/stage.py
async def edit_video(ctx: Ctx, row: VideoRow, *, rng: random.Random | None = None) -> State
async def edit_worker(ctx: Ctx) -> None   # claim pronto→editando em ctx.channels_for("edicao"), IDLE_SLEEP = 3
```

**Regras de `clean_title`, na ordem:**
1. Remover URLs `https?://\S+`, `@menções` (`@\w[\w.]*`), toda `#hashtag` (`#\w+`, com acentos) e emojis (o `_EMOJI` de layout, mais `‍` e `️`).
2. Remover palavras soltas de `JUNK_WORDS`, sem diferenciar maiúsculas.
3. Se houver `" | "`, ficar só com a parte antes do último `" | "`, desde que ela tenha 10 caracteres ou mais.
4. Reduzir pontuação repetida: `!{2,}` vira `!`, `\?{2,}` vira `?`, `\.{2,}` vira `…`.
5. Juntar espaços e tirar das pontas os espaços e os caracteres `|-–—•·:,;`.
6. Se passar de 90 caracteres, cortar no último espaço até a posição 89, tirar a pontuação final e acrescentar `…`.
7. Maiúsculas e minúsculas ficam como vieram.

**Regras de `choose_style`:**
- Os candidatos são as entradas com peso > 0 que estão em `available`.
- Se `recent[0] == recent[1]` e sobrar mais de um candidato, esse estilo sai da lista.
- Sem candidatos, devolve `FALLBACK_STYLE`. Com candidatos, faz `rng.choices` pelos pesos.

**Geometria dos estilos** (canvas 1080×1920; vídeo sempre `-t min(info.duration, 180)`, exceto meme):
- **`faixa_cima`:** a faixa tem 300, 420 ou 540 px conforme 1, 2 ou 3+ linhas do `fit_text` na caixa 1000×(altura−80). O vídeo vai de `scale=1080:(1920-h):force_original_aspect_ratio=increase,crop=1080:(1920-h)` até `y=h`. O crédito fica em 28 px, cinza, no canto inferior direito da faixa.
- **`faixa_dupla`:** faixa de 480 px em cima com o texto; o vídeo é recortado para 1080×960 em `y=480`; a faixa de baixo, de 480 px, tem o `texto_base` em 44 px e o crédito.
- **`comentario`:**
  - O fundo é o próprio vídeo em `scale=1080:1920:...increase,crop,boxblur=30:5`.
  - O cartão vem de `comment_card` com `width=960`, centralizado, com topo em y=160.
  - O vídeo vai em `scale=w_buraco:h_buraco:...increase,crop`, sobreposto na posição do buraco e embaixo do cartão: ordem fundo → vídeo → cartão.
- **`borrado`:** o fundo é borrado como no `comentario`. O vídeo aparece inteiro em `scale=1080:1920:force_original_aspect_ratio=decrease`, centralizado. O texto é branco com contorno preto de 6 px, numa caixa de 1000×360 com topo em y=120.
- **`karaoke`:** o vídeo ocupa 1080×1920 com escala e recorte. Uma faixa de 300 a 540 px em cima segue a regra do `faixa_cima`. O ASS vem de `build_ass(words, margin_v=360)`. Sem palavras, a legenda fica de fora.
- **`meme`:**
  - Faixa em cima com o texto, com a altura do `faixa_cima`.
  - Na área restante, metade fica com a `capa` (imagem, com escala e recorte) e metade com o clipe do meme (escala e recorte), conforme `imagem_em`.
  - A duração e o áudio são os do clipe.
  - Sem `capa`, a `edit_video` extrai um quadro da fonte.
- **Áudio:**
  - O original entra com `aresample=48000`.
  - Quando a fonte não tem trilha, entra `anullsrc` com a duração do vídeo; em todos os casos o vídeo sai com uma trilha estéreo.
  - O último quadro é congelado com `tpad=stop_mode=clone` se o vídeo for menor que a duração.
- **Saída:** `-c:v libx264 -preset veryfast -crf 20 -pix_fmt yuv420p -r 30 -c:a aac -b:a 160k -ar 48000 -movflags +faststart`.

**Fluxo de `edit_video`:**
1. `cfg = ctx.cfgs[row.canal]` e `work = ctx.data/"editados"/str(row.id)`.
2. O texto é `row.manchete`, ou, se ela não existe, `headline_for(row.titulo_origem, cfg.manchete_padrao, row.id)`.
3. O estilo é `row.estilo` se estiver disponível. Se ele não estiver disponível, usa `FALLBACK_STYLE` e loga um aviso com o motivo. Sem estilo definido, usa `choose_style(cfg.estilos[row.tom], await recent_styles(...), disponíveis, rng or random.Random())`.
4. No `karaoke`, a lista de palavras vem de `transcribe` quando o faster-whisper existe. Sem ele, a lista fica vazia e o log avisa.
5. A capa é `row.caminho_capa` se o arquivo existir. Se não, sai de `extract_frame` para `work/capa.jpg`.
6. Chama `prepare_job` e depois `run_ffmpeg`. O progresso vai para `ctx.bus.progress`.
7. Gera `make_thumb` em `ctx.data/"capas"/f"{id}-final.jpg"`.
8. Grava `update_fields(estilo=, manchete=, caminho_final=)`. A transição vai para `revisar` se `cfg.revisar_antes_de_publicar`; se não, para `editado`. Publica o evento `video.estado` e o log "editado (estilo, N s)".
9. `RenderError` ou qualquer exceção leva a `transition(erro)`, com evento e log, e nunca relança.

- [ ] **Step 1: Escrever os testes que falham**

```python
# tests/test_manchete.py
@pytest.mark.parametrize("title,expected", [
    ("Bolacha #engraçado #humor #memesbr", "Bolacha"),
    ("ARTHUR CONVIDOU A HATER PRO PODCAST! 😳 #mbl #partidomissao", "ARTHUR CONVIDOU A HATER PRO PODCAST!"),
    ("Starbucks em choque com o que viu 👀💀", "Starbucks em choque com o que viu"),
    ("Épico 😂 #memesjurídicos #advogado", "Épico"),
    ("O Primeiro Video Game da História 🕹️ #curiosidades", "O Primeiro Video Game da História"),
    ("A Samsung ENGANOU seus usuários? 😭📱", "A Samsung ENGANOU seus usuários?"),
    ("Game Dev ☠️", "Game Dev"),
    ("Real", "Real"),
    ("Can't stop skating the new park🔥 #gaming", "Can't stop skating the new park"),
    ("Essa Cena Horripilante Foi Inspirada na Arte de Damien Hirst — The Cell (2000)",
     "Essa Cena Horripilante Foi Inspirada na Arte de Damien Hirst — The Cell (2000)"),
    ("The Average Guy..", "The Average Guy…"),
    ("How Rockstar convinced GTA 6 Fans 😭🕹️", "How Rockstar convinced GTA 6 Fans"),
    ("Tesla Roadster Flying Car 🚀", "Tesla Roadster Flying Car"),
    ("usa o cinto 😂 #comedyeffect", "usa o cinto"),
    ("A mão mais rápido do oeste...🤠", "A mão mais rápido do oeste…"),
])
def test_examples_from_reference_shorts(title, expected): assert clean_title(title) == expected
def test_cuts_channel_suffix_after_pipe(): assert clean_title("Lula fala sobre economia | Jornal da Band") == "Lula fala sobre economia"
def test_long_title_truncated_at_word(): ...   # 200 chars → len ≤ 90, endswith("…"), a última palavra é inteira
def test_fallback_rotation():
    assert headline_for("#shorts #fyp 😂", ["A", "B"], 3) == "B"
    assert headline_for("Ok!", ["A", "B"], 0) == "A" and headline_for("Real", ["A"], 0) == "Real"
def test_mentions_and_links_removed(): assert clean_title("Veja isso @fulano https://x.com/y") == "Veja isso"

# tests/test_cards.py
def test_bar_image_is_white_with_dark_text(): ...    # (1080, 420); pixel (5,5) branco; há pixel com luminância < 50
def test_fit_text_never_exceeds_box(): ...           # 300 chars → ≤ 4 linhas, tamanho ≥ mínimo, cada linha cabe na largura
def test_comment_card_has_transparent_hole(): ...    # alfa 0 no centro do buraco e no canto do cartão; 255 no corpo
def test_text_with_only_emoji_does_not_crash(): ...

# tests/test_styles.py  (clipes gerados em fixture com lavfi: h=640x360 3 s com seno, v=360x640 3 s, mudo=480x480 2 s, meme=320x240 2 s com som, capa.jpg)
def test_choose_style_weights_and_no_triple_repeat(): ...  # recent ["a","a"], pesos {a:.9,b:.1} → nunca "a" em 50 sorteios; indisponível nunca sai; vazio → "faixa_cima"
def test_available_styles_reports_missing_clip(tmp_path): ...  # sem shrek.mp4 → "falta o clipe" em meme_shrek
@pytest.mark.parametrize("style", ["faixa_cima", "faixa_dupla", "comentario", "borrado", "karaoke", "meme_shrek"])
@pytest.mark.parametrize("src", ["h", "v", "mudo"])
async def test_every_style_renders_vertical_with_audio(style, src, clips, tmp_path):
    info = await probe(out)
    assert (info.width, info.height) == (1080, 1920) and info.has_audio
    assert abs(info.duration - expected) < 0.35     # expected = duração da fonte, ou do clipe do meme
async def test_faixa_cima_top_is_white(clips, tmp_path): ...  # quadro em 1 s: (540, 20) ≈ branco; (540, 1500) não é branco
def test_long_source_capped_at_180s(tmp_path): ...  # info.duration=400 → job.duration == 180 (sem render)
def test_karaoke_writes_ass_with_words(tmp_path): ... # o .ass contém "OLÁ" e o override de cor amarela

# tests/test_edit_stage.py  (fixture ctx; vídeo em `pronto` com caminho_local = clipe gerado)
async def test_edit_video_renders_and_marks_editado(ctx): ...  # editado; final existe; estilo ∈ tabela serio; manchete limpa; capas/<id>-final.jpg existe
async def test_user_style_and_text_are_respected(ctx): ...
async def test_missing_meme_clip_falls_back(ctx): ...          # estilo "meme_shrek" sem clipe → faixa_cima e log com "falta o clipe"
async def test_revisar_when_channel_asks(ctx): ...
async def test_render_failure_goes_to_erro_with_queue(ctx, monkeypatch): ...  # fila_erro == pronto
```

- [ ] **Step 2: Rodar e confirmar a falha** — `.venv/bin/python -m pytest tests/test_manchete.py tests/test_cards.py tests/test_styles.py tests/test_edit_stage.py -q`. Esperado: ImportError.
- [ ] **Step 3: Implementar** os módulos acima. O texto de cada faixa ou cartão é um PNG RGBA gerado pelo Pillow e entra no `filter_complex` como `-loop 1 -i arquivo.png` com `overlay`. Só o karaokê usa `ass=`.
- [ ] **Step 4: Rodar e confirmar que passa.** Esperado: tudo verde. Em seguida, rodar a suíte inteira da task mais as de T1.
- [ ] **Step 5: Commit** — `feat: script-only edit stage with seven styles`.

---

### Task 3: Drive e pacotes

**Files:**
- Modify: `canaldark/drive.py`, acrescentando implementações abaixo do protocolo sem mudar o protocolo
- Create: `canaldark/pack.py`
- Test: `tests/test_drive.py`, `tests/test_pack.py`

**Interfaces:**
- Consumes: T1 (`Drive`, `DriveUsage`, `DriveError`, pastas, `pasta()`, `Ctx`, `db.*`, `PACK_STATES`).
- Produces:

```python
# drive.py
Runner = Callable[[list[str]], Awaitable[tuple[int, str, str]]]   # (returncode, stdout, stderr)
class PastaDrive:
    def __init__(self, root: Path)           # remoto "a/b/c.mp4" == root/"a/b/c.mp4"; enviar sobrescreve
class RcloneDrive:
    def __init__(self, remote: str, base: str, runner: Runner | None = None, delays: tuple[float, ...] = (5, 20, 60))
def make_drive(cfg: DriveConfig, root: Path) -> Drive   # "pasta" → PastaDrive(root/cfg.pasta_local/cfg.pasta); "rclone" → RcloneDrive(cfg.remote, cfg.pasta)

# pack.py
async def pack_in_progress(db, channel: str) -> int                     # count_states(PACK_STATES)
async def start_next_pack(ctx: Ctx, channel: str) -> list[int]
async def pack_tick(ctx: Ctx) -> int                                    # start_next_pack em ctx.channels_for("edicao")
async def pack_manager(ctx: Ctx, every: float = 15) -> None             # pack_tick em loop até ctx.stop; nunca morre por exceção
async def download_pack_video(ctx: Ctx, row: VideoRow) -> State         # row já está em descendo
async def pack_download_worker(ctx: Ctx) -> None                        # claim separado→descendo; IDLE_SLEEP = 3
```

**Comandos do rclone** (alvo `f"{remote}{base}/{remoto}"`):
- `enviar`: `["rclone", "moveto", str(local), alvo/nome]`.
- `baixar`: `["rclone", "copyto", alvo, str(pasta_local/nome)]`.
- `mover`: `["rclone", "moveto", alvo_origem, alvo_destino]`.
- `apagar`: `["rclone", "deletefile", alvo]`. Os códigos de saída 3 e 4 (não encontrado) não são erro.
- `uso`: `["rclone", "about", remote, "--json"]`, lendo `used` e `total`.
- **Falhas:** um código de saída diferente de zero faz esperar `delays[i]` e tentar de novo. Esgotadas as tentativas, vira `DriveError` com as últimas 300 letras do stderr.
- **rclone ausente:** um `FileNotFoundError` do runner vira `DriveError("rclone não encontrado: instale com `brew install rclone` e rode `rclone config`")`.
- **Runner padrão:** `asyncio.create_subprocess_exec`.

**Regras de `start_next_pack`:**
- Se `pack_in_progress > 0`, devolve `[]`.
- Se não, separa até `ctx.plat.pacote` vídeos em `no_drive`, por `nota DESC, id ASC`.
- `pacote = (MAX(pacote) do canal ou 0) + 1`.
- Cada vídeo passa de `no_drive` para `separado`, com evento `video.estado` e o log "[canal] pacote N: K vídeo(s)".

**Regras de `download_pack_video`:**
- Se `remoto_bruto` não está em `EM_EDICAO`, chama `mover(remoto_bruto, pasta(canal, EM_EDICAO))` e atualiza `remoto_bruto`.
- Chama `baixar(remoto_bruto, ctx.data/"pacote"/canal)` e grava `caminho_local`.
- Transição para `pronto`.
- Qualquer exceção leva a `erro`, com `fila_erro = separado`, evento e log. Nunca relança.

- [ ] **Step 1: Escrever os testes que falham**

```python
# tests/test_drive.py
async def test_pasta_drive_roundtrip(tmp_path):
    d = PastaDrive(tmp_path / "drive"); f = tmp_path / "1.mp4"; f.write_bytes(b"x")
    r = await d.enviar(f, "politica/1-captados")
    assert r == "politica/1-captados/1.mp4" and not f.exists()
    r2 = await d.mover(r, "politica/2-em-edicao"); local = await d.baixar(r2, tmp_path / "pack")
    assert local.read_bytes() == b"x"; await d.apagar(r2); await d.apagar(r2)   # segunda vez não falha
    assert (await d.uso()).usado == 0
async def test_pasta_drive_enviar_overwrites_existing_remote(tmp_path): ...
async def test_rclone_commands(tmp_path): ...            # runner falso grava argv; confere o argv exato de cada método
async def test_rclone_retries_then_fails(): ...          # sempre (1, "", "boom"), delays (0, 0, 0) → DriveError com "boom" depois de 3 chamadas
async def test_rclone_apagar_ignores_missing(): ...      # (4, "", "not found") → sem erro
async def test_rclone_uso_parses_json(): ...             # '{"total": 100, "used": 25, "free": 75}' → fracao == 0.25
async def test_rclone_missing_binary(): ...              # FileNotFoundError → DriveError com "brew install rclone"
def test_make_drive_by_type(tmp_path): ...

# tests/test_pack.py  (fixture ctx com FakeDrive)
async def test_starts_pack_of_15_by_score(ctx): ...      # 20 em no_drive → 15 de maior nota, pacote == 1, separado
async def test_no_new_pack_while_current_runs(ctx): ...  # 1 vídeo em editando → []
async def test_erro_and_descartado_do_not_block(ctx): ...  # pacote 1 todo arquivado, exceto 1 erro e 1 descartado → pacote 2 começa
async def test_revisar_blocks_next_pack(ctx): ...
async def test_download_moves_to_em_edicao_and_downloads(ctx): ...  # remoto_bruto "teste/2-em-edicao/5.mp4"; caminho_local existe; pronto
async def test_download_failure_goes_to_erro_separado(ctx): ...
async def test_paused_channel_gets_no_pack(ctx): ...     # ctx.active = lambda s: [] → pack_tick == 0
```

- [ ] **Step 2: Rodar e confirmar a falha** — `.venv/bin/python -m pytest tests/test_drive.py tests/test_pack.py -q`.
- [ ] **Step 3: Implementar.**
- [ ] **Step 4: Rodar e confirmar que passa.**
- [ ] **Step 5: Commit** — `feat: drive backends (folder, rclone) and 15-video packs`.

---

### Task 4: Captação (fontes, tendências, nota, download, envio ao Drive)

**Files:**
- Rewrite: `canaldark/capture/sources.py`, `canaldark/capture/download.py`, `canaldark/capture/stage.py`
- Create: `canaldark/capture/trends.py`, `canaldark/capture/scoring.py`
- Test: `tests/test_sources.py`, `tests/test_scoring.py`, `tests/test_capture_stage.py`

**Interfaces:**
- Consumes: T1 (`SourceSpec`, `Candidate`, `Ctx`, `db.*`, `Drive`, `pasta`, `CAPTADOS`).
- Produces:

```python
# sources.py
class SourceError(Exception)
Extractor = Callable[[str, dict], Awaitable[dict]]
def listing_url(spec: SourceSpec, limit: int = 30) -> str
def candidate_from_entry(entry: dict, spec: SourceSpec) -> Candidate | None
async def list_candidates(spec: SourceSpec, *, client: httpx.AsyncClient, extract: Extractor | None = None,
                          keys: dict[str, str] | None = None, limit: int = 30) -> list[Candidate]
# trends.py
TRENDS_RSS = "https://trends.google.com/trending/rss?geo={geo}"
async def trending_terms(client: httpx.AsyncClient, geo: str = "BR", *, ttl: float = 3600) -> list[str]
def clear_cache() -> None
# scoring.py
def normalize(text: str) -> str
def matches_any(title: str, terms: list[str]) -> bool
def passes(c: Candidate, *, banned: list[str], min_s: float = 5, max_s: float = 180) -> bool
def score(c: Candidate, *, keywords: list[str], trending: list[str], authorized: bool, now: datetime) -> float
# download.py
class DownloadFailed(Exception)
@dataclass
class DownloadResult: video: Path; info: Path | None; capa: Path | None; meta: dict
async def download(url: str, out_dir: Path, on_progress, cookies_browser: str | None = None, *, name: str,
                   require_cc: bool = False, direct_url: str | None = None,
                   client: httpx.AsyncClient | None = None) -> DownloadResult
# stage.py
async def capture_cycle(ctx: Ctx, channel: str, *, list_fn=list_candidates, trends_fn=trending_terms,
                        now: datetime | None = None) -> int
async def download_one(ctx: Ctx, row: VideoRow) -> State      # row em baixando
async def upload_one(ctx: Ctx, row: VideoRow) -> State        # row em enviando
async def download_worker(ctx: Ctx) -> None                   # claim descoberto→baixando
async def drive_upload_worker(ctx: Ctx) -> None               # claim baixado→enviando
```

**Regras:**
- **`listing_url`:**
  - `youtube_canal`: `url.rstrip("/") + "/shorts"`.
  - `youtube_busca`: `f"ytsearchdate{limit}:{busca}"`. Com `so_creative_commons`: `f"https://www.youtube.com/results?search_query={quote_plus(busca)}&sp=EgIwAQ%3D%3D"`.
  - `tiktok_perfil` e `instagram_perfil`: a própria `url`.
- **Extrator padrão:** `yt_dlp.YoutubeDL({"extract_flat": "in_playlist", "quiet": True, "playlistend": limit, "cookiesfrombrowser": ...}).extract_info(url, download=False)` numa thread.
- **`candidate_from_entry`, campos:**
  - `url`: `entry["url"]` ou `webpage_url`.
  - `autor`: `uploader_id`, ganhando `@` na frente se não tiver; se faltar, `channel`, `uploader` ou o rótulo da fonte.
  - `views`: `view_count`.
  - `publicado_em`: `timestamp` (UTC) ou `upload_date` (AAAAMMDD).
  - `capa`: a última de `thumbnails` ou `thumbnail`.
  - **Descartes:** sem url, sem título ou com `live_status` em `is_live`/`is_upcoming`, devolve `None`.
- **Pexels:** `GET https://api.pexels.com/videos/search?query=…&orientation=portrait&per_page=limit` com `Authorization: <chave>`.
  - `autor`: `f"{user.name} · Pexels"`. `licenca`: `"Pexels"`. `capa`: `image`.
  - `direto`: o `video_files` com `height > width` cuja altura fica mais perto de 1920.
- **Pixabay:** `GET https://pixabay.com/api/videos/?key=…&q=…&per_page=…&safesearch=true`, item por item de `hits`.
  - `url`: `pageURL`. `autor`: `f"{user} · Pixabay"`. `views`: `views`. `capa`: `videos.large.thumbnail`.
  - `direto`: `videos.large.url`, ou `medium.url` se o large faltar.
  - Só entram vídeos em pé: a API do Pixabay não filtra orientação, então descarta o item quando o arquivo escolhido tem `width >= height`.
- **Chave faltando:** `SourceError("chave do Pexels não configurada: rode `canaldark auth pexels`")`, e o equivalente para o Pixabay.
- **`trending_terms`:** lê o RSS com o feedparser e guarda os títulos em cache por geo. Antes do `ttl`, não chama a rede de novo. Se falhar, devolve o último cache ou `[]`.
- **`score`:**
  - `vph = views / max(1, horas desde publicado_em)`, ou 0 sem views. Sem data, conta 168 h.
  - `s = 2*log10(1+vph)`.
  - Bônus: +2 se publicado há menos de 48 h; +2 se `matches_any(keywords)`; +1.5 se `matches_any(trending)`; +0.5 se a fonte for autorizada.
  - Resultado: `round(s, 3)`.
  - `normalize` faz casefold e tira acentos (NFKD sem `Mn`). O match é por palavra inteira (`\b`).
- **`passes`:** duração `None` passa; fora de `[min_s, max_s]` falha; título com palavra proibida (normalizada) falha.
- **`download`:**
  - **Via yt-dlp:** `outtmpl = out_dir/f"{name}.%(ext)s"`, `writeinfojson`, `writethumbnail` com o postprocessor `FFmpegThumbnailsConvertor` (`format: jpg`), `merge_output_format: mp4` e `match_filter` de `duration <= 600`. Mantém os `RETRY_DELAYS = (5, 20, 60)` do v1.
  - **Com `require_cc`:** se `"Creative Commons"` não estiver em `info.get("license")`, apaga os arquivos e lança `DownloadFailed("o vídeo não é Creative Commons; ignorado")`.
  - **Com `direct_url`:** baixa o arquivo via httpx em stream, grava um `info.json` mínimo (`{"url": url, "direct_url": direct_url}`) e não usa yt-dlp.
  - **Pedido de login:** se a mensagem do yt-dlp contém `Sign in to confirm` ou `not a bot`, não repete as tentativas e lança `DownloadFailed("o YouTube pediu para confirmar que não é um robô: defina cookies_navegador no canal (ex.: chrome) e tente de novo")`. O `list_candidates` faz o mesmo com `SourceError`.
- **`capture_cycle`:**
  1. Se `count_states(canal, [descoberto, baixando, baixado, enviando, no_drive]) >= cfg.estoque_max`, loga "estoque cheio" e devolve 0.
  2. Se `(await ctx.drive.uso()).fracao >= ctx.plat.drive.limite_uso`, loga um aviso e devolve 0. Quando `uso()` devolve `None`, a `fracao` é `None` (a `PastaDrive` não tem total) ou o `uso()` falha, a captação segue.
  3. Lê cada fonte isolada: a falha de uma vira log de aviso e as outras continuam.
  4. Aplica `passes`, tira os já vistos (`db.seen`), calcula a nota, ordena e respeita `room = cfg.captar_por_dia - vídeos do canal criados hoje (SP)`.
  5. Insere cada vídeo com `autor`, `fonte=spec.rotulo()`, `fonte_autorizada=spec.autorizado`, `tom=spec.tom`, `exige_cc=spec.so_creative_commons`, `nota`, `duracao` e `url_midia=c.direto`, com evento e log.
  6. Usa `ctx.keys` nas fontes do Pexels e do Pixabay.
- **`download_one`:**
  - Chama `download(..., name=str(row.id), out_dir=ctx.data/"captados"/canal, cookies_browser=cfg.cookies_navegador, require_cc=row.exige_cc, direct_url=row.url_midia)`.
  - Move a capa para `ctx.data/"capas"/f"{id}-origem.jpg"`.
  - Grava `caminho_local`, `caminho_capa` e `duracao` (o `meta["duration"]`, se houver), e transiciona para `baixado`.
  - Falha leva a `erro`, com `fila_erro = descoberto`.
- **`upload_one`:**
  - Grava antes `remoto_bruto = pasta(canal, CAPTADOS, nome_do_arquivo)`. Depois envia o `info.json` (se existir) e o vídeo para `pasta(canal, CAPTADOS)`.
  - **Retomada:** se o `caminho_local` não existe mais e o `remoto_bruto` já está gravado, o vídeo já subiu antes da queda; pula o envio.
  - Grava `caminho_local=None` e transiciona para `no_drive`.
  - Falha leva a `erro`, com `fila_erro = baixado`.

- [ ] **Step 1: Escrever os testes que falham**

```python
# tests/test_sources.py
def test_listing_urls(): ...       # "@GaiaMemes/shorts"; "ytsearchdate30:meme gta 6"; CC contém "sp=EgIwAQ%3D%3D"
async def test_youtube_flat_entries_become_candidates(): ...  # extrator falso → autor "@GaiaMemes", views 1000, publicado_em definido
async def test_pexels_picks_portrait_file(): ...              # MockTransport → direto = arquivo em pé mais perto de 1920; autor "Ana · Pexels"
async def test_pixabay_parses_hits(): ...                    # item com large 1920x1080 (deitado) fica de fora
async def test_missing_key_raises_source_error(): ...
async def test_live_entries_are_dropped(): ...                # live_status "is_live" → None
async def test_bot_check_asks_for_cookies(): ...              # extrator falso lança "Sign in to confirm you're not a bot" → SourceError com "cookies_navegador"

# tests/test_scoring.py
def test_score_components():
    c = Candidate(url="u", titulo="Lula no debate", views=3600, publicado_em=now - timedelta(hours=1))
    assert score(c, keywords=["lula"], trending=["Debate"], authorized=True, now=now) == pytest.approx(2*math.log10(3601) + 2 + 2 + 1.5 + 0.5, abs=1e-3)
def test_unknown_views_and_date(): ...   # só os bônus de palavra
def test_passes_filters(): ...           # 200 s falha; 3 s falha; None passa; palavra proibida "vazado" pega "VAZADO"
async def test_trending_terms_cache(): ... # 2 itens; segunda chamada dentro do ttl não chama a rede; falha devolve o cache

# tests/test_capture_stage.py  (fixture ctx com FakeDrive)
async def test_cycle_inserts_best_candidates(ctx): ...   # 5 candidatos, captar_por_dia 3 → 3 maiores notas; autor, fonte e tom gravados
async def test_cycle_skips_seen_and_failed_sources(ctx): ...
async def test_cycle_stops_when_stock_full(ctx): ...
async def test_cycle_stops_when_drive_almost_full(ctx): ... # FakeDrive.usage = 95% → 0 e log de aviso
async def test_download_one_sets_paths(ctx, monkeypatch): ... # download falso cria arquivos → baixado; capa em dados/capas
async def test_upload_one_moves_to_drive(ctx): ...         # no_drive; remoto_bruto "teste/1-captados/<id>.mp4"; o local some; info.json no FakeDrive
async def test_upload_failure_goes_to_erro_baixado(ctx): ...
async def test_upload_resumes_after_crash_mid_move(ctx): ... # local já apagado e remoto_bruto gravado → no_drive sem chamar enviar
async def test_cycle_ignores_drive_without_total(ctx): ...  # FakeDrive.usage = DriveUsage(10, None) → a captação segue
```

- [ ] **Step 2: Rodar e confirmar a falha** — `.venv/bin/python -m pytest tests/test_sources.py tests/test_scoring.py tests/test_capture_stage.py -q`.
- [ ] **Step 3: Implementar.**
- [ ] **Step 4: Rodar e confirmar que passa.**
- [ ] **Step 5: Commit** — `feat: script-only capture (sources, trends, scoring, drive upload)`.

---

### Task 5: Publicação (Facebook, TikTok, destinos, limites, arquivamento)

**Files:**
- Modify: `canaldark/publish/base.py`
- Create: `canaldark/publish/facebook.py`, `canaldark/publish/tiktok.py`
- Rewrite: `canaldark/publish/stage.py`
- Keep: `youtube.py`, `instagram.py`, `tunnel.py` e `schedule.py`. Ajustar só se o `ChannelConfig` v2 quebrar algo.
- Test: `tests/test_facebook.py`, `tests/test_tiktok.py`, `tests/test_publish_stage.py`

**Interfaces:**
- Consumes: T1 (`Ctx`, `VideoRow`, `Destination`, `db.*`, `Drive`, `pasta`, `PUBLICADOS`, `USADOS`, `rules.publish_allowed`), além de `schedule.next_slot`, `youtube.YouTubeUploader` e `instagram.InstagramUploader` do v1.
- Produces:

```python
# base.py
@dataclass
class PublishResult: id_externo: str; url: str; status: Literal["publicado", "rascunho"] = "publicado"
def account_status(platform: str, account: str) -> Literal["conectada", "falta"]   # só keyring, sem rede
# facebook.py
VERSION = "v23.0"; FB_DAILY_CAP = 30
def authorize(account: str, user_token: str, page_id: str, client: httpx.Client | None = None) -> str
class FacebookUploader:
    def __init__(self, account: str, client: httpx.AsyncClient | None = None)
    async def upload(self, video, title, description, tags, on_progress, synthetic=False) -> PublishResult
# tiktok.py
API = "https://open.tiktokapis.com/v2"; TIKTOK_DAILY_CAP = 15
CHUNK = 10 * 1024**2; MAX_SINGLE = 64 * 1024**2
def build_auth_url(client_key: str, redirect_uri: str, state: str, code_challenge: str) -> str
def authorize(account: str, client_key: str, client_secret: str, *, port: int = 8765,
              open_browser=webbrowser.open, client: httpx.Client | None = None) -> str
async def refresh_if_needed(secret: dict, account: str, client: httpx.AsyncClient) -> dict
def chunk_plan(size: int) -> tuple[int, int]     # (chunk_size, total_chunks)
class TikTokUploader:
    def __init__(self, account: str, modo: Literal["rascunho", "publicar"] = "rascunho",
                 client: httpx.AsyncClient | None = None)
    async def upload(self, video, title, description, tags, on_progress, synthetic=False) -> PublishResult
# stage.py
LIMITS_24H = {"instagram": 25, "facebook": 30, "tiktok": 15}; YOUTUBE_DAILY = 90
COOLDOWN_AFTER_LIMIT = timedelta(hours=1); URGENT_SCORE = 12.0
def destinations(row: VideoRow, cfg: ChannelConfig) -> list[Destination]
def caption(row: VideoRow, cfg: ChannelConfig) -> str
def connected(ctx: Ctx, dest: Destination) -> bool
def channel_publishable(ctx: Ctx, cfg: ChannelConfig, now: datetime) -> bool
async def claim_due(ctx: Ctx, now: datetime) -> VideoRow | None
async def publish_video(ctx: Ctx, row: VideoRow) -> State
async def publish_now(ctx: Ctx, video_id: int) -> State      # ignora o horário; levanta PublishBlocked na pausa eleitoral ou se o vídeo não está em editado
class PublishBlocked(Exception)
async def publish_worker(ctx: Ctx) -> None
async def archive_video(ctx: Ctx, row: VideoRow) -> State    # row em arquivando
async def archive_worker(ctx: Ctx) -> None                   # claim publicado→arquivando
class DryRunUploader                                         # do v1; grava em ctx.data/"ensaio"
def real_uploaders(ctx: Ctx) -> Callable[[str, str], Any]    # dry_run → DryRunUploader; tiktok usa o tiktok_modo do canal cujo contas["tiktok"] == conta
```

**Regras:**
- **Facebook:** o segredo é `{"token": page_token, "page_id"}`.
  1. `POST {GRAPH}/{page_id}/video_reels` com `upload_phase=start` devolve `video_id`.
  2. `POST https://rupload.facebook.com/video-upload/{VERSION}/{video_id}` com os headers `Authorization: OAuth <token>`, `offset: 0` e `file_size`, mandando os bytes.
  3. `POST video_reels` com `upload_phase=finish`, `video_id`, `video_state=PUBLISHED` e `description`.
  4. Consultar `GET /{video_id}?fields=status` a cada 5 s, até `status.video_status == "ready"` ou `publishing_phase.status == "complete"` (no máximo 10 min).
  - URL de retorno: `https://www.facebook.com/reel/{video_id}`.
  - Erros: código 190 ou HTTP 401 viram `AuthExpired`; os códigos `{4, 17, 32, 613}` viram `RetryLater`.
  - `authorize`: `GET /{page_id}?fields=access_token,name` com o token de usuário, para guardar o token da Página.
- **TikTok:** o segredo é `{access_token, refresh_token, expires_at (epoch), open_id, client_key, client_secret}`.
  - **`refresh_if_needed`:** quando faltam menos de 5 min para expirar, chama `POST /oauth/token/` com `grant_type=refresh_token` e salva o resultado.
  - **`chunk_plan`:** até `MAX_SINGLE` devolve `(size, 1)`. Acima disso devolve `(CHUNK, size // CHUNK)`, e o último pedaço absorve o resto.
  - **Modo `rascunho`:**
    1. `POST /post/publish/inbox/video/init/` com `{"source_info": {"source": "FILE_UPLOAD", "video_size", "chunk_size", "total_chunk_count"}}`.
    2. `PUT upload_url` de cada pedaço, com `Content-Range: bytes a-b/size` e `Content-Type: video/mp4`.
    3. `POST /post/publish/status/fetch/` com o `publish_id` até `SEND_TO_USER_INBOX` ou `PUBLISH_COMPLETE`; `FAILED` vira `RuntimeError`.
    - Retorno: `PublishResult(publish_id, "", "rascunho")`.
  - **Modo `publicar`:**
    1. `POST /post/publish/creator_info/query/` para ler os níveis de privacidade.
    2. Usar `PUBLIC_TO_EVERYONE` se estiver na lista; se não, o primeiro da lista.
    3. `POST /post/publish/video/init/` com `post_info` (`title` = legenda até 2200 caracteres, `privacy_level`) e `source_info`.
    - Retorno: status `publicado`, com url `https://www.tiktok.com/@{username}` quando disponível ou `""`.
  - **Erros:** `error.code` com `access_token_invalid` vira `AuthExpired`; `rate_limit_exceeded` ou `spam_risk_too_many_posts` viram `RetryLater`.
  - **`authorize`:**
    - PKCE com `code_verifier` aleatório e `code_challenge = base64url(sha256)` sem `=`.
    - Abre o navegador em `build_auth_url` (`https://www.tiktok.com/v2/auth/authorize/` com `client_key`, `scope=user.info.basic,video.upload,video.publish`, `response_type=code`, `redirect_uri`, `state`, `code_challenge` e `code_challenge_method=S256`).
    - Um servidor local em `http://localhost:{port}/callback` recebe o `code`, que é trocado em `/oauth/token/` com o `code_verifier`.
    - Antes de implementar, conferir nos docs atuais do TikTok se o tipo de app aceita esse redirect; se não aceitar, documentar o redirect exigido no `docs/contas.md`.
- **`destinations`:** `json.loads(row.destinos)` validado como `Destination`. Sem isso, `[Destination(plataforma=p, conta=c) for p, c in cfg.contas.items()]`.
- **`caption`:** `f"{row.manchete}\n\nCréditos: {row.autor} ({row.url_origem})"`, mais `"\n\n" + " ".join(cfg.hashtags)` se houver hashtags.
  - O título do YouTube é `row.manchete[:100]`.
  - `synthetic` é sempre `False`.
- **`connected`:** `True` em `ctx.dry_run`. Fora do ensaio, `account_status(...) == "conectada"`.
- **`channel_publishable`:** `publish_allowed(cfg, now)` e pelo menos um destino conectado. Caso contrário, `claim_due` pula o canal.
- **`claim_due`:**
  - Para cada canal em `ctx.channels_for("publicacao")` que esteja publicável, pega o vídeo `editado` de maior nota (`nota DESC, id`) que ainda tenha algum destino pendente e possível agora, ou seja, fora de `ctx.cooldown`, fora de `ctx.paused_accounts` e com limite não atingido.
  - Um vídeo só é elegível se pelo menos um destino dele está conectado e pendente. Destinos não conectados nunca tornam um vídeo elegível; assim um vídeo cujos destinos (padrão ou trocados na aba) estão todos desconectados fica em `editado` e nunca é arquivado sem ter saído em lugar nenhum.
  - Usa `next_slot(cfg, now, publicados_hoje, nota >= URGENT_SCORE, último)`.
  - Faz `claim(editado → publicando, video_id=...)`.
- **`publish_video`:** para cada destino que ainda não está em `publicacoes` com status `publicado`, `rascunho` ou `pulado`:
  - Conta não conectada: grava `pulado` com motivo "conta não conectada".
  - Limite das últimas 24 h atingido (YouTube: 90 no dia em SP somando contas): fica pendente.
  - Upload com sucesso: grava o status do resultado.
  - `AuthExpired`: acrescenta `(plataforma, conta)` em `ctx.paused_accounts`, loga o aviso com o comando `canaldark auth` e fica pendente.
  - `RetryLater`: `ctx.cooldown[(plataforma, conta)] = agora + COOLDOWN_AFTER_LIMIT` e fica pendente.
  - Outra exceção: `erro`, com `fila_erro = editado`.
  - **Resultado:** tudo resolvido leva a `publicado`; algo pendente devolve o vídeo para `editado`.
- **`archive_video`:**
  1. Grava antes `remoto_final = pasta(canal, PUBLICADOS, data_SP, nome_do_arquivo)` e depois chama `enviar(caminho_final, pasta(canal, PUBLICADOS, data_SP))`. **Retomada:** se o `caminho_final` não existe mais e o `remoto_final` já está gravado, pula o envio.
  2. Se `remoto_bruto` existe: com `manter_originais`, `mover(remoto_bruto, pasta(canal, USADOS))`; se não, `apagar(remoto_bruto)`.
  3. Apaga `ctx.data/"editados"/str(id)` e o `caminho_local`, se ainda existir. Grava `caminho_local=None` e `caminho_final=None`. As capas ficam.
  4. Transição para `arquivado`. Falha leva a `erro`, com `fila_erro = publicado`.

- [ ] **Step 1: Escrever os testes que falham**

```python
# tests/test_facebook.py
async def test_three_phase_upload(tmp_path, monkeypatch): ...   # start → rupload (headers offset/file_size) → finish (PUBLISHED, description) → status ready → url ".../reel/<id>"
async def test_expired_token_raises_auth_expired(...): ...
async def test_rate_limit_raises_retry_later(...): ...
# tests/test_tiktok.py
def test_chunk_plan():
    MB = 1024**2
    assert chunk_plan(3*MB) == (3*MB, 1) and chunk_plan(64*MB) == (64*MB, 1)
    assert chunk_plan(100*MB) == (10*MB, 10) and chunk_plan(105*MB) == (10*MB, 10)
async def test_inbox_upload_returns_rascunho(...): ...      # endpoint inbox, Content-Range certo, SEND_TO_USER_INBOX → status "rascunho"
async def test_direct_post_uses_creator_privacy(...): ...   # opções ["SELF_ONLY"] → privacy_level "SELF_ONLY"; PUBLISH_COMPLETE → "publicado"
async def test_refresh_expired_token(...): ...
def test_auth_url_has_pkce(): ...                           # code_challenge, code_challenge_method=S256, video.upload
# tests/test_publish_stage.py  (fixture ctx; uploaders falsos)
def test_destinations_default_and_override(): ...
def test_caption_has_credit_and_hashtags(): ...
async def test_publishes_to_connected_and_skips_missing(ctx, monkeypatch): ...  # dry_run False; só o youtube conectado → youtube 'publicado', os outros 'pulado'; publicado
async def test_no_connected_account_pauses_channel(ctx, monkeypatch): ...       # claim_due None; continua editado
async def test_override_to_unconnected_only_is_never_claimed(ctx, monkeypatch): ...  # canal com youtube conectado, mas o vídeo trocado só para tiktok desconectado → claim_due None
async def test_daily_limit_keeps_video_pending(ctx, monkeypatch): ...           # 25 posts do instagram em 24 h → volta para editado
async def test_retry_later_cooldown_and_auth_expired_pause(ctx, monkeypatch): ...
async def test_blackout_blocks_politics(ctx): ...
async def test_archive_moves_final_and_deletes_original(ctx): ...  # "teste/3-publicados/<data>/<id>.mp4" existe; o original some; a pasta local some; capas ficam; arquivado
async def test_archive_keeps_original_when_configured(ctx): ...
async def test_archive_resumes_after_crash(ctx): ...      # final já no Drive, local apagado, remoto_final gravado → arquivado sem chamar enviar
async def test_publish_now_requires_editado(ctx): ...     # vídeo em pronto → PublishBlocked
async def test_dry_run_copies_to_ensaio(ctx): ...
```

- [ ] **Step 2: Rodar e confirmar a falha** — `.venv/bin/python -m pytest tests/test_facebook.py tests/test_tiktok.py tests/test_publish_stage.py -q`.
- [ ] **Step 3: Implementar.**
- [ ] **Step 4: Rodar e confirmar que passa.** Rodar também `tests/test_schedule.py`.
- [ ] **Step 5: Commit** — `feat: facebook and tiktok uploaders, per-video destinations, archive to drive`.

---

### Task 6: Pipeline, CLI, serviço do macOS e ciclo completo

**Files:**
- Rewrite: `canaldark/pipeline.py`, `canaldark/cli.py`
- Create: `canaldark/service.py`
- Test: `tests/test_pipeline.py`, `tests/test_cli.py`, `tests/test_service.py`

**Interfaces:**
- Consumes: tudo de T1 a T5.
- Produces (o T7 usa isto):

```python
# pipeline.py
CONCURRENCY = {"download": 2, "drive_upload": 1, "pack_download": 1, "edit": 1, "publish": 1, "archive": 1}
def load_keys() -> dict[str, str]                       # env PEXELS_API_KEY/PIXABAY_API_KEY ou keyring pexels:chave/pixabay:chave
async def build_ctx(root: Path, *, dry_run: bool) -> Ctx  # lê plataforma.yaml, canais/, estilos/, memes/; abre dados/plataforma.db; EventBus; make_drive; real_uploaders
class Pipeline:
    def __init__(self, ctx: Ctx)                         # define ctx.active = self.active
    ctx: Ctx
    paused: dict[tuple[str | None, str | None], datetime | None]
    def pause(self, channel: str | None = None, stage: str | None = None, until: datetime | None = None) -> None
    def resume(self, channel: str | None = None, stage: str | None = None) -> None
    def is_paused(self, channel: str, stage: str) -> bool
    def active(self, stage: str) -> list[str]
    async def capture_now(self, channel: str) -> int
    async def start(self, capture: bool = True) -> None  # recover() + loops de captação por canal (intervalo do canal) + pack_manager + workers de CONCURRENCY
    async def stop(self) -> None
# cli.py (argparse, prog "canaldark")
#   run [--ensaio] [--porta N] [--raiz DIR]          porta padrão = plataforma.yaml; confere ffmpeg; uvicorn timeout_graceful_shutdown=3
#   auth youtube <conta> [--segredo dados/client_secret.json]
#   auth instagram <conta> --ig-user-id ID
#   auth facebook <conta> --page-id ID               (token de usuário pedido por getpass)
#   auth tiktok <conta> --client-key K --client-secret S [--porta-retorno 8765]
#   auth pexels | auth pixabay                       (chave por getpass → keyring)
#   servico instalar [--porta N] [--ensaio] | servico remover
# service.py
LABEL = "com.canaldark.plataforma"
def plist_path() -> Path                                  # ~/Library/LaunchAgents/com.canaldark.plataforma.plist
def plist_xml(repo: Path, executable: Path, port: int, ensaio: bool) -> str
def install(repo: Path, port: int, ensaio: bool, run=subprocess.run) -> Path   # grava; launchctl bootout (ignora erro) + bootstrap gui/<uid>
def remove(run=subprocess.run) -> None
```

**O plist:**
- `ProgramArguments`: `[<repo>/.venv/bin/canaldark, run, --porta, N, (--ensaio)]`.
- `WorkingDirectory`: o repo.
- `EnvironmentVariables`:
  - `PATH`: `/opt/homebrew/opt/ffmpeg-full/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin`;
  - `PYTHONPATH`: o repo.
- `RunAtLoad` e `KeepAlive`: true.
- Saídas: `StandardOutPath` em `<repo>/dados/plataforma.log`, `StandardErrorPath` em `<repo>/dados/plataforma.err`.

- [ ] **Step 1: Escrever os testes que falham**

```python
# tests/test_pipeline.py
async def test_full_cycle_in_ensaio(tmp_path, monkeypatch):
    # raiz temporária com cópias de estilos/ e memes/*.yaml; plataforma.yaml com pacote: 2; um canal com uma fonte youtube_busca
    # list_candidates falso devolve 3 candidatos; download falso gera um clipe real de 2 s; PastaDrive; dry_run=True
    # start(); espera até 60 s (consultando o banco) até 2 arquivados e 1 em no_drive
    assert len(list((root/"dados/drive/CanalDark/teste/3-publicados").rglob("*.mp4"))) == 2
    assert len(list((root/"dados/ensaio").glob("*.mp4"))) >= 2 and not any((root/"dados/pacote").rglob("*.mp4"))
async def test_recover_on_start(tmp_path): ...       # vídeo em editando antes do start → pronto
async def test_pause_stage_stops_claims(tmp_path): ...  # pause(stage="edicao") → nada sai de pronto
# tests/test_cli.py
def test_run_port_defaults_from_platform_file(tmp_path): ...
def test_auth_subcommands_exist(): ...                # youtube, instagram, facebook, tiktok, pexels, pixabay
# tests/test_service.py
def test_plist_has_keepalive_path_and_pythonpath(tmp_path): ...
def test_install_writes_and_bootstraps(tmp_path, monkeypatch): ...   # run falso grava ["launchctl","bootstrap",f"gui/{uid}", caminho]
```

- [ ] **Step 2: Rodar e confirmar a falha.**
- [ ] **Step 3: Implementar.**
- [ ] **Step 4: Rodar a suíte inteira.** `.venv/bin/python -m pytest -q`. Esperado: tudo verde.
- [ ] **Step 5: Commit** — `feat: v2 pipeline wiring, cli and launchd service`.

---

### Task 7: Painel web de 3 abas

**Files:**
- Rewrite: `canaldark/web/app.py`, `canaldark/web/static/index.html`, `canaldark/web/static/app.js`, `canaldark/web/static/style.css`
- Create: `docs/painel.png`, uma captura da aba Edição com dados de demonstração
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes:
  - de T1: `Ctx`, `db.*`, `TAB_STATES`, `TAB_OF_QUEUE`, `replace_sources`, `load_channels`;
  - de T2: `styles.available_styles`;
  - de T5: `publish.stage.publish_now`, `PublishBlocked`, `destinations`, `base.account_status`;
  - de T6: a interface do `Pipeline`. Nos testes, usar um `FakePipeline` com `ctx`, `paused`, `pause`, `resume`, `is_paused` e `capture_now`.
- Produces: `create_app(pipeline, port: int) -> FastAPI` e a API abaixo, com JSON em português.

| Método e rota | Corpo ou parâmetros | Efeito |
|---|---|---|
| GET `/api/state` | — | `{canais:[{id,nome}], ensaio, pausas:[{channel,stage,until}], drive:{tipo, uso:{usado,total,fracao}\|null}, tendencias:[str], pacotes:{canal:{numero,feitos,total}}}` |
| GET `/api/videos` | `aba`, `channel?`, `limit=30`, `before_id?` | lista de cards (abaixo); `erro` aparece na aba de `TAB_OF_QUEUE[fila_erro]` |
| GET `/api/videos/{id}` | — | linha completa + `publicacoes` |
| POST `/api/videos/{id}/retry` | — | `erro` → `fila_erro` |
| POST `/api/videos/{id}/discard` | — | apaga os arquivos locais, `remoto_bruto` no Drive e as capas; → `descartado` (não vale para `arquivado`) |
| POST `/api/videos/{id}/approve` | — | `revisar` → `editado` |
| POST `/api/videos/{id}/style` | `{estilo}` | o estilo tem que estar disponível (se não, 422); grava; `editado`, `revisar` ou `erro` com fila `pronto` → `pronto`; nos estados de trabalho, em `publicado`, `arquivado` e `descartado` devolve 409; nos outros, só grava |
| POST `/api/videos/{id}/text` | `{manchete}` | de 1 a 120 caracteres depois do strip (se não, 422); mesma regra de refazer |
| POST `/api/videos/{id}/destinations` | `{destinos:[{plataforma,conta}]}` | valida como `Destination` e grava o JSON |
| POST `/api/videos/{id}/publish-now` | — | `publish_now`; `PublishBlocked` → 409 |
| GET `/api/sources` | `channel` | `[{index, tipo, url, busca, tom, autorizado, so_creative_commons, rotulo}]` |
| POST `/api/sources` | `{channel, tipo, url?, busca?, tom, autorizado, so_creative_commons}` | acrescenta e chama `replace_sources`; atualiza `ctx.cfgs[channel]` |
| PATCH `/api/sources` | `{channel, index, tom?, autorizado?, mover_para?}` | altera, ou move para outro canal (grava os dois arquivos) |
| DELETE `/api/sources` | `channel`, `index` | remove |
| GET `/api/styles` | — | `[{nome, rotulo, disponivel, motivo}]` |
| GET `/api/accounts` | — | `{plataforma:[{conta, status}]}`, com `status` ∈ `conectada`, `falta`, `expirada` (está em `ctx.paused_accounts`), `ensaio` |
| POST `/api/pause`, `/api/resume` | `{channel?, stage?, until?}` | como no v1 (horário sem fuso = SP); `resume` sem canal e sem etapa também esvazia `ctx.paused_accounts` |
| POST `/api/capture-now` | `channel` | `{novos: n}` |
| GET `/media/{id}/{nome}` | `final.mp4`, `local.mp4`, `origem.jpg`, `final.jpg` | arquivo, ou 404 |
| GET `/fonte.ttf` | — | `FONT` |
| GET `/`, `/static/*` | — | os arquivos do painel com `Cache-Control: no-cache` (no Mac, o navegador guardava versões velhas do painel v1) |
| WS `/ws` | — | os eventos do bus (como no v1) |

**Campos de cada card:**
- identificação: `id`, `canal`, `titulo`, `manchete`, `autor`, `fonte`, `autorizada`, `tom`;
- estado: `estado`, `status`, `progresso`, `erro`, `nota`;
- edição: `estilo`, `pacote`;
- publicação: `destinos` (efetivos), `publicacoes:[{plataforma,conta,status,url}]`, `proximo_horario`, que é ISO, só para `editado`;
- mídia: `capa`, que é `final.jpg` se existir, senão `origem.jpg`, senão null; `video`, que é `final.mp4` se o arquivo local existir; `local`, que é o `local.mp4` da fonte se existir.

**O campo `status` por aba:**
- `andamento`: os estados de trabalho `baixando`, `enviando`, `descendo`, `editando`, `publicando` e `arquivando`.
- `fila`: `descoberto`, `baixado`, `separado` e `pronto`, e também `editado` quando está na aba Publicação.
- `ok`: `no_drive`, `arquivado` e `publicado`, e também `editado` quando está na aba Edição.
- `revisar` para `revisar`; `erro` para `erro`.

**Interface (PT-BR), regras:**
- **Topo:** "Canal Dark", abas `Captação | Edição | Publicação` (`role="tab"`, `aria-selected`), com hash `#captacao`, `#edicao` e `#publicacao`, e lembrando a última aba. Também tem o select de canal, o selo "ensaio", "Buscar agora", "Pausar tudo" e o indicador "ao vivo/reconectando".
- **Captação:**
  - Uma coluna à esquerda com:
    - "Fontes do canal", com rótulo, tipo, chip de tom, interruptor autorizado/sem permissão, select "mover para" e remover;
    - um formulário com tipo, url/busca, tom, autorizado e Creative Commons;
    - "Em alta agora no Brasil";
    - o medidor do Drive (barra e %) e o estoque do canal.
  - À direita, a grade de prévias.
- **Edição:**
  - Para cada canal, o cabeçalho "Pacote N · X de 15 editados".
  - Em cada prévia, o select de estilo (os indisponíveis ficam desabilitados com o motivo no `title`) e o campo do texto, que salva com Enter ou com o botão "Salvar".
  - Botões "Refazer" (manda o estilo atual de novo), "Aprovar" (só em `revisar`) e "Descartar".
- **Publicação:**
  - Em cada prévia, o próximo horário e, por rede, um select de conta com as contas daquela rede em todos os canais mais a opção "não publicar". Também o botão "Publicar agora", os links e os chips `publicado`, `rascunho` ou `pulado`.
  - No fim, o quadro "Contas", com rede × conta, o status e o comando para conectar (ex.: `canaldark auth tiktok politica --client-key … --client-secret …`).
- **Prévia** (componente comum):
  - É uma moldura de celular 9:16.
  - Com `video`, mostra `<video muted loop playsinline>`, que toca quando visível (IntersectionObserver). Tirar o `hidden` antes de chamar `observe`.
  - Sem `video`, mostra uma montagem do estilo: faixa branca com a manchete (ou o título) em `@font-face` vinda de `/fonte.ttf`, e no meio o `local` em vídeo ou a `capa` em imagem.
  - Mostra o selo de estado, a barra de progresso embaixo, o texto de erro e "Tentar de novo".
  - O CSS tem `[hidden]{display:none !important}`.
- **Pausar por aba:** cada aba tem o botão "Pausar"/"Retomar" da sua etapa (POST `/api/pause` ou `/api/resume` com `stage`), além do "Pausar tudo" do topo.
- **Detalhe:** clicar numa prévia abre um painel com o vídeo com som (`controls`), os dados da linha e as publicações, vindos de GET `/api/videos/{id}`.
- **Log:** um painel recolhível embaixo.
- **Largura de celular:** abaixo de 900 px vira uma coluna só, com margem lateral de 16 px e sem rolagem horizontal.
- **Fonte de inspiração:** a versão de prévias 9:16 guardada no `git stash` (`git stash show -p stash@{0}`) pode servir de referência para o componente de prévia.

- [ ] **Step 1: Escrever os testes que falham**

```python
# tests/test_web.py  (TestClient base_url "http://localhost:8001"; FakePipeline sobre a fixture ctx)
def test_tabs_list_videos_by_state(...): ...      # descoberto+no_drive na captação; pronto+editado na edição; editado+arquivado na publicação
def test_error_shows_in_tab_of_its_queue(...): ...
def test_style_change_rerenders(...): ...          # editado → POST style → pronto e estilo gravado; desconhecido → 422
def test_text_change_validates(...): ...
def test_destinations_override_saved(...): ...
def test_sources_add_toggle_move_delete(...): ...  # cópia temporária de canais/; YAML atualizado, comentários mantidos, ctx.cfgs atualizado
def test_accounts_status(...): ...
def test_state_has_pack_progress_and_drive(...): ...
def test_discard_removes_local_and_remote(...): ...
def test_local_only_blocks_other_hosts(...): ...
def test_media_routes(...): ...
def test_ws_streams_events(...): ...               # header host localhost:8001
```

- [ ] **Step 2: Rodar e confirmar a falha.**
- [ ] **Step 3: Implementar a API e a interface.**
- [ ] **Step 4: Rodar e confirmar que passa, e fazer uma verificação visual.** Rodar um script de rascunho (fora do repo) que sobe o app com um banco de demonstração, com vídeos em vários estados e um MP4 gerado. Tirar capturas das 3 abas em 1440×1000 e em 390×844 com o Playwright (`executablePath: /opt/pw-browsers/chromium`; o Chromium sem codecs não toca H.264, então gerar o MP4 de demo com VP9 em `.mp4`). Conferir cada captura. A da aba Edição em 1440×1000 vira `docs/painel.png`.
- [ ] **Step 5: Commit** — `feat: three-tab panel with 9:16 previews, sources, styles and destinations`.

---

### Task 8: Documentação

**Files:**
- Rewrite: `README.md`, `docs/contas.md`
- Modify: `memes/LEIAME.md`, para revisar o texto da T1

**Interfaces:**
- Consumes: os comandos exatos da T6 (lista na Task 6) e as configs da T1.
- Produces: docs em PT-BR, sem promessa que o código não cumpre.

**Conteúdo:**
- **`README.md`:**
  - o que a plataforma faz (3 abas, ciclo do Drive, pacote de 15);
  - instalação no macOS: Python 3.12 via uv, `brew install ffmpeg-full deno rclone`, `pip install -e ".[ml]"` e `chflags nohidden .venv/lib/python3.12/site-packages/*.pth`;
  - primeiro uso: `canaldark run --ensaio` e a URL `http://localhost:8001`;
  - onde ficam as configs (`plataforma.yaml`, `canais/`, `estilos/`, `memes/`);
  - como colocar os clipes dos memes;
  - como trocar para o Drive real (rclone);
  - como deixar a plataforma ligada sempre (`canaldark servico instalar`);
  - avisos: crédito e risco de strike com fontes sem permissão, conteúdo reutilizado no YouTube, pausa eleitoral;
  - diferenças em relação ao v1.
- **`docs/contas.md`:**
  - **Contas:** quais criar (a tabela da pesquisa, seção 2.5) e a regra da Meta (pessoa real como admin, sem perfis falsos).
  - **Google:** um projeto no Google Cloud com Drive API + YouTube Data API, consentimento externo e app publicado, e um cliente "App para computador". Em seguida, o `rclone config` com o client próprio e o `canaldark auth youtube <conta>`.
  - **Meta:** um app Empresa com Instagram e Facebook, `canaldark auth instagram`/`facebook`, e o token de 60 dias.
  - **TikTok:** o app de desenvolvedor, a auditoria (privado até aprovar; o modo `rascunho` manda para a caixa de entrada) e o `canaldark auth tiktok`.
  - **Pexels e Pixabay:** onde criar a chave grátis e o `canaldark auth pexels|pixabay`.
  - **YouTube:** o aviso sobre "privado até auditoria" e como pedir a auditoria gratuita.
  - Todos os links vêm da pesquisa em `/mnt/project-files/canal-dark/pesquisa/automacao-e-canais.md`.

- [ ] **Step 1: Escrever** os documentos.
- [ ] **Step 2: Conferir os comandos.** Comparar cada comando citado com `canaldark --help` e com o `--help` de cada subcomando, no branch que já tem a T6. Esperado: todos existem com esses nomes.
- [ ] **Step 3: Commit** — `docs: v2 setup, accounts and drive guide`.

---

## Depois das tasks

**Revisão do branch inteiro:**
- Pacote de revisão de `03e6362..HEAD`.
- Revisor no modelo mais capaz, com o spec, este plano e a seção Review Focus.
- Achados Critical e Important são corrigidos com TDD numa passada.
- Achados Minor ficam listados para o André.

**Verificação no Mac do André:**
1. O v1 continua rodando na porta 8000, na pasta atual. As mudanças locais dele ficam guardadas num branch local `v1-ensaio`, fora deste PR.
2. O v2 roda numa pasta separada: `git fetch origin feat/v1 && git worktree add ../canal-dark-v2 origin/feat/v1`, um `.venv` próprio, `pip install -e ".[ml]"` e `canaldark run --ensaio` (porta 8001). Cada pasta tem o seu `dados/`, então os dois não se misturam.
3. Confere as 3 abas em `http://localhost:8001` e deixa um pacote rodar até `arquivado` com fontes reais do YouTube.
