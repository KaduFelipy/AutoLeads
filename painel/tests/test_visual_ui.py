import json
import uuid
from pathlib import Path
from unittest.mock import patch

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse


class AutoLeadsVisualUITests(StaticLiveServerTestCase):
    def setUp(self):
        super().setUp()
        self.state_reads = 0
        from painel.models import Campanha, Destinatario, Empresa, Perfil, Pesquisa, RegraTag, Tag, Texto

        suffix = uuid.uuid4().hex
        self.empresa = Empresa.objects.create(
            nome='Oficina Sintética', endereco='Rua de Teste, 123', telefone='5546999000000',
            site='https://example.invalid/oficina', maps_id=f'visual-ui-{suffix}',
        )
        self.tag = Tag.objects.create(nome='Mecânica sintética', tipo=Tag.Tipo.RAMO)
        Perfil.objects.get_or_create(empresa=self.empresa)[0].tags.add(self.tag)
        self.pesquisa = Pesquisa.objects.create(termos='oficina sintética Curitiba', limite=20)
        self.texto = Texto.objects.create(nome='Texto sintético', corpo='Olá {nome}, mensagem fictícia.')
        regra = RegraTag.objects.create(termo='oficina', prioridade=1)
        regra.tags.add(self.tag)
        self.campanha = Campanha.objects.create(
            nome='Campanha sintética', texto_snapshot='Olá Oficina Sintética', limite=1,
            session_id='sessao-visual-ficticia',
        )
        self.destinatario = Destinatario.objects.create(
            campanha=self.campanha, empresa=self.empresa, ordem=1, telefone=self.empresa.telefone,
            mensagem='Olá Oficina Sintética', mensagem_hash='b' * 64,
        )

    @staticmethod
    def conexao_sintetica():
        return {
            'status': 'disconnected', 'status_legivel': 'Desconectado', 'conectado': False,
            'consulta_ok': True, 'qr_disponivel': False,
            'verificado_em': '2026-11-15T12:00:00-03:00', 'erro': '',
        }

    def resposta_estado(self, route):
        from urllib.parse import parse_qs, urlparse

        self.state_reads += 1
        query = parse_qs(urlparse(route.request.url).query)
        secao = query.get('secao', [''])[0]
        base = {'observado_em': '2026-11-15T12:00:00-03:00', 'conexao': self.conexao_sintetica()}
        base['inicio'] = {
            'total_empresas': 1, 'total_pesquisas': 1, 'campanhas_ativas': 0,
            'pesquisas_recentes': [{
                'id': self.pesquisa.pk, 'termos': self.pesquisa.termos, 'estado': 'pendente',
                'rotulo': 'Pendente', 'processadas': 0, 'criada_em': '2026-11-15T12:00:00-03:00',
                'inicio': None, 'fim': None, 'erro': '',
            }],
        }
        base['pesquisas'] = {'itens': [{
            'id': self.pesquisa.pk, 'termos': self.pesquisa.termos, 'estado': 'pendente',
            'rotulo': 'Pendente', 'processadas': 0, 'criada_em': '2026-11-15T12:00:00-03:00',
            'inicio': None, 'fim': None, 'erro': '',
        }]}
        base['campanhas'] = {'itens': [{
            'id': self.campanha.pk, 'nome': self.campanha.nome, 'estado': 'rascunho',
            'rotulo': 'Rascunho', 'motivo_pausa': '', 'destinatarios_total': 1,
            'criado_em': '2026-11-15T12:00:00-03:00',
        }]}
        base['campanha'] = {
            'id': self.campanha.pk, 'estado': 'rascunho', 'rotulo': 'Rascunho', 'motivo_pausa': '',
            'contagens': {
                'destinatarios_total': 1, 'sem_tentativa': 1, 'sem_tentativa_bloqueados': 0,
                'tentativas_total': 0, 'tentativas_confirmadas': 0,
                'destinatarios_por_estado': {}, 'tentativas_por_estado': {},
            },
            'destinatarios': [{'id': self.destinatario.pk, 'rotulo': 'Aguardando execução'}],
            'tentativas': [],
        }
        base['historico'] = {'tentativas': [], 'reservas_antigas': []}
        route.fulfill(status=200, content_type='application/json',
            headers={'Cache-Control': 'no-store'}, body=json.dumps(base))

    def test_telas_responsivas_contraste_foco_e_previews_sinteticos(self):
        from playwright.sync_api import sync_playwright

        urls = [
            reverse('dashboard'), reverse('pesquisas'), reverse('contatos'),
            reverse('contato_editar', kwargs={'pk': self.empresa.pk}),
            reverse('textos'), reverse('texto_editar'), reverse('campanhas'),
            reverse('campanha_detalhe', kwargs={'pk': self.campanha.pk}), reverse('historico'),
            reverse('whatsapp'), reverse('tags'), reverse('regras'),
        ]
        output = Path(__file__).resolve().parents[2] / 'docs' / 'preview' / 'autoleads-navy'
        output.mkdir(parents=True, exist_ok=True)
        configuracao_ficticia = {'configurada': False, 'session_id': ''}
        with patch('painel.views._conexao', return_value=self.conexao_sintetica()), \
             patch('painel.services.status.observar_conexao', return_value=self.conexao_sintetica()), \
             patch('painel.services.integration.configuracao_publica', return_value=configuracao_ficticia):
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                for width, height in ((1440, 1000), (768, 1024), (390, 844)):
                    page = browser.new_page(viewport={'width': width, 'height': height}, device_scale_factor=1)
                    page.route('**/estado/**', self.resposta_estado)
                    for url in urls:
                        page.goto(f'{self.live_server_url}{url}')
                        page.locator('.brand img').wait_for(state='visible')
                        page.wait_for_function("document.querySelector('.brand img')?.naturalWidth > 0")
                        page.wait_for_function('document.documentElement.scrollWidth <= window.innerWidth')
                        self.assertLessEqual(page.locator('body').evaluate('(el) => el.scrollWidth'), width)
                        self.assertEqual(page.locator('body').evaluate('(el) => getComputedStyle(el).fontSize'), '16px')
                        self.assertEqual(page.locator('.nav-caption:visible').count(), 0 if width <= 900 else 2)
                        if url == reverse('dashboard'):
                            expected_columns = 4 if width >= 1101 else 2 if width > 600 else 1
                            columns = page.locator('.metric-grid').evaluate(
                                "el => getComputedStyle(el).gridTemplateColumns.split(' ').length")
                            self.assertEqual(columns, expected_columns)
                            if width in (1440, 390):
                                page.screenshot(path=str(output / f'dashboard-{width}.png'), full_page=True)
                        if url == reverse('campanhas') and width in (1440, 390):
                            page.screenshot(path=str(output / f'campanhas-{width}.png'), full_page=True)
                        visible_targets = page.locator(
                            'a:visible, button:visible, input:not([type="hidden"]):not([type="checkbox"]):not([type="radio"]):visible, select:visible, textarea:visible, summary:visible, .company-choice:visible'
                        ).evaluate_all('(items) => items.map(el => ({tag: el.tagName, text: el.textContent.trim(), height: el.getBoundingClientRect().height}))')
                        too_small = [target for target in visible_targets if target['height'] < 44]
                        self.assertEqual(too_small, [], f'alvos menores que 44px em {url} a {width}px: {too_small}')
                        outside_targets = page.evaluate('''() => [...document.querySelectorAll('a, button, input:not([type="hidden"]), select, textarea, summary')]
                          .filter(el => el.getClientRects().length && !el.closest('.table-wrap, .sidebar nav'))
                          .filter(el => { const rect = el.getBoundingClientRect(); return rect.left < 0 || rect.right > innerWidth })
                          .map(el => ({tag: el.tagName, text: el.textContent.trim(), rect: el.getBoundingClientRect().toJSON()}))''')
                        self.assertEqual(outside_targets, [], f'alvos fora da janela em {url} a {width}px: {outside_targets}')
                    page.close()
                for width, height in ((390, 844), (768, 1024)):
                    page = browser.new_page(viewport={'width': width, 'height': height})
                    page.route('**/estado/**', self.resposta_estado)
                    page.goto(f'{self.live_server_url}{reverse("dashboard")}')
                    page.keyboard.press('Tab')  # Logo
                    page.keyboard.press('Tab')  # Primeiro link de navegação
                    for index in range(8):
                        if index in (0, 7):
                            link = page.locator('.sidebar nav a').nth(index)
                            self.assertTrue(link.evaluate('(el) => el === document.activeElement'))
                            styles = link.evaluate('(el) => ({outline: getComputedStyle(el).outlineWidth, rect: el.getBoundingClientRect().toJSON(), nav: el.parentElement.getBoundingClientRect().toJSON()})')
                            self.assertEqual(styles['outline'], '3px')
                            self.assertGreaterEqual(styles['rect']['left'] - 6, styles['nav']['left'])
                            self.assertLessEqual(styles['rect']['right'] + 6, styles['nav']['right'])
                        if index < 7:
                            page.keyboard.press('Tab')
                    page.emulate_media(reduced_motion='reduce')
                    self.assertEqual(page.locator('.button.primary').first.evaluate(
                        '(el) => getComputedStyle(el).transitionDuration'), '0s')
                    page.close()
                browser.close()

    def test_cores_computed_pares_contraste_e_paginacao_de_selecao(self):
        from playwright.sync_api import sync_playwright

        with patch('painel.views._conexao', return_value=self.conexao_sintetica()), \
             patch('painel.services.status.observar_conexao', return_value=self.conexao_sintetica()), \
             patch('painel.services.integration.configuracao_publica', return_value={'configurada': False, 'session_id': ''}):
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={'width': 390, 'height': 844})
                page.route('**/estado/**', self.resposta_estado)
                page.goto(f'{self.live_server_url}{reverse("campanhas")}')
                page.locator('input[name="modo"][value="manual"]').check()
                page.locator('[data-company-row] input[type="checkbox"]').first.check()
                page.wait_for_function("document.querySelector('#manual-total')?.textContent === '1'")
                selected_style = page.locator('.company-choice').first.evaluate(
                    "el => ({background: getComputedStyle(el).backgroundColor, text: getComputedStyle(el).color})")
                page.locator('#id_mensagem').fill('Rascunho preservado durante o polling')
                deadline = __import__('time').monotonic() + 6
                while self.state_reads < 2 and __import__('time').monotonic() < deadline:
                    page.wait_for_timeout(50)
                self.assertGreaterEqual(self.state_reads, 2, 'duas leituras sintéticas de estado devem ocorrer')
                self.assertEqual(page.locator('#id_mensagem').input_value(), 'Rascunho preservado durante o polling')
                self.assertEqual(page.locator('#manual-total').text_content(), '1')
                page.locator('#id_mensagem').focus()
                focus_style = page.locator('#id_mensagem').evaluate(
                    "el => ({outline: getComputedStyle(el).outlineWidth, color: getComputedStyle(el).outlineColor})")
                self.assertEqual(focus_style['outline'], '3px')
                field_border = page.locator('#id_mensagem').evaluate(
                    "el => ({border: getComputedStyle(el).borderTopColor, background: getComputedStyle(el).backgroundColor})")
                primary = page.locator('.button.primary').first
                primary.hover()
                primary_style = primary.evaluate(
                    "el => ({background: getComputedStyle(el).backgroundColor, color: getComputedStyle(el).color})")
                outline = page.locator('.button.outline').first
                outline.hover()
                outline_style = outline.evaluate(
                    "el => ({background: getComputedStyle(el).backgroundColor, color: getComputedStyle(el).color})")
                contrast = page.evaluate('''pairs => {
                  const rgb = value => value.match(/[\\d.]+/g).map(Number).slice(0, 3)
                  const luminance = color => {
                    const [r, g, b] = rgb(color).map(c => c / 255).map(c => c <= .04045 ? c / 12.92 : ((c + .055) / 1.055) ** 2.4)
                    return .2126 * r + .7152 * g + .0722 * b
                  }
                  return pairs.map(([foreground, background]) => {
                    const pair = [luminance(foreground), luminance(background)].sort((x, y) => y - x)
                    return (pair[0] + .05) / (pair[1] + .05)
                  })
                }''', [
                    [primary_style['color'], primary_style['background']],
                    [outline_style['color'], outline_style['background']],
                    [field_border['border'], field_border['background']],
                    [selected_style['text'], selected_style['background']],
                    [focus_style['color'], field_border['background']],
                ])
                for index, measured in enumerate(contrast):
                    minimum = 3 if index in (2, 4) else 4.5
                    self.assertGreaterEqual(measured, minimum,
                        f'par de contraste {index}: {measured:.2f}:1 (mínimo {minimum}:1)')
                page.locator('[data-company-row] input[type="checkbox"]').first.uncheck()
                self.assertEqual(page.locator('#manual-total').text_content(), '0')
                values = page.evaluate('''() => {
                  const rgb = value => value.match(/[\\d.]+/g).map(Number).slice(0, 3)
                  const luminance = color => {
                    const [r, g, b] = rgb(color).map(c => c / 255).map(c => c <= .04045 ? c / 12.92 : ((c + .055) / 1.055) ** 2.4)
                    return .2126 * r + .7152 * g + .0722 * b
                  }
                  const ratio = (a, b) => { const pair = [luminance(a), luminance(b)].sort((x, y) => y - x); return (pair[0] + .05) / (pair[1] + .05) }
                  const style = selector => getComputedStyle(document.querySelector(selector))
                  const body = style('body'), panel = style('.panel'), muted = style('.muted'), primary = style('.button.primary'), field = style('#id_mensagem')
                  return {
                    body: body.backgroundColor, ink: body.color,
                    panel: panel.backgroundColor, muted: muted.color,
                    primary: primary.backgroundColor, primaryText: primary.color,
                    field: field.backgroundColor, fieldText: field.color,
                    ratios: [ratio(body.color, body.backgroundColor), ratio(muted.color, body.backgroundColor),
                      ratio(primary.color, primary.backgroundColor), ratio(field.color, field.backgroundColor)],
                    line: style('.panel').borderTopColor,
                    tableFont: getComputedStyle(document.querySelector('th')).fontSize,
                    helperFont: getComputedStyle(document.querySelector('.muted')).fontSize,
                    labelFont: getComputedStyle(document.querySelector('label[for="id_nome"]')).fontSize,
                  }
                }''')
                for measured in values['ratios']:
                    self.assertGreaterEqual(measured, 4.5)
                self.assertEqual(values['tableFont'], '14px')
                self.assertEqual(values['helperFont'], '14px')
                self.assertEqual(values['labelFont'], '14px')
                page.emulate_media(reduced_motion='reduce')
                browser.close()
