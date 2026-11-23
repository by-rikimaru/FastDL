const st = document.getElementById("st");
const send = (m) => chrome.runtime.sendMessage(m);

async function refresh() {
  const s = await send({ type: "status" });
  st.innerHTML = (s.alive ? '<span class="ok">● FastDL açık</span>' : '<span class="bad">● FastDL kapalı</span>')
    + (s.pending ? ` — kuyrukta ${s.pending} sayfa` : "");
  document.getElementById("cap").checked = s.captureAll;
}

document.getElementById("all").onclick = async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const r = await send({ type: "sendAll", tabId: tab.id });
  st.textContent = r.count ? `${r.count} link FastDL'e gönderildi.` : (r.error || "Bu sayfada fuckingfast linki yok.");
};
document.getElementById("clear").onclick = async () => { await send({ type: "clear" }); refresh(); };
document.getElementById("cap").onchange = (e) => send({ type: "captureAll", value: e.target.checked });
refresh();
