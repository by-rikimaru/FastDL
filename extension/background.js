// FastDL Yakalayıcı - arka plan servisi
const APP = "http://127.0.0.1:9614";
const HOST_RE = /(^|\.)fuckingfast\.(co|net|com)$/i;
// Dosya sayfası: https://fuckingfast.net/ycocmxztk0ti (#dosyaadi olabilir)
const PAGE_RE = /^https?:\/\/(?:www\.)?fuckingfast\.(?:co|net|com)\/[a-z0-9]{8,16}\/?(?:[?#].*)?$/i;

const isFF = (u) => { try { return HOST_RE.test(new URL(u).hostname); } catch { return false; } };
const hashName = (u) => {
  try {
    const h = decodeURIComponent(new URL(u).hash.slice(1));
    return /\.\w{2,5}$/.test(h) ? h : null;
  } catch { return null; }
};

async function appAlive() {
  try { const r = await fetch(APP + "/ping"); return r.ok; } catch { return false; }
}

async function cookieHeader(url) {
  const list = await chrome.cookies.getAll({ url });
  return list.map((c) => `${c.name}=${c.value}`).join("; ");
}

async function sendToApp(items) {
  const payload = [];
  for (const it of items) {
    payload.push({
      url: it.url,
      filename: it.filename || null,
      referer: it.referer || null,
      cookies: await cookieHeader(it.url),
      userAgent: navigator.userAgent,
    });
  }
  const r = await fetch(APP + "/add", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-FastDL": "1" },
    body: JSON.stringify(payload),
  });
  if (!r.ok) throw new Error("FastDL yanıt vermedi");
}

function notify(title, msg) {
  chrome.action.setBadgeText({ text: msg ? "!" : "" });
  chrome.action.setTitle({ title: `FastDL - ${title}${msg ? ": " + msg : ""}` });
}

// --- 1) Tarayıcıda başlayan indirmeleri yakala ---
chrome.downloads.onCreated.addListener(async (item) => {
  const { captureAll } = await chrome.storage.local.get("captureAll");
  const url = item.finalUrl || item.url;
  if (!url.startsWith("http")) return;
  // /dl/ linki başka bir sunucuya yönlenebilir: orijinal url, son url ve referrer'a bak
  if (!captureAll && ![item.url, item.finalUrl, item.referrer].some((u) => u && isFF(u))) return;
  try { await chrome.downloads.pause(item.id); } catch {}
  if (!(await appAlive())) {
    try { await chrome.downloads.resume(item.id); } catch {}
    notify("Uygulama kapalı", "indirme tarayıcıda devam ediyor");
    return;
  }
  try {
    const name = item.filename ? item.filename.split(/[\\/]/).pop() : null;
    await sendToApp([{ url, referer: item.referrer, filename: name }]);
    await chrome.downloads.cancel(item.id);
    await chrome.downloads.erase({ id: item.id });
    notify("Gönderildi", "");
  } catch (e) {
    try { await chrome.downloads.resume(item.id); } catch {}
  }
});

// --- 2) fuckingfast sayfasından gerçek indirme linkini çıkar ---
// Sayfanın içinde çalışır: "/<id>/download?t=..." linkini bulur, tarayıcıda
// 1 baytlık istekle çözüp asıl dosya sunucusu adresini (ts.fuckingfast.net/d/...) döndürür.
async function extractInPage() {
  const html = document.documentElement.outerHTML.replace(/\\\//g, "/");
  let m = html.match(/["'](\/[a-z0-9]+\/download\?t=[^"'\s<>]+)["']/i);
  let link = m ? m[1].replace(/&amp;/g, "&") : null;
  if (!link) {
    // Eski site düzeni
    m = html.match(/https?:\/\/(?:[\w-]+\.)?fuckingfast\.(?:co|net|com)\/dl\/[^"'\s<>\\)]+/i);
    link = m ? m[0] : null;
  }
  if (!link) return null;
  const abs = new URL(link, location.href).href;
  const title = ((document.querySelector("meta[name=title]") || {}).content || document.title || "").trim();
  let finalUrl = abs;
  try {
    const ac = new AbortController();
    const r = await fetch(abs, { headers: { Range: "bytes=0-0" }, signal: ac.signal });
    if (r.ok && !(r.headers.get("content-type") || "").includes("text/html")) finalUrl = r.url;
    ac.abort();
  } catch {}
  return { url: finalUrl, title };
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function resolvePage(pageUrl) {
  const tab = await chrome.tabs.create({ url: pageUrl, active: false });
  try {
    const t0 = Date.now();
    let shown = false;
    while (Date.now() - t0 < 120000) {
      await sleep(1500);
      let res = null;
      try {
        const [r] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: extractInPage });
        res = r && r.result;
      } catch {}
      if (res) return { url: res.url, filename: hashName(pageUrl) || res.title || null, referer: pageUrl };
      // Doğrulama ekranı varsa kullanıcının görmesi için sekmeyi öne getir
      if (!shown && Date.now() - t0 > 8000) {
        shown = true;
        await chrome.tabs.update(tab.id, { active: true });
      }
    }
    throw new Error("Link bulunamadı: " + pageUrl);
  } finally {
    chrome.tabs.remove(tab.id).catch(() => {});
  }
}

let running = false;
async function processPages(urls) {
  if (!(await appAlive())) { notify("Uygulama kapalı", "önce FastDL'i açın"); return { ok: 0, fail: urls.length }; }
  const queue = [...new Set(urls)];
  const { pending = [] } = await chrome.storage.local.get("pending");
  await chrome.storage.local.set({ pending: [...new Set([...pending, ...queue])] });
  if (running) return { queued: queue.length };
  running = true;
  let ok = 0, fail = 0;
  try {
    while (true) {
      const { pending: p = [] } = await chrome.storage.local.get("pending");
      if (!p.length) break;
      const u = p[0];
      chrome.action.setBadgeText({ text: String(p.length) });
      try { await sendToApp([await resolvePage(u)]); ok++; } catch { fail++; }
      const { pending: p2 = [] } = await chrome.storage.local.get("pending");
      await chrome.storage.local.set({ pending: p2.filter((x) => x !== u) });
    }
  } finally {
    running = false;
    chrome.action.setBadgeText({ text: fail ? "!" : "" });
    chrome.action.setTitle({ title: `FastDL - ${ok} gönderildi, ${fail} başarısız` });
  }
  return { ok, fail };
}

async function linksOnTab(tabId, onlySelection) {
  const [r] = await chrome.scripting.executeScript({
    target: { tabId },
    args: [onlySelection],
    func: (sel) => {
      let anchors = [...document.querySelectorAll("a[href]")];
      if (sel) {
        const s = window.getSelection();
        anchors = anchors.filter((a) => s && s.containsNode(a, true));
      }
      return anchors.map((a) => a.href);
    },
  });
  return (r.result || []).filter((u) => PAGE_RE.test(u));
}

// --- 3) Sağ tık menüleri ---
chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({ id: "one", title: "FastDL ile indir", contexts: ["link"] });
  chrome.contextMenus.create({ id: "sel", title: "Seçili fuckingfast linklerini FastDL'e gönder", contexts: ["selection"] });
  chrome.contextMenus.create({ id: "all", title: "Sayfadaki tüm fuckingfast linklerini FastDL'e gönder", contexts: ["page"] });
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId === "one") {
    const u = info.linkUrl;
    if (PAGE_RE.test(u)) processPages([u]);
    else if (await appAlive()) sendToApp([{ url: u, referer: info.pageUrl, filename: hashName(u) }]);
  } else {
    const links = await linksOnTab(tab.id, info.menuItemId === "sel");
    if (links.length) processPages(links);
  }
});

// --- Popup mesajları ---
chrome.runtime.onMessage.addListener((msg, _s, reply) => {
  (async () => {
    if (msg.type === "status") {
      const { pending = [], captureAll = false } = await chrome.storage.local.get(["pending", "captureAll"]);
      reply({ alive: await appAlive(), pending: pending.length, captureAll });
    } else if (msg.type === "sendAll") {
      const tab = await chrome.tabs.get(msg.tabId);
      if (PAGE_RE.test(tab.url || "")) {
        // Şu an bir dosya sayfasındayız: bu sekmede çöz, yeni sekme açma
        const [r] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: extractInPage });
        const res = r && r.result;
        if (res && (await appAlive())) {
          await sendToApp([{ url: res.url, filename: hashName(tab.url) || res.title, referer: tab.url }]);
          reply({ count: 1 });
        } else reply({ count: 0, error: res ? "FastDL kapalı" : "Link bulunamadı (doğrulama bitmemiş olabilir)" });
        return;
      }
      const links = await linksOnTab(msg.tabId, false);
      processPages(links);
      reply({ count: links.length });
    } else if (msg.type === "captureAll") {
      await chrome.storage.local.set({ captureAll: msg.value });
      reply({});
    } else if (msg.type === "clear") {
      await chrome.storage.local.set({ pending: [] });
      reply({});
    }
  })();
  return true;
});

// Servis yeniden başlarsa yarım kalan kuyruğa devam et
chrome.runtime.onStartup.addListener(async () => {
  const { pending = [] } = await chrome.storage.local.get("pending");
  if (pending.length) processPages([]);
});
