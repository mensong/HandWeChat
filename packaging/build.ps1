#Requires -Version 5.1
[CmdletBinding()]
param(
    [ValidateSet("All", "Bundle")]
    [string]$Target = "All",
    [string]$Python = "",
    [switch]$SkipDependencyInstall
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $root ".venv\Scripts\python.exe"

Push-Location $root
try {
    if ([string]::IsNullOrWhiteSpace($Python)) {
        if (-not (Test-Path -LiteralPath $venvPython)) {
            & python setup.py install_deps
            if ($LASTEXITCODE -ne 0) {
                throw "Failed to initialize the .venv virtual environment."
            }
        }
        $Python = $venvPython
    }

    if (-not $SkipDependencyInstall) {
        & $Python -m pip install --disable-pip-version-check -r (
            Join-Path $PSScriptRoot "requirements-build.txt"
        )
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to install build dependencies."
        }
    }

    if ($Target -in @("All", "Bundle")) {
        & $Python -m PyInstaller @(
            "--clean"
            "--noconfirm"
            (Join-Path $PSScriptRoot "HandWeChat.spec")
        )
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to build HandWeChat bundle."
        }
    }

    if ($Target -in @("All", "Bundle")) {
        $bundleDir = Join-Path $root (Join-Path "dist" "HandWeChat")
        $dataDir = Join-Path $bundleDir "data"
        $logDir = Join-Path $dataDir "logs"
        New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
        New-Item -ItemType Directory -Force -Path $logDir | Out-Null

        foreach ($fileName in @("api_config.json", "gui_config.json", "ocr_calibration.json")) {
            $source = Join-Path $root (Join-Path "data" $fileName)
            if (Test-Path -LiteralPath $source) {
                Copy-Item -LiteralPath $source -Destination (
                    Join-Path $dataDir $fileName
                ) -Force
            }
        }
    }
}
finally {
    Pop-Location
}
