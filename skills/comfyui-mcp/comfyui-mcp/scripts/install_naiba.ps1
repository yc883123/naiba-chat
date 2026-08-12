param([string]$Url = "")

$ErrorActionPreference = "Stop"
$process = Get-CimInstance Win32_Process |
  Where-Object { $_.Name -eq "python.exe" -and $_.CommandLine -match "(?i)ComfyUI.+main\.py" } |
  Select-Object -First 1
if (-not $process) {
  throw "No running ComfyUI Python process was found. Start ComfyUI with its normal launcher first."
}

$python = [string]$process.ExecutablePath
$commandLine = [string]$process.CommandLine
$mainMatch = [regex]::Match($commandLine, '(?i)(?:^|\s)(?:"(?<path>[^"]*ComfyUI[\\/]main\.py)"|(?<path>\S*ComfyUI[\\/]main\.py))')
if (-not $mainMatch.Success) {
  throw "The running ComfyUI process does not expose a readable main.py path."
}
$mainPy = [System.IO.Path]::GetFullPath($mainMatch.Groups['path'].Value)
$comfyRoot = Split-Path -Parent $mainPy

if (-not $Url) {
  $portMatch = [regex]::Match($commandLine, '(?i)(?:^|\s)--port(?:=|\s+)(?<port>\d+)')
  $port = if ($portMatch.Success) { $portMatch.Groups['port'].Value } else { "8188" }
  $listenMatch = [regex]::Match($commandLine, '(?i)(?:^|\s)--listen(?:=|\s+)(?<host>[^\s]+)')
  $hostName = if ($listenMatch.Success) { $listenMatch.Groups['host'].Value.Trim('"') } else { "127.0.0.1" }
  if ($hostName -in @("0.0.0.0", "::")) { $hostName = "127.0.0.1" }
  $Url = "http://${hostName}:${port}"
}
$Url = $Url.TrimEnd('/')

try {
  $null = Invoke-RestMethod -Uri "$Url/system_stats" -TimeoutSec 5
} catch {
  throw "ComfyUI is running but its HTTP API is not reachable at ${Url}: $($_.Exception.Message)"
}

& $python -c "from mcp.server.fastmcp import FastMCP" 2>$null
$mcpReady = $LASTEXITCODE -eq 0
if (-not $mcpReady) {
  & $python -m pip install --disable-pip-version-check "mcp>=1.2.0,<2"
  if ($LASTEXITCODE -ne 0) { throw "Failed to install the MCP dependency into ${python}." }
  & $python -c "from mcp.server.fastmcp import FastMCP"
  $mcpReady = $LASTEXITCODE -eq 0
}
if (-not $mcpReady) {
  throw "The ComfyUI Python still cannot import FastMCP after dependency installation."
}

$skillRoot = Split-Path -Parent $PSScriptRoot
$registration = [ordered]@{
  id = "comfyui"
  command = $python
  args = @((Join-Path $PSScriptRoot "comfyui_mcp_server.py"))
  env = [ordered]@{
    COMFYUI_URL = $Url
    COMFYUI_ROOT = $comfyRoot
    COMFYUI_WORKFLOWS_DIR = (Join-Path $skillRoot "workflows")
  }
  enabled = $true
}

[ordered]@{
  ready = $true
  discovered_from = "running_process"
  launcher_compatible = $true
  registration = $registration
} | ConvertTo-Json -Depth 8
