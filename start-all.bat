@echo off
REM ============================================
REM  Stash OS Gateway Launcher
REM  Starts hermes services + cloudflared tunnels
REM ============================================

setlocal enabledelayedexpansion

REM Kill any existing processes
echo [1/6] Cleaning up old processes...
for /f "tokens=5" %%p in ('netstat -ano 2^>/dev/null ^| findstr "9119 8642" ^| findstr "LISTENING"') do (
    taskkill /PID %%p /F >nul 2>&1
)
taskkill /IM cloudflared.exe /F >nul 2>&1
timeout /t 3 >nul

REM Start gateway (API server on 8642)
echo [2/6] Starting gateway on port 8642...
start "Hermes Gateway" /min cmd /c "cd /d G:\LLMAPI\hermes-agent && set HERMES_CONFIG=%USERPROFILE%\.hermes\config.yaml&& set API_SERVER_ENABLED=true&& set API_SERVER_KEY=ea98a07e53a4a62b3e8d72aaabc1da6e4f3bfd2ba29daea78c04f1d2f6af0510&& D:\anaconda\envs\hermesenv\python.exe -m hermes_cli.main gateway run --force --no-supervise"

REM Start dashboard server (port 9119)
echo [3/6] Starting dashboard on port 9119...
start "Hermes Dashboard" /min cmd /c "cd /d G:\LLMAPI\hermes-agent && set HERMES_CONFIG=%USERPROFILE%\.hermes\config.yaml&& set HERMES_POSTGRES_URL=postgresql://neondb_owner:npg_VaUKlZ5I1CrF@ep-super-snow-b1yufqim-pooler.c-5.eu-central-1.aws.neon.tech/neondb?sslmode=require&& set API_SERVER_ENABLED=true&& set API_SERVER_KEY=ea98a07e53a4a62b3e8d72aaabc1da6e4f3bfd2ba29daea78c04f1d2f6af0510&& D:\anaconda\envs\hermesenv\python.exe -m hermes_cli.main serve"

REM Wait for ports
echo [4/6] Waiting for services to start...
timeout /t 12 >nul

REM Start cloudflared tunnels
echo [5/6] Starting tunnels...
start "Tunnel API" /min G:\LLMAPI\cloudflared.exe tunnel --url http://localhost:8642
start "Tunnel Dashboard" /min G:\LLMAPI\cloudflared.exe tunnel --url http://localhost:9119
timeout /t 10 >nul

REM Get tunnel URLs from logs
echo [6/6] Reading tunnel URLs...
echo.
echo ============================================
echo   STASH OS - Gateway Status
echo ============================================
echo.

echo API Server (port 8642):
netstat -ano 2>/dev/null | findstr "8642" | findstr "LISTENING" >nul && echo   Status: RUNNING || echo   Status: NOT RUNNING
echo.

echo Dashboard (port 9119):
netstat -ano 2>/dev/null | findstr "9119" | findstr "LISTENING" >nul && echo   Status: RUNNING || echo   Status: NOT RUNNING
echo.

echo Dashboard Auth: admin / stash-os-dashboard-2024
echo.

echo Cloudflared Tunnels:
echo   Check cf-8642-err.log and cf-9119-err.log for URLs
echo ============================================

pause
