import tempfile
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

from django.conf import settings
from django.db import connection, connections
from django.db.backends.sqlite3.base import DatabaseWrapper
from django.db.migrations.executor import MigrationExecutor
from django.test import SimpleTestCase, TestCase, TransactionTestCase


class LegacySchemaTests(TestCase):
    def test_legado_preserva_tabela_e_ids_e_novas_colunas_ficam_separadas(self):
        from painel.models import Empresa

        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO empresas (id, nome, maps_id) VALUES (%s, %s, %s)",
                [17, 'Legado', 'maps:legado']
            )
            cursor.execute("PRAGMA table_info(empresas)")
            empresa_columns = [row[1] for row in cursor.fetchall()]
            cursor.execute("PRAGMA table_info(envios_whatsapp)")
            reserva_columns = [row[1] for row in cursor.fetchall()]

        self.assertEqual(Empresa.objects.get(pk=17).nome, 'Legado')
        self.assertEqual(empresa_columns, [
            'id', 'nome', 'endereco', 'telefone', 'site', 'avaliacao', 'maps_id', 'chave'
        ])
        self.assertEqual(reserva_columns, [
            'id', 'empresa_id', 'session_id', 'telefone', 'mensagem', 'mensagem_hash',
            'status', 'message_id', 'erro', 'criado_em', 'atualizado_em'
        ])
        self.assertEqual(Empresa.objects.filter(pk=17).count(), 1)


class LegacyAdoptionTests(SimpleTestCase):
    def test_adota_esquema_existente_sem_alterar_dados(self):
        migration = import_module('painel.migrations.0001_initial')

        with tempfile.TemporaryDirectory() as pasta:
            configuracao = dict(settings.DATABASES['default'])
            configuracao['NAME'] = str(Path(pasta) / 'legado.sqlite3')
            banco = DatabaseWrapper(configuracao, 'legacy_probe')
            try:
                    with banco.cursor() as cursor:
                        cursor.execute("""
                            CREATE TABLE empresas (
                                id INTEGER PRIMARY KEY, nome TEXT NOT NULL, endereco TEXT,
                                telefone TEXT, site TEXT,
                                avaliacao REAL CHECK (avaliacao BETWEEN 0 AND 5),
                                maps_id TEXT NOT NULL UNIQUE, chave TEXT UNIQUE
                            )
                        """)
                        cursor.execute("INSERT INTO empresas (id, nome, maps_id) VALUES (17, 'Legado', 'maps:17')")
                        cursor.execute("""
                            CREATE TABLE envios_whatsapp (
                                id INTEGER PRIMARY KEY, empresa_id INTEGER NOT NULL,
                                session_id TEXT NOT NULL, telefone TEXT NOT NULL,
                                mensagem TEXT NOT NULL, mensagem_hash TEXT NOT NULL,
                                status TEXT NOT NULL, message_id TEXT, erro TEXT,
                                criado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                                atualizado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                                UNIQUE (session_id, telefone, mensagem_hash)
                            )
                        """)
                    editor = SimpleNamespace(connection=banco)
                    migration.validar_legado(None, editor)
                    migration.criar_tabelas_legadas(None, editor)
                    with banco.cursor() as cursor:
                        cursor.execute('SELECT id, nome, maps_id FROM empresas')
                        self.assertEqual(cursor.fetchall(), [(17, 'Legado', 'maps:17')])
                        cursor.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN ('empresas', 'envios_whatsapp')")
                        self.assertEqual(cursor.fetchone()[0], 2)
            finally:
                banco.close()


class MecanicaDataMigrationTests(TransactionTestCase):
    def _banco_em_0004(self, pasta):
        configuracao = dict(settings.DATABASES['default'])
        configuracao['NAME'] = str(Path(pasta) / 'mecanica.sqlite3')
        banco = DatabaseWrapper(configuracao, 'default')
        banco._original_test_default = connections._connections.default
        connections._connections.default = banco
        executor = MigrationExecutor(banco)
        executor.migrate([('painel', '0004_autorizacaoreenvio_destinatario_and_more')])
        estado = executor.loader.project_state([('painel', '0004_autorizacaoreenvio_destinatario_and_more')])
        return banco, executor, estado.apps

    def _fechar_banco_probe(self, banco):
        connections._connections.default = banco._original_test_default
        banco.close()

    def test_migracao_mecanica_aditiva_preserva_legado_e_idempotencia(self):
        adicionar_regra_mecanica = import_module('painel.migrations.0005_regra_mecanica').adicionar_regra_mecanica

        with tempfile.TemporaryDirectory() as pasta:
            banco, executor, apps = self._banco_em_0004(pasta)
            try:
                Tag = apps.get_model('painel', 'Tag')
                RegraTag = apps.get_model('painel', 'RegraTag')
                ramo, _ = Tag.objects.using(banco.alias).get_or_create(nome='Mecânica', tipo='ramo')
                categoria, _ = Tag.objects.using(banco.alias).get_or_create(nome='Personalizada', tipo='categoria')
                regra_antiga = RegraTag.objects.using(banco.alias).create(termo='polimento', prioridade=10)
                regra_antiga.tags.add(categoria)
                editor = SimpleNamespace(connection=banco)

                adicionar_regra_mecanica(apps, editor)
                nova = RegraTag.objects.using(banco.alias).get(termo='mecanica')
                self.assertEqual(nova.prioridade, 0)
                self.assertEqual(list(nova.tags.values_list('nome', 'tipo')), [('Mecânica', 'ramo')])
                self.assertEqual(list(regra_antiga.tags.values_list('pk', flat=True)), [categoria.pk])
                snapshot = list(RegraTag.objects.using(banco.alias).order_by('pk').values_list('pk', 'termo', 'prioridade'))
                adicionar_regra_mecanica(apps, editor)
                self.assertEqual(list(RegraTag.objects.using(banco.alias).order_by('pk').values_list('pk', 'termo', 'prioridade')), snapshot)
                self.assertEqual(nova.tags.count(), 1)
            finally:
                    self._fechar_banco_probe(banco)

    def test_migracao_mecanica_preserva_regra_personalizada_equivalente(self):
        adicionar_regra_mecanica = import_module('painel.migrations.0005_regra_mecanica').adicionar_regra_mecanica

        for termo, associar_tag in [('MECÂNICA', True), ('mecanica', False)]:
            with self.subTest(termo=termo), tempfile.TemporaryDirectory() as pasta:
                banco, executor, apps = self._banco_em_0004(pasta)
                try:
                    Tag = apps.get_model('painel', 'Tag')
                    RegraTag = apps.get_model('painel', 'RegraTag')
                    regra = RegraTag.objects.using(banco.alias).create(termo=termo, prioridade=42)
                    if associar_tag:
                        categoria = Tag.objects.using(banco.alias).create(nome='Customizada', tipo='categoria')
                        regra.tags.add(categoria)
                    antes = (regra.pk, regra.prioridade, list(regra.tags.values_list('pk', flat=True)))

                    adicionar_regra_mecanica(apps, SimpleNamespace(connection=banco))

                    regra.refresh_from_db()
                    self.assertEqual(RegraTag.objects.using(banco.alias).count(), 4)
                    self.assertEqual((regra.pk, regra.prioridade, list(regra.tags.values_list('pk', flat=True))), antes)
                finally:
                    self._fechar_banco_probe(banco)

    def test_recusa_esquema_incompativel_sem_tentar_corrigir(self):
        migration = import_module('painel.migrations.0001_initial')

        with tempfile.TemporaryDirectory() as pasta:
            configuracao = dict(settings.DATABASES['default'])
            configuracao['NAME'] = str(Path(pasta) / 'incompativel.sqlite3')
            banco = DatabaseWrapper(configuracao, 'legacy_probe')
            try:
                    with banco.cursor() as cursor:
                        cursor.execute('CREATE TABLE empresas (id INTEGER PRIMARY KEY, nome TEXT)')
                        cursor.execute("INSERT INTO empresas (id, nome) VALUES (17, 'Preservado')")
                    editor = SimpleNamespace(connection=banco)
                    with self.assertRaisesRegex(RuntimeError, 'incompatível'):
                        migration.validar_legado(None, editor)
                    with banco.cursor() as cursor:
                        cursor.execute('SELECT id, nome FROM empresas')
                        self.assertEqual(cursor.fetchall(), [(17, 'Preservado')])
            finally:
                banco.close()
