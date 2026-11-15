import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.db import close_old_connections
from django.test import TestCase, TransactionTestCase
from django.utils import timezone


class WorkerTests(TestCase):
    def test_comando_trabalhador_once_usa_cliente_local_sem_bloquear_a_thread(self):
        # O cliente é uma implementação falsa; o teste não chama o OpenWA.
        cliente = type('ClienteFalso', (), {'session_id': 'teste'})()
        with tempfile.TemporaryDirectory() as pasta:
            with patch('painel.services.worker.WORKER_LOCK', Path(pasta) / 'worker.lock'):
                with patch('painel.services.integration.cliente_local', return_value=cliente):
                    with patch('painel.services.worker.executar_ciclo', return_value={'enviado': False}) as ciclo:
                        call_command('trabalhador', '--once', verbosity=0)
        ciclo.assert_called_once_with(cliente)

    def test_lock_impede_segundo_trabalhador_e_libera_apos_contexto(self):
        from painel.services.worker import WorkerAlreadyRunning, WorkerLock

        with tempfile.TemporaryDirectory() as pasta:
            caminho = Path(pasta) / 'worker.lock'
            with WorkerLock(caminho):
                with self.assertRaises(WorkerAlreadyRunning):
                    with WorkerLock(caminho):
                        pass
            with WorkerLock(caminho):
                self.assertTrue(True)

    def test_recuperacao_altera_somente_tentativas_proprias_enviando(self):
        from painel.models import Campanha, Destinatario, Empresa, Reserva, Tentativa
        from painel.services.worker import recuperar_interrompidos

        empresa = Empresa.objects.create(nome='Interrompida', maps_id='maps:worker')
        campanha = Campanha.objects.create(nome='Worker', texto_snapshot='Oi', limite=2, session_id='s')
        destinatario = Destinatario.objects.create(
            campanha=campanha, empresa=empresa, ordem=1, telefone='5546991110028',
            mensagem='Oi', mensagem_hash='c' * 64,
        )
        reserva = Reserva.objects.create(
            empresa_id=empresa.pk, session_id='s', telefone='5546991110028',
            mensagem='Oi', mensagem_hash='c' * 64, status='processando'
        )
        tentativa = Tentativa.objects.create(
            destinatario=destinatario, reserva=reserva, estado='enviando', telefone='5546991110028'
        )
        cli_reserva = Reserva.objects.create(
            empresa_id=empresa.pk, session_id='cli', telefone='5546992220028',
            mensagem='Outro', mensagem_hash='d' * 64, status='processando'
        )

        self.assertEqual(recuperar_interrompidos(), 1)

        tentativa.refresh_from_db()
        cli_reserva.refresh_from_db()
        self.assertEqual(tentativa.estado, 'incerto')
        self.assertEqual(cli_reserva.status, 'processando')

    def test_ciclo_desconectado_pausa_campanha_sem_post(self):
        from painel.models import Campanha, Destinatario, Empresa
        from painel.services.worker import executar_ciclo

        empresa = Empresa.objects.create(nome='Pausa', telefone='46991110028', maps_id='maps:pause')
        campanha = Campanha.objects.create(
            nome='Worker', texto_snapshot='Oi', limite=1, session_id='s', estado='ativa'
        )
        Destinatario.objects.create(
            campanha=campanha, empresa=empresa, ordem=1, telefone='5546991110028',
            mensagem='Oi', mensagem_hash='e' * 64,
        )
        cliente = type('Cliente', (), {
            'session_id': 's', 'status': lambda self: {'status': 'qr_ready'},
            'chamadas': [],
        })()

        with patch('painel.services.worker.atualizar_recibos', return_value=0):
            resultado = executar_ciclo(cliente)

        campanha.refresh_from_db()
        self.assertEqual(campanha.estado, 'pausada')
        self.assertFalse(resultado.get('enviado', False))

    def test_sem_cliente_pausa_preservando_cooldown_global(self):
        from datetime import timedelta
        from painel.models import Campanha
        from painel.services.worker import executar_ciclo

        limite = timezone.now() + timedelta(seconds=30)
        campanha = Campanha.objects.create(
            nome='Cooldown desconectado', texto_snapshot='Oi', limite=1,
            session_id='s', estado='ativa', proximo_envio=limite,
        )
        executar_ciclo(None)
        campanha.refresh_from_db()
        self.assertEqual(campanha.estado, 'pausada')
        self.assertEqual(campanha.proximo_envio, limite)


class MecanicaWorkerTests(TransactionTestCase):
    def test_recaptura_falsa_classifica_nova_existente_preserva_supressao_e_progresso(self):
        from painel.models import Empresa, Perfil, Pesquisa, RegraTag, ResultadoPesquisa, Tag
        from painel.services.worker import processar_pesquisa

        ramo, _ = Tag.objects.get_or_create(nome='Mecânica', tipo='ramo')
        RegraTag.objects.filter(termo__iexact='mecanica').delete()
        RegraTag.objects.filter(termo__iexact='mecânica').delete()
        regra = RegraTag.objects.create(termo='mecanica', prioridade=1)
        regra.tags.add(ramo)
        antiga = Empresa.objects.create(nome='Existente', maps_id='maps:mecanica-existing')
        perfil = Perfil.objects.create(empresa=antiga, tags_suprimidas=[ramo.pk])
        pesquisa = Pesquisa.objects.create(termos='Mecanica Pato Branco')

        def consulta(termos, limite, ao_capturar):
            ao_capturar({'maps_id': antiga.maps_id, 'nome': antiga.nome}, 'https://maps.test/existing', termos[0])
            ao_capturar({'maps_id': 'maps:mecanica-new', 'nome': 'Nova'}, 'https://maps.test/new', termos[0])

        self.assertTrue(processar_pesquisa(pesquisa.pk, consultor=consulta))
        pesquisa.refresh_from_db()
        perfil.refresh_from_db()
        nova = Empresa.objects.get(maps_id='maps:mecanica-new')
        self.assertEqual(pesquisa.processadas, 2)
        self.assertEqual(pesquisa.resultados.count(), 2)
        self.assertEqual(ResultadoPesquisa.objects.filter(pesquisa=pesquisa).count(), 2)
        self.assertEqual(Empresa.objects.filter(maps_id__startswith='maps:mecanica-').count(), 2)
        self.assertFalse(perfil.tags.filter(pk=ramo.pk).exists())
        self.assertTrue(nova.perfil.tags.filter(pk=ramo.pk).exists())


class PesquisaPlaywrightPersistenceTests(TransactionTestCase):
    reset_sequences = True

    def test_callback_playwright_persiste_origens_e_progresso_no_limite(self):
        from playwright.sync_api import sync_playwright
        from painel.models import Pesquisa, ResultadoPesquisa
        from painel.services.worker import processar_pesquisa

        pesquisa = Pesquisa.objects.create(
            termos='retífica local\ncafeteria local', limite=2,
        )
        linhas = [
            ('maps:local-1', 'Oficina Um'),
            ('maps:local-2', 'Café Dois'),
            ('maps:local-3', 'Ignorado Três'),
        ]

        def consultar_em_pagina_local(termos, limite, ao_capturar):
            self.assertEqual(limite, 2)
            html = ''.join(
                f'<article data-maps="{maps_id}" data-nome="{nome}"></article>'
                for maps_id, nome in linhas
            )
            with sync_playwright() as playwright:
                navegador = playwright.chromium.launch(headless=True)
                try:
                    pagina = navegador.new_page()
                    pagina.set_content(html)
                    capturadas = pagina.locator('article').evaluate_all(
                        "els => els.map(el => ({maps_id: el.dataset.maps, nome: el.dataset.nome}))"
                    )
                    with ThreadPoolExecutor(max_workers=1) as leitor_progresso:
                        for indice, item in enumerate(capturadas[:limite]):
                            termo = termos[indice % len(termos)]
                            ao_capturar(item, f"https://maps.google.test/place/{item['maps_id']}", termo)
                            if indice == 0:
                                def ler_progresso():
                                    close_old_connections()
                                    try:
                                        return Pesquisa.objects.get(pk=pesquisa.pk).processadas
                                    finally:
                                        close_old_connections()

                                self.assertEqual(leitor_progresso.submit(ler_progresso).result(), 1)
                finally:
                    navegador.close()

        self.assertTrue(processar_pesquisa(pesquisa.pk, consultor=consultar_em_pagina_local))
        pesquisa.refresh_from_db()
        self.assertEqual(pesquisa.estado, Pesquisa.Estado.CONCLUIDA)
        self.assertEqual(pesquisa.processadas, 2)
        self.assertEqual(
            list(ResultadoPesquisa.objects.filter(pesquisa=pesquisa).order_by('termo').values_list('termo', flat=True)),
            ['cafeteria local', 'retífica local'],
        )
        self.assertEqual(pesquisa.resultados.count(), 2)

    def test_falha_no_segundo_callback_preserva_primeiro_progresso_e_para_lote(self):
        from playwright.sync_api import sync_playwright
        from painel.models import Empresa, Pesquisa
        from painel.services.worker import processar_pesquisa

        pesquisa = Pesquisa.objects.create(termos='oficinas locais', limite=3)
        linhas = [
            ('maps:primeira', 'Primeira local'),
            ('maps:invalida', ''),
            ('maps:terceira', 'Terceira local'),
        ]

        def consultar_com_erro_local(termos, limite, ao_capturar):
            with sync_playwright() as playwright:
                navegador = playwright.chromium.launch(headless=True)
                try:
                    pagina = navegador.new_page()
                    pagina.set_content(''.join(
                        f'<article data-maps="{maps_id}" data-nome="{nome}"></article>'
                        for maps_id, nome in linhas
                    ))
                    capturadas = pagina.locator('article').evaluate_all(
                        "els => els.map(el => ({maps_id: el.dataset.maps, nome: el.dataset.nome}))"
                    )
                    for item in capturadas[:limite]:
                        ao_capturar(item, f"https://maps.google.test/place/{item['maps_id']}", termos[0])
                finally:
                    navegador.close()

        self.assertFalse(processar_pesquisa(pesquisa.pk, consultor=consultar_com_erro_local))
        pesquisa.refresh_from_db()
        self.assertEqual(pesquisa.estado, Pesquisa.Estado.FALHA)
        self.assertEqual(pesquisa.processadas, 1)
        self.assertEqual(pesquisa.resultados.count(), 1)
        self.assertEqual(Empresa.objects.get(maps_id='maps:primeira').nome, 'Primeira local')
        self.assertFalse(Empresa.objects.filter(maps_id='maps:terceira').exists())
        self.assertIn('banco local', pesquisa.erro)
        self.assertNotIn('Primeira local', pesquisa.erro)
