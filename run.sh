#!/bin/zsh
# ReadLater 一键启动：本机 http://localhost:8765 ，局域网设备访问 http://<Mac IP>:8765
cd "$(dirname "$0")"
exec /Users/shiyusheng/.workbuddy/binaries/python/envs/default/bin/python -m uvicorn server:app --host 0.0.0.0 --port 8765
