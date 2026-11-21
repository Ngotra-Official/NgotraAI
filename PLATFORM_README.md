# Ngotra AI Platform (ngotra.site)

Complete Web App & API Gateway Platform built on top of `gemini_web2api` with Adsterra Monetization, User Dashboard, Admin Panel, and a free Gemini API anyone can use.

---

## 🌟 Highlights

1. **Modern Light-Themed Web Application**:
   - Modern, professional light theme using Tailwind CSS and Lucide icons.
   - **Landing Page (`/`)**: Hero, models showcase, "How It Works", and connection details.
   - **User Dashboard (`/dashboard`)**: API key management, live token balance, ad-cycle cooldown, 3-step Ad Unlocker, in-browser AI Playground, and copy-ready API examples.
   - **Admin Control Center (`/admin`)**: Analytics, Monetization manager, Gemini engine config, user management with VIP/manual token grants, and live API request logs.

2. **Ad-Gated Token Model (Watch 3 Ads = 500,000 Tokens)**:
   - Users generate their personal API keys (`sk-ngo-...`).
   - Each API request spends its estimated token usage from the user's balance.
   - With an empty balance, requests return **HTTP 402** with instructions to watch ads.
   - Users complete the 3-step Adsterra direct link viewer (with a 10s verification countdown) to add **500,000 tokens**.
   - A new ad cycle unlocks after a configurable cooldown (default: **1 hour**).

3. **Complete Adsterra Monetization**:
   - **Direct Links**: Dynamic rotation of Adsterra direct links in the 3-step unlock flow.
   - **Banners**:
     - `728x90` Leaderboard (Top of landing & dashboard)
     - `300x250` Medium Rectangle (Dashboard sidebar)
     - `468x60` In-Page Banner
   - **Popunder & Social Bar**: Script injection slots manageable in real time from the Admin Panel.

4. **OpenAI-Compatible Gemini API for Any Client**:
   - One endpoint that works with any OpenAI-compatible app, tool, or script:
     - Web apps, chat UIs, editors and IDEs
     - Python and Node.js `openai` SDKs
     - `curl` and any HTTP client
     - Framework integrations such as LangChain
   - Supports **Streaming SSE**, **Tool / Function Calling**, and **Multimodal Image Uploads**.

---

## 🚀 Quick Start

### 1. Start the Platform
```bash
python run_platform.py --host 0.0.0.0 --port 8081
```

### 2. Access the Web App
- **Landing Page**: `http://localhost:8081/` (or `https://ngotra.site/`)
- **User Dashboard**: `http://localhost:8081/dashboard`
- **Admin Control Center**: `http://localhost:8081/admin`
- **OpenAI API Base URL**: `http://localhost:8081/v1` (or `https://ngotra.site/v1`)

---

## 🔑 Default Admin Account

During first launch, the database is automatically initialized with:
- **Admin Email**: `admin@ngotra.site`
- **Admin Password**: `Admin@Ngotra2026!`
- **Master Admin Key**: `sk-ngo-admin-master-key` (Permanent VIP unlimited bypass)

> **Note**: You can change your password or create additional admin users anytime in the platform.

---

## 🛠️ Admin Panel Capabilities

Access `/admin` with the admin credentials:
1. **Metrics & Overview**: View total registered users, funded users, today's requests, and ad impressions.
2. **Adsterra & Monetization Manager**:
   - Set number of ads required per cycle (default: 3).
   - Set tokens granted per ad cycle (default: 500,000).
   - Set ad cycle cooldown in hours (default: 1 hour).
   - Set ad verification cooldown timer (default: 10s).
   - Paste Adsterra Direct Link URLs (rotates between them).
   - Paste Adsterra 728x90, 300x250, and 468x60 banner HTML/scripts.
   - Paste Adsterra Popunder and Social Bar scripts.
   - Independent toggles for banners, popunder, and direct link flow.
3. **Gemini Engine & Domain Settings**:
   - Configure Gemini cookies directly from UI (no file editing required). Paste one cookie per line to build a pool; users are assigned consistently to one account so load is spread out.

### Connect Gemini Account (browser connector)

Users sign in to Gemini in their own browser and connect with the free **Ngotra Gemini Connector** browser extension. The connector shares only their Gemini web-session cookie with the site; it is stored encrypted and never exposed to other users. A verified connection is required before API keys can be created or API requests accepted. Live connection status, re-verification, and disconnect are available in Dashboard → Settings.

   - Set proxy URL, default model, and backend BL version.
   - Changes take effect immediately without restarting the server!
4. **User Management**:
   - Search users by email.
   - Grant manual tokens (+500k, +1M).
   - Toggle permanent VIP bypass (unlimited access without ads).
   - Ban or unban accounts.
5. **Live Request Logs**:
   - Real-time table of incoming queries, models used, tokens, latency, status codes, and IP addresses.

---

## API Setup Guide

### Connection details
- **Base URL**: `https://ngotra.site/v1`
- **API Key**: `sk-ngo-YOUR_KEY`
- **Model**: `gemini-3.7-flash` (or `gemini-3.5-flash-thinking`)

Use these three values in any OpenAI-compatible client. Watch 3 ads to earn 500,000 tokens; a new ad cycle unlocks after 1 hour.

### cURL
```bash
curl https://ngotra.site/v1/chat/completions \
  -H "Authorization: Bearer sk-ngo-YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini-3.7-flash","messages":[{"role":"user","content":"Hello!"}]}'
```

### Python example
```python
from openai import OpenAI

client = OpenAI(
    base_url="https://ngotra.site/v1",
    api_key="sk-ngo-YOUR_KEY"
)

response = client.chat.completions.create(
    model="gemini-3.7-flash",
    messages=[{"role": "user", "content": "Hello Ngotra AI!"}],
    stream=True
)

for chunk in response:
    print(chunk.choices[0].delta.content or "", end="")
```

---

## 🧪 Testing

Run the full automated test suite:
```bash
python tests/test_platform.py
```
This tests all pages, public config, user registration, empty-balance 402 checks, the 3-step ad unlock flow, token grants and cooldown, and the OpenAI `/v1/models` endpoint.
