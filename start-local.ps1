param([int]$Port = 8766, [ValidateSet('deepseek', 'baseline')][string]$Provider = 'deepseek')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
& '.\.venv\Scripts\python.exe' -m travel_agent serve --provider $Provider --port $Port
exit $LASTEXITCODE
