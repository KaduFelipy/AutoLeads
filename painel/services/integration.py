"""Adaptador HTTP local para a sessão OpenWA já configurada no projeto."""

import base64
import binascii
import os
from pathlib import Path
from urllib.parse import urlsplit

from django.utils import timezone

import mensagens_openwa


NOME_SESSAO = 'scrapgooglemaps'
TIMEOUT_HTTP = 10
PNG_MAGIC = b'\x89PNG\r\n\x1a\n'
ROTULOS_ESTADO = {
    'ready': 'Conectado',
    'qr_ready': 'Aguardando leitura do QR Code',
    'disconnected': 'Desconectado',
    'initializing': 'Iniciando sessão',
    'starting': 'Iniciando sessão',
    'failed': 'Falha na sessão',
    'action_required': 'Ação necessária no serviço local',
    'nao_configurado': 'Serviço não configurado',
    'sem_sessao': 'Sessão ainda não criada',
    'indisponivel': 'Serviço indisponível',
}


def _ler_config():
    try:
        caminho = os.environ.get('PAINEL_OPENWA_CONFIG')
        return mensagens_openwa.ler_config(caminho) if caminho else mensagens_openwa.ler_config()
    except (OSError, UnicodeError):
        return {}


def configuracao_publica():
    """Expõe somente a disponibilidade e o identificador público da sessão."""
    config = _ler_config()
    url = config.get('OPENWA_URL', '')
    chave = config.get('OPENWA_API_KEY', '')
    session_id = config.get('OPENWA_SESSION_ID', '')
    return {
        'configurada': bool(url and chave),
        'session_id': session_id,
    }


def cliente_local():
    config = _ler_config()
    url = (config.get('OPENWA_URL') or '').strip()
    chave = config.get('OPENWA_API_KEY') or ''
    session_id = (config.get('OPENWA_SESSION_ID') or '').strip()
    if not url or not chave:
        raise ValueError('Configure o serviço OpenWA local para usar a conexão.')
    try:
        partes = urlsplit(url)
        host = (partes.hostname or '').lower()
        partes.port  # valida porta malformada sem revelar a URL ao usuário
    except ValueError:
        raise ValueError('O endereço configurado do OpenWA local é inválido.') from None
    if (partes.scheme.lower() not in ('http', 'https') or
            host not in ('127.0.0.1', 'localhost', '::1') or
            partes.username is not None or partes.password is not None):
        raise ValueError('O OpenWA deve usar um endereço local de loopback.')
    return mensagens_openwa.OpenWAClient(url, chave, session_id=session_id,
                                         timeout=TIMEOUT_HTTP)


def _estado_basico(status, **extras):
    return {
        'status': status,
        'status_legivel': ROTULOS_ESTADO.get(status, 'Estado informado pelo serviço local'),
        'conectado': status == 'ready',
        'qr_disponivel': status == 'qr_ready',
        'verificado_em': timezone.now(),
        **extras,
    }


def estado_conexao():
    publico = configuracao_publica()
    if not publico['configurada']:
        return _estado_basico('nao_configurado', conectado=False,
                              qr_disponivel=False,
                              erro='Configure o serviço OpenWA local.')
    try:
        cliente = cliente_local()
        if not cliente.session_id:
            return _estado_basico('sem_sessao', conectado=False,
                                  qr_disponivel=False)
        resposta = cliente.status()
        status = resposta.get('status') if isinstance(resposta, dict) else None
        if not isinstance(status, str):
            return _estado_basico('indisponivel', conectado=False,
                                  qr_disponivel=False,
                                  erro='O OpenWA retornou um estado inválido.')
        return _estado_basico(status)
    except (mensagens_openwa.OpenWAError, ValueError, OSError, RuntimeError):
        return _estado_basico('indisponivel', conectado=False,
                              qr_disponivel=False,
                              erro='Não foi possível consultar o serviço OpenWA local.')


def obter_qr():
    cliente = cliente_local()
    if not cliente.session_id:
        raise mensagens_openwa.OpenWAError('Conecte uma sessão local antes de solicitar o QR Code.')
    try:
        estado = cliente.status()
        if not isinstance(estado, dict) or estado.get('status') != 'qr_ready':
            raise mensagens_openwa.OpenWAError('O QR Code ainda não está disponível.')
        resposta = cliente.request('GET', cliente.caminho_sessao() + '/qr')
    except mensagens_openwa.OpenWAError:
        raise
    except (ValueError, OSError, RuntimeError):
        raise mensagens_openwa.OpenWAError('Não foi possível obter o QR Code do OpenWA local.') from None
    valor = resposta.get('qrCode') if isinstance(resposta, dict) else None
    prefixo = 'data:image/png;base64,'
    if not isinstance(valor, str) or not valor.startswith(prefixo):
        raise mensagens_openwa.OpenWAError('O OpenWA retornou um QR Code em formato inválido.')
    try:
        imagem = base64.b64decode(valor[len(prefixo):], validate=True)
    except (binascii.Error, ValueError):
        raise mensagens_openwa.OpenWAError('O OpenWA retornou um QR Code em formato inválido.') from None
    if not imagem.startswith(PNG_MAGIC):
        raise mensagens_openwa.OpenWAError('O OpenWA retornou uma imagem que não é PNG.')
    return imagem


def _salvar_session_id(session_id):
    """Atualiza somente a linha de sessão, mantendo intactas as outras linhas."""
    caminho_qa = os.environ.get('PAINEL_OPENWA_CONFIG')
    caminho = Path(caminho_qa) if caminho_qa else mensagens_openwa.CONFIG
    conteudo = caminho.read_text(encoding='utf-8') if caminho.exists() else ''
    linhas = [linha for linha in conteudo.splitlines()
              if not linha.startswith('OPENWA_SESSION_ID=')]
    linhas.append('OPENWA_SESSION_ID=' + session_id)
    caminho.write_text('\n'.join(linhas) + '\n', encoding='utf-8')


def conectar_sessao():
    cliente = cliente_local()
    resposta = cliente.request('GET', '/sessions')
    if not isinstance(resposta, list):
        raise mensagens_openwa.OpenWAError('O OpenWA retornou uma lista de sessões inválida.')
    if cliente.session_id:
        sessao = next((item for item in resposta
                       if isinstance(item, dict) and item.get('id') == cliente.session_id), None)
        if sessao is None:
            raise mensagens_openwa.OpenWAError('A sessão configurada não foi encontrada no OpenWA local.')
    else:
        sessao = next((item for item in resposta
                       if isinstance(item, dict) and item.get('name') == NOME_SESSAO), None)
        if sessao is None:
            sessao = cliente.request('POST', '/sessions', {'name': NOME_SESSAO})
    if not isinstance(sessao, dict) or not isinstance(sessao.get('id'), str) or not sessao['id']:
        raise mensagens_openwa.OpenWAError('O OpenWA retornou uma sessão inválida.')
    cliente.session_id = sessao['id']
    _salvar_session_id(cliente.session_id)
    if not sessao.get('engineLoaded') and sessao.get('status') != 'ready':
        cliente.request('POST', cliente.caminho_sessao() + '/start')
    return None
