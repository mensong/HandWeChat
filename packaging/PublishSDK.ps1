#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$Version = "",
    [string]$OutputDir = "",
    [switch]$Upload,
    [string]$Repository = "pypi",
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$packageName = "handwechat_gateway_sdk"
$distributionName = "handwechat-gateway-sdk"
$sdkRoot = Join-Path $root "build\sdk"
$packageDir = Join-Path $sdkRoot $packageName

$venvPython = Join-Path $root ".venv\Scripts\python.exe"
if ([string]::IsNullOrWhiteSpace($Python)) {
    if (Test-Path -LiteralPath $venvPython) {
        $Python = $venvPython
    }
    else {
        $Python = "python"
    }
}

if ([string]::IsNullOrWhiteSpace($OutputDir)) {
    $OutputDir = Join-Path $root "dist\sdk"
}

if ([string]::IsNullOrWhiteSpace($Version)) {
    $initPath = Join-Path $root "handwechat\__init__.py"
    $match = [regex]::Match(
        [IO.File]::ReadAllText($initPath),
        '__version__\s*=\s*"([^"]+)"'
    )
    if (-not $match.Success) {
        throw "Unable to read __version__ from handwechat/__init__.py"
    }
    $Version = $match.Groups[1].Value
}

Write-Host "Building $distributionName $Version"

$utf8 = New-Object System.Text.UTF8Encoding($false)

if (Test-Path -LiteralPath $sdkRoot) {
    Remove-Item -LiteralPath $sdkRoot -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $packageDir | Out-Null
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

$sourceDir = Join-Path $root "handwechat\gateway"
Copy-Item -LiteralPath (Join-Path $sourceDir "client.py") -Destination $packageDir
Copy-Item -LiteralPath (Join-Path $sourceDir "protocol.py") -Destination $packageDir

$clientPath = Join-Path $packageDir "client.py"
$clientText = [IO.File]::ReadAllText($clientPath, $utf8)
$oldImport = "from handwechat.gateway.protocol import Decision"
$newImport = "from .protocol import Decision"
if (-not $clientText.Contains($oldImport)) {
    throw "Decision import not found in client.py; update PublishSDK.ps1."
}
[IO.File]::WriteAllText(
    $clientPath,
    $clientText.Replace($oldImport, $newImport),
    $utf8
)

$initLines = @(
    "# -*- coding: utf-8 -*-",
    '"""HandWeChat message gateway consumer SDK."""',
    "",
    "from $packageName.client import GatewayClient, Offer, PinInfo, GatewayContext, SendResult, ChatInfo",
    "from $packageName.protocol import Decision",
    "",
    ('__version__ = "' + $Version + '"'),
    "",
    '__all__ = ["GatewayClient", "Offer", "PinInfo", "GatewayContext", "SendResult", "ChatInfo", "Decision"]',
    ""
)
[IO.File]::WriteAllText(
    (Join-Path $packageDir "__init__.py"),
    ($initLines -join "`r`n"),
    $utf8
)

$pyproject = @"
[build-system]
requires = ["setuptools>=61", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "$distributionName"
version = "$Version"
description = "HandWeChat message gateway consumer client"
readme = "README.md"
requires-python = ">=3.10"
dependencies = ["websockets>=12.0"]

[tool.setuptools]
packages = ["$packageName"]
"@
[IO.File]::WriteAllText(
    (Join-Path $sdkRoot "pyproject.toml"),
    $pyproject + "`r`n",
    $utf8
)

$readme = @"
# $distributionName

Consumer SDK for the HandWeChat message gateway.

## Install

    pip install $distributionName

## Usage

    import asyncio

    from $packageName import GatewayClient, Offer, Decision


    def on_offer(offer: Offer) -> Decision:
        if offer.content == "hello":
            return Decision.consume("hi")
        return Decision.skip()


    async def main() -> None:
        client = GatewayClient(app_id="my-app", token="***", on_offer=on_offer)
        await client.run()


    asyncio.run(main())

See the gateway application guide in the HandWeChat repository for the full protocol.
"@
[IO.File]::WriteAllText(
    (Join-Path $sdkRoot "README.md"),
    $readme + "`r`n",
    $utf8
)

& $Python -m pip wheel $sdkRoot --no-deps --wheel-dir $OutputDir
if ($LASTEXITCODE -ne 0) {
    throw "Failed to build the SDK wheel."
}

$wheels = @(Get-ChildItem -LiteralPath $OutputDir -Filter "$packageName-*.whl")
if ($wheels.Count -eq 0) {
    throw "No wheel produced in $OutputDir."
}

if ($Upload) {
    & $Python -c "import twine" 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "twine is not installed. Run: $Python -m pip install twine"
    }
    $wheel = $wheels | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    & $Python -m twine upload --repository $Repository $wheel.FullName
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to upload $($wheel.Name)."
    }
}

Write-Host "SDK artifacts:"
$wheels | ForEach-Object { Write-Host "  $($_.FullName)" }
