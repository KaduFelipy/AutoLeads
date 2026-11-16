import json
from pathlib import Path
import unittest

from playwright.sync_api import Error as PlaywrightError, sync_playwright


ROOT = Path(__file__).resolve().parents[2]
JS = ROOT / 'painel' / 'static' / 'painel' / 'estado.js'
CSS = ROOT / 'painel' / 'static' / 'painel' / 'painel.css'
ORIGIN = 'http://127.0.0.1:9999'


class PollingUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def open_page(self, section='whatsapp', responses=None, extra=''):
        responses = list(responses or [])
        page = self.browser.new_page()
        page.clock.install()
        calls = {'state': 0, 'qr': 0}
        page.add_init_script('window.__stateCalls = 0; window.__qrCalls = 0')

        def route(route):
            path = route.request.url.removeprefix(ORIGIN)
            if path == '/':
                route.fulfill(status=200, content_type='text/html', body='<main>Teste local sintético</main>')
            elif path.startswith('/estado/'):
                calls['state'] += 1
                page.evaluate('window.__stateCalls++')
                response = responses.pop(0) if responses else {'status': 503, 'body': 'indisponível'}
                if response.get('pending'):
                    calls['pending'] = route
                    return
                route.fulfill(status=response.get('status', 200),
                              content_type='application/json',
                              body=json.dumps(response.get('body', response)))
            elif path.startswith('/whatsapp/qr/'):
                calls['qr'] += 1
                page.evaluate('window.__qrCalls++')
                route.fulfill(status=200, content_type='image/svg+xml',
                              body='<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"><rect width="1" height="1"/></svg>')
            else:
                route.fulfill(status=404, body='')

        page.route(f'{ORIGIN}/**', route)
        page.goto(f'{ORIGIN}/')
        page.set_content(f'''<body data-state-section="{section}" data-state-url="{ORIGIN}/estado/">
          <div id="connection-state" data-connected="unknown"><i data-connection-dot></i><span data-connection-label>Estado indisponível</span></div>
          <p id="state-stale" class="state-stale" role="status" hidden>Atualização indisponível; último dado recebido em 12:00.</p>
          <p data-connection-status>Inicial</p><p data-connection-checked>—</p>
          <form data-connect-form><input name="campo" value="rascunho"></form>
          <section class="qr-panel" data-qr-panel hidden><h2>QR sintético</h2><img data-qr-image alt="QR sintético"></section>
          <form id="pesquisa-form"><textarea>texto preservado</textarea></form>
          <div data-research-row="1"><span data-field="status">Pendente</span><span data-field="processadas">0</span></div>
          {extra}</body>''')
        page.add_script_tag(content='''window.__scheduled = []; const nativeTimeout = window.setTimeout.bind(window);
          window.setTimeout = (fn, delay, ...args) => { window.__scheduled.push(delay); return nativeTimeout(fn, delay, ...args) }''')
        page.add_style_tag(path=str(CSS))
        page.add_script_tag(path=str(JS))
        return page, calls

    @staticmethod
    def wait(page, expression):
        page.wait_for_function(expression, timeout=2000)

    @staticmethod
    def conectado(qr=False):
        return {'observado_em': '2026-11-15T12:00:00-03:00', 'conexao': {
            'status': 'qr_ready' if qr else 'ready',
            'status_legivel': 'Aguardando leitura' if qr else 'Conectado',
            'conectado': False if qr else True, 'qr_disponivel': qr,
            'verificado_em': '2026-11-15T12:00:00-03:00', 'consulta_ok': True, 'erro': ''}}

    def test_proximo_poll_e_agendado_tres_segundos_apos_a_resposta(self):
        page, calls = self.open_page(responses=[self.conectado()] * 2)
        try:
            self.wait(page, 'document.querySelector("#connection-state").dataset.connected === "true"')
            self.wait(page, 'window.__scheduled.includes(3000)')
            self.assertEqual(calls['state'], 1)
            page.clock.run_for(2999)
            self.assertEqual(calls['state'], 1)
            page.clock.run_for(1)
            self.wait(page, 'window.__stateCalls === 2')
            self.assertEqual(calls['state'], 2)
        finally:
            page.close()

    def test_backoff_de_falha_cresce_6_12_24_e_limita_em_30_segundos(self):
        failure = {'status': 503, 'body': {'erro': 'offline'}}
        page, calls = self.open_page(responses=[failure] * 5)
        try:
            self.wait(page, 'window.__stateCalls === 1')
            self.wait(page, 'window.__scheduled.includes(6000)')
            self.assertEqual(calls['state'], 1)
            for delay, expected in [(6000, 2), (12000, 3), (24000, 4), (30000, 5)]:
                page.clock.run_for(delay)
                self.wait(page, f'window.__stateCalls === {expected}')
                self.assertEqual(calls['state'], expected)
                next_delay = {2: 12000, 3: 24000, 4: 30000}.get(expected)
                if next_delay:
                    self.wait(page, f'window.__scheduled.includes({next_delay})')
        finally:
            page.close()

    def test_falha_conserva_conexao_conhecida_e_mostra_aviso_visivel_apos_15s(self):
        failure = {'status': 503, 'body': {'erro': 'indisponível'}}
        page, calls = self.open_page(responses=[self.conectado(), failure, failure])
        try:
            self.wait(page, 'document.querySelector("#connection-state").dataset.connected === "true"')
            self.wait(page, 'window.__scheduled.includes(3000)')
            page.clock.run_for(3000)
            self.wait(page, 'window.__stateCalls === 2')
            self.wait(page, 'window.__scheduled.includes(6000)')
            self.assertEqual(calls['state'], 2)
            self.assertEqual(page.locator('[data-connection-label]').inner_text(), 'WhatsApp conectado')
            page.clock.run_for(12000)
            self.assertTrue(page.locator('#state-stale').is_visible())
            self.assertEqual(page.locator('[data-connection-label]').inner_text(), 'WhatsApp conectado')
        finally:
            page.close()

    def test_aba_oculta_pausa_e_retorno_dispara_uma_leitura_imediata(self):
        page, calls = self.open_page(responses=[self.conectado()] * 3)
        try:
            self.wait(page, 'document.querySelector("#connection-state").dataset.connected === "true"')
            self.wait(page, 'window.__scheduled.includes(3000)')
            page.evaluate("Object.defineProperty(document, 'hidden', {configurable:true, get:() => window.__hidden})")
            page.evaluate('window.__hidden = true; document.dispatchEvent(new Event("visibilitychange"))')
            page.clock.run_for(60000)
            self.assertEqual(calls['state'], 1)
            page.evaluate('window.__hidden = false; document.dispatchEvent(new Event("visibilitychange"))')
            self.wait(page, 'window.__stateCalls === 2')
            self.assertEqual(calls['state'], 2)
            page.clock.run_for(3000)
            self.wait(page, 'window.__stateCalls === 3')
            self.assertEqual(calls['state'], 3)
        finally:
            page.close()

    def test_qr_atual_fica_visivel_e_css_real_nao_sobrepoe_hidden(self):
        page, calls = self.open_page(responses=[self.conectado(qr=True)])
        try:
            self.wait(page, 'document.querySelector("[data-qr-panel]").hidden === false')
            self.wait(page, 'document.querySelector("[data-qr-image]").getAttribute("src")?.startsWith("/whatsapp/qr/")')
            self.assertTrue(page.locator('[data-qr-panel]').is_visible())
            self.assertNotEqual(page.locator('[data-qr-panel]').evaluate('(e) => getComputedStyle(e).display'), 'none')
            self.wait(page, 'window.__qrCalls === 1')
            self.assertEqual(calls['qr'], 1)
        finally:
            page.close()

    def test_ready_remove_qr_antigo_e_novo_qr_pode_ser_solicitado_uma_vez(self):
        unavailable = self.conectado()
        unavailable['conexao'].update({'consulta_ok': False, 'conectado': None,
                                       'status': 'indisponivel', 'qr_disponivel': False})
        page, calls = self.open_page(responses=[self.conectado(qr=True), self.conectado(),
                                                self.conectado(qr=True), unavailable])
        try:
            self.wait(page, 'document.querySelector("[data-qr-image]").getAttribute("src")?.startsWith("/whatsapp/qr/")')
            self.wait(page, 'window.__scheduled.includes(3000)')
            page.clock.run_for(3000)
            self.wait(page, 'document.querySelector("[data-qr-panel]").hidden')
            self.assertTrue(page.locator('[data-qr-panel]').evaluate('(e) => e.hidden'))
            self.assertIsNone(page.locator('[data-qr-image]').get_attribute('src'))
            page.clock.run_for(3000)
            self.wait(page, 'window.__stateCalls === 3')
            self.wait(page, 'document.querySelector("[data-qr-panel]").hidden === false')
            self.assertTrue(page.locator('[data-qr-panel]').is_visible())
            self.wait(page, 'window.__qrCalls === 2')
            self.assertEqual(calls['qr'], 2)
            page.clock.run_for(3000)
            self.wait(page, 'window.__stateCalls === 4')
            self.assertTrue(page.locator('[data-qr-panel]').evaluate('(e) => e.hidden'))
            self.assertIsNone(page.locator('[data-qr-image]').get_attribute('src'))
        finally:
            page.close()

    def test_http500_remove_qr_previamente_exibido(self):
        failure = {'status': 500, 'body': {'erro': 'indisponível'}}
        page, _ = self.open_page(responses=[self.conectado(qr=True), failure])
        try:
            self.wait(page, 'document.querySelector("[data-qr-panel]").hidden === false')
            self.wait(page, 'window.__scheduled.includes(3000)')
            page.clock.run_for(3000)
            self.wait(page, 'window.__stateCalls === 2')
            self.wait(page, 'document.querySelector("#state-stale").hidden === false')
            self.assertTrue(page.locator('[data-qr-panel]').evaluate('(e) => e.hidden'))
            self.assertIsNone(page.locator('[data-qr-image]').get_attribute('src'))
        finally:
            page.close()

    def test_leitura_pendente_mostra_stale_15s_apos_ultima_observacao_valida(self):
        page, calls = self.open_page(responses=[self.conectado(), {'pending': True}])
        try:
            self.wait(page, 'document.querySelector("#connection-state").dataset.connected === "true"')
            self.wait(page, 'window.__scheduled.includes(3000)')
            page.clock.run_for(3000)
            self.wait(page, 'window.__stateCalls === 2')
            page.clock.run_for(12000)
            self.assertTrue(page.locator('#state-stale').is_visible())
            self.assertIn('15 segundos', page.locator('#state-stale').inner_text())
        finally:
            if 'pending' in calls:
                try:
                    calls['pending'].abort()
                except PlaywrightError:
                    pass
            page.close()

    def test_poll_preserva_formulario_ativo_e_atualiza_texto_da_linha(self):
        payload = self.conectado()
        payload['pesquisas'] = {'itens': [{'id': 1, 'rotulo': 'Processando', 'processadas': 7,
            'termos': 'termo atualizado', 'criada_em': '2026-11-15T12:00:00-03:00', 'erro': ''}]}
        page, _ = self.open_page(section='pesquisas', responses=[payload])
        try:
            self.wait(page, 'document.querySelector("[data-field=processadas]").textContent === "7"')
            self.assertEqual(page.locator('#pesquisa-form textarea').input_value(), 'texto preservado')
            self.assertEqual(page.locator('[data-field=status]').inner_text(), 'Processando')
        finally:
            page.close()

