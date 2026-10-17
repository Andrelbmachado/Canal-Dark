# Clipes dos memes

Os estilos `meme_shrek` e `meme_dicaprio` montam o vídeo com a faixa do texto em cima e, embaixo, a capa do vídeo captado e um clipe curto do meme. Os clipes ficam nesta pasta, com o nome do campo `clipe` de cada `memes/<nome>.yaml`:

| Estilo | Arquivo | Config |
|---|---|---|
| `meme_shrek` | `memes/shrek.mp4` | `memes/shrek.yaml` |
| `meme_dicaprio` | `memes/dicaprio.mp4` | `memes/dicaprio.yaml` |

Enquanto o clipe não existe, o estilo aparece como "falta o clipe" na aba Edição e o vídeo sai no estilo `faixa_cima`.

## Como deve ser o clipe

- **Limpo:** sem legenda, sem texto por cima e sem marca d'água. O texto da faixa já vem do título do vídeo captado.
- **De 3 a 10 segundos.** O vídeo final tem a duração do clipe e usa o som dele.
- **Qualquer proporção:** a plataforma ajusta e recorta o clipe para caber na metade da tela.
- **MP4** (H.264 com áudio AAC é o mais garantido).

## Exemplo: recortar um trecho com yt-dlp e ffmpeg

```bash
# 1. baixe o vídeo de onde sai o meme (troque o link)
yt-dlp -f "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]" -o memes/original.mp4 "https://www.youtube.com/watch?v=..."

# 2. recorte 6 segundos a partir de 1min23s, sem o texto (escolha um trecho limpo)
ffmpeg -ss 00:01:23 -i memes/original.mp4 -t 6 -c:v libx264 -crf 18 -pix_fmt yuv420p \
  -c:a aac -b:a 160k -movflags +faststart memes/shrek.mp4

# 3. apague o vídeo original
rm memes/original.mp4
```

Confira o resultado com `ffprobe memes/shrek.mp4`: a duração deve ficar entre 3 e 10 s. Use só trechos que você tem direito de usar.

## Outro meme

1. Coloque o clipe em `memes/<nome>.mp4` e crie `memes/<nome>.yaml` com `clipe: <nome>.mp4` e `imagem_em: cima` (ou `baixo`, para a capa ir embaixo do clipe).
2. Crie `estilos/meme_<nome>.yaml` com `tipo: meme`, `meme: <nome>` e um `rotulo`.
3. Acrescente `meme_<nome>` com um peso na tabela `estilos:` dos canais que vão usá-lo (cada tabela continua somando 1.0).
