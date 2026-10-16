@echo off
setlocal
pushd "%~dp0"

where.exe ffmpeg.exe >nul 2>&1
if not errorlevel 1 goto :ffmpeg_found

if defined LOCALAPPDATA (
    for /d %%P in ("%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg_*") do (
        for /d %%V in ("%%~fP\ffmpeg-*\bin") do (
            if exist "%%~fV\ffmpeg.exe" (
                set "PATH=%%~fV;%PATH%"
                goto :ffmpeg_found
            )
        )
    )
)

echo FFmpeg was not found.
echo Install an FFmpeg build with the ddagrab filter and AMD AMF encoders,
echo or add its bin folder to PATH, then run this file again.
goto :failed

:ffmpeg_found
where.exe pyw.exe >nul 2>&1
if not errorlevel 1 (
    pyw.exe -3 lightrec.py
    goto :finished
)

where.exe pythonw.exe >nul 2>&1
if not errorlevel 1 (
    pythonw.exe lightrec.py
    goto :finished
)

where.exe py.exe >nul 2>&1
if not errorlevel 1 (
    py.exe -3 lightrec.py
    goto :finished
)

where.exe python.exe >nul 2>&1
if not errorlevel 1 (
    python.exe lightrec.py
    goto :finished
)

echo Python 3 was not found. Install Python 3 with Tkinter, then run this file again.

:failed
popd
pause
exit /b 1

:finished
set "APP_EXIT=%ERRORLEVEL%"
popd
exit /b %APP_EXIT%
