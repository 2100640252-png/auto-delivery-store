#!/bin/bash
# 自助下单 + 自动发货商城启动脚本
# 用法：bash run.sh   （监听 0.0.0.0:5000）
set -e
cd "$(dirname "$0")"
if [ -d ".venv" ]; then
  .venv/bin/python app.py
elif command -v python3 >/dev/null 2>&1; then
  python3 -m venv .venv 2>/dev/null || true
  .venv/bin/pip install -q -r requirements.txt
  .venv/bin/python app.py
else
  echo "需要 python3，请先安装 Python 3.10+"
  exit 1
fi
