import hashlib
import math
from datetime import date, datetime, time

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from mensagens_openwa import normalizar_telefone, renderizar_mensagem
from painel.models import (
    Campanha, Destinatario, Empresa, Perfil, Reserva, Tag, Tentativa, Texto,
)


ESTADOS_CONFIRMADOS = (Tentativa.Estado.ENVIADO, Tentativa.Estado.ENTREGUE, Tentativa.Estado.LIDO)


def ultimo_disparo(telefone):
    try:
        numero = normalizar_telefone(telefone)
    except (ValueError, TypeError):
        return None
    return (Tentativa.objects.filter(telefone=numero, estado__in=ESTADOS_CONFIRMADOS)
            .aggregate(ultimo=Max('confirmado_em'))['ultimo'])


def _ultimo_disparo_empresa(empresa):
    telefones = set()
    try:
        if empresa.telefone:
            telefones.add(normalizar_telefone(empresa.telefone))
    except (TypeError, ValueError):
        pass
    if not telefones:
        return None
    return (Tentativa.objects.filter(telefone__in=telefones, estado__in=ESTADOS_CONFIRMADOS)
            .aggregate(ultimo=Max('confirmado_em'))['ultimo'])


def _lista(valor):
    if valor is None or valor == '':
        return []
    return list(valor) if isinstance(valor, (list, tuple, set)) else [valor]


def filtrar_contatos(filtros):
    filtros = filtros or {}
    empresas = list(Empresa.objects.all().order_by('id'))
    ramos = {str(valor).casefold() for valor in _lista(
        filtros.get('ramos', filtros.get('ramo_ids', filtros.get('ramo')))
    )}
    categorias = {str(valor).casefold() for valor in _lista(
        filtros.get('categorias', filtros.get('categoria_ids', filtros.get('categoria')))
    )}
    cidade = str(filtros.get('cidade') or '').strip().casefold()
    presenca = filtros.get('com_telefone', filtros.get('telefone'))
    if isinstance(presenca, str):
        if presenca.casefold() in ('true', '1', 'sim', 'com'):
            presenca = True
        elif presenca.casefold() in ('false', '0', 'não', 'nao', 'sem'):
            presenca = False
        else:
            presenca = None
    quando = filtros.get('ultimo_disparo')
    desde = filtros.get('desde') or filtros.get('sem_disparo_desde')

    saida = []
    for empresa in empresas:
        try:
            tags = list(empresa.perfil.tags.all())
        except Perfil.DoesNotExist:
            tags = []
        tags_ramo = {valor for tag in tags if tag.tipo == Tag.Tipo.RAMO
                     for valor in (tag.nome.casefold(), str(tag.pk))}
        tags_categoria = {valor for tag in tags if tag.tipo == Tag.Tipo.CATEGORIA
                          for valor in (tag.nome.casefold(), str(tag.pk))}
        if ramos and not tags_ramo.intersection(ramos):
            continue
        if categorias and not tags_categoria.intersection(categorias):
            continue
        if cidade and cidade not in (empresa.endereco or '').casefold():
            continue
        possui = bool(empresa.telefone and empresa.telefone.strip())
        if presenca is True and not possui or presenca is False and possui:
            continue
        ultimo = _ultimo_disparo_empresa(empresa)
        if quando in ('nunca', 'never') and ultimo is not None:
            continue
        if (quando in ('desde', 'sem_disparo_desde') or desde) and desde:
            limite = _parse_datetime(desde)
            if ultimo is not None and ultimo >= limite:
                continue
        saida.append(empresa)
    return saida


def _parse_datetime(valor):
    if isinstance(valor, datetime):
        return timezone.make_aware(valor, timezone.get_current_timezone()) if timezone.is_naive(valor) else valor
    if isinstance(valor, date):
        return timezone.make_aware(datetime.combine(valor, time.min), timezone.get_current_timezone())
    try:
        parsed = datetime.fromisoformat(str(valor))
    except ValueError as erro:
        raise ValueError('A data do filtro de último disparo é inválida.') from erro
    return timezone.make_aware(parsed, timezone.get_current_timezone()) if timezone.is_naive(parsed) else parsed


def _texto(corpo):
    if isinstance(corpo, Texto):
        if corpo.arquivado:
            raise ValueError('Não é possível usar um texto arquivado.')
        return corpo.corpo
    return str(corpo) if corpo is not None else ''


def preparar_campanha(nome, texto, filtros, limite, session_id, intervalo=5, *, selecao=None):
    nome = str(nome or '').strip()
    session_id = str(session_id or '').strip()
    if not nome:
        raise ValueError('Informe o nome da campanha.')
    if not session_id:
        raise ValueError('Informe a sessão do WhatsApp.')
    if selecao is None and (isinstance(limite, bool) or not isinstance(limite, int) or limite < 1):
        raise ValueError('O limite deve ser um inteiro positivo.')
    if isinstance(intervalo, bool) or not isinstance(intervalo, (int, float)) or not math.isfinite(intervalo) or intervalo < 0:
        raise ValueError('O intervalo não pode ser negativo.')

    modelo = _texto(texto)
    # Valida sintaxe e limites ainda que não existam contatos elegíveis.
    renderizar_mensagem(modelo, {campo: 'x' for campo in ('nome', 'endereco', 'telefone', 'site', 'avaliacao')})
    filtros = dict(filtros or {})
    if selecao is not None:
        from painel.services.selection import listar_grupos, validar_selecao
        selecao_normalizada = validar_selecao(selecao)
        return _preparar_campanha_selecao(
            nome, modelo, filtros, session_id, float(intervalo), selecao_normalizada, listar_grupos(filtros)
        )
    candidatos = filtrar_contatos(filtros)
    pontuados = []
    exclusoes = []
    por_telefone = {}
    for empresa in candidatos:
        try:
            telefone = normalizar_telefone(empresa.telefone)
        except (TypeError, ValueError):
            exclusoes.append({'empresa_id': empresa.pk, 'motivo': 'telefone ausente ou inválido'})
            continue
        instante = _ultimo_disparo_empresa(empresa)
        texto_final = renderizar_mensagem(modelo, {
            campo: '' if getattr(empresa, campo) is None else getattr(empresa, campo)
            for campo in ('nome', 'endereco', 'telefone', 'site', 'avaliacao')
        })
        candidato = (instante is not None, instante, empresa.pk, empresa, texto_final, telefone)
        atual = por_telefone.get(telefone)
        if atual is None:
            por_telefone[telefone] = [candidato, [empresa.pk]]
        else:
            atual[1].append(empresa.pk)
            if candidato[:3] < atual[0][:3]:
                por_telefone[telefone] = [candidato, atual[1]]

    data_minima = datetime.min.replace(tzinfo=timezone.get_current_timezone())
    pontuados = sorted((dados[0] + (dados[1],) for numero, dados in por_telefone.items()),
                       key=lambda item: (item[0], item[1] or data_minima, item[2]))
    existentes = set(Reserva.objects.filter(session_id=session_id).values_list(
        'telefone', 'mensagem_hash'
    ))
    elegiveis = []
    for item in pontuados:
        _, _, _, empresa, texto_final, numero, relacionados = item
        digest = hashlib.sha256(texto_final.encode('utf-8')).hexdigest()
        if (numero, digest) in existentes:
            exclusoes.append({'empresa_id': empresa.pk, 'telefone': numero, 'motivo': 'envio já reservado nesta sessão'})
            continue
        elegiveis.append((empresa, texto_final, numero, digest, relacionados))

    selecionados = elegiveis[:limite]
    filtros_snapshot = dict(filtros)
    filtros_snapshot['exclusoes'] = exclusoes
    with transaction.atomic():
        campanha = Campanha.objects.create(
            nome=nome, texto_snapshot=modelo, filtros_snapshot=filtros_snapshot,
            exclusoes=exclusoes, limite=limite, session_id=session_id, intervalo=float(intervalo),
        )
        Destinatario.objects.bulk_create([
            Destinatario(campanha=campanha, empresa=empresa,
                         empresas_relacionadas=sorted(set(relacionados)), ordem=ordem,
                         telefone=numero, mensagem=mensagem, mensagem_hash=digest)
            for ordem, (empresa, mensagem, numero, digest, relacionados) in enumerate(selecionados, 1)
        ])
    return campanha


def _prioridade_selecao(candidato):
    instante = candidato['instante']
    return (instante is not None,
            instante or datetime.min.replace(tzinfo=timezone.get_current_timezone()),
            candidato['empresa'].pk)


def _montar_candidato(empresa, telefone, modelo):
    instante = _ultimo_disparo_empresa(empresa)
    mensagem = renderizar_mensagem(modelo, {
        campo: '' if getattr(empresa, campo) is None else getattr(empresa, campo)
        for campo in ('nome', 'endereco', 'telefone', 'site', 'avaliacao')
    })
    return {
        'empresa': empresa,
        'telefone': telefone,
        'instante': instante,
        'mensagem': mensagem,
        'hash': hashlib.sha256(mensagem.encode('utf-8')).hexdigest(),
    }


def _avaliar_candidatos(empresas, modelo, exclusoes, invalidos, existentes):
    candidatos = {}
    for empresa in empresas:
        try:
            telefone = normalizar_telefone(empresa.telefone)
        except (TypeError, ValueError):
            if empresa.pk not in invalidos:
                exclusoes.append({'empresa_id': empresa.pk, 'motivo': 'telefone ausente ou inválido'})
                invalidos.add(empresa.pk)
            continue
        candidato = _montar_candidato(empresa, telefone, modelo)
        if (telefone, candidato['hash']) in existentes:
            exclusoes.append({'empresa_id': empresa.pk, 'telefone': telefone,
                              'motivo': 'envio já reservado nesta sessão'})
            continue
        candidatos[empresa.pk] = candidato
    return candidatos


def _deduplicar_candidatos(candidatos, relacionados_por_telefone):
    por_telefone = {}
    for original in candidatos:
        candidato = dict(original)
        telefone = candidato['telefone']
        candidato['relacionadas'] = relacionados_por_telefone[telefone]
        anterior = por_telefone.get(telefone)
        if anterior is None:
            por_telefone[telefone] = candidato
        elif _prioridade_selecao(candidato) < _prioridade_selecao(anterior):
            por_telefone[telefone] = candidato
    return sorted(por_telefone.values(), key=_prioridade_selecao)


def _preparar_campanha_selecao(nome, modelo, filtros, session_id, intervalo, selecao, grupos):
    from painel.models import Empresa

    por_chave = {grupo['chave']: grupo for grupo in grupos}
    exclusoes = []
    invalidos = set()
    if selecao['modo'] == 'quantidades':
        chaves = [grupo['chave'] for grupo in grupos if selecao['quantidades'].get(grupo['chave'], 0) > 0]
        solicitados = {chave: selecao['quantidades'][chave] for chave in chaves}
        ids_marcados = []
        ids_elegiveis = {
            empresa.pk for chave in chaves for empresa in por_chave[chave]['empresas']
        }
    else:
        from painel.services.selection import listar_grupos

        ids_marcados = selecao['empresa_ids']
        filtrados = {empresa.pk for grupo in grupos for empresa in grupo['empresas']}
        empresas_marcadas = list(Empresa.objects.filter(pk__in=ids_marcados).order_by('id'))
        fora = [empresa for empresa in empresas_marcadas if empresa.pk not in filtrados]
        exclusoes.extend({'empresa_id': empresa.pk, 'motivo': 'empresa marcada fora dos filtros comuns'}
                         for empresa in fora)
        ids_elegiveis = set(ids_marcados) & filtrados
        primeira_grupo = {}
        solicitados = {grupo['chave']: 0 for grupo in grupos}
        grupos_sem_filtro = listar_grupos({})
        for grupo in grupos_sem_filtro:
            for empresa in grupo['empresas']:
                if empresa.pk in ids_marcados and empresa.pk not in primeira_grupo:
                    primeira_grupo[empresa.pk] = grupo['chave']
                    solicitados[grupo['chave']] += 1
        chaves = list(por_chave)

    empresas_por_id = {}
    membros = {chave: [] for chave in chaves}
    for chave in chaves:
        for empresa in por_chave[chave]['empresas']:
            if empresa.pk in ids_elegiveis:
                empresas_por_id[empresa.pk] = empresa
                membros[chave].append(empresa)

    existentes = set(Reserva.objects.filter(session_id=session_id).values_list('telefone', 'mensagem_hash'))
    empresas_a_avaliar = list(empresas_por_id.values())
    candidatos_por_empresa = _avaliar_candidatos(
        empresas_a_avaliar, modelo, exclusoes, invalidos, existentes
    )
    relacionados_por_telefone = {}
    for candidato in candidatos_por_empresa.values():
        relacionados_por_telefone.setdefault(candidato['telefone'], []).append(candidato['empresa'].pk)
    relacionados_por_telefone = {
        telefone: sorted(set(empresa_ids))
        for telefone, empresa_ids in relacionados_por_telefone.items()
    }
    selecionados = []
    atribuicoes = []
    preparados = {chave: 0 for chave in chaves}
    usados = set()
    if selecao['modo'] == 'quantidades':
        for chave in chaves:
            candidatos_grupo = [candidatos_por_empresa[empresa.pk] for empresa in membros[chave]
                                if empresa.pk in candidatos_por_empresa]
            candidatos = _deduplicar_candidatos(
                candidatos_grupo, relacionados_por_telefone
            )
            quantidade = solicitados[chave]
            for candidato in candidatos:
                telefone = candidato['telefone']
                if telefone in usados or preparados[chave] >= quantidade:
                    continue
                candidato['grupo'] = chave
                selecionados.append(candidato)
                usados.add(telefone)
                preparados[chave] += 1
                atribuicoes.append({'empresa_id': candidato['empresa'].pk, 'telefone': telefone, 'grupo': chave})
    else:
        candidatos = _deduplicar_candidatos(
            candidatos_por_empresa.values(), relacionados_por_telefone
        )
        for candidato in candidatos:
            telefone = candidato['telefone']
            if telefone in usados:
                continue
            chave = primeira_grupo.get(candidato['empresa'].pk)
            candidato['grupo'] = chave
            selecionados.append(candidato)
            usados.add(telefone)
            preparados[chave] += 1
            atribuicoes.append({'empresa_id': candidato['empresa'].pk, 'telefone': telefone, 'grupo': chave})

    grupos_snapshot = []
    for chave in chaves:
        solicitado = solicitados[chave]
        preparado = preparados[chave]
        registro = {'chave': chave, 'nome': por_chave[chave]['nome'], 'tipo': por_chave[chave]['tipo'],
                    'solicitado': solicitado, 'preparado': preparado}
        if preparado < solicitado:
            registro['motivo_reducao'] = 'Não havia números elegíveis suficientes para completar a quantidade.'
        grupos_snapshot.append(registro)
    selecao_snapshot = {
        'modo': selecao['modo'],
        'empresa_ids': ids_marcados,
        'grupos': grupos_snapshot,
        'atribuicoes': atribuicoes,
    }
    selecionados.sort(key=_prioridade_selecao)
    filtros_snapshot = dict(filtros)
    filtros_snapshot['selecao'] = selecao_snapshot
    filtros_snapshot['exclusoes'] = exclusoes
    with transaction.atomic():
        campanha = Campanha.objects.create(
            nome=nome, texto_snapshot=modelo, filtros_snapshot=filtros_snapshot,
            exclusoes=exclusoes, limite=selecao['total'], session_id=session_id, intervalo=intervalo,
        )
        Destinatario.objects.bulk_create([
            Destinatario(campanha=campanha, empresa=candidato['empresa'],
                         empresas_relacionadas=sorted(set(candidato['relacionadas'])), ordem=ordem,
                         telefone=candidato['telefone'], mensagem=candidato['mensagem'], mensagem_hash=candidato['hash'])
            for ordem, candidato in enumerate(selecionados, 1)
        ])
    return campanha


def iniciar_campanha(campanha_id):
    with transaction.atomic():
        campanha = Campanha.objects.select_for_update().get(pk=campanha_id)
        if campanha.estado != Campanha.Estado.RASCUNHO:
            raise ValueError('Somente campanhas em rascunho podem ser iniciadas.')
        if not campanha.destinatarios.exists():
            raise ValueError('A campanha não possui destinatários elegíveis.')
        campanha.estado = Campanha.Estado.ATIVA
        campanha.iniciada_em = timezone.now()
        campanha.save(update_fields=('estado', 'iniciada_em'))
        return campanha
