"""Processo local serial para executar pesquisas, envios e recibos."""

import signal
from threading import Event

from django.core.management import BaseCommand, CommandError

from painel.services.integration import cliente_local
from painel.services.worker import (
    WorkerAlreadyRunning,
    executar_trabalhador,
    executar_uma_vez,
)


class Command(BaseCommand):
    help = 'Executa o trabalhador local de pesquisas e campanhas.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--once', action='store_true',
            help='Executa um único ciclo e encerra (útil para diagnóstico local).',
        )

    def handle(self, *args, **options):
        try:
            if options['once']:
                executar_uma_vez(cliente_local)
                self.stdout.write('Ciclo do trabalhador concluído.')
                return

            parar = Event()
            antigos = {}

            def solicitar_parada(signum, frame):
                parar.set()

            for sig in (signal.SIGINT, getattr(signal, 'SIGTERM', signal.SIGINT)):
                if sig not in antigos:
                    antigos[sig] = signal.getsignal(sig)
                    signal.signal(sig, solicitar_parada)
            try:
                executar_trabalhador(cliente_local, parar.is_set)
            finally:
                for sig, handler in antigos.items():
                    signal.signal(sig, handler)
        except WorkerAlreadyRunning as erro:
            raise CommandError(str(erro)) from erro
