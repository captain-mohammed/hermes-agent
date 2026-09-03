@echo off
start "Hermes Gateway" /min cmd /c "cd /d G:\LLMAPI\hermes-agent && set HERMES_CONFIG=%USERPROFILE%\.hermes\config.yaml&& set API_SERVER_ENABLED=true&& set API_SERVER_KEY=ea98a07e53a4a62b3e8d72aaabc1da6e4f3bfd2ba29daea78c04f1d2f6af0510&& D:\anaconda\envs\hermesenv\python.exe -m hermes_cli.main gateway run --force --no-supervise > G:\LLMAPI\hermes-gw.log 2>&1"
echo GATEWAY_LAUNCHED