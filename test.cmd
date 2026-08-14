@echo off
rem Builds and runs the Last.Pld test suite.
rem
rem Covers the settings/DPAPI round trip, the OAuth loopback listener, Spotify
rem response parsing, and reachability of the real Spotify endpoints. It stops
rem at the auth boundary: anything past it needs a client ID and a signed-in
rem account, so those paths are exercised by hand.
setlocal
set FW=C:\Windows\Microsoft.NET\Framework64\v4.0.30319
set WM=C:\Windows\System32\WinMetadata
cd /d "%~dp0"

if not exist "_build" mkdir "_build"

echo Building tests ...
"%FW%\csc.exe" /nologo /target:exe /main:LastPld.TestHarness /out:_build\TestPlaylists.exe^
 /reference:"%WM%\Windows.Media.winmd"^
 /reference:"%WM%\Windows.Foundation.winmd"^
 /reference:"%WM%\Windows.Storage.winmd"^
 /reference:"%FW%\System.Runtime.dll"^
 /reference:"%FW%\System.Runtime.InteropServices.WindowsRuntime.dll"^
 /reference:System.Windows.Forms.dll^
 /reference:System.Drawing.dll^
 /reference:System.Security.dll^
 Last.Pld.cs Playlists.cs tests\TestPlaylists.cs
if errorlevel 1 (echo BUILD FAILED & exit /b 1)

echo.
_build\TestPlaylists.exe
set RC=%ERRORLEVEL%

rem The harness writes its settings next to its own exe; don't leave it behind.
if exist _build\playlists.txt del _build\playlists.txt

exit /b %RC%
