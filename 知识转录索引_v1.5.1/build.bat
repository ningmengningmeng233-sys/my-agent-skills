@echo off
rem Transcript index - Windows native entry (delegates to Git Bash build.sh)
rem Usage: build.bat [--force|--dry-run]
setlocal
set "BASH="

where bash >nul 2>nul
if not errorlevel 1 (
  set "BASH=bash"
) else (
  rem Probe common Git for Windows install locations
  for %%G in (
    "%ProgramFiles%\Git\bin\bash.exe"
    "%ProgramFiles(x86)%\Git\bin\bash.exe"
    "%LocalAppData%\Programs\Git\bin\bash.exe"
  ) do (
    if exist "%%~G" set "BASH=%%~G"
  )
)
if not defined BASH (
  echo [ERROR] Git Bash not found. Install Git for Windows, or run: bash build.sh %*
  exit /b 1
)

"%BASH%" "%~dp0build.sh" %*
exit /b %errorlevel%
