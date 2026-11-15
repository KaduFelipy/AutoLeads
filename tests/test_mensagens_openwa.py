import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import mensagens_openwa as mensagens


class ServidorOpenWA(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def responder(self, codigo, dados):
        self.send_response(codigo)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(dados).encode())

    def do_GET(self):
        self.server.chamadas.append(('GET', self.path))
        if '/contacts/check/' in self.path:
            self.responder(200, {'number': '5546999991234', 'exists': self.server.existe,
                                 'whatsappId': '5546999991234@c.us' if self.server.existe else None})
        else:
            self.responder(200, {'id': 'teste', 'status': self.server.status_sessao})

    def do_POST(self):
        dados = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.chamadas.append(('POST', self.path, dados, self.headers.get('X-API-Key')))
        self.responder(self.server.status_envio, {'messageId': 'msg-1', 'timestamp': 1700000000}
                       if self.server.status_envio == 201 else {'message': 'erro simulado'})


class MensagensTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.servidor = ThreadingHTTPServer(('127.0.0.1', 0), ServidorOpenWA)
        cls.thread = threading.Thread(target=cls.servidor.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.servidor.shutdown()
        cls.servidor.server_close()
        cls.thread.join()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.banco = Path(self.tmp.name) / 'empresas.db'
        with closing(sqlite3.connect(self.banco)) as db:
            db.execute('CREATE TABLE empresas (id INTEGER PRIMARY KEY, nome TEXT, endereco TEXT, telefone TEXT, site TEXT, avaliacao REAL)')
            db.executemany('INSERT INTO empresas VALUES (?, ?, ?, ?, ?, ?)', [
                (1, 'Café Central', 'Centro, Pato Branco', '(46) 99999-1234', None, 4.8),
                (2, 'Mercado Norte', 'Norte, Pato Branco', '(46) 3225-2462', None, 4.1),
                (3, 'Café sem telefone', 'Centro, Pato Branco', None, None, None),
            ])
            db.commit()
        self.servidor.chamadas = []
        self.servidor.existe = True
        self.servidor.status_sessao = 'ready'
        self.servidor.status_envio = 201
        self.cliente = mensagens.OpenWAClient(
            f'http://127.0.0.1:{self.servidor.server_port}', 'chave-teste', 'teste')

    def tearDown(self):
        self.tmp.cleanup()

    def test_telefone_brasileiro_preserva_fixo_e_celular(self):
        for entrada, esperado in [('(46) 99999-1234', '5546999991234'),
                                   ('+55 46 99999-1234', '5546999991234'),
                                   ('0055 46 3225-2462', '554632252462'),
                                   ('(46) 3225-2462', '554632252462')]:
            with self.subTest(entrada=entrada):
                self.assertEqual(mensagens.normalizar_telefone(entrada), esperado)
        for entrada in [None, '0800 123 4567', '99999-1234', 'texto46 999991234',
                        '+46 99999-1234', '+55 99999-1234', '0055 99999-1234']:
            with self.subTest(entrada=entrada), self.assertRaises(ValueError):
                mensagens.normalizar_telefone(entrada)

    def test_filtros_combinados_nao_mudam_banco(self):
        empresas = mensagens.selecionar_empresas(self.banco, nome='Café', endereco='Centro',
                                                avaliacao_min=4.5, limite=10)
        self.assertEqual([empresa['id'] for empresa in empresas], [1])
        self.assertEqual(mensagens.selecionar_empresas(self.banco, nome="' OR 1=1 --"), [])

    def test_filtro_sem_distinguir_caixa_em_nome_acentuado(self):
        empresas = mensagens.selecionar_empresas(self.banco, nome='CAFÉ', avaliacao_min=4.5)
        self.assertEqual([empresa['id'] for empresa in empresas], [1])

    def test_mensagem_com_apenas_nome_e_modelo_valido(self):
        resumo = mensagens.processar_lote(self.banco, '{nome}', ids=[1])
        self.assertEqual(resumo['previas'][0]['mensagem'], 'Café Central')

    def test_previa_personaliza_sem_http_nem_historico(self):
        resumo = mensagens.processar_lote(self.banco, 'Olá, {nome}!', nome='Café', limite=10)
        self.assertEqual(resumo['previas'][0]['mensagem'], 'Olá, Café Central!')
        self.assertEqual(resumo['previstos'], 1)
        self.assertEqual(resumo['ignorados'], 1)
        self.assertEqual(self.servidor.chamadas, [])
        with closing(sqlite3.connect(self.banco)) as db:
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE name='envios_whatsapp'").fetchall(), [])

    def test_envia_endpoint_correto_e_registra_aceite(self):
        resumo = mensagens.processar_lote(self.banco, 'Olá, {nome}!', self.cliente,
                                          enviar=True, ids=[1], intervalo=0)
        self.assertEqual(resumo['aceitos'], 1)
        post = [chamada for chamada in self.servidor.chamadas if chamada[0] == 'POST'][0]
        self.assertEqual(post, ('POST', '/api/sessions/teste/messages/send-text',
                               {'chatId': '5546999991234@c.us', 'text': 'Olá, Café Central!'}, 'chave-teste'))
        with closing(sqlite3.connect(self.banco)) as db:
            self.assertEqual(db.execute('SELECT status, message_id FROM envios_whatsapp').fetchall(),
                             [('aceita', 'msg-1')])

    def test_repetir_lote_nao_reenvia(self):
        for _ in range(2):
            resumo = mensagens.processar_lote(self.banco, 'Olá!', self.cliente,
                                              enviar=True, ids=[1], intervalo=0)
        self.assertEqual(resumo['repetidos'], 1)
        self.assertEqual(len([c for c in self.servidor.chamadas if c[0] == 'POST']), 1)

    def test_numero_nao_registrado_nao_recebe_mensagem(self):
        self.servidor.existe = False
        resumo = mensagens.processar_lote(self.banco, 'Olá!', self.cliente,
                                          enviar=True, ids=[1], intervalo=0)
        self.assertEqual(resumo['sem_whatsapp'], 1)
        self.assertFalse(any(c[0] == 'POST' for c in self.servidor.chamadas))

    def test_erro_apos_post_bloqueia_reenvio_automatico(self):
        self.servidor.status_envio = 503
        resumo = mensagens.processar_lote(self.banco, 'Olá!', self.cliente,
                                          enviar=True, ids=[1, 2], intervalo=0)
        self.assertEqual(resumo['incertos'], 1)
        self.assertTrue(resumo['interrompido'])
        mensagens.processar_lote(self.banco, 'Olá!', self.cliente,
                                 enviar=True, ids=[1], intervalo=0)
        self.assertEqual(len([c for c in self.servidor.chamadas if c[0] == 'POST']), 1)

    def test_sessao_desconectada_impede_envio(self):
        self.servidor.status_sessao = 'qr_ready'
        with self.assertRaises(mensagens.OpenWAError):
            mensagens.processar_lote(self.banco, 'Olá!', self.cliente,
                                     enviar=True, ids=[1], intervalo=0)
        self.assertFalse(any(c[0] == 'POST' for c in self.servidor.chamadas))

    def test_modelo_invalido_falha_antes_de_enviar(self):
        for modelo in [' ', '{campo_inexistente}', 'x' * 4097]:
            with self.subTest(modelo=modelo[:30]), self.assertRaises(ValueError):
                mensagens.processar_lote(self.banco, modelo, self.cliente,
                                         enviar=True, ids=[1], intervalo=0)
        self.assertEqual(self.servidor.chamadas, [])


if __name__ == '__main__':
    unittest.main()
