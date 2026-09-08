param(
    [string]$TargetHost = "192.168.2.113",
    [string]$RemotePath = "/docker/homeassistant/custom_components"
)

Write-Host "Comprobando conexion con $TargetHost..." -ForegroundColor Cyan
if (-not (Test-Connection -ComputerName $TargetHost -Count 1 -Quiet)) {
    Write-Warning "El host $TargetHost no responde a ping. Asegurate de que el portatil/servidor este encendido."
    exit 1
}

Write-Host "Copiando custom_components/aigosmart a $TargetHost:$RemotePath..." -ForegroundColor Cyan
scp -r -o BatchMode=yes -o StrictHostKeyChecking=no custom_components/aigosmart "root@${TargetHost}:${RemotePath}/"

if ($LASTEXITCODE -ne 0) {
    Write-Error "Error durante la copia por SCP."
    exit 1
}

Write-Host "Reiniciando contenedor de Home Assistant..." -ForegroundColor Cyan
ssh -o BatchMode=yes -o StrictHostKeyChecking=no "root@${TargetHost}" "docker restart homeassistant"

if ($LASTEXITCODE -eq 0) {
    Write-Host "Home Assistant reiniciado con exito." -ForegroundColor Green
} else {
    Write-Warning "No se pudo ejecutar docker restart via SSH."
}
