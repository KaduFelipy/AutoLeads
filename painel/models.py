from django.db import models
from django.utils import timezone


class Empresa(models.Model):
    nome = models.TextField()
    endereco = models.TextField(null=True, blank=True)
    telefone = models.TextField(null=True, blank=True)
    site = models.TextField(null=True, blank=True)
    avaliacao = models.FloatField(null=True, blank=True)
    maps_id = models.TextField(unique=True)
    chave = models.TextField(null=True, blank=True, unique=True)

    class Meta:
        db_table = 'empresas'
        managed = False

    def __str__(self):
        return self.nome


class Tag(models.Model):
    class Tipo(models.TextChoices):
        RAMO = 'ramo', 'Ramo'
        CATEGORIA = 'categoria', 'Categoria'

    nome = models.CharField(max_length=120)
    tipo = models.CharField(max_length=16, choices=Tipo.choices)

    class Meta:
        constraints = [models.UniqueConstraint(fields=('nome', 'tipo'), name='painel_tag_nome_tipo_uniq')]
        ordering = ('tipo', 'nome')

    def __str__(self):
        return self.nome


class Perfil(models.Model):
    empresa = models.OneToOneField(Empresa, on_delete=models.CASCADE, related_name='perfil')
    tags = models.ManyToManyField(Tag, blank=True, related_name='perfis')
    tags_suprimidas = models.JSONField(default=list, blank=True)


class RegraTag(models.Model):
    termo = models.CharField(max_length=200, unique=True)
    prioridade = models.PositiveIntegerField(default=0)
    tags = models.ManyToManyField(Tag, related_name='regras')

    class Meta:
        ordering = ('-prioridade', 'termo')


class Pesquisa(models.Model):
    class Estado(models.TextChoices):
        PENDENTE = 'pendente', 'Pendente'
        PROCESSANDO = 'processando', 'Processando'
        CONCLUIDA = 'concluida', 'Concluída'
        FALHA = 'falha', 'Falha'

    termos = models.TextField()
    limite = models.PositiveIntegerField(null=True, blank=True)
    estado = models.CharField(max_length=16, choices=Estado.choices, default=Estado.PENDENTE)
    processadas = models.PositiveIntegerField(default=0)
    erro = models.TextField(blank=True, default='')
    inicio = models.DateTimeField(null=True, blank=True)
    fim = models.DateTimeField(null=True, blank=True)
    criada_em = models.DateTimeField(auto_now_add=True)


class ResultadoPesquisa(models.Model):
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE, related_name='resultados')
    pesquisa = models.ForeignKey(Pesquisa, on_delete=models.CASCADE, related_name='resultados')
    termo = models.TextField()
    url = models.TextField(blank=True, default='')
    capturado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=('empresa', 'pesquisa', 'termo'), name='painel_resultado_origem_uniq')]


class Texto(models.Model):
    nome = models.CharField(max_length=160)
    corpo = models.TextField()
    arquivado = models.BooleanField(default=False)
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)


class Campanha(models.Model):
    class Estado(models.TextChoices):
        RASCUNHO = 'rascunho', 'Rascunho'
        ATIVA = 'ativa', 'Ativa'
        PAUSADA = 'pausada', 'Pausada'
        CONCLUIDA = 'concluida', 'Concluída'

    nome = models.CharField(max_length=160)
    texto_snapshot = models.TextField()
    filtros_snapshot = models.JSONField(default=dict, blank=True)
    exclusoes = models.JSONField(default=list, blank=True)
    limite = models.PositiveIntegerField()
    session_id = models.CharField(max_length=160)
    intervalo = models.FloatField(default=5)
    estado = models.CharField(max_length=16, choices=Estado.choices, default=Estado.RASCUNHO)
    motivo_pausa = models.TextField(blank=True, default='')
    proximo_envio = models.DateTimeField(null=True, blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)
    iniciada_em = models.DateTimeField(null=True, blank=True)


class Destinatario(models.Model):
    campanha = models.ForeignKey(Campanha, on_delete=models.CASCADE, related_name='destinatarios')
    empresa = models.ForeignKey(Empresa, on_delete=models.PROTECT, related_name='destinatarios')
    empresas_relacionadas = models.JSONField(default=list, blank=True)
    ordem = models.PositiveIntegerField()
    telefone = models.CharField(max_length=20)
    mensagem = models.TextField()
    mensagem_hash = models.CharField(max_length=64)
    bloqueado_motivo = models.TextField(blank=True, default='')
    bloqueado_em = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ('ordem',)
        constraints = [models.UniqueConstraint(fields=('campanha', 'telefone'), name='painel_destinatario_camp_telefone_uniq')]


class Reserva(models.Model):
    empresa_id = models.IntegerField()
    session_id = models.TextField()
    telefone = models.TextField()
    mensagem = models.TextField()
    mensagem_hash = models.TextField()
    status = models.TextField()
    message_id = models.TextField(null=True, blank=True)
    erro = models.TextField(null=True, blank=True)
    criado_em = models.DateTimeField(default=timezone.now)
    atualizado_em = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'envios_whatsapp'
        managed = False
        unique_together = (('session_id', 'telefone', 'mensagem_hash'),)


class Tentativa(models.Model):
    class Estado(models.TextChoices):
        FILA = 'fila', 'Na fila'
        ENVIANDO = 'enviando', 'Enviando'
        ACEITA = 'aceita', 'Aceita'
        ENVIADO = 'enviado', 'Enviado'
        ENTREGUE = 'entregue', 'Entregue'
        LIDO = 'lido', 'Lido'
        FALHA = 'falha', 'Falha'
        INCERTO = 'incerto', 'Pendente de verificação'

    destinatario = models.ForeignKey(Destinatario, on_delete=models.PROTECT, related_name='tentativas')
    reserva = models.ForeignKey(Reserva, on_delete=models.PROTECT, related_name='tentativas')
    estado = models.CharField(max_length=16, choices=Estado.choices, default=Estado.FILA)
    telefone = models.CharField(max_length=20, blank=True, default='')
    session_id = models.CharField(max_length=160, blank=True, default='')
    mensagem = models.TextField(blank=True, default='')
    mensagem_hash = models.CharField(max_length=64, blank=True, default='')
    chat_id = models.TextField(null=True, blank=True)
    message_id = models.TextField(null=True, blank=True)
    erro = models.TextField(blank=True, default='')
    iniciado_em = models.DateTimeField(null=True, blank=True)
    confirmado_em = models.DateTimeField(null=True, blank=True)
    ultima_verificacao = models.DateTimeField(null=True, blank=True)
    atualizado_em = models.DateTimeField(auto_now=True)
    criado_em = models.DateTimeField(auto_now_add=True)


class Evento(models.Model):
    tentativa = models.ForeignKey(Tentativa, on_delete=models.CASCADE, related_name='eventos')
    estado = models.CharField(max_length=16, choices=Tentativa.Estado.choices)
    detalhe = models.TextField(blank=True, default='')
    criado_em = models.DateTimeField(auto_now_add=True)


class AutorizacaoReenvio(models.Model):
    tentativa_anterior = models.ForeignKey(Tentativa, on_delete=models.PROTECT, related_name='autorizacoes_reenvio')
    destinatario = models.OneToOneField(Destinatario, on_delete=models.PROTECT, null=True, blank=True,
                                        related_name='autorizacao_reenvio')
    motivo = models.TextField()
    criada_em = models.DateTimeField(auto_now_add=True)
    consumida_em = models.DateTimeField(null=True, blank=True)


class HistoricoTag(models.Model):
    class Acao(models.TextChoices):
        ADICIONADA = 'adicionada', 'Adicionada'
        REMOVIDA = 'removida', 'Removida'

    class Origem(models.TextChoices):
        AUTOMATICA = 'automatica', 'Automática'
        MANUAL = 'manual', 'Manual'

    empresa = models.ForeignKey(Empresa, on_delete=models.PROTECT, related_name='historico_tags')
    tag = models.ForeignKey(Tag, on_delete=models.SET_NULL, null=True, blank=True)
    tag_nome = models.CharField(max_length=120)
    tag_tipo = models.CharField(max_length=16)
    acao = models.CharField(max_length=16, choices=Acao.choices)
    origem = models.CharField(max_length=16, choices=Origem.choices)
    pesquisa = models.ForeignKey(Pesquisa, on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name='alteracoes_tags')
    criado_em = models.DateTimeField(auto_now_add=True)
