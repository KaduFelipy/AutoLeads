import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import test_scrapy as scraper
from playwright.sync_api import sync_playwright


class PersistenciaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'empresas.db'
        self.db = scraper.abrir_banco(self.path)
        self.empresa = dict(nome='Mercado São José', endereco='Rua A, 10',
                            telefone='(46) 1234-5678', site='https://mercado.test',
                            avaliacao=4.6)
        self.url = 'https://www.google.com/maps/place/Mercado/data=!1s0x1:0x2'

    def tearDown(self):
        if hasattr(self, 'db'):
            self.db.close()
        self.tmp.cleanup()

    def test_salva_os_cinco_campos_e_persiste_entre_execucoes(self):
        scraper.salvar_empresa(self.db, self.empresa, self.url)
        self.db.close()
        self.db = scraper.abrir_banco(self.path)
        self.assertEqual(self.db.execute(
            'SELECT nome, endereco, telefone, site, avaliacao FROM empresas'
        ).fetchone(), ('Mercado São José', 'Rua A, 10', '(46) 1234-5678',
                      'https://mercado.test', 4.6))
        scraper.salvar_empresa(self.db, self.empresa, self.url)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM empresas').fetchone()[0], 1)

    def test_url_diferente_e_nome_normalizado_nao_duplicam_empresa(self):
        scraper.salvar_empresa(self.db, self.empresa, self.url)
        outra = dict(self.empresa, nome='  MERCADO   SÃO JOSÉ ', endereco=' RUA A, 10 ')
        scraper.salvar_empresa(self.db, outra, 'https://www.google.com/maps/place/Outra/data=!1s0x3:0x4')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM empresas').fetchone()[0], 1)

    def test_mesmo_maps_atualiza_dados_sem_apagar_campos_ausentes(self):
        scraper.salvar_empresa(self.db, self.empresa, self.url)
        scraper.salvar_empresa(self.db, dict(self.empresa, telefone=None, site=None,
                                            avaliacao=4.8), self.url + '?hl=pt-BR')
        self.assertEqual(self.db.execute(
            'SELECT telefone, site, avaliacao FROM empresas'
        ).fetchone(), ('(46) 1234-5678', 'https://mercado.test', 4.8))

    def test_filiais_com_mesmo_nome_em_enderecos_diferentes_sao_preservadas(self):
        scraper.salvar_empresa(self.db, self.empresa, self.url)
        scraper.salvar_empresa(self.db, dict(self.empresa, endereco='Rua B, 20'),
                               'https://www.google.com/maps/place/Filial/data=!1s0x3:0x4')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM empresas').fetchone()[0], 2)

    def test_campos_ausentes_e_apostrofos(self):
        scraper.salvar_empresa(self.db, dict(nome="Mercado D'Ávila", endereco=None,
                                            telefone=None, site=None, avaliacao=None), self.url)
        self.assertEqual(self.db.execute('SELECT nome, telefone, avaliacao FROM empresas').fetchone(),
                         ("Mercado D'Ávila", None, None))

    def test_nao_salva_empresa_sem_nome(self):
        with self.assertRaises(ValueError):
            scraper.salvar_empresa(self.db, dict(self.empresa, nome=' '), self.url)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM empresas').fetchone()[0], 0)


class CapturaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.page = self.browser.new_page()

    def tearDown(self):
        self.page.close()

    def test_extrai_ficha_sem_confundir_avaliacao_da_lista(self):
        self.page.set_content('''
          <div role="feed"><span role="img" aria-label="2,0 estrelas"></span></div>
          <div role="main"><h1>Mercado Central</h1>
            <button data-item-id="address" aria-label="Endereço: Rua Central, 42"></button>
            <button data-item-id="phone:tel:4612345678" aria-label="Telefone: (46) 1234-5678"></button>
            <a data-item-id="authority" href="https://central.test">Site</a>
            <span role="img" aria-label="4,7 estrelas"></span>
          </div>''')
        self.assertEqual(scraper.capturar_empresa(self.page), {
            'nome': 'Mercado Central', 'endereco': 'Rua Central, 42',
            'telefone': '(46) 1234-5678', 'site': 'https://central.test', 'avaliacao': 4.7})

    def test_ficha_sem_campos_opcionais(self):
        self.page.set_content('<div role="main"><h1>Padaria</h1></div>')
        self.assertEqual(scraper.capturar_empresa(self.page), {
            'nome': 'Padaria', 'endereco': None, 'telefone': None,
            'site': None, 'avaliacao': None})

    def test_busca_clica_na_empresa_sem_fechar_lista_e_nao_repete_captura(self):
        html = '''<button aria-label="Fechar" onclick="document.querySelector('[role=feed]').remove()">x</button>
          <div role="feed"><a class="hfpxzc" aria-label="Mercado Central"
            href="https://www.google.com/maps/place/Central/data=!1s0x1:0x2"
            onclick="event.preventDefault();history.pushState({},'',this.href);
              document.getElementById('ficha').innerHTML='<h1>Mercado Central</h1><button data-item-id=address aria-label=&quot;Endereço: Rua A, 10&quot;></button>'">Empresa</a>
            <p>Você chegou ao final da lista</p></div><div role="main" id="ficha"></div>'''
        self.page.route('https://www.google.com/maps/**', lambda route: route.fulfill(
            content_type='text/html; charset=utf-8', body=html))
        with tempfile.TemporaryDirectory() as tmp:
            banco = Path(tmp) / 'empresas.db'
            total = scraper.consultar_google_maps(self.page, ['Mercado Pato Branco'], banco, limite=1)
            self.assertEqual(total, 1)
            scraper.consultar_google_maps(self.page, ['Mercado Pato Branco'], banco, limite=1)
            with closing(sqlite3.connect(banco)) as db:
                self.assertEqual(db.execute('SELECT nome, endereco FROM empresas').fetchall(),
                                 [('Mercado Central', 'Rua A, 10')])

    def test_busca_sem_resultados_continua_para_proximo_termo(self):
        def responder(route):
            if 'Inexistente' in route.request.url:
                html = '<p>O Google Maps não encontrou Inexistente</p>'
            else:
                html = '<div role="main"><h1 class="DUwDvf">Padaria</h1></div>'
            route.fulfill(content_type='text/html; charset=utf-8', body=html)
        self.page.route('https://www.google.com/maps/**', responder)
        with tempfile.TemporaryDirectory() as tmp:
            banco = Path(tmp) / 'empresas.db'
            total = scraper.consultar_google_maps(self.page, ['Inexistente', 'Padaria'], banco)
            self.assertEqual(total, 1)
            with closing(sqlite3.connect(banco)) as db:
                self.assertEqual(db.execute('SELECT nome FROM empresas').fetchall(), [('Padaria',)])

    def test_callback_substitui_sqlite_e_registra_origem_em_todos_os_termos(self):
        html = '''<div role="feed"><a class="hfpxzc" aria-label="Mercado Central"
          href="https://www.google.com/maps/place/Central/data=!1s0x1:0x2"
          onclick="event.preventDefault();history.pushState({},'',this.href);
            document.getElementById('ficha').innerHTML='<h1>Mercado Central</h1>'">Empresa</a>
          <p>Você chegou ao final da lista</p></div><div role="main" id="ficha"></div>'''
        self.page.route('https://www.google.com/maps/**', lambda route: route.fulfill(
            content_type='text/html; charset=utf-8', body=html))
        with tempfile.TemporaryDirectory() as tmp:
            banco = Path(tmp) / 'painel-nao-deve-criar.db'
            capturas = []

            total = scraper.consultar_google_maps(
                self.page, ['Retífica Pato Branco', 'Oficina mecânica Pato Branco'], banco,
                ao_capturar=lambda dados, url, termo: capturas.append((dados['nome'], url, termo)),
            )

            self.assertEqual(total, 2)
            self.assertEqual([captura[2] for captura in capturas], [
                'Retífica Pato Branco', 'Oficina mecânica Pato Branco'
            ])
            self.assertFalse(banco.exists())


if __name__ == '__main__':
    unittest.main()
