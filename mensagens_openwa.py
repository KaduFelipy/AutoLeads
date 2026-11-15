"""Envio de mensagens para empresas do SQLite usando rmyndharis/OpenWA."""

import argparse
import base64
import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import string
import sys
import time
from contextlib import closing
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

PASTA = Path(__file__).resolve().parent
BANCO = PASTA / "empresas.db"
CONFIG = PASTA / "openwa.env"
CAMPOS = {"nome", "endereco", "telefone", "site", "avaliacao"}


class OpenWAError(RuntimeError):
    def __init__(self, mensagem, http_status=None):
        super().__init__(mensagem)
        self.http_status = http_status


class OpenWAClient:
    def __init__(self, url, api_key, session_id="", timeout=60):
        partes = urlsplit(url)
        if partes.scheme not in ("http", "https") or not partes.netloc or partes.query or partes.fragment:
            raise ValueError("OPENWA_URL deve ser uma URL HTTP/HTTPS válida, sem consulta ou fragmento.")
        if not api_key:
            raise ValueError("Configure OPENWA_API_KEY em openwa.env.")
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.session_id = session_id
        self.timeout = timeout

    def request(self, metodo, caminho, dados=None):
        corpo = json.dumps(dados, ensure_ascii=False).encode("utf-8") if dados is not None else None
        pedido = Request(self.url + "/api" + caminho, data=corpo, method=metodo,
                         headers={"X-API-Key": self.api_key, "Content-Type": "application/json"})
        try:
            with urlopen(pedido, timeout=self.timeout) as resposta:
                bruto = resposta.read()
                return json.loads(bruto) if bruto else {}
        except HTTPError as erro:
            erro.close()
            raise OpenWAError(f"OpenWA respondeu HTTP {erro.code}.", erro.code) from None
        except (URLError, TimeoutError, OSError):
            raise OpenWAError("Não foi possível concluir a conexão com o OpenWA.") from None
        except (ValueError, UnicodeError):
            raise OpenWAError("OpenWA retornou uma resposta JSON inválida.") from None

    def caminho_sessao(self):
        if not self.session_id:
            raise ValueError("Conecte o WhatsApp primeiro: mensagens_openwa.py conectar")
        return "/sessions/" + quote(self.session_id, safe="")

    def status(self):
        return self.request("GET", self.caminho_sessao())

    def verificar_numero(self, telefone):
        resposta = self.request("GET", self.caminho_sessao() + "/contacts/check/" + telefone)
        if not isinstance(resposta, dict) or not isinstance(resposta.get("exists"), bool):
            raise OpenWAError("Resposta inválida na consulta do número.")
        return resposta

    def enviar_texto(self, chat_id, mensagem):
        resposta = self.request("POST", self.caminho_sessao() + "/messages/send-text",
                               {"chatId": chat_id, "text": mensagem})
        if not isinstance(resposta, dict) or not resposta.get("messageId"):
            raise OpenWAError("A resposta do envio não contém messageId; confira o histórico do OpenWA.")
        return resposta


def normalizar_telefone(telefone):
    """Aceita DDD brasileiro com/sem +55; não inventa DDD nem acrescenta o nono dígito."""
    if not telefone or not re.fullmatch(r"[+\d()\s.-]+", telefone, flags=re.ASCII):
        raise ValueError("Telefone ausente ou inválido.")
    numero = re.sub(r"\D", "", telefone)
    internacional = telefone.strip().startswith("+") or numero.startswith("00")
    if numero.startswith("0055"):
        numero = numero[2:]
    if internacional and (not numero.startswith("55") or len(numero) not in (12, 13)):
        raise ValueError("Número internacional precisa conter +55, DDD e telefone completo.")
    if not internacional and len(numero) in (10, 11):
        numero = "55" + numero
    if not re.fullmatch(r"55[1-9]\d(?:[2-5]\d{7}|9\d{8})", numero):
        raise ValueError("O telefone precisa ter DDD e formato brasileiro válido.")
    return numero


def selecionar_empresas(banco=BANCO, nome=None, endereco=None, avaliacao_min=None, ids=None, limite=50):
    caminho = Path(banco).resolve()
    if not caminho.is_file():
        raise ValueError(f"Banco não encontrado: {caminho}")
    if limite < 1 or avaliacao_min is not None and not 0 <= avaliacao_min <= 5:
        raise ValueError("Limite deve ser positivo; avaliação deve estar entre 0 e 5.")
    clausulas, parametros = [], []
    for campo, valor in (("nome", nome), ("endereco", endereco)):
        if valor:
            # instr busca texto literal, incluindo apóstrofos e sinais %/_ .
            clausulas.append(f"instr(casefold(COALESCE({campo}, '')), casefold(?)) > 0")
            parametros.append(valor)
    if avaliacao_min is not None:
        clausulas.append("avaliacao >= ?")
        parametros.append(avaliacao_min)
    if ids:
        clausulas.append("id IN (" + ",".join("?" for _ in ids) + ")")
        parametros.extend(ids)
    where = " WHERE " + " AND ".join(clausulas) if clausulas else ""
    with closing(sqlite3.connect(caminho.as_uri() + "?mode=ro", uri=True)) as db:
        db.create_function("casefold", 1, lambda valor: valor.casefold(), deterministic=True)
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(
            "SELECT id, nome, endereco, telefone, site, avaliacao FROM empresas" + where +
            " ORDER BY id LIMIT ?", (*parametros, limite))]


def renderizar_mensagem(modelo, empresa):
    valores = {}
    for _, campo, formato, conversao in string.Formatter().parse(modelo):
        if campo is not None:
            campo_normalizado = campo.lower()
            if campo_normalizado not in CAMPOS or formato or conversao:
                raise ValueError("Use apenas os campos: " + ", ".join(sorted(CAMPOS)))
            valor = empresa.get(campo_normalizado)
            valores[campo] = valor if valor is not None else ""
    mensagem = modelo.format_map(valores)
    if not mensagem.strip() or len(mensagem) > 4096:
        raise ValueError("A mensagem deve ter entre 1 e 4096 caracteres.")
    return mensagem


def abrir_historico(banco):
    db = sqlite3.connect(banco, timeout=30)
    db.execute("""
        CREATE TABLE IF NOT EXISTS envios_whatsapp (
            id INTEGER PRIMARY KEY,
            empresa_id INTEGER NOT NULL,
            session_id TEXT NOT NULL,
            telefone TEXT NOT NULL,
            mensagem TEXT NOT NULL,
            mensagem_hash TEXT NOT NULL,
            status TEXT NOT NULL,
            message_id TEXT,
            erro TEXT,
            criado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            atualizado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (session_id, telefone, mensagem_hash)
        )
    """)
    db.commit()
    return db


def processar_lote(banco, modelo, cliente=None, enviar=False, intervalo=5, **filtros):
    if not math.isfinite(intervalo) or intervalo < 0:
        raise ValueError("O intervalo não pode ser negativo.")
    empresas = selecionar_empresas(banco, **filtros)
    # Valida o modelo mesmo quando a seleção está vazia e antes de qualquer HTTP.
    renderizar_mensagem(modelo, {campo: "x" for campo in CAMPOS})
    resumo = dict(previstos=0, ignorados=0, repetidos=0, aceitos=0, sem_whatsapp=0,
                  falhas=0, incertos=0, interrompido=False, previas=[])
    numeros = set()
    for empresa in empresas:
        try:
            telefone = normalizar_telefone(empresa["telefone"])
        except ValueError:
            resumo["ignorados"] += 1
            continue
        if telefone in numeros:
            resumo["repetidos"] += 1
            continue
        numeros.add(telefone)
        resumo["previas"].append(dict(empresa_id=empresa["id"], nome=empresa["nome"],
                                      telefone=telefone, mensagem=renderizar_mensagem(modelo, empresa)))
    resumo["previstos"] = len(resumo["previas"])
    if not enviar or not resumo["previas"]:
        return resumo
    if cliente is None:
        raise ValueError("Configure o cliente OpenWA antes de enviar.")
    if cliente.status().get("status") != "ready":
        raise OpenWAError("A sessão WhatsApp não está pronta. Execute conectar e leia o QR code.")
    with closing(abrir_historico(banco)) as db:
        tentou_post = False
        for previa in resumo["previas"]:
            digest = hashlib.sha256(previa["mensagem"].encode("utf-8")).hexdigest()
            identidade = (cliente.session_id, previa["telefone"], digest)
            if db.execute("SELECT 1 FROM envios_whatsapp WHERE session_id=? AND telefone=? AND mensagem_hash=?",
                          identidade).fetchone():
                resumo["repetidos"] += 1
                continue
            try:
                contato = cliente.verificar_numero(previa["telefone"])
            except OpenWAError:
                # GET não envia mensagens. Uma próxima execução pode verificar novamente.
                resumo["falhas"] += 1
                resumo["interrompido"] = True
                break
            if contato["exists"] and not contato.get("whatsappId"):
                raise OpenWAError("Número registrado sem identificador WhatsApp na resposta.")
            # A reserva é persistida ANTES do POST. Também protege contra duas execuções simultâneas.
            with db:
                cursor = db.execute("""
                    INSERT OR IGNORE INTO envios_whatsapp
                        (empresa_id, session_id, telefone, mensagem, mensagem_hash, status)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (previa["empresa_id"], cliente.session_id, previa["telefone"], previa["mensagem"],
                       digest, "processando" if contato["exists"] else "sem_whatsapp"))
            if cursor.rowcount == 0:
                resumo["repetidos"] += 1
                continue
            if not contato["exists"]:
                resumo["sem_whatsapp"] += 1
                continue
            if tentou_post:
                time.sleep(intervalo)
            tentou_post = True
            erro, message_id, interromper = None, None, False
            try:
                resposta = cliente.enviar_texto(contato["whatsappId"], previa["mensagem"])
                status, message_id = "aceita", resposta["messageId"]
                resumo["aceitos"] += 1
            except OpenWAError as falha:
                # Timeout, HTTP 5xx ou resposta malformada podem ocorrer após o aceite.
                status = "falha" if falha.http_status and 400 <= falha.http_status < 500 else "incerto"
                erro = str(falha)
                resumo["falhas" if status == "falha" else "incertos"] += 1
                interromper = True
            with db:
                db.execute("""UPDATE envios_whatsapp SET status=?, message_id=?, erro=?,
                              atualizado_em=CURRENT_TIMESTAMP WHERE id=?""",
                           (status, message_id, erro, cursor.lastrowid))
            if interromper:
                resumo["interrompido"] = True
                break
    return resumo


def ler_config(caminho=CONFIG):
    valores = {}
    if Path(caminho).exists():
        for linha in Path(caminho).read_text(encoding="utf-8").splitlines():
            chave, separador, valor = linha.partition("=")
            if separador and not chave.strip().startswith("#"):
                valores[chave.strip()] = valor.strip().strip('"').strip("'")
    for chave in ("OPENWA_URL", "OPENWA_API_KEY", "OPENWA_SESSION_ID"):
        if chave in os.environ:
            valores[chave] = os.environ[chave]
    return valores


def configurar():
    if not CONFIG.exists():
        CONFIG.write_text("OPENWA_URL=http://127.0.0.1:2785\nOPENWA_API_KEY=" + secrets.token_hex(32) +
                          "\nOPENWA_SESSION_ID=\n", encoding="utf-8")
    print("Configuração local disponível em openwa.env (chave não exibida).")


def conectar(cliente, nome="scrapgooglemaps"):
    sessoes = cliente.request("GET", "/sessions")
    sessao = next((sessao for sessao in sessoes
                   if sessao["id"] == cliente.session_id or not cliente.session_id and sessao["name"] == nome), None)
    if sessao is None:
        if cliente.session_id:
            raise OpenWAError("Sessão configurada não foi encontrada no OpenWA.")
        sessao = cliente.request("POST", "/sessions", {"name": nome})
    cliente.session_id = sessao["id"]
    linhas = CONFIG.read_text(encoding="utf-8").splitlines() if CONFIG.exists() else []
    linhas = [linha for linha in linhas if not linha.startswith("OPENWA_SESSION_ID=")]
    CONFIG.write_text("\n".join(linhas) + "\nOPENWA_SESSION_ID=" + cliente.session_id + "\n", encoding="utf-8")
    if not sessao.get("engineLoaded") and sessao["status"] != "ready":
        cliente.request("POST", cliente.caminho_sessao() + "/start")
    for _ in range(30):
        estado = cliente.status()
        if estado["status"] == "ready":
            print("WhatsApp conectado e pronto para envio.")
            return
        if estado["status"] == "qr_ready":
            qr = cliente.request("GET", cliente.caminho_sessao() + "/qr")["qrCode"]
            prefixo = "data:image/png;base64,"
            if not qr.startswith(prefixo):
                raise OpenWAError("Formato inesperado do QR code.")
            arquivo = PASTA / "openwa-qr.png"
            arquivo.write_bytes(base64.b64decode(qr[len(prefixo):], validate=True))
            print(f"Leia o QR em {arquivo} usando WhatsApp > Aparelhos conectados.")
            print("Depois confira a conexão: mensagens_openwa.py status")
            return
        if estado["status"] in ("failed", "action_required"):
            raise OpenWAError("A sessão exige atenção. Consulte o painel do OpenWA.")
        time.sleep(2)
    raise OpenWAError("A sessão ainda está inicializando. Execute conectar novamente em alguns instantes.")


def main():
    # Suporta acentos e emojis também no terminal Windows e em saída redirecionada.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="comando", required=True)
    sub.add_parser("configurar", help="Cria configuração e chave locais")
    conectar_parser = sub.add_parser("conectar", help="Cria/inicia sessão e salva o QR code")
    conectar_parser.add_argument("--nome", default="scrapgooglemaps")
    sub.add_parser("status", help="Consulta estado da sessão WhatsApp")
    for comando in ("listar", "prever", "enviar"):
        item = sub.add_parser(comando)
        item.add_argument("--banco", type=Path, default=BANCO)
        item.add_argument("--nome")
        item.add_argument("--endereco")
        item.add_argument("--avaliacao-min", type=float)
        item.add_argument("--ids", type=int, nargs="+")
        item.add_argument("--limite", type=int, default=50, help="Máximo de empresas selecionadas (padrão: 50)")
        if comando != "listar":
            texto = item.add_mutually_exclusive_group(required=True)
            texto.add_argument("--mensagem")
            texto.add_argument("--arquivo", type=Path, help="Arquivo UTF-8 com a mensagem")
            item.add_argument("--intervalo", type=float, default=5, help="Segundos entre envios (padrão: 5)")
    args = parser.parse_args()
    try:
        if args.comando == "configurar":
            configurar()
            return 0
        cliente = None
        if args.comando in ("conectar", "status", "enviar"):
            config = ler_config()
            cliente = OpenWAClient(config.get("OPENWA_URL", "http://127.0.0.1:2785"),
                                   config.get("OPENWA_API_KEY"), config.get("OPENWA_SESSION_ID", ""))
        if args.comando == "conectar":
            conectar(cliente, args.nome)
            return 0
        if args.comando == "status":
            print(json.dumps(cliente.status(), ensure_ascii=False, indent=2))
            return 0
        filtros = dict(nome=args.nome, endereco=args.endereco, avaliacao_min=args.avaliacao_min,
                       ids=args.ids, limite=args.limite)
        if args.comando == "listar":
            print(json.dumps(selecionar_empresas(args.banco, **filtros), ensure_ascii=False, indent=2))
            return 0
        modelo = args.arquivo.read_text(encoding="utf-8-sig") if args.arquivo else args.mensagem
        resultado = processar_lote(args.banco, modelo, cliente, enviar=args.comando == "enviar",
                                   intervalo=args.intervalo, **filtros)
        print(json.dumps(resultado, ensure_ascii=False, indent=2))
        return 1 if resultado["interrompido"] else 0
    except (ValueError, OpenWAError, OSError, sqlite3.Error) as erro:
        parser.exit(1, f"Erro: {erro}\n")


if __name__ == "__main__":
    raise SystemExit(main())
