<#
.SYNOPSIS
  Set up this Windows machine as an AutoPPT worker (run once per machine, in the auto-logon worker account).

.DESCRIPTION
  1. Creates a venv and installs the requirements of both repositories.
  2. Runs the worker self-check; stops if the basics fail.
  3. Registers a logon-triggered scheduled task that starts the worker loop.
     Not a Windows service on purpose: services run in Session 0, where PowerPoint COM fails.

  Office (PowerPoint), the Microsoft YaHei and DengXian fonts, Node.js and the Codex CLI must already be
  installed, and every Codex profile home must be logged in with `codex login`.
#>
param(
    [Parameter(Mandatory = $true)][string]$ApiUrl,
    [Parameter(Mandatory = $true)][string]$WorkerToken,
    [Parameter(Mandatory = $true)][string]$Template,
    [Parameter(Mandatory = $true)][string]$Profiles,
    [string]$ServiceRepo = (Resolve-Path "$PSScriptRoot\..").Path,
    [string]$AutopptRepo = (Resolve-Path "$PSScriptRoot\..\..\autoppt").Path,
    [string]$JobsRoot = "D:\autoppt-jobs\worker",
    [string]$CodexBin = "",
    [string]$TaskName = "AutoPPT Worker"
)

$ErrorActionPreference = "Stop"
$venv = Join-Path $ServiceRepo ".venv"
if (-not (Test-Path $venv)) { python -m venv $venv }
$python = Join-Path $venv "Scripts\python.exe"
& $python -m pip install --upgrade pip
& $python -m pip install -r (Join-Path $ServiceRepo "requirements.txt") -r (Join-Path $AutopptRepo "requirements.txt") pywin32

$env:AUTOPPT_REPO = $AutopptRepo
if ($CodexBin) { $env:AUTOPPT_CODEX_BIN = $CodexBin }
New-Item -ItemType Directory -Force -Path $JobsRoot | Out-Null
Push-Location $ServiceRepo
try {
    & $python -m autoppt_service.worker_check --template $Template --profiles $Profiles --jobs-root $JobsRoot --json-out (Join-Path $JobsRoot "worker-check.json")
    if ($LASTEXITCODE -ne 0) { throw "worker self-check failed; see $JobsRoot\worker-check.json" }
} finally { Pop-Location }

$tokenFile = Join-Path $JobsRoot "worker-token.txt"
Set-Content -LiteralPath $tokenFile -Value $WorkerToken -Encoding ascii
icacls $tokenFile /inheritance:r /grant:r "$($env:USERNAME):(R)" | Out-Null

$command = @"
`$env:AUTOPPT_REPO='$AutopptRepo'; $(if ($CodexBin) { "`$env:AUTOPPT_CODEX_BIN='$CodexBin';" })
Set-Location '$ServiceRepo'
& '$python' -m autoppt_service.worker_client --api '$ApiUrl' --token (Get-Content '$tokenFile') --jobs-root '$JobsRoot' --template '$Template' --profiles '$Profiles'
"@
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -Command $command"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -RunLevel Limited -Force | Out-Null
Write-Output "Registered '$TaskName'. It starts at logon of $($env:USERNAME); check $JobsRoot\worker-check.json for capabilities."
