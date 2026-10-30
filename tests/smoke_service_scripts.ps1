# Smoke tests for the PowerShell service script orchestration.
# Native Python and pip calls are mocked; no project environment is modified.
$ErrorActionPreference = 'Stop'

$TestsDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $TestsDir
. (Join-Path $ProjectDir 'scripts\mcp_service.ps1')

$script:failures = 0
$script:calls = New-Object System.Collections.Generic.List[string]

function Assert-Equal {
    param($Actual, $Expected, [string]$Message)
    if ($Actual -ne $Expected) {
        Write-Host "FAIL: $Message (expected [$Expected], got [$Actual])" -ForegroundColor Red
        $script:failures++
    }
}

function Reset-Mocks {
    $script:calls = New-Object System.Collections.Generic.List[string]
    $script:depsReady = $false
}

# 1. A healthy project venv is used without running pip.
Reset-Mocks
function Get-PreferredVenvPython { return 'C:\fake\.venv\Scripts\python.exe' }
function New-ProjectVenv { $script:calls.Add('create'); return 'C:\fake\created' }
function Test-PythonDependencies { param($Python) return $true }
function Update-Pip { param($Python) $script:calls.Add('pip'); return $true }
function Install-PythonRequirements { param($Python, $Requirements) $script:calls.Add('install'); return $true }
function Select-Python { $script:calls.Add('select'); return 'C:\fake\system' }

$result = Ensure-Python
Assert-Equal $result 'C:\fake\.venv\Scripts\python.exe' 'healthy venv is selected'
Assert-Equal ($script:calls -join ',') '' 'healthy venv skips pip, install and fallback'

# 2. Missing venv is created, pip is upgraded, then requirements are installed.
Reset-Mocks
function Get-PreferredVenvPython { return $null }
function New-ProjectVenv { $script:calls.Add('create'); return 'C:\fake\.venv\Scripts\python.exe' }
function Test-PythonDependencies { param($Python) return $script:depsReady }
function Update-Pip { param($Python) $script:calls.Add('pip'); return $true }
function Install-PythonRequirements { param($Python, $Requirements) $script:calls.Add('install'); $script:depsReady = $true; return $true }
function Select-Python { $script:calls.Add('select'); return 'C:\fake\system' }

$result = Ensure-Python
Assert-Equal $result 'C:\fake\.venv\Scripts\python.exe' 'created venv is selected after install'
Assert-Equal ($script:calls -join ',') 'create,pip,install' 'venv creation, pip upgrade and install run in order'

# 3. OSS_MCP_AUTO_INSTALL=0 must not create a venv and falls back to PATH python.
Reset-Mocks
$env:OSS_MCP_AUTO_INSTALL = '0'
function Get-PreferredVenvPython { return $null }
function New-ProjectVenv { $script:calls.Add('create'); return 'C:\fake\created' }
function Select-Python { $script:calls.Add('select'); return 'C:\fake\system' }

$result = Ensure-Python
Remove-Item Env:OSS_MCP_AUTO_INSTALL
Assert-Equal $result 'C:\fake\system' 'auto install disabled falls back to PATH python'
Assert-Equal ($script:calls -join ',') 'select' 'auto install disabled skips venv creation'

# 4. A broken existing venv is not repaired when auto install is disabled.
Reset-Mocks
$env:OSS_MCP_AUTO_INSTALL = '0'
function Get-PreferredVenvPython { return 'C:\fake\.venv\Scripts\python.exe' }
function Test-PythonDependencies { param($Python) return $false }
function Update-Pip { param($Python) $script:calls.Add('pip'); return $true }
function Install-PythonRequirements { param($Python, $Requirements) $script:calls.Add('install'); return $true }
function Select-Python { $script:calls.Add('select'); return 'C:\fake\system' }

$result = Ensure-Python
Remove-Item Env:OSS_MCP_AUTO_INSTALL
Assert-Equal $result 'C:\fake\system' 'broken venv falls back when auto install is disabled'
Assert-Equal ($script:calls -join ',') 'select' 'broken venv is not modified when auto install is disabled'

# 5. PYTHON_BIN is strict: validate only, never create or install.
Reset-Mocks
$env:PYTHON_BIN = 'C:\fake\explicit.exe'
function New-ProjectVenv { $script:calls.Add('create'); return 'C:\fake\created' }
function Update-Pip { param($Python) $script:calls.Add('pip'); return $true }
function Install-PythonRequirements { param($Python, $Requirements) $script:calls.Add('install'); return $true }
function Select-Python { $script:calls.Add('select'); return 'C:\fake\explicit.exe' }

$result = Ensure-Python
Remove-Item Env:PYTHON_BIN
Assert-Equal $result 'C:\fake\explicit.exe' 'PYTHON_BIN python is used as provided'
Assert-Equal ($script:calls -join ',') 'select' 'PYTHON_BIN never creates or modifies environments'

if ($script:failures -gt 0) {
    Write-Host "$($script:failures) PowerShell service script test(s) failed." -ForegroundColor Red
    exit 1
}

Write-Host 'POWERSHELL SERVICE SCRIPT TESTS PASSED'
exit 0
