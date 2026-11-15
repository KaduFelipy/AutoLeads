from django.test import TestCase

from painel.forms import CampanhaForm, TextoForm
from mensagens_openwa import renderizar_mensagem


class TextoFormCamposCaseInsensitiveTest(TestCase):
    def test_texto_com_campo_misto_e_salvo_sem_alteracao(self):
        form = TextoForm(data={"nome": "Teste", "corpo": "Olá, {Nome}!"})

        self.assertTrue(form.is_valid(), form.errors)
        texto = form.save()

        self.assertEqual(texto.corpo, "Olá, {Nome}!")
        self.assertEqual(renderizar_mensagem(texto.corpo, {"nome": "Empresa Sintética"}), "Olá, Empresa Sintética!")

    def test_campanha_aceita_campo_misto_com_campos_obrigatorios(self):
        form = CampanhaForm(
            data={
                "nome": "Campanha de teste",
                "texto": "",
                "mensagem": "Olá, {nOmE}!",
                "limite": 5,
                "intervalo": 5,
                "telefone": "",
            }
        )

        self.assertTrue(form.is_valid(), form.errors)
