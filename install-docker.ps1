# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Docker-first installer for EMS SolarFlow API control (Windows PowerShell).
#
# Requires Docker Desktop with Linux containers and working `docker compose`.
#
# Run from an empty folder, no repository checkout required:
#   powershell -ExecutionPolicy Bypass -File .\install-docker.ps1
#   powershell -ExecutionPolicy Bypass -File .\install-docker.ps1 -Analytics

[CmdletBinding()]
param(
    [switch]$Analytics,
    [string]$Tag = "latest",
    [switch]$NoStart,
    [switch]$DryRun,
    [switch]$Force,
    [switch]$Help
)

$ErrorActionPreference = "Stop"
$Image = "ghcr.io/basecubedev/ems-solarflow-api-control"

function Write-Info($msg) { Write-Host $msg }

function Show-Usage {
    @"
EMS SolarFlow Docker-first installer (Windows PowerShell)

Usage:
  powershell -ExecutionPolicy Bypass -File .\install-docker.ps1 [options]

Options:
  -Analytics      Enable Analytics (bundled InfluxDB) alongside EMS.
  -Tag <tag>      Container image tag to use (default: latest).
  -NoStart        Set everything up but do not start the stack.
  -DryRun         Show what would happen without writing or starting anything.
  -Force          Overwrite an existing docker-compose.yml / config.json.
  -Help           Show this help.

Examples:
  powershell -ExecutionPolicy Bypass -File .\install-docker.ps1
  powershell -ExecutionPolicy Bypass -File .\install-docker.ps1 -Analytics
  powershell -ExecutionPolicy Bypass -File .\install-docker.ps1 -Tag v0.6.0
  powershell -ExecutionPolicy Bypass -File .\install-docker.ps1 -DryRun

After install:
  docker compose ps
  docker compose logs -f
  docker compose exec ems python3 emsctl.py diagnose
  docker compose exec ems python3 emsctl.py influx status   # with -Analytics
"@ | Write-Host
}

if ($Help) { Show-Usage; exit 0 }

function Invoke-Step {
    param([scriptblock]$Action, [string]$Describe)
    if ($DryRun) { Write-Info "DRY-RUN: $Describe"; return }
    & $Action
}

# The single docker-compose.yml uses `env_file: required: false`, added in
# Docker Compose v2.24.0.
$MinComposeVersion = [version]"2.24.0"

# Report a missing/unsupported prerequisite. Fatal for a real install; in
# -DryRun it is only a warning, since no Docker command is executed.
function Add-PrereqProblem($message) {
    if ($DryRun) {
        Write-Warning $message
        Write-Info "Dry-run continues because no Docker command will be executed."
        return
    }
    Write-Error $message
}

function Test-Docker {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        Add-PrereqProblem "Docker is not installed. Install Docker Desktop with Linux containers."
        return
    }
    docker compose version *> $null
    if ($LASTEXITCODE -ne 0) {
        Add-PrereqProblem "'docker compose' (Compose v2) is not available."
        return
    }

    $short = (docker compose version --short 2>$null)
    $parsed = $null
    if ($short -and [version]::TryParse(($short -replace '^v', '' -replace '[^0-9.].*$', ''), [ref]$parsed)) {
        if ($parsed -lt $MinComposeVersion) {
            Add-PrereqProblem "Docker Compose v$MinComposeVersion or newer is required because this setup uses optional env_file.required:false (found v$short)."
            return
        }
    }

    # Daemon reachability only matters for a real install.
    if (-not $DryRun) {
        docker info *> $null
        if ($LASTEXITCODE -ne 0) { Write-Error "Cannot talk to Docker. Is Docker Desktop running?" }
    }
}

$ComposeYaml = @'
# EMS SolarFlow API control — Docker-first setup.
#
# EMS only:
#   docker compose up -d
#
# EMS + Analytics (bundled InfluxDB):
#   docker compose --profile with-analytics up -d
services:
  ems:
    image: ghcr.io/basecubedev/ems-solarflow-api-control:latest
    container_name: ems-solarflow-api-control
    restart: unless-stopped
    environment:
      PUID: "${PUID:-}"
      PGID: "${PGID:-}"
      TZ: "${TZ:-UTC}"
      EMS_IN_CONTAINER: "1"
    env_file:
      - path: ./config/influxdb.env
        required: false
    ports:
      - "8080:8080"
    volumes:
      - ./config:/app/config
      - ./data:/app/data

  influxdb:
    image: influxdb:2.7
    container_name: ems-influxdb
    profiles:
      - with-analytics
    ports:
      - "8086:8086"
    env_file:
      - ./config/influxdb.env
    volumes:
      - ./data/influxdb:/var/lib/influxdb2
    restart: unless-stopped
'@

function Write-Compose {
    if ((Test-Path docker-compose.yml) -and (-not $Force)) {
        Write-Info "Keeping existing docker-compose.yml (use -Force to overwrite)."
        return
    }
    if ($DryRun) { Write-Info "DRY-RUN: write docker-compose.yml (image tag: $Tag)"; return }
    $content = $ComposeYaml
    if ($Tag -ne "latest") {
        $content = $content.Replace("${Image}:latest", "${Image}:${Tag}")
    }
    Set-Content -Path docker-compose.yml -Value $content -Encoding utf8
    Write-Info "Wrote docker-compose.yml (image tag: $Tag)."
}

function Get-EnvFileZone {
    $value = ""
    if (Test-Path .env) {
        foreach ($line in Get-Content -Encoding UTF8 .env) {
            if ($line -cmatch '^\s*(export\s+)?TZ\s*[=:]\s*(.*)$') {
                $value = ($Matches[2] -replace ' #.*$', '').Trim().Trim('"').Trim("'")
            }
        }
    }
    return $value
}

function Resolve-TimeZone {
    $candidates = @($env:TZ, (Get-EnvFileZone))
    try {
        $local = [System.TimeZoneInfo]::Local.Id
        $convert = [System.TimeZoneInfo].GetMethod(
            "TryConvertWindowsIdToIanaId", [type[]]@([string], [string].MakeByRefType()))
        $iana = $null
        if ($convert -and [System.TimeZoneInfo]::TryConvertWindowsIdToIanaId($local, [ref]$iana)) {
            $candidates += $iana
        }
        $candidates += $local
    } catch { }
    foreach ($candidate in $candidates) {
        if ($candidate -and $candidate -cmatch '^(UTC|[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+)+)$') {
            return $candidate
        }
    }
    return ""
}

function Write-Env {
    # Windows / Docker Desktop does not use PUID/PGID, so .env holds the time
    # zone, and the Analytics profile once Enable-AnalyticsProfile adds it.
    $zone = Resolve-TimeZone
    if ($env:TZ -and $env:TZ -cne $zone) {
        Write-Warning "TZ=$($env:TZ) does not name a time zone; it is not used."
    }
    $kept = Get-EnvFileZone
    if ($kept -and $kept -cne $zone) {
        if ($env:TZ -and $env:TZ -ceq $zone) {
            Write-Warning "TZ=$($env:TZ) from this shell replaces TZ=$kept in .env."
        } else {
            Write-Warning "TZ=$kept in .env is not a zone name this installer writes; it is replaced."
        }
    }
    if (-not $zone) {
        Write-Warning "This Windows time zone could not be translated; the containers use UTC until TZ is set in .env, for example TZ=Europe/Berlin."
    }
    $script:Zone = $zone
    if ($DryRun) { Write-Info "DRY-RUN: write .env (TZ=$(if ($zone) { $zone } else { 'unset' }))"; return }
    $existing = @()
    if (Test-Path .env) { $existing = @(Get-Content -Encoding UTF8 .env) }
    $kept = @($existing | Where-Object { $_ -cnotmatch '^\s*(export\s+)?TZ\s*[=:]' })
    $lines = $kept
    if ($zone) { $lines = @("TZ=$zone") + $kept }
    if (($lines -join "`n") -cne ($existing -join "`n")) {
        [System.IO.File]::WriteAllLines((Join-Path $PWD.ProviderPath ".env"), [string[]]$lines)
    }
    if (-not (Select-String -CaseSensitive -Path docker-compose.yml -Pattern '^\s*(-\s*)?["'']?TZ["'']?(\s*[:=]|\s*$|\s+#.*$)' -Quiet)) {
        Write-Warning "The kept docker-compose.yml passes no TZ to the containers, so they run on UTC; see docs/docker.md#time-zone."
    } elseif ($zone) {
        Write-Info "docker-compose.yml passes TZ; the containers that take it run in the time zone $zone (TZ in .env)."
    }
}

function Enable-AnalyticsProfile {
    # Default plain `docker compose up -d` to the Analytics profile. Called only
    # after config/influxdb.env exists so the bundled InfluxDB service (required
    # env_file) is not pulled into scope before its secret file is generated.
    if ($DryRun) { Write-Info "DRY-RUN: set COMPOSE_PROFILES=with-analytics in .env"; return }
    $lines = @()
    if (Test-Path .env) {
        $lines = @(Get-Content -Encoding UTF8 .env | Where-Object { $_ -cnotmatch '^\s*COMPOSE_PROFILES\s*=' })
    }
    $lines += "COMPOSE_PROFILES=with-analytics"
    [System.IO.File]::WriteAllLines((Join-Path $PWD.ProviderPath ".env"), [string[]]$lines)
}

function Invoke-Compose {
    param([Parameter(ValueFromRemainingArguments = $true)]$ComposeArgs)
    if ($DryRun) { Write-Info "DRY-RUN: docker compose $($ComposeArgs -join ' ')"; return }
    $previous = $env:TZ
    if ($script:Zone) { $env:TZ = $script:Zone } else { Remove-Item Env:TZ -ErrorAction SilentlyContinue }
    try {
        & docker compose @ComposeArgs
        if ($LASTEXITCODE -ne 0) { Write-Error "docker compose $($ComposeArgs -join ' ') failed." }
    } finally {
        if ($null -eq $previous) { Remove-Item Env:TZ -ErrorAction SilentlyContinue } else { $env:TZ = $previous }
    }
}

Test-Docker

Invoke-Step { New-Item -ItemType Directory -Force -Path config, data | Out-Null } "mkdir config data"
if ($Analytics) {
    Invoke-Step { New-Item -ItemType Directory -Force -Path data/influxdb | Out-Null } "mkdir data/influxdb"
}

Write-Compose
Write-Env

if ($Analytics) {
    if ((Test-Path config/config.json) -and (-not $Force)) {
        Write-Info "Keeping existing config/config.json (use -Force to re-run config init)."
    } else {
        Invoke-Compose run --rm ems python3 emsctl.py config init --analytics --yes --no-backup
    }
    Invoke-Compose run --rm ems python3 emsctl.py influx init --no-start
    Enable-AnalyticsProfile
}

if ($NoStart -or $DryRun) {
    Write-Info ""
    Write-Info "Setup prepared. Start it with:"
    if ($Analytics) {
        Write-Info "  docker compose --profile with-analytics up -d"
    } else {
        Write-Info "  docker compose up -d"
    }
    exit 0
}

Invoke-Compose up -d

if ($Analytics) {
    & docker compose exec -T ems python3 emsctl.py influx sync
    & docker compose exec -T ems python3 emsctl.py influx status
}

Write-Info ""
Write-Info "EMS is starting."
Write-Info "  dashboard:  http://localhost:8080"
if ($Analytics) {
    Write-Info "  analytics:  http://localhost:8086 (bundled InfluxDB)"
}
Write-Info ""
Write-Info "Useful commands:"
Write-Info "  docker compose ps"
Write-Info "  docker compose logs -f"
Write-Info "  docker compose exec ems python3 emsctl.py diagnose"
if ($Analytics) {
    Write-Info "  docker compose exec ems python3 emsctl.py influx status"
}
Write-Info ""
Write-Info "Next: choose your grid meter and set your devices in config/config.json,"
Write-Info "or run the guided setup assistant:"
Write-Info "  docker compose exec ems python3 emsctl.py config init"
Write-Info 'For a Zendure SmartMeter D0, select "Zendure Grid Meter via local HTTP" (recommended).'
Write-Info "Then restart EMS with: docker compose restart"
