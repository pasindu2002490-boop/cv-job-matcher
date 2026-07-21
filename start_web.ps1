param(
    [string]$Email = "pasindu2002490@gmail.com",
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Virtual environment not found. Run: python -m venv .venv"
}

$env:SMTP_HOST = "smtp.gmail.com"
$env:SMTP_PORT = "587"
$env:SMTP_USERNAME = $Email
$env:SMTP_FROM = $Email
$env:SMTP_USE_TLS = "1"
$env:SMTP_USE_SSL = "0"
$env:WEB_PORT = $Port.ToString()
$env:GROQ_MODEL = "openai/gpt-oss-20b"
$env:LLM_LIMIT = "500"
$env:LLM_BATCH_SIZE = "10"

$secretsFile = Join-Path $PSScriptRoot ".env"
if (Test-Path -LiteralPath $secretsFile) {
    foreach ($line in Get-Content -LiteralPath $secretsFile) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) { continue }
        $parts = $trimmed.Split("=", 2)
        if ($parts.Count -eq 2) {
            [Environment]::SetEnvironmentVariable($parts[0].Trim(), $parts[1].Trim(), "Process")
        }
    }
    Write-Host "Loaded local configuration from .env" -ForegroundColor Green
}

function Read-SecretEnvironmentValue([string]$Name, [string]$Prompt) {
    if ([Environment]::GetEnvironmentVariable($Name, "Process")) { return }
    $secureValue = Read-Host $Prompt -AsSecureString
    $valuePointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureValue)
    try {
        [Environment]::SetEnvironmentVariable(
            $Name,
            [Runtime.InteropServices.Marshal]::PtrToStringBSTR($valuePointer),
            "Process"
        )
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($valuePointer)
    }
}

Read-SecretEnvironmentValue "SMTP_PASSWORD" "Paste the Gmail App Password"
Read-SecretEnvironmentValue "GROQ_API_KEY" "Paste the Groq API key"

Write-Host "Starting CV Job Matcher at http://127.0.0.1:$Port" -ForegroundColor Cyan
Write-Host "Strict experience filtering and Groq review are enabled." -ForegroundColor Green
Write-Host "Keep this window open. Press Ctrl+C to stop." -ForegroundColor Yellow
& $python -m cv_job_matcher.web
