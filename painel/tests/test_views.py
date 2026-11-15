import sys
import types
from unittest.mock import patch

from django.test import Client, TestCase, override_settings
from django.urls import reverse


def integration_fake():
    module = types.ModuleType('painel.services.integration')
    module.configuracao_publica = lambda: {'configurada': True, 'session_id': 'teste'}
    module.estado_conexao = lambda: {'conectado': False, 'status': 'disconnected'}
    module.obter_qr = lambda: b'qr-sintetico'
    module.conectar_sessao = lambda: None
    return module


class InterfaceRoutesTests(TestCase):
    def setUp(self):
        self.connection_patch = patch('painel.views._conexao', return_value={'conectado': False})
        self.connection_patch.start()
        self.addCleanup(self.connection_patch.stop)

    def test_rotas_de_consulta_carregam(self):
        with patch.dict(sys.modules, {'painel.services.integration': integration_fake()}):
            for nome in ('dashboard', 'pesquisas', 'contatos', 'textos', 'campanhas',
                         'historico', 'whatsapp', 'tags', 'regras'):
                with self.subTest(nome=nome):
                    resposta = self.client.get(reverse(nome))
                    self.assertEqual(resposta.status_code, 200)

    def test_endpoints_de_acao_recusam_get(self):
        for nome in ('conectar', 'campanha_acao', 'reenvio'):
            with self.subTest(nome=nome):
                resposta = self.client.get(reverse(nome, kwargs={'pk': 1})
                                           if nome != 'conectar' else reverse(nome))
                self.assertEqual(resposta.status_code, 405)

    def test_qr_e_sem_cache(self):
        with patch.dict(sys.modules, {'painel.services.integration': integration_fake()}):
            resposta = self.client.get(reverse('qr'))
        self.assertEqual(resposta.status_code, 200)
        self.assertIn('no-store', resposta.headers.get('Cache-Control', ''))
        self.assertEqual(resposta.content, b'qr-sintetico')

    def test_qr_indisponivel_tambem_nao_fica_em_cache(self):
        from painel.services import integration
        with patch.object(integration, 'obter_qr', side_effect=ValueError('sem QR')):
            resposta = self.client.get(reverse('qr'))
        self.assertEqual(resposta.status_code, 503)
        self.assertIn('no-store', resposta.headers.get('Cache-Control', ''))

    def test_nome_externo_e_escapado_na_lista(self):
        from painel.models import Empresa
        empresa = Empresa.objects.create(nome='<script>alert(1)</script>', site='javascript:alert(2)', maps_id='empresa-xss')
        resposta = self.client.get(reverse('contatos'))
        self.assertEqual(resposta.status_code, 200)
        self.assertNotContains(resposta, '<script>alert(1)</script>')
        self.assertContains(resposta, '&lt;script&gt;alert(1)&lt;/script&gt;')
        self.assertNotContains(resposta, 'javascript:alert(2)')

    def test_pesquisa_invalida_nao_cria_registro(self):
        from painel.models import Pesquisa
        resposta = self.client.post(reverse('pesquisas'), {'termos': '  ', 'limite': '0'})
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(Pesquisa.objects.count(), 0)

    def test_filtros_tags_combinam_dimensoes_por_e(self):
        from painel.models import Empresa, Perfil, Tag
        ramo_a = Tag.objects.create(nome='Ramo A', tipo='ramo')
        ramo_b = Tag.objects.create(nome='Ramo B', tipo='ramo')
        categoria_a = Tag.objects.create(nome='Categoria A', tipo='categoria')
        categoria_b = Tag.objects.create(nome='Categoria B', tipo='categoria')
        empresa_a = Empresa.objects.create(nome='A', maps_id='filtro-a')
        empresa_b = Empresa.objects.create(nome='B', maps_id='filtro-b')
        Perfil.objects.get_or_create(empresa=empresa_a)[0].tags.add(ramo_a, categoria_a)
        Perfil.objects.get_or_create(empresa=empresa_b)[0].tags.add(ramo_b, categoria_b)
        resposta = self.client.get(reverse('contatos'), {
            'ramos': [ramo_a.pk, ramo_b.pk], 'categorias': [categoria_a.pk],
        })
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, '<strong>A</strong>')
        self.assertNotContains(resposta, '<strong>B</strong>')

    def test_ultima_data_sem_disparo_inclui_contatos_sem_historico(self):
        from painel.models import Empresa
        empresa = Empresa.objects.create(nome='Sem histórico', maps_id='sem-historico')
        resposta = self.client.get(reverse('contatos'), {
            'disparo': 'sem_disparo_desde', 'desde': '2026-10-04',
        })
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, 'Sem histórico')
        self.assertContains(resposta, 'Nunca')

    def test_historico_legado_nao_promove_aceita_a_entregue(self):
        from painel.models import Reserva
        Reserva.objects.create(empresa_id=808, session_id='sessao-teste', telefone='5546999110028',
                              mensagem='Sintética', mensagem_hash='hash-antigo', status='aceita')
        resposta = self.client.get(reverse('historico'))
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, 'Aceita · confirmação não registrada')
        self.assertNotContains(resposta, 'Entregue')

    def test_inicio_de_campanha_sem_destinatarios_volta_com_mensagem(self):
        from painel.models import Campanha
        campanha = Campanha.objects.create(nome='Vazia', texto_snapshot='Oi', limite=1,
                                           session_id='sessao-teste')
        resposta = self.client.post(reverse('campanha_acao', kwargs={'pk': campanha.pk}),
                                    {'acao': 'iniciar'}, follow=True)
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, 'não possui destinatários')
        campanha.refresh_from_db()
        self.assertEqual(campanha.estado, Campanha.Estado.RASCUNHO)

    def test_previa_identifica_destinatario_bloqueado_por_repeticao(self):
        from painel.models import Campanha, Destinatario, Empresa
        empresa = Empresa.objects.create(nome='Contato bloqueado', maps_id='bloqueado-ui')
        campanha = Campanha.objects.create(nome='Prévia', texto_snapshot='Oi', limite=1,
                                           session_id='sessao-teste')
        Destinatario.objects.create(campanha=campanha, empresa=empresa, ordem=1,
                                    telefone='5546999110028', mensagem='Oi',
                                    mensagem_hash='hash-sintetico',
                                    bloqueado_motivo='Telefone já confirmado em campanha anterior')
        resposta = self.client.get(reverse('campanha_detalhe', kwargs={'pk': campanha.pk}))
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, 'Repetição bloqueada')
        self.assertContains(resposta, 'Telefone já confirmado em campanha anterior')

    def test_prepara_snapshot_de_campanha_sem_iniciar_envios(self):
        from painel.models import Campanha, Empresa, Perfil, Tag, Texto
        ramo = Tag.objects.create(nome='Oficina', tipo='ramo')
        empresa = Empresa.objects.create(nome='Oficina do Bairro', telefone='46991160028',
                                         endereco='Centro', maps_id='preview-ui')
        Perfil.objects.get_or_create(empresa=empresa)[0].tags.add(ramo)
        texto = Texto.objects.create(nome='Apresentação', corpo='Olá, {nome}!')
        with patch('painel.services.integration.configuracao_publica',
                   return_value={'configurada': True, 'session_id': 'sessao-teste'}):
            resposta = self.client.post(reverse('campanhas'), {
                'nome': 'Prévia teste', 'texto': texto.pk, 'mensagem': '',
                'tags': [ramo.pk], 'cidade': 'Centro', 'com_telefone': 'sim',
                'ultimo_disparo': 'sem_disparo_desde', 'desde': '2026-10-04',
                'limite': 5, 'intervalo': 5,
            })
        self.assertEqual(resposta.status_code, 302)
        campanha = Campanha.objects.get(nome='Prévia teste')
        self.assertEqual(campanha.estado, Campanha.Estado.RASCUNHO)
        self.assertEqual(campanha.filtros_snapshot['desde'], '2026-10-04')
        self.assertIs(campanha.filtros_snapshot['com_telefone'], True)
        self.assertEqual(campanha.destinatarios.get().mensagem, 'Olá, Oficina do Bairro!')


    def test_get_campanhas_apresenta_modos_e_grupos(self):
        from painel.models import Empresa, Perfil, Tag
        ramo = Tag.objects.create(nome='Mecanica', tipo='ramo')
        empresa = Empresa.objects.create(nome='Oficina sintetica', telefone='46991160028', maps_id='grupo-get')
        Perfil.objects.get_or_create(empresa=empresa)[0].tags.add(ramo)
        resposta = self.client.get(reverse('campanhas'))
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, 'Quantidade por grupo')
        self.assertContains(resposta, 'Escolher clientes')
        self.assertContains(resposta, 'Mecanica')
        self.assertContains(resposta, 'Oficina sintetica')

    def test_atualizar_lista_preserva_selecao_nome_cotas_sem_campanha(self):
        from painel.models import Campanha, Empresa, Perfil, Tag
        ramo = Tag.objects.create(nome='Mecanica', tipo='ramo')
        empresa = Empresa.objects.create(nome='Oficina sintetica', maps_id='atualizar-ui')
        Perfil.objects.get_or_create(empresa=empresa)[0].tags.add(ramo)
        resposta = self.client.post(reverse('campanhas'), {
            'acao': 'atualizar', 'modo': 'manual', 'empresa_ids': [empresa.pk],
            'nome': 'Minha selecao', 'cidade': 'Outro local', 'com_telefone': 'sim',
            'ultimo_disparo': '', 'desde': '', 'mensagem': '', 'intervalo': '13',
            'quantidade_tag_%s' % ramo.pk: '2',
        })
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(Campanha.objects.count(), 0)
        self.assertContains(resposta, 'Minha selecao')
        self.assertContains(resposta, 'Selecionados fora do filtro')
        self.assertContains(resposta, 'value="13"')
        self.assertContains(resposta, 'value="2"')
        self.assertContains(resposta, f'value="{empresa.pk}" data-empresa-id="{empresa.pk}" checked')

    def test_preparar_manual_cria_apenas_rascunho_com_snapshot(self):
        from painel.models import Campanha, Empresa, Perfil, Tag
        ramo = Tag.objects.create(nome='Mecanica', tipo='ramo')
        empresa = Empresa.objects.create(nome='Oficina sintetica', telefone='46991160028', maps_id='manual-ui')
        Perfil.objects.get_or_create(empresa=empresa)[0].tags.add(ramo)
        with patch('painel.services.integration.configuracao_publica',
                   return_value={'configurada': True, 'session_id': 'sessao-falsa'}):
            resposta = self.client.post(reverse('campanhas'), {
                'acao': 'preparar', 'modo': 'manual', 'empresa_ids': [empresa.pk],
                'nome': 'Manual', 'mensagem': 'Ola {nome}', 'texto': '', 'cidade': '',
                'com_telefone': 'sim', 'ultimo_disparo': '', 'desde': '', 'intervalo': '5',
            })
        self.assertEqual(resposta.status_code, 302)
        campanha = Campanha.objects.get(nome='Manual')
        self.assertEqual(campanha.estado, Campanha.Estado.RASCUNHO)
        self.assertEqual(campanha.destinatarios.count(), 1)
        self.assertEqual(campanha.filtros_snapshot['selecao']['modo'], 'manual')

    def test_preparar_vazio_nao_seleciona_todos(self):
        from painel.models import Campanha, Empresa
        Empresa.objects.create(nome='Nao selecionar', telefone='46991160028', maps_id='vazio-ui')
        resposta = self.client.post(reverse('campanhas'), {
            'acao': 'preparar', 'modo': 'manual', 'empresa_ids': [], 'nome': 'Vazia',
            'mensagem': 'Oi', 'cidade': '', 'com_telefone': 'sim', 'ultimo_disparo': '',
            'desde': '', 'intervalo': '5',
        })
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(Campanha.objects.count(), 0)

    def test_preparar_quantidades_cria_rascunho_e_rejeita_cota_invalida(self):
        from painel.models import Campanha, Empresa, Perfil, Tag
        ramo = Tag.objects.create(nome='Mecanica', tipo='ramo')
        empresa = Empresa.objects.create(nome='Oficina sintetica', telefone='46991160028', maps_id='cota-ui')
        Perfil.objects.get_or_create(empresa=empresa)[0].tags.add(ramo)
        base = {'acao': 'preparar', 'modo': 'quantidades', 'nome': 'Cotas',
                'mensagem': 'Oi {nome}', 'texto': '', 'cidade': '', 'com_telefone': 'sim',
                'ultimo_disparo': '', 'desde': '', 'intervalo': '5'}
        with patch('painel.services.integration.configuracao_publica',
                   return_value={'configurada': True, 'session_id': 'sessao-falsa'}):
            resposta = self.client.post(reverse('campanhas'), {**base, 'quantidade_tag_%s' % ramo.pk: '1'})
        self.assertEqual(resposta.status_code, 302)
        campanha = Campanha.objects.get(nome='Cotas')
        self.assertEqual(campanha.estado, Campanha.Estado.RASCUNHO)
        self.assertEqual(campanha.limite, 1)
        with patch('painel.services.integration.configuracao_publica',
                   return_value={'configurada': True, 'session_id': 'sessao-falsa'}):
            resposta = self.client.post(reverse('campanhas'), {**base, 'nome': 'Invalida',
                'quantidade_tag_%s' % ramo.pk: '-1'})
        self.assertEqual(resposta.status_code, 200)
        self.assertFalse(Campanha.objects.filter(nome='Invalida').exists())

    def test_payload_contraditorio_e_desde_sem_data_sao_rejeitados(self):
        from painel.models import Campanha, Empresa
        empresa = Empresa.objects.create(nome='Marcada', maps_id='contraditorio-ui')
        resposta = self.client.post(reverse('campanhas'), {
            'acao': 'preparar', 'modo': 'quantidades', 'empresa_ids': [empresa.pk],
            'nome': 'Contraditoria', 'mensagem': 'Oi', 'com_telefone': 'sim',
            'ultimo_disparo': 'sem_disparo_desde', 'desde': '', 'intervalo': '5',
        })
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(Campanha.objects.count(), 0)

    def test_atualizar_preserva_cotas_inativas_em_modo_manual(self):
        import json
        from painel.models import Campanha, Empresa, Tag
        tag = Tag.objects.create(nome='Cota preservada', tipo='ramo')
        empresa = Empresa.objects.create(nome='Manual preservada', maps_id='preserva-manual')
        resposta = self.client.post(reverse('campanhas'), {
            'acao': 'atualizar', 'modo': 'manual', 'empresa_ids': [empresa.pk],
            'preservar_quantidades': json.dumps({f'quantidade_tag_{tag.pk}': '4'}),
            'nome': '', 'mensagem': '', 'intervalo': '', 'cidade': '', 'com_telefone': 'sim',
            'ultimo_disparo': '', 'desde': '',
        })
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(Campanha.objects.count(), 0)
        self.assertContains(resposta, f'id="id_quantidade_tag_{tag.pk}"')
        self.assertContains(resposta, 'value="4"')
        self.assertContains(resposta, 'value="%s" data-empresa-id="%s" checked' % (empresa.pk, empresa.pk))
        self.assertNotContains(resposta, 'Selecione um texto salvo ou escreva a mensagem.')

    def test_atualizar_preserva_ids_inativos_em_modo_quantidades(self):
        import json
        from painel.models import Campanha, Empresa, Perfil, Tag
        tag = Tag.objects.create(nome='Cota ativa', tipo='ramo')
        empresa = Empresa.objects.create(nome='Manual restaurada', maps_id='preserva-quantidades')
        Perfil.objects.get_or_create(empresa=empresa)[0].tags.add(tag)
        resposta = self.client.post(reverse('campanhas'), {
            'acao': 'atualizar', 'modo': 'quantidades', f'quantidade_tag_{tag.pk}': '2',
            'preservar_empresa_ids': json.dumps([empresa.pk]), 'nome': '', 'mensagem': '',
            'intervalo': '', 'cidade': '', 'com_telefone': '', 'ultimo_disparo': '', 'desde': '',
        })
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(Campanha.objects.count(), 0)
        self.assertContains(resposta, f'value="{empresa.pk}" data-empresa-id="{empresa.pk}" checked')
        self.assertContains(resposta, 'Marcadas neste grupo: <span data-group-count="tag:%s">1</span>' % tag.pk)
        self.assertContains(resposta, 'value="2"')

    def test_selecao_nova_recusa_tag_legada_disjunta(self):
        from painel.models import Campanha, Empresa, Perfil, Tag
        selecionado = Tag.objects.create(nome='Grupo alvo', tipo='ramo')
        antigo = Tag.objects.create(nome='Filtro legado', tipo='ramo')
        empresa = Empresa.objects.create(nome='Do grupo alvo', telefone='46991160028', maps_id='tag-alvo')
        Perfil.objects.get_or_create(empresa=empresa)[0].tags.add(selecionado)
        resposta = self.client.post(reverse('campanhas'), {
            'acao': 'preparar', 'modo': 'quantidades', f'quantidade_tag_{selecionado.pk}': '1',
            'tags': [antigo.pk], 'nome': 'Nova', 'mensagem': 'Oi', 'intervalo': '5',
            'cidade': '', 'com_telefone': 'sim', 'ultimo_disparo': '', 'desde': '',
        })
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(Campanha.objects.count(), 0)
        self.assertContains(resposta, 'Não combine tags legadas com uma seleção por grupos.')

    def test_erros_de_cota_e_modo_sao_visiveis(self):
        from painel.models import Empresa, Tag
        tag = Tag.objects.create(nome='Erro de cota', tipo='ramo')
        Empresa.objects.create(nome='Sintética', telefone='46991160028', maps_id='erro-cota')
        base = {'acao': 'preparar', 'modo': 'quantidades', f'quantidade_tag_{tag.pk}': '-1',
                'quantidade_sem_classificacao': '1', 'nome': 'Inválida', 'mensagem': 'Oi',
                'intervalo': '5', 'cidade': '', 'com_telefone': 'sim', 'ultimo_disparo': '', 'desde': ''}
        resposta = self.client.post(reverse('campanhas'), base)
        self.assertEqual(resposta.status_code, 200)
        erro_cota = resposta.context['form'].errors[f'quantidade_tag_{tag.pk}'][0]
        self.assertContains(resposta, str(erro_cota))
        resposta = self.client.post(reverse('campanhas'), {**base, 'modo': 'modo-desconhecido'})
        self.assertEqual(resposta.status_code, 200)
        erro_modo = resposta.context['form'].errors['modo'][0]
        self.assertContains(resposta, str(erro_modo))

    def test_atualizar_nao_exige_validacao_nativa_de_conteudo(self):
        resposta = self.client.get(reverse('campanhas'))
        self.assertContains(resposta, 'name="acao" value="atualizar" formnovalidate')

    def test_erro_de_mensagem_no_preparo_manual_preserva_cotas_inativas(self):
        import json
        from painel.models import Campanha, Empresa, Tag
        tag = Tag.objects.create(nome='Cota no erro manual', tipo='ramo')
        empresa = Empresa.objects.create(nome='Marcada no erro', maps_id='erro-preserva-manual')
        resposta = self.client.post(reverse('campanhas'), {
            'acao': 'preparar', 'modo': 'manual', 'empresa_ids': [empresa.pk],
            'preservar_quantidades': json.dumps({f'quantidade_tag_{tag.pk}': '3'}),
            'nome': 'Teste inválido', 'mensagem': 'Oi {campo_inexistente}', 'intervalo': '5',
            'cidade': '', 'com_telefone': '', 'ultimo_disparo': '', 'desde': '',
        })
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(Campanha.objects.count(), 0)
        self.assertContains(resposta, f'id="id_quantidade_tag_{tag.pk}"')
        self.assertContains(resposta, 'value="3"')
        self.assertContains(resposta, f'value="{empresa.pk}" data-empresa-id="{empresa.pk}" checked')

    def test_erro_de_cota_no_preparo_preserva_ids_manuais_inativos(self):
        import json
        from painel.models import Campanha, Empresa, Perfil, Tag
        tag = Tag.objects.create(nome='Cota inválida', tipo='ramo')
        empresa = Empresa.objects.create(nome='Marcada na cota inválida', maps_id='erro-preserva-cotas')
        Perfil.objects.get_or_create(empresa=empresa)[0].tags.add(tag)
        resposta = self.client.post(reverse('campanhas'), {
            'acao': 'preparar', 'modo': 'quantidades', f'quantidade_tag_{tag.pk}': '-1',
            'quantidade_sem_classificacao': '1', 'preservar_empresa_ids': json.dumps([empresa.pk]),
            'nome': 'Cota inválida', 'mensagem': 'Oi', 'intervalo': '5', 'cidade': '',
            'com_telefone': '', 'ultimo_disparo': '', 'desde': '',
        })
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(Campanha.objects.count(), 0)
        self.assertContains(resposta, f'value="{empresa.pk}" data-empresa-id="{empresa.pk}" checked')
        erro_cota = resposta.context['form'].errors[f'quantidade_tag_{tag.pk}'][0]
        self.assertContains(resposta, str(erro_cota))

    def test_cotas_negativa_fracionaria_acima_limite_e_grupo_desconhecido_recusadas(self):
        from painel.models import Campanha, Tag
        ramo = Tag.objects.create(nome='Cota', tipo='ramo')
        base = {'acao': 'preparar', 'modo': 'quantidades', 'nome': 'Inválida',
                'mensagem': 'Oi', 'com_telefone': 'sim', 'ultimo_disparo': '', 'desde': '', 'intervalo': '5'}
        for valor in ('-1', '1.5', '1001'):
            with self.subTest(valor=valor):
                resposta = self.client.post(reverse('campanhas'), {
                    **base, f'quantidade_tag_{ramo.pk}': valor,
                    'quantidade_sem_classificacao': '1' if valor == '1001' else '',
                })
                self.assertEqual(resposta.status_code, 200)
        resposta = self.client.post(reverse('campanhas'), {**base, 'quantidade_tag_999999': '1'})
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(Campanha.objects.count(), 0)

    def test_manual_com_id_inexistente_e_nome_externo_escapado_na_previa(self):
        from painel.models import Campanha, Destinatario, Empresa
        resposta = self.client.post(reverse('campanhas'), {
            'acao': 'preparar', 'modo': 'manual', 'empresa_ids': ['999999'],
            'nome': 'Inválida', 'mensagem': 'Oi', 'com_telefone': 'sim',
            'ultimo_disparo': '', 'desde': '', 'intervalo': '5',
        })
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(Campanha.objects.count(), 0)
        empresa = Empresa.objects.create(nome='<script>alert(1)</script>', telefone='46991160028', maps_id='xss-campanha')
        campanha = Campanha.objects.create(nome='&lt;script&gt;nome', texto_snapshot='Oi',
            filtros_snapshot={'selecao': {'modo': 'manual', 'empresa_ids': [empresa.pk],
                'grupos': [{'tipo': 'ramo', 'nome': '<img src=x onerror=alert(1)>', 'solicitado': 1, 'preparado': 1}],
                'atribuicoes': []}}, limite=1, session_id='sessao-falsa')
        Destinatario.objects.create(campanha=campanha, empresa=empresa, ordem=1,
            telefone='5546999110028', mensagem='Oi', mensagem_hash='hash-falso')
        detalhe = self.client.get(reverse('campanha_detalhe', kwargs={'pk': campanha.pk}))
        self.assertEqual(detalhe.status_code, 200)
        self.assertNotContains(detalhe, '<img src=x onerror=alert(1)>')
        self.assertContains(detalhe, '&lt;img src=x onerror=alert(1)&gt;')


class FormValidationTests(TestCase):
    def test_site_e_avaliacao_sao_validados_no_servidor(self):
        from painel.forms import EmpresaForm
        from painel.models import Empresa
        empresa = Empresa.objects.create(nome='Teste', maps_id='form-site')
        form = EmpresaForm(data={'nome': 'Teste', 'endereco': '', 'telefone': '',
                                 'site': 'http://[', 'avaliacao': '6', 'tags': []},
                           instance=empresa)
        self.assertFalse(form.is_valid())
        self.assertIn('site', form.errors)
        self.assertIn('avaliacao', form.errors)

    def test_template_de_mensagem_desconhecido_e_rejeitado(self):
        from painel.forms import CampanhaForm
        form = CampanhaForm(data={'nome': 'Teste', 'texto': '', 'mensagem': 'Oi {campo_invalido}',
                                  'tags': [], 'cidade': '', 'com_telefone': '',
                                  'ultimo_disparo': '', 'desde': '', 'limite': 5, 'intervalo': 5})
        self.assertFalse(form.is_valid())
        self.assertIn('mensagem', form.errors)

    def test_edicao_recalcula_chave_de_deduplicacao(self):
        import json
        from painel.models import Empresa
        empresa = Empresa.objects.create(nome='Oficina X', endereco='Rua antiga', maps_id='edit-chave')
        resposta = self.client.post(reverse('contato_editar', kwargs={'pk': empresa.pk}), {
            'nome': 'Oficina X', 'endereco': 'Rua nova', 'telefone': '', 'site': '',
            'avaliacao': '', 'tags': [],
        })
        self.assertEqual(resposta.status_code, 302)
        empresa.refresh_from_db()
        self.assertEqual(empresa.chave, json.dumps(['oficina x', 'rua nova']))

    def test_detalhe_de_contato_mostra_origens_de_pesquisa(self):
        from painel.models import Empresa, Pesquisa, ResultadoPesquisa
        empresa = Empresa.objects.create(nome='Origem sintética', maps_id='origem-ui')
        pesquisa = Pesquisa.objects.create(termos='cafeteria em Curitiba')
        ResultadoPesquisa.objects.create(empresa=empresa, pesquisa=pesquisa,
                                         termo='cafeteria em Curitiba', url='https://maps.test/ficha')
        resposta = self.client.get(reverse('contato_editar', kwargs={'pk': empresa.pk}))
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, 'Origens de pesquisa')
        self.assertContains(resposta, 'cafeteria em Curitiba')


class CsrfTests(TestCase):
    @override_settings(DEBUG=False)
    def test_post_sem_token_csrf_e_recusado(self):
        client = Client(enforce_csrf_checks=True)
        resposta = client.post(reverse('conectar'))
        self.assertEqual(resposta.status_code, 403)

    def test_atualizar_e_preparar_exigem_csrf(self):
        client = Client(enforce_csrf_checks=True)
        for acao in ('atualizar', 'preparar'):
            with self.subTest(acao=acao):
                resposta = client.post(reverse('campanhas'), {'acao': acao})
                self.assertEqual(resposta.status_code, 403)

