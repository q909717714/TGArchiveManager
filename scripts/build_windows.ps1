param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root

function Invoke-ExternalStep {
    param(
        [string]$Name,
        [scriptblock]$Command
    )

    Write-Host "== $Name =="
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Name failed with exit code $LASTEXITCODE"
    }
}

function Invoke-ExeStep {
    param(
        [string]$Name,
        [string]$FilePath,
        [string]$Arguments
    )

    Write-Host "== $Name =="
    $Process = Start-Process `
        -FilePath $FilePath `
        -ArgumentList $Arguments `
        -Wait `
        -PassThru `
        -WindowStyle Hidden
    if ($Process.ExitCode -ne 0) {
        throw "$Name failed with exit code $($Process.ExitCode)"
    }
}

Write-Host "== TGArchiveManager build =="
Write-Host "Root: $Root"

Invoke-ExternalStep "Compile check" { & $Python -m compileall . -q }
Invoke-ExternalStep "Unit tests" { & $Python -m unittest discover -s tests }
Invoke-ExternalStep "Source preflight" { & $Python scripts\preflight_check.py --root . }

$PreviousQtQpaPlatform = $env:QT_QPA_PLATFORM
$env:QT_QPA_PLATFORM = "offscreen"
try {
    Invoke-ExternalStep "Source GUI check" { & $Python main.py --check-gui }
}
finally {
    if ($null -eq $PreviousQtQpaPlatform) {
        Remove-Item Env:\QT_QPA_PLATFORM -ErrorAction SilentlyContinue
    }
    else {
        $env:QT_QPA_PLATFORM = $PreviousQtQpaPlatform
    }
}

Invoke-ExternalStep "PyInstaller" { & $Python -m PyInstaller --noconfirm --clean TGArchiveManager.spec }

$DistRoot = Join-Path $Root "dist\TGArchiveManager"
$ExePath = Join-Path $DistRoot "TGArchiveManager.exe"
if (!(Test-Path $ExePath)) {
    throw "Build completed but executable was not found: $ExePath"
}

Write-Host "== Runtime directory setup =="
$RuntimeDirs = @(
    "config",
    "sessions",
    "logs",
    "logs\tasks",
    "downloads",
    "exports",
    "data"
)
foreach ($RelativePath in $RuntimeDirs) {
    New-Item -ItemType Directory -Force -Path (Join-Path $DistRoot $RelativePath) | Out-Null
}

Copy-Item -Force `
    -Path (Join-Path $Root "config\config.yaml.example") `
    -Destination (Join-Path $DistRoot "config\config.yaml.example")

Invoke-ExternalStep "Dist preflight" { & $Python scripts\preflight_check.py --root $DistRoot }
Invoke-ExeStep "Built executable check" $ExePath "--check"

$PreviousQtQpaPlatform = $env:QT_QPA_PLATFORM
$env:QT_QPA_PLATFORM = "offscreen"
try {
    Invoke-ExeStep "Built GUI check" $ExePath "--check-gui"
}
finally {
    if ($null -eq $PreviousQtQpaPlatform) {
        Remove-Item Env:\QT_QPA_PLATFORM -ErrorAction SilentlyContinue
    }
    else {
        $env:QT_QPA_PLATFORM = $PreviousQtQpaPlatform
    }
}

Write-Host "Build output: $DistRoot"
