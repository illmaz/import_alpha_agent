#!/usr/bin/env bash
#
# One-time VPS preparation. Run ON the server, as root, on a fresh Ubuntu 22.04
# droplet:
#
#   ssh root@<ip>
#   git clone <repo> /opt/importalpha && cd /opt/importalpha
#   ./deploy/setup.sh api.example.com you@example.com
#
# Installs Docker, creates a non-root deploy user, configures the firewall,
# adds swap, installs certbot and obtains the first certificate.
#
# Idempotent: safe to re-run. It does not start the application — that is
# deploy.sh, run from your laptop.

set -euo pipefail

DOMAIN="${1:-}"
EMAIL="${2:-}"
DEPLOY_USER="${DEPLOY_USER:-deploy}"
APP_DIR="${APP_DIR:-/opt/importalpha}"
SWAP_SIZE="${SWAP_SIZE:-2G}"

if [ -z "$DOMAIN" ] || [ -z "$EMAIL" ]; then
  echo "usage: $0 <domain> <email-for-letsencrypt>" >&2
  echo "   eg: $0 api.example.com ops@example.com" >&2
  exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
  echo "error: run as root (this installs packages and edits the firewall)" >&2
  exit 1
fi

log() { printf '\n==> %s\n' "$1"; }

# --- packages ---------------------------------------------------------------

log "Updating packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl gnupg git ufw

# --- docker -----------------------------------------------------------------

if command -v docker >/dev/null 2>&1; then
  log "Docker already installed ($(docker --version))"
else
  log "Installing Docker from the official repository"
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io \
    docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker
fi

# --- swap -------------------------------------------------------------------
# A 2 GB droplet building images and running a JVM will OOM without it. Swap is
# a safety net for build spikes, not a substitute for RAM.

if swapon --show | grep -q '/swapfile'; then
  log "Swap already configured"
else
  log "Creating ${SWAP_SIZE} swap file"
  fallocate -l "$SWAP_SIZE" /swapfile
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  sysctl -w vm.swappiness=10 >/dev/null
  grep -q '^vm.swappiness' /etc/sysctl.conf || echo 'vm.swappiness=10' >> /etc/sysctl.conf
fi

# --- firewall ---------------------------------------------------------------
# Allow SSH before enabling, or you lock yourself out of the box.

log "Configuring firewall"
ufw allow OpenSSH >/dev/null
ufw allow 80/tcp  >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null
ufw status verbose

# --- deploy user ------------------------------------------------------------

if id "$DEPLOY_USER" >/dev/null 2>&1; then
  log "User $DEPLOY_USER already exists"
else
  log "Creating $DEPLOY_USER"
  adduser --disabled-password --gecos "" "$DEPLOY_USER"
fi
usermod -aG docker "$DEPLOY_USER"

# Copy root's authorised keys so you can ssh in as the deploy user.
if [ -f /root/.ssh/authorized_keys ]; then
  install -d -m 700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "/home/$DEPLOY_USER/.ssh"
  install -m 600 -o "$DEPLOY_USER" -g "$DEPLOY_USER" \
    /root/.ssh/authorized_keys "/home/$DEPLOY_USER/.ssh/authorized_keys"
fi

# --- unattended security updates --------------------------------------------

log "Enabling unattended security upgrades"
apt-get install -y -qq unattended-upgrades
dpkg-reconfigure -f noninteractive unattended-upgrades

# --- application directory ---------------------------------------------------

log "Preparing $APP_DIR"
mkdir -p "$APP_DIR" /var/www/certbot
chown -R "$DEPLOY_USER:$DEPLOY_USER" "$APP_DIR"

# --- certificate -------------------------------------------------------------
# Standalone mode binds :80 itself, so it must run before nginx starts. After
# this, renewals use the webroot that the nginx container mounts.

log "Installing certbot"
apt-get install -y -qq certbot

if [ -d "/etc/letsencrypt/live/$DOMAIN" ]; then
  log "Certificate for $DOMAIN already present"
else
  log "Requesting a certificate for $DOMAIN"
  echo "    DNS must already point at this box, or this step fails."
  certbot certonly --standalone \
    --non-interactive --agree-tos \
    --email "$EMAIL" \
    -d "$DOMAIN" \
    --preferred-challenges http
fi

# Renew via the webroot the nginx container serves, then reload nginx.
cat > /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh <<'HOOK'
#!/usr/bin/env bash
docker exec importalpha-nginx nginx -s reload 2>/dev/null || true
HOOK
chmod +x /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh

# --- nginx config ------------------------------------------------------------

NGINX_CONF="$APP_DIR/deploy/nginx.conf"
if [ -f "$NGINX_CONF" ] && grep -q REPLACE_WITH_DOMAIN "$NGINX_CONF"; then
  log "Writing $DOMAIN into deploy/nginx.conf"
  sed -i "s/REPLACE_WITH_DOMAIN/$DOMAIN/g" "$NGINX_CONF"
fi

log "Done."
cat <<EOS

Next:
  1. Put the production .env on the server at $APP_DIR/.env
     (see docs/DEPLOYMENT.md — it is never committed and never copied by
     these scripts).
  2. From your laptop:  DEPLOY_HOST=$DEPLOY_USER@$DOMAIN ./deploy/deploy.sh
  3. Check:             https://$DOMAIN/health

Certificate renewal is handled by the certbot systemd timer already installed
by the package. Confirm with:  systemctl list-timers | grep certbot
EOS
