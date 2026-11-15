import json
import re
import unicodedata
from urllib.parse import parse_qs, unquote, urlsplit

from django.db import transaction

from painel.models import Empresa, HistoricoTag, Perfil, Pesquisa, RegraTag, ResultadoPesquisa, Tag


def _normalizar(texto):
    return ' '.join(unicodedata.normalize('NFKC', texto or '').casefold().split())


def _normalizar_classificacao(texto):
    normalizado = _normalizar(texto)
    decomposed = unicodedata.normalize('NFKD', normalizado)
    return ''.join(char for char in decomposed if not unicodedata.combining(char))


def _identificar_maps(url):
    url = unquote(url or '')
    match = re.search(r'!1s([^!/?]+)', url)
    if match:
        return match.group(1)
    partes = urlsplit(url)
    parametros = parse_qs(partes.query)
    for campo in ('cid', 'query_place_id'):
        if parametros.get(campo):
            return campo + ':' + parametros[campo][0]
    return partes.netloc + partes.path.split('/@')[0].rstrip('/')


def classificar(termo):
    normalizado = _normalizar_classificacao(termo)
    regras = sorted(RegraTag.objects.prefetch_related('tags'),
                    key=lambda regra: (-len(_normalizar_classificacao(regra.termo)), -regra.prioridade, regra.termo))
    encontradas = []
    for regra in regras:
        regra_normalizada = _normalizar_classificacao(regra.termo)
        if regra_normalizada and regra_normalizada in normalizado:
            encontradas.append((regra.prioridade, len(regra_normalizada), list(regra.tags.all())))
    if encontradas:
        vencedora = max((especificidade, prioridade) for prioridade, especificidade, _ in encontradas)
        tags_vencedoras = [tag for prioridade, especificidade, tags in encontradas
                           if (especificidade, prioridade) == vencedora for tag in tags]
    else:
        tags_vencedoras = []
    unicas = {tag.pk: tag for tag in tags_vencedoras}
    if unicas:
        return sorted(unicas.values(), key=lambda tag: (tag.tipo, tag.nome.casefold()))
    tag, _ = Tag.objects.get_or_create(nome='Sem classificação', tipo=Tag.Tipo.CATEGORIA)
    return [tag]


def registrar_resultado(pesquisa, dados, url, termo=None):
    nome = ' '.join((dados.get('nome') or '').split())
    if not nome:
        raise ValueError('A empresa precisa ter um nome para ser salva.')
    campos = ('endereco', 'telefone', 'site')
    limpos = {campo: ' '.join(dados.get(campo).split()) if dados.get(campo) else None for campo in campos}
    avaliacao = dados.get('avaliacao')
    maps_id = str(dados.get('maps_id') or _identificar_maps(url))
    if not maps_id:
        raise ValueError('A captura precisa identificar a ficha no Google Maps.')
    chave = json.dumps([_normalizar(nome), _normalizar(limpos['endereco'])]) if limpos['endereco'] else None
    termo_origem = termo if termo is not None else pesquisa.termos

    with transaction.atomic():
        empresa = Empresa.objects.filter(maps_id=maps_id).first()
        if empresa is None and chave:
            empresa = Empresa.objects.filter(chave=chave).first()
        if empresa is None:
            empresa = Empresa.objects.create(nome=nome, maps_id=maps_id, chave=chave, **limpos, avaliacao=avaliacao)
        else:
            atualizacoes = {'nome': nome}
            if maps_id and empresa.maps_id != maps_id:
                atualizacoes['maps_id'] = maps_id
            if chave and empresa.chave != chave:
                atualizacoes['chave'] = chave
            for campo, valor in limpos.items():
                if valor is not None:
                    atualizacoes[campo] = valor
            if avaliacao is not None:
                atualizacoes['avaliacao'] = avaliacao
            for campo, valor in atualizacoes.items():
                setattr(empresa, campo, valor)
            empresa.save(update_fields=list(atualizacoes))

        ResultadoPesquisa.objects.get_or_create(
            empresa=empresa, pesquisa=pesquisa, termo=termo_origem,
            defaults={'url': url or ''},
        )
        perfil, _ = Perfil.objects.get_or_create(empresa=empresa)
        suprimidas = set(perfil.tags_suprimidas or [])
        tags_auto = classificar(termo_origem)
        atuais = set(perfil.tags.values_list('pk', flat=True))
        adicionar = [tag for tag in tags_auto if tag.pk not in suprimidas and tag.pk not in atuais]
        if adicionar:
            perfil.tags.add(*adicionar)
            HistoricoTag.objects.bulk_create([
                HistoricoTag(empresa=empresa, tag=tag, tag_nome=tag.nome, tag_tipo=tag.tipo,
                             acao=HistoricoTag.Acao.ADICIONADA,
                             origem=HistoricoTag.Origem.AUTOMATICA, pesquisa=pesquisa)
                for tag in adicionar
            ])
        return empresa


def editar_tags(empresa, tags):
    with transaction.atomic():
        perfil, _ = Perfil.objects.get_or_create(empresa=empresa)
        tags_atuais = {tag.pk: tag for tag in perfil.tags.all()}
        atuais = set(tags_atuais)
        escolhidas = {tag.pk if isinstance(tag, Tag) else int(tag) for tag in tags}
        tags_escolhidas = {tag.pk: tag for tag in Tag.objects.filter(pk__in=escolhidas)}
        perfil.tags.set(tags_escolhidas.values())
        suprimidas = set(perfil.tags_suprimidas or [])
        suprimidas.update(atuais - escolhidas)
        suprimidas.difference_update(escolhidas)
        perfil.tags_suprimidas = sorted(suprimidas)
        perfil.save(update_fields=['tags_suprimidas'])
        alteracoes = [
            HistoricoTag(empresa=empresa, tag=tag, tag_nome=tag.nome, tag_tipo=tag.tipo,
                         acao=HistoricoTag.Acao.REMOVIDA, origem=HistoricoTag.Origem.MANUAL)
            for pk, tag in tags_atuais.items() if pk not in escolhidas
        ] + [
            HistoricoTag(empresa=empresa, tag=tag, tag_nome=tag.nome, tag_tipo=tag.tipo,
                         acao=HistoricoTag.Acao.ADICIONADA, origem=HistoricoTag.Origem.MANUAL)
            for pk, tag in tags_escolhidas.items() if pk not in atuais
        ]
        if alteracoes:
            HistoricoTag.objects.bulk_create(alteracoes)
