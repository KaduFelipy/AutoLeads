"""Inicialização e encerramento seguros do painel local."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path


class Lancador:
    def __init__(self, runtime, raiz: Path, porta: int = 8000):
        self.runtime = runtime
        self.raiz = Path(raiz).resolve()
        self.porta = porta
        self.runtime.porta = porta

    def iniciar(self, sem_openwa=False, abrir_navegador=True):
        with self.runtime.bloqueio(self.raiz):
            existentes = self.runtime.processos_do_projeto(self.raiz)
            if existentes:
                # Inclui instalações antigas: nunca inicia ao lado de um componente que já está ativo.
                return "ja_ativo"

            iniciados = []
            try:
                if not sem_openwa:
                    if not self.runtime.iniciar_openwa(self.raiz):
                        print("Aviso: Docker/OpenWA indisponível; o painel iniciará sem conexão WhatsApp.")
                self.runtime.preparar_banco(self.raiz)
                iniciados.append(self.runtime.iniciar_processo("worker", [], self.raiz))
                iniciados.append(self.runtime.iniciar_processo("web", [], self.raiz))
                if not self.runtime.aguardar_painel(self.porta, iniciados, self.raiz):
                    raise RuntimeError("O painel não respondeu em http://127.0.0.1:%s/" % self.porta)
                self.runtime.salvar_estado(self.raiz, iniciados)
            except Exception:
                for identidade in reversed(iniciados):
                    if self.runtime.processo_ativo(identidade, self.raiz):
                        self.runtime.encerrar_processo(identidade, self.raiz)
                raise
            if abrir_navegador:
                self.runtime.abrir_painel("http://127.0.0.1:%s/" % self.porta)
            return "iniciado"

    def parar(self):
        with self.runtime.bloqueio(self.raiz):
            processos = self.runtime.processos_do_projeto(self.raiz)
            for identidade in processos:
                if self.runtime.processo_ativo(identidade, self.raiz):
                    self.runtime.encerrar_processo(identidade, self.raiz)
            openwa_ok = self.runtime.parar_openwa(self.raiz)
            self.runtime.remover_estado(self.raiz)
            if processos:
                return "parado" if openwa_ok else "parado_openwa_indisponivel"
            return "ja_parado" if openwa_ok else "ja_parado_openwa_indisponivel"


class RuntimeWindows:
    """Adaptador de sistema; só atua em processos que correspondem ao projeto."""

    def _powershell(self, script, check=True):
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if check and result.returncode:
            raise RuntimeError("Não foi possível consultar processos locais do projeto.")
        return result

    def _consultar(self, raiz, pid=None):
        python_paths = [
            str(Path(raiz) / "venv" / "Scripts" / "python.exe"),
            str(Path(raiz) / "venv" / "Scripts" / "pythonw.exe"),
        ]
        cfg = Path(raiz) / "venv" / "pyvenv.cfg"
        if cfg.is_file():
            for line in cfg.read_text(encoding="utf-8", errors="replace").splitlines():
                key, separator, value = line.partition("=")
                if separator and key.strip().lower() == "home":
                    python_paths.append(str(Path(value.strip()) / "python.exe"))
                    python_paths.append(str(Path(value.strip()) / "pythonw.exe"))
                    break
        python_paths = "','".join(path.replace("'", "''") for path in python_paths)
        manage_path = str(Path(raiz) / "manage.py").replace("'", "''")
        pid_clause = ""
        if pid is not None:
            pid_clause = " | Where-Object { $_.ProcessId -eq %d }" % int(pid)
        script = (
            "$ErrorActionPreference='Stop'; $pythonPaths=@('%s'); $manage='%s'; "
            "$exePattern='(?:'+(($pythonPaths | ForEach-Object {[regex]::Escape($_)}) -join '|')+')'; "
            "$managePattern=[regex]::Escape($manage); "
            "$workerPattern='(?i)^\\s*\"?'+$exePattern+'\"?\\s+\"?'+$managePattern+'\"?\\s+trabalhador(?:\\s|$)'; "
            "$webPattern='(?i)^\\s*\"?'+$exePattern+'\"?\\s+\"?'+$managePattern+'\"?\\s+runserver(?:\\s|$)'; "
            "$rows=Get-CimInstance -ErrorAction Stop Win32_Process%s | Where-Object { "
            "$_.CommandLine -and $_.ExecutablePath -in $pythonPaths -and "
            "($_.CommandLine -match $workerPattern -or $_.CommandLine -match $webPattern) "
            "} | ForEach-Object { [PSCustomObject]@{ pid=[int]$_.ProcessId; "
            "created=$($_.CreationDate.ToUniversalTime().ToString('o')); parent_pid=[int]$_.ParentProcessId; "
            "name=$(if ($_.CommandLine -match $workerPattern) {'worker'} else {'web'}) } }; "
            "$rows | ConvertTo-Json -Compress" % (python_paths, manage_path, pid_clause)
        )
        result = self._powershell(script, check=False)
        if result.returncode:
            raise RuntimeError("Não foi possível verificar com segurança os processos do projeto.")
        if not result.stdout.strip():
            return []
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("A resposta de consulta de processos do Windows estava inválida.") from exc
        if data is None:
            return []
        return data if isinstance(data, list) else [data]

    def processos_do_projeto(self, raiz):
        return self._consultar(raiz)

    def processo_ativo(self, identidade, raiz):
        atual = self._consultar(raiz, identidade["pid"])
        return any(
            int(p["pid"]) == int(identidade["pid"])
            and p["created"] == identidade["created"]
            and p["name"] == identidade["name"]
            for p in atual
        )

    def encerrar_processo(self, identidade, raiz):
        if not self.processo_ativo(identidade, raiz):
            return
        result = subprocess.run(
            ["taskkill.exe", "/PID", str(int(identidade["pid"])), "/F", "/T"],
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode and self.processo_ativo(identidade, raiz):
            raise RuntimeError("Não foi possível encerrar um processo validado do painel.")

    @contextmanager
    def bloqueio(self, raiz):
        caminho = Path(raiz) / ".launcher.lock"
        arquivo = open(caminho, "a+b")
        try:
            arquivo.seek(0)
            if arquivo.read(1) == b"":
                arquivo.write(b"0")
                arquivo.flush()
            arquivo.seek(0)
            if os.name == "nt":
                import msvcrt
                try:
                    msvcrt.locking(arquivo.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise RuntimeError("Outro comando iniciar.py ou parar.py já está em andamento.") from exc
            else:
                import fcntl
                try:
                    fcntl.flock(arquivo.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    raise RuntimeError("Outro comando iniciar.py ou parar.py já está em andamento.") from exc
            yield
        finally:
            try:
                arquivo.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(arquivo.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(arquivo.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            arquivo.close()

    def _python(self, raiz):
        exe = Path(raiz) / "venv" / "Scripts" / "python.exe"
        if not exe.is_file():
            raise RuntimeError("Python do projeto não encontrado em venv\\Scripts\\python.exe.")
        return str(exe)

    def _executar(self, comando, raiz, check=False):
        result = subprocess.run(
            comando, cwd=raiz, capture_output=True, text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if check and result.returncode:
            raise RuntimeError("O comando local falhou (código %s)." % result.returncode)
        return result

    def preparar_banco(self, raiz):
        self._executar([self._python(raiz), str(Path(raiz) / "manage.py"), "preparar_banco"], raiz, check=True)

    def iniciar_openwa(self, raiz):
        try:
            docker = subprocess.run(
                ["docker.exe", "info", "--format", "{{.ServerVersion}}"], cwd=raiz,
                capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except FileNotFoundError:
            return False
        if docker.returncode:
            return False
        compose = ["docker.exe", "compose", "--env-file", "openwa.env", "-f", "compose.openwa.yaml", "up", "-d", "openwa"]
        return self._executar(compose, raiz).returncode == 0

    def parar_openwa(self, raiz):
        comando = ["docker.exe", "compose", "--env-file", "openwa.env", "-f", "compose.openwa.yaml", "stop", "openwa"]
        try:
            result = self._executar(comando, raiz)
            return result.returncode == 0
        except FileNotFoundError:
            return False

    def iniciar_processo(self, nome, comando, raiz):
        python = self._python(raiz)
        manage = str(Path(raiz) / "manage.py")
        args = [python, manage, "trabalhador"] if nome == "worker" else [
            python, manage, "runserver", "127.0.0.1:%s" % getattr(self, "porta", 8000), "--noreload", "--insecure"
        ]
        log_dir = Path(tempfile.gettempdir()) / "ScrapGoogleMaps"
        log_dir.mkdir(parents=True, exist_ok=True)
        sufixo = "%s-%s" % (nome, time.time_ns())
        stdout = open(log_dir / (sufixo + ".out.log"), "ab")
        stderr = open(log_dir / (sufixo + ".err.log"), "ab")
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            process = subprocess.Popen(args, cwd=raiz, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, creationflags=flags)
        finally:
            stdout.close()
            stderr.close()
        try:
            # Consulta identidade e criação antes de manter o processo em segundo plano.
            for _ in range(30):
                rows = self._consultar(raiz, process.pid)
                if rows:
                    return rows[0]
                if process.poll() is not None:
                    break
                time.sleep(0.1)
            raise RuntimeError("O processo %s não apareceu como processo do projeto." % nome)
        except Exception:
            self._encerrar_processo_criado(process)
            raise

    @staticmethod
    def _encerrar_processo_criado(process):
        if process.poll() is not None:
            return
        try:
            subprocess.run(
                ["taskkill.exe", "/PID", str(int(process.pid)), "/F", "/T"],
                capture_output=True,
                text=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except FileNotFoundError:
            process.terminate()

    def aguardar_painel(self, porta, processos, raiz):
        limite = time.monotonic() + 30
        while time.monotonic() < limite:
            web_ativo = any(p["name"] == "web" and self.processo_ativo(p, raiz) for p in processos)
            worker_ativo = any(p["name"] == "worker" and self.processo_ativo(p, raiz) for p in processos)
            if not web_ativo or not worker_ativo:
                return False
            try:
                with urllib.request.urlopen("http://127.0.0.1:%s/" % porta, timeout=1) as response:
                    if response.status < 500:
                        return True
            except Exception:
                time.sleep(0.25)
        return False

    def abrir_painel(self, url):
        import webbrowser
        webbrowser.open(url)

    def salvar_estado(self, raiz, processos):
        alvo = Path(raiz) / ".launcher-state.json"
        temporario = alvo.with_suffix(".tmp")
        temporario.write_text(json.dumps(processos), encoding="utf-8")
        os.replace(temporario, alvo)

    def remover_estado(self, raiz):
        (Path(raiz) / ".launcher-state.json").unlink(missing_ok=True)


def criar_lancador(raiz, porta=8000):
    return Lancador(RuntimeWindows(), Path(raiz), porta)
