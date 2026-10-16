"""Linha de comando do Canal Dark."""
from __future__ import annotations

import argparse
import getpass
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="canaldark", description="Canal Dark: Shorts/Reels automáticos")
    sub = p.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="sobe captação, edição, publicação e o painel")
    run.add_argument("--ensaio", action="store_true", help="faz tudo menos publicar de verdade")
    run.add_argument("--porta", type=int, default=8000)
    run.add_argument("--raiz", default=".", help="pasta com canais/, sons/ e dados/")

    auth = sub.add_parser("auth", help="conecta uma conta do YouTube ou do Instagram")
    auth_sub = auth.add_subparsers(dest="plataforma", required=True)
    yt = auth_sub.add_parser("youtube", help="abre o navegador para autorizar o upload no YouTube")
    yt.add_argument("conta", help="nome da conta, igual ao usado em `contas:` no YAML do canal")
    yt.add_argument("--segredo", default="dados/client_secret.json",
                    help="arquivo OAuth baixado do Google Cloud (tipo: app para computador)")
    ig = auth_sub.add_parser("instagram", help="guarda o token do Instagram no chaveiro do sistema")
    ig.add_argument("conta", help="nome da conta, igual ao usado em `contas:` no YAML do canal")
    ig.add_argument("--ig-user-id", required=True, help="ID numérico da conta profissional do Instagram")
    return p


def _auth(args) -> None:
    if args.plataforma == "youtube":
        from canaldark.publish.youtube import authorize

        secret = Path(args.segredo)
        if not secret.exists():
            raise SystemExit(f"arquivo {secret} não encontrado; veja docs/contas.md")
        authorize(args.conta, secret)
        print(f"YouTube conectado: {args.conta}")
    else:
        from canaldark.publish.instagram import authorize

        token = getpass.getpass("Cole o token de longa duração do Instagram: ").strip()
        print(f"Instagram conectado: @{authorize(args.conta, token, args.ig_user_id)}")


async def _run(args) -> None:
    import os
    import shutil

    import uvicorn

    from canaldark.config import load_channels, load_sounds
    from canaldark.db import open_db
    from canaldark.events import EventBus
    from canaldark.pipeline import Pipeline
    from canaldark.web.app import create_app

    os.chdir(Path(args.raiz).expanduser())
    if shutil.which("ffmpeg") is None:
        raise SystemExit("FFmpeg não encontrado. Instale com `brew install ffmpeg` ou `sudo apt install ffmpeg`.")
    try:
        cfgs = load_channels(Path("canais"))
    except ValueError as e:
        raise SystemExit(str(e))
    if not cfgs:
        raise SystemExit("nenhum canal em canais/*.yaml")
    Path("dados").mkdir(exist_ok=True)
    db = await open_db(Path("dados/canaldark.db"))
    bus = EventBus(db)
    pipeline = Pipeline(db, bus, cfgs, load_sounds(Path("sons/catalogo.yaml")), root=Path("."), dry_run=args.ensaio)
    server = uvicorn.Server(uvicorn.Config(create_app(pipeline, args.porta), host="127.0.0.1", port=args.porta,
                                           log_level="warning", timeout_graceful_shutdown=3))
    await pipeline.start()
    print(f"Canal Dark rodando: abra http://localhost:{args.porta}" + (" (ensaio)" if args.ensaio else ""))
    try:
        await server.serve()
    finally:
        await pipeline.stop()
        await db.close()


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.cmd == "auth":
        _auth(args)
    elif args.cmd == "run":
        import asyncio

        asyncio.run(_run(args))


if __name__ == "__main__":
    main()
