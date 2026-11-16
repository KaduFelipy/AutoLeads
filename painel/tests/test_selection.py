from django.test import TestCase


class SelectionTests(TestCase):
    def setUp(self):
        from painel.models import Empresa, Perfil, Tag

        self.legada = Empresa.objects.create(nome='Legada', maps_id='selection:legacy')
        self.sem_perfil = Empresa.objects.create(nome='Sem Perfil', maps_id='selection:no-profile')
        self.vazia = Empresa.objects.create(nome='Vazia', maps_id='selection:empty')
        Perfil.objects.create(empresa=self.vazia)
        self.retifica = Empresa.objects.create(nome='Retífica', maps_id='selection:retifica')
        self.mecanica = Empresa.objects.create(nome='Mecânica', maps_id='selection:mecanica')
        self.multitag = Empresa.objects.create(nome='Multitag', maps_id='selection:multi')
        self.ramo = Tag.objects.get_or_create(nome='Mecânica', tipo=Tag.Tipo.RAMO)[0]
        self.categoria = Tag.objects.get_or_create(nome='Retífica', tipo=Tag.Tipo.CATEGORIA)[0]
        self.placeholder = Tag.objects.get_or_create(nome='Sem classificação', tipo=Tag.Tipo.CATEGORIA)[0]
        Perfil.objects.create(empresa=self.legada).tags.add(self.placeholder)
        Perfil.objects.create(empresa=self.retifica).tags.add(self.categoria)
        Perfil.objects.create(empresa=self.mecanica).tags.add(self.placeholder, self.ramo)
        Perfil.objects.create(empresa=self.multitag).tags.add(self.ramo, self.categoria)

    def test_grupos_usam_tags_atuais_e_sem_classificacao_virtual(self):
        from painel.services.selection import listar_grupos

        grupos = listar_grupos({})
        por_chave = {g['chave']: g for g in grupos}
        sem_classificacao = {e.pk for e in por_chave['sem_classificacao']['empresas']}
        self.assertEqual(sem_classificacao, {self.legada.pk, self.sem_perfil.pk, self.vazia.pk})
        self.assertEqual({e.pk for e in por_chave[f'tag:{self.ramo.pk}']['empresas']},
                         {self.mecanica.pk, self.multitag.pk})
        self.assertEqual({e.pk for e in por_chave[f'tag:{self.categoria.pk}']['empresas']},
                         {self.retifica.pk, self.multitag.pk})
        self.assertNotIn(f'tag:{self.placeholder.pk}', por_chave)

    def test_ordem_grupos_ramos_categorias_e_virtual(self):
        from painel.models import Tag
        from painel.services.selection import listar_grupos

        ramo_z = Tag.objects.create(nome='Zeta', tipo=Tag.Tipo.RAMO)
        ramo_a = Tag.objects.create(nome='Alfa', tipo=Tag.Tipo.RAMO)
        categoria_z = Tag.objects.create(nome='Zeta', tipo=Tag.Tipo.CATEGORIA)
        categoria_a = Tag.objects.create(nome='Alfa', tipo=Tag.Tipo.CATEGORIA)
        chaves = [g['chave'] for g in listar_grupos({})]
        relevantes = [chave for chave in chaves if chave in {
            f'tag:{ramo_a.pk}', f'tag:{self.ramo.pk}', f'tag:{ramo_z.pk}',
            f'tag:{categoria_a.pk}', f'tag:{self.categoria.pk}', f'tag:{categoria_z.pk}',
            'sem_classificacao',
        }]
        self.assertEqual(relevantes, [f'tag:{ramo_a.pk}', f'tag:{self.ramo.pk}', f'tag:{ramo_z.pk}',
                                      f'tag:{categoria_a.pk}', f'tag:{self.categoria.pk}',
                                      f'tag:{categoria_z.pk}', 'sem_classificacao'])

    def test_filtro_comum_e_aplicado_a_todos_os_grupos(self):
        from painel.services.selection import listar_grupos

        grupos = listar_grupos({'cidade': 'Centro'})
        self.assertTrue(all(not g['empresas'] for g in grupos))

    def test_normaliza_cotas_por_soma(self):
        from painel.services.selection import validar_selecao

        normalizada = validar_selecao({'modo': 'quantidades', 'quantidades': {
            f'tag:{self.ramo.pk}': 2, 'sem_classificacao': 5,
        }})
        self.assertEqual(normalizada, {'modo': 'quantidades', 'quantidades': {
            f'tag:{self.ramo.pk}': 2, 'sem_classificacao': 5,
        }, 'total': 7})

    def test_manual_deduplica_ids_e_confirma_existencia(self):
        from painel.services.selection import validar_selecao

        self.assertEqual(validar_selecao({'modo': 'manual', 'empresa_ids': [
            self.legada.pk, self.legada.pk, self.retifica.pk,
        ]}), {'modo': 'manual', 'empresa_ids': [self.legada.pk, self.retifica.pk], 'total': 2})

    def test_recusa_payloads_invalidos(self):
        from painel.services.selection import validar_selecao

        invalidos = [
            {}, {'modo': 'outro', 'empresa_ids': [self.legada.pk]},
            {'modo': 'manual', 'empresa_ids': []},
            {'modo': 'manual', 'empresa_ids': [True]},
            {'modo': 'manual', 'empresa_ids': [999999]},
            {'modo': 'manual', 'empresa_ids': [self.legada.pk], 'quantidades': {}},
            {'modo': 'quantidades', 'quantidades': {}},
            {'modo': 'quantidades', 'quantidades': {f'tag:{self.placeholder.pk}': 1}},
            {'modo': 'quantidades', 'quantidades': {'tag:999999': 1}},
            {'modo': 'quantidades', 'quantidades': {f'tag:{self.ramo.pk}': True}},
            {'modo': 'quantidades', 'quantidades': {f'tag:{self.ramo.pk}': -1}},
            {'modo': 'quantidades', 'quantidades': {f'tag:{self.ramo.pk}': 1.5}},
            {'modo': 'quantidades', 'quantidades': {f'tag:{self.ramo.pk}': 1001}},
            {'modo': 'quantidades', 'quantidades': {'sem_classificacao': 1001}},
            {'modo': 'quantidades', 'quantidades': {f'tag:{self.ramo.pk}': 600,
                                                    'sem_classificacao': 401}},
            {'modo': 'quantidades', 'quantidades': {f'tag:{self.ramo.pk}': 1},
             'empresa_ids': [self.legada.pk]},
        ]
        for payload in invalidos:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                validar_selecao(payload)

    def test_lista_manual_busca_literal_normalizada_e_nao_duplica_multitag(self):
        from painel.models import Empresa, Perfil
        from painel.services.selection import listar_empresas_selecao

        academia = Empresa.objects.create(nome='  Academia São Lucas  ', maps_id='selection:academia')
        Perfil.objects.create(empresa=academia).tags.add(self.ramo, self.categoria)
        pagina = listar_empresas_selecao({}, busca=' ACADÊMIA   SAO ')
        self.assertEqual(pagina['total'], 1)
        self.assertEqual([item['id'] for item in pagina['empresas']], [academia.pk])
        self.assertEqual(pagina['empresas'][0]['classificacoes'], [
            {'chave': f'tag:{self.ramo.pk}', 'nome': self.ramo.nome, 'tipo': 'ramo'},
            {'chave': f'tag:{self.categoria.pk}', 'nome': self.categoria.nome, 'tipo': 'categoria'},
        ])

    def test_lista_manual_trata_meta_caracteres_como_literais_e_pagina_global(self):
        from painel.models import Empresa, Perfil
        from painel.services.selection import listar_empresas_selecao

        literal = Empresa.objects.create(nome='Loja %_[] (.*)', maps_id='selection:literal')
        for index in range(120):
            empresa = Empresa.objects.create(nome=f'Empresa {index:03}', maps_id=f'selection:paged:{index}')
            Perfil.objects.get_or_create(empresa=empresa)[0].tags.add(self.ramo, self.categoria)
        self.assertEqual(listar_empresas_selecao({}, busca='%_[] (.*)')['empresas'][0]['id'], literal.pk)
        paginas = [listar_empresas_selecao({}, pagina=n) for n in (1, 2, 3)]
        self.assertEqual([p['total'] for p in paginas], [127, 127, 127])
        self.assertEqual([len(p['empresas']) for p in paginas], [50, 50, 27])
        ids = [e['id'] for p in paginas for e in p['empresas']]
        self.assertEqual(len(ids), len(set(ids)))

    def test_lista_manual_valida_busca_classificacao_e_pagina(self):
        from painel.services.selection import listar_empresas_selecao

        with self.assertRaises(ValueError):
            listar_empresas_selecao({}, busca='x' * 201)
        with self.assertRaises(ValueError):
            listar_empresas_selecao({}, classificacao=f'tag:{self.placeholder.pk}')
        with self.assertRaises(ValueError):
            listar_empresas_selecao({}, pagina='abc')

    def test_lista_manual_combina_nome_e_classificacao_e_ajusta_pagina(self):
        from painel.models import Empresa, Perfil
        from painel.services.selection import listar_empresas_selecao

        academia = Empresa.objects.create(nome='Academia A', maps_id='selection:and:a')
        oficina = Empresa.objects.create(nome='Academia B', maps_id='selection:and:b')
        Perfil.objects.create(empresa=academia).tags.add(self.ramo)
        Perfil.objects.create(empresa=oficina).tags.add(self.categoria)
        resultado = listar_empresas_selecao({}, busca='academia', classificacao=f'tag:{self.ramo.pk}')
        self.assertEqual([item['id'] for item in resultado['empresas']], [academia.pk])
        final = listar_empresas_selecao({}, busca='sem resultado', pagina=99)
        self.assertEqual((final['pagina'], final['total_paginas'], final['total']), (1, 1, 0))
        longa = listar_empresas_selecao({}, pagina=99)
        self.assertEqual(longa['pagina'], longa['total_paginas'])
