"""Leitura agregada do estado persistido, sem executar trabalho operacional."""

from collections import Counter

from django.db.models import Count
from datetime import datetime

from django.utils import timezone


ESTADOS_TENTATIVA = ('fila', 'enviando', 'aceita', 'enviado', 'entregue', 'lido', 'falha', 'incerto')
ROTULOS = {
    'fila': 'Na fila', 'enviando': 'Enviando', 'aceita': 'Aceita, aguardando confirmação',
    'enviado': 'Enviado', 'entregue': 'Entregue', 'lido': 'Lido', 'falha': 'Falha explícita',
    'incerto': 'Pendente de verificação', 'rascunho': 'Rascunho', 'ativa': 'Ativa',
    'pausada': 'Pausada', 'concluida': 'Concluída', 'pendente': 'Pendente',
    'processando': 'Processando', 'falha_pesquisa': 'Falha',
}


def _iso(valor):
    return valor.isoformat() if hasattr(valor, 'isoformat') else valor


def observar_conexao():
    try:
        from .integration import estado_conexao
        estado = estado_conexao()
    except (ImportError, OSError, RuntimeError, ValueError):
        estado = None
    if not isinstance(estado, dict):
        estado = {}
    status = estado.get('status')
    if not isinstance(status, str):
        status = 'indisponivel'
    conhecidos = {'ready', 'qr_ready', 'disconnected', 'initializing', 'starting', 'failed',
                  'action_required', 'nao_configurado', 'sem_sessao'}
    ok = status in conhecidos and (not estado.get('erro') or status in ('nao_configurado', 'sem_sessao'))
    conectado = True if status == 'ready' and ok else (False if ok else None)
    erro = '' if ok else 'Serviço local indisponível; último estado válido mantido.'
    verificado = estado.get('verificado_em')
    if isinstance(verificado, str):
        try:
            verificado = datetime.fromisoformat(verificado)
        except ValueError:
            verificado = None
    return {
        'status': status, 'status_legivel': estado.get('status_legivel') or {
            'ready': 'Conectado', 'qr_ready': 'Aguardando leitura do QR Code',
            'disconnected': 'Desconectado', 'nao_configurado': 'Serviço não configurado',
            'sem_sessao': 'Sessão ainda não criada', 'indisponivel': 'Serviço indisponível',
        }.get(status, 'Estado desconhecido'),
        'conectado': conectado, 'qr_disponivel': bool(ok and status == 'qr_ready'),
        'verificado_em': verificado or timezone.now(),
        'consulta_ok': ok, 'erro': erro,
    }


def _tentativa(item):
    return {
        'id': item.pk, 'destinatario_id': item.destinatario_id,
        'campanha_id': item.destinatario.campanha_id,
        'empresa_nome': item.destinatario.empresa.nome,
        'campanha_nome': item.destinatario.campanha.nome,
        'telefone': item.telefone or item.destinatario.telefone,
        'estado': item.estado, 'rotulo': ROTULOS.get(item.estado, item.estado),
        'erro': item.erro, 'confirmado_em': _iso(item.confirmado_em),
        'ultima_verificacao': _iso(item.ultima_verificacao),
        'atualizado_em': _iso(item.atualizado_em), 'criado_em': _iso(item.criado_em),
    }


def obter_estado_painel(secao, *, campanha_id=None):
    from painel.models import Campanha, Destinatario, Empresa, Pesquisa, Reserva, Tentativa

    secoes = ('inicio', 'pesquisas', 'campanhas', 'campanha', 'historico', 'whatsapp', 'outra')
    if secao not in secoes:
        raise ValueError('Seção de estado inválida.')
    if (secao == 'campanha') != (campanha_id is not None):
        raise ValueError('Informe somente os parâmetros previstos para a seção.')
    conexao = observar_conexao()
    resultado = {'observado_em': _iso(timezone.now()), 'conexao': conexao}
    if secao == 'inicio':
        pesquisas = list(Pesquisa.objects.order_by('-criada_em')[:5])
        resultado['inicio'] = {
            'total_empresas': Empresa.objects.count(), 'total_pesquisas': Pesquisa.objects.count(),
            'campanhas_ativas': Campanha.objects.filter(estado__in=('ativa', 'pausada')).count(),
            'pesquisas_recentes': [_pesquisa(item) for item in pesquisas],
        }
    elif secao == 'pesquisas':
        resultado['pesquisas'] = {'itens': [_pesquisa(item) for item in Pesquisa.objects.order_by('-criada_em')]}
    elif secao == 'campanhas':
        campanhas = Campanha.objects.annotate(destinatarios_total_q=Count('destinatarios')).order_by('-criado_em')
        resultado['campanhas'] = {'itens': [_campanha(item) for item in campanhas]}
    elif secao == 'historico':
        tentativas = list(Tentativa.objects.select_related('destinatario__empresa', 'destinatario__campanha')
                          .order_by('-criado_em')[:250])
        reservas = list(Reserva.objects.exclude(pk__in=Tentativa.objects.values_list('reserva_id', flat=True))
                        .order_by('-criado_em')[:250])
        resultado['historico'] = {'tentativas': [_tentativa(item) for item in tentativas],
            'reservas_antigas': [_reserva(item) for item in reservas]}
    elif secao == 'campanha':
        campanha = Campanha.objects.filter(pk=campanha_id).first()
        if campanha is None:
            raise LookupError('Campanha não encontrada.')
        destinatarios = list(Destinatario.objects.filter(campanha=campanha)
                             .select_related('empresa').order_by('ordem', 'id'))
        tentativas = list(Tentativa.objects.filter(destinatario__campanha=campanha)
                          .select_related('destinatario__empresa', 'destinatario__campanha')
                          .order_by('criado_em', 'id'))
        por_destinatario = {}
        for tentativa in tentativas:
            por_destinatario[tentativa.destinatario_id] = tentativa
        destinatarios_por_estado = Counter()
        sem_tentativa = sem_bloqueados = 0
        estados_destino = []
        for destinatario in destinatarios:
            tentativa = por_destinatario.get(destinatario.pk)
            if tentativa:
                destinatarios_por_estado[tentativa.estado] += 1
                estado = tentativa.estado
                rotulo = ROTULOS.get(estado, estado)
            else:
                sem_tentativa += 1
                sem_bloqueados += bool(destinatario.bloqueado_motivo)
                estado = None
                rotulo = 'Bloqueado' if destinatario.bloqueado_motivo else 'Aguardando execução'
            estados_destino.append({'id': destinatario.pk, 'tentativa_id': tentativa.pk if tentativa else None,
                'estado': estado, 'rotulo': rotulo, 'bloqueado_motivo': destinatario.bloqueado_motivo})
        tentativas_por_estado = Counter(item.estado for item in tentativas)
        resultado['campanha'] = {
            **_campanha(campanha),
            'contagens': {
                'destinatarios_total': len(destinatarios), 'sem_tentativa': sem_tentativa,
                'sem_tentativa_bloqueados': sem_bloqueados,
                'destinatarios_por_estado': {estado: destinatarios_por_estado[estado] for estado in ESTADOS_TENTATIVA},
                'tentativas_total': len(tentativas),
                'tentativas_por_estado': {estado: tentativas_por_estado[estado] for estado in ESTADOS_TENTATIVA},
                'tentativas_confirmadas': sum(item.estado in ('enviado', 'entregue', 'lido') for item in tentativas),
            },
            'destinatarios': estados_destino, 'tentativas': [_tentativa(item) for item in tentativas],
        }
    return resultado


def _pesquisa(item):
    rotulo = {'falha': 'Falha'}.get(item.estado, ROTULOS.get(item.estado, item.estado))
    return {'id': item.pk, 'termos': item.termos, 'estado': item.estado,
        'rotulo': rotulo, 'processadas': item.processadas,
        'criada_em': _iso(item.criada_em), 'inicio': _iso(item.inicio), 'fim': _iso(item.fim),
        'erro': item.erro}


def _campanha(item):
    return {'id': item.pk, 'nome': item.nome, 'estado': item.estado,
        'rotulo': ROTULOS.get(item.estado, item.estado), 'motivo_pausa': item.motivo_pausa,
        'destinatarios_total': getattr(item, 'destinatarios_total_q', None)
            if getattr(item, 'destinatarios_total_q', None) is not None else item.destinatarios.count(),
        'criado_em': _iso(item.criado_em)}


def _reserva(item):
    labels = {'aceita': 'Aceita · confirmação não registrada',
        'enviado': 'Status legado · confirmação não verificada',
        'processando': 'Pendente de verificação', 'incerto': 'Pendente de verificação'}
    return {'id': item.pk, 'empresa_id': item.empresa_id, 'telefone': item.telefone,
        'rotulo': labels.get((item.status or '').casefold(), item.status or 'Status legado'),
        'criado_em': _iso(item.criado_em), 'erro': item.erro or ''}
