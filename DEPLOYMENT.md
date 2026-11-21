# Ngotra AI — GitHub + Hosting Guide (ngotra.site)

This guide covers two things:
1. Pushing your code to GitHub safely (without leaking secrets/user data).
2. Hosting the platform on a VPS at `ngotra.site` with HTTPS.

---

## PART 1 — Push to GitHub

### 1.1 Generate two secret keys first

Run these in PowerShell to generate random secrets. **Save them somewhere safe** — you'll need them on the server.

```powershell
# JWT signing key
[Convert]::ToBase64String((1..48 | ForEach-Object { Get-Random -Max 256 }) -as [byte[]])

# Cookie/token encryption key
[Convert]::ToBase64String((1..48 | ForEach-Object { Get-Random -Max 256 }) -as [byte[]])
```

> Never commit these. They are supplied to the server as environment variables.

### 1.2 What is intentionally NOT pushed

The updated `.gitignore` excludes:
- `data/` and all `*.db` files (users, password hashes, encrypted sessions, logs)
- `config.json`, `*.pem`, `*.key`, `.env`, cookie files
- `node_modules/`
- `*.rar` backups (e.g. `ngotra v2.rar` — may contain secrets)

The obfuscated `gemini-connector.zip` **is** included so downloads work immediately.

### 1.3 Initialize and push

In `C:\Users\gg\Desktop\gemini-web2api-main\gemini-web2api-main`:

```powershell
# If this folder is not a git repo yet:
git init

# Check what will be added (verify NO data/ or config.json appears)
git status

# Add everything except ignored files
git add .

# First commit
git commit -m "first commit"

git branch -M main

git remote add origin https://github.com/Ngotra-Official/NgotraAI.git

git push -u origin main
```

If `git add .` shows `data/` or `config.json`, stop and fix `.gitignore` before committing.

For later updates:
```powershell
git add .
git commit -m "describe your change"
git push
```

> If GitHub asks for credentials, use a **Personal Access Token** instead of your password:
> GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic) → Generate new token (check `repo`).

---

## PART 2 — Host on a VPS (Ubuntu 22.04/24.04)

Recommended: a small VPS (1–2 GB RAM) with a public IP, plus DNS pointing `ngotra.site` and `www.ngotra.site` to that IP.

### 2.1 Install Docker

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
# log out and back in
```

### 2.2 Get the code on the server

```bash
sudo mkdir -p /opt/ngotra
sudo chown $USER:$USER /opt/ngotra
cd /opt/ngotra
git clone https://github.com/Ngotra-Official/NgotraAI.git .
```

### 2.3 Set secrets

```bash
cat > .env <<'EOF'
NGOTRA_JWT_SECRET=PASTE_THE_FIRST_GENERATED_KEY_HERE
NGOTRA_COOKIE_KEY=PASTE_THE_SECOND_GENERATED_KEY_HERE
EOF
chmod 600 .env
```

### 2.4 Start the app

```bash
docker compose up -d --build
docker compose logs -f ngotra
```

The app now listens on port `8081`. Test it:

```bash
curl http://localhost:8081/health
```

You should see `{"status":"healthy", ...}`.

### 2.5 Put Nginx + HTTPS in front (for ngotra.site)

```bash
sudo apt update
sudo apt install -y nginx certbot python3-certbot-nginx
```

Create `/etc/nginx/sites-available/ngotra`:

```nginx
server {
    listen 80;
    server_name ngotra.site www.ngotra.site;

    location / {
        proxy_pass http://127.0.0.1:8081;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Required for real-time SSE streaming
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 300s;
        chunked_transfer_encoding on;
    }
}
```

Enable it and get a free SSL certificate:

```bash
sudo ln -s /etc/nginx/sites-available/ngotra /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d ngotra.site -d www.ngotra.site
```

Certbot will automatically configure HTTPS and renewal. Verify: `https://ngotra.site/health`.

### 2.6 Cloudflare (you have a `cloudflare/` folder)

If `ngotra.site` is behind Cloudflare:
- DNS record `A` → your VPS IP, **Proxy status: DNS only** (grey cloud) until HTTPS works, then you may enable orange cloud.
- SSL/TLS mode: **Full (strict)**.
- If you use Cloudflare for the API Gateway instead of this VPS, deploy `cloudflare/worker.js` separately — that is an alternative, not required for the platform.

---

## PART 3 — First-time setup after deployment

1. Open `https://ngotra.site/` and register your admin account — **or** use the seeded admin:
   - Email: `admin@ngotra.site`
   - Password: `Admin@Ngotra2026!`
   - **Change this password immediately** in Dashboard → Settings.

2. Admin → Gemini Engine:
   - Set **Site Domain** to `ngotra.site` (this fixes links in API error messages).
   - Paste your Gemini web cookie(s), one per line, as the shared pool.

3. Admin → Monetization:
   - Add Adsterra direct links, banners, and verify the ad count / token reward / cooldown.

4. Test the API:
   ```bash
   curl https://ngotra.site/v1/models \
     -H "Authorization: Bearer sk-ngo-YOUR_KEY"
   ```

---

## PART 4 — Operations

```bash
# View logs
docker compose logs -f ngotra

# Restart
docker compose restart ngotra

# Update after a git push
cd /opt/ngotra && git pull && docker compose up -d --build

# Backup (important — contains accounts and settings)
cd /opt/ngotra && tar czf backup-$(date +%F).tar.gz data/

# Restore
tar xzf backup-YYYY-MM-DD.tar.gz
```

Set a cron to back up daily and keep recent copies off-server.

---

## Security checklist

- [ ] Two random secrets set in `.env` (not the defaults).
- [ ] `.env` is `chmod 600` and never committed.
- [ ] HTTPS enforced (certbot / Cloudflare Full strict).
- [ ] Admin password changed from the default.
- [ ] `data/` volume backed up regularly.
- [ ] Firewall allows only 80, 443, and SSH (e.g. `sudo ufw allow 80,443/tcp`).
- [ ] Users connecting Gemini accounts understand they are sharing a Google session (see Privacy Policy).

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `/health` works but site is 502 | Check `docker compose logs ngotra`; ensure Nginx proxies to `127.0.0.1:8081`. |
| API streaming stops early | Ensure `proxy_buffering off;` is in Nginx. |
| 404 on `/privacy` or old UI | Restart container: `docker compose restart ngotra`. |
| "database is locked" | Ensure `./data` is a persistent volume, not a container layer. |
| Login sessions reset on restart | JWT secret changed — keep it stable in `.env`. |
| Extension download 404 | Run `python build_extension.py` before building, or keep `gemini-connector.zip` in the repo. |
