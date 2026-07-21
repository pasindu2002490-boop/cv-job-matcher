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
$env:LLM_BATCH_SIZE = "15"

$securePassword = Read-Host "Paste the Gmail App Password" -AsSecureString
$passwordPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)
try {
    $env:SMTP_PASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($passwordPointer)
}
finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($passwordPointer)
    Remove-Variable securePassword, passwordPointer -ErrorAction SilentlyContinue
}

$secureGroqKey = Read-Host "Paste the Groq API key" -AsSecureString
$groqKeyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureGroqKey)
try {
    $env:GROQ_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($groqKeyPointer)
}
finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($groqKeyPointer)
    Remove-Variable secureGroqKey, groqKeyPointer -ErrorAction SilentlyContinue
}

Write-Host "Starting CV Job Matcher at http://127.0.0.1:$Port" -ForegroundColor Cyan
Write-Host "Strict experience filtering and Groq review are enabled." -ForegroundColor Green
Write-Host "Keep this window open. Press Ctrl+C to stop." -ForegroundColor Yellow
& $python -m cv_job_matcher.web
