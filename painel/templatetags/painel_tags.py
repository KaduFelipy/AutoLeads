from urllib.parse import urlsplit

from django import template

register = template.Library()


@register.filter
def http_url(value):
    """Retorna somente URLs externas HTTP/HTTPS para links capturados."""
    if not value:
        return ''
    try:
        partes = urlsplit(value)
        partes.port
        if (partes.scheme.lower() in ('http', 'https') and partes.netloc and
                partes.username is None and partes.password is None):
            return value
    except (TypeError, ValueError):
        pass
    return ''
