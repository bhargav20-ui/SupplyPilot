param(
    [Parameter(Mandatory = $true)]
    [string]$RepositoryUrl
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "Git is not installed or is not on PATH. Install Git for Windows, reopen PowerShell, and retry."
}

if (-not (Test-Path ".git")) {
    git init -b main
    if ($LASTEXITCODE -ne 0) { throw "git init failed." }
} else {
    git branch -M main
    if ($LASTEXITCODE -ne 0) { throw "Could not switch the local repository to main." }
}

git add -A
if ($LASTEXITCODE -ne 0) { throw "git add failed." }

git diff --cached --quiet
if ($LASTEXITCODE -eq 1) {
    git commit -m "Initial commit: SupplyPilot multi-agent supply chain intelligence"
    if ($LASTEXITCODE -ne 0) { throw "git commit failed. Configure your Git name/email, then retry." }
} elseif ($LASTEXITCODE -gt 1) {
    throw "Could not inspect the Git staging area."
}

$existingRemote = git remote get-url origin 2>$null
if ($LASTEXITCODE -eq 0 -and $existingRemote) {
    git remote set-url origin $RepositoryUrl
} else {
    git remote add origin $RepositoryUrl
}
if ($LASTEXITCODE -ne 0) { throw "Could not configure the origin remote." }

git push -u origin main
if ($LASTEXITCODE -ne 0) {
    throw "Push failed. Confirm that the GitHub repository exists, is empty, and your Git credentials are configured."
}

Write-Host "SupplyPilot pushed successfully to $RepositoryUrl" -ForegroundColor Green