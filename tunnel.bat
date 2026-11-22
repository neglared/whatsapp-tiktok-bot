@echo off
title Tunnel Internet
cd /d "%~dp0"
echo Ouverture du tunnel public...
echo.
echo Cherche la ligne "https://....trycloudflare.com" ci-dessous.
echo C'est l'adresse a mettre dans Meta (suivie de /webhook).
echo.
cloudflared.exe tunnel --url http://localhost:8000
pause
