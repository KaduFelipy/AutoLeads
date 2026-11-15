import sqlite3
import tempfile
from contextlib import closing, contextmanager
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connections
from django.test import SimpleTestCase


@contextmanager
def _conexao_temporaria(caminho):
    """Troca apenas o wrapper thread-local, sem fechar o SQLite in-memory do runner."""
    original = connections['default']
    configuracao = deepcopy(original.settings_dict)
    configuracao['NAME'] = str(caminho)
    temporaria = type(original)(configuracao, 'default')
    connections['default'] = temporaria
    try:
        yield temporaria
    finally:
        temporaria.close()
        connections['default'] = original


class PrepararBancoTests(SimpleTestCase):
    databases = {'default'}

    def setUp(self):
        super().setUp()
        self.pasta_teste = tempfile.TemporaryDirectory()
        self.addCleanup(self.pasta_teste.cleanup)
        self.lock_teste = Path(self.pasta_teste.name) / 'worker.lock'
        self.patch_lock = patch('painel.services.worker.WORKER_LOCK', self.lock_teste)
        self.patch_lock.start()
        self.addCleanup(self.patch_lock.stop)

    def _criar_legado(self, caminho):
        with closing(sqlite3.connect(caminho)) as db:
            db.execute('''CREATE TABLE empresas (
                id INTEGER PRIMARY KEY, nome TEXT NOT NULL, endereco TEXT, telefone TEXT,
                site TEXT, avaliacao REAL CHECK (avaliacao BETWEEN 0 AND 5),
                maps_id TEXT NOT NULL UNIQUE, chave TEXT UNIQUE)''')
            db.execute("INSERT INTO empresas (id, nome, maps_id) VALUES (17, 'Legado', 'maps:17')")
            db.execute('''CREATE TABLE envios_whatsapp (
                id INTEGER PRIMARY KEY, empresa_id INTEGER NOT NULL, session_id TEXT NOT NULL,
                telefone TEXT NOT NULL, mensagem TEXT NOT NULL, mensagem_hash TEXT NOT NULL,
                status TEXT NOT NULL, message_id TEXT, erro TEXT,
                criado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                atualizado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (session_id, telefone, mensagem_hash))''')
            db.execute("INSERT INTO envios_whatsapp (id, empresa_id, session_id, telefone, mensagem, mensagem_hash, status) VALUES (23,17,'s','5546','Oi','hash','enviado')")
            db.commit()

    def test_backup_sqlite_copia_dados_consistentemente(self):
        from painel.management.commands.preparar_banco import backup_sqlite

        with tempfile.TemporaryDirectory() as pasta:
            origem = Path(pasta) / 'origem.sqlite3'
            backup_dir = Path(pasta) / 'backups'
            self._criar_legado(origem)
            backup = backup_sqlite(origem, backup_dir)
            with closing(sqlite3.connect(backup)) as db:
                self.assertEqual(db.execute('SELECT id,nome FROM empresas').fetchall(), [(17, 'Legado')])
                self.assertEqual(db.execute('SELECT id,status FROM envios_whatsapp').fetchall(), [(23, 'enviado')])

    def test_comando_faz_backup_validado_e_migra_copia_sem_perder_ids(self):
        with tempfile.TemporaryDirectory() as pasta:
            db_path = Path(pasta) / 'legado.sqlite3'
            backup_dir = Path(pasta) / 'backups'
            self._criar_legado(db_path)
            with _conexao_temporaria(db_path):
                call_command('preparar_banco', backup_dir=str(backup_dir), verbosity=0)
                with closing(sqlite3.connect(db_path)) as db:
                    self.assertEqual(db.execute('SELECT id,nome FROM empresas').fetchall(), [(17, 'Legado')])
                    self.assertEqual(db.execute('SELECT id,status FROM envios_whatsapp').fetchall(), [(23, 'enviado')])
                self.assertEqual(len(list(backup_dir.glob('*.sqlite3'))), 1)

    def test_esquema_incompativel_falha_depois_do_backup_sem_alterar_origem(self):
        with tempfile.TemporaryDirectory() as pasta:
            db_path = Path(pasta) / 'incompativel.sqlite3'
            backup_dir = Path(pasta) / 'backups'
            with closing(sqlite3.connect(db_path)) as db:
                db.execute('CREATE TABLE empresas (id INTEGER PRIMARY KEY, nome TEXT)')
                db.execute("INSERT INTO empresas VALUES (17, 'Preservado')")
                db.commit()
            with _conexao_temporaria(db_path):
                with self.assertRaisesRegex(RuntimeError, 'incompatível'):
                    call_command('preparar_banco', backup_dir=str(backup_dir), verbosity=0)
                with closing(sqlite3.connect(db_path)) as db:
                    self.assertEqual(db.execute('SELECT * FROM empresas').fetchall(), [(17, 'Preservado')])
                    self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [('empresas',)])
                self.assertEqual(len(list(backup_dir.glob('*.sqlite3'))), 1)

    def test_migracao_e_validada_em_copia_antes_de_tocar_origem(self):
        from painel.management.commands import preparar_banco

        with tempfile.TemporaryDirectory() as pasta:
            db_path = Path(pasta) / 'legado.sqlite3'
            backup_dir = Path(pasta) / 'backups'
            self._criar_legado(db_path)
            migracao_original = []
            chamadas = []

            def call_migrate(command, **kwargs):
                migracao_original.append(kwargs.get('database'))
                raise RuntimeError('Falha simulada antes da migração original')

            with _conexao_temporaria(db_path):
                with patch.object(preparar_banco, 'call_command', side_effect=call_migrate):
                    with self.assertRaisesRegex(RuntimeError, 'Falha simulada'):
                        call_command('preparar_banco', backup_dir=str(backup_dir), verbosity=0)
                self.assertEqual(migracao_original, ['default'])
                backup = next(backup_dir.glob('*.sqlite3'))
                with closing(sqlite3.connect(backup)) as db:
                    self.assertEqual(db.execute('SELECT id,nome FROM empresas').fetchall(), [(17, 'Legado')])
                    self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE name='django_migrations'").fetchall(), [])
                with closing(sqlite3.connect(db_path)) as db:
                    self.assertEqual(db.execute('SELECT id,nome FROM empresas').fetchall(), [(17, 'Legado')])
                    self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE name='django_migrations'").fetchall(), [])

    def test_migracao_e_recusada_se_worker_tem_lock_ativo(self):
        from painel.services.worker import WorkerLock, WorkerAlreadyRunning
        from painel.management.commands import preparar_banco

        with tempfile.TemporaryDirectory() as pasta:
            db_path = Path(pasta) / 'legado.sqlite3'
            backup_dir = Path(pasta) / 'backups'
            lock = Path(pasta) / 'worker.lock'
            self._criar_legado(db_path)
            with _conexao_temporaria(db_path):
                with patch('painel.services.worker.WORKER_LOCK', lock):
                    with WorkerLock(lock):
                        with self.assertRaises(WorkerAlreadyRunning):
                            call_command('preparar_banco', backup_dir=str(backup_dir), verbosity=0)
                self.assertEqual(list(backup_dir.glob('*.sqlite3')) if backup_dir.exists() else [], [])
                with closing(sqlite3.connect(db_path)) as db:
                    self.assertEqual(db.execute('SELECT id,nome FROM empresas').fetchall(), [(17, 'Legado')])

    def test_falha_na_validacao_shadow_nao_chama_migracao_da_origem(self):
        from painel.management.commands import preparar_banco

        with tempfile.TemporaryDirectory() as pasta:
            db_path = Path(pasta) / 'legado.sqlite3'
            backup_dir = Path(pasta) / 'backups'
            self._criar_legado(db_path)
            with _conexao_temporaria(db_path):
                with patch.object(preparar_banco.subprocess, 'run', return_value=SimpleNamespace(returncode=1)):
                    with patch.object(preparar_banco, 'call_command') as migrate_origem:
                        with self.assertRaisesRegex(CommandError, 'cópia falhou'):
                            call_command('preparar_banco', backup_dir=str(backup_dir), verbosity=0)
                        migrate_origem.assert_not_called()
                backup = next(backup_dir.glob('*.sqlite3'))
                for caminho in (db_path, backup):
                    with closing(sqlite3.connect(caminho)) as db:
                        self.assertEqual(db.execute('SELECT id,nome FROM empresas').fetchall(), [(17, 'Legado')])
                        self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE name='django_migrations'").fetchall(), [])
