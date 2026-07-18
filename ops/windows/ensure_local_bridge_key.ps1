# AGENT: ROLE: Create/reuse a local loopback bridge API key without printing it to operators.
# AGENT: ENTRYPOINT: called by `_env.bat` only when bridge auth is required and no key was supplied.
# AGENT: STATE / SIDE EFFECTS: writes a random key beneath the ignored `logs/` directory.
param(
  [Parameter(Mandatory = $true)]
  [string]$KeyFile
)

$ErrorActionPreference = "Stop"
$path = [System.IO.Path]::GetFullPath($KeyFile)
$parent = Split-Path -Parent $path
if ($parent) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }

$key = ""
if (Test-Path -LiteralPath $path) {
  $key = (Get-Content -LiteralPath $path -Raw).Trim()
}
if ($key -notmatch '^[a-fA-F0-9]{64}$') {
  $bytes = [byte[]]::new(32)
  $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
  try {
    $rng.GetBytes($bytes)
  } finally {
    $rng.Dispose()
  }
  $key = ([System.BitConverter]::ToString($bytes)).Replace("-", "").ToLowerInvariant()
  [System.IO.File]::WriteAllText($path, $key + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))
}

# stdout is consumed directly into the caller's environment; callers must not echo it.
Write-Output $key
