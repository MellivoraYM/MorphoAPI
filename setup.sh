#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"

sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip build-essential curl

if [ ! -d "$VENV_DIR" ]; then
  python3 -m venv "$VENV_DIR"
fi

source "$VENV_DIR/bin/activate"

pip install --upgrade pip
pip install -r "$PROJECT_DIR/requirements.txt"

cat <<'EOT'

安装完成。

启动服务（生产）：
  source .venv/bin/activate
  gunicorn -k uvicorn.workers.UvicornWorker -w 2 -b 0.0.0.0:8000 app.main:app

可选环境变量：
  MONGO_ENABLED=true
  MONGO_URI=mongodb://localhost:27017
  MONGO_DB=morpho

如需后台运行，请使用 systemd 或 tmux。
EOT
