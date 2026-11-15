from collections import OrderedDict

from django import forms

from .models import Empresa, RegraTag, Tag, Texto


class PesquisaForm(forms.Form):
    termos = forms.CharField(label='Termos de pesquisa', widget=forms.Textarea(attrs={'rows': 4, 'placeholder': 'Uma pesquisa por linha, por exemplo: retífica em Pato Branco'}), help_text='Informe um termo por linha. Inclua a cidade no termo.')
    limite = forms.IntegerField(label='Limite de resultados por termo', required=False, min_value=1, max_value=1000, help_text='Opcional. Deixe vazio para usar o limite padrão do scraper.')

    def clean_termos(self):
        termos = [linha.strip() for linha in self.cleaned_data['termos'].splitlines() if linha.strip()]
        if not termos:
            raise forms.ValidationError('Informe ao menos um termo.')
        if len(termos) > 30:
            raise forms.ValidationError('Informe no máximo 30 termos por pesquisa.')
        return '\n'.join(dict.fromkeys(termos))


class EmpresaForm(forms.ModelForm):
    tags = forms.ModelMultipleChoiceField(label='Tags', queryset=Tag.objects.all(), required=False, widget=forms.SelectMultiple(attrs={'size': 6}), help_text='Use Ctrl ou Command para selecionar várias tags.')

    class Meta:
        model = Empresa
        fields = ('nome', 'endereco', 'telefone', 'site', 'avaliacao')
        labels = {'nome': 'Nome', 'endereco': 'Endereço', 'telefone': 'Telefone', 'site': 'Site', 'avaliacao': 'Avaliação'}
        widgets = {'endereco': forms.Textarea(attrs={'rows': 3})}

    def clean_site(self):
        from urllib.parse import urlsplit
        site = (self.cleaned_data.get('site') or '').strip()
        if site:
            try:
                partes = urlsplit(site)
            except ValueError:
                raise forms.ValidationError('Informe um endereço completo iniciado por http:// ou https://.') from None
            try:
                partes.port
            except ValueError:
                raise forms.ValidationError('Informe um endereço completo iniciado por http:// ou https://.') from None
            if (partes.scheme.lower() not in ('http', 'https') or not partes.netloc or
                    partes.username is not None or partes.password is not None):
                raise forms.ValidationError('Informe um endereço completo iniciado por http:// ou https://.')
        return site or None

    def clean_avaliacao(self):
        valor = self.cleaned_data.get('avaliacao')
        if valor is not None and not 0 <= valor <= 5:
            raise forms.ValidationError('A avaliação deve ficar entre 0 e 5.')
        return valor


class ContatoFiltroForm(forms.Form):
    nome = forms.CharField(label='Nome', required=False)
    cidade = forms.CharField(label='Cidade ou região', required=False)
    ramos = forms.ModelMultipleChoiceField(label='Ramo', queryset=Tag.objects.filter(tipo=Tag.Tipo.RAMO), required=False, widget=forms.SelectMultiple(attrs={'size': 4}))
    categorias = forms.ModelMultipleChoiceField(label='Categoria', queryset=Tag.objects.filter(tipo=Tag.Tipo.CATEGORIA), required=False, widget=forms.SelectMultiple(attrs={'size': 4}))
    telefone = forms.ChoiceField(label='Telefone', required=False, choices=(('', 'Todos'), ('sim', 'Com telefone'), ('nao', 'Sem telefone')))
    disparo = forms.ChoiceField(label='Último disparo', required=False, choices=(('', 'Qualquer data'), ('nunca', 'Nunca recebeu'), ('sem_disparo_desde', 'Sem disparo desde')))
    desde = forms.DateField(label='Sem disparo desde', required=False, widget=forms.DateInput(attrs={'type': 'date'}))

    def clean(self):
        data = super().clean()
        if data.get('disparo') == 'sem_disparo_desde' and not data.get('desde'):
            self.add_error('desde', 'Informe a data inicial.')
        return data


class TextoForm(forms.ModelForm):
    class Meta:
        model = Texto
        fields = ('nome', 'corpo')
        labels = {'nome': 'Nome do texto', 'corpo': 'Mensagem'}
        widgets = {'corpo': forms.Textarea(attrs={'rows': 8, 'placeholder': 'Olá, {nome}!'} )}

    def clean_corpo(self):
        corpo = self.cleaned_data['corpo']
        if not 1 <= len(corpo) <= 4096:
            raise forms.ValidationError('A mensagem deve ter entre 1 e 4096 caracteres.')
        from mensagens_openwa import renderizar_mensagem
        try:
            renderizar_mensagem(corpo, {campo: 'x' for campo in ('nome', 'endereco', 'telefone', 'site', 'avaliacao')})
        except ValueError as erro:
            raise forms.ValidationError(str(erro)) from None
        return corpo


class CampanhaFiltroForm(forms.Form):
    cidade = forms.CharField(label='Cidade ou região', required=False)
    com_telefone = forms.ChoiceField(label='Presença de telefone', required=False, initial='sim', choices=(('sim', 'Com telefone'), ('', 'Qualquer contato'), ('nao', 'Sem telefone')))
    ultimo_disparo = forms.ChoiceField(label='Último disparo', required=False, choices=(('', 'Qualquer data'), ('nunca', 'Nunca recebeu'), ('sem_disparo_desde', 'Sem disparo desde')))
    desde = forms.DateField(label='Sem disparo desde', required=False, widget=forms.DateInput(attrs={'type': 'date'}))

    def clean(self):
        data = super().clean()
        if data.get('ultimo_disparo') == 'sem_disparo_desde' and not data.get('desde'):
            self.add_error('desde', 'Informe a data inicial.')
        return data


class CampanhaForm(CampanhaFiltroForm):
    nome = forms.CharField(label='Nome da campanha', max_length=160)
    texto = forms.ModelChoiceField(label='Texto salvo', queryset=Texto.objects.filter(arquivado=False), required=False)
    mensagem = forms.CharField(label='Ou escreva uma mensagem', required=False, widget=forms.Textarea(attrs={'rows': 5}), help_text='Use {nome}, {endereco}, {telefone}, {site} ou {avaliacao}.')
    modo = forms.ChoiceField(label='Modo de seleção', required=False, choices=(('', 'Selecione'), ('quantidades', 'Quantidade por grupo'), ('manual', 'Escolher clientes')))
    empresa_ids = forms.CharField(required=False, widget=forms.HiddenInput)
    tags = forms.ModelMultipleChoiceField(label='Tags', queryset=Tag.objects.all(), required=False, widget=forms.SelectMultiple(attrs={'size': 5}))
    limite = forms.IntegerField(label='Quantidade máxima', required=False, min_value=1, max_value=1000, initial=50)
    intervalo = forms.FloatField(label='Intervalo entre mensagens (segundos)', min_value=1, max_value=3600, initial=5)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['tags'].label_from_instance = lambda tag: f'{tag.get_tipo_display()} · {tag.nome}'
        from painel.services.selection import listar_grupos
        self.grupos = listar_grupos({})
        self.chaves_grupo = {grupo['chave'] for grupo in self.grupos}
        self.campos_cota = {}
        for grupo in self.grupos:
            sufixo = grupo['chave'].replace(':', '_')
            nome_campo = 'quantidade_' + sufixo
            self.fields[nome_campo] = forms.IntegerField(
                label=f"Quantidade · {grupo['nome']}", required=False, min_value=0, max_value=1000,
                widget=forms.NumberInput(attrs={'min': '0', 'max': '1000', 'step': '1', 'data-quota': ''}),
            )
            self.campos_cota[nome_campo] = grupo['chave']
        ordem = ('nome', 'texto', 'mensagem', 'intervalo', 'cidade', 'com_telefone',
                 'ultimo_disparo', 'desde', 'modo', 'empresa_ids', 'tags', 'limite',
                 *self.campos_cota)
        self.fields = OrderedDict((nome, self.fields[nome]) for nome in ordem if nome in self.fields)

    def clean(self):
        data = super().clean()
        if data.get('ultimo_disparo') == 'sem_disparo_desde' and not data.get('desde'):
            self.add_error('desde', 'Informe a data inicial.')
        if not data.get('texto') and not (data.get('mensagem') or '').strip():
            raise forms.ValidationError('Selecione um texto salvo ou escreva a mensagem.')
        mensagem = (data.get('mensagem') or '').strip()
        if mensagem and not data.get('texto'):
            from mensagens_openwa import renderizar_mensagem
            try:
                renderizar_mensagem(mensagem, {campo: 'x' for campo in ('nome', 'endereco', 'telefone', 'site', 'avaliacao')})
            except ValueError as erro:
                self.add_error('mensagem', str(erro))
        if self.is_bound and data.get('modo'):
            from painel.services.selection import validar_selecao
            dados = self.data
            modo = data['modo']
            if data.get('tags'):
                self.add_error(None, 'Não combine tags legadas com uma seleção por grupos.')
            ids_raw = dados.getlist('empresa_ids') if hasattr(dados, 'getlist') else []
            cotas_preenchidas = {
                chave: data.get(campo) for campo, chave in self.campos_cota.items()
                if data.get(campo) not in (None, '')
            }
            extras_cota = [key for key in dados.keys() if key.startswith('quantidade_') and key not in self.campos_cota]
            if extras_cota:
                self.add_error(None, 'A seleção contém grupo desconhecido.')
            if modo == 'manual':
                if cotas_preenchidas:
                    self.add_error(None, 'Remova as cotas para usar a seleção manual.')
                if len(ids_raw) != 1:
                    # Também aceita formulário comum que envia cada checkbox como valor repetido.
                    ids_raw = dados.getlist('empresa_ids')
                try:
                    ids = [int(valor) for valor in ids_raw if str(valor).strip()]
                    if len(ids) != len([valor for valor in ids_raw if str(valor).strip()]):
                        raise ValueError('ID inválido')
                    selecao = validar_selecao({'modo': 'manual', 'empresa_ids': ids})
                    data['selecao'] = selecao
                except (ValueError, TypeError) as erro:
                    self.add_error(None, str(erro))
            elif modo == 'quantidades':
                if any(str(valor).strip() for valor in ids_raw):
                    self.add_error(None, 'Remova os clientes marcados para usar cotas.')
                try:
                    selecao = validar_selecao({'modo': 'quantidades', 'quantidades': cotas_preenchidas})
                    data['selecao'] = selecao
                except (ValueError, TypeError) as erro:
                    self.add_error(None, str(erro))
        return data


class TagForm(forms.ModelForm):
    class Meta:
        model = Tag
        fields = ('nome', 'tipo')
        labels = {'nome': 'Nome', 'tipo': 'Tipo'}


class RegraTagForm(forms.ModelForm):
    class Meta:
        model = RegraTag
        fields = ('termo', 'prioridade', 'tags')
        labels = {'termo': 'Trecho do termo de pesquisa', 'prioridade': 'Prioridade', 'tags': 'Tags associadas'}
        widgets = {'tags': forms.SelectMultiple(attrs={'size': 5})}


class ReenvioForm(forms.Form):
    motivo = forms.CharField(label='Motivo do reenvio', min_length=3, max_length=500, widget=forms.Textarea(attrs={'rows': 3}))
