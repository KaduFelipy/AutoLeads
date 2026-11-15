"""Inicia o painel local e o trabalhador em segundo plano."""

import argparse
from pathlib import Path

from controle_lancador import criar_lancador


def main():
    parser = argparse.ArgumentParser(description="Inicia o painel ScrapGoogleMaps local.")
    parser.add_argument("--sem-openwa", action="store_true", help="não tenta iniciar o OpenWA local")
    parser.add_argument("--nao-abrir", action="store_true", help="não abre o navegador")
    parser.add_argument("--porta", type=int, default=8000, help="porta local do painel")
    args = parser.parse_args()
    if not 1024 <= args.porta <= 65535:
        parser.error("--porta deve estar entre 1024 e 65535")
    try:
        resultado = criar_lancador(Path(__file__).resolve().parent, args.porta).iniciar(
            sem_openwa=args.sem_openwa,
            abrir_navegador=not args.nao_abrir,
        )
    except Exception as exc:
        parser.exit(1, "Falha ao iniciar o painel: %s\n" % exc)
    if resultado == "ja_ativo":
        print("Um processo deste projeto já está em execução; nenhuma instância duplicada foi criada.")
        print("Se a inicialização estiver incompleta, execute parar.py e depois iniciar.py.")
    else:
        print("Painel local iniciado em http://127.0.0.1:%s/" % args.porta)
        print("Use parar.py para encerrar o painel e o OpenWA deste projeto.")


if __name__ == "__main__":
    main()
