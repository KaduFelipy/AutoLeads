from django.test import TestCase

class ContactsTests(TestCase):
    def test_classificacao_ignora_acentos_sem_mudar_chave_de_identidade(self):
        from painel.models import RegraTag, Tag
        from painel.services.contacts import classificar

        mecanica = Tag.objects.create(nome='Mecânica teste', tipo='ramo')
        retifica = Tag.objects.get(nome='Retífica', tipo='categoria')
        regra_mecanica = RegraTag.objects.create(termo='mecânica')
        regra_mecanica.tags.add(mecanica)
        regra_retifica = RegraTag.objects.create(termo='retifica')
        regra_retifica.tags.add(retifica)

        self.assertIn(mecanica, classificar('Mecanica Pato Branco'))
        self.assertIn(mecanica, classificar('Mecânica Pato Branco'))
        self.assertIn(retifica, classificar('RETIFICA Pato Branco'))
        self.assertIn(Tag.objects.get(nome='Oficina mecânica', tipo='categoria'),
                      classificar('oficina mecanica Pato Branco'))
        self.assertIn(Tag.objects.get(nome='Oficina mecânica', tipo='categoria'),
                      classificar('oficina me\u0302canica Pato Branco'))

    def test_regras_equivalentes_empatadas_unem_tags_e_vazio_segue_sem_classificacao(self):
        from painel.models import RegraTag, Tag
        from painel.services.contacts import classificar

        uma = Tag.objects.create(nome='Uma', tipo='ramo')
        duas = Tag.objects.create(nome='Duas', tipo='ramo')
        primeira = RegraTag.objects.create(termo='mecânica', prioridade=3)
        primeira.tags.add(uma)
        segunda = RegraTag.objects.create(termo='MECANICA', prioridade=3)
        segunda.tags.add(duas)

        self.assertEqual(set(classificar('mecanica')), {uma, duas})
        self.assertEqual([tag.nome for tag in classificar('sem regra alguma')], ['Sem classificação'])

    def test_empresa_unica_com_origens_e_correcao_manual(self):
        from painel.models import Empresa, Pesquisa, Perfil
        from painel.services.contacts import registrar_resultado, editar_tags
        a = Pesquisa.objects.create(termos='Retífica Pato Branco')
        b = Pesquisa.objects.create(termos='Oficina mecânica Pato Branco')
        dados = dict(nome='Motor', endereco='Rua A', telefone='46991160028')
        empresa = registrar_resultado(a, dados, 'https://maps.google.com/?cid=123')
        registrar_resultado(b, dados, 'https://maps.google.com/?cid=123')
        self.assertEqual(Empresa.objects.count(), 1)
        self.assertEqual(empresa.resultados.count(), 2)
        perfil = Perfil.objects.get(empresa=empresa)
        self.assertTrue(perfil.tags.filter(nome='Retífica').exists())
        editar_tags(empresa, [])
        registrar_resultado(a, dados, 'https://maps.google.com/?cid=123')
        self.assertFalse(perfil.tags.exists())

    def test_recaptura_nao_apaga_dados(self):
        from painel.models import Pesquisa
        from painel.services.contacts import registrar_resultado
        p = Pesquisa.objects.create(termos='Sem regra')
        e = registrar_resultado(p, dict(nome='Empresa', endereco='Rua', telefone='46991160028'), 'https://maps/?cid=1')
        registrar_resultado(p, dict(nome='Empresa', endereco=None, telefone=None), 'https://maps/?cid=1')
        e.refresh_from_db()
        self.assertEqual(e.telefone, '46991160028')
        self.assertTrue(e.perfil.tags.filter(nome='Sem classificação').exists())

    def test_chave_unicode_usa_serializacao_sqlite_legada_e_deduplica(self):
        import json
        from painel.models import Empresa, Pesquisa
        from painel.services.contacts import registrar_resultado

        pesquisa = Pesquisa.objects.create(termos='cafeteria')
        dados = {'nome': 'Café São José', 'endereco': 'Av. São José, 10'}
        empresa = registrar_resultado(pesquisa, dados, 'https://maps.google.com/?cid=unicode-1')
        repetida = registrar_resultado(pesquisa, dados, 'https://maps.google.com/?cid=unicode-2')
        chave_legada = json.dumps(['café são josé', 'av. são josé, 10'])

        self.assertEqual(Empresa.objects.count(), 1)
        self.assertEqual(repetida.pk, empresa.pk)
        self.assertEqual(empresa.chave, chave_legada)

    def test_recaptura_atualiza_chave_depois_de_mudanca_de_endereco(self):
        from painel.models import Empresa, Pesquisa
        from painel.services.contacts import registrar_resultado

        pesquisa = Pesquisa.objects.create(termos='oficina')
        empresa = registrar_resultado(
            pesquisa, {'nome': 'Oficina A', 'endereco': 'Rua antiga'},
            'https://maps.google.com/?cid=oficina-1'
        )
        registrar_resultado(
            pesquisa, {'nome': 'Oficina A', 'endereco': 'Rua nova'},
            'https://maps.google.com/?cid=oficina-1'
        )
        recuperada = registrar_resultado(
            pesquisa, {'nome': 'Oficina A', 'endereco': 'Rua nova'},
            'https://maps.google.com/?cid=oficina-2'
        )

        self.assertEqual(Empresa.objects.count(), 1)
        self.assertEqual(recuperada.pk, empresa.pk)
        self.assertEqual(recuperada.chave, '["oficina a", "rua nova"]')

    def test_regra_mais_especifica_prevalece_sobre_regra_generica(self):
        from painel.models import RegraTag, Tag
        from painel.services.contacts import classificar

        generica = Tag.objects.create(nome='Oficina genérica', tipo='categoria')
        regra = RegraTag.objects.create(termo='oficina', prioridade=100)
        regra.tags.add(generica)

        nomes = {tag.nome for tag in classificar('oficina mecânica em Pato Branco')}

        self.assertIn('Oficina mecânica', nomes)
        self.assertNotIn('Oficina genérica', nomes)

    def test_historico_registra_mudancas_automaticas_e_manuais(self):
        from painel.models import HistoricoTag, Pesquisa
        from painel.services.contacts import editar_tags, registrar_resultado

        pesquisa = Pesquisa.objects.create(termos='retífica')
        empresa = registrar_resultado(
            pesquisa, {'nome': 'Motor', 'endereco': 'Rua A'}, 'https://maps/?cid=historico'
        )
        tag = empresa.perfil.tags.get(nome='Retífica')
        editar_tags(empresa, [])

        eventos = list(HistoricoTag.objects.order_by('id').values_list('acao', 'origem', 'tag_nome'))

        self.assertIn(('adicionada', 'automatica', 'Retífica'), eventos)
        self.assertIn(('removida', 'manual', 'Retífica'), eventos)
