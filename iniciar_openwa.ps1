$ErrorActionPreference = 'Stop'
Push-Location -LiteralPath $PSScriptRoot
try {
    $pythonProjeto = Join-Path $PSScriptRoot 'venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $pythonProjeto)) {
        $pythonProjeto = (Get-Command python -ErrorAction Stop).Source
    }
    & $pythonProjeto mensagens_openwa.py configurar
    if ($LASTEXITCODE -ne 0) { throw 'Falha ao criar a configuração do OpenWA.' }
    docker info --format '{{.ServerVersion}}'
    if ($LASTEXITCODE -ne 0) { throw 'Abra o Docker Desktop e tente novamente.' }
    docker compose --env-file openwa.env -f compose.openwa.yaml pull --quiet
    if ($LASTEXITCODE -ne 0) { throw 'Não foi possível baixar a imagem do OpenWA.' }
    docker compose --env-file openwa.env -f compose.openwa.yaml up -d --wait --wait-timeout 180
    if ($LASTEXITCODE -ne 0) { throw 'OpenWA não ficou pronto. Confira docker compose logs.' }
    Write-Host 'OpenWA disponível em http://127.0.0.1:2785'
    Write-Host 'Para conectar o WhatsApp: .\venv\Scripts\python.exe mensagens_openwa.py conectar'
} finally {
    Pop-Location
}
