"""Agrupamento de empresas e validação do contrato de seleção de campanhas."""

import math
import unicodedata

from painel.models import Empresa, Perfil, Tag


PLACEHOLDER_NAME = 'sem classificação'
MAX_SELECAO = 1000
TAMANHO_PAGINA = 50


def _normalizar_busca(valor):
    texto = unicodedata.normalize('NFKD', valor)
    return ' '.join(''.join(caractere for caractere in texto
                            if not unicodedata.category(caractere).startswith('M')).casefold().split())


def listar_empresas_selecao(filtros_comuns, *, busca='', classificacao='todas', pagina=1):
    """Lista empresas únicas para seleção manual, com busca literal e paginação global."""
    if not isinstance(busca, str) or len(busca) > 200:
        raise ValueError('A busca deve ter no máximo 200 caracteres.')
    if isinstance(pagina, bool):
        raise ValueError('Página inválida.')
    try:
        pagina = int(pagina)
    except (TypeError, ValueError):
        raise ValueError('Página inválida.') from None
    if pagina < 1:
        raise ValueError('Página inválida.')
    grupos = listar_grupos(filtros_comuns)
    por_chave = {grupo['chave']: grupo for grupo in grupos}
    if classificacao != 'todas' and classificacao not in por_chave:
        raise ValueError('Classificação inválida.')
    busca_normalizada = _normalizar_busca(busca)
    classificacoes = {}
    for grupo in grupos:
        for empresa in grupo['empresas']:
            classificacoes.setdefault(empresa.pk, []).append({
                'chave': grupo['chave'], 'nome': grupo['nome'], 'tipo': grupo['tipo'],
            })
    elegiveis = {empresa.pk: empresa for grupo in grupos for empresa in grupo['empresas']}
    if classificacao != 'todas':
        permitidos = {empresa.pk for empresa in por_chave[classificacao]['empresas']}
        elegiveis = {pk: empresa for pk, empresa in elegiveis.items() if pk in permitidos}
    if busca_normalizada:
        elegiveis = {pk: empresa for pk, empresa in elegiveis.items()
                     if busca_normalizada in _normalizar_busca(empresa.nome)}
    empresas = [elegiveis[pk] for pk in sorted(elegiveis)]
    total = len(empresas)
    total_paginas = max(1, math.ceil(total / TAMANHO_PAGINA))
    pagina = min(pagina, total_paginas)
    itens = empresas[(pagina - 1) * TAMANHO_PAGINA:pagina * TAMANHO_PAGINA]
    return {
        'consulta': {'q': busca_normalizada, 'classificacao': classificacao,
                     'filtros_comuns': filtros_comuns},
        'pagina': pagina, 'tamanho_pagina': TAMANHO_PAGINA,
        'total': total, 'total_paginas': total_paginas,
        'empresas': [{'id': empresa.pk, 'nome': empresa.nome,
                      'telefone': empresa.telefone or None,
                      'classificacoes': classificacoes.get(empresa.pk, [])}
                     for empresa in itens],
    }


def listar_grupos(filtros=None):
    """Retorna grupos ordenados por ramo, categoria e grupo virtual."""
    # Import local evita dependência circular quando campaigns importar este serviço.
    from painel.services.campaigns import filtrar_contatos

    filtradas = filtrar_contatos(filtros or {})
    por_id = {
        empresa.pk: empresa
        for empresa in Empresa.objects.filter(pk__in=[e.pk for e in filtradas])
        .select_related('perfil').prefetch_related('perfil__tags')
    }
    empresas = [por_id[e.pk] for e in filtradas if e.pk in por_id]
    tags = list(Tag.objects.all())
    ordem_tipo = {Tag.Tipo.RAMO: 0, Tag.Tipo.CATEGORIA: 1}
    tags.sort(key=lambda tag: (ordem_tipo.get(tag.tipo, 2), tag.nome.casefold(), tag.pk))
    placeholder_ids = {
        tag.pk for tag in tags
        if tag.tipo == Tag.Tipo.CATEGORIA and tag.nome.strip().casefold() == PLACEHOLDER_NAME
    }

    ids_por_tag = {tag.pk: [] for tag in tags if tag.pk not in placeholder_ids}
    sem_classificacao = []
    for empresa in empresas:
        try:
            perfil = empresa.perfil
        except Perfil.DoesNotExist:
            sem_classificacao.append(empresa)
            continue
        tags_empresa = list(perfil.tags.all())
        efetivas = [tag for tag in tags_empresa if tag.pk not in placeholder_ids]
        if not efetivas:
            sem_classificacao.append(empresa)
        for tag in efetivas:
            ids_por_tag[tag.pk].append(empresa)

    grupos = []
    for tag in tags:
        if tag.pk in placeholder_ids:
            continue
        grupos.append({
            'chave': f'tag:{tag.pk}',
            'nome': tag.nome,
            'tipo': tag.tipo,
            'ordem': len(grupos),
            'empresas': ids_por_tag[tag.pk],
        })
    grupos.append({
        'chave': 'sem_classificacao',
        'nome': 'Sem classificação',
        'tipo': 'sem_classificacao',
        'ordem': len(grupos),
        'empresas': sem_classificacao,
    })
    return grupos


def validar_selecao(selecao):
    """Valida e normaliza seleção manual ou cotas por grupos atuais."""
    if not isinstance(selecao, dict) or 'modo' not in selecao:
        raise ValueError('Informe um modo de seleção válido.')
    modo = selecao.get('modo')

    if modo == 'manual':
        if set(selecao) != {'modo', 'empresa_ids'}:
            raise ValueError('A seleção manual contém campos incompatíveis.')
        ids = selecao['empresa_ids']
        if not isinstance(ids, (list, tuple)) or not ids:
            raise ValueError('Marque de 1 a 1000 empresas.')
        unicos = []
        vistos = set()
        for empresa_id in ids:
            if isinstance(empresa_id, bool) or not isinstance(empresa_id, int) or empresa_id < 1:
                raise ValueError('A lista contém um ID de empresa inválido.')
            if empresa_id not in vistos:
                vistos.add(empresa_id)
                unicos.append(empresa_id)
        if len(unicos) > MAX_SELECAO:
            raise ValueError('Marque de 1 a 1000 empresas distintas.')
        existentes = set(Empresa.objects.filter(pk__in=unicos).values_list('pk', flat=True))
        if existentes != set(unicos):
            raise ValueError('A lista contém empresa inexistente.')
        return {'modo': 'manual', 'empresa_ids': unicos, 'total': len(unicos)}

    if modo == 'quantidades':
        if set(selecao) != {'modo', 'quantidades'} or not isinstance(selecao.get('quantidades'), dict):
            raise ValueError('As cotas devem conter somente quantidades por grupo.')
        cotas = selecao['quantidades']
        tags = list(Tag.objects.all())
        tags_validas = {f'tag:{tag.pk}' for tag in tags
                        if not (tag.tipo == Tag.Tipo.CATEGORIA
                                and tag.nome.strip().casefold() == PLACEHOLDER_NAME)}
        chaves_validas = tags_validas | {'sem_classificacao'}
        total = 0
        normalizadas = {}
        for chave, quantidade in cotas.items():
            if not isinstance(chave, str) or chave not in chaves_validas:
                raise ValueError('A seleção contém grupo desconhecido.')
            if isinstance(quantidade, bool) or not isinstance(quantidade, int) or not 0 <= quantidade <= MAX_SELECAO:
                raise ValueError('Cada cota deve ser um inteiro entre 0 e 1000.')
            normalizadas[chave] = quantidade
            total += quantidade
        if total < 1 or total > MAX_SELECAO:
            raise ValueError('A soma das cotas deve ficar entre 1 e 1000.')
        return {'modo': 'quantidades', 'quantidades': normalizadas, 'total': total}

    raise ValueError('Modo de seleção desconhecido.')
