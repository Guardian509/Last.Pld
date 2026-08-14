@echo off
rem Builds Last.Pld with the in-box C# compiler. No .NET SDK, no Windows Kits.
setlocal
set FW=C:\Windows\Microsoft.NET\Framework64\v4.0.30319
set WM=C:\Windows\System32\WinMetadata
cd /d "%~dp0"

if not exist "%FW%\csc.exe" (
  echo ERROR: csc.exe not found at %FW%
  exit /b 1
)

echo Building Last.Pld.exe ...
"%FW%\csc.exe" /nologo /target:winexe /out:Last.Pld.exe^
 /reference:"%WM%\Windows.Media.winmd"^
 /reference:"%WM%\Windows.Foundation.winmd"^
 /reference:"%WM%\Windows.Storage.winmd"^
 /reference:"%FW%\System.Runtime.dll"^
 /reference:"%FW%\System.Runtime.InteropServices.WindowsRuntime.dll"^
 /reference:System.Windows.Forms.dll^
 /reference:System.Drawing.dll^
 /reference:System.Security.dll^
 Last.Pld.cs Playlists.cs
if errorlevel 1 (echo BUILD FAILED & exit /b 1)

echo Building loopcap.exe ...
"%FW%\csc.exe" /nologo /target:exe /out:loopcap.exe LoopCap.cs
if errorlevel 1 (echo BUILD FAILED & exit /b 1)

echo.
echo OK.  Last.Pld.exe and loopcap.exe are up to date.
endlocal
