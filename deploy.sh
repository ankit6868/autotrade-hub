#!/bin/bash
# =============================================================================
#  AutoTrade Hub — VPS Deployment Script
#  Works on Ubuntu 22.04 / 24.04 (DigitalOcean, Vultr, Linode, Hetzner …)
#
#  USAGE (run as root or sudo):
#    curl -sSL https://raw.githubusercontent.com/YOUR_REPO/main/deploy.sh | bash
#  OR copy to your server and run:
#    chmod +x deploy.sh && sudo ./deploy.sh
# =============================================================================
set -euo pipefail

REPO_URL="${REPO_URL:-}"          # Set to your GitHub repo URL
APP_DIR="${APP_DIR:-/opt/tradebot}"
COMPOSE_FILE="docker-compose.prod.yml"
ENV_FILE=".env.prod"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERR]${NC}   $*"; exit 1; }

# ── 1. Install Docker ─────────────────────────────────────────────────────────
install_docker() {
  if command -v docker &>/dev/null; then
    success "Docker already installed: $(docker --version)"
    return
  fi
  info "Installing Docker..."
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl gnupg
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
    https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
  systemctl enable --now docker
  success "Docker installed: $(docker --version)"
}

# ── 2. Clone / update repo ────────────────────────────────────────────────────
setup_repo() {
  if [ -z "$REPO_URL" ]; then
    warn "REPO_URL not set — skipping git clone."
    warn "Make sure your code is already in $APP_DIR"
    return
  fi

  if [ -d "$APP_DIR/.git" ]; then
    info "Pulling latest code..."
    cd "$APP_DIR" && git pull
  else
    info "Cloning repo to $APP_DIR..."
    git clone "$REPO_URL" "$APP_DIR"
  fi
  success "Code ready in $APP_DIR"
}

# ── 3. Setup .env.prod ────────────────────────────────────────────────────────
setup_env() {
  cd "$APP_DIR"
  if [ ! -f "$ENV_FILE" ]; then
    if [ -f ".env.prod.example" ]; then
      cp .env.prod.example "$ENV_FILE"
      warn ".env.prod created from example — YOU MUST EDIT IT BEFORE CONTINUING"
      warn "  nano $APP_DIR/.env.prod"
      echo ""
      echo "Required values to fill in:"
      echo "  APP_SECRET_KEY     — run: python3 -c \"import secrets; print(secrets.token_hex(32))\""
      echo "  POSTGRES_PASSWORD  — any strong password"
      echo "  DOMAIN             — your domain or server IP"
      echo "  CLERK_SECRET_KEY   — from https://dashboard.clerk.com"
      echo ""
      read -p "Press Enter after editing .env.prod to continue..." _
    else
      error ".env.prod not found and no .env.prod.example to copy from. Create it manually."
    fi
  else
    success ".env.prod already exists"
  fi
}

# ── 4. Open firewall ──────────────────────────────────────────────────────────
setup_firewall() {
  if command -v ufw &>/dev/null; then
    info "Configuring UFW firewall..."
    ufw allow 22/tcp   comment "SSH"    2>/dev/null || true
    ufw allow 80/tcp   comment "HTTP"   2>/dev/null || true
    ufw allow 443/tcp  comment "HTTPS"  2>/dev/null || true
    ufw --force enable 2>/dev/null || true
    success "Firewall configured (SSH + HTTP + HTTPS)"
  fi
}

# ── 5. Build & start ──────────────────────────────────────────────────────────
deploy() {
  cd "$APP_DIR"
  info "Building Docker images (this takes a few minutes on first run)..."
  docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" build --parallel

  info "Starting containers..."
  docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" up -d

  info "Waiting for services to be healthy..."
  sleep 15

  info "Container status:"
  docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" ps
}

# ── 6. Print summary ──────────────────────────────────────────────────────────
print_summary() {
  DOMAIN=$(grep '^DOMAIN=' "$APP_DIR/$ENV_FILE" | cut -d= -f2 | tr -d ' ')
  echo ""
  echo "============================================================"
  success "AutoTrade Hub deployed!"
  echo "============================================================"
  echo ""
  if [[ "$DOMAIN" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "  URL (IP):  http://$DOMAIN"
    warn "  No HTTPS — set a real domain and re-deploy for SSL"
  else
    echo "  URL:       https://$DOMAIN"
    echo "  (Caddy will auto-issue a Let's Encrypt cert — wait 30s)"
  fi
  echo ""
  echo "  Logs:      docker compose -f $COMPOSE_FILE --env-file $ENV_FILE logs -f"
  echo "  Stop:      docker compose -f $COMPOSE_FILE --env-file $ENV_FILE down"
  echo "  Update:    git pull && docker compose ... up -d --build"
  echo ""
}

# ── Main ──────────────────────────────────────────────────────────────────────
main() {
  echo ""
  echo "============================================================"
  echo "  AutoTrade Hub — Deployment Script"
  echo "============================================================"
  echo ""

  [ "$(id -u)" -eq 0 ] || error "Run as root: sudo ./deploy.sh"

  install_docker
  setup_repo
  setup_env
  setup_firewall
  deploy
  print_summary
}

main "$@"
