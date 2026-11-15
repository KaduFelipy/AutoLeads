import base64
import os
from unittest.mock import patch

from django.test import SimpleTestCase


class FakeOpenWA:
    def __init__(self, url, api_key, session_id='', timeout=60):
        self.url = url
        self.api_key = api_key
        self.session_id = session_id
        self.timeout = timeout
        self.respostas = []
        self.chamadas = []

    def request(self, method, path, data=None):
        self.chamadas.append((method, path, data))
        resposta = self.respostas.pop(0)
        if isinstance(resposta, Exception):
            raise resposta
        return resposta

    def caminho_sessao(self):
        return '/sessions/' + self.session_id

    def status(self):
        return self.request('GET', self.caminho_sessao())


class IntegrationTests(SimpleTestCase):
    def setUp(self):
        self.config = {'OPENWA_URL': 'http://127.0.0.1:2785',
                       'OPENWA_API_KEY': 'synthetic-secret',
                       'OPENWA_SESSION_ID': 'sessao-teste'}
        self.config_patch = patch('mensagens_openwa.ler_config', return_value=self.config)
        self.ler_config_fake = self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.env_patch = patch.dict(os.environ, {'PAINEL_OPENWA_CONFIG': ''})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def test_config_path_de_qa_e_server_only(self):
        from painel.services.integration import _ler_config
        fake_path = r'C:\temp\openwa-fixture.env'
        with patch.dict(os.environ, {'PAINEL_OPENWA_CONFIG': fake_path}), \
             patch('mensagens_openwa.ler_config', return_value=self.config) as leitor:
            config = _ler_config()
        leitor.assert_called_once_with(fake_path)
        self.assertEqual(config['OPENWA_SESSION_ID'], 'sessao-teste')

    def test_cliente_aceita_somente_loopback_e_timeout_curto(self):
        from painel.services.integration import cliente_local
        with patch('mensagens_openwa.ler_config', return_value=self.config):
            cliente = cliente_local()
        self.assertEqual(cliente.url, 'http://127.0.0.1:2785')
        self.assertEqual(cliente.timeout, 10)
        for url in ('http://10.0.0.1:2785', 'http://127.0.0.1.attacker.test:2785',
                    'http://user@localhost:2785'):
            with self.subTest(url=url), patch('mensagens_openwa.ler_config',
                                                return_value={**self.config, 'OPENWA_URL': url}):
                with self.assertRaises(ValueError):
                    cliente_local()

    def test_configuracao_publica_nao_expoe_chave(self):
        from painel.services.integration import configuracao_publica
        with patch('mensagens_openwa.ler_config', return_value=self.config):
            publico = configuracao_publica()
        self.assertEqual(publico, {'configurada': True, 'session_id': 'sessao-teste'})
        self.assertNotIn('api_key', publico)
        self.assertNotIn('synthetic-secret', repr(publico))

    def test_estado_pronto_tem_horario_e_nao_expoe_detalhes_da_chave(self):
        from painel.services.integration import estado_conexao
        cliente = FakeOpenWA('', '', 'sessao-teste')
        cliente.respostas = [{'status': 'ready'}]
        with patch('painel.services.integration.cliente_local', return_value=cliente):
            estado = estado_conexao()
        self.assertTrue(estado['conectado'])
        self.assertEqual(estado['status'], 'ready')
        self.assertIsNotNone(estado['verificado_em'])
        self.ler_config_fake.assert_called()

    def test_estado_desconectado_e_erro_de_rede_sao_representados(self):
        from mensagens_openwa import OpenWAError
        from painel.services.integration import estado_conexao
        cliente = FakeOpenWA('', '', 'sessao-teste')
        cliente.respostas = [{'status': 'qr_ready'}]
        with patch('painel.services.integration.cliente_local', return_value=cliente):
            estado = estado_conexao()
        self.assertFalse(estado['conectado'])
        self.assertTrue(estado['qr_disponivel'])
        cliente.status = lambda: (_ for _ in ()).throw(OpenWAError('OpenWA offline.'))
        with patch('painel.services.integration.cliente_local', return_value=cliente):
            estado = estado_conexao()
        self.assertFalse(estado['conectado'])
        self.assertIn('erro', estado)

    def test_qr_exige_formato_png_data_uri(self):
        from mensagens_openwa import OpenWAError
        from painel.services.integration import obter_qr
        png = b'\x89PNG\r\n\x1a\n' + b'payload-sintetico'
        cliente = FakeOpenWA('', '', 'sessao-teste')
        cliente.respostas = [
            {'status': 'qr_ready'},
            {'qrCode': 'data:image/png;base64,' + base64.b64encode(png).decode('ascii')},
        ]
        with patch('painel.services.integration.cliente_local', return_value=cliente):
            self.assertEqual(obter_qr(), png)
        cliente.respostas = [{'status': 'qr_ready'}, {'qrCode': 'data:image/png;base64,%%%'}]
        with patch('painel.services.integration.cliente_local', return_value=cliente):
            with self.assertRaises(OpenWAError):
                obter_qr()

    def test_conectar_reusa_sessao_e_persiste_id_sem_alterar_segredo(self):
        from painel.services import integration
        cliente = FakeOpenWA('', '', 'sessao-teste')
        cliente.respostas = [[{'id': 'sessao-teste', 'name': 'scrapgooglemaps',
                               'status': 'qr_ready', 'engineLoaded': True}]]
        arquivo = 'OPENWA_URL=http://127.0.0.1:2785\nOPENWA_API_KEY=synthetic-secret\nOPENWA_SESSION_ID=\n'
        with patch.object(integration, 'cliente_local', return_value=cliente), \
             patch('mensagens_openwa.CONFIG') as config_path:
            config_path.exists.return_value = True
            config_path.read_text.return_value = arquivo
            integration.conectar_sessao()
            escrita = config_path.write_text.call_args.args[0]
        self.assertIn('OPENWA_API_KEY=synthetic-secret', escrita)
        self.assertIn('OPENWA_SESSION_ID=sessao-teste', escrita)
        self.assertEqual(cliente.chamadas, [('GET', '/sessions', None)])

    def test_conectar_cria_somente_sessao_com_nome_proprio_e_inicia_sem_esperar(self):
        from painel.services import integration
        cliente = FakeOpenWA('', '', '')
        cliente.respostas = [[], {'id': 'sessao-nova', 'name': 'scrapgooglemaps',
                                  'status': 'initializing', 'engineLoaded': False}, {}]
        arquivo = 'OPENWA_API_KEY=synthetic-secret\n'
        with patch.object(integration, 'cliente_local', return_value=cliente), \
             patch('mensagens_openwa.CONFIG') as config_path:
            config_path.exists.return_value = True
            config_path.read_text.return_value = arquivo
            integration.conectar_sessao()
        self.assertEqual(cliente.chamadas, [
            ('GET', '/sessions', None),
            ('POST', '/sessions', {'name': 'scrapgooglemaps'}),
            ('POST', '/sessions/sessao-nova/start', None),
        ])
