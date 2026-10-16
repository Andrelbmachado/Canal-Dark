"""Linha de comando do Canal Dark."""
from __future__ import annotations

import argparse


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="canaldark", description="Canal Dark: Shorts/Reels automáticos")
    sub = p.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="sobe captação, edição, publicação e o painel")
    run.add_argument("--ensaio", action="store_true", help="faz tudo menos publicar de verdade")
    run.add_argument("--porta", type=int, default=8000)
    run.add_argument("--raiz", default=".", help="pasta com canais/, sons/ e dados/")
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.cmd == "run":
        raise SystemExit("ainda não implementado")


if __name__ == "__main__":
    main()
