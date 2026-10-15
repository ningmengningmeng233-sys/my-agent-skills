param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $CliArgs
)

$ErrorActionPreference = "Stop"
$skillRoot = Split-Path -Parent $PSScriptRoot
$projectRoot = Join-Path $skillRoot "v4"

# Codex/uv 的输出统一为 UTF-8，避免 Windows 默认代码页造成中文乱码。
$env:PYTHONUTF8 = "1"
Push-Location $skillRoot
try {
    & uv run --project $projectRoot python -m v4.core.cli @CliArgs
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}
finally {
    Pop-Location
}
