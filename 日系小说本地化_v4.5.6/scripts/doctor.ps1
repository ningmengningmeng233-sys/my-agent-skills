param()

$ErrorActionPreference = "Stop"
$skillRoot = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $PSScriptRoot "novel-localization.ps1"
$provider = if ($env:NOVEL_LLM_PROVIDER) { $env:NOVEL_LLM_PROVIDER.ToLower() } else { "deepseek" }
$keyName = ($provider.ToUpper() + "_API_KEY")
$baseName = ($provider.ToUpper() + "_BASE_URL")
$modelName = ($provider.ToUpper() + "_MODEL")

Write-Output "novel-localization doctor"
Write-Output "skill_root=$skillRoot"
Write-Output "provider=$provider"
Write-Output "api_key_present=$(-not [string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($keyName)))"
Write-Output "base_url=$([Environment]::GetEnvironmentVariable($baseName))"
Write-Output "model=$([Environment]::GetEnvironmentVariable($modelName))"

if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($keyName))) {
    Write-Output "result=NOT_READY (请设置 $keyName；本脚本不会打印密钥)"
    exit 2
}

& $launcher assets
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Output "result=READY"
