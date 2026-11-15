from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST

from . import forms


def _conexao():
    try:
        from .services.integration import estado_conexao
        estado = estado_conexao()
        return estado if isinstance(estado, dict) else {'conectado': bool(estado)}
    except (ImportError, RuntimeError, OSError, ValueError):
        return {}


def _base(request, secao, **contexto):
    estado = _conexao()
    conectado = estado.get('conectado', estado.get('connected', estado.get('status') in ('CONNECTED', 'connected', 'conectado')))
    return {'secao': secao, 'conectado': conectado, **contexto}


def _rotular_tentativa(tentativa):
    tentativa.estado_legivel = {
        'aceita': 'Aceita, aguardando confirmação',
        'incerto': 'Pendente de verificação',
    }.get(tentativa.estado, tentativa.get_estado_display())
    return tentativa


@require_GET
def dashboard(request):
    from .models import Campanha, Empresa, Pesquisa
    return render(request, 'painel/dashboard.html', _base(request, 'inicio',
        total_empresas=Empresa.objects.count(), total_pesquisas=Pesquisa.objects.count(),
        campanhas_ativas=Campanha.objects.filter(estado__in=('ativa', 'pausada')).count(),
        pesquisas_recentes=Pesquisa.objects.order_by('-criada_em')[:5]))


def pesquisas(request):
    from .models import Pesquisa
    if request.method == 'POST':
        form = forms.PesquisaForm(request.POST)
        if form.is_valid():
            pesquisa = Pesquisa.objects.create(termos=form.cleaned_data['termos'], limite=form.cleaned_data['limite'])
            messages.success(request, 'Pesquisa adicionada à fila. O trabalhador local fará a captura.')
            return redirect('pesquisas')
    else:
        form = forms.PesquisaForm()
    return render(request, 'painel/pesquisas.html', _base(request, 'pesquisas', form=form,
        pesquisas=Pesquisa.objects.order_by('-criada_em'), titulo='Pesquisas'))


@require_GET
def contatos(request):
    from django.db.models import Max
    from mensagens_openwa import normalizar_telefone
    from .models import Empresa, Tentativa
    form = forms.ContatoFiltroForm(request.GET or None)
    empresas = Empresa.objects.select_related('perfil').prefetch_related('perfil__tags').order_by('nome')
    if form.is_valid():
        dados = form.cleaned_data
        if dados.get('nome'):
            empresas = empresas.filter(nome__icontains=dados['nome'])
        from .services.campaigns import filtrar_contatos
        filtros = {
            'ramos': [tag.nome for tag in dados.get('ramos', [])],
            'categorias': [tag.nome for tag in dados.get('categorias', [])],
            'cidade': dados.get('cidade', '').strip(),
            'com_telefone': {'sim': True, 'nao': False}.get(dados.get('telefone')),
            'ultimo_disparo': {'nunca': 'nunca', 'sem_disparo_desde': 'sem_disparo_desde'}.get(dados.get('disparo')),
            'desde': dados['desde'].isoformat() if dados.get('desde') else None,
        }
        elegiveis = filtrar_contatos(filtros)
        empresas = empresas.filter(pk__in=[item.pk for item in elegiveis])
    empresas = list(empresas)
    telefones = {}
    for empresa in empresas:
        try:
            if empresa.telefone:
                telefones[empresa.pk] = normalizar_telefone(empresa.telefone)
        except ValueError:
            pass
    datas = dict(Tentativa.objects.filter(
        telefone__in=set(telefones.values()),
        estado__in=(Tentativa.Estado.ENVIADO, Tentativa.Estado.ENTREGUE, Tentativa.Estado.LIDO),
    ).values('telefone').annotate(ultimo=Max('confirmado_em')).values_list('telefone', 'ultimo'))
    for empresa in empresas:
        empresa.ultimo_disparo_ui = datas.get(telefones.get(empresa.pk))
    return render(request, 'painel/contatos.html', _base(request, 'contatos', form=form,
        empresas=empresas))


def contato_editar(request, pk):
    import json
    import unicodedata
    from django.db import IntegrityError
    from .models import Empresa, Perfil
    from .services.contacts import editar_tags
    empresa = get_object_or_404(Empresa, pk=pk)
    if request.method == 'POST':
        form = forms.EmpresaForm(request.POST, instance=empresa)
        if form.is_valid():
            atualizado = form.save(commit=False)
            normalizar = lambda texto: ' '.join(unicodedata.normalize('NFKC', texto or '').casefold().split())
            atualizado.chave = (json.dumps([normalizar(atualizado.nome), normalizar(atualizado.endereco)])
                                if atualizado.endereco else None)
            duplicado = atualizado.chave and Empresa.objects.exclude(pk=empresa.pk).filter(chave=atualizado.chave).exists()
            if duplicado:
                form.add_error(None, 'Já existe uma empresa com o mesmo nome e endereço normalizados.')
            else:
                try:
                    atualizado.save()
                except IntegrityError:
                    form.add_error(None, 'Não foi possível salvar: existe um contato com esta identidade.')
                else:
                    editar_tags(atualizado, form.cleaned_data['tags'])
                    messages.success(request, 'Contato atualizado.')
                    return redirect('contatos')
    else:
        try:
            tags_atuais = empresa.perfil.tags.all()
        except Perfil.DoesNotExist:
            tags_atuais = []
        form = forms.EmpresaForm(instance=empresa, initial={'tags': tags_atuais})
    resultados = empresa.resultados.select_related('pesquisa').order_by('-capturado_em')
    return render(request, 'painel/contato_editar.html', _base(request, 'contatos', form=form,
        empresa=empresa, resultados=resultados))


def textos(request):
    from .models import Texto
    if request.method == 'POST':
        texto = get_object_or_404(Texto, pk=request.POST.get('arquivar'))
        texto.arquivado = True
        texto.save(update_fields=['arquivado', 'atualizado_em'])
        messages.success(request, 'Texto arquivado.')
        return redirect('textos')
    return render(request, 'painel/textos.html', _base(request, 'textos',
        textos=Texto.objects.order_by('arquivado', '-atualizado_em'), form=forms.TextoForm()))


def texto_editar(request, pk=None):
    from .models import Texto
    texto = get_object_or_404(Texto, pk=pk) if pk else None
    form = forms.TextoForm(request.POST or None, instance=texto)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Texto salvo.')
        return redirect('textos')
    return render(request, 'painel/form.html', _base(request, 'textos', form=form,
        titulo='Editar texto' if texto else 'Novo texto', descricao='Campos disponíveis: {nome}, {endereco}, {telefone}, {site} e {avaliacao}.',
        submit_label='Salvar texto', voltar_url='/textos/'))


def campanhas(request):
    import json
    from .models import Campanha, Empresa
    from .services.selection import listar_grupos

    def filtros_de(dados):
        return {
            'cidade': (dados.get('cidade') or '').strip(),
            'com_telefone': {'sim': True, 'nao': False}.get(dados.get('com_telefone')),
            'ultimo_disparo': dados.get('ultimo_disparo') or None,
            'desde': dados['desde'].isoformat() if dados.get('desde') else None,
        }

    form = forms.CampanhaForm(request.POST if request.method == 'POST' else None)
    acao = (request.POST.get('acao') or '') if request.method == 'POST' else ''
    modo = request.POST.get('modo', '') if request.method == 'POST' else ''
    filtro_form = forms.CampanhaFiltroForm(request.POST if request.method == 'POST' else None)
    atualizar = acao == 'atualizar'
    preparar = acao == 'preparar' or (request.method == 'POST' and not acao)
    selecao_ids = []
    selecao_invalida = False
    if request.method == 'POST':
        ids_ativos = request.POST.getlist('empresa_ids')
        ids_preservados = None
        ids_preservados_invalidos = False
        if request.POST.get('preservar_empresa_ids'):
            try:
                ids_preservados = json.loads(request.POST['preservar_empresa_ids'])
                if not isinstance(ids_preservados, list):
                    ids_preservados = []
                    ids_preservados_invalidos = True
            except (TypeError, ValueError):
                ids_preservados = []
                ids_preservados_invalidos = True
        if atualizar and ids_preservados_invalidos:
            selecao_invalida = True
        ids_para_renderizar = (ids_preservados if modo == 'quantidades' and ids_preservados is not None
                               else ids_ativos)
        for valor in ids_para_renderizar:
            try:
                selecao_ids.append(int(valor))
            except (TypeError, ValueError):
                selecao_invalida = True
        selecao_ids = list(dict.fromkeys(selecao_ids))

    quantidades_estado = {}
    if request.method == 'POST':
        if request.POST.get('preservar_quantidades'):
            try:
                preservadas = json.loads(request.POST['preservar_quantidades'])
                if isinstance(preservadas, dict):
                    quantidades_estado.update({
                        chave: str(valor) for chave, valor in preservadas.items()
                        if isinstance(chave, str) and chave.startswith('quantidade_')
                        and isinstance(valor, (str, int)) and len(str(valor)) <= 4
                    })
            except (TypeError, ValueError):
                pass
        for campo_cota in form.campos_cota:
            if campo_cota in request.POST:
                quantidades_estado[campo_cota] = request.POST.get(campo_cota, '')

    filtros = {'com_telefone': True} if request.method == 'GET' else {}
    grupos = listar_grupos(filtros)
    selecionados_fora_filtro = []
    erros_filtro = {}
    erros_atualizacao = []
    if request.method == 'POST' and acao not in ('', 'atualizar', 'preparar'):
        form.add_error(None, 'A ação solicitada não é válida.')
    if request.method == 'POST' and (atualizar or preparar):
        if filtro_form.is_valid():
            filtros = filtros_de(filtro_form.cleaned_data)
            grupos = listar_grupos(filtros)
        else:
            erros_filtro = {campo: list(erros) for campo, erros in filtro_form.errors.items()}
        if selecao_ids:
            existentes = {empresa.pk: empresa for empresa in Empresa.objects.filter(pk__in=selecao_ids)}
            fora_ids = set(selecao_ids) - {empresa.pk for grupo in grupos for empresa in grupo['empresas']}
            selecionados_fora_filtro = [existentes[pk] for pk in selecao_ids if pk in fora_ids and pk in existentes]
            if atualizar and (len(existentes) != len(selecao_ids) or selecao_invalida):
                erro = 'A lista contém ID de empresa inválido ou inexistente.'
                erros_atualizacao.append(erro)
        elif atualizar and selecao_invalida:
            erro = 'A lista contém ID de empresa inválido ou inexistente.'
            erros_atualizacao.append(erro)

    if preparar and filtro_form.is_valid() and form.is_valid():
        from .services.campaigns import preparar_campanha
        from .services.integration import configuracao_publica
        dados = form.cleaned_data
        texto = dados['texto'] or dados['mensagem'].strip()
        configuracao = configuracao_publica()
        selecao_validada = dados.get('selecao')
        selecao = None
        if selecao_validada:
            selecao = ({'modo': selecao_validada['modo'], 'empresa_ids': selecao_validada['empresa_ids']}
                       if selecao_validada['modo'] == 'manual' else
                       {'modo': selecao_validada['modo'], 'quantidades': selecao_validada['quantidades']})
        if selecao is None:
            selecionadas = list(dados.get('tags', []))
            filtros.update({
                'ramos': [tag.nome for tag in selecionadas if tag.tipo == 'ramo'],
                'categorias': [tag.nome for tag in selecionadas if tag.tipo == 'categoria'],
            })
        limite = dados.get('limite') or (selecao_validada or {}).get('total')
        try:
            campanha = preparar_campanha(dados['nome'], texto, filtros, limite,
                configuracao.get('session_id', ''), dados['intervalo'], selecao=selecao)
        except (ValueError, TypeError) as erro:
            form.add_error(None, str(erro))
        else:
            messages.success(request, 'Prévia preparada. Revise cada destinatário antes de iniciar.')
            return redirect('campanha_detalhe', pk=campanha.pk)

    grupos_ui = []
    for grupo in grupos:
        item = dict(grupo)
        campo_cota = 'quantidade_' + grupo['chave'].replace(':', '_')
        item['campo_cota'] = form[campo_cota]
        item['campo_cota_nome'] = campo_cota
        item['cota_atual'] = quantidades_estado.get(campo_cota, '')
        item['selecionados'] = len({
            empresa.pk for empresa in grupo['empresas'] if empresa.pk in selecao_ids
        })
        item['erros_cota'] = [] if atualizar else form.errors.get(campo_cota, [])
        grupos_ui.append(item)

    erros_modo = [] if atualizar else form.errors.get('modo', [])
    preservar_quantidades_json = json.dumps(quantidades_estado, ensure_ascii=False)
    preservar_empresa_ids_json = json.dumps(selecao_ids)
    return render(request, 'painel/campanhas.html', _base(request, 'campanhas', form=form,
        campanhas=Campanha.objects.order_by('-criado_em'), grupos=grupos_ui,
        selecionados_fora_filtro=selecionados_fora_filtro, modo=modo or 'quantidades', acao=acao,
        selecao_ids=set(selecao_ids), erros_filtro=erros_filtro,
        erros_atualizacao=erros_atualizacao, erros_modo=erros_modo,
        preservar_quantidades_json=preservar_quantidades_json,
        preservar_empresa_ids_json=preservar_empresa_ids_json))


@require_GET
def campanha_detalhe(request, pk):
    from .models import Campanha
    campanha = get_object_or_404(Campanha.objects.prefetch_related(
        'destinatarios__empresa', 'destinatarios__tentativas__eventos'), pk=pk)
    destinatarios = list(campanha.destinatarios.all())
    tentativas = []
    for destinatario in destinatarios:
        tentativas_item = [_rotular_tentativa(item) for item in destinatario.tentativas.order_by('-criado_em')]
        destinatario.tentativa_recente = tentativas_item[0] if tentativas_item else None
        tentativas.extend(tentativas_item)
    resumo_selecao = campanha.filtros_snapshot.get('selecao')
    empresas_selecionadas = set()
    if resumo_selecao:
        if resumo_selecao.get('modo') == 'manual':
            empresas_selecionadas = set(resumo_selecao.get('empresa_ids', []))
        else:
            empresas_selecionadas = {
                empresa_id for destinatario in destinatarios
                for empresa_id in (destinatario.empresas_relacionadas or [destinatario.empresa_id])
            }
    return render(request, 'painel/campanha_detalhe.html', _base(request, 'campanhas', campanha=campanha,
        destinatarios=destinatarios, tentativas=tentativas, resumo_selecao=resumo_selecao,
        empresas_selecionadas=len(empresas_selecionadas)))


@require_POST
def campanha_acao(request, pk):
    from .models import Campanha
    campanha = get_object_or_404(Campanha, pk=pk)
    acao = request.POST.get('acao')
    if acao == 'iniciar' and campanha.estado == Campanha.Estado.RASCUNHO:
        from .services.campaigns import iniciar_campanha
        try:
            iniciar_campanha(campanha.pk)
        except ValueError as erro:
            messages.error(request, str(erro))
        else:
            messages.success(request, 'Campanha iniciada.')
    elif acao == 'pausar' and campanha.estado == Campanha.Estado.ATIVA:
        campanha.estado = Campanha.Estado.PAUSADA
        campanha.motivo_pausa = 'Pausada pelo usuário.'
        campanha.save(update_fields=['estado', 'motivo_pausa'])
        messages.success(request, 'Próximos envios pausados.')
    elif acao == 'retomar' and campanha.estado == Campanha.Estado.PAUSADA:
        campanha.estado = Campanha.Estado.ATIVA
        campanha.motivo_pausa = ''
        campanha.save(update_fields=['estado', 'motivo_pausa'])
        messages.success(request, 'Campanha retomada. O envio não começa automaticamente se o WhatsApp estiver desconectado.')
    else:
        messages.error(request, 'A ação não é válida para o estado atual da campanha.')
    return redirect('campanha_detalhe', pk=pk)


def historico(request):
    from .models import Reserva, Tentativa
    tentativas = Tentativa.objects.select_related('destinatario__empresa', 'destinatario__campanha').prefetch_related('eventos').order_by('-criado_em')
    tentativas = [_rotular_tentativa(item) for item in tentativas[:250]]
    reservas_antigas = Reserva.objects.exclude(
        pk__in=Tentativa.objects.values_list('reserva_id', flat=True)
    ).order_by('-criado_em')[:250]
    for reserva in reservas_antigas:
        reserva.estado_legivel = {
            'aceita': 'Aceita · confirmação não registrada',
            'enviado': 'Status legado · confirmação não verificada',
            'processando': 'Pendente de verificação',
            'incerto': 'Pendente de verificação',
        }.get((reserva.status or '').casefold(), reserva.status or 'Status legado')
    return render(request, 'painel/historico.html', _base(request, 'historico',
        tentativas=tentativas, reservas_antigas=reservas_antigas))


@require_GET
def whatsapp(request):
    from .services.integration import configuracao_publica, estado_conexao
    estado = estado_conexao()
    return render(request, 'painel/whatsapp.html', _base(request, 'whatsapp', estado=estado,
        configuracao=configuracao_publica()))


@require_GET
def qr(request):
    from .services.integration import obter_qr
    from mensagens_openwa import OpenWAError
    try:
        imagem = obter_qr()
    except (OpenWAError, ValueError, OSError):
        resposta = HttpResponse('QR Code indisponível no serviço local.', status=503,
                                content_type='text/plain; charset=utf-8')
    else:
        resposta = HttpResponse(imagem, content_type='image/png')
    resposta['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resposta['Pragma'] = 'no-cache'
    resposta['X-Content-Type-Options'] = 'nosniff'
    return resposta


@require_POST
def conectar(request):
    from .services.integration import conectar_sessao
    try:
        conectar_sessao()
    except (ValueError, OSError, RuntimeError) as erro:
        messages.error(request, str(erro))
    else:
        messages.success(request, 'Solicitação de conexão enviada ao serviço local. Atualize o estado em instantes.')
    return redirect('whatsapp')


def tags(request):
    from .models import Tag
    instancia = get_object_or_404(Tag, pk=request.GET.get('editar')) if request.GET.get('editar') else None
    if request.method == 'POST':
        if request.POST.get('excluir'):
            tag = get_object_or_404(Tag, pk=request.POST['excluir'])
            tag.delete()
            messages.success(request, 'Tag removida.')
        else:
            instancia = get_object_or_404(Tag, pk=request.POST['editar']) if request.POST.get('editar') else None
            form = forms.TagForm(request.POST, instance=instancia)
            if form.is_valid():
                form.save()
                messages.success(request, 'Tag salva.')
            else:
                return render(request, 'painel/tags.html', _base(request, 'tags', form=form, tags=Tag.objects.all(), regras=[], editando=instancia))
        return redirect('tags')
    return render(request, 'painel/tags.html', _base(request, 'tags', form=forms.TagForm(instance=instancia), tags=Tag.objects.all(), regras=[], editando=instancia))


def regras(request):
    from .models import RegraTag
    instancia = get_object_or_404(RegraTag, pk=request.GET.get('editar')) if request.GET.get('editar') else None
    if request.method == 'POST':
        if request.POST.get('excluir'):
            get_object_or_404(RegraTag, pk=request.POST['excluir']).delete()
            messages.success(request, 'Regra removida.')
        else:
            instancia = get_object_or_404(RegraTag, pk=request.POST['editar']) if request.POST.get('editar') else None
            form = forms.RegraTagForm(request.POST, instance=instancia)
            if form.is_valid():
                form.save()
                messages.success(request, 'Regra salva.')
            else:
                return render(request, 'painel/regras.html', _base(request, 'tags', form=form, regras=RegraTag.objects.prefetch_related('tags'), editando=instancia))
        return redirect('regras')
    return render(request, 'painel/regras.html', _base(request, 'tags', form=forms.RegraTagForm(instance=instancia), regras=RegraTag.objects.prefetch_related('tags'), editando=instancia))


@require_POST
def reenvio(request, pk):
    form = forms.ReenvioForm(request.POST)
    if not form.is_valid():
        messages.error(request, 'Informe um motivo válido para o reenvio.')
        return redirect('historico')
    from .services.delivery import autorizar_reenvio
    try:
        campanha = autorizar_reenvio(pk, form.cleaned_data['motivo'])
    except ValueError as erro:
        messages.error(request, str(erro))
        return redirect('historico')
    messages.success(request, 'Nova prévia de reenvio preparada. Revise antes de iniciar.')
    return redirect('campanha_detalhe', pk=campanha.pk)
