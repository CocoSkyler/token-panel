@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set PYTHONIOENCODING=utf-8

where python >nul 2>nul
if %errorlevel%==0 (
  python server\glm_panel_server.py %*
  goto end
)
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 server\glm_panel_server.py %*
  goto end
)

echo [错误] 未找到 Python。请先安装 Python 3.10+ 并勾选 "Add to PATH"。
pause

:end
pause
