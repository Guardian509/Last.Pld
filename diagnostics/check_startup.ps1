# Verdict on whether Last.Pld actually launched at logon.
# Run after signing back in:  powershell -ExecutionPolicy Bypass -File check_startup.ps1

$dir = "C:\Users\Nicka\NowPlaying"

Write-Host "=== logon ===" -ForegroundColor Cyan
# explorer.exe starting is a good proxy for the interactive shell logon
$explorer = Get-Process explorer -ErrorAction SilentlyContinue | Sort-Object StartTime | Select-Object -First 1
$logon = $null
if ($explorer) {
    $logon = $explorer.StartTime
    "shell (explorer) started : $logon"
} else {
    "explorer not found - cannot establish logon time"
}
"boot time                : " + (Get-CimInstance Win32_OperatingSystem).LastBootUpTime

Write-Host "`n=== scheduled task ===" -ForegroundColor Cyan
$t = Get-ScheduledTask -TaskName "Last.Pld" -ErrorAction SilentlyContinue
if (-not $t) {
    Write-Host "TASK MISSING" -ForegroundColor Red
} else {
    "State      : $($t.State)"
    $i = Get-ScheduledTaskInfo -TaskName "Last.Pld"
    "LastRun    : $($i.LastRunTime)"
    "LastResult : $($i.LastTaskResult)   (267009 = running, which is expected)"
}

Write-Host "`n=== process ===" -ForegroundColor Cyan
$p = Get-Process "Last.Pld" -ErrorAction SilentlyContinue | Sort-Object StartTime | Select-Object -First 1
if ($p) {
    "pid        : $($p.Id)"
    "started    : $($p.StartTime)"
    "cpu(s)     : $([math]::Round($p.CPU,1))"
    $cmd = (Get-CimInstance Win32_Process -Filter "Name='Last.Pld.exe'" | Select-Object -First 1).CommandLine
    "cmdline    : $cmd"
} else {
    Write-Host "NOT RUNNING" -ForegroundColor Red
}

Write-Host "`n=== startup.log (last 5 launches) ===" -ForegroundColor Cyan
if (Test-Path "$dir\startup.log") {
    Get-Content "$dir\startup.log" -Encoding UTF8 | Select-Object -Last 5
} else {
    "no startup.log yet"
}

Write-Host "`n=== tracks logged since logon ===" -ForegroundColor Cyan
if ((Test-Path "$dir\lastpld.csv") -and $logon) {
    $rows = Import-Csv "$dir\lastpld.csv" | Where-Object {
        $ts = $_.timestamp -as [datetime]
        $ts -and $ts -ge $logon
    }
    "count: " + @($rows).Count
    $rows | Select-Object -Last 5 | Format-Table timestamp, title, artist, app -AutoSize
}

Write-Host "`n=== VERDICT ===" -ForegroundColor Yellow
if ($p -and $logon) {
    $delta = ($p.StartTime - $logon).TotalSeconds
    "Last.Pld started {0:N0}s after the shell" -f $delta
    if ($delta -lt 180 -and $delta -gt -60) {
        Write-Host "PASS - launched at logon by the scheduled task" -ForegroundColor Green
    } else {
        Write-Host "INCONCLUSIVE - process start is far from logon; it may have been started manually" -ForegroundColor Yellow
    }
} elseif (-not $p) {
    Write-Host "FAIL - Last.Pld is not running after logon" -ForegroundColor Red
}
