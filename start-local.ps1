param([int]$Port = 8766)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
& '.\.venv\Scripts\python.exe' -m travel_agent serve --provider deepseek --port $Port
exit $LASTEXITCODE
