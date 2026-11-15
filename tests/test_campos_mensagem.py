import unittest

from mensagens_openwa import renderizar_mensagem


class RenderizarMensagemCamposCaseInsensitiveTest(unittest.TestCase):
    def test_aceita_variacoes_de_case_nos_campos_permitidos(self):
        empresa = {
            "nome": "Empresa Sintética",
            "endereco": "Rua de Teste, 10",
            "telefone": "11999990000",
            "site": "https://example.test",
            "avaliacao": "4,8",
        }

        self.assertEqual(
            renderizar_mensagem("{Nome}/{NOME}/{nome}", empresa),
            "Empresa Sintética/Empresa Sintética/Empresa Sintética",
        )
        self.assertEqual(
            renderizar_mensagem("{eNdErEcO} {TeLeFoNe} {SiTe} {AvAlIaCaO}", empresa),
            "Rua de Teste, 10 11999990000 https://example.test 4,8",
        )

    def test_valores_none_viram_vazio_e_zero_e_preservado(self):
        empresa = {"nome": None, "avaliacao": 0}

        self.assertEqual(renderizar_mensagem("[{NOME}] [{AVALIACAO}]", empresa), "[] [0]")

    def test_preserva_chaves_de_escape_dobradas(self):
        self.assertEqual(renderizar_mensagem("{{Nome}}", {"nome": "Ignorado"}), "{Nome}")

    def test_rejeita_campos_e_formatacoes_nao_permitidos(self):
        for modelo in ("{Nome.x}", "{Nome[0]}", "{Outro}", "{Nome!r}", "{Nome:>10}"):
            with self.subTest(modelo=modelo):
                with self.assertRaises(ValueError):
                    renderizar_mensagem(modelo, {"nome": "Empresa Sintética"})


if __name__ == "__main__":
    unittest.main()
