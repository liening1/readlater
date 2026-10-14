#!/usr/bin/env bash
# ReadLater 一键安装：创建独立 .venv 并安装依赖（macOS / Linux，需 Python 3.10+）
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "✗ 未找到 Python 3，请先安装：https://www.python.org/downloads/"
  exit 1
fi

"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' || {
  echo "✗ 需要 Python 3.10 或更高版本（当前：$("$PY" -V)）"; exit 1; }

echo "→ 创建虚拟环境 .venv …"
"$PY" -m venv .venv
echo "→ 安装依赖（5 个包）…"
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q -r requirements.txt

echo
echo "✅ 安装完成。启动："
echo "   ./run.sh"
echo "   然后打开 http://localhost:8765"
