// Background service worker: reads the signed-in Gemini session and connects
// it to the platform when the site asks (one-click flow).

const COOKIE_ORDER = [
  "SID", "HSID", "SSID", "APISID", "SAPISID", "LSID", "OSID", "SIDCC", "AEC", "NID",
  "COMPASS", "__Secure-1PAPISID", "__Secure-1PSID", "__Secure-1PSIDTS",
  "__Secure-1PSIDCC", "__Secure-3PAPISID", "__Secure-3PSID", "__Secure-3PSIDTS",
  "__Secure-3PSIDCC", "__Secure-OSID"
];
const CORE = ["SAPISID"];
const SESSION_ALTERNATIVES = ["__Secure-1PSID", "__Secure-3PSID", "SID"];

async function readBestGeminiCookies() {
  const stores = await chrome.cookies.getAllCookieStores();
  const best = new Map();
  for (const store of stores) {
    const cookies = await chrome.cookies.getAll({ storeId: store.id });
    for (const c of cookies) {
      if (!c.value) continue;
      const domain = (c.domain || "").replace(/^\./, "").toLowerCase();
      if (domain !== "google.com" && !domain.endsWith(".google.com")) continue;
      const prev = best.get(c.name);
      if (!prev || (c.path === "/" && prev.path !== "/") || (c.secure && !prev.secure)) {
        best.set(c.name, c);
      }
    }
  }
  const hasCore = CORE.every((n) => best.has(n));
  const hasSession = SESSION_ALTERNATIVES.some((n) => best.has(n));
  if (!hasCore || !hasSession) return null;
  return COOKIE_ORDER
    .filter((n) => best.has(n))
    .map((n) => `${n}=${best.get(n).value}`)
    .join("; ");
}

async function connectToPlatform(origin, cookie) {
  const res = await fetch(`${origin}/api/user/cookie`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ cookie }),
  });
  let data = {};
  try { data = await res.json(); } catch (e) { /* ignore */ }
  return {
    ok: res.ok,
    message: data.message || (data.detail && data.detail.message) || `HTTP ${res.status}`,
  };
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || message.type !== "ngotra-connect") return false;

  (async () => {
    try {
      const cookie = await readBestGeminiCookies();
      if (!cookie) {
        sendResponse({
          ok: false,
          message: "No Gemini session found. Open gemini.google.com, sign in, then try again.",
          openGemini: true,
        });
        return;
      }
      const origins = [message.origin, "http://localhost:8081", "https://ngotra.site"];
      let result = { ok: false, message: "Could not reach the platform." };
      for (const origin of origins) {
        if (!origin) continue;
        try {
          result = await connectToPlatform(origin, cookie);
          if (result.ok) break;
        } catch (e) {
          result = { ok: false, message: e.message || String(e) };
        }
      }
      sendResponse(result);
    } catch (e) {
      sendResponse({ ok: false, message: e.message || String(e) });
    }
  })();

  return true; // keep message channel open for async response
});
