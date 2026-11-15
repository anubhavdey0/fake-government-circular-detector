#!/usr/bin/env bash
# One-time setup of a fresh Ubuntu server for circverify: Oracle Cloud Always Free (Ampere, ARM) or
# any other VPS. Installs Docker, adds swap, opens ports 80 and 443, fetches the code and builds it.
#
#   DOMAIN=yourname.duckdns.org bash setup.sh
#
# Then load the index (restore-data.sh) or build it on the server, and start everything with
#   cd /opt/circverify && sudo docker compose --profile https up -d
set -euo pipefail

DOMAIN="${DOMAIN:?set DOMAIN to the address that points at this server, e.g. DOMAIN=yourname.duckdns.org}"
REPO="${REPO:-https://github.com/anubhavdey0/fake-government-circular-detector.git}"
DIR="${DIR:-/opt/circverify}"

echo "== Docker"
if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
  # Docker's own installer: the current engine and compose plugin, for x86 and ARM alike.
  curl -fsSL https://get.docker.com | sudo sh
fi
sudo systemctl enable --now docker
sudo apt-get install -y git >/dev/null

echo "== Swap (room for a burst of checks on a small machine)"
if ! swapon --show=NAME --noheadings | grep -q '^/swapfile$'; then
  sudo fallocate -l 4G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile >/dev/null
  sudo swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

echo "== Firewall"
# Oracle's Ubuntu images ship iptables rules that reject everything but SSH, including traffic
# forwarded to containers. Open the web ports and drop the blanket FORWARD reject (Docker manages
# forwarding for its own containers). The cloud's security list must allow 80 and 443 as well.
for port in 80 443; do
  sudo iptables -C INPUT -p tcp --dport "$port" -m conntrack --ctstate NEW -j ACCEPT 2>/dev/null \
    || sudo iptables -I INPUT 1 -p tcp --dport "$port" -m conntrack --ctstate NEW -j ACCEPT
done
while sudo iptables -D FORWARD -j REJECT --reject-with icmp-host-prohibited 2>/dev/null; do :; done
if command -v netfilter-persistent >/dev/null; then
  sudo netfilter-persistent save >/dev/null
fi
if command -v ufw >/dev/null && sudo ufw status | grep -q "Status: active"; then
  sudo ufw allow 80/tcp >/dev/null
  sudo ufw allow 443/tcp >/dev/null
fi

echo "== Code in $DIR"
if [ -d "$DIR/.git" ]; then
  sudo git -C "$DIR" pull --ff-only
else
  sudo git clone --depth 1 "$REPO" "$DIR"
fi
sudo chown -R "$(id -u):$(id -g)" "$DIR"
cd "$DIR"
if [ ! -f .env ]; then
  printf 'CIRCVERIFY_DOMAIN=%s\nCIRCVERIFY_PUBLIC_URL=https://%s\n' "$DOMAIN" "$DOMAIN" > .env
fi

echo "== Build (a few minutes)"
sudo docker compose build

cat <<EOF

Ready. Next:
  1. Load the index:  bash deploy/oracle/restore-data.sh /path/to/circverify-data.tar
     (or build it here: sudo docker compose run --rm watcher circverify crawl, then index)
  2. Start it:        sudo docker compose --profile https up -d
  3. Open https://$DOMAIN
EOF
