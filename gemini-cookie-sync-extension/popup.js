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

async function siteFetch(path, options) {
  for (const origin of SITE_ORIGINS) {
    try {
      const res = await fetch(origin + path, Object.assign({ credentials: "include" }, options || {}));
      return res;
    } catch (e) { /* try next origin */ }
  }
  return null;
}

async function refreshAccountPanel() {
  const emailEl = document.getElementById("account-email");
  const statusEl = document.getElementById("account-status");
  try {
    const meRes = await siteFetch("/api/auth/me");
    if (!meRes || !meRes.ok) {
      emailEl.textContent = "Not signed in to Ngotra";
      statusEl.textContent = "Sign in on ngotra.site first.";
      statusEl.className = "note warn";
      return;
    }
    const me = await meRes.json();
    emailEl.textContent = (me.user && me.user.email) || "Signed in to Ngotra";

    const connRes = await siteFetch("/api/user/cookie");
    if (connRes && connRes.ok) {
      const conn = await connRes.json();
      const ok = conn.connection && conn.connection.valid;
      statusEl.textContent = "Connection status: " + (ok ? "Connected ✓" : "Not connected");
      statusEl.className = "note " + (ok ? "ok" : "warn");
    } else {
      statusEl.textContent = "Connection status: unavailable";
      statusEl.className = "note warn";
    }
  } catch (e) {
    emailEl.textContent = "Ngotra site unavailable";
    statusEl.textContent = "Open ngotra.site and sign in.";
    statusEl.className = "note warn";
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

    const origins = ["https://ngotra.site", "http://localhost:8081"];
    let done = false;
    let lastError = "";
    for (const origin of origins) {
      try {
        const res = await fetch(origin + "/api/user/cookie", {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ cookie: cookieString })
        });
        if (res.ok) {
          const data = await res.json();
          setStatus("Account connected successfully." + (data.message ? "\n" + data.message : ""), "ok");
          await refreshAccountPanel();
          done = true;
          break;
        }
        lastError = "HTTP " + res.status;
      } catch (e) {
        lastError = e.message || String(e);
      }
    }
    if (!done) throw new Error("Could not connect. Sign in on the Ngotra site first, then try again.\n" + lastError);
  } catch (error) {
    setStatus(error.message || String(error), "warn");
  } finally {
    connectButton.disabled = false;
  }
});

