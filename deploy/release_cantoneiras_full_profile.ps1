# Publica apenas Cantoneiras MTG3 e recupera os factos de Perfil Completo das
# folhas pendentes. Windows PowerShell 5.1.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidateSet('Prepare','Apply')][string]$Mode,
    [string]$Commit,
    [string]$Manifest,
    [string]$Repo = 'C:\OCR-Suite\kanban-mes',
    [string]$KitRoot = 'C:\OCR-Suite\kit'
)
$ErrorActionPreference = 'Stop'
. (Join-Path $KitRoot 'kanban_ops.ps1')

$port = 8100
$expectedPhysical = 'F:\Apps\OCR-Suite\kanban-mes'
$physical = Get-KanbanPhysicalPath $Repo
if (-not $physical -or ($physical -ine $expectedPhysical -and
        -not $physical.StartsWith($expectedPhysical + '\', [StringComparison]::OrdinalIgnoreCase))) {
    throw "A instalacao nao esta fisicamente em $expectedPhysical"
}
$data = Join-Path $Repo 'data'
$database = Join-Path $data 'app.db'
$python = Join-Path $Repo '.venv\Scripts\python.exe'
$envFile = Join-Path $Repo '.env'
$reports = Join-Path $data 'full-profile-release'
$temporary = Join-Path $data '_tmp\full-profile-release'
New-Item -ItemType Directory -Force -Path $reports,$temporary | Out-Null
if (-not $Manifest) { $Manifest = Join-Path $reports 'latest-release.json' }

function Invoke-GitChecked {
    param([string[]]$Arguments)
    $value = @(& git -C $Repo @Arguments)
    if ($LASTEXITCODE -ne 0) { throw "Git falhou: $($Arguments -join ' ')" }
    return $value
}

function Save-Json {
    param([string]$Path, [object]$Value)
    [IO.File]::WriteAllText(
        $Path, ($Value | ConvertTo-Json -Depth 12) + "`n",
        (New-Object Text.UTF8Encoding($false)))
}

function Import-AppEnvironment {
    foreach ($line in [IO.File]::ReadAllLines($envFile)) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$') {
            [Environment]::SetEnvironmentVariable(
                $Matches[1], $Matches[2].Trim().Trim('"').Trim("'"), 'Process')
        }
    }
    $env:MES_DATA_DIR = $data
    $env:PYTHONDONTWRITEBYTECODE = '1'
    $env:PYTHONPATH = ''
}

function Get-Health {
    try {
        return Invoke-RestMethod -Uri "http://127.0.0.1:$port/health" -TimeoutSec 5
    } catch { return $null }
}

function Assert-PortOwner {
    $owners = @(Get-NetTCPConnection -LocalPort $port -State Listen `
        -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique)
    foreach ($ownerPid in $owners) {
        if (-not (Test-KanbanPortOwner -OwnerPid $ownerPid `
                -ExpectedRepo $Repo -ExpectedPort $port)) {
            throw "A porta $port pertence a outro processo (PID $ownerPid)"
        }
    }
    return $owners
}

function Get-PendingOcrCount {
    $count = & $python -B -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); print(c.execute(\"select count(*) from sheets where status='pending' and image_path is not null\").fetchone()[0]); c.close()" $database
    if ($LASTEXITCODE -ne 0) { throw 'Nao foi possivel verificar OCR pendente' }
    return [int]$count
}

function Invoke-Recovery {
    param([string]$CodeRoot, [string[]]$Arguments)
    & $python -B (Join-Path $CodeRoot 'scripts\recover_full_profile_pending.py') `
        --db $database @Arguments
    if ($LASTEXITCODE -ne 0) { throw 'A recuperacao de Perfil Completo falhou' }
}

function Start-Cantoneiras {
    $start = Join-Path $KitRoot 'start_kanban.ps1'
    Start-Process powershell.exe -WindowStyle Hidden -ArgumentList (
        "-NoProfile -ExecutionPolicy Bypass -File `"$start`" -Repo `"$Repo`" -Port $port"
    ) | Out-Null
}

function New-SqliteBackup {
    param([string]$Destination)
    if (Test-Path -LiteralPath $Destination) { throw "Backup ja existe: $Destination" }
    & $python -B -c "import sqlite3,sys,pathlib; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); assert d.execute('pragma quick_check').fetchone()[0]=='ok'; d.close(); s.close()" $database $Destination
    if ($LASTEXITCODE -ne 0) { throw 'Backup SQLite consistente falhou' }
}

Import-AppEnvironment
$dirty = @(Invoke-GitChecked @('status','--porcelain'))
if ($dirty.Count -gt 0) { throw 'A instalacao tem alteracoes locais' }
if (([string](Invoke-GitChecked @('symbolic-ref','--short','HEAD'))).Trim() -ne 'main') {
    throw 'A instalacao tem de estar na branch main'
}
$before = ([string](Invoke-GitChecked @('rev-parse','HEAD'))).Trim()
$healthBefore = Get-Health
if (-not $healthBefore -or $healthBefore.app -ne 'kanban-mes' -or
        $healthBefore.platform -ne 'win32' -or $healthBefore.engine -ne 'legacy' -or
        $healthBefore.commit -ne $before) {
    throw 'O health local nao confirma Cantoneiras legacy no commit instalado'
}

if ($Mode -eq 'Prepare') {
    if ($Commit -notmatch '^[0-9a-fA-F]{40}$') {
        throw 'Prepare exige o SHA Git completo da release testada'
    }
    $null = Invoke-GitChecked @('fetch','origin','main')
    $null = Invoke-GitChecked @('merge-base','--is-ancestor',$before,$Commit)
    $null = Invoke-GitChecked @('merge-base','--is-ancestor',$Commit,'origin/main')
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    $candidate = Join-Path $temporary ("candidate-$stamp-" + $Commit.Substring(0,12))
    $null = Invoke-GitChecked @('worktree','add','--detach',$candidate,$Commit)
    $report = Join-Path $reports "pending-$stamp.json"
    Invoke-Recovery $candidate @('--report',$report)
    $fingerprint = & $python -B -c "import os,sys; os.chdir(sys.argv[1]); sys.path.insert(0,sys.argv[1]); from app.health import code_fingerprint; print(code_fingerprint())" $candidate
    if ($LASTEXITCODE -ne 0) { throw 'Nao foi possivel calcular o fingerprint candidato' }
    $manifestValue = [ordered]@{
        format_version = 1
        app = 'kanban-mes'
        previous_commit = $before
        release_commit = $Commit
        engine = 'legacy'
        candidate = $candidate
        code_fingerprint = ([string]$fingerprint).Trim()
        database = $database
        env_sha256 = (Get-FileHash -LiteralPath $envFile -Algorithm SHA256).Hash
        report = $report
        report_sha256 = (Get-FileHash -LiteralPath $report -Algorithm SHA256).Hash
        created_at = (Get-Date).ToUniversalTime().ToString('o')
    }
    Save-Json $Manifest $manifestValue
    Write-Host "PREPARE OK: $Manifest"
    Write-Host "Rever a simulacao: $report"
    exit 0
}

$release = Get-Content -LiteralPath $Manifest -Raw -Encoding UTF8 | ConvertFrom-Json
if ($release.format_version -ne 1 -or $release.app -ne 'kanban-mes' -or
        $release.previous_commit -ne $before -or $release.database -ine $database -or
        $release.engine -ne 'legacy') {
    throw 'O manifesto nao corresponde ao estado atual da instalacao'
}
$Commit = [string]$release.release_commit
if ((Get-FileHash -LiteralPath $release.report -Algorithm SHA256).Hash -ne
        $release.report_sha256) { throw 'O relatorio de simulacao foi alterado' }
if ((Get-FileHash -LiteralPath $envFile -Algorithm SHA256).Hash -ne
        $release.env_sha256) { throw 'A configuracao mudou desde Prepare' }
Invoke-Recovery $release.candidate @('--check','--from-report',$release.report)
if ((Get-PendingOcrCount) -gt 0) {
    throw 'Existe OCR em execucao ou por concluir; nenhuma aplicacao foi parada'
}
$owners = @(Assert-PortOwner)
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
$backupRoot = 'C:\OCR-Suite\saida\backups\kanban-mes'
New-Item -ItemType Directory -Force -Path $backupRoot | Out-Null
$releaseBackup = Join-Path $backupRoot "before-full-profile-release-$stamp.db"
New-SqliteBackup $releaseBackup
$stopped = $false
$updated = $false
try {
    foreach ($ownerPid in $owners) { Stop-Process -Id $ownerPid -Force }
    $stopped = $true
    for ($i=0; $i -lt 40; $i++) {
        if (@(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue).Count -eq 0) { break }
        Start-Sleep -Milliseconds 250
    }
    if (@(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue).Count -gt 0) {
        throw 'A porta 8100 nao foi libertada'
    }
    if ((Get-PendingOcrCount) -gt 0) { throw 'Entrou novo OCR; publicacao cancelada' }
    $null = Invoke-GitChecked @('fetch','origin','main')
    $null = Invoke-GitChecked @('merge','--ff-only',$Commit)
    $updated = $true
    if ((Get-FileHash -LiteralPath $envFile -Algorithm SHA256).Hash -ne $release.env_sha256) {
        throw 'A publicacao alterou o .env'
    }
    Start-Cantoneiras
    $stopped = $false
    $health = $null
    for ($i=0; $i -lt 30; $i++) {
        $health = Get-Health
        if ($health -and $health.commit -eq $Commit -and
                $health.code_fingerprint -eq $release.code_fingerprint -and
                $health.engine -eq 'legacy' -and $health.platform -eq 'win32') { break }
        Start-Sleep -Seconds 2
    }
    if (-not $health -or $health.commit -ne $Commit -or
            $health.code_fingerprint -ne $release.code_fingerprint -or
            $health.engine -ne 'legacy') { throw 'A nova aplicacao falhou a verificacao de health' }

    $recoveryBackup = Join-Path $backupRoot "before-full-profile-recovery-$stamp.db"
    Invoke-Recovery $Repo @('--apply','--from-report',$release.report,'--backup',$recoveryBackup)

    foreach ($route in @('/','/?status=pending','/sheet/024f54607858','/health')) {
        $response = Invoke-WebRequest -Uri ("http://127.0.0.1:$port" + $route) `
            -UseBasicParsing -TimeoutSec 30
        if ([int]$response.StatusCode -ne 200) { throw "Falha HTTP em $route" }
    }
    & $python -B -c "import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); from app import db; c=db.connect(Path(sys.argv[2])); n=[x['sheet_no'] for x in db.list_sheets(c)]; assert n==sorted(n); c.close()" $Repo $database
    if ($LASTEXITCODE -ne 0) { throw 'O historico local nao ficou ordenado' }
    $public = Invoke-RestMethod -Uri ("https://cantoneiras.nikufra.ai/health?release=" + $Commit + '&t=' + $stamp) -TimeoutSec 20
    if ($public.commit -ne $Commit -or $public.code_fingerprint -ne $release.code_fingerprint -or
            $public.engine -ne 'legacy' -or $public.platform -ne 'win32') {
        throw 'O acesso publico nao confirmou esta release Windows'
    }
    Write-Host "APPLY OK: commit $Commit; backup $releaseBackup"
} catch {
    if ($updated) {
        $null = Invoke-GitChecked @('reset','--hard',$before)
        $updated = $false
    }
    if ($stopped -or -not (Get-Health)) { Start-Cantoneiras }
    throw
}
