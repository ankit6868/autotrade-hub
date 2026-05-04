# Deploy: Render + Vercel + Supabase

Three-service stack, all on free tier:

| Component | Service  | Free tier                                 |
|-----------|----------|-------------------------------------------|
| Database  | Supabase | 500 MB Postgres, never sleeps             |
| Backend   | Render   | 750 hrs/month, sleeps after ~15min idle   |
| Frontend  | Vercel   | Unlimited bandwidth, never sleeps         |

> Repo: https://github.com/ankit6868/autotrade-hub
> Branch: `main` (commit `c82db53` or newer)

---

## Already done

- [x] Supabase Postgres project created (`tradebot`, region `ap-south-1` / Mumbai)
- [x] `render.yaml` blueprint committed (backend-only)
- [x] `frontend/vercel.json` committed
- [x] Clerk dev keys ready

The pre-filled values for every step below are in `.deploy-creds.tmp` (gitignored, in repo root).

---

## 1. Deploy backend → Render

1. Open **https://dashboard.render.com/select-repo?type=blueprint**
2. Pick the `ankit6868/autotrade-hub` repo → **Connect**.
3. Render auto-detects `render.yaml` and shows one service: `autotrade-backend`.
4. Render will ask you to fill four `sync: false` env vars before the build starts:

| Key                    | Value                                                                                                                            |
|------------------------|----------------------------------------------------------------------------------------------------------------------------------|
| `DATABASE_URL`         | `postgresql+psycopg://postgres.amprzizjsjqlnvopgkid:wDYb397laVk0qa0u@aws-1-ap-south-1.pooler.supabase.com:5432/postgres`         |
| `CLERK_JWKS_URL`       | `https://square-bream-24.clerk.accounts.dev/.well-known/jwks.json`                                                               |
| `CLERK_ISSUER`         | `https://square-bream-24.clerk.accounts.dev`                                                                                     |
| `CORS_ALLOWED_ORIGINS` | `https://autotrade-hub.vercel.app` *(temporary placeholder — update after step 2)*                                               |

> The other env vars (`APP_SECRET_KEY`, `ENV`, `ENABLE_DOCS`, `RUN_MIGRATIONS`, `APP_VERSION`) are auto-set by `render.yaml`.

5. Click **Apply**. First build takes ~5–7 minutes (Docker layer + pip install).
6. When status turns **Live**, copy the public URL — it will look like:
   ```
   https://autotrade-backend.onrender.com
   ```
7. Sanity check:
   ```bash
   curl https://autotrade-backend.onrender.com/api/health
   # → {"status":"ok",...}
   ```

> **Note**: Render free tier sleeps after 15 minutes of inactivity. First request after sleep takes ~30 seconds to wake up. The frontend is configured to retry, so you'll just see a brief loading state.

---

## 2. Deploy frontend → Vercel

1. Open **https://vercel.com/new**
2. Pick `ankit6868/autotrade-hub` → **Import**.
3. **Configure Project**:
   - **Root Directory**: click *Edit* → `frontend`
   - **Framework Preset**: Next.js *(auto-detected)*
   - **Build Command**: leave default *(reads from vercel.json)*
   - **Install Command**: leave default
4. Expand **Environment Variables** and add:

| Key                                  | Value                                                              |
|--------------------------------------|--------------------------------------------------------------------|
| `BACKEND_URL`                        | `https://autotrade-backend.onrender.com` *(from step 1.6)*         |
| `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`  | `pk_test_c3F1YXJlLWJyZWFtLTI0LmNsZXJrLmFjY291bnRzLmRldiQ`          |
| `CLERK_SECRET_KEY`                   | `sk_test_S4VFkPnC5oeoXfklmhcesn7I5F7ulSusO8oyVEi9Yw`               |
| `NEXT_PUBLIC_CLERK_SIGN_IN_URL`      | `/sign-in`                                                         |
| `NEXT_PUBLIC_CLERK_SIGN_UP_URL`      | `/sign-up`                                                         |
| `NEXT_PUBLIC_CLERK_AFTER_SIGN_IN_URL`| `/`                                                                |
| `NEXT_PUBLIC_CLERK_AFTER_SIGN_UP_URL`| `/setup`                                                           |

5. Click **Deploy**. Build takes ~2 minutes.
6. Once live, Vercel gives you the URL — typically:
   ```
   https://autotrade-hub.vercel.app
   ```
   (or `https://autotrade-hub-<hash>-ankit6868s-projects.vercel.app` for previews).

---

## 3. Update CORS on Render

The backend's `CORS_ALLOWED_ORIGINS` placeholder needs the **real** Vercel URL.

1. Open **https://dashboard.render.com** → `autotrade-backend` → **Environment**.
2. Edit `CORS_ALLOWED_ORIGINS` → set to your actual Vercel URL (comma-separated for multiple):
   ```
   https://autotrade-hub.vercel.app,https://autotrade-hub-git-main-ankit6868s-projects.vercel.app
   ```
3. **Save Changes** — Render redeploys automatically (~1 min).

---

## 4. Configure Clerk redirect URLs

1. Open **https://dashboard.clerk.com** → your app → **Domains** / **Paths**.
2. Add the Vercel domain to **Allowed redirect URLs**:
   ```
   https://autotrade-hub.vercel.app
   https://autotrade-hub.vercel.app/sign-in
   https://autotrade-hub.vercel.app/sign-up
   ```
3. Save.

---

## 5. Smoke test

```bash
# Backend up
curl https://autotrade-backend.onrender.com/api/health

# Frontend serves
curl -I https://autotrade-hub.vercel.app

# Frontend → Backend rewrite (Vercel proxies /api/* to BACKEND_URL)
curl https://autotrade-hub.vercel.app/api/health
```

In the browser:

1. Go to `https://autotrade-hub.vercel.app`
2. Sign up with email — Clerk handles auth.
3. After sign-in, you should land on `/setup` (broker connect screen).
4. Open DevTools → Network → confirm `/api/*` calls return 200.

---

## Troubleshooting

**Backend build fails with `psycopg` error**
The `DATABASE_URL` must use the `postgresql+psycopg://` scheme (not `postgres://` or `postgresql://`). Check `.deploy-creds.tmp` for the canonical string.

**Frontend gets 401 from Clerk middleware**
Confirm the `NEXT_PUBLIC_*` vars on Vercel match what's in Clerk dashboard. After changing env vars on Vercel, you must **Redeploy** for them to take effect (Settings → Deployments → ⋯ → Redeploy).

**CORS errors in browser console**
The backend `CORS_ALLOWED_ORIGINS` doesn't include your Vercel URL — re-do step 3 with the exact origin (no trailing slash).

**Backend wakes slowly (~30s) on first request**
Expected on Render free tier. Either upgrade to Starter ($7/mo, no sleep), or hit `/api/health` from an external pinger every 10 minutes (e.g. UptimeRobot free).

**Want to switch DB regions / passwords**
Edit `.deploy-creds.tmp`, regenerate the Supabase password in their dashboard, and update `DATABASE_URL` on Render.

---

## Quick reference — env vars at a glance

### Render (backend)
```
DATABASE_URL=postgresql+psycopg://postgres.amprzizjsjqlnvopgkid:wDYb397laVk0qa0u@aws-1-ap-south-1.pooler.supabase.com:5432/postgres
CLERK_JWKS_URL=https://square-bream-24.clerk.accounts.dev/.well-known/jwks.json
CLERK_ISSUER=https://square-bream-24.clerk.accounts.dev
CORS_ALLOWED_ORIGINS=https://autotrade-hub.vercel.app
ENV=production
ENABLE_DOCS=false
RUN_MIGRATIONS=1
APP_VERSION=1.0.0
APP_SECRET_KEY=<auto-generated by render.yaml>
```

### Vercel (frontend)
```
BACKEND_URL=https://autotrade-backend.onrender.com
NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=pk_test_c3F1YXJlLWJyZWFtLTI0LmNsZXJrLmFjY291bnRzLmRldiQ
CLERK_SECRET_KEY=sk_test_S4VFkPnC5oeoXfklmhcesn7I5F7ulSusO8oyVEi9Yw
NEXT_PUBLIC_CLERK_SIGN_IN_URL=/sign-in
NEXT_PUBLIC_CLERK_SIGN_UP_URL=/sign-up
NEXT_PUBLIC_CLERK_AFTER_SIGN_IN_URL=/
NEXT_PUBLIC_CLERK_AFTER_SIGN_UP_URL=/setup
```
