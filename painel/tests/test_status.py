from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone


class EstadoPainelTests(TestCase):
    def test_rota_retorna_estado_operacional_json(self):
        from painel.models import Campanha, Empresa, Pesquisa
        Pesquisa.objects.create(termos='consulta sintética', estado='processando', processadas=2)
        Empresa.objects.create(nome='Empresa sintética', maps_id='status:empresa')
        Campanha.objects.create(nome='Campanha sintética', texto_snapshot='Oi', limite=1,
                               session_id='sessao-falsa', estado=Campanha.Estado.ATIVA)
        with patch('painel.services.integration.estado_conexao', return_value={
            'status': 'ready', 'status_legivel': 'Conectado', 'conectado': True,
            'qr_disponivel': False, 'verificado_em': '2026-11-15T12:00:00-03:00',
        }):
            resposta = self.client.get(reverse('estado_painel'), {'secao': 'inicio'})
        self.assertEqual(resposta.status_code, 200)
        corpo = resposta.json()
        self.assertTrue(corpo['conexao']['conectado'])
        self.assertEqual(corpo['inicio']['total_empresas'], 1)
        self.assertEqual(corpo['inicio']['pesquisas_recentes'][0]['processadas'], 2)
        self.assertIn('no-store', resposta.headers['Cache-Control'])

    def test_campanha_conta_destinatarios_e_tentativas_sem_promover_aceita(self):
        from painel.models import Campanha, Destinatario, Empresa, Reserva, Tentativa
        campanha = Campanha.objects.create(nome='Campanha', texto_snapshot='Oi', limite=2,
                                           session_id='sessao-falsa')
        for ordem in (1, 2):
            empresa = Empresa.objects.create(nome=f'Empresa {ordem}', maps_id=f'status:{ordem}')
            destinatario = Destinatario.objects.create(campanha=campanha, empresa=empresa,
                ordem=ordem, telefone=f'55469990000{ordem}', mensagem='Oi', mensagem_hash=str(ordem),
                bloqueado_motivo='Sem autorização' if ordem == 2 else '')
            if ordem == 1:
                reserva = Reserva.objects.create(empresa_id=empresa.pk, session_id='sessao-falsa',
                    telefone=destinatario.telefone, mensagem='Oi', mensagem_hash='a', status='aceita')
                Tentativa.objects.create(destinatario=destinatario, reserva=reserva, estado='aceita')
                entregue = Tentativa.objects.create(destinatario=destinatario, reserva=reserva, estado='entregue')
                Tentativa.objects.create(destinatario=destinatario, reserva=reserva, estado='incerto')
                Tentativa.objects.filter(pk=entregue.pk).update(criado_em=timezone.now() + timezone.timedelta(seconds=1))
        with patch('painel.services.integration.estado_conexao', return_value={'status': 'disconnected', 'conectado': False}):
            resposta = self.client.get(reverse('estado_painel'), {'secao': 'campanha', 'campanha_id': campanha.pk})
        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()['campanha']['contagens']
        self.assertEqual(dados['destinatarios_total'], 2)
        self.assertEqual(dados['sem_tentativa'], 1)
        self.assertEqual(dados['sem_tentativa_bloqueados'], 1)
        self.assertEqual(dados['tentativas_total'], 3)
        self.assertEqual(dados['tentativas_por_estado']['aceita'], 1)
        self.assertEqual(dados['destinatarios_por_estado']['entregue'], 1)
        self.assertEqual(dados['tentativas_confirmadas'], 1)
        self.assertEqual(sum(dados['destinatarios_por_estado'].values()) + dados['sem_tentativa'],
                         dados['destinatarios_total'])

    def test_campanha_concluida_com_aceita_nao_afirma_entrega(self):
        from painel.models import Campanha, Destinatario, Empresa, Reserva, Tentativa
        campanha = Campanha.objects.create(nome='Concluída', texto_snapshot='Oi', limite=1,
            session_id='sessao-falsa', estado=Campanha.Estado.CONCLUIDA)
        empresa = Empresa.objects.create(nome='Pendente', maps_id='status:concluida')
        destinatario = Destinatario.objects.create(campanha=campanha, empresa=empresa, ordem=1,
            telefone='5546999000100', mensagem='Oi', mensagem_hash='x')
        reserva = Reserva.objects.create(empresa_id=empresa.pk, session_id='sessao-falsa',
            telefone=destinatario.telefone, mensagem='Oi', mensagem_hash='x', status='aceita')
        Tentativa.objects.create(destinatario=destinatario, reserva=reserva, estado='aceita')
        with patch('painel.services.integration.estado_conexao', return_value={'status': 'disconnected'}):
            dados = self.client.get(reverse('estado_painel'), {
                'secao': 'campanha', 'campanha_id': campanha.pk}).json()['campanha']
        self.assertEqual(dados['estado'], 'concluida')
        self.assertEqual(dados['contagens']['tentativas_por_estado']['aceita'], 1)
        self.assertEqual(dados['contagens']['destinatarios_por_estado']['aceita'], 1)
        self.assertEqual(dados['contagens']['destinatarios_por_estado']['entregue'], 0)

    def test_falha_de_conexao_nao_apaga_estado_local_e_fica_incerta(self):
        from painel.models import Empresa
        empresa = Empresa.objects.create(nome='Preservada', maps_id='status:offline')
        with patch('painel.services.integration.estado_conexao', side_effect=OSError('segredo externo')):
            resposta = self.client.get(reverse('estado_painel'), {'secao': 'inicio'})
        corpo = resposta.json()
        self.assertFalse(corpo['conexao']['consulta_ok'])
        self.assertIsNone(corpo['conexao']['conectado'])
        self.assertEqual(corpo['inicio']['total_empresas'], 1)
        self.assertEqual(corpo['conexao']['erro'], 'Serviço local indisponível; último estado válido mantido.')
        self.assertNotIn('segredo externo', resposta.content.decode())

    def test_status_desconhecido_ou_ready_com_erro_nao_e_confirmacao(self):
        from painel.services.status import observar_conexao
        for estado in ({'status': 'unexpected'}, {'status': 'ready', 'erro': 'erro'}):
            with self.subTest(estado=estado), patch('painel.services.integration.estado_conexao', return_value=estado):
                observado = observar_conexao()
                self.assertFalse(observado['consulta_ok'])
                self.assertIsNone(observado['conectado'])

    def test_estado_real_nao_configurado_e_desconectado_valido(self):
        from painel.services import integration
        from painel.services.status import observar_conexao
        with patch.object(integration, '_ler_config', return_value={}):
            observado = observar_conexao()
        self.assertTrue(observado['consulta_ok'])
        self.assertFalse(observado['conectado'])
        self.assertEqual(observado['status'], 'nao_configurado')

    def test_parametros_metodos_e_cache_da_rota_estado(self):
        invalidos = (
            {}, {'secao': 'desconhecida'}, {'secao': 'campanha'},
            {'secao': 'inicio', 'campanha_id': ''}, {'secao': 'outra', 'campo': 'x'},
            {'secao': 'campanha', 'campanha_id': '0'},
        )
        for query in invalidos:
            with self.subTest(query=query):
                resposta = self.client.get(reverse('estado_painel'), query)
                self.assertEqual(resposta.status_code, 400)
                self.assertIn('no-store', resposta.headers['Cache-Control'])
        ausente = self.client.get(reverse('estado_painel'), {'secao': 'campanha', 'campanha_id': '99999'})
        self.assertEqual(ausente.status_code, 404)
        self.assertIn('no-store', ausente.headers['Cache-Control'])
        post = self.client.post(reverse('estado_painel'), {'secao': 'inicio'})
        self.assertEqual(post.status_code, 405)
        self.assertIn('no-store', post.headers['Cache-Control'])

    def test_get_repetido_nao_chama_acoes_nem_modifica_banco(self):
        from painel.models import Campanha, Empresa, Pesquisa, Tentativa
        empresa = Empresa.objects.create(nome='Imutável', maps_id='status:read-only')
        Pesquisa.objects.create(termos='não processar')
        campanha = Campanha.objects.create(nome='Não iniciar', texto_snapshot='Oi', limite=1,
                                           session_id='sessao-falsa')
        before = (Empresa.objects.count(), Pesquisa.objects.count(), Campanha.objects.count(), Tentativa.objects.count())
        with patch('painel.services.integration.estado_conexao', return_value={'status': 'ready'}), \
             patch('painel.services.worker.executar_ciclo', side_effect=AssertionError), \
             patch('painel.services.worker.processar_pesquisa', side_effect=AssertionError), \
             patch('painel.services.delivery.atualizar_recibos', side_effect=AssertionError), \
             patch('painel.services.delivery.enviar_um', side_effect=AssertionError):
            for _ in range(2):
                self.assertEqual(self.client.get(reverse('estado_painel'), {'secao': 'inicio'}).status_code, 200)
                self.assertEqual(self.client.get(reverse('estado_painel'), {'secao': 'campanhas'}).status_code, 200)
        after = (Empresa.objects.count(), Pesquisa.objects.count(), Campanha.objects.count(), Tentativa.objects.count())
        self.assertEqual(before, after)
        self.assertTrue(Empresa.objects.filter(pk=empresa.pk).exists())
        self.assertEqual(campanha.estado, Campanha.Estado.RASCUNHO)

