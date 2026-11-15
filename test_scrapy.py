"""Busca empresas no Google Maps e mantém uma única ficha por empresa no SQLite3."""

import argparse
import json
import logging
import re
import sqlite3
import unicodedata
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, parse_qs

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, expect, sync_playwright

url_base_busca = "https://www.google.com/maps/search/"
palavras_chaves = (
    "Cafeteria Pato Branco"
    # "Mercearia Pato Branco",
    # "Mercado Pato Branco",
    # "Panificadora Pato Branco",
    # "Mecanica Pato Branco",
    # "AutoEletrica Pato Branco",
    # "Automotivo Pato Branco",
    # "Oficina Pato Branco",
    # "Retifica Pato Branco",
    # "Injeção Pato Branco",
    # "Funilaria Pato Branco",
    # "Pintura Pato Branco",
    # "Borracharia Pato Branco"
    # "butiques Pato Branco",
    # "boutiquesPato Branco",
    # "confecções Pato Branco",
    # "brechos Pato Branco",
    # "saloes Pato Branco",
    # "atelie Pato Branco",
    # "moda Pato Branco",
    # "outlet Pato Branco",
    # "vestuario Pato Branco"

)
BANCO = Path(__file__).with_name("empresas.db")
logger = logging.getLogger(__name__)


class ErroCallbackCaptura(RuntimeError):
    """Propaga falha na persistência externa sem tratá-la como timeout do navegador."""


def normalizar(texto):
    return " ".join(unicodedata.normalize("NFKC", texto or "").casefold().split())


def identificar_maps(url):
    """Ignora idioma, zoom e coordenadas quando há uma identificação do lugar."""
    url = unquote(url)
    identificador = re.search(r"!1s([^!/?]+)", url)
    if identificador:
        return identificador.group(1)
    partes = urlsplit(url)
    parametros = parse_qs(partes.query)
    for campo in ("cid", "query_place_id"):
        if parametros.get(campo):
            return campo + ":" + parametros[campo][0]
    return partes.netloc + partes.path.split("/@")[0].rstrip("/")


def abrir_banco(caminho=BANCO):
    conexao = sqlite3.connect(caminho)
    conexao.execute("""
        CREATE TABLE IF NOT EXISTS empresas (
            id INTEGER PRIMARY KEY,
            nome TEXT NOT NULL,
            endereco TEXT,
            telefone TEXT,
            site TEXT,
            avaliacao REAL CHECK (avaliacao BETWEEN 0 AND 5),
            maps_id TEXT NOT NULL UNIQUE,
            chave TEXT UNIQUE
        )
    """)
    conexao.commit()
    return conexao


def salvar_empresa(conexao, empresa, url_maps):
    """Compara antes de gravar; atualiza dados existentes sem apagar campos ausentes."""
    dados = [empresa.get(campo) for campo in
             ("nome", "endereco", "telefone", "site", "avaliacao")]
    dados[:4] = [" ".join(valor.split()) or None if valor else None for valor in dados[:4]]
    if not dados[0]:
        raise ValueError("A empresa precisa ter um nome para ser salva.")
    maps_id = identificar_maps(url_maps)
    # Sem endereço, não une empresas apenas por nome: podem ser filiais.
    chave = json.dumps([normalizar(dados[0]), normalizar(dados[1])]) if dados[1] else None
    with conexao:
        existente = conexao.execute(
            "SELECT id FROM empresas WHERE maps_id = ? OR chave = ?",
            (maps_id, chave),
        ).fetchone()
        if existente:
            conexao.execute("""
                UPDATE empresas SET nome = ?,
                    endereco = COALESCE(?, endereco),
                    telefone = COALESCE(?, telefone),
                    site = COALESCE(?, site),
                    avaliacao = COALESCE(?, avaliacao),
                    chave = COALESCE(?, chave)
                WHERE id = ?
            """, (*dados, chave, existente[0]))
            return False
        conexao.execute("""
            INSERT INTO empresas (nome, endereco, telefone, site, avaliacao, maps_id, chave)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (*dados, maps_id, chave))
        return True


def capturar_empresa(page: Page):
    titulo = page.locator("h1").last
    titulo.wait_for(state="visible", timeout=15000)
    painel = titulo.locator("xpath=ancestor::*[@role='main'][1]")
    if not painel.count():
        raise ValueError("A ficha da empresa não foi encontrada.")

    def ler_campo(seletor):
        elemento = painel.locator(seletor).first
        if not elemento.count():
            return None
        texto = elemento.get_attribute("aria-label") or elemento.inner_text()
        texto = re.sub(r"^(Endereço|Address|Telefone|Phone):\s*", "", texto, flags=re.I)
        return " ".join(texto.split()) or None

    site = painel.locator('a[data-item-id="authority"]').first
    estrelas = painel.locator('[role="img"][aria-label*="estrela"], '
                              '[role="img"][aria-label*="star"]').first
    nota = None
    if estrelas.count():
        valor = re.search(r"\d+(?:[.,]\d+)?", estrelas.get_attribute("aria-label") or "")
        if valor:
            nota = float(valor.group().replace(",", "."))
    return {
        "nome": " ".join(titulo.inner_text().split()),
        "endereco": ler_campo('[data-item-id="address"]'),
        "telefone": ler_campo('[data-item-id^="phone:tel:"]'),
        "site": site.get_attribute("href") if site.count() else None,
        "avaliacao": nota,
    }


def consultar_google_maps(page: Page, termos=palavras_chaves, banco=BANCO, limite=None,
                          ao_capturar=None):
    """Abre cada ficha, percorre a lista com rolagem e salva imediatamente."""
    if isinstance(termos, str):
        termos = [termos]
    conexao = abrir_banco(banco) if ao_capturar is None else None
    visitadas = set()
    total = 0

    def persistir(empresa, url, termo):
        if ao_capturar is None:
            salvar_empresa(conexao, empresa, url)
            return
        try:
            ao_capturar(empresa, url, termo)
        except Exception as erro:
            raise ErroCallbackCaptura('Falha ao registrar o resultado da pesquisa.') from erro

    try:
        for termo in termos:
            if not termo.strip():
                continue
            visitadas_termo = set()
            vistas = visitadas_termo if ao_capturar is not None else visitadas
            page.goto(url_base_busca + quote(termo.strip(), safe="") + "?hl=pt-BR",
                      wait_until="domcontentloaded", timeout=60000)
            consentimento = page.get_by_role("button", name=re.compile(
                r"^(Rejeitar tudo|Reject all)$", re.I))
            if consentimento.count():
                consentimento.first.click()
            vazia = page.get_by_text(re.compile(
                r"O Google Maps não encontrou|Google Maps can't find|Nenhum resultado|No results found", re.I))
            page.locator('a.hfpxzc, h1.DUwDvf, [role="feed"]').or_(vazia).first.wait_for(
                state="visible", timeout=20000)
            if vazia.count():
                logger.info("Nenhuma empresa encontrada para: %s", termo)
                continue
            # O Maps pode abrir diretamente uma única empresa.
            if not page.locator('[role="feed"]').count():
                identificador = identificar_maps(page.url)
                if identificador in vistas:
                    continue
                empresa = capturar_empresa(page)
                persistir(empresa, page.url, termo)
                visitadas.add(identificador)
                visitadas_termo.add(identificador)
                total += 1
                if limite and total >= limite:
                    return total
                continue

            sem_novos = 0
            tentadas = set()
            for _ in range(200):
                resultados = page.locator("a.hfpxzc").evaluate_all(
                    "links => links.map(a => ({url: a.href, nome: a.getAttribute('aria-label')}))"
                )
                novos = [item for item in resultados
                         if identificar_maps(item["url"]) not in vistas | tentadas]
                for item in novos:
                    identificador = identificar_maps(item["url"])
                    if identificador in vistas:
                        continue
                    tentadas.add(identificador)
                    try:
                        link = page.locator("a.hfpxzc[href=" + json.dumps(item["url"]) + "]").first
                        link.click(timeout=10000)
                        marca = re.search(r"!1s([^!/?]+)", item["url"])
                        if marca:
                            expect(page).to_have_url(re.compile(re.escape(marca.group())), timeout=15000)
                        titulo = page.locator("h1").last
                        if item["nome"]:
                            expect(titulo).to_have_text(item["nome"], timeout=15000)
                        empresa = capturar_empresa(page)
                        if ao_capturar is None:
                            inserida = salvar_empresa(conexao, empresa, item["url"])
                        else:
                            persistir(empresa, item["url"], termo)
                            inserida = True
                        visitadas.add(identificador)
                        visitadas_termo.add(identificador)
                        total += 1
                        logger.info("%s: %s", "Salva" if inserida else "Atualizada", empresa["nome"])
                        if limite and total >= limite:
                            return total
                    except (PlaywrightTimeoutError, AssertionError, ValueError) as erro:
                        if isinstance(erro, ErroCallbackCaptura):
                            raise
                        logger.warning("Não foi possível capturar %s: %s", item["nome"],
                                       str(erro).splitlines()[0])

                feed = page.locator('[role="feed"]').first
                if not feed.count():
                    break
                fim = feed.get_by_text(re.compile(
                    r"Você chegou ao final da lista|You've reached the end of the list", re.I))
                if fim.count():
                    break
                sem_novos = sem_novos + 1 if not novos else 0
                if sem_novos >= 5:
                    logger.warning("Lista sem novos resultados após cinco tentativas: %s", termo)
                    break
                feed.evaluate("el => el.scrollTo(0, el.scrollHeight)")
                page.wait_for_timeout(1500)
            else:
                logger.warning("Limite de rolagens atingido para: %s", termo)
    finally:
        if conexao is not None:
            conexao.close()
    return total


def test_consultaGoogleMaps(page: Page):
    consultar_google_maps(page)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("termos", nargs="*", help='Exemplo: "Mercado Pato Branco"')
    parser.add_argument("--banco", type=Path, default=BANCO)
    parser.add_argument("--limite", type=int, help="Limite de fichas por execução")
    args = parser.parse_args()
    if args.limite is not None and args.limite < 1:
        parser.error("--limite deve ser maior que zero")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(locale="pt-BR")
            total = consultar_google_maps(page, args.termos or palavras_chaves, args.banco, args.limite)
            logger.info("%s fichas processadas. Banco: %s", total, args.banco.resolve())
        finally:
            browser.close()


if __name__ == "__main__":
    main()
