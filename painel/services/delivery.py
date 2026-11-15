"""Reservas duráveis, envio controlado e atualização auditável de recibos."""

from datetime import datetime, timezone as datetime_timezone
from hashlib import sha256
from django.db.models import Max
import time
from threading import Lock
from urllib.parse import quote

from django.db import IntegrityError, OperationalError, transaction
from django.db.models import Q
from django.utils import timezone

from mensagens_openwa import OpenWAError, normalizar_telefone
from painel.models import (
    AutorizacaoReenvio, Campanha, Destinatario, Empresa, Evento, Reserva, Tentativa,
)


CONFIRMADOS = (Tentativa.Estado.ENVIADO, Tentativa.Estado.ENTREGUE, Tentativa.Estado.LIDO)
TERMINAIS_REENVIO = (Tentativa.Estado.FALHA, *CONFIRMADOS)
ATIVOS_RESERVA = {'processando', 'aceita', 'enviando', 'incerto'}
_RESERVA_LOCK = Lock()


def _evento(tentativa, estado, detalhe=''):
    Evento.objects.create(tentativa=tentativa, estado=estado, detalhe=detalhe)


def _bloquear_destinatario(destinatario, motivo):
    Destinatario.objects.filter(pk=destinatario.pk, bloqueado_motivo='').update(
        bloqueado_motivo=motivo, bloqueado_em=timezone.now()
    )


def _reservar_tentativa(destinatario_id, autorizacao_reenvio_id=None):
    """Persiste a reserva legada e a tentativa própria antes de qualquer POST."""
    with transaction.atomic():
        destinatario = (Destinatario.objects.select_for_update()
                        .select_related('campanha', 'empresa').get(pk=destinatario_id))
        campanha = Campanha.objects.select_for_update().get(pk=destinatario.campanha_id)
        if campanha.estado != Campanha.Estado.ATIVA or destinatario.bloqueado_motivo:
            return None
        if destinatario.tentativas.exists():
            return None
        try:
            telefone = normalizar_telefone(destinatario.telefone)
        except (ValueError, TypeError):
            _bloquear_destinatario(destinatario, 'Telefone inválido na prévia.')
            return None
        mensagem_hash = sha256(destinatario.mensagem.encode('utf-8')).hexdigest()
        if mensagem_hash != destinatario.mensagem_hash:
            _bloquear_destinatario(destinatario, 'A mensagem da prévia não corresponde ao hash salvo.')
            return None

        autorizacao = None
        if autorizacao_reenvio_id is not None:
            try:
                autorizacao = (AutorizacaoReenvio.objects.select_for_update()
                               .select_related('tentativa_anterior__reserva')
                               .get(pk=autorizacao_reenvio_id))
            except AutorizacaoReenvio.DoesNotExist:
                return None
            anterior = autorizacao.tentativa_anterior
            if (autorizacao.consumida_em is not None or
                    autorizacao.destinatario_id != destinatario.pk or
                    anterior.estado not in TERMINAIS_REENVIO or
                    (anterior.estado in CONFIRMADOS and anterior.confirmado_em is None) or
                    campanha.session_id != (anterior.session_id or campanha.session_id) or
                    telefone != (anterior.telefone or telefone) or
                    mensagem_hash != (anterior.mensagem_hash or mensagem_hash)):
                return None
        elif hasattr(destinatario, 'autorizacao_reenvio'):
            # Uma prévia de reenvio exige o token criado junto a ela.
            return None

        identidade = {
            'session_id': campanha.session_id,
            'telefone': telefone,
            'mensagem_hash': mensagem_hash,
        }
        reserva = Reserva.objects.filter(**identidade).first()
        if reserva is not None:
            if autorizacao is None:
                _bloquear_destinatario(destinatario, 'Uma reserva anterior já protege este telefone e texto.')
                return None
            if reserva.status.casefold() in ATIVOS_RESERVA:
                return None
            if reserva.tentativas.filter(estado__in=(Tentativa.Estado.ENVIANDO,
                    Tentativa.Estado.ACEITA, Tentativa.Estado.INCERTO)).exists():
                return None
            # Reutilização explícita da mesma identidade adquire o lease no ledger
            # antes de consumir o token; recibos tardios ficam restritos à tentativa antiga.
            Reserva.objects.filter(pk=reserva.pk).update(
                status='processando', message_id=None, erro=None, atualizado_em=timezone.now()
            )
        else:
            try:
                with transaction.atomic():
                    reserva = Reserva.objects.create(
                        empresa_id=destinatario.empresa_id,
                        session_id=campanha.session_id,
                        telefone=telefone,
                        mensagem=destinatario.mensagem,
                        mensagem_hash=mensagem_hash,
                        status='processando',
                    )
            except IntegrityError:
                if autorizacao is None:
                    _bloquear_destinatario(destinatario, 'Uma reserva concorrente já protege este telefone e texto.')
                return None

        if autorizacao is not None:
            consumida = AutorizacaoReenvio.objects.filter(
                pk=autorizacao.pk, consumida_em__isnull=True
            ).update(consumida_em=timezone.now())
            if consumida != 1:
                return None

        tentativa = Tentativa.objects.create(
            destinatario=destinatario,
            reserva=reserva,
            estado=Tentativa.Estado.ENVIANDO,
            telefone=telefone,
            session_id=campanha.session_id,
            mensagem=destinatario.mensagem,
            mensagem_hash=mensagem_hash,
            iniciado_em=timezone.now(),
        )
        _evento(tentativa, Tentativa.Estado.ENVIANDO, 'Reserva persistida antes da chamada externa.')
        return tentativa


def reservar_tentativa(destinatario_id, autorizacao_reenvio_id=None):
    # BEGIN IMMEDIATE serializa a identidade. SQLite compartilhado em memória pode
    # responder SQLITE_LOCKED antes do busy_timeout; repetir só a transação curta.
    # SQLite não oferece SELECT FOR UPDATE; serializa aquisições dentro deste
    # trabalhador e a constraint UNIQUE do ledger coordena processos distintos.
    with _RESERVA_LOCK:
        for tentativa_numero in range(10):
            try:
                return _reservar_tentativa(destinatario_id, autorizacao_reenvio_id)
            except OperationalError as erro:
                if 'locked' not in str(erro).casefold() or tentativa_numero == 9:
                    raise
                time.sleep(0.05 * (tentativa_numero + 1))
    return None


def _pausar(campanha, motivo):
    if campanha.estado == Campanha.Estado.ATIVA:
        Campanha.objects.filter(pk=campanha.pk, estado=Campanha.Estado.ATIVA).update(
            estado=Campanha.Estado.PAUSADA, motivo_pausa=motivo
        )


def _sessao_pronta(cliente, campanha):
    if not cliente or getattr(cliente, 'session_id', None) != campanha.session_id:
        _pausar(campanha, 'Sessão do WhatsApp diferente da sessão congelada na campanha.')
        return False
    try:
        estado = cliente.status()
    except (OpenWAError, OSError, RuntimeError, ValueError):
        _pausar(campanha, 'Não foi possível confirmar a conexão do WhatsApp local.')
        return False
    if not isinstance(estado, dict) or estado.get('status') != 'ready':
        _pausar(campanha, 'WhatsApp desconectado; reconecte e retome a campanha manualmente.')
        return False
    return True


def _atualizar_fila(campanha, tentativa_criada=False):
    agora = timezone.now()
    if tentativa_criada:
        proximo = agora + timezone.timedelta(seconds=campanha.intervalo) if campanha.intervalo else None
        # O intervalo pertence ao trabalhador serial como um todo; persisti-lo
        # também ao concluir a campanha impede a próxima campanha de ultrapassá-lo.
        Campanha.objects.filter(pk=campanha.pk).update(proximo_envio=proximo)
    pendentes = Destinatario.objects.filter(
        campanha=campanha, bloqueado_motivo='', tentativas__isnull=True
    ).exists()
    if not pendentes:
        Campanha.objects.filter(pk=campanha.pk, estado=Campanha.Estado.ATIVA).update(
            estado=Campanha.Estado.CONCLUIDA
        )
        return
def enviar_um(cliente):
    """Envia no máximo uma mensagem e encerra/ignora identidades já reservadas."""
    agora = timezone.now()
    campanha = Campanha.objects.filter(estado=Campanha.Estado.ATIVA).order_by('criado_em', 'pk').first()
    if campanha is None:
        return False
    ultima_liberacao = Campanha.objects.aggregate(ultima=Max('proximo_envio'))['ultima']
    if ((ultima_liberacao is not None and ultima_liberacao > agora) or
            (campanha.proximo_envio is not None and campanha.proximo_envio > agora)):
        return False
    for campanha in (campanha,):
        if not _sessao_pronta(cliente, campanha):
            return False
        while True:
            destinatario = (Destinatario.objects.filter(
                campanha=campanha, bloqueado_motivo='', tentativas__isnull=True
            ).order_by('ordem', 'pk').first())
            if destinatario is None:
                _atualizar_fila(campanha)
                break
            try:
                telefone = normalizar_telefone(destinatario.telefone)
                contato = cliente.verificar_numero(telefone)
            except (OpenWAError, OSError, RuntimeError, ValueError):
                # Uma falha de consulta GET não prova tentativa de envio; pode ser verificada depois.
                return False
            campanha.refresh_from_db(fields=('estado', 'session_id'))
            if campanha.estado != Campanha.Estado.ATIVA:
                return False
            if not _sessao_pronta(cliente, campanha):
                return False

            # GET inválido não deve criar uma reserva nem ser confundido com
            # um contato válido sem WhatsApp.
            if not isinstance(contato, dict) or not isinstance(contato.get('exists'), bool):
                _pausar(campanha, 'Resposta inválida ao validar o número no WhatsApp; fila pausada sem envio.')
                return False
            if contato.get('exists') is True and (
                    not isinstance(contato.get('whatsappId'), str) or not contato.get('whatsappId')):
                _pausar(campanha, 'Resposta de validação sem identificador WhatsApp; fila pausada sem envio.')
                return False

            reenvio = getattr(destinatario, 'autorizacao_reenvio', None)
            tentativa = reservar_tentativa(
                destinatario.pk, reenvio.pk if reenvio is not None else None
            )
            if tentativa is None:
                destinatario.refresh_from_db(fields=('bloqueado_motivo',))
                campanha.refresh_from_db(fields=('estado',))
                if campanha.estado != Campanha.Estado.ATIVA:
                    return False
                if destinatario.bloqueado_motivo:
                    continue
                return False

            existe = isinstance(contato, dict) and contato.get('exists') is True
            chat_id = contato.get('whatsappId') if existe else None
            if not existe or not isinstance(chat_id, str) or not chat_id:
                _finalizar_tentativa(tentativa.pk, Tentativa.Estado.FALHA,
                                     'Número sem WhatsApp disponível para envio.',
                                     reserva_status='sem_whatsapp')
                _atualizar_fila(campanha, tentativa_criada=True)
                return True

            Tentativa.objects.filter(pk=tentativa.pk).update(chat_id=chat_id)
            try:
                resposta = cliente.enviar_texto(chat_id, tentativa.mensagem)
                message_id = resposta.get('messageId') if isinstance(resposta, dict) else None
                if not isinstance(message_id, str) or not message_id:
                    raise OpenWAError('OpenWA não retornou um identificador de mensagem.')
            except OpenWAError as erro:
                if erro.http_status and 400 <= erro.http_status < 500:
                    estado, reserva_status = Tentativa.Estado.FALHA, 'falha'
                    detalhe = str(erro)
                else:
                    estado, reserva_status = Tentativa.Estado.INCERTO, 'incerto'
                    detalhe = 'Envio interrompido sem resultado conclusivo; verifique antes de reenviar.'
                _finalizar_tentativa(tentativa.pk, estado, detalhe, reserva_status=reserva_status)
                _atualizar_fila(campanha, tentativa_criada=True)
                return True
            except (TimeoutError, OSError, RuntimeError, ValueError):
                _finalizar_tentativa(
                    tentativa.pk, Tentativa.Estado.INCERTO,
                    'Envio interrompido sem resultado conclusivo; verifique antes de reenviar.',
                    reserva_status='incerto',
                )
                _atualizar_fila(campanha, tentativa_criada=True)
                return True

            _finalizar_tentativa(tentativa.pk, Tentativa.Estado.ACEITA,
                                 'OpenWA aceitou a solicitação; aguardando confirmação.',
                                 reserva_status='aceita', message_id=message_id)
            _atualizar_fila(campanha, tentativa_criada=True)
            return True
    return False


def _finalizar_tentativa(tentativa_id, estado, detalhe, reserva_status, message_id=None):
    with transaction.atomic():
        tentativa = Tentativa.objects.select_for_update().select_related('reserva').get(pk=tentativa_id)
        tentativa.estado = estado
        tentativa.erro = detalhe if estado in (Tentativa.Estado.FALHA, Tentativa.Estado.INCERTO) else ''
        tentativa.message_id = message_id
        tentativa.atualizado_em = timezone.now()
        tentativa.save(update_fields=('estado', 'erro', 'message_id', 'atualizado_em'))
        Reserva.objects.filter(pk=tentativa.reserva_id).update(
            status=reserva_status, message_id=message_id, erro=tentativa.erro or None,
            atualizado_em=timezone.now(),
        )
        _evento(tentativa, estado, detalhe)


def _mensagens_da_resposta(resposta):
    if isinstance(resposta, list):
        return [item for item in resposta if isinstance(item, dict)]
    if not isinstance(resposta, dict):
        return []
    for chave in ('messages', 'data', 'items'):
        valor = resposta.get(chave)
        if isinstance(valor, list):
            return [item for item in valor if isinstance(item, dict)]
        if isinstance(valor, dict):
            achadas = _mensagens_da_resposta(valor)
            if achadas:
                return achadas
    return [resposta] if any(chave in resposta for chave in ('waMessageId', 'messageId', 'id')) else []


def _id_mensagem(item):
    for chave in ('waMessageId', 'messageId', 'id'):
        valor = item.get(chave)
        if isinstance(valor, str) and valor:
            return valor
    for chave in ('key', 'message'):
        valor = item.get(chave)
        if isinstance(valor, dict):
            encontrado = _id_mensagem(valor)
            if encontrado:
                return encontrado
    return None


def _timestamp_recibo(item):
    valor = item.get('timestampMs') or item.get('timestamp') or item.get('t')
    if isinstance(valor, (int, float)):
        if valor > 10_000_000_000:
            valor /= 1000
        try:
            return datetime.fromtimestamp(valor, tz=datetime_timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(valor, str):
        try:
            data = datetime.fromisoformat(valor.replace('Z', '+00:00'))
        except ValueError:
            return None
        return timezone.make_aware(data, datetime_timezone.utc) if timezone.is_naive(data) else data
    return None


def _estado_confirmado(item):
    if not isinstance(item, dict):
        return None
    status = str(item.get('status') or '').casefold()
    try:
        ack = int(item['ack']) if item.get('ack') is not None else None
    except (TypeError, ValueError):
        ack = None
    if status in ('failed', 'error', 'rejected', 'falha'):
        return Tentativa.Estado.FALHA
    if status in ('read', 'played', 'lido'):
        return Tentativa.Estado.LIDO
    if status in ('delivered', 'received', 'entregue'):
        return Tentativa.Estado.ENTREGUE
    if ack == -1:
        return Tentativa.Estado.FALHA
    # O estado sent/ack 1 pode ser persistido antes de uma falha interna no OpenWA.
    return None


def atualizar_recibos(cliente):
    """Consulta somente IDs próprios; atualiza estado quando há recibo forte."""
    agora = timezone.now()
    candidatos = Tentativa.objects.filter(
        estado__in=(Tentativa.Estado.ACEITA, Tentativa.Estado.ENVIADO,
                    Tentativa.Estado.ENTREGUE, Tentativa.Estado.INCERTO),
        message_id__isnull=False,
    ).exclude(chat_id__isnull=True).filter(
        Q(ultima_verificacao__isnull=True) | Q(ultima_verificacao__lte=agora - timezone.timedelta(seconds=10))
    ).order_by('pk')
    atualizadas = 0
    cache = {}
    for tentativa in candidatos:
        chave = (tentativa.session_id, tentativa.chat_id)
        if chave not in cache:
            try:
                if getattr(cliente, 'session_id', None) != tentativa.session_id:
                    cache[chave] = None
                else:
                    path = (cliente.caminho_sessao() + '/messages?chatId=' +
                            quote(tentativa.chat_id, safe='') + '&limit=100&inlineMedia=false')
                    cache[chave] = _mensagens_da_resposta(cliente.request('GET', path))
            except (OpenWAError, OSError, RuntimeError, ValueError):
                cache[chave] = None
        mensagens = cache[chave]
        tentativa_atualizada = False
        with transaction.atomic():
            atual = Tentativa.objects.select_for_update().get(pk=tentativa.pk)
            atual.ultima_verificacao = agora
            if mensagens is not None:
                evento_recibo = next((item for item in mensagens
                                      if _id_mensagem(item) == atual.message_id), None)
                novo_estado = _estado_confirmado(evento_recibo) if evento_recibo else None
                ordem = {
                    Tentativa.Estado.ACEITA: 1,
                    Tentativa.Estado.INCERTO: 1,
                    Tentativa.Estado.ENVIADO: 2,
                    Tentativa.Estado.ENTREGUE: 3,
                    Tentativa.Estado.LIDO: 4,
                }
                negativo_aplicavel = (novo_estado == Tentativa.Estado.FALHA and
                                      atual.estado in (Tentativa.Estado.ACEITA, Tentativa.Estado.INCERTO))
                if novo_estado and (negativo_aplicavel or
                                    ordem.get(novo_estado, 0) > ordem.get(atual.estado, 0)):
                    atual.estado = novo_estado
                    if novo_estado == Tentativa.Estado.FALHA:
                        atual.erro = 'O OpenWA reportou falha explícita para a mensagem.'
                        campos = ('estado', 'erro', 'ultima_verificacao', 'atualizado_em')
                    else:
                        atual.confirmado_em = atual.confirmado_em or _timestamp_recibo(evento_recibo) or agora
                        atual.erro = ''
                        campos = ('estado', 'confirmado_em', 'erro', 'ultima_verificacao', 'atualizado_em')
                    atual.save(update_fields=campos)
                    reserva_status = {
                        Tentativa.Estado.FALHA: 'falha',
                        Tentativa.Estado.ENVIADO: 'enviado',
                        Tentativa.Estado.ENTREGUE: 'entregue',
                        Tentativa.Estado.LIDO: 'lido',
                    }[novo_estado]
                    # Apenas a tentativa corrente projeta estado no ledger compartilhado.
                    ultima = (Tentativa.objects.filter(reserva_id=atual.reserva_id)
                              .order_by('-pk').values_list('pk', flat=True).first())
                    if ultima == atual.pk:
                        Reserva.objects.filter(pk=atual.reserva_id).update(
                            status=reserva_status, atualizado_em=agora,
                            erro=atual.erro or None,
                        )
                    _evento(atual, novo_estado, 'Confirmação observada no recibo do OpenWA.')
                    atualizadas += 1
                    tentativa_atualizada = True
            if not tentativa_atualizada:
                Tentativa.objects.filter(pk=atual.pk).update(ultima_verificacao=agora)
    return atualizadas


def recuperar_interrompidos():
    """Marca apenas Tentativas do painel ainda em Enviando como incertas."""
    agora = timezone.now()
    with transaction.atomic():
        tentativas = list(Tentativa.objects.select_for_update().filter(estado=Tentativa.Estado.ENVIANDO))
        for tentativa in tentativas:
            tentativa.estado = Tentativa.Estado.INCERTO
            tentativa.erro = 'Aplicação interrompida durante uma tentativa; verifique o recibo antes de reenviar.'
            tentativa.ultima_verificacao = None
            tentativa.save(update_fields=('estado', 'erro', 'ultima_verificacao', 'atualizado_em'))
            Reserva.objects.filter(pk=tentativa.reserva_id).update(
                status='incerto', erro=tentativa.erro, atualizado_em=agora
            )
            _evento(tentativa, Tentativa.Estado.INCERTO, tentativa.erro)
    return len(tentativas)


def autorizar_reenvio(tentativa_id, motivo):
    motivo = str(motivo or '').strip()
    if len(motivo) < 3:
        raise ValueError('Informe um motivo para o reenvio.')
    with transaction.atomic():
        anterior = (Tentativa.objects.select_for_update()
                    .select_related('destinatario__empresa', 'destinatario__campanha')
                    .get(pk=tentativa_id))
        if (anterior.estado not in TERMINAIS_REENVIO or
                (anterior.estado in CONFIRMADOS and anterior.confirmado_em is None)):
            raise ValueError('A tentativa ainda precisa de verificação ou não terminou.')
        origem = anterior.destinatario.campanha
        sessao = anterior.session_id or origem.session_id
        telefone = anterior.telefone or anterior.destinatario.telefone
        mensagem = anterior.mensagem or anterior.destinatario.mensagem
        digest = anterior.mensagem_hash or anterior.destinatario.mensagem_hash
        if not sessao or not telefone or not mensagem or not digest:
            raise ValueError('A tentativa não contém os dados imutáveis necessários para preparar o reenvio.')
        campanha = Campanha.objects.create(
            nome=f'Reenvio de {origem.nome}'[:160],
            texto_snapshot=origem.texto_snapshot,
            filtros_snapshot={'reenvio_de_tentativa': anterior.pk},
            limite=1, session_id=sessao, intervalo=origem.intervalo,
            estado=Campanha.Estado.RASCUNHO,
        )
        destinatario = Destinatario.objects.create(
            campanha=campanha,
            empresa=anterior.destinatario.empresa,
            empresas_relacionadas=anterior.destinatario.empresas_relacionadas,
            ordem=1, telefone=telefone, mensagem=mensagem, mensagem_hash=digest,
        )
        AutorizacaoReenvio.objects.create(
            tentativa_anterior=anterior, destinatario=destinatario, motivo=motivo,
        )
        return campanha
