"""Valida, copia e aplica migrações ao SQLite configurado pelo painel."""

from datetime import datetime, timezone
from importlib import import_module
from contextlib import closing
from pathlib import Path
import os
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace

from django.conf import settings
from django.core.management import BaseCommand, CommandError, call_command
from django.db import connections
from painel.services.worker import WorkerLock


def backup_sqlite(origem, pasta_destino):
    """Cria snapshot consistente usando sqlite3 backup (inclui conteúdo WAL)."""
    origem = Path(origem).resolve()
    destino_dir = Path(pasta_destino).resolve()
    destino_dir.mkdir(parents=True, exist_ok=True)
    instante = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    destino = destino_dir / f'empresas-{instante}.sqlite3'
    with closing(sqlite3.connect(origem)) as fonte, closing(sqlite3.connect(destino)) as copia:
        fonte.backup(copia)
        copia.commit()
    return destino


def validar_esquema_atual(connection):
    validar_legado = import_module('painel.migrations.0001_initial').validar_legado
    validar_legado(None, SimpleNamespace(connection=connection))


def validar_migracoes_em_copia(copia, timeout=120):
    """Executa o migrate real em outro processo apontado ao snapshot temporário."""
    ambiente = {
        nome: os.environ[nome]
        for nome in ('PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'VIRTUAL_ENV', 'PYTHONPATH')
        if os.environ.get(nome)
    }
    ambiente.update({
        'DJANGO_SETTINGS_MODULE': 'config.settings',
        'DJANGO_SECRET_KEY': 'chave-ficticia-da-validacao-de-migracao',
        'PAINEL_DB': str(Path(copia).resolve()),
    })
    comando = [sys.executable, str(Path(settings.BASE_DIR) / 'manage.py'), 'migrate', '--noinput']
    try:
        resultado = subprocess.run(
            comando, cwd=str(settings.BASE_DIR), env=ambiente,
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as erro:
        raise CommandError('Não foi possível validar as migrações na cópia temporária.') from erro
    if resultado.returncode:
        raise CommandError(
            'A validação das migrações na cópia falhou; o banco de origem permaneceu intacto.'
        )


class Command(BaseCommand):
    help = 'Faz backup SQLite consistente, valida uma cópia e aplica migrações.'

    def add_arguments(self, parser):
        parser.add_argument('--database', default='default')
        parser.add_argument('--backup-dir', default=None)

    def handle(self, *args, **options):
        alias = options['database']
        if alias != 'default':
            raise CommandError('A preparação local suporta somente o banco padrão do painel.')

        # O lock cobre toda a janela entre snapshot, validação e migração. Assim
        # não há trabalhador ativo nem novo worker iniciando enquanto o schema muda.
        with WorkerLock():
            banco = connections[alias]
            nome = banco.settings_dict['NAME']
            backup_dir = options['backup_dir'] or (Path(settings.BASE_DIR) / 'backups')
            if nome == ':memory:':
                backup = None
            else:
                caminho = Path(nome).resolve()
                backup = backup_sqlite(caminho, backup_dir) if caminho.exists() else None

            validar_esquema_atual(banco)
            with tempfile.TemporaryDirectory(prefix='painel-migracao-') as pasta_temporaria:
                pasta_temporaria = Path(pasta_temporaria)
                copia = pasta_temporaria / 'validacao.sqlite3'
                if backup is not None:
                    backup_sqlite(backup, pasta_temporaria)
                    origem = next(pasta_temporaria.glob('*.sqlite3'))
                    origem.replace(copia)
                elif nome == ':memory:':
                    banco.ensure_connection()
                    with closing(sqlite3.connect(copia)) as destino:
                        banco.connection.backup(destino)
                        destino.commit()
                else:
                    with closing(sqlite3.connect(copia)):
                        pass
                validar_migracoes_em_copia(copia)

            call_command('migrate', database=alias, interactive=False,
                         verbosity=options['verbosity'])
        if backup:
            self.stdout.write(f'Backup SQLite criado: {backup}')
        else:
            self.stdout.write('Banco validado e migrações aplicadas; sem backup de arquivo preexistente.')
