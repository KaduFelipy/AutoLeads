import unicodedata

from django.db import migrations


def _normalizar_termo(termo):
    base = ' '.join(unicodedata.normalize('NFKC', termo or '').casefold().split())
    decomposed = unicodedata.normalize('NFKD', base)
    return ''.join(char for char in decomposed if not unicodedata.combining(char))


def adicionar_regra_mecanica(apps, schema_editor):
    RegraTag = apps.get_model('painel', 'RegraTag')
    Tag = apps.get_model('painel', 'Tag')
    alias = schema_editor.connection.alias

    regras = RegraTag.objects.using(alias).all().only('pk', 'termo')
    if any(_normalizar_termo(regra.termo) == 'mecanica' for regra in regras):
        return

    regra, criada = RegraTag.objects.using(alias).get_or_create(
        termo='mecanica', defaults={'prioridade': 0}
    )
    if criada:
        ramo, _ = Tag.objects.using(alias).get_or_create(nome='Mecânica', tipo='ramo')
        regra.tags.add(ramo)


class Migration(migrations.Migration):
    dependencies = [('painel', '0004_autorizacaoreenvio_destinatario_and_more')]

    operations = [migrations.RunPython(adicionar_regra_mecanica, migrations.RunPython.noop)]
