param(
    [Parameter(Mandatory = $true)]
    [string]$NodeExe,
    [Parameter(Mandatory = $true)]
    [string]$Root,
    [Parameter(Mandatory = $true)]
    [string]$Port,
    [Parameter(Mandatory = $true)]
    [string]$DashboardLog,
    [Parameter(Mandatory = $true)]
    [string]$DashboardErrLog,
    [Parameter(Mandatory = $true)]
    [string]$DashboardPid,
    [string]$NextBin = "",
    [string]$StandaloneServer = "",
    [string]$PackageMode = "0"
)

$ErrorActionPreference = "Stop"

$env:PORT = $Port
$env:HOSTNAME = "127.0.0.1"

if ($PackageMode -eq "1" -and $StandaloneServer) {
    $arguments = '"{0}"' -f $StandaloneServer
} else {
    $arguments = '"{0}" start -p {1}' -f $NextBin, $Port
}

$process = Start-Process `
    -FilePath $NodeExe `
    -WorkingDirectory $Root `
    -ArgumentList $arguments `
    -RedirectStandardOutput $DashboardLog `
    -RedirectStandardError $DashboardErrLog `
    -WindowStyle Hidden `
    -PassThru

Set-Content -Path $DashboardPid -Value ([string]$process.Id)
