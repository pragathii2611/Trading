#!/usr/bin/env bash
# One-time setup on a fresh Ubuntu VPS (run as root):
#   curl -fsSL https://raw.githubusercontent.com/pragathii2611/Trading/main/deploy/setup-vps.sh | bash
# Re-run any time to update to the latest code.
set -euo pipefail
REPO=${REPO:-https://github.com/pragathii2611/Trading.git}
DIR=/opt/ai-trader

# Small servers (1 GB, e.g. AWS free tier) need swap to build the image.
if [ "$(swapon --show | wc -l)" -eq 0 ] && [ "$(awk '/MemTotal/ {print $2}' /proc/meminfo)" -lt 2000000 ]; then
  echo "Adding 2 GB swap…"
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
if ! command -v docker >/dev/null; then
  echo "Installing Docker…"
  curl -fsSL https://get.docker.com | sh
fi
if [ -d "$DIR/.git" ]; then git -C "$DIR" pull --ff-only; else git clone "$REPO" "$DIR"; fi
cd "$DIR"

IP=$(curl -fsS https://api.ipify.org)
DOMAIN=${DOMAIN:-$(echo "$IP" | tr . -).sslip.io}   # free hostname that points at this server

if [ ! -f .env ]; then
  cp .env.example .env
  read -rsp "Choose an app password (avoid the $ character): " PW </dev/tty; echo
  read -rp  "Kite API key (blank to add later): " KEY </dev/tty
  read -rsp "Kite API secret (blank to add later): " SECRET </dev/tty; echo
  esc() { printf '%s' "$1" | sed -e 's/[\\/&|]/\\&/g'; }
  sed -i "s|^APP_PASSWORD=.*|APP_PASSWORD=$(esc "$PW")|; s|^KITE_API_KEY=.*|KITE_API_KEY=$(esc "$KEY")|; s|^KITE_API_SECRET=.*|KITE_API_SECRET=$(esc "$SECRET")|" .env
  if [ -n "$KEY" ]; then sed -i "s|^DATA_SOURCE=.*|DATA_SOURCE=kite|" .env; else sed -i "s|^DATA_SOURCE=.*|DATA_SOURCE=yahoo|" .env; fi
  chmod 600 .env
fi

if command -v ufw >/dev/null; then ufw allow 22/tcp >/dev/null; ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null; fi
DOMAIN=$DOMAIN docker compose -f deploy/docker-compose.yml up -d --build

cat <<MSG

✅ AI Trader is running.

  App URL (open on your iPhone):   https://$DOMAIN
  Static IP to whitelist on Kite:  $IP
  Kite redirect URL:               https://$DOMAIN/kite/callback

Next: developers.kite.trade → your app → set the redirect URL and add the IP to the IP whitelist.
Settings live in $DIR/.env — after editing run:  DOMAIN=$DOMAIN docker compose -f deploy/docker-compose.yml up -d
MSG
