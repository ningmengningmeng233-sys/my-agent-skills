param(
  [ValidateSet('start','status','stop','restart')]
  [string]$Action = 'start',
  [int]$Port = 0,
  [string]$PythonExe = ''
)
$ErrorActionPreference = 'Stop'
$skillPath = $PSScriptRoot
if (-not $skillPath) { throw 'start_web.ps1 must be invoked as a file (PSScriptRoot is empty)' }
Set-Location -LiteralPath $skillPath
$python = if ($PythonExe) { $PythonExe } `
          elseif ($env:KNOWLEDGE_INDEX_PYTHON) { $env:KNOWLEDGE_INDEX_PYTHON } `
          else { 'python' }
$entry = Join-Path $skillPath 'scripts/launch_web.py'
$argv = @($entry, $Action)
if ($Port -gt 0) { $argv += @('--port', "$Port") }
& $python @argv
exit $LASTEXITCODE
