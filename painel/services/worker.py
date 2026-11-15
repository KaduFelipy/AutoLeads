"""Ciclo serial do processo local para pesquisas, campanhas e recibos."""

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager
from pathlib import Path

from django.conf import settings
from django.db import close_old_connections, transaction
from django.db.models import F
from django.utils import timezone

from painel.models import Campanha, Pesquisa
from painel.services.contacts import registrar_resultado
from painel.services.delivery import atualizar_recibos, enviar_um, recuperar_interrompidos


WORKER_LOCK = Path(settings.BASE_DIR) / '.worker.lock'
INTERVALO_CICLO = 2


class WorkerAlreadyRunning(RuntimeError):
    pass


class ErroPersistenciaPesquisa(RuntimeError):
    """Falha local ao persistir um resultado capturado, sem dados da empresa."""


def _persistir_captura(pesquisa_id, dados, url, termo):
    """Grava uma captura fora da thread Playwright e fecha sua conexão por item."""
    close_old_connections()
    try:
        pesquisa = Pesquisa.objects.get(pk=pesquisa_id)
        with transaction.atomic():
            registrar_resultado(pesquisa, dados, url, termo)
            Pesquisa.objects.filter(pk=pesquisa_id).update(processadas=F('processadas') + 1)
    except Exception as erro:
        raise ErroPersistenciaPesquisa('Falha ao persistir resultado da pesquisa.') from erro
    finally:
        close_old_connections()


def _causa_persistencia(erro):
    vistos = set()
    atual = erro
    while atual is not None and id(atual) not in vistos:
        if isinstance(atual, ErroPersistenciaPesquisa):
            return True
        vistos.add(id(atual))
        atual = atual.__cause__ or atual.__context__
    return False


class WorkerLock(AbstractContextManager):
    def __init__(self, path=None):
        self.path = Path(path if path is not None else WORKER_LOCK)
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open('a+b')
        self.handle.seek(0, os.SEEK_END)
        if self.handle.tell() == 0:
            self.handle.write(b' ')
            self.handle.flush()
        try:
            self.handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as erro:
            self.handle.close()
            self.handle = None
            raise WorkerAlreadyRunning('Já existe um trabalhador ativo para este projeto.') from erro
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(json.dumps({'pid': os.getpid(), 'iniciado_em': timezone.now().isoformat()}).encode('utf-8'))
        self.handle.flush()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self.handle is not None:
            try:
                self.handle.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            finally:
                self.handle.close()
                self.handle = None
        return False


def marcar_pesquisas_interrompidas():
    agora = timezone.now()
    return Pesquisa.objects.filter(estado=Pesquisa.Estado.PROCESSANDO).update(
        estado=Pesquisa.Estado.FALHA,
        fim=agora,
        erro='A pesquisa foi interrompida antes de terminar. Crie uma nova execução para tentar novamente.',
    )


def processar_pesquisa(pesquisa_id, consultor=None):
    """Executa uma pesquisa pendente; consultor é um ponto de teste sem navegação real."""
    with transaction.atomic():
        pesquisa = Pesquisa.objects.select_for_update().get(pk=pesquisa_id)
        if pesquisa.estado != Pesquisa.Estado.PENDENTE:
            return False
        pesquisa.estado = Pesquisa.Estado.PROCESSANDO
        pesquisa.inicio = timezone.now()
        pesquisa.erro = ''
        pesquisa.save(update_fields=('estado', 'inicio', 'erro'))

    termos = [linha.strip() for linha in pesquisa.termos.splitlines() if linha.strip()]

    try:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix='painel-pesquisa-db') as persistidor:
            def ao_capturar(dados, url, termo):
                # Esperar cada item preserva a ordem e o progresso incremental;
                # nenhuma transação permanece aberta durante Playwright/navegação.
                persistidor.submit(
                    _persistir_captura, pesquisa.pk, dados, url, termo
                ).result()

            if consultor is not None:
                consultor(termos, pesquisa.limite, ao_capturar)
            else:
                from playwright.sync_api import sync_playwright
                from test_scrapy import consultar_google_maps

                with sync_playwright() as playwright:
                    navegador = playwright.chromium.launch(headless=True)
                    try:
                        pagina = navegador.new_page(locale='pt-BR')
                        consultar_google_maps(
                            pagina, termos=termos, limite=pesquisa.limite,
                            ao_capturar=ao_capturar,
                        )
                    finally:
                        navegador.close()
    except Exception as erro:
        mensagem = (
            'Não foi possível salvar um resultado no banco local; a pesquisa foi interrompida.'
            if _causa_persistencia(erro) else
            'Não foi possível concluir a pesquisa. Crie uma nova execução para tentar novamente.'
        )
        Pesquisa.objects.filter(pk=pesquisa.pk, estado=Pesquisa.Estado.PROCESSANDO).update(
            estado=Pesquisa.Estado.FALHA, fim=timezone.now(),
            erro=mensagem,
        )
        return False
    Pesquisa.objects.filter(pk=pesquisa.pk, estado=Pesquisa.Estado.PROCESSANDO).update(
        estado=Pesquisa.Estado.CONCLUIDA, fim=timezone.now(), erro=''
    )
    return True


def _pausar_sem_cliente():
    Campanha.objects.filter(estado=Campanha.Estado.ATIVA).update(
        estado=Campanha.Estado.PAUSADA,
        motivo_pausa='Cliente OpenWA não configurado ou indisponível; retome manualmente após reconectar.',
    )


def executar_ciclo(cliente=None):
    pesquisa = Pesquisa.objects.filter(estado=Pesquisa.Estado.PENDENTE).order_by('criada_em', 'pk').first()
    pesquisa_processada = processar_pesquisa(pesquisa.pk) if pesquisa else False
    recibos = atualizar_recibos(cliente) if cliente is not None else 0
    if cliente is None:
        _pausar_sem_cliente()
        enviado = False
    else:
        enviado = enviar_um(cliente)
    return {'pesquisa_processada': pesquisa_processada, 'recibos_atualizados': recibos,
            'enviado': enviado}


def executar_trabalhador(cliente_factory, parar, intervalo=INTERVALO_CICLO):
    """Mantém o lock por toda a vida e recupera somente trabalho próprio."""
    with WorkerLock():
        recuperar_interrompidos()
        marcar_pesquisas_interrompidas()
        while not parar():
            try:
                cliente = cliente_factory()
            except (OSError, RuntimeError, ValueError):
                cliente = None
            executar_ciclo(cliente)
            if parar():
                break
            time.sleep(intervalo)


def executar_uma_vez(cliente_factory):
    """Executa um ciclo isolado sob o mesmo lock do trabalhador contínuo."""
    with WorkerLock():
        recuperar_interrompidos()
        marcar_pesquisas_interrompidas()
        try:
            cliente = cliente_factory()
        except (OSError, RuntimeError, ValueError):
            cliente = None
        return executar_ciclo(cliente)
