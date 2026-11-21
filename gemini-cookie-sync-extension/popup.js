const CORE_REQUIRED = ["SAPISID"];
const SESSION_ALTERNATIVES = ["__Secure-1PSID", "__Secure-3PSID", "SID"];
const EXPORT_ORDER = [
  "SID",
  "HSID",
  "SSID",
  "APISID",
  "SAPISID",
  "LSID",
  "OSID",
  "SIDCC",
  "AEC",
  "NID",
  "COMPASS",
  "__Secure-1PAPISID",
  "__Secure-1PSID",
  "__Secure-1PSIDTS",
  "__Secure-1PSIDCC",
  "__Secure-1PSIDRTS",
  "__Secure-3PAPISID",
  "__Secure-3PSID",
  "__Secure-3PSIDTS",
  "__Secure-3PSIDCC",
  "__Secure-3PSIDRTS",
  "__Secure-OSID",
  "__Host-1PLSID",
  "__Host-3PLSID"
];

const LOOKUP_URLS = [
  "https://gemini.google.com/app",
  "https://accounts.google.com/",
  "https://www.google.com/",
  "https://google.com/"
];

const statusEl = document.getElementById("status");
const connectButton = document.getElementById("connect");

function setStatus(message, kind = "") {
  statusEl.textContent = message;
  statusEl.className = kind;
}

function normalizeDomain(domain = "") {
  return domain.replace(/^\./, "").toLowerCase();
}

function isGoogleCookie(cookie) {
  const domain = normalizeDomain(cookie.domain);
  return domain === "google.com" || domain.endsWith(".google.com");
}

function cookieKey(cookie) {
  const partition = cookie.partitionKey ? JSON.stringify(cookie.partitionKey) : "";
  return [
    cookie.storeId || "",
    cookie.name,
    cookie.domain,
    cookie.path,
    partition
  ].join("|");
}

function scoreCookie(cookie) {
  const domain = (cookie.domain || "").toLowerCase();
  let score = 0;

  if (domain === ".google.com") score += 120;
  else if (domain === "google.com") score += 110;
  else if (domain === ".gemini.google.com") score += 100;
  else if (domain === "gemini.google.com") score += 95;
  else if (domain === ".accounts.google.com") score += 80;
  else if (domain === "accounts.google.com") score += 75;
  else if (domain.endsWith(".google.com")) score += 40;

  if (cookie.path === "/") score += 10;
  if (cookie.secure) score += 3;
  if (cookie.httpOnly) score += 2;
  if (!cookie.partitionKey) score += 2;
  if (!cookie.session) score += 1;

  return score;
}

async function readGoogleCookies() {
  const stores = await chrome.cookies.getAllCookieStores();
  const deduped = new Map();

  for (const store of stores) {
    const queries = [
      chrome.cookies.getAll({ storeId: store.id }),
      ...LOOKUP_URLS.map((url) => chrome.cookies.getAll({ storeId: store.id, url }))
    ];

    const results = await Promise.allSettled(queries);

    for (const result of results) {
      if (result.status !== "fulfilled") continue;
      for (const cookie of result.value) {
        if (!isGoogleCookie(cookie) || !cookie.value) continue;
        deduped.set(cookieKey(cookie), cookie);
      }
    }
  }

  return [...deduped.values()];
}

function selectBestCookies(cookies) {
  const selected = new Map();

  for (const name of EXPORT_ORDER) {
    const candidates = cookies
      .filter((cookie) => cookie.name === name && cookie.value)
      .sort((a, b) => scoreCookie(b) - scoreCookie(a));

    if (candidates.length > 0) selected.set(name, candidates[0]);
  }

  return selected;
}

function validateSelection(selected) {
  const missingCore = CORE_REQUIRED.filter((name) => !selected.has(name));
  const sessionCookie = SESSION_ALTERNATIVES.find((name) => selected.has(name));
  return {
    missingCore,
    sessionCookie,
    valid: missingCore.length === 0 && Boolean(sessionCookie)
  };
}

async function buildInspection() {
  const cookies = await readGoogleCookies();
  const selected = selectBestCookies(cookies);
  return { selected, validation: validateSelection(selected) };
}

function buildCookieString(info) {
  return EXPORT_ORDER
    .filter((name) => info.selected.has(name))
    .map((name) => `${name}=${info.selected.get(name).value}`)
    .join("; ");
}

const SITE_ORIGINS = ["https://ngotra.site", "http://localhost:8081"];

function setDot(kind) {
  const dot = document.getElementById("account-dot");
  if (!dot) return;
  dot.className = "dot" + (kind ? " " + kind : "");
}

async function findSiteTab() {
  const tabs = await new Promise((resolve) => {
    chrome.tabs.query(
      { url: ["https://ngotra.site/*", "http://localhost:8081/*"] },
      (t) => resolve(t || [])
    );
  });
  return tabs[0] || null;
}

// Call the platform through the page's content script so the SameSite login
// cookie is included (a direct fetch from chrome-extension:// cannot send it).
async function siteApi(path, method, body) {
  const tab = await findSiteTab();
  if (!tab) return { ok: false, status: 0, data: null, error: "Keep the Ngotra tab open and signed in." };
  return new Promise((resolve) => {
    try {
      chrome.tabs.sendMessage(
        tab.id,
        { type: "ngotra-api", path: path, method: method || "GET", body: body },
        (res) => {
          if (chrome.runtime && chrome.runtime.lastError) {
            resolve({ ok: false, status: 0, data: null,
              error: "Reload the Ngotra tab to activate the connector." });
          } else {
            resolve(res || { ok: false, status: 0, data: null, error: "No response from site." });
          }
        }
      );
    } catch (e) {
      resolve({ ok: false, status: 0, data: null, error: e.message || String(e) });
    }
  });
}

async function refreshAccountPanel() {
  const emailEl = document.getElementById("account-email");
  const statusEl = document.getElementById("account-status");
  try {
    const meRes = await siteApi("/api/auth/me", "GET");
    if (!meRes || !meRes.ok) {
      emailEl.textContent = "Not signed in to Ngotra";
      statusEl.textContent = "Sign in on ngotra.site first.";
      statusEl.className = "note warn";
      setDot("warn");
      setStatus("Please sign in on the Ngotra website first.", "warn");
      return;
    }
    const me = meRes.data || {};
    const email = (me.user && me.user.email) || "Signed in to Ngotra";
    emailEl.textContent = email;

    const connRes = await siteApi("/api/user/cookie", "GET");
    if (connRes && connRes.ok) {
      const conn = connRes.data || {};
      const ok = conn.connection && conn.connection.valid;
      statusEl.textContent = ok ? "Connected & verified ✓" : "Not connected";
      statusEl.className = "note " + (ok ? "ok" : "warn");
      setDot(ok ? "ok" : "warn");
      if (ok) {
        setStatus("Signed in as " + email + ".\nYour Gemini account is connected.", "ok");
        connectButton.textContent = "Reconnect Gemini Account";
      } else {
        setStatus("Signed in as " + email + ".\nVerify your Gemini account below.", "warn");
        connectButton.textContent = "Verify & Connect";
      }
    } else {
      statusEl.textContent = "Connection status: unavailable";
      statusEl.className = "note warn";
      setDot("warn");
      setStatus("Signed in as " + email + ".\nCould not check connection.", "warn");
    }
  } catch (e) {
    emailEl.textContent = "Ngotra site unavailable";
    statusEl.textContent = "Open ngotra.site and sign in.";
    statusEl.className = "note warn";
    setDot("warn");
  }
}

refreshAccountPanel();

connectButton.addEventListener("click", async () => {
  connectButton.disabled = true;
  setStatus("Verifying your Gemini account…");

  try {
    const info = await buildInspection();
    if (!info.validation.valid) {
      await chrome.tabs.create({ url: "https://gemini.google.com/app" });
      throw new Error("Please sign in to Gemini in the tab that opened, then click Verify and Connect again.");
    }
    const cookieString = buildCookieString(info);

    const res = await siteApi("/api/user/cookie", "POST", { cookie: cookieString });
    let done = res.ok;
    let lastError = res.error || ("HTTP " + res.status);
    if (done) {
      const data = res.data || {};
      setStatus("Account connected successfully." + (data.message ? "\n" + data.message : ""), "ok");
      await refreshAccountPanel();
    }
    if (!done) throw new Error("Could not connect. Sign in on the Ngotra site first, then try again.\n" + lastError);
  } catch (error) {
    setStatus(error.message || String(error), "warn");
  } finally {
    connectButton.disabled = false;
  }
});

