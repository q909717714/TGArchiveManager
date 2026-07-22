param(
    [string]$Python = "",
    [switch]$InstallDeps
)

$ErrorActionPreference = "Stop"

$Root = Resolve-Path $PSScriptRoot
Set-Location $Root

function Resolve-PythonCommand {
    if ($Python.Trim()) {
        return $Python.Trim()
    }

    $VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
    if (Test-Path $VenvPython) {
        return (Resolve-Path $VenvPython).Path
    }

    return "python"
}

function Invoke-Step {
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

$PythonCommand = Resolve-PythonCommand
Write-Host "== TGArchiveManager package =="
Write-Host "Root: $Root"
Write-Host "Python: $PythonCommand"

if ($InstallDeps) {
    Invoke-Step "Install packaging dependencies" {
        & $PythonCommand -m pip install -r requirements-dev.txt
    }
}

Invoke-Step "Build Windows package" {
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_windows.ps1 -Python $PythonCommand
}

Write-Host "Package ready: $(Join-Path $Root 'dist\TGArchiveManager')"
