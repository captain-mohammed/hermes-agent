@echo off
REM ============================================================
REM  Stash OS - start gateway + dashboard + tunnels (no pause)
REM  Flow: kill old -> start tunnels -> update config with new
REM  tunnel URLs -> start gateway + dashboard
REM ============================================================
taskkill /IM cloudflared.exe /F >nul 2>&1
for /f "tokens=5" %%p in ('netstat -ano 2^>nul ^| findstr "9119 8642" ^| findstr "LISTENING"') do (
    taskkill /PID %%p /F >nul 2>&1
)
ping -n 3 127.0.0.1 >nul

echo [1/4] Starting tunnels...
start "Tunnel API" /min G:\LLMAPI\cloudflared.exe tunnel --url http://localhost:8642 --logfile G:\LLMAPI\cf-8642.log
start "Tunnel Dashboard" /min G:\LLMAPI\cloudflared.exe tunnel --url http://localhost:9119 --logfile G:\LLMAPI\cf-9119.log

ping -n 15 127.0.0.1 >nul

echo [2/4] Updating dashboard.public_url + CORS with new tunnel URLs...
D:\anaconda\envs\hermesenv\python.exe G:\LLMAPI\hermes-agent\update_tunnel_config.py

echo [3/4] Starting gateway on 8642...
start "Hermes Gateway" /min cmd /c "cd /d G:\LLMAPI\hermes-agent && set HERMES_CONFIG=%USERPROFILE%\.hermes\config.yaml&& set API_SERVER_ENABLED=true&& set API_SERVER_KEY=ea98a07e53a4a62b3e8d72aaabc1da6e4f3bfd2ba29daea78c04f1d2f6af0510&& D:\anaconda\envs\hermesenv\python.exe -m hermes_cli.main gateway run --force --no-supervise > G:\LLMAPI\hermes-gw.log 2>&1"

echo [4/4] Starting dashboard on 9119...
start "Hermes Dashboard" /min cmd /c "cd /d G:\LLMAPI\hermes-agent && set HERMES_CONFIG=%USERPROFILE%\.hermes\config.yaml&& set HERMES_POSTGRES_URL=postgresql://neondb_owner:npg_VaUKlZ5I1CrF@ep-super-snow-b1yufqim-pooler.c-5.eu-central-1.aws.neon.tech/neondb?sslmode=require&& set API_SERVER_ENABLED=true&& set API_SERVER_KEY=ea98a07e53a4a62b3e8d72aaabc1da6e4f3bfd2ba29daea78c04f1d2f6af0510&& D:\anaconda\envs\hermesenv\python.exe -m hermes_cli.main serve > G:\LLMAPI\hermes-serve.log 2>&1"

echo ALL STARTED