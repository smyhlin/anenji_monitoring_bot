#!/usr/bin/env bash
# Deploy the bot to the CloudKey and restart its service.
# Override with env vars: HOST=root@192.168.1.249 KEY=~/.ssh/key DIR=/opt/anenji-monitor
set -euo pipefail

HOST=${HOST:-root@192.168.1.249}
KEY=${KEY:-$HOME/.ssh/cloudkey_uckg2p_ed25519}
DIR=${DIR:-/opt/anenji-monitor}
SOURCE=$(cd "$(dirname "$0")/.." && pwd)

rsync -az --delete \
  --exclude '.venv' --exclude '__pycache__' --exclude '*.egg-info' \
  --exclude 'state.db*' --exclude '.git' \
  -e "ssh -i $KEY" "$SOURCE/" "$HOST:$DIR/"

ssh -i "$KEY" "$HOST" "chown -R root:root '$DIR' && systemctl restart anenji-monitor"
echo "deployed to $HOST:$DIR"
