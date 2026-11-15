from datetime import timedelta

from django.test import TestCase
from django.utils import timezone


class CampaignsTests(TestCase):
    def setUp(self):
        from painel.models import Empresa, Perfil

        self.empresa_nunca = Empresa.objects.create(
            nome='Nunca', telefone='+55 (46) 99111-0028', maps_id='maps:nunca'
        )
        self.empresa_antiga = Empresa.objects.create(
            nome='Antiga', telefone='46992220028', maps_id='maps:antiga'
        )
        self.empresa_recente = Empresa.objects.create(
            nome='Recente', telefone='46993330028', maps_id='maps:recente'
        )
        Perfil.objects.get_or_create(empresa=self.empresa_nunca)

    def test_prepara_priorizando_nunca_e_mais_antigo_com_snapshot(self):
        from painel.models import Campanha, Destinatario, Reserva, Tentativa, Texto
        from painel.services.campaigns import preparar_campanha

        texto = Texto.objects.create(nome='Saudação', corpo='Olá, {nome}!')
        historica = Campanha.objects.create(
            nome='Histórico', texto_snapshot='Mensagem antiga', limite=2, session_id='sessao'
        )
        destinatario_antigo = Destinatario.objects.create(
            campanha=historica, empresa=self.empresa_antiga, ordem=1,
            telefone='5546992220028', mensagem='Mensagem antiga', mensagem_hash='x' * 64,
        )
        destinatario_recente = Destinatario.objects.create(
            campanha=historica, empresa=self.empresa_recente, ordem=2,
            telefone='5546993330028', mensagem='Mensagem antiga', mensagem_hash='y' * 64,
        )
        reserva = Reserva.objects.create(
            empresa_id=self.empresa_antiga.pk, session_id='sessao',
            telefone='5546992220028', mensagem='Mensagem antiga',
            mensagem_hash='antiga', status='enviado'
        )
        tentativa = Tentativa.objects.create(
            reserva=reserva, destinatario=destinatario_antigo, telefone='5546992220028',
            estado='enviado', confirmado_em=timezone.now() - timedelta(days=10)
        )
        reserva_recente = Reserva.objects.create(
            empresa_id=self.empresa_recente.pk, session_id='sessao',
            telefone='5546993330028', mensagem='Olá, Recente!',
            mensagem_hash='recente', status='enviado'
        )
        Tentativa.objects.create(
            reserva=reserva_recente, destinatario=destinatario_recente, telefone='5546993330028',
            estado='enviado', confirmado_em=timezone.now() - timedelta(days=1)
        )

        campanha = preparar_campanha('Teste', texto, {}, 2, 'sessao', intervalo=0)

        self.assertEqual(campanha.estado, Campanha.Estado.RASCUNHO)
        destinatarios = list(campanha.destinatarios.order_by('ordem'))
        self.assertEqual([item.telefone for item in destinatarios], [
            '5546991110028', '5546992220028'
        ])
        self.assertEqual(destinatarios[0].mensagem, 'Olá, Nunca!')
        texto.corpo = 'Texto alterado'
        texto.save()
        self.assertEqual(destinatarios[0].mensagem, 'Olá, Nunca!')
        self.assertIsNotNone(tentativa.pk)

    def test_deduplica_telefone_compartilhado_antes_do_limite(self):
        from painel.models import Empresa
        from painel.services.campaigns import preparar_campanha

        Empresa.objects.create(nome='Filial B', telefone='(46) 99111-0028', maps_id='maps:filial-b')
        campanha = preparar_campanha('Teste', 'Oi {nome}', {}, 1, 'sessao')

        self.assertEqual(campanha.destinatarios.count(), 1)
        item = campanha.destinatarios.get()
        self.assertEqual(item.telefone, '5546991110028')
        self.assertIn(self.empresa_nunca.pk, item.empresas_relacionadas)

    def test_filtra_tags_em_or_e_dimensoes_em_and(self):
        from painel.models import Perfil, Tag
        from painel.services.campaigns import filtrar_contatos

        ramo = Tag.objects.get(nome='Mecânica', tipo='ramo')
        categoria = Tag.objects.get(nome='Retífica', tipo='categoria')
        Perfil.objects.get_or_create(empresa=self.empresa_nunca)[0].tags.add(ramo, categoria)

        resultados = filtrar_contatos({'ramos': ['Mecânica', 'Alimentação'], 'categorias': ['Retífica']})

        self.assertEqual([empresa.pk for empresa in resultados], [self.empresa_nunca.pk])

    def test_telefone_invalido_fica_em_exclusoes_antes_do_limite(self):
        from painel.models import Empresa
        from painel.services.campaigns import preparar_campanha

        Empresa.objects.create(nome='Inválido', telefone='123', maps_id='maps:invalido')
        campanha = preparar_campanha('Teste', 'Oi', {}, 1, 'sessao')

        self.assertEqual(campanha.destinatarios.count(), 1)
        self.assertTrue(campanha.filtros_snapshot.get('exclusoes'))

    def test_rejeita_texto_e_parametros_invalidos(self):
        from painel.services.campaigns import preparar_campanha

        for corpo, limite, intervalo in [('', 1, 5), ('x' * 4097, 1, 5), ('Oi {desconhecido}', 1, 5), ('Oi', 0, 5), ('Oi', 1, -1)]:
            with self.subTest(corpo=corpo[:20], limite=limite, intervalo=intervalo):
                with self.assertRaises(ValueError):
                    preparar_campanha('Teste', corpo, {}, limite, 'sessao', intervalo)

    def test_personalizacao_preserva_avaliacao_zero(self):
        from painel.models import Empresa
        from painel.services.campaigns import preparar_campanha

        avaliada = Empresa.objects.create(
            nome='Nota baixa', telefone='46994440028', avaliacao=0.0, maps_id='maps:nota-zero'
        )
        campanha = preparar_campanha('Teste', 'Nota {avaliacao}', {}, 10, 'sessao')

        item = campanha.destinatarios.get(empresa=avaliada)
        self.assertEqual(item.mensagem, 'Nota 0.0')

    def test_iniciar_campanha_nao_recalcula_destinatarios(self):
        from painel.services.campaigns import iniciar_campanha, preparar_campanha

        campanha = preparar_campanha('Teste', 'Oi', {}, 10, 'sessao')
        quantidade = campanha.destinatarios.count()
        self.empresa_nunca.telefone = None
        self.empresa_nunca.save()

        iniciada = iniciar_campanha(campanha.pk)

        self.assertEqual(iniciada.estado, 'ativa')
        self.assertEqual(iniciada.destinatarios.count(), quantidade)

    def test_cotas_por_grupo_preparam_soma_solicitada_com_snapshot(self):
        from painel.models import Empresa, Perfil, Tag
        from painel.services.campaigns import preparar_campanha

        mecanica = Tag.objects.get(nome='Mecânica', tipo=Tag.Tipo.RAMO)
        perfil = Perfil.objects.get(empresa=self.empresa_nunca)
        perfil.tags.add(mecanica)
        self.empresa_nunca.endereco = 'Local'
        self.empresa_nunca.save(update_fields=('endereco',))
        ids_mecanica = {self.empresa_nunca.pk}
        for i in range(1):
            empresa = Empresa.objects.create(nome=f'Mecânica {i}', telefone=f'4699111000{i+1}', maps_id=f'mec:{i}')
            empresa.endereco = 'Local'
            empresa.save(update_fields=('endereco',))
            Perfil.objects.get_or_create(empresa=empresa)[0].tags.add(mecanica)
            ids_mecanica.add(empresa.pk)
        ids_sem = set()
        for i in range(5):
            empresa = Empresa.objects.create(nome=f'Sem classe {i}', telefone=f'4699222000{i+1}', maps_id=f'sem:{i}')
            empresa.endereco = 'Local'
            empresa.save(update_fields=('endereco',))
            Perfil.objects.get_or_create(empresa=empresa)
            ids_sem.add(empresa.pk)

        campanha = preparar_campanha(
            'Grupos', 'Oi {nome}', {'cidade': 'Local'}, 7, 'sessao-teste',
            selecao={'modo': 'quantidades', 'quantidades': {f'tag:{mecanica.pk}': 2, 'sem_classificacao': 5}},
        )

        self.assertEqual(campanha.destinatarios.count(), 7)
        snapshot = campanha.filtros_snapshot['selecao']
        self.assertEqual([grupo['preparado'] for grupo in snapshot['grupos']], [2, 5])
        self.assertEqual(campanha.limite, 7)
        for destinatario in campanha.destinatarios.all():
            self.assertTrue(destinatario.mensagem.startswith('Oi '))
            atribuicao = next(a for a in snapshot['atribuicoes'] if a['telefone'] == destinatario.telefone)
            self.assertIn(destinatario.empresa_id, ids_mecanica if atribuicao['grupo'] == f'tag:{mecanica.pk}' else ids_sem)

    def test_cotas_nao_compensam_falta_e_reserva_telefone_invalido_nao_consumem_cota(self):
        import hashlib
        from painel.models import Empresa, Perfil, Reserva, Tag
        from painel.services.campaigns import preparar_campanha

        mecanica = Tag.objects.get(nome='Mecânica', tipo=Tag.Tipo.RAMO)
        Perfil.objects.get_or_create(empresa=self.empresa_nunca)[0].tags.add(mecanica)
        self.empresa_nunca.endereco = 'Local'
        self.empresa_nunca.save(update_fields=('endereco',))
        sem = Empresa.objects.create(nome='Sem classe', telefone='46995550001', maps_id='sem:um')
        sem.endereco = 'Local'
        sem.save(update_fields=('endereco',))
        Perfil.objects.get_or_create(empresa=sem)
        mensagem = 'Oi Sem classe'
        Reserva.objects.create(empresa_id=sem.pk, session_id='sessao-teste', telefone='5546995550001',
                               mensagem=mensagem, mensagem_hash=hashlib.sha256(mensagem.encode()).hexdigest(), status='enviado')
        invalida = Empresa.objects.create(nome='Inválido', telefone='123', maps_id='sem:invalido')
        invalida.endereco = 'Local'
        invalida.save(update_fields=('endereco',))
        Perfil.objects.get_or_create(empresa=invalida)

        campanha = preparar_campanha(
            'Grupos', 'Oi {nome}', {'cidade': 'Local'}, 5, 'sessao-teste',
            selecao={'modo': 'quantidades', 'quantidades': {f'tag:{mecanica.pk}': 2, 'sem_classificacao': 3}},
        )

        resumo = campanha.filtros_snapshot['selecao']['grupos']
        self.assertEqual([(g['solicitado'], g['preparado']) for g in resumo], [(2, 1), (3, 0)])
        self.assertEqual(campanha.destinatarios.count(), 1)
        motivos = [item['motivo'] for item in campanha.exclusoes]
        self.assertTrue(any('reservado' in motivo for motivo in motivos))
        self.assertTrue(any('inválido' in motivo for motivo in motivos))

    def test_manual_usa_apenas_ids_marcados_deduplica_numero_e_exclui_fora_dos_filtros(self):
        from painel.models import Empresa, Perfil, Tag
        from painel.services.campaigns import preparar_campanha

        ramo = Tag.objects.get(nome='Mecânica', tipo=Tag.Tipo.RAMO)
        Perfil.objects.get_or_create(empresa=self.empresa_nunca)[0].tags.add(ramo)
        compartilhada = Empresa.objects.create(nome='Outra empresa', telefone='46991110028', maps_id='manual:compartilhada')
        fora = Empresa.objects.create(nome='Fora', telefone='46995550028', maps_id='manual:fora')
        self.empresa_nunca.endereco = 'Avenida Central'
        self.empresa_nunca.save(update_fields=('endereco',))
        compartilhada.endereco = 'Avenida Central'
        compartilhada.save(update_fields=('endereco',))
        fora.endereco = 'Rua distante'
        fora.save(update_fields=('endereco',))
        Perfil.objects.get_or_create(empresa=compartilhada)
        Perfil.objects.get_or_create(empresa=fora)

        campanha = preparar_campanha(
            'Manual', 'Oi {nome}', {'cidade': 'Avenida'}, 2, 'sessao-manual',
            selecao={'modo': 'manual', 'empresa_ids': [self.empresa_nunca.pk, compartilhada.pk, self.empresa_nunca.pk, fora.pk]},
        )

        itens = list(campanha.destinatarios.all())
        self.assertEqual(len(itens), 1)
        self.assertEqual(itens[0].empresa_id, self.empresa_nunca.pk)
        self.assertIn('Nunca', itens[0].mensagem)
        self.assertEqual(itens[0].empresas_relacionadas, sorted([self.empresa_nunca.pk, compartilhada.pk]))
        self.assertEqual(campanha.limite, 3)
        self.assertEqual(campanha.filtros_snapshot['selecao']['empresa_ids'],
                         [self.empresa_nunca.pk, compartilhada.pk, fora.pk])
        self.assertTrue(any(ex.get('empresa_id') == fora.pk for ex in campanha.exclusoes))

    def test_manual_conta_marcados_fora_do_filtro_no_solicitado_do_grupo(self):
        from painel.models import Empresa, Perfil, Tag
        from painel.services.campaigns import preparar_campanha

        ramo = Tag.objects.get(nome='Mecânica', tipo=Tag.Tipo.RAMO)
        dentro = Empresa.objects.create(nome='Dentro', telefone='46990002001', maps_id='manual:filtro:dentro',
                                        endereco='Curitiba')
        fora = Empresa.objects.create(nome='Fora', telefone='46990002002', maps_id='manual:filtro:fora',
                                      endereco='São Paulo')
        for empresa in (dentro, fora):
            Perfil.objects.get_or_create(empresa=empresa)[0].tags.add(ramo)

        campanha = preparar_campanha('Manual filtrado', 'Oi {nome}', {'cidade': 'Curitiba'}, 2, 'sessao-filtro',
            selecao={'modo': 'manual', 'empresa_ids': [dentro.pk, fora.pk]})

        resumo = next(grupo for grupo in campanha.filtros_snapshot['selecao']['grupos']
                      if grupo['chave'] == f'tag:{ramo.pk}')
        self.assertEqual((resumo['solicitado'], resumo['preparado']), (2, 1))
        self.assertTrue(resumo.get('motivo_reducao'))
        self.assertEqual(sum(grupo['solicitado'] for grupo in campanha.filtros_snapshot['selecao']['grupos']), 2)
        self.assertEqual(campanha.destinatarios.count(), 1)
        self.assertTrue(any(item.get('empresa_id') == fora.pk for item in campanha.exclusoes))

    def test_manual_conta_multitag_sem_dup_e_todas_fora_dos_filtros(self):
        from painel.models import Empresa, Perfil, Tag
        from painel.services.campaigns import preparar_campanha

        ramo = Tag.objects.get(nome='Mecânica', tipo=Tag.Tipo.RAMO)
        categoria = Tag.objects.get(nome='Retífica', tipo=Tag.Tipo.CATEGORIA)
        empresas = [
            Empresa.objects.create(nome='Multi', telefone='46990003001', maps_id='manual:fora:multi', endereco='Porto Alegre'),
            Empresa.objects.create(nome='Ramo', telefone='46990003002', maps_id='manual:fora:ramo', endereco='São Paulo'),
            Empresa.objects.create(nome='Categoria', telefone='46990003003', maps_id='manual:fora:categoria', endereco='Rio'),
        ]
        Perfil.objects.get_or_create(empresa=empresas[0])[0].tags.add(ramo, categoria)
        Perfil.objects.get_or_create(empresa=empresas[1])[0].tags.add(ramo)
        Perfil.objects.get_or_create(empresa=empresas[2])[0].tags.add(categoria)

        campanha = preparar_campanha('Manual tudo fora', 'Oi {nome}', {'cidade': 'Curitiba'}, 3, 'sessao-tudo-fora',
            selecao={'modo': 'manual', 'empresa_ids': [empresa.pk for empresa in empresas]})

        resumo = {grupo['chave']: grupo for grupo in campanha.filtros_snapshot['selecao']['grupos']}
        self.assertEqual((resumo[f'tag:{ramo.pk}']['solicitado'], resumo[f'tag:{ramo.pk}']['preparado']), (2, 0))
        self.assertEqual((resumo[f'tag:{categoria.pk}']['solicitado'], resumo[f'tag:{categoria.pk}']['preparado']), (1, 0))
        self.assertEqual(sum(grupo['solicitado'] for grupo in resumo.values()), 3)
        self.assertEqual(sum(grupo['preparado'] for grupo in resumo.values()), 0)
        self.assertEqual(campanha.limite, 3)
        self.assertEqual(campanha.destinatarios.count(), 0)
        self.assertTrue(all(grupo.get('motivo_reducao') for grupo in resumo.values() if grupo['solicitado']))

    def test_cota_sobreposta_reserva_numero_apenas_no_primeiro_grupo_com_representante_do_grupo(self):
        from painel.models import Empresa, Perfil, Tag
        from painel.services.campaigns import preparar_campanha

        primeiro = Tag.objects.get(nome='Mecânica', tipo=Tag.Tipo.RAMO)
        segundo = Tag.objects.get(nome='Retífica', tipo=Tag.Tipo.CATEGORIA)
        Perfil.objects.get_or_create(empresa=self.empresa_nunca)[0].tags.add(primeiro, segundo)
        adicional = Empresa.objects.create(nome='Mesmo número', telefone=self.empresa_nunca.telefone, maps_id='sobreposto')
        Perfil.objects.get_or_create(empresa=adicional)[0].tags.add(segundo)

        campanha = preparar_campanha(
            'Sobreposição', 'Oi {nome}', {}, 2, 'sessao-sobreposta',
            selecao={'modo': 'quantidades', 'quantidades': {f'tag:{primeiro.pk}': 1, f'tag:{segundo.pk}': 1}},
        )

        item = campanha.destinatarios.get()
        atribuicao = campanha.filtros_snapshot['selecao']['atribuicoes'][0]
        self.assertEqual(atribuicao['grupo'], f'tag:{primeiro.pk}')
        self.assertEqual(item.empresa_id, self.empresa_nunca.pk)
        self.assertEqual(item.empresas_relacionadas, sorted([self.empresa_nunca.pk, adicional.pk]))

    def test_cota_tenta_proximo_representante_se_mensagem_do_primeiro_ja_foi_reservada(self):
        import hashlib
        from painel.models import Empresa, Perfil, Reserva, Tag
        from painel.services.campaigns import preparar_campanha

        ramo = Tag.objects.get(nome='Mecânica', tipo=Tag.Tipo.RAMO)
        primeiro = Empresa.objects.create(nome='Primeiro', telefone='46990001001', maps_id='reserva:primeiro')
        segundo = Empresa.objects.create(nome='Segundo', telefone='46990001001', maps_id='reserva:segundo')
        Perfil.objects.get_or_create(empresa=primeiro)[0].tags.add(ramo)
        Perfil.objects.get_or_create(empresa=segundo)[0].tags.add(ramo)
        mensagem_reservada = 'Oi Primeiro'
        Reserva.objects.create(empresa_id=primeiro.pk, session_id='sessao-reservada',
            telefone='5546990001001', mensagem=mensagem_reservada,
            mensagem_hash=hashlib.sha256(mensagem_reservada.encode()).hexdigest(), status='enviado')

        campanha = preparar_campanha('Reserva', 'Oi {nome}', {}, 1, 'sessao-reservada',
            selecao={'modo': 'quantidades', 'quantidades': {f'tag:{ramo.pk}': 1}})

        item = campanha.destinatarios.get()
        self.assertEqual(item.empresa_id, segundo.pk)
        self.assertEqual(item.mensagem, 'Oi Segundo')

    def test_manual_tenta_proximo_representante_se_mensagem_ja_foi_reservada(self):
        import hashlib
        from painel.models import Empresa, Perfil, Reserva
        from painel.services.campaigns import preparar_campanha

        primeiro = Empresa.objects.create(nome='Manual A', telefone='46990001002', maps_id='manual:reserva:a')
        segundo = Empresa.objects.create(nome='Manual B', telefone='46990001002', maps_id='manual:reserva:b')
        Perfil.objects.get_or_create(empresa=primeiro)
        Perfil.objects.get_or_create(empresa=segundo)
        mensagem_reservada = 'Oi Manual A'
        Reserva.objects.create(empresa_id=primeiro.pk, session_id='sessao-manual-reservada',
            telefone='5546990001002', mensagem=mensagem_reservada,
            mensagem_hash=hashlib.sha256(mensagem_reservada.encode()).hexdigest(), status='enviado')

        campanha = preparar_campanha('Manual', 'Oi {nome}', {}, 2, 'sessao-manual-reservada',
            selecao={'modo': 'manual', 'empresa_ids': [primeiro.pk, segundo.pk]})

        item = campanha.destinatarios.get()
        self.assertEqual(item.empresa_id, segundo.pk)
        self.assertEqual(item.mensagem, 'Oi Manual B')

    def test_mensagem_personalizada_invalida_nao_vira_exclusao_de_telefone(self):
        from painel.models import Empresa, Perfil
        from painel.services.campaigns import preparar_campanha

        empresa = Empresa.objects.create(nome='x' * 4095, telefone='46990001003', maps_id='texto:longo')
        Perfil.objects.get_or_create(empresa=empresa)

        with self.assertRaisesRegex(ValueError, 'mensagem'):
            preparar_campanha('Texto inválido', 'Oi {nome}', {}, 1, 'sessao-texto',
                selecao={'modo': 'manual', 'empresa_ids': [empresa.pk]})

        from painel.models import Campanha
        self.assertFalse(Campanha.objects.filter(nome='Texto inválido').exists())

    def test_prioridade_empate_por_id_e_selecao_invalida(self):
        from painel.models import Empresa, Perfil, Tag
        from painel.services.campaigns import preparar_campanha

        ramo = Tag.objects.get(nome='Mecânica', tipo=Tag.Tipo.RAMO)
        Perfil.objects.get_or_create(empresa=self.empresa_nunca)[0].tags.add(ramo)
        empate_a = Empresa.objects.create(nome='Empate A', telefone='46996660001', maps_id='empate:a')
        empate_b = Empresa.objects.create(nome='Empate B', telefone='46996660002', maps_id='empate:b')
        Perfil.objects.get_or_create(empresa=empate_a)[0].tags.add(ramo)
        Perfil.objects.get_or_create(empresa=empate_b)[0].tags.add(ramo)
        campanha = preparar_campanha('Prioridade', 'Oi', {}, 2, 'sessao-prioridade',
            selecao={'modo': 'quantidades', 'quantidades': {f'tag:{ramo.pk}': 2}})
        self.assertEqual([d.empresa_id for d in campanha.destinatarios.all()], [self.empresa_nunca.pk, empate_a.pk])
        for selecao in (
            {'modo': 'quantidades', 'quantidades': {'tag:999999': 1}},
            {'modo': 'manual', 'empresa_ids': [999999]},
            {'modo': 'outro'},
        ):
            with self.subTest(selecao=selecao), self.assertRaises(ValueError):
                preparar_campanha('Inválida', 'Oi', {}, 1, 'sessao', selecao=selecao)

    def test_selecao_preserva_prioridade_nunca_antigo_e_id(self):
        from painel.models import Campanha, Destinatario, Empresa, Perfil, Reserva, Tag, Tentativa
        from painel.services.campaigns import preparar_campanha

        ramo = Tag.objects.get(nome='Mecânica', tipo='ramo')
        Perfil.objects.get_or_create(empresa=self.empresa_nunca)[0].tags.add(ramo)
        empresas = [self.empresa_nunca]
        for nome, maps_id, telefone in [('Recente', 'prio:recente', '46997770001'),
                                        ('Antiga', 'prio:antiga', '46997770002'),
                                        ('Empate 1', 'prio:empate1', '46997770003'),
                                        ('Empate 2', 'prio:empate2', '46997770004')]:
            empresa = Empresa.objects.create(nome=nome, telefone=telefone, maps_id=maps_id)
            Perfil.objects.get_or_create(empresa=empresa)[0].tags.add(ramo)
            empresas.append(empresa)
        historico = Campanha.objects.create(nome='Histórico', texto_snapshot='Oi', limite=4, session_id='histórico')
        empate = timezone.now() - timedelta(days=5)
        instantes = [timezone.now() - timedelta(days=1), timezone.now() - timedelta(days=10), empate, empate]
        for ordem, (empresa, instante) in enumerate(zip(empresas[1:], instantes), 1):
            destinatario = Destinatario.objects.create(campanha=historico, empresa=empresa, ordem=ordem,
                telefone='55' + empresa.telefone, mensagem='Oi', mensagem_hash='a' * 64)
            reserva = Reserva.objects.create(empresa_id=empresa.pk, session_id='histórico',
                telefone=destinatario.telefone, mensagem='Oi', mensagem_hash='a' * 64, status='enviado')
            Tentativa.objects.create(destinatario=destinatario, reserva=reserva, estado='enviado',
                                     telefone=destinatario.telefone, confirmado_em=instante)

        campanha = preparar_campanha('Prioridade', 'Oi', {}, 5, 'sessao-prioridade',
            selecao={'modo': 'quantidades', 'quantidades': {f'tag:{ramo.pk}': 5}})

        self.assertEqual([item.empresa_id for item in campanha.destinatarios.all()],
                         [self.empresa_nunca.pk, empresas[2].pk, empresas[3].pk, empresas[4].pk, empresas[1].pk])

    def test_iniciar_selecao_nao_recalcula_snapshot_ou_destinatarios(self):
        from painel.services.campaigns import iniciar_campanha, preparar_campanha

        campanha = preparar_campanha('Manual congelado', 'Oi {nome}', {}, 1, 'sessao',
            selecao={'modo': 'manual', 'empresa_ids': [self.empresa_nunca.pk]})
        snapshot = campanha.filtros_snapshot.copy()
        self.empresa_nunca.telefone = None
        self.empresa_nunca.save(update_fields=('telefone',))

        iniciada = iniciar_campanha(campanha.pk)

        self.assertEqual(iniciada.destinatarios.count(), 1)
        self.assertEqual(iniciada.filtros_snapshot, snapshot)
