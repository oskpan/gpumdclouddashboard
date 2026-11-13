# Finds gpumd.exe and Python, reports running GPUMD processes, starts the gpumdq server and opens the dashboard.
$ErrorActionPreference = 'SilentlyContinue'
$here   = $PSScriptRoot
$report = Join-Path $here 'startup_report.txt'
Start-Transcript -Path $report -Force | Out-Null
Write-Host "=== gpumdq launcher  $(Get-Date) ===`n"

# ---------- 1. Python (prefer Anaconda: it ships psutil) ----------
$py = $null
foreach ($c in @("$env:USERPROFILE\anaconda3\python.exe", "$env:USERPROFILE\miniconda3\python.exe",
                 "$env:LOCALAPPDATA\anaconda3\python.exe", "C:\ProgramData\anaconda3\python.exe")) {
    if (Test-Path $c) { $py = $c; break }
}
if (-not $py) { $py = (Get-Command python.exe | Select-Object -First 1).Source }
if (-not $py) { Write-Host "ERROR: no Python found."; Stop-Transcript | Out-Null; exit 1 }
Write-Host "Python : $py"
& $py -c "import psutil" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "psutil missing -> installing (needed to detect gpumd runs started outside gpumdq)"
    & $py -m pip install psutil
}

# ---------- 2. gpumd.exe ----------
$cache = Join-Path $here 'gpumd_path.txt'
$gpumd = $null
if (Test-Path $cache) { $p = (Get-Content $cache -Raw).Trim(); if (Test-Path $p) { $gpumd = $p } }
if (-not $gpumd) {
    $found = @()
    $onPath = (Get-Command gpumd.exe | Select-Object -ExpandProperty Source)
    if ($onPath) { $found += $onPath }
    # executables of gpumd processes that are running right now
    $found += (Get-CimInstance Win32_Process -Filter "Name='gpumd.exe'" | ForEach-Object { $_.ExecutablePath })
    if (-not $found) {
        Write-Host "Searching all local drives for gpumd.exe (can take a few minutes, only done once)..."
        foreach ($d in (Get-PSDrive -PSProvider FileSystem | Where-Object { $_.Used -gt 0 })) {
            Write-Host "  scanning $($d.Root)"
            $found += (cmd /c "dir /s /b /a-d `"$($d.Root)gpumd.exe`" 2>nul") |
                      Where-Object { $_ -and $_ -notmatch '\\\$Recycle\.Bin\\' }
        }
    }
    $found = $found | Where-Object { $_ } | Sort-Object -Unique
    if ($found) {
        Write-Host "`ngpumd.exe copies found:"
        $found | ForEach-Object { Write-Host ("  {0}   ({1})" -f $_, (Get-Item $_).LastWriteTime) }
        $gpumd = $found | Sort-Object { (Get-Item $_).LastWriteTime } -Descending | Select-Object -First 1
        Set-Content -Path $cache -Value $gpumd
    }
}
if (-not $gpumd) {
    Write-Host "`nERROR: gpumd.exe not found. Put its full path in gpumd_path.txt next to this script and run again."
    Stop-Transcript | Out-Null; exit 1
}
Write-Host "Using  : $gpumd   (edit gpumd_path.txt to change)"

# ---------- 3. GPUMD runs already going ----------
Write-Host "`nRunning gpumd processes:"
$running = Get-CimInstance Win32_Process -Filter "Name='gpumd.exe'"
if ($running) {
    $running | ForEach-Object { Write-Host ("  pid {0}  started {1}  {2}" -f $_.ProcessId, $_.CreationDate, $_.CommandLine) }
} else { Write-Host "  none" }
$wsl = Get-CimInstance Win32_Process -Filter "Name='wsl.exe' OR Name='wslhost.exe'"
if ($wsl) { Write-Host "  (note: WSL is running; gpumd inside WSL is not visible from Windows)" }
Write-Host "`nGPUs:"; nvidia-smi --query-gpu=index,name,utilization.gpu,memory.used,memory.total --format=csv,noheader

# ---------- 4. start server (unless it is already up) ----------
$up = $false
try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 http://127.0.0.1:8765/api/state | Out-Null; $up = $true } catch {}
if ($up) {
    Write-Host "`ngpumdq server already running."
} else {
    $cmdval = if ($gpumd -match ' ') { "`"$gpumd`"" } else { $gpumd }
    $bat = Join-Path $here 'run_server.bat'
    @("@echo off", "title gpumdq server (close this window to stop the dashboard; jobs keep running)",
      "set `"GPUMD_CMD=$cmdval`"", "`"$py`" `"%~dp0gpumdq.py`" serve", "pause") |
        Set-Content -Path $bat -Encoding ASCII
    Start-Process -FilePath $bat -WorkingDirectory $here -WindowStyle Minimized
    Write-Host "`nStarting gpumdq server..."
    for ($i = 0; $i -lt 20 -and -not $up; $i++) {
        Start-Sleep -Milliseconds 500
        try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 http://127.0.0.1:8765/api/state | Out-Null; $up = $true } catch {}
    }
    if ($up) { Write-Host "Server is up." } else { Write-Host "ERROR: server did not respond; open the 'gpumdq server' window to see why." }
}
if ($up) {
    $st = (Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8765/api/state).Content | ConvertFrom-Json
    Write-Host "`nDetected by gpumdq: $($st.external.Count) external run(s)"
    $st.external | ForEach-Object { Write-Host ("  pid {0}  GPU {1}  {2}  progress {3}" -f $_.pid, $_.gpu, $_.workdir, $_.progress) }
    Start-Process "http://127.0.0.1:8765"
}
Stop-Transcript | Out-Null
