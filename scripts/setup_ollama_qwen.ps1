# Setup Ollama and Qwen model for LedgerLens if missing
$ollamaExe = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"
if (-not (Test-Path $ollamaExe)) {
    Write-Host "Installing Ollama via winget..."
    winget install -e --id Ollama.Ollama --accept-source-agreements --accept-package-agreements --silent
}

# Ensure Ollama daemon is running
$ollamaHost = "http://127.0.0.1:11434"
try {
    $resp = Invoke-RestMethod -Uri "$ollamaHost/api/tags" -Method Get -TimeoutSec 2
} catch {
    Write-Host "Starting Ollama serve..."
    Start-Process -FilePath $ollamaExe -ArgumentList "serve" -WindowStyle Hidden
    Start-Sleep -Seconds 3
}

# Ensure qwen2:0.5b model is downloaded
try {
    $resp = Invoke-RestMethod -Uri "$ollamaHost/api/tags" -Method Get -TimeoutSec 2
    $hasModel = $resp.models | Where-Object { $_.name -like "*qwen2:0.5b*" }
    if (-not $hasModel) {
        Write-Host "Pulling qwen2:0.5b model..."
        & $ollamaExe pull qwen2:0.5b
    }
} catch {
    Write-Host "Could not query Ollama tags"
}
