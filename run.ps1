<#
    run.ps1 - start Lathe on Windows without hand-setting environment variables.

    Two things kept going wrong when doing this by hand:

      - $env:MT5_PASSWORD = my!password  fails, because PowerShell reads the
        bare text as a command. Quoting it works but then the password is in
        Get-History and in the console scrollback.
      - The log files are written to ..\vault\output\, a sibling of this
        folder, so looking for them in the repo finds nothing.

    This prompts for the password without echoing or recording it, and prints
    where the logs actually land before it starts.

        .\run.ps1            # run preflight.py (checks only, sends no orders)
        .\run.ps1 -Bot       # run trader.py
        .\run.ps1 -NoLogin   # skip credentials, use the account MT5 has open

    The account number and server are remembered in account.local.txt, which
    is not tracked by git. The password never is.
#>
[CmdletBinding()]
param(
    [switch] $Bot,
    [switch] $NoLogin,
    [string] $Login,
    [string] $Server
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

function Fail($message) {
    Write-Host "[run] $message" -ForegroundColor Red
    exit 1
}

if (-not (Test-Path -LiteralPath 'trader.py')) {
    Fail "trader.py is not here. Keep run.ps1 in the repository root."
}

# 1. Python, and the packages the bot needs.
try { $null = & python --version 2>&1 } catch { Fail "python is not on PATH." }
$missing = & python -c @"
import importlib.util as u
print(' '.join(n for n, m in (('PyYAML','yaml'), ('numpy','numpy'), ('MetaTrader5','MetaTrader5')) if u.find_spec(m) is None))
"@
if ($missing.Trim()) { Fail "missing packages: $($missing.Trim()). Run: pip install -r requirements.txt" }

# 2. The terminal has to be running: the MetaTrader5 package attaches to it.
if (-not (Get-Process -Name 'terminal64' -ErrorAction SilentlyContinue)) {
    Fail "MetaTrader 5 is not running. Start it and sign in, then try again."
}

# 3. Account details. Remembered between runs; the password never is.
$store = Join-Path $PSScriptRoot 'account.local.txt'
if (-not $NoLogin) {
    if (-not $Login -or -not $Server) {
        if (Test-Path -LiteralPath $store) {
            $saved = Get-Content -LiteralPath $store | Where-Object { $_ -match '=' }
            foreach ($line in $saved) {
                $name, $value = $line -split '=', 2
                switch ($name.Trim()) {
                    'login'  { if (-not $Login)  { $Login  = $value.Trim() } }
                    'server' { if (-not $Server) { $Server = $value.Trim() } }
                }
            }
        }
    }
    if (-not $Login)  { $Login  = Read-Host 'MT5 account number' }
    if (-not $Server) { $Server = Read-Host 'MT5 server (e.g. MetaQuotes-Demo)' }

    Set-Content -LiteralPath $store -Value @("login=$Login", "server=$Server")

    # -AsSecureString keeps it off the screen and out of Get-History. It still
    # has to reach the child process as a plain environment variable, which is
    # how the MetaTrader5 package expects it.
    $secure = Read-Host "Password for $Login" -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        $env:MT5_PASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
    $env:MT5_LOGIN  = $Login
    $env:MT5_SERVER = $Server
    Write-Host "[run] pinned to account $Login on $Server" -ForegroundColor Cyan
} else {
    # Without credentials the connector borrows whichever account the terminal
    # currently has open - which may be the live one, if that is what is
    # selected. PAPER mode cannot send an order either way, but position sizing
    # would read that account's balance.
    Remove-Item Env:MT5_LOGIN, Env:MT5_PASSWORD, Env:MT5_SERVER -ErrorAction SilentlyContinue
    Write-Host "[run] no credentials set: using whichever account MT5 has open." -ForegroundColor Yellow
    Write-Host "[run] check the login printed below is the one you meant." -ForegroundColor Yellow
}

# 4. Say where the logs go, since they are written outside this folder.
$logs = & python -c "import yaml,os;c=yaml.safe_load(open('config.yaml'))['logging'];print(chr(10).join(os.path.abspath(c[k]['path']) for k in ('trade_log','decision_log','rejection_log')))"
Write-Host "[run] logs:" -ForegroundColor Cyan
$logs -split "`n" | ForEach-Object { if ($_.Trim()) { Write-Host "       $($_.Trim())" } }

# 5. Go.
if ($Bot) {
    Write-Host "[run] starting trader.py - Ctrl+C to stop" -ForegroundColor Green
    & python trader.py
} else {
    Write-Host "[run] running preflight.py - checks only, no orders are sent" -ForegroundColor Green
    & python preflight.py
}
