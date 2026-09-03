@echo off
set HERMES_HOME=C:\Users\angel\AppData\Local\hermes
set API_SERVER_ENABLED=true
set API_SERVER_KEY=ea98a07e53a4a62b3e8d72aaabc1da6e4f3bfd2ba29daea78c04f1d2f6af0510
set HERMES_POSTGRES_URL=postgresql://neondb_owner:npg_VaUKlZ5I1CrF@ep-super-snow-b1yufqim-pooler.c-5.eu-central-1.aws.neon.tech/neondb?sslmode=require
cd /d G:\LLMAPI\hermes-agent
D:\anaconda\envs\hermesenv\python.exe -m hermes_cli.main gateway run --force --no-supervise
