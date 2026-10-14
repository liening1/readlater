#!/usr/bin/env bash
# ReadLater 启动脚本：默认监听 0.0.0.0:8765（局域网内手机可直接访问）
# 可选环境变量：
#   PORT=8765                 监听端口
#   HOST=0.0.0.0              监听地址（改为 127.0.0.1 则仅本机可访问）
#   READLATER_PROXY=http://127.0.0.1:7897   外网抓取走代理（国内网络建议设置）
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8765}"
HOST="${HOST:-0.0.0.0}"

if [ -x .venv/bin/python ]; then
  PY="./.venv/bin/python"
else
  echo "首次运行，自动安装依赖…"
  ./install.sh
  PY="./.venv/bin/python"
fi

echo "ReadLater → http://localhost:${PORT}  （局域网：http://<本机IP>:${PORT}）"
exec "$PY" -m uvicorn server:app --host "$HOST" --port "$PORT"
