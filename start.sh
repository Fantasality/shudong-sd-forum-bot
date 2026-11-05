#!/usr/bin/env bash
# macOS / Linux 启动脚本。Windows 用「启动.bat」。
set -e
cd "$(dirname "$0")"

PY=python3
command -v "$PY" >/dev/null 2>&1 || { echo "没找到 python3，先装 Python 3.10+"; exit 1; }

echo "[1/4] $($PY --version)"
if ! $PY -c "import numpy" >/dev/null 2>&1; then
  echo "[2/4] 安装依赖…"
  $PY -m pip install -q -r bot/requirements.txt
else
  echo "[2/4] 依赖已就绪"
fi

if [ ! -f bot/config.json ]; then
  cp bot/config.example.json bot/config.json
  echo "[3/4] 已生成 bot/config.json"
else
  echo "[3/4] 配置已存在"
fi

echo "[4/4] 启动控制台 http://127.0.0.1:8765"
$PY bot/run.py web --port 8765 &
SRV=$!
sleep 2
if command -v open >/dev/null 2>&1; then
  open http://127.0.0.1:8765
elif command -v xdg-open >/dev/null 2>&1; then
  xdg-open http://127.0.0.1:8765
else
  echo "浏览器打开 http://127.0.0.1:8765"
fi

trap "kill $SRV 2>/dev/null || true" EXIT
wait $SRV
