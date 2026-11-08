# Genera .env a partir de .env.example reemplazando cada <generado> por un secreto aleatorio (Windows).
# Uso: powershell -ExecutionPolicy Bypass -File .\scripts\init-env.ps1 [-Forzar]
param([switch]$Forzar)
$ErrorActionPreference = "Stop"
$raiz = Split-Path -Parent $PSScriptRoot
Set-Location $raiz
if ((Test-Path .env) -and -not $Forzar) {
  Write-Host ".env ya existe; no se modifica. Use -Forzar para regenerarlo (luego: docker compose down -v)."
  exit 0
}
function Aleatorio([int]$bytes) {
  $b = New-Object byte[] $bytes
  [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b)
  -join ($b | ForEach-Object { $_.ToString("x2") })
}
$salida = foreach ($linea in Get-Content .env.example) {
  if ($linea -eq "JWT_SECRET=<generado>") { "JWT_SECRET=$(Aleatorio 32)" }
  elseif ($linea -match '^(ADMIN_PASSWORD|DEMO_CLIENTE_PASSWORD)=<generado>$') { "$($Matches[1])=Cm-$(Aleatorio 6)" }
  elseif ($linea -match '^([A-Z_]+_PASSWORD)=<generado>$') { "$($Matches[1])=$(Aleatorio 16)" }
  else { $linea }
}
# UTF-8 sin BOM y finales LF para que Docker Compose lo lea igual que en Linux
[System.IO.File]::WriteAllText((Join-Path $raiz ".env"), (($salida -join "`n") + "`n"), (New-Object System.Text.UTF8Encoding($false)))
Write-Host "Se creo .env con secretos aleatorios. Cuentas de la demo:"
Get-Content .env | Select-String -Pattern '^(ADMIN_EMAIL|ADMIN_PASSWORD|DEMO_CLIENTE_PASSWORD)='
