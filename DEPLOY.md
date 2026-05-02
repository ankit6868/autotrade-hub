# AutoTrade Hub — Deployment Guide

Three ways to host. Pick whichever fits you.

---

## Option A — VPS (Recommended, $6–12/month)

Best for: full control, real trading, 24/7 uptime.
Works on: DigitalOcean, Vultr, Linode, Hetzner.

### 1. Create a server
- OS: **Ubuntu 22.04 LTS**
- Size: **2 vCPU / 2 GB RAM** minimum ($12/mo DigitalOcean, $6/mo Hetzner)
- Enable SSH access

### 2. Point your domain
Create an **A record** in your DNS pointing `trade.yourdomain.com → your-server-ip`.
(Or use just the IP for testing — HTTPS won't auto-provision without a domain.)

### 3. SSH into the server and deploy
```bash
ssh root@your-server-ip

# Upload your project (from your local machine)
scp -r C:\Users\Ankit\Desktop\tradebot root@your-server-ip:/opt/tradebot

# Or clone from GitHub if you pushed it:
git clone https://github.com/YOUR/tradebot.git /opt/tradebot

cd /opt/tradebot
```

### 4. Configure environment
```bash
cp .env.prod.example .env.prod
nano .env.prod
```

Fill in these values:
| Variable | How to get it |
|---|---|
| `APP_SECRET_KEY` | Run: `python3 -c "import secrets; print(secrets.token_hex(32))"` |
| `POSTGRES_PASSWORD` | Any strong password, e.g. `openssl rand -base64 24` |
| `DOMAIN` | Your domain, e.g. `trade.example.com` |
| `CLERK_JWKS_URL` | Clerk Dashboard → API Keys → JWKS URL |
| `CLERK_ISSUER` | Same page, Issuer URL |
| `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` | Clerk Dashboard → Publishable key |
| `CLERK_SECRET_KEY` | Clerk Dashboard → Secret key |

### 5. Run the deploy script
```bash
chmod +x deploy.sh
sudo ./deploy.sh
```

That's it. Caddy auto-issues a Let's Encrypt SSL cert.
Your app will be live at `https://trade.yourdomain.com`.

---

## Option B — Railway (~$5/month, easiest cloud)

Best for: quick testing, no server management.

1. Push your code to GitHub
2. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub
3. Select the repo
4. Railway auto-detects `railway.toml` and builds the backend
5. Add a **PostgreSQL** service (click Add → Database → PostgreSQL)
6. Set environment variables in Railway dashboard (same as `.env.prod` values)
7. For the frontend: create a second service → select the `frontend/` directory

---

## Option C — Render (Free tier available)

Best for: zero cost testing (sleeps after 15 min idle).

1. Push to GitHub
2. Go to [render.com](https://render.com) → New → Blueprint
3. Select your repo — Render reads `render.yaml` automatically
4. Fill in the env vars in the Render dashboard
5. Deploy

> ⚠️ Free tier services sleep after 15 min — not suitable for real trading.
> Upgrade to Starter ($7/mo) for always-on.

---

## Updating after changes

```bash
# On the VPS:
cd /opt/tradebot
git pull
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --build
```

---

## Useful commands

```bash
# View logs
docker compose -f docker-compose.prod.yml --env-file .env.prod logs -f

# View just backend logs
docker compose -f docker-compose.prod.yml --env-file .env.prod logs -f backend

# Stop everything
docker compose -f docker-compose.prod.yml --env-file .env.prod down

# Restart a single service
docker compose -f docker-compose.prod.yml --env-file .env.prod restart backend

# Run database migrations manually
docker compose -f docker-compose.prod.yml --env-file .env.prod exec backend alembic upgrade head

# Open a shell in the backend container
docker compose -f docker-compose.prod.yml --env-file .env.prod exec backend bash
```

---

## What gets deployed

| Service | Port | Description |
|---|---|---|
| `caddy` | 80, 443 | HTTPS reverse proxy, auto SSL |
| `frontend` | 3000 (internal) | Next.js app |
| `backend` | 8000 (internal) | FastAPI + trading engine |
| `postgres` | 5432 (internal) | PostgreSQL database |

Only Caddy is exposed to the internet. Everything else is on a private Docker network.

---

## Clerk setup for production

1. Go to [dashboard.clerk.com](https://dashboard.clerk.com)
2. Create a production application (or use existing)
3. Add your domain to **Allowed origins**: `https://trade.yourdomain.com`
4. Copy the **Publishable key** → `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`
5. Copy the **Secret key** → `CLERK_SECRET_KEY`
6. Copy **JWKS URL** → `CLERK_JWKS_URL`
7. Copy **Issuer** → `CLERK_ISSUER`

---

## Minimum server specs

| Plan | Specs | Suitable for |
|---|---|---|
| Dev/Testing | 1 vCPU, 1 GB RAM | Paper trading only |
| Production | 2 vCPU, 2 GB RAM | Live trading, up to 10 users |
| Scale | 4 vCPU, 4 GB RAM | 10–50 users |
