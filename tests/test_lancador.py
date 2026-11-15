import tempfile
import unittest
from types import SimpleNamespace
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from controle_lancador import Lancador, RuntimeWindows


class RuntimeFalso:
    def __init__(self):
        self.processos = []
        self.encerrados = []
        self.preparou = 0
        self.openwa = []
        self.pronto = True
        self.falha_worker = False
        self.falha_web = False
        self.abrir = 0
        self.stop_openwa_ok = True

    @contextmanager
    def bloqueio(self, raiz):
        yield

    def salvar_estado(self, raiz, processos):
        self.estado = list(processos)

    def remover_estado(self, raiz):
        self.estado = []

    def processos_do_projeto(self, raiz):
        return list(self.processos)

    def iniciar_processo(self, nome, comando, raiz):
        if (nome == "worker" and self.falha_worker) or (nome == "web" and self.falha_web):
            raise RuntimeError("falha simulada")
        identidade = {"name": nome, "pid": len(self.processos) + 100, "created": 1}
        self.processos.append(identidade)
        return identidade

    def processo_ativo(self, identidade, raiz):
        return identidade in self.processos

    def encerrar_processo(self, identidade, raiz):
        self.encerrados.append(identidade)
        if identidade in self.processos:
            self.processos.remove(identidade)

    def preparar_banco(self, raiz):
        self.preparou += 1

    def iniciar_openwa(self, raiz):
        self.openwa.append("iniciar")
        return False

    def parar_openwa(self, raiz):
        self.openwa.append("parar")
        return self.stop_openwa_ok

    def aguardar_painel(self, porta, processos, raiz):
        return self.pronto

    def abrir_painel(self, url):
        self.abrir += 1


class LancadorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.raiz = Path(self.temp.name)
        self.runtime = RuntimeFalso()
        self.lancador = Lancador(self.runtime, self.raiz, porta=8000)

    def tearDown(self):
        self.temp.cleanup()

    def test_iniciar_prepara_banco_e_inicia_worker_e_painel_sem_openwa(self):
        resultado = self.lancador.iniciar(sem_openwa=True, abrir_navegador=False)
        self.assertEqual(resultado, "iniciado")
        self.assertEqual(self.runtime.preparou, 1)
        self.assertEqual([p["name"] for p in self.runtime.processos], ["worker", "web"])
    def test_instancia_existente_e_reaproveitada_sem_repetir_preparacao(self):
        self.runtime.processos.extend([
            {"name": "worker", "pid": 21, "created": 1},
            {"name": "web", "pid": 22, "created": 1},
        ])
        self.assertEqual(self.lancador.iniciar(sem_openwa=True), "ja_ativo")
        self.assertEqual(self.runtime.preparou, 0)
        self.assertEqual(len(self.runtime.processos), 2)

    def test_falha_parcial_encerrra_apenas_processo_criado_nesta_chamada(self):
        self.runtime.falha_web = True
        with self.assertRaisesRegex(RuntimeError, "falha simulada"):
            self.lancador.iniciar(sem_openwa=True)
        self.assertEqual([p["name"] for p in self.runtime.encerrados], ["worker"])

    def test_parar_encerra_processos_proprios_e_apenas_faz_compose_stop(self):
        self.runtime.processos.extend([
            {"name": "worker", "pid": 21, "created": 1},
            {"name": "web", "pid": 22, "created": 1},
        ])
        self.lancador.parar()
        self.assertEqual([p["name"] for p in self.runtime.encerrados], ["worker", "web"])
        self.assertEqual(self.runtime.openwa, ["parar"])

    def test_parar_sem_estado_e_seguro(self):
        self.assertEqual(self.lancador.parar(), "ja_parado")
        self.assertEqual(self.runtime.encerrados, [])

    def test_openwa_com_falha_de_stop_e_reportado_separadamente(self):
        self.runtime.stop_openwa_ok = False
        self.runtime.processos.extend([
            {"name": "worker", "pid": 21, "created": 1},
            {"name": "web", "pid": 22, "created": 1},
        ])
        self.assertEqual(self.lancador.parar(), "parado_openwa_indisponivel")

    def test_openwa_indisponivel_nao_impede_painel(self):
        self.assertEqual(self.lancador.iniciar(abrir_navegador=False), "iniciado")
        self.assertEqual(self.runtime.openwa, ["iniciar"])
        self.assertEqual([p["name"] for p in self.runtime.processos], ["worker", "web"])


class RuntimeWindowsTests(unittest.TestCase):
    def test_consulta_usa_caminhos_exatos_e_retorna_identidade(self):
        runtime = RuntimeWindows()
        resposta = '[{"pid":123,"created":"2026-10-05T12:00:00.0000000Z","name":"web"}]'
        with patch.object(runtime, "_powershell", return_value=SimpleNamespace(returncode=0, stdout=resposta)) as ps:
            processo = runtime.processos_do_projeto(Path("C:/Projeto ScrapGoogleMaps"))
        self.assertEqual(processo[0]["pid"], 123)
        script = ps.call_args.args[0]
        self.assertIn("venv", script)
        self.assertIn("manage.py", script)
        self.assertIn("$ErrorActionPreference='Stop'", script)
        self.assertIn("[regex]::Escape($manage)", script)
        self.assertIn("CommandLine -match $workerPattern", script)
        self.assertIn("(?:\\s|$)", script)

    def test_consulta_inclui_python_base_declarado_no_venv(self):
        with tempfile.TemporaryDirectory() as pasta:
            root = Path(pasta)
            (root / "venv").mkdir()
            (root / "venv" / "pyvenv.cfg").write_text("home = C:\\PythonBase\n", encoding="utf-8")
            runtime = RuntimeWindows()
            resposta = '[{"pid":124,"created":"agora","name":"worker"}]'
            with patch.object(runtime, "_powershell", return_value=SimpleNamespace(returncode=0, stdout=resposta)) as ps:
                runtime.processos_do_projeto(root)
            script = ps.call_args.args[0]
            self.assertIn("C:\\PythonBase\\python.exe", script)

    def test_encerramento_usa_arvore_somente_apos_revalidar_identidade(self):
        runtime = RuntimeWindows()
        identidade = {"pid": 123, "created": "horario", "name": "web"}
        with patch.object(runtime, "_consultar", return_value=[identidade]), patch("controle_lancador.subprocess.run", return_value=SimpleNamespace(returncode=0)) as run:
            runtime.encerrar_processo(identidade, Path("C:/Projeto"))
        self.assertEqual(run.call_args.args[0][-2:], ["/F", "/T"])

    def test_stop_openwa_retorna_falha_e_cim_nao_terminante_falha_fechado(self):
        runtime = RuntimeWindows()
        with patch.object(runtime, "_executar", return_value=SimpleNamespace(returncode=1)):
            self.assertFalse(runtime.parar_openwa(Path("C:/Projeto")))
        with patch.object(runtime, "_powershell", return_value=SimpleNamespace(returncode=0, stdout="")) as ps:
            runtime.processos_do_projeto(Path("C:/Projeto"))
        self.assertIn("$ErrorActionPreference='Stop'", ps.call_args.args[0])

    def test_startup_sem_identidade_encerra_arvore_que_acabou_de_criar(self):
        runtime = RuntimeWindows()
        processo = SimpleNamespace(pid=777, poll=lambda: None, terminate=lambda: None)
        with tempfile.TemporaryDirectory() as temp:
            log_dir = Path(temp)
            with patch.object(runtime, "_python", return_value="C:/Projeto/venv/Scripts/python.exe"), \
                 patch("controle_lancador.tempfile.gettempdir", return_value=temp), \
                 patch("controle_lancador.subprocess.Popen", return_value=processo), \
                 patch.object(runtime, "_consultar", return_value=[]), \
                 patch("controle_lancador.time.sleep"), \
                 patch("controle_lancador.subprocess.run", return_value=SimpleNamespace(returncode=0)) as run:
                with self.assertRaisesRegex(RuntimeError, "não apareceu"):
                    runtime.iniciar_processo("web", [], Path("C:/Projeto"))
        self.assertIn("/T", run.call_args.args[0])

    def test_process_pid_igual_com_timestamp_diferente_nao_e_validado(self):
        runtime = RuntimeWindows()
        identidade = {"pid": 123, "created": "original", "name": "web"}
        atual = {"pid": 123, "created": "reutilizado", "name": "web"}
        with patch.object(runtime, "_consultar", return_value=[atual]):
            self.assertFalse(runtime.processo_ativo(identidade, Path("C:/Projeto")))

    def test_consulta_falha_fechado_se_powershell_nao_responde(self):
        runtime = RuntimeWindows()
        with patch.object(runtime, "_powershell", return_value=SimpleNamespace(returncode=1, stdout="")):
            with self.assertRaisesRegex(RuntimeError, "verificar com segurança"):
                runtime.processos_do_projeto(Path("C:/Projeto"))

    def test_resposta_null_do_cim_significa_que_nao_ha_processos_do_projeto(self):
        runtime = RuntimeWindows()
        with patch.object(runtime, "_powershell", return_value=SimpleNamespace(returncode=0, stdout="null")):
            self.assertEqual(runtime.processos_do_projeto(Path("C:/Projeto")), [])

    def test_nao_encerra_pid_reutilizado_ou_alheio(self):
        runtime = RuntimeWindows()
        identidade = {"pid": 123, "created": "horario-antigo", "name": "web"}
        with patch.object(runtime, "_consultar", return_value=[]), patch("controle_lancador.subprocess.run") as run:
            runtime.encerrar_processo(identidade, Path("C:/Projeto"))
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
