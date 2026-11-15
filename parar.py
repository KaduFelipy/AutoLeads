"""Encerra apenas os processos identificados deste projeto e para o serviço OpenWA."""

from pathlib import Path

from controle_lancador import criar_lancador


def main():
    try:
        resultado = criar_lancador(Path(__file__).resolve().parent).parar()
    except Exception as exc:
        raise SystemExit("Falha ao parar o painel: %s" % exc)
    if resultado in {"parado", "ja_parado"}:
        print("Os processos do projeto foram encerrados e o OpenWA foi parado sem remover dados ou sessão." if resultado == "parado" else "Nenhum processo estava em execução; o OpenWA foi parado sem remover dados ou sessão.")
    else:
        if resultado == "parado_openwa_indisponivel":
            print("Os processos do painel e trabalhador foram encerrados.")
        else:
            print("Nenhum processo do painel ou trabalhador estava em execução.")
        print("Aviso: não foi possível confirmar que o OpenWA parou. Confira o Docker Desktop e tente novamente.")


if __name__ == "__main__":
    main()
