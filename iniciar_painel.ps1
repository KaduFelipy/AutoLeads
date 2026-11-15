param(
    [ValidateRange(1024, 65535)]
    [int]$Porta = 8000,
    [switch]$SemOpenWA
)

$ErrorActionPreference = 'Stop'
$Projeto = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$Python = Join-Path $Projeto 'venv\Scripts\python.exe'
$ManagePy = Join-Path $Projeto 'manage.py'
$Compose = Join-Path $Projeto 'compose.openwa.yaml'
$ConfigOpenWA = Join-Path $Projeto 'openwa.env'
$Filhos = @()
$LogDir = Join-Path $env:TEMP 'ScrapGoogleMaps'

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw 'O ambiente Python do projeto não foi encontrado em venv\Scripts\python.exe.'
}
if (-not (Test-Path -LiteralPath $ManagePy -PathType Leaf)) {
    throw 'manage.py não foi encontrado na pasta do projeto.'
}

New-Item -ItemType Directory -Path $LogDir -Force | Out-Null

try {
    if (-not $SemOpenWA) {
        $Docker = Get-Command docker -ErrorAction SilentlyContinue
        if (-not $Docker) {
            Write-Warning 'Docker não está disponível. O painel iniciará sem conexão WhatsApp.'
        } elseif (-not (Test-Path -LiteralPath $ConfigOpenWA -PathType Leaf)) {
            Write-Warning 'openwa.env não existe. O painel iniciará sem conexão WhatsApp.'
        } elseif (-not (Test-Path -LiteralPath $Compose -PathType Leaf)) {
            Write-Warning 'A configuração Docker do OpenWA não foi encontrada. O painel iniciará sem conexão WhatsApp.'
        } else {
            & $Docker.Source info *> $null
            if ($LASTEXITCODE -ne 0) {
                Write-Warning 'O Docker Desktop não está pronto. O painel iniciará sem conexão WhatsApp.'
            } else {
                & $Docker.Source compose --env-file $ConfigOpenWA -f $Compose up -d *> $null
                if ($LASTEXITCODE -ne 0) {
                    Write-Warning 'O OpenWA não pôde ser iniciado. O painel iniciará sem conexão WhatsApp.'
                } else {
                    Write-Host 'OpenWA local iniciado.'
                }
            }
        }
    }

    Write-Host 'Preparando o banco local com backup e migrações...'
    & $Python $ManagePy preparar_banco
    if ($LASTEXITCODE -ne 0) {
        throw 'A preparação do banco falhou. O painel e o trabalhador não foram iniciados.'
    }

    $SufixoLog = [guid]::NewGuid().ToString('N')
    $WorkerOut = Join-Path $LogDir "worker-$SufixoLog.out.log"
    $WorkerErr = Join-Path $LogDir "worker-$SufixoLog.err.log"
    $WebOut = Join-Path $LogDir "web-$SufixoLog.out.log"
    $WebErr = Join-Path $LogDir "web-$SufixoLog.err.log"

    $ArgumentosWorker = '"' + $ManagePy + '" trabalhador'
    $ArgumentosServidor = '"' + $ManagePy + '" runserver 127.0.0.1:' + $Porta + ' --noreload --insecure'
    $Trabalhador = Start-Process -FilePath $Python `
        -ArgumentList $ArgumentosWorker `
        -WorkingDirectory $Projeto -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $WorkerOut -RedirectStandardError $WorkerErr
    $Filhos += $Trabalhador

    $Servidor = Start-Process -FilePath $Python `
        -ArgumentList $ArgumentosServidor `
        -WorkingDirectory $Projeto -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $WebOut -RedirectStandardError $WebErr
    $Filhos += $Servidor

    Write-Host "Painel local: http://127.0.0.1:$Porta/"
    Write-Host 'Pressione Ctrl+C para encerrar o servidor e o trabalhador iniciados por este script.'
    Write-Host "Logs locais: $LogDir"

    while (-not $Servidor.HasExited) {
        if ($Trabalhador.HasExited) {
            throw 'O trabalhador encerrou. Consulte os logs locais para ver o motivo.'
        }
        Start-Sleep -Seconds 1
        $Servidor.Refresh()
        $Trabalhador.Refresh()
    }
    throw 'O servidor Django encerrou. Consulte os logs locais para ver o motivo.'
}
finally {
    foreach ($Filho in $Filhos) {
        try {
            $Filho.Refresh()
            if (-not $Filho.HasExited) {
                Stop-Process -Id $Filho.Id -Force -ErrorAction SilentlyContinue
                $Filho.WaitForExit(5000) | Out-Null
            }
        } catch {
            # Cleanup is limited to child process IDs started above.
        }
    }
}
