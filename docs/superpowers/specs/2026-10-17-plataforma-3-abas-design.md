# Canal Dark v2: plataforma de 3 abas, só com scripts

Data: 17/10/2026. Substitui o painel e o "cérebro" do v1 (`2026-10-16-canal-dark-design.md`); o motor de baixar, renderizar e publicar continua.

## 1. Objetivo

Uma plataforma local, aberta em `http://localhost:8001`, que capta Shorts de outros canais, guarda tudo no Google Drive, baixa pacotes de 15 vídeos por canal, edita cada vídeo num estilo padrão, publica sozinha nas redes de cada canal, devolve o vídeo final ao Drive e apaga a cópia local. Tudo é feito por scripts Python, sem IA. A única exceção opcional é a legenda automática do estilo karaokê, que usa o faster-whisper local.

**Sucesso:** com as contas conectadas, o André abre a plataforma, vê as 3 abas trabalhando sozinhas e só intervém quando quer trocar um estilo, um texto, uma conta ou descartar um vídeo.

## 2. Decisões do André (17/10)

1. O texto da faixa vem do **título original**, por regras fixas, sem IA.
2. Cada canal **publica sozinho** nas redes e contas padrão. Os menus da aba Publicação servem para mudar um vídeo específico.
3. O pacote do Drive é de **15 vídeos por canal**.
4. **Uma conta por tema em cada rede**, misturando vídeos sérios e de meme.
5. Nos memes do Shrek e do DiCaprio, a **capa do vídeo captado** é a imagem e o título é o texto, tudo automático.
6. A captação puxa **também de canais sem permissão escrita**, sempre com crédito ao autor. O André aceitou o risco de strike. Cada fonte fica marcada como "autorizado" ou "sem permissão".
7. **As 3 telas** (parte 1 do desenho) estão aprovadas: abas Captação, Edição e Publicação no topo, com o log embaixo. O chat com o Claude sai.

Ficou definido por mim, sem pergunta: o estilo de edição é um **padrão por canal** com menu para trocar por vídeo, e a plataforma **evolui o código atual** do Canal Dark em vez de começar outro app.

## 3. Os 4 canais

| Canal (id) | Tema | Fontes iniciais |
|---|---|---|
| `politica` | Eleições 2026: Lula e Flávio Bolsonaro | busca no YouTube "Lula Flávio Bolsonaro", @investigapinhel |
| `tecnologia` | Tecnologia e IA | busca "tecnologia IA celular", @AlmanaqueSOS |
| `jogos` | Jogos, GTA 6 | busca "GTA 6", busca "meme games", @GaiaMemes |
| `bebes_gatos` | Bebês e gatos | Pexels, Pixabay, busca Creative Commons "gatos fofos" |

O canal de cada um dos 3 canais que o André passou é provisório: eu não consegui abrir os vídeos daqui. Ele move qualquer fonte de canal na aba Captação. Os ids `gta6` e `ia` do v1 viram `jogos` e `tecnologia`.

No canal de bebês, o padrão é material licenciado (Pexels, Pixabay, Creative Commons). Repostar crianças de perfis de terceiros tem risco legal maior (ECA e LGPD), então essas fontes não entram por padrão.

## 4. Ciclo de vida de um vídeo

Cada etapa é feita por um "trabalhador" que pega um vídeo de uma fila, trabalha nele e o deixa na fila seguinte.

| Aba | Fila → trabalhando → resultado | O que acontece |
|---|---|---|
| Captação | `descoberto` → `baixando` → `baixado` | o yt-dlp baixa o vídeo, o `info.json` e a capa para `dados/captados/<canal>/` |
| Captação | `baixado` → `enviando` → `no_drive` | o vídeo e o `info.json` sobem para `<canal>/1-captados/` no Drive e saem do disco; a capa (jpg pequeno) fica local para a prévia |
| Edição | `no_drive` → `separado` | quando o pacote do canal acaba, os próximos 15 são separados (maior nota primeiro, depois os mais antigos) e movidos no Drive para `2-em-edicao/` |
| Edição | `separado` → `descendo` → `pronto` | o vídeo desce do Drive para `dados/pacote/<canal>/` |
| Edição | `pronto` → `editando` → `editado` | o FFmpeg monta o estilo escolhido e gera a capa do vídeo final |
| Publicação | `editado` → `publicando` → `publicado` | no horário do canal, o vídeo sobe para cada destino |
| Publicação | `publicado` → `arquivando` → `arquivado` | o final sobe para `3-publicados/AAAA-MM-DD/`, o original sai do Drive e a pasta local é apagada |

Também existem estes estados:
- `erro`: guarda a fila de onde o vídeo saiu, e "Tentar de novo" o devolve para lá.
- `revisar`: só aparece se o canal tiver `revisar_antes_de_publicar: true`, que vem desligado nos 4.
- `descartado`: apaga os arquivos locais e do Drive, e o vídeo nunca volta, porque o link fica na lista de vistos.

**Regras do pacote:**
- Cada canal tem no máximo 15 vídeos locais entre `separado` e `arquivando`.
- O próximo pacote só desce quando o atual termina. Um vídeo em `erro` não segura o pacote: ele conta como terminado e mantém os arquivos locais até alguém tentar de novo ou descartar.
- **Estoque máximo:** a captação de um canal pausa quando ele tem `estoque_max` vídeos em `no_drive` (padrão 60). Isso protege os 15 GB do Drive grátis.

**Se a plataforma cair**, cada estado "trabalhando" volta para a sua fila ao reiniciar:
- `baixando` → `descoberto`
- `enviando` → `baixado`
- `descendo` → `separado`
- `editando` → `pronto`
- `publicando` → `editado`
- `arquivando` → `publicado`

## 5. Captação

**Tipos de fonte** (em `canais/<canal>.yaml` e editáveis na aba):

| Tipo | Exemplo | Como lê |
|---|---|---|
| `youtube_canal` | `https://www.youtube.com/@GaiaMemes` | `yt-dlp --flat-playlist` na aba `/shorts` |
| `youtube_busca` | busca "meme gta 6", opção `so_creative_commons` | `yt-dlp "ytsearchdate30:..."`; com a opção ligada, só baixa se o campo `license` for Creative Commons |
| `tiktok_perfil` | `https://www.tiktok.com/@perfil` | yt-dlp, com cookies do navegador se pedir login |
| `instagram_perfil` | `https://www.instagram.com/perfil/` | yt-dlp com cookies; **experimental**, porque o Instagram bloqueia com frequência |
| `pexels` / `pixabay` | busca "cute cat" | API grátis (o André cria a chave), só vídeos em pé |

Cada fonte tem:
- `tom: serio | meme`, que escolhe a tabela de estilos;
- `autorizado: true | false`, padrão false, que aparece como selo na aba;
- o crédito, preenchido sozinho com o @ do autor de cada vídeo.

**Nota de cada candidato, sem IA:**
- views por hora desde a publicação, em escala log;
- bônus se o vídeo tem menos de 48 h;
- bônus se o título tem uma `palavras_chave` do canal ou um termo em alta no Google Trends Brasil (RSS `trends.google.com/trending/rss?geo=BR`);
- bônus pequeno para fonte autorizada.

**Filtros:** de 5 a 180 s, sem live, nunca visto antes e sem `palavras_proibidas` do canal. A cada `intervalo_captacao_min`, entram no máximo `captar_por_dia` vídeos por canal, sempre os de maior nota.

A aba Captação mostra a lista "Em alta agora no Brasil", vinda do RSS do Google Trends.

## 6. Edição

**Texto da faixa** (`canaldark/manchete.py`), a partir do título original:
1. Tira links, @menções, todas as #hashtags, emojis, palavras de lixo ("shorts", "fyp", "viral") e separadores nas pontas (`|`, `-`, `•`).
2. Junta espaços e reduz pontuação repetida ("!!!" vira "!").
3. Mantém maiúsculas e minúsculas como vieram.
4. Corta em 90 caracteres na última palavra inteira, com "…".
5. Se sobrar menos de 3 letras, usa a próxima frase da lista `manchete_padrao` do canal, em rodízio.

O André pode reescrever o texto na aba Edição, e o vídeo é refeito.

**Estilos** (presets em `estilos/<nome>.yaml`; medidas para 1080×1920):

| Estilo | Como fica |
|---|---|
| `faixa_cima` | faixa branca em cima com altura automática (300, 420 ou 540 px para 1 a 3 linhas), texto preto em Montserrat ExtraBold de 60 a 72 px; o vídeo preenche o resto (escala e recorte central); crédito cinza pequeno no canto da faixa |
| `faixa_dupla` | faixa de 480 px em cima com o texto, vídeo de 960 px no meio, faixa de 480 px embaixo com o `texto_base` do canal e o crédito |
| `comentario` | fundo com o próprio vídeo borrado, cartão branco de cantos arredondados com avatar genérico, "@autor" e o texto como comentário, e o vídeo dentro do cartão |
| `borrado` | vídeo inteiro no centro sobre uma cópia ampliada e borrada; texto branco com contorno no topo |
| `karaoke` | vídeo em tela cheia, faixa com o texto em cima, legenda palavra por palavra (palavra atual em amarelo) gerada pelo faster-whisper; sem o faster-whisper, sai sem legenda e avisa no log |
| `meme_shrek`, `meme_dicaprio` | faixa com o texto em cima; abaixo, a capa do vídeo captado e o clipe do meme, um em cima do outro; dura o tamanho do clipe |

- **Texto:** os textos e o cartão são desenhados com Pillow (quebra de linha e tamanho medidos de verdade) e entram no FFmpeg como imagem. Só o karaokê depende do libass (`ffmpeg-full` no Mac).
- **Memes:** cada meme é um `memes/<nome>.yaml` mais o clipe limpo (`memes/shrek.mp4`, `memes/dicaprio.mp4`). O André coloca os clipes, porque daqui eu não consigo baixar do YouTube. Sem o clipe, o estilo aparece como "falta o clipe" e o vídeo sai em `faixa_cima`.
- **Escolha automática do estilo:** cada canal tem duas tabelas de pesos, uma para fontes de tom sério e outra para tom meme. Por exemplo, sério: `faixa_cima 0.5, faixa_dupla 0.3, karaoke 0.2`; meme: `comentario 0.4, meme_shrek 0.3, meme_dicaprio 0.3`. O sorteio nunca repete o mesmo estilo 3 vezes seguidas no canal, porque variar ajuda contra a regra de "conteúdo inautêntico" do YouTube.
- **Áudio:** o original continua. Nos memes, entra o som do clipe do meme. Não há narração por voz sintética.
- **Saída:** H.264 + AAC a 30 fps, mais a capa do vídeo final.

**Fica para depois:** o estilo "comparação lado a lado" precisa de dois vídeos de origem por post, o que muda a regra do pacote. Sai desta versão.

## 7. Publicação

- **Redes:**
  - YouTube e Instagram: os uploaders do v1 continuam.
  - Facebook: novo, pela API de Reels de Página.
  - TikTok: novo, pela Content Posting API.
- **Destinos de cada vídeo:** vêm de `contas:` do canal, por exemplo `{youtube: politica, instagram: politica, facebook: politica, tiktok: politica}`. Mudar a rede ou a conta de um vídeo na aba grava o destino só naquele vídeo.
- **Horários:** valem os `horarios` e o `max_por_dia` de cada canal, com a tolerância de 60 min e a prioridade para nota alta do v1.
- **Limites por conta:**
  - Instagram: 25 em 24 h.
  - Facebook: 30 em 24 h.
  - TikTok: 15 em 24 h, um número conservador, porque os limites reais não são publicados.
  - YouTube: 90 por dia somando todos os canais, porque o limite de 100 é por projeto do Google.
- **TikTok:** até o app passar na auditoria, o modo padrão é `rascunho`. O vídeo vai para a caixa de entrada do TikTok e o André publica pelo celular. Depois da auditoria, basta trocar para `publicar` no YAML.
- **Conta não conectada:**
  - Se só alguns destinos estão conectados, o vídeo vai para eles e os outros ficam marcados como "pulado".
  - Se nenhum destino do canal está conectado e a plataforma não está em ensaio, a publicação do canal fica em pausa com um aviso. Assim nenhum pacote é arquivado sem ter sido postado.
- **Legenda do post:** texto da faixa, `Créditos: @autor (link)` e as `hashtags` do canal. Sem rótulo de IA, porque nada é sintético.
- **Pausa eleitoral:** o v1 já pausa o canal de política de 22 a 26/10. Isso é mais rígido que a regra do TSE, que só proíbe conteúdo alterado por IA nesse período. Fica ligado por padrão e pode ser desligado com `pausa_eleitoral: false`.
- **Ensaio** (`--ensaio`): faz tudo, inclusive o ciclo do Drive, mas copia os vídeos para `dados/ensaio/` em vez de postar.

## 8. Google Drive

- **Interface `Drive`** com `enviar`, `baixar`, `mover`, `apagar` e `uso`. Tem duas implementações:
  - `RcloneDrive`: chama o `rclone` com o client OAuth do próprio André, porque o client compartilhado do rclone deixa de funcionar em 2026.
  - `PastaDrive`: usa uma pasta local, `dados/drive/` por padrão. É o padrão até o rclone estar configurado e também serve nos testes.
- **Pastas no Drive:** `CanalDark/<canal>/1-captados`, `2-em-edicao`, `3-publicados` e, só com `manter_originais: true`, `4-originais-usados`.
- **Uso do Drive:** a aba Captação mostra o espaço usado, pelo `rclone about`. Acima de 90%, a captação pausa e aparece um aviso.
- **Configuração global** em `plataforma.yaml`: `porta: 8001`, `drive: {tipo: pasta|rclone, remote: "gdrive:", pasta: CanalDark, manter_originais: false}` e `pacote: 15`.

## 9. As 3 telas (aprovado na parte 1)

- **Topo:** "Canal Dark", as abas Captação, Edição e Publicação, o filtro de canal, o selo de ensaio, "Pausar tudo" e o estado da conexão.
- **Captação:** fontes do canal (colar link, tipo, tom, selo autorizado, remover), "Em alta agora", medidor do Drive e estoque por canal, e as prévias em pé com barra de progresso. Tem os botões "Buscar agora" e "Pausar".
- **Edição:** "Pacote 3 · 7 de 15" por canal e as prévias 9:16. Antes de editar, a prévia é uma montagem do estilo no navegador; depois, é o MP4 final em loop, sem som. Cada prévia tem o menu de estilo, o campo do texto, "Refazer" e "Descartar".
- **Publicação:** a fila por horário com o próximo horário de cada vídeo, os destinos com o menu de rede e conta, "Publicar agora", os links do que já saiu e os erros. Embaixo, o quadro de contas mostra cada rede e conta como conectada, falta conectar ou expirada, com o comando para conectar.
- **Log ao vivo:** embaixo das três abas, recolhível. Tudo atualiza pelo WebSocket do v1.
- **Detalhe:** clicar numa prévia abre o vídeo com som e os dados.

## 10. Rodar sozinho no Mac

- `canaldark run --porta 8001 [--ensaio]` sobe tudo.
- `canaldark servico instalar` grava um LaunchAgent (`~/Library/LaunchAgents/com.canaldark.plataforma.plist`) com `RunAtLoad` e `KeepAlive`. O PATH inclui o `ffmpeg-full` e o PYTHONPATH aponta para a pasta, porque o macOS esconde os `.pth` do `.venv`. Assim a plataforma liga com o Mac e volta sozinha se cair. `canaldark servico remover` desfaz.
- **Conectar contas:** `canaldark auth youtube|instagram|facebook|tiktok <conta>`.
- **Guia:** `docs/contas.md` passa a cobrir as 4 redes, o rclone e as chaves do Pexels e do Pixabay, com o que a pesquisa encontrou:
  - TikTok: os posts ficam privados até a auditoria.
  - YouTube: os uploads podem ficar privados até uma auditoria gratuita.
  - Meta: exige uma pessoa real como administradora.

## 11. O que sai do código

- O cérebro (`brain.py`, `claude -p` e Ollama).
- O chat e o servidor MCP.
- A voz Kokoro.
- As fontes `google_news`, `rss` e `reddit`, porque não trazem vídeo.
- Os formatos `noticia` e `dialogo`.
- As regras escritas para a IA.
- As dependências `mcp` e `kokoro`.

O banco é novo (`dados/plataforma.db`), e o `dados/canaldark.db` do v1 fica intocado.

## 12. Erros

- **Isolamento:** cada trabalhador trata um vídeo por vez e nunca para a fila por causa de um vídeo. O erro vai para a tela com a mensagem e o botão "Tentar de novo".
- **rclone:** 3 tentativas, com espera de 5, 20 e 60 s, antes de virar `erro`.
- **Drive cheio ou não configurado:** aviso na aba Captação.
- **yt-dlp:** quando aparece "confirme que não é um robô", a mensagem pede os cookies do navegador (`cookies_navegador`).
- **Publicação:**
  - Token vencido: pausa só aquela conta, com aviso.
  - Limite atingido: tenta de novo em 1 h.
  - Outra falha: vira `erro`.

## 13. Testes

**Testes automáticos (pytest), sem rede:**
- regras do texto, com os 15 títulos reais dos exemplos;
- sorteio de estilo com a regra das 3 repetições;
- nota da captação;
- separação de pacote;
- máquina de estados e retomada após queda;
- `PastaDrive` e os comandos do `RcloneDrive` com subprocesso falso;
- cada estilo renderizado de verdade com o FFmpeg num clipe gerado (confere 1080×1920, duração, áudio e a cor da faixa);
- uploaders de Facebook e TikTok com respostas HTTP falsas;
- API do painel;
- um ciclo completo em ensaio, de fonte falsa até `arquivado`, usando a `PastaDrive`.

**Verificação manual:** captura de tela das 3 abas. No Mac do André, um pacote de verdade em ensaio.

## 14. Fora desta versão

- O estilo comparação.
- O TikTok público antes da auditoria.
- Apagar posts nas redes.
- Downloads confiáveis do Instagram e do Facebook (ficam como experimentais).
- Narração por voz.
- Qualquer IA além da legenda local.
