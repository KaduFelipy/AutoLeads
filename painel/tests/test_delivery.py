from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from urllib.parse import quote
from unittest.mock import patch

from django.db import close_old_connections
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from mensagens_openwa import OpenWAError


class FakeOpenWA:
    session_id = 'sessao-teste'

    def __init__(self):
        self.estado = {'status': 'ready'}
        self.contato = {'exists': True, 'whatsappId': '5546991110028@c.us'}
        self.envio = {'messageId': 'wamid-1'}
        self.recibos = {'messages': []}
        self.chamadas = []
        self.durante_post = None

    def status(self):
        self.chamadas.append(('status',))
        return self.estado

    def verificar_numero(self, telefone):
        self.chamadas.append(('check', telefone))
        return self.contato

    def enviar_texto(self, chat_id, mensagem):
        self.chamadas.append(('send', chat_id, mensagem))
        if self.durante_post:
            self.durante_post()
        if isinstance(self.envio, Exception):
            raise self.envio
        return self.envio

    def request(self, method, path, data=None):
        self.chamadas.append((method, path, data))
        if isinstance(self.recibos, Exception):
            raise self.recibos
        return self.recibos

    def caminho_sessao(self):
        return '/sessions/' + quote(self.session_id, safe='')


class DeliveryTests(TestCase):
    def setUp(self):
        from painel.models import Campanha, Destinatario, Empresa

        self.empresa = Empresa.objects.create(
            nome='Empresa teste', telefone='(46) 99111-0028', maps_id='maps:delivery'
        )
        self.campanha = Campanha.objects.create(
            nome='Campanha teste', texto_snapshot='Oi {nome}', limite=1,
            session_id='sessao-teste', intervalo=0, estado='ativa'
        )
        self.destinatario = Destinatario.objects.create(
            campanha=self.campanha, empresa=self.empresa, ordem=1,
            telefone='5546991110028', mensagem='Oi Empresa teste',
            mensagem_hash=sha256(b'Oi Empresa teste').hexdigest(),
            empresas_relacionadas=[self.empresa.pk],
        )
        self.cliente = FakeOpenWA()

    def test_reserva_e_tentativa_persistem_antes_do_post(self):
        from painel.models import Reserva, Tentativa
        from painel.services.delivery import enviar_um

        self.cliente.durante_post = lambda: self.assertEqual(
            Tentativa.objects.filter(destinatario=self.destinatario, estado='enviando').count(), 1
        )

        self.assertTrue(enviar_um(self.cliente))

        tentativa = Tentativa.objects.get(destinatario=self.destinatario)
        self.assertEqual(tentativa.estado, 'aceita')
        self.assertEqual(tentativa.message_id, 'wamid-1')
        self.assertIsNone(tentativa.confirmado_em)
        self.assertEqual(Reserva.objects.count(), 1)
        self.assertEqual(self.cliente.chamadas[-1][0], 'send')

    def test_aceite_nao_atualiza_ultimo_disparo_e_reserva_bloqueia_repeticao(self):
        from painel.models import Tentativa
        from painel.services.campaigns import ultimo_disparo
        from painel.services.delivery import enviar_um, reservar_tentativa

        enviar_um(self.cliente)
        tentativa = Tentativa.objects.get(destinatario=self.destinatario)

        self.assertIsNone(ultimo_disparo(self.destinatario.telefone))
        self.assertIsNone(reservar_tentativa(self.destinatario.pk))
        self.assertEqual(Tentativa.objects.filter(destinatario=self.destinatario).count(), 1)
        self.assertEqual(tentativa.estado, 'aceita')

    def test_sessao_desconectada_pausa_proxima_tentativa(self):
        from painel.services.delivery import enviar_um

        self.cliente.estado = {'status': 'qr_ready'}

        self.assertFalse(enviar_um(self.cliente))

        self.campanha.refresh_from_db()
        self.assertEqual(self.campanha.estado, 'pausada')
        self.assertIn('desconect', self.campanha.motivo_pausa.casefold())
        self.assertFalse(any(chamada[0] == 'send' for chamada in self.cliente.chamadas))

    def test_sessao_configurada_diferente_da_campanha_nao_envia(self):
        from painel.services.delivery import enviar_um

        self.cliente.session_id = 'outra-sessao'

        self.assertFalse(enviar_um(self.cliente))
        self.campanha.refresh_from_db()
        self.assertEqual(self.campanha.estado, 'pausada')
        self.assertFalse(any(chamada[0] in ('status', 'send') for chamada in self.cliente.chamadas))

    def test_falha_http_4xx_e_incerto_timeout_nao_geram_reenvio(self):
        from painel.models import Campanha, Destinatario, Tentativa
        from painel.services.delivery import enviar_um

        self.cliente.envio = OpenWAError('Recusado.', 400)
        self.assertTrue(enviar_um(self.cliente))
        falha = Tentativa.objects.get(destinatario=self.destinatario)
        self.assertEqual(falha.estado, 'falha')

        # Uma tentativa incerta usa outro destinatário/campanha e não é enviada de novo automaticamente.
        self.campanha2 = Campanha.objects.create(
            nome='Segunda', texto_snapshot='Oi', limite=1, session_id='sessao-teste', estado='ativa'
        )
        empresa2 = type(self.empresa).objects.create(
            nome='Empresa incerta', telefone='46992220028', maps_id='maps:incerta'
        )
        destinatario2 = Destinatario.objects.create(
            campanha=self.campanha2, empresa=empresa2, ordem=1, telefone='5546992220028',
            mensagem='Oi Empresa incerta', mensagem_hash=sha256(b'Oi Empresa incerta').hexdigest(),
        )
        self.cliente.envio = OpenWAError('Conexão interrompida.')
        self.assertTrue(enviar_um(self.cliente))
        incerta = Tentativa.objects.get(destinatario=destinatario2)
        self.assertEqual(incerta.estado, 'incerto')
        self.assertFalse(enviar_um(self.cliente))
        self.assertEqual(Tentativa.objects.filter(destinatario=destinatario2).count(), 1)

    def test_recibo_entregue_confirma_envio_e_ack_read_promove_sem_regressao(self):
        from painel.models import Tentativa
        from painel.services.campaigns import ultimo_disparo
        from painel.services.delivery import atualizar_recibos, enviar_um

        enviar_um(self.cliente)
        tentativa = Tentativa.objects.get(destinatario=self.destinatario)
        self.cliente.recibos = {'messages': [{
            'waMessageId': 'wamid-1', 'ack': 2, 'status': 'delivered',
            'timestamp': int(timezone.now().timestamp()),
        }]}

        self.assertEqual(atualizar_recibos(self.cliente), 1)

        tentativa.refresh_from_db()
        self.assertEqual(tentativa.estado, 'entregue')
        primeiro = tentativa.confirmado_em
        self.assertIsNotNone(primeiro)
        self.assertIsNotNone(ultimo_disparo(self.destinatario.telefone))
        self.cliente.recibos = {'items': [{'messageId': 'wamid-1', 'ack': 1, 'status': 'sent'}]}
        self.assertEqual(atualizar_recibos(self.cliente), 0)
        tentativa.refresh_from_db()
        self.assertEqual(tentativa.estado, 'entregue')

    def test_ack_um_status_sent_sem_recibo_nao_confirma(self):
        from painel.models import Tentativa
        from painel.services.delivery import atualizar_recibos, enviar_um

        enviar_um(self.cliente)
        tentativa = Tentativa.objects.get(destinatario=self.destinatario)
        self.cliente.recibos = {'data': [{'id': 'wamid-1', 'ack': 1, 'status': 'sent'}]}

        self.assertEqual(atualizar_recibos(self.cliente), 0)

        tentativa.refresh_from_db()
        self.assertEqual(tentativa.estado, 'aceita')
        self.assertIsNone(tentativa.confirmado_em)

    def test_status_forte_sem_ack_confirma_entrega_e_leitura(self):
        from painel.models import Tentativa
        from painel.services.delivery import atualizar_recibos, enviar_um

        enviar_um(self.cliente)
        tentativa = Tentativa.objects.get(destinatario=self.destinatario)
        self.cliente.recibos = {'messages': [{'id': 'wamid-1', 'status': 'delivered'}]}
        self.assertEqual(atualizar_recibos(self.cliente), 1)
        tentativa.refresh_from_db()
        self.assertEqual(tentativa.estado, 'entregue')
        self.assertIsNotNone(tentativa.confirmado_em)

        tentativa.ultima_verificacao = timezone.now() - timedelta(seconds=11)
        tentativa.save(update_fields=['ultima_verificacao'])
        self.cliente.recibos = {'messages': [{'id': 'wamid-1', 'status': 'read'}]}
        self.assertEqual(atualizar_recibos(self.cliente), 1)
        tentativa.refresh_from_db()
        self.assertEqual(tentativa.estado, 'lido')

    def test_status_sent_sem_ack_permanece_aceita(self):
        from painel.models import Tentativa
        from painel.services.delivery import atualizar_recibos, enviar_um

        enviar_um(self.cliente)
        tentativa = Tentativa.objects.get(destinatario=self.destinatario)
        self.cliente.recibos = {'messages': [{'id': 'wamid-1', 'status': 'sent'}]}
        self.assertEqual(atualizar_recibos(self.cliente), 0)
        tentativa.refresh_from_db()
        self.assertEqual(tentativa.estado, 'aceita')
        self.assertIsNone(tentativa.confirmado_em)

    def test_campanha_mais_antiga_mantem_fila_durante_intervalo(self):
        from datetime import timedelta
        from painel.models import Campanha, Destinatario, Empresa
        from painel.services.delivery import enviar_um

        self.campanha.intervalo = 60
        self.campanha.proximo_envio = timezone.now() + timedelta(seconds=60)
        self.campanha.save(update_fields=['intervalo', 'proximo_envio'])
        empresa2 = Empresa.objects.create(nome='Posterior', telefone='46992220028', maps_id='maps:posterior')
        posterior = Campanha.objects.create(
            nome='Posterior', texto_snapshot='Oi', limite=1, session_id='sessao-teste',
            intervalo=0, estado='ativa', criado_em=timezone.now() + timedelta(seconds=1),
        )
        Destinatario.objects.create(
            campanha=posterior, empresa=empresa2, ordem=1, telefone='5546992220028',
            mensagem='Oi Posterior', mensagem_hash=sha256(b'Oi Posterior').hexdigest(),
        )

        self.assertFalse(enviar_um(self.cliente))
        self.assertFalse(any(chamada[0] == 'send' for chamada in self.cliente.chamadas))

    def test_ultimo_envio_conclui_mas_intervalo_ainda_bloqueia_proxima_campanha(self):
        from datetime import timedelta
        from painel.models import Campanha, Destinatario, Empresa
        from painel.services.delivery import enviar_um

        self.campanha.intervalo = 60
        self.campanha.save(update_fields=['intervalo'])
        empresa2 = Empresa.objects.create(nome='Posterior final', maps_id='maps:posterior-final')
        posterior = Campanha.objects.create(
            nome='Posterior final', texto_snapshot='Oi', limite=1, session_id='sessao-teste',
            intervalo=0, estado='ativa', criado_em=timezone.now() + timedelta(seconds=1),
        )
        Destinatario.objects.create(
            campanha=posterior, empresa=empresa2, ordem=1, telefone='5546992220028',
            mensagem='Oi Posterior', mensagem_hash=sha256(b'Oi Posterior').hexdigest(),
        )

        self.assertTrue(enviar_um(self.cliente))
        self.campanha.refresh_from_db()
        self.assertEqual(self.campanha.estado, 'concluida')
        self.assertGreater(self.campanha.proximo_envio, timezone.now())
        self.cliente.chamadas.clear()
        self.assertFalse(enviar_um(self.cliente))
        self.assertFalse(any(chamada[0] == 'send' for chamada in self.cliente.chamadas))

    def test_pausar_por_desconexao_preserva_cooldown(self):
        from datetime import timedelta
        from painel.services.delivery import enviar_um

        limite = timezone.now() - timedelta(seconds=1)
        self.campanha.proximo_envio = limite
        self.campanha.save(update_fields=['proximo_envio'])
        self.cliente.estado = {'status': 'qr_ready'}
        self.assertFalse(enviar_um(self.cliente))
        self.campanha.refresh_from_db()
        self.assertEqual(self.campanha.estado, 'pausada')
        self.assertEqual(self.campanha.proximo_envio, limite)

    def test_recebimento_read_promove_idempotente(self):
        from painel.models import Evento, Tentativa
        from painel.services.delivery import atualizar_recibos, enviar_um

        enviar_um(self.cliente)
        tentativa = Tentativa.objects.get(destinatario=self.destinatario)
        agora = int(timezone.now().timestamp())
        self.cliente.recibos = {'items': [{
            'messageId': 'wamid-1', 'status': 'read', 'ack': 3, 'timestamp': agora,
        }]}

        self.assertEqual(atualizar_recibos(self.cliente), 1)
        self.assertEqual(atualizar_recibos(self.cliente), 0)

        tentativa.refresh_from_db()
        self.assertEqual(tentativa.estado, 'lido')
        self.assertEqual(Evento.objects.filter(tentativa=tentativa).count(), 3)

    def test_pausa_ou_desconexao_durante_preflight_nao_faz_post(self):
        from painel.services.delivery import enviar_um

        def pausar_ao_verificar(_telefone):
            self.campanha.estado = 'pausada'
            self.campanha.save(update_fields=['estado'])
            self.cliente.estado = {'status': 'qr_ready'}
            return self.cliente.contato

        self.cliente.verificar_numero = pausar_ao_verificar

        self.assertFalse(enviar_um(self.cliente))

        self.assertFalse(any(chamada[0] == 'send' for chamada in self.cliente.chamadas))

    def test_reserva_cli_preexistente_bloqueia_worker_e_worker_bloqueia_cli(self):
        from painel.models import Reserva
        from painel.services.delivery import enviar_um
        from django.db import connection

        Reserva.objects.create(
            empresa_id=self.empresa.pk, session_id='sessao-teste', telefone=self.destinatario.telefone,
            mensagem=self.destinatario.mensagem, mensagem_hash=self.destinatario.mensagem_hash,
            status='processando',
        )
        self.assertFalse(enviar_um(self.cliente))
        self.assertFalse(any(chamada[0] == 'send' for chamada in self.cliente.chamadas))

        with connection.cursor() as cursor:
            cursor.execute(
                'INSERT OR IGNORE INTO envios_whatsapp '
                '(empresa_id, session_id, telefone, mensagem, mensagem_hash, status) '
                'VALUES (%s, %s, %s, %s, %s, %s)',
                [self.empresa.pk, 'sessao-teste', self.destinatario.telefone,
                 self.destinatario.mensagem, self.destinatario.mensagem_hash, 'processando'],
            )
            self.assertEqual(cursor.rowcount, 0)

    def test_receipt_sem_prova_e_timeout_nao_confirmam_nem_regredem(self):
        from painel.models import Tentativa
        from painel.services.delivery import atualizar_recibos, enviar_um

        enviar_um(self.cliente)
        tentativa = Tentativa.objects.get(destinatario=self.destinatario)
        self.cliente.recibos = OpenWAError('Serviço temporariamente indisponível.')

        self.assertEqual(atualizar_recibos(self.cliente), 0)

        tentativa.refresh_from_db()
        self.assertEqual(tentativa.estado, 'aceita')
        self.assertIsNone(tentativa.confirmado_em)

    def test_recibo_negativo_explicito_falha_sem_regredir_entrega(self):
        from painel.models import Tentativa
        from painel.services.delivery import atualizar_recibos, enviar_um

        enviar_um(self.cliente)
        tentativa = Tentativa.objects.get(destinatario=self.destinatario)
        self.cliente.recibos = {'messages': [{'id': 'wamid-1', 'status': 'failed', 'ack': -1}]}
        self.assertEqual(atualizar_recibos(self.cliente), 1)
        tentativa.refresh_from_db()
        self.assertEqual(tentativa.estado, 'falha')

    def test_preflight_invalido_pausa_sem_reservar_ou_postar(self):
        from painel.models import Reserva, Tentativa
        from painel.services.delivery import enviar_um

        self.cliente.contato = {'exists': True, 'whatsappId': None}
        self.assertFalse(enviar_um(self.cliente))
        self.campanha.refresh_from_db()
        self.assertEqual(self.campanha.estado, 'pausada')
        self.assertEqual(Reserva.objects.count(), 0)
        self.assertEqual(Tentativa.objects.count(), 0)
        self.assertFalse(any(item[0] == 'send' for item in self.cliente.chamadas))

    def test_reserva_explicita_reenvio_consumida_uma_vez_preserva_linhas(self):
        from painel.models import AutorizacaoReenvio, Reserva, Tentativa
        from painel.services.delivery import autorizar_reenvio, reservar_tentativa

        # O primeiro envio já tem reserva permanente e falha definitiva.
        self.cliente.envio = OpenWAError('Recusado.', 400)
        from painel.services.delivery import enviar_um
        enviar_um(self.cliente)
        anterior = Tentativa.objects.get(destinatario=self.destinatario)
        campanha_nova = autorizar_reenvio(anterior.pk, 'Solicitação explícita do operador')
        destinatario_novo = campanha_nova.destinatarios.get()
        autorizacao = AutorizacaoReenvio.objects.get(destinatario=destinatario_novo)
        from painel.services.campaigns import iniciar_campanha
        iniciar_campanha(campanha_nova.pk)

        novo = reservar_tentativa(destinatario_novo.pk, autorizacao.pk)

        self.assertIsNotNone(novo)
        self.assertEqual(novo.reserva_id, anterior.reserva_id)
        self.assertIsNotNone(AutorizacaoReenvio.objects.get(pk=autorizacao.pk).consumida_em)
        self.assertIsNone(reservar_tentativa(destinatario_novo.pk, autorizacao.pk))
        self.assertEqual(Reserva.objects.count(), 1)
        self.assertEqual(Tentativa.objects.count(), 2)

    def test_autorizacao_de_outro_destinatario_ou_sessao_nao_e_consumida(self):
        from painel.models import AutorizacaoReenvio, Campanha, Destinatario, Reserva, Tentativa
        from painel.services.delivery import autorizar_reenvio, reservar_tentativa

        reserva = Reserva.objects.create(
            empresa_id=self.empresa.pk, session_id='sessao-teste', telefone=self.destinatario.telefone,
            mensagem=self.destinatario.mensagem, mensagem_hash=self.destinatario.mensagem_hash,
            status='falha',
        )
        anterior = Tentativa.objects.create(
            destinatario=self.destinatario, reserva=reserva, telefone=self.destinatario.telefone,
            session_id='sessao-teste', mensagem=self.destinatario.mensagem,
            mensagem_hash=self.destinatario.mensagem_hash, estado='falha',
        )
        campanha_nova = autorizar_reenvio(anterior.pk, 'Solicitação explícita')
        alvo = campanha_nova.destinatarios.get()
        autorizacao = AutorizacaoReenvio.objects.get(destinatario=alvo)
        empresa2 = type(self.empresa).objects.create(nome='Outra', maps_id='maps:outra')
        outra = Destinatario.objects.create(
            campanha=Campanha.objects.create(nome='Outra', texto_snapshot='Oi', limite=1, session_id='sessao-teste'),
            empresa=empresa2, ordem=1, telefone=alvo.telefone, mensagem=alvo.mensagem,
            mensagem_hash=alvo.mensagem_hash,
        )

        self.assertIsNone(reservar_tentativa(outra.pk, autorizacao.pk))
        campanha_nova.session_id = 'outra-sessao'
        campanha_nova.save(update_fields=['session_id'])
        self.assertIsNone(reservar_tentativa(alvo.pk, autorizacao.pk))
        autorizacao.refresh_from_db()
        self.assertIsNone(autorizacao.consumida_em)


class DeliveryConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        # envios_whatsapp é managed=False e o flush do Django não a limpa.
        from painel.models import Reserva
        Reserva.objects.all().delete()

    def test_duas_conexoes_temporarias_nao_reservam_a_mesma_identidade_duas_vezes(self):
        from painel.models import Campanha, Destinatario, Empresa, Tentativa
        from painel.services.delivery import reservar_tentativa

        empresa = Empresa.objects.create(nome='Concorrência', maps_id='maps:concorrencia')
        campanha = Campanha.objects.create(nome='Concorrente', texto_snapshot='Oi', limite=1, session_id='s', estado='ativa')
        destinatario = Destinatario.objects.create(
            campanha=campanha, empresa=empresa, ordem=1, telefone='5546998880012',
            mensagem='Oi', mensagem_hash=sha256(b'Oi').hexdigest(),
        )
        barreira = __import__('threading').Barrier(2)

        def reservar():
            close_old_connections()
            barreira.wait(timeout=5)
            try:
                return reservar_tentativa(destinatario.pk)
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            resultados = list(pool.map(lambda _: reservar(), range(2)))

        self.assertEqual(sum(resultado is not None for resultado in resultados), 1)
        self.assertEqual(Tentativa.objects.filter(destinatario=destinatario).count(), 1)

    def test_commit_da_reserva_e_tentativa_visivel_em_conexao_independente_no_post(self):
        from painel.models import Campanha, Destinatario, Empresa, Reserva, Tentativa
        from painel.services.delivery import enviar_um

        empresa = Empresa.objects.create(nome='Commit', maps_id='maps:commit')
        campanha = Campanha.objects.create(nome='Commit', texto_snapshot='Oi', limite=1,
                                           session_id='sessao-teste', estado='ativa')
        destinatario = Destinatario.objects.create(
            campanha=campanha, empresa=empresa, ordem=1, telefone='5546991110028',
            mensagem='Oi', mensagem_hash=sha256(b'Oi').hexdigest(),
        )
        cliente = FakeOpenWA()
        observada = []

        def consultar_outra_conexao():
            close_old_connections()
            try:
                observada.append((
                    Tentativa.objects.filter(destinatario=destinatario, estado='enviando').count(),
                    Reserva.objects.filter(session_id='sessao-teste').count(),
                ))
            finally:
                close_old_connections()

        cliente.durante_post = consultar_outra_conexao
        self.assertTrue(enviar_um(cliente))
        self.assertEqual(observada, [(1, 1)])

    def test_nao_autoriza_reenvio_de_tentativa_incerta(self):
        from painel.models import Campanha, Destinatario, Empresa, Reserva, Tentativa
        from painel.services.delivery import autorizar_reenvio

        empresa = Empresa.objects.create(nome='Incerta', maps_id='maps:incerta-auth')
        campanha = Campanha.objects.create(nome='Anterior', texto_snapshot='Oi', limite=1, session_id='sessao-teste')
        destinatario = Destinatario.objects.create(
            campanha=campanha, empresa=empresa, ordem=1, telefone='5546993330028',
            mensagem='Oi', mensagem_hash=sha256(b'Oi').hexdigest(),
        )
        reserva = Reserva.objects.create(
            empresa_id=empresa.pk, session_id='sessao-teste', telefone=destinatario.telefone,
            mensagem=destinatario.mensagem, mensagem_hash=destinatario.mensagem_hash,
            status='incerto',
        )
        tentativa = Tentativa.objects.create(
            destinatario=destinatario, reserva=reserva, telefone=destinatario.telefone,
            estado='incerto',
        )

        with self.assertRaises(ValueError):
            autorizar_reenvio(tentativa.pk, 'Motivo explícito')

    def test_duas_autorizacoes_nao_adquirem_lease_simultanea_e_recibo_antigo_nao_libera(self):
        from painel.models import (AutorizacaoReenvio, Campanha, Destinatario, Empresa,
                                   Reserva, Tentativa)
        from painel.services.delivery import autorizar_reenvio, atualizar_recibos, reservar_tentativa
        from painel.services.campaigns import iniciar_campanha

        empresa = Empresa.objects.create(nome='Lease', maps_id='maps:lease')
        origem = Campanha.objects.create(nome='Base', texto_snapshot='Oi', limite=1, session_id='s')
        d0 = Destinatario.objects.create(campanha=origem, empresa=empresa, ordem=1,
            telefone='5546991110028', mensagem='Oi', mensagem_hash=sha256(b'Oi').hexdigest())
        reserva = Reserva.objects.create(empresa_id=empresa.pk, session_id='s', telefone=d0.telefone,
            mensagem='Oi', mensagem_hash=sha256(b'Oi').hexdigest(), status='falha', message_id='old')
        anterior = Tentativa.objects.create(destinatario=d0, reserva=reserva, estado='falha',
            telefone=d0.telefone, session_id='s', mensagem='Oi', mensagem_hash=sha256(b'Oi').hexdigest(),
            message_id='old', chat_id='chat')
        primeira = autorizar_reenvio(anterior.pk, 'Operador autorizou reenvio 1')
        segunda = autorizar_reenvio(anterior.pk, 'Operador autorizou reenvio 2')
        d1, d2 = primeira.destinatarios.get(), segunda.destinatarios.get()
        a1, a2 = AutorizacaoReenvio.objects.get(destinatario=d1), AutorizacaoReenvio.objects.get(destinatario=d2)
        iniciar_campanha(primeira.pk)
        iniciar_campanha(segunda.pk)

        nova = reservar_tentativa(d1.pk, a1.pk)
        self.assertIsNotNone(nova)
        self.assertIsNone(reservar_tentativa(d2.pk, a2.pk))
        a2.refresh_from_db()
        self.assertIsNone(a2.consumida_em)
        reserva.refresh_from_db()
        self.assertEqual(reserva.status, 'processando')
        self.assertIsNone(reserva.message_id)

        anterior.estado = 'aceita'
        anterior.save(update_fields=['estado'])
        cliente = FakeOpenWA()
        cliente.session_id = 's'
        cliente.recibos = {'messages': [{'id': 'old', 'status': 'delivered'}]}
        self.assertEqual(atualizar_recibos(cliente), 1)
        reserva.refresh_from_db()
        self.assertEqual(reserva.status, 'processando')
