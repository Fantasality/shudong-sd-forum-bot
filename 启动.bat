@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

echo.
echo   ============================================
echo      树洞控制台 · 启动中
echo   ============================================
echo.

rem ---- 1. 找 Python ----
set PY=
python --version >nul 2>&1 && set PY=python
if not defined PY (
  py -3 --version >nul 2>&1 && set PY=py -3
)
if not defined PY (
  echo   [X] 没找到 Python。
  echo       去 https://www.python.org/downloads/ 装 3.10 以上，
  echo       安装第一步记得勾 "Add Python to PATH"，然后重新双击本文件。
  echo.
  pause
  exit /b 1
)
for /f "tokens=*" %%v in ('%PY% --version 2^>^&1') do echo   [1/4] %%v

rem ---- 2. 依赖 ----
%PY% -c "import numpy" >nul 2>&1
if errorlevel 1 (
  echo   [2/4] 装依赖 numpy ...
  %PY% -m pip install -q -r bot\requirements.txt
) else (
  echo   [2/4] 依赖已就绪
)

rem ---- 3. 配置 ----
if not exist bot\config.json (
  copy bot\config.example.json bot\config.json >nul
  echo   [3/4] 已生成 bot\config.json（第一次跑，先去网页里填两把 Key）
) else (
  echo   [3/4] 配置已存在
)

rem ---- 4. 起服务 ----
echo   [4/4] 启动本地控制台 http://127.0.0.1:8765
start "shudong-web" /min %PY% bot\run.py web --port 8765

rem 等端口起来再开浏览器，避免打开是空白页
%PY% -c "import socket,time;t=time.time();
while time.time()-t<25:
    s=socket.socket();
    try:
        s.connect(('127.0.0.1',8765));print('ok');break
    except Exception:
        time.sleep(0.4)
    finally:
        s.close()" >nul 2>&1

start "" http://127.0.0.1:8765

echo.
echo   控制台已打开：http://127.0.0.1:8765
echo   第一次用：先填 DeepSeek 和 SiliconFlow 两把 Key，再回「账号矩阵」生成号。
echo   关掉这个黑窗口不会停服务；要停就在网页顶栏点「停止」。
echo.
echo   这个窗口可以最小化，别关（关了控制台就没了）。
pause
