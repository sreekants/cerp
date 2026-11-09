<#
.SYNOPSIS
  Install cerp as a Windows service under a dedicated low-privilege account (MPC001-120).

.DESCRIPTION
  Uses NSSM (https://nssm.cc) to supervise "cerp serve". Run from an elevated PowerShell.
  The service runs as a virtual service account (NT SERVICE\cerp): no password, no interactive logon,
  and file access only where granted below. Configuration reloads when cerp.yaml or the exposure file is
  saved (server.reload_poll_seconds), since Windows has no SIGHUP.

.EXAMPLE
  .\install-service.ps1 -InstallDir C:\cerp -ConfigDir C:\ProgramData\cerp -DataDir D:\webos\db -ReadOnlyData
#>
param(
    [Parameter(Mandatory)] [string] $InstallDir,      # holds .venv\Scripts\cerp.exe
    [Parameter(Mandatory)] [string] $ConfigDir,       # cerp.yaml, exposure.yaml, registry.yaml
    [Parameter(Mandatory)] [string] $DataDir,         # webos databases
    [string] $LogDir = "$env:ProgramData\cerp\logs",
    [string] $Nssm = "nssm.exe",
    [switch] $ReadOnlyData                            # grant read only (no write tools exposed)
)
$ErrorActionPreference = "Stop"
$name = "cerp"
$account = "NT SERVICE\$name"
$exe = Join-Path $InstallDir ".venv\Scripts\cerp.exe"
if (-not (Test-Path $exe)) { throw "cerp.exe not found at $exe" }
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

& $Nssm install $name $exe serve --config (Join-Path $ConfigDir "cerp.yaml")
& $Nssm set $name DisplayName "cerp (Claude ERP) MCP server"
& $Nssm set $name ObjectName $account
& $Nssm set $name Start SERVICE_AUTO_START
& $Nssm set $name AppStdout (Join-Path $LogDir "service.out.log")
& $Nssm set $name AppStderr (Join-Path $LogDir "service.err.log")
& $Nssm set $name AppStopMethodConsole 35000     # cerp drains in-flight calls for up to 30 s
& $Nssm set $name AppExit Default Restart

# Least privilege: the service account gets only these grants.
icacls $InstallDir /grant "${account}:(OI)(CI)RX" | Out-Null
icacls $ConfigDir /grant "${account}:(OI)(CI)R" | Out-Null
icacls $LogDir /grant "${account}:(OI)(CI)M" | Out-Null
if ($ReadOnlyData) {
    icacls $DataDir /grant "${account}:(OI)(CI)R" | Out-Null
} else {
    icacls $DataDir /grant "${account}:(OI)(CI)M" | Out-Null   # SQLite needs modify for -wal/-shm files
}
Start-Service $name
Get-Service $name
