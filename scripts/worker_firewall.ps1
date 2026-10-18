<#
.SYNOPSIS
  Restrict a worker machine's outbound traffic to the hosts the worker needs (P7 egress allow-list).

.DESCRIPTION
  Uploaded documents can carry prompt injection; the agent sandbox already blocks network access for
  agent commands, and this is the second fence: the whole machine may only reach the AutoPPT API /
  object storage, the model endpoints and Windows' own update and licensing services.

  Windows Firewall rules take IP addresses, not host names, so every -AllowHost is resolved now and the
  addresses are written into one allow rule. CDN-backed hosts (chatgpt.com, api.openai.com) change
  addresses: run the script again from a scheduled task (-Apply -Quiet every hour) to refresh the rule.

  Without -Apply the script only prints the plan as JSON (default; safe to run anywhere).
  -Apply needs an elevated PowerShell and sets the default outbound action of all profiles to Block.
  -Remove deletes the rules and restores Allow.

.EXAMPLE
  .\worker_firewall.ps1 -AllowHost api.autoppt.example,storage.example.com
  .\worker_firewall.ps1 -AllowHost api.autoppt.example -Apply
#>
param(
    [string[]]$AllowHost = @(),
    # Codex CLI with a ChatGPT login; replace with the model gateway when D2 moves to API keys.
    [string[]]$ModelHost = @("chatgpt.com", "api.openai.com", "auth.openai.com"),
    # Office activation, Windows Update and certificate revocation; without them Office stops working.
    [string[]]$SystemHost = @("officecdn.microsoft.com", "login.microsoftonline.com", "activation.sls.microsoft.com",
                              "ctldl.windowsupdate.com", "crl.microsoft.com", "ocsp.digicert.com"),
    [string]$Group = "AutoPPT Worker egress",
    [switch]$Apply,
    [switch]$Remove,
    [switch]$Quiet
)

$ErrorActionPreference = "Stop"

function Resolve-Addresses([string[]]$Names) {
    $resolved = [ordered]@{}
    foreach ($name in $Names) {
        if ([System.Net.IPAddress]::TryParse($name, [ref]$null)) { $resolved[$name] = @($name); continue }
        try {
            $resolved[$name] = @([System.Net.Dns]::GetHostAddresses($name) | ForEach-Object { $_.IPAddressToString } | Sort-Object -Unique)
        } catch {
            $resolved[$name] = @()
        }
    }
    return $resolved
}

if ($Remove) {
    Get-NetFirewallRule -Group $Group -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    Set-NetFirewallProfile -Profile Domain, Private, Public -DefaultOutboundAction Allow
    if (-not $Quiet) { Write-Output "removed group '$Group'; default outbound action is Allow again" }
    exit 0
}

$hosts = @($AllowHost + $ModelHost + $SystemHost | Where-Object { $_ } | Select-Object -Unique)
if ($AllowHost.Count -eq 0) { throw "give the AutoPPT API / storage host with -AllowHost" }
$resolved = Resolve-Addresses $hosts
$unresolved = @($resolved.Keys | Where-Object { $resolved[$_].Count -eq 0 })
$addresses = @($resolved.Values | ForEach-Object { $_ } | Sort-Object -Unique)
$plan = [ordered]@{
    group = $Group
    default_outbound = "Block"
    rules = @(
        [ordered]@{ name = "$Group - DNS"; protocol = "UDP"; remote_port = "53"; remote_address = "Any" },
        [ordered]@{ name = "$Group - loopback"; protocol = "Any"; remote_port = "Any"; remote_address = "127.0.0.1,::1" },
        [ordered]@{ name = "$Group - allowed hosts"; protocol = "TCP"; remote_port = "443,80"; remote_address = ($addresses -join ",") }
    )
    hosts = $resolved
    unresolved = $unresolved
}
if (-not $Apply) {
    $plan | ConvertTo-Json -Depth 5
    exit 0
}
if ($unresolved.Count -gt 0) { throw "cannot resolve $($unresolved -join ', '); not applying a rule that would cut them off" }
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { throw "-Apply needs an elevated PowerShell" }

Get-NetFirewallRule -Group $Group -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName "$Group - DNS" -Group $Group -Direction Outbound -Action Allow -Protocol UDP -RemotePort 53 | Out-Null
New-NetFirewallRule -DisplayName "$Group - loopback" -Group $Group -Direction Outbound -Action Allow -RemoteAddress 127.0.0.1, ::1 | Out-Null
New-NetFirewallRule -DisplayName "$Group - allowed hosts" -Group $Group -Direction Outbound -Action Allow -Protocol TCP `
    -RemotePort 443, 80 -RemoteAddress $addresses | Out-Null
Set-NetFirewallProfile -Profile Domain, Private, Public -DefaultOutboundAction Block
if (-not $Quiet) { Write-Output "applied: $($addresses.Count) addresses for $($hosts.Count) hosts; default outbound Block" }
