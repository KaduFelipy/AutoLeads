import json
from unittest.mock import patch

from django.contrib.staticfiles.testing import StaticLiveServerTestCase


class AutoLeadsPollingUITests(StaticLiveServerTestCase):
    def test_logo_carrega_em_desktop_mobile_e_recebe_foco_de_teclado(self):
        from playwright.sync_api import sync_playwright

        with patch('painel.views._conexao', return_value={'status': 'disconnected',
                'status_legivel': 'Desconectado', 'consulta_ok': True, 'conectado': False,
                'qr_disponivel': False}), \
             patch('painel.services.status.observar_conexao', return_value={
                'status': 'disconnected', 'status_legivel': 'Desconectado',
                'consulta_ok': True, 'conectado': False, 'qr_disponivel': False,
                'verificado_em': '2026-11-15T12:00:00-03:00', 'erro': ''}):
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={'width': 1280, 'height': 900})
                page.goto(f'{self.live_server_url}/')
                logo = page.locator('.brand img')
                page.wait_for_function("document.querySelector('.brand img')?.naturalWidth > 0")
                desktop = logo.bounding_box()
                self.assertGreater(desktop['width'], 0)
                self.assertLessEqual(desktop['width'], 144)
                self.assertEqual(logo.get_attribute('alt'), 'AutoLeads')
                page.keyboard.press('Tab')
                self.assertTrue(page.locator('.brand').evaluate('(el) => el === document.activeElement'))
                self.assertNotEqual(page.locator('.brand').evaluate(
                    "el => getComputedStyle(el).outlineStyle"), 'none')
                page.set_viewport_size({'width': 390, 'height': 844})
                mobile = logo.bounding_box()
                self.assertGreater(mobile['width'], 0)
                self.assertLessEqual(mobile['x'] + mobile['width'], 390)
                browser.close()

    def test_pesquisa_atualiza_sem_recarregar_e_preserva_texto_digitado(self):
        from playwright.sync_api import sync_playwright
        from painel.models import Pesquisa

        pesquisa = Pesquisa.objects.create(termos='pesquisa sintética', estado='processando', processadas=0)
        resposta = {'observado_em': '2026-11-15T12:00:00-03:00',
            'conexao': {'status': 'ready', 'status_legivel': 'Conectado', 'conectado': True,
                'qr_disponivel': False, 'verificado_em': '2026-11-15T12:00:00-03:00',
                'consulta_ok': True, 'erro': ''},
            'pesquisas': {'itens': [{'id': pesquisa.pk, 'termos': pesquisa.termos,
                'estado': 'processando', 'rotulo': 'Processando', 'processadas': 3,
                'criada_em': '2026-11-15T12:00:00-03:00', 'inicio': '2026-11-15T12:00:00-03:00',
                'fim': None, 'erro': ''}]}}
        with patch('painel.views._conexao', return_value={'status': 'disconnected',
                'status_legivel': 'Desconectado', 'consulta_ok': True, 'conectado': False,
                'qr_disponivel': False}):
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page()
                page.route('**/estado/**', lambda route: route.fulfill(status=200,
                    content_type='application/json', headers={'Cache-Control': 'no-store'},
                    body=json.dumps(resposta)))
                page.goto(f'{self.live_server_url}/pesquisas/')
                page.locator('textarea[name="termos"]').fill('rascunho que deve permanecer')
                page.wait_for_function("""id => {
                  const row = document.querySelector(`[data-research-row="${id}"]`)
                  return row && row.querySelector('[data-field="processadas"]')?.textContent.trim() === '3'
                }""", arg=pesquisa.pk, timeout=5000)
                self.assertEqual(page.locator('textarea[name="termos"]').input_value(),
                                 'rascunho que deve permanecer')
                browser.close()

    def test_campanha_sem_tentativa_recebe_primeira_tentativa_e_atualiza_acoes(self):
        from playwright.sync_api import sync_playwright
        from painel.models import Campanha, Destinatario, Empresa

        empresa = Empresa.objects.create(nome='Atualização', telefone='5546999000999', maps_id='ui-status-update')
        campanha = Campanha.objects.create(nome='Campanha ativa', texto_snapshot='Texto imutável', limite=1,
            session_id='sessao-falsa', estado=Campanha.Estado.ATIVA)
        destinatario = Destinatario.objects.create(campanha=campanha, empresa=empresa, ordem=1,
            telefone=empresa.telefone, mensagem='Texto imutável', mensagem_hash='hash')
        resposta = {'observado_em': '2026-11-15T12:00:00-03:00', 'conexao': {
            'status': 'ready', 'status_legivel': 'Conectado', 'conectado': True,
            'qr_disponivel': False, 'verificado_em': '2026-11-15T12:00:00-03:00',
            'consulta_ok': True, 'erro': ''}, 'campanha': {
            'id': campanha.pk, 'estado': 'pausada', 'rotulo': 'Pausada', 'motivo_pausa': 'Pausada pelo usuário.',
            'contagens': {'destinatarios_total': 1, 'sem_tentativa': 0, 'sem_tentativa_bloqueados': 0,
                'destinatarios_por_estado': {'fila': 0, 'enviando': 0, 'aceita': 1, 'enviado': 0,
                    'entregue': 0, 'lido': 0, 'falha': 0, 'incerto': 0}, 'tentativas_total': 1,
                'tentativas_por_estado': {'fila': 0, 'enviando': 0, 'aceita': 1, 'enviado': 0,
                    'entregue': 0, 'lido': 0, 'falha': 0, 'incerto': 0}, 'tentativas_confirmadas': 0},
            'destinatarios': [{'id': destinatario.pk, 'tentativa_id': 1, 'estado': 'aceita',
                'rotulo': 'Aceita, aguardando confirmação', 'bloqueado_motivo': ''}],
            'tentativas': [{'id': 1, 'destinatario_id': destinatario.pk, 'campanha_id': campanha.pk,
                'empresa_nome': empresa.nome, 'campanha_nome': campanha.nome, 'telefone': empresa.telefone,
                'estado': 'aceita', 'rotulo': 'Aceita, aguardando confirmação', 'erro': '',
                'confirmado_em': None, 'ultima_verificacao': None, 'atualizado_em': None,
                'criado_em': '2026-11-15T12:00:00-03:00'}]}}
        with patch('painel.services.status.observar_conexao', return_value={
                'status': 'ready', 'status_legivel': 'Conectado', 'conectado': True,
                'qr_disponivel': False, 'consulta_ok': True, 'erro': ''}):
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page()
                page.route('**/estado/**', lambda route: route.fulfill(status=200,
                    content_type='application/json', headers={'Cache-Control': 'no-store'},
                    body=json.dumps(resposta)))
                page.goto(f'{self.live_server_url}/campanhas/{campanha.pk}/')
                page.wait_for_function('''id => document.querySelector(`[data-attempt-row="${id}"] [data-field="status"]`)?.textContent.includes('Aceita')''', arg=1)
                self.assertEqual(page.locator('[data-recipient-state]').text_content(), 'Aceita, aguardando confirmação')
                self.assertTrue(page.locator('[data-campaign-action="retomar"]').is_visible())
                self.assertFalse(page.locator('[data-campaign-action="pausar"]').is_visible())
                self.assertEqual(page.locator('[data-count="entregue"]').count(), 0)
                self.assertIn('Texto imutável', page.locator('.message-preview').text_content())
                browser.close()

    def test_pesquisa_nova_aparece_na_tabela_sem_substituir_formulario(self):
        from playwright.sync_api import sync_playwright

        resposta = {'observado_em': '2026-11-15T12:00:00-03:00', 'conexao': {
            'status': 'disconnected', 'status_legivel': 'Desconectado', 'conectado': False,
            'qr_disponivel': False, 'verificado_em': '2026-11-15T12:00:00-03:00',
            'consulta_ok': True, 'erro': ''}, 'pesquisas': {'itens': [{
            'id': 902, 'termos': 'Pesquisa dinâmica', 'estado': 'processando',
            'rotulo': 'Processando', 'processadas': 4, 'criada_em': '2026-11-15T12:00:00-03:00',
            'inicio': None, 'fim': None, 'erro': ''}]}}
        with patch('painel.services.status.observar_conexao', return_value={
                'status': 'disconnected', 'status_legivel': 'Desconectado', 'conectado': False,
                'qr_disponivel': False, 'consulta_ok': True, 'erro': ''}), \
             patch('painel.services.delivery.autorizar_reenvio', side_effect=lambda pk, motivo: Campanha.objects.create(
                 nome='Reenvio sintético', texto_snapshot='Oi', limite=1, session_id='sessao-sintetica')):
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True); page = browser.new_page()
                page.route('**/estado/**', lambda route: route.fulfill(status=200,
                    content_type='application/json', headers={'Cache-Control': 'no-store'},
                    body=json.dumps(resposta)))
                page.goto(f'{self.live_server_url}/pesquisas/')
                page.locator('textarea[name="termos"]').fill('rascunho preservado')
                page.wait_for_function("document.querySelector('[data-research-row=\"902\"] [data-field=\"processadas\"]')?.textContent === '4'")
                self.assertEqual(page.locator('textarea[name="termos"]').input_value(), 'rascunho preservado')
                browser.close()

    def test_linha_nova_no_historico_preserva_textarea_de_reenvio(self):
        from playwright.sync_api import sync_playwright

        calls = {'count': 0}
        def status_response(route):
            calls['count'] += 1
            first = calls['count'] == 1
            attempts = []
            for attempt_id, state in ((903, 'falha' if first else 'incerto'),
                                      (904, 'aceita' if first else 'falha')):
                label = {'falha': 'Falha explícita', 'aceita': 'Aceita, aguardando confirmação',
                         'incerto': 'Pendente de verificação'}[state]
                attempts.append({'id': attempt_id, 'destinatario_id': 44, 'campanha_id': 45,
                    'empresa_nome': 'Empresa externa', 'campanha_nome': 'Campanha sintética',
                    'telefone': '5546999000000', 'estado': state, 'rotulo': label, 'erro': 'erro',
                    'confirmado_em': None, 'ultima_verificacao': None,
                    'atualizado_em': '2026-11-15T12:00:00-03:00',
                    'criado_em': '2026-11-15T12:00:00-03:00'})
            body = {'observado_em': '2026-11-15T12:00:00-03:00', 'conexao': {
                'status': 'disconnected', 'status_legivel': 'Desconectado', 'conectado': False,
                'qr_disponivel': False, 'verificado_em': '2026-11-15T12:00:00-03:00',
                'consulta_ok': True, 'erro': ''}, 'historico': {'tentativas': attempts,
                'reservas_antigas': []}}
            route.fulfill(status=200, content_type='application/json',
                headers={'Cache-Control': 'no-store'}, body=json.dumps(body))
        with patch('painel.services.status.observar_conexao', return_value={
                'status': 'disconnected', 'status_legivel': 'Desconectado', 'conectado': False,
                'qr_disponivel': False, 'consulta_ok': True, 'erro': ''}), \
             patch('painel.services.delivery.autorizar_reenvio', side_effect=lambda pk, motivo: Campanha.objects.create(
                 nome='Reenvio sintético', texto_snapshot='Oi', limite=1, session_id='sessao-sintetica')):
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True); page = browser.new_page()
                page.route('**/estado/**', status_response)
                page.goto(f'{self.live_server_url}/historico/')
                page.locator('[data-attempt-row="903"] details summary').click()
                page.locator('[data-attempt-row="903"] textarea[name="motivo"]').fill('revisar depois')
                page.wait_for_function("document.querySelector('[data-attempt-row=\"903\"] [data-field=\"status\"]')?.textContent.includes('Pendente') && document.querySelector('[data-attempt-row=\"904\"] [data-field=\"status\"]')?.textContent.includes('Falha')", timeout=6000)
                self.assertEqual(page.locator('[data-attempt-row="904"] form[action="/tentativas/904/reenviar/"]').count(), 1)
                self.assertEqual(page.locator('[data-attempt-row="903"] textarea[name="motivo"]').input_value(), 'revisar depois')
                browser.close()

    def test_reenvio_dinamico_de_aceita_para_falha_envia_post_com_csrf(self):
        from playwright.sync_api import sync_playwright
        from painel.models import Campanha, Destinatario, Empresa, Reserva, Tentativa

        empresa = Empresa.objects.create(nome='Reenvio sintético', maps_id='ui-reenvio')
        campanha = Campanha.objects.create(nome='Original', texto_snapshot='Oi', limite=1,
            session_id='sessao-sintetica')
        destinatario = Destinatario.objects.create(campanha=campanha, empresa=empresa, ordem=1,
            telefone='5546999000777', mensagem='Oi', mensagem_hash='a' * 64)
        reserva = Reserva.objects.create(empresa_id=empresa.pk, session_id='sessao-sintetica',
            telefone=destinatario.telefone, mensagem='Oi', mensagem_hash='a' * 64, status='aceita')
        tentativa = Tentativa.objects.create(destinatario=destinatario, reserva=reserva, estado='aceita',
            telefone=destinatario.telefone, session_id='sessao-sintetica', mensagem='Oi', mensagem_hash='a' * 64)
        estado = {'observado_em': '2026-11-15T12:00:00-03:00', 'conexao': {
            'status': 'disconnected', 'status_legivel': 'Desconectado', 'conectado': False,
            'qr_disponivel': False, 'verificado_em': '2026-11-15T12:00:00-03:00',
            'consulta_ok': True, 'erro': ''}, 'historico': {'tentativas': [{
            'id': tentativa.pk, 'destinatario_id': destinatario.pk, 'campanha_id': campanha.pk,
            'empresa_nome': empresa.nome, 'campanha_nome': campanha.nome, 'telefone': destinatario.telefone,
            'estado': 'falha', 'rotulo': 'Falha explícita', 'erro': 'Falha sintética',
            'confirmado_em': None, 'ultima_verificacao': None, 'atualizado_em': None,
            'criado_em': '2026-11-15T12:00:00-03:00'}], 'reservas_antigas': []}}
        with patch('painel.services.status.observar_conexao', return_value={
                'status': 'disconnected', 'status_legivel': 'Desconectado', 'conectado': False,
                'qr_disponivel': False, 'consulta_ok': True, 'erro': ''}), \
             patch('painel.services.delivery.autorizar_reenvio', side_effect=lambda pk, motivo: Campanha.objects.create(
                 nome='Reenvio sintético', texto_snapshot='Oi', limite=1, session_id='sessao-sintetica')):
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True); page = browser.new_page()
                respostas_post = []
                page.on('response', lambda response: respostas_post.append(response.status)
                        if '/tentativas/' in response.url and '/reenviar/' in response.url else None)
                page.route('**/estado/**', lambda route: route.fulfill(status=200,
                    content_type='application/json', headers={'Cache-Control': 'no-store'}, body=json.dumps(estado)))
                page.goto(f'{self.live_server_url}/historico/')
                page.locator(f'[data-attempt-row="{tentativa.pk}"] details summary').wait_for(state='visible')
                page.locator(f'[data-attempt-row="{tentativa.pk}"] details summary').click()
                motivo = page.locator(f'[data-attempt-row="{tentativa.pk}"] textarea[name="motivo"]')
                motivo.fill('Operador autorizou a revisão')
                self.assertTrue(page.locator(f'[data-attempt-row="{tentativa.pk}"] input[name="csrfmiddlewaretoken"]').input_value())
                page.locator(f'[data-attempt-row="{tentativa.pk}"] button[type="submit"]').click()
                page.wait_for_load_state('domcontentloaded')
                self.assertNotEqual(page.locator('body').get_attribute('data-state-section'), None)
                self.assertIn(302, respostas_post)
                self.assertNotIn(403, respostas_post)
                browser.close()
        self.assertEqual(Campanha.objects.count(), 2)
        self.assertEqual(Campanha.objects.exclude(pk=campanha.pk).get().estado, Campanha.Estado.RASCUNHO)

    def test_lista_manual_preserva_selecao_ao_paginar_e_recolhe_resumo_51_para_50(self):
        from playwright.sync_api import sync_playwright
        from painel.models import Empresa, Perfil, Tag

        ramo = Tag.objects.create(nome='Ramo sintético', tipo=Tag.Tipo.RAMO)
        categoria = Tag.objects.create(nome='Categoria sintética', tipo=Tag.Tipo.CATEGORIA)
        empresas = []
        for index in range(120):
            empresa = Empresa.objects.create(nome=f'Empresa {index:03}', telefone=f'554699900{index:04}',
                                               maps_id=f'ui-selection:{index}')
            if index < 60:
                Perfil.objects.get_or_create(empresa=empresa)[0].tags.add(ramo, categoria)
            empresas.append(empresa)
        estado = {'observado_em': '2026-11-15T12:00:00-03:00', 'conexao': {
            'status': 'disconnected', 'status_legivel': 'Desconectado', 'conectado': False,
            'qr_disponivel': False, 'verificado_em': '2026-11-15T12:00:00-03:00',
            'consulta_ok': True, 'erro': ''}, 'campanhas': {'itens': []}}
        with patch('painel.services.status.observar_conexao', return_value={
                'status': 'disconnected', 'status_legivel': 'Desconectado', 'conectado': False,
                'qr_disponivel': False, 'consulta_ok': True, 'erro': ''}):
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page()
                page.route('**/estado/**', lambda route: route.fulfill(status=200,
                    content_type='application/json', headers={'Cache-Control': 'no-store'},
                    body=json.dumps(estado)))
                page.goto(f'{self.live_server_url}/campanhas/')
                page.locator('input[name="modo"][value="manual"]').check()
                page.wait_for_function("document.querySelectorAll('#company-results input[data-empresa-id]').length === 50")
                for checkbox in page.locator('#company-results input[data-empresa-id]').all():
                    checkbox.check()
                page.locator('#company-pagination [data-page-step="1"]').click()
                target_id = empresas[50].pk
                target = page.locator(f'#company-results input[data-empresa-id="{target_id}"]')
                target.wait_for(state='visible')
                target.check()
                page.locator('#company-search').fill('sem correspondência')
                page.wait_for_function("document.querySelector('#company-result-count')?.textContent === '0 empresas'")
                page.wait_for_function("document.querySelector('#selected-companies-summary h3')?.textContent.includes('(51)')")
                summary_next = page.locator('#selected-companies-summary [data-summary-pagination] button').nth(1)
                summary_next.click()
                page.locator(f'#selected-companies-summary input[data-summary-id="{target_id}"]').evaluate(
                    "node => { node.checked = false; node.dispatchEvent(new Event('change', {bubbles:true})) }")
                page.wait_for_function("document.querySelector('#selected-companies-summary h3')?.textContent.includes('(50)')")
                self.assertEqual(page.locator('#manual-total').text_content(), '50')
                self.assertEqual(page.locator('#selected-companies-summary [data-summary-pagination]').count(), 0)
                browser.close()
