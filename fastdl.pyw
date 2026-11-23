# -*- coding: utf-8 -*-
"""
FastDL - IDM benzeri, çok bağlantılı ve devam ettirilebilir indirme yöneticisi.
Tarayıcı eklentisinden (extension/) gelen linkleri 127.0.0.1:9614 üzerinden alır.
Bitince RAR/7z/ZIP arşivlerini (şifreli olanlar dahil) WinRAR/7-Zip ile çıkartır.
Sadece Python standart kütüphanesi kullanılır.
"""
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from tkinter import filedialog, font as tkfont, messagebox, ttk

VERSION = "1.0.0"
REPO = "by-rikimaru/FastDL"

APP_DIR = os.path.dirname(os.path.abspath(__file__))
# Kullanıcı verileri program klasöründe değil, %APPDATA%\FastDL içinde durur;
# böylece kurulum/güncelleme listeye dokunmaz.
DATA_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "FastDL")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
JOBS_PATH = os.path.join(DATA_DIR, "jobs.json")


def migrate_data():
    """Eski sürümler verileri program klasöründe tutuyordu; bir kereliğine taşı."""
    os.makedirs(DATA_DIR, exist_ok=True)
    for name in ("config.json", "jobs.json"):
        old, new = os.path.join(APP_DIR, name), os.path.join(DATA_DIR, name)
        if os.path.exists(old) and not os.path.exists(new):
            try:
                shutil.move(old, new)
            except OSError:
                pass
PORT = 9614
CHUNK = 256 * 1024
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# Durumlar
QUEUED, WAITING, CONNECTING, DOWNLOADING = "Sırada", "Bekliyor", "Bağlanıyor", "İndiriliyor"
PAUSED, STOPPED, DONE, ERROR = "Duraklatıldı", "Durduruldu", "Tamamlandı", "Hata"
STARTABLE = (QUEUED, WAITING, PAUSED, STOPPED, ERROR)

# Tüm indirmelerin okuduğu canlı ayarlar (hız sınırı, bayt/sn; 0 = kapalı)
LIVE = {"limit": 0}


def human(n):
    if n is None or n < 0:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024


def human_time(s):
    if s is None or s == float("inf"):
        return ""
    s = int(s)
    h, r = divmod(s, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def parse_version(s):
    nums = [int(x) for x in re.findall(r"\d+", s or "")[:3]]
    return tuple(nums + [0] * (3 - len(nums)))


def safe_name(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name or "dosya"


def name_from_cd(cd):
    if not cd:
        return None
    m = re.search(r"filename\*\s*=\s*[^']*''([^;]+)", cd, re.I)
    if m:
        return urllib.parse.unquote(m.group(1).strip('" '))
    m = re.search(r'filename\s*=\s*"?([^";]+)"?', cd, re.I)
    return m.group(1).strip() if m else None


def preallocate(path, size):
    """Dosyayı sıfır yazmadan istenen boyutta oluşturur (seyrek dosya).
    Windows'ta f.truncate() boyut kadar sıfır yazar; 2 GB için dakikalar sürer."""
    with open(path, "wb") as f:
        if os.name == "nt":
            try:
                import ctypes
                import msvcrt
                from ctypes import wintypes
                k32 = ctypes.WinDLL("kernel32", use_last_error=True)
                k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID,
                                                wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                                ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
                ret = wintypes.DWORD()
                k32.DeviceIoControl(msvcrt.get_osfhandle(f.fileno()), 0x900C4,  # FSCTL_SET_SPARSE
                                    None, 0, None, 0, ctypes.byref(ret), None)
            except Exception:  # noqa
                pass
        if size:
            f.seek(size - 1)
            f.write(b"\0")


# --------------------------------------------------------------------------- #
#  Arşiv yardımcıları
# --------------------------------------------------------------------------- #
def archive_info(filename):
    """(temel_ad, parça_no, tür) ya da None. tür: 'rar' veya '7z' (7z/zip)."""
    if not filename:
        return None
    m = re.match(r"^(.+?)\.part0*(\d+)\.rar$", filename, re.I)
    if m:
        return m.group(1), int(m.group(2)), "rar"
    m = re.match(r"^(.+)\.rar$", filename, re.I)
    if m:
        return m.group(1), 1, "rar"
    m = re.match(r"^(.+)\.(?:7z|zip)\.0*(\d+)$", filename, re.I)
    if m:
        return m.group(1), int(m.group(2)), "7z"
    m = re.match(r"^(.+)\.(?:7z|zip)$", filename, re.I)
    if m:
        return m.group(1), 1, "7z"
    return None


def find_tool(kind):
    pf = [os.environ.get("ProgramFiles", r"C:\Program Files"),
          os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
          os.environ.get("ProgramW6432", r"C:\Program Files")]
    names = [("WinRAR", "UnRAR.exe"), ("7-Zip", "7z.exe")] if kind == "rar" else [("7-Zip", "7z.exe")]
    for folder, exe in names:
        for base in pf:
            p = os.path.join(base, folder, exe)
            if os.path.isfile(p):
                return p
    for exe in ("UnRAR.exe", "7z.exe") if kind == "rar" else ("7z.exe",):
        p = shutil.which(exe)
        if p:
            return p
    return None


class CloudflareBlocked(Exception):
    pass


# --------------------------------------------------------------------------- #
#  İndirme işi
# --------------------------------------------------------------------------- #
class Job:
    def __init__(self, url, folder, filename=None, headers=None, connections=8, jid=None):
        self.id = jid or uuid.uuid4().hex
        self.url = url
        self.folder = folder
        self.filename = filename
        self.headers = headers or {}
        self.connections = connections
        self.size = None
        self.resumable = False
        self.segments = []          # [start, end, done]
        self.status = QUEUED
        self.in_queue = True        # kuyruk tarafından başlatıldı/başlatılacak
        self.error = ""
        self.extract = ""           # "" bekliyor, "-" otomatik çıkartma yok, ya da çıkartma durumu
        self.speed = 0.0
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._bucket = None
        self._pause_status = PAUSED
        self.thread = None

    # ---- kalıcılık ----
    FIELDS = ("id", "url", "folder", "filename", "headers", "connections", "size",
              "resumable", "segments", "status", "in_queue", "error", "extract")

    def to_dict(self):
        d = {k: getattr(self, k) for k in self.FIELDS}
        if self.active:  # kapanışta inmekte olanlar açılışta devam etsin
            d["status"], d["in_queue"] = QUEUED, True
        if d["extract"].startswith("Çıkartılıyor"):
            d["extract"] = ""
        return d

    @classmethod
    def from_dict(cls, d, legacy=False):
        j = cls(d["url"], d["folder"], d.get("filename"), d.get("headers"),
                d.get("connections", 8), d["id"])
        for k in cls.FIELDS[5:]:
            if k in d:
                setattr(j, k, d[k])
        if "in_queue" not in d:
            j.in_queue = j.status in (QUEUED, WAITING, CONNECTING, DOWNLOADING)
        if j.status in (CONNECTING, DOWNLOADING):
            j.status, j.in_queue = QUEUED, True
        if legacy:  # eski sürüm: "Bekliyor" = kuyrukta; eski bitenleri otomatik çıkartma
            if j.status == WAITING:
                j.status, j.in_queue = QUEUED, True
            j.extract = "-" if j.status == DONE else ""
        return j

    @property
    def path(self):
        return os.path.join(self.folder, self.filename or "dosya")

    @property
    def part_path(self):
        return self.path + ".fdlpart"

    @property
    def downloaded(self):
        if self.status == DONE and self.size:
            return self.size
        return sum(s[2] for s in self.segments)

    @property
    def active(self):
        return self.thread is not None and self.thread.is_alive()

    # ---- ağ ----
    def _open(self, extra=None, timeout=30):
        h = {"User-Agent": DEFAULT_UA, "Accept": "*/*"}
        h.update(self.headers)
        h.update(extra or {})
        try:
            return urllib.request.urlopen(urllib.request.Request(self.url, headers=h), timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code in (403, 503) and (e.headers.get("cf-mitigated") or
                                         "cloudflare" in (e.headers.get("Server") or "").lower()):
                raise CloudflareBlocked() from e
            raise

    def probe(self):
        with self._open({"Range": "bytes=0-0"}) as r:
            if "text/html" in r.headers.get("Content-Type", "").lower():
                raise RuntimeError("Dosya yerine HTML sayfası döndü (link süresi dolmuş olabilir)")
            cd_name = name_from_cd(r.headers.get("Content-Disposition"))
            if r.status == 206:
                m = re.search(r"/(\d+)", r.headers.get("Content-Range", ""))
                self.size = int(m.group(1)) if m else None
                self.resumable = self.size is not None
            else:
                cl = r.headers.get("Content-Length")
                self.size = int(cl) if cl else None
                self.resumable = False
            final = r.geturl()
        if not self.filename:
            name = cd_name or urllib.parse.unquote(
                os.path.basename(urllib.parse.urlparse(final).path)) or "dosya"
            self.filename = safe_name(name)
            base, ext = os.path.splitext(self.filename)
            i = 1
            while os.path.exists(self.path) or os.path.exists(self.part_path):
                self.filename = f"{base} ({i}){ext}"
                i += 1

    # ---- kontrol ----
    def start(self, on_change, by_queue=False):
        if self.active or self.status == DONE:
            return
        self.in_queue = by_queue
        self._stop.clear()
        self.error = ""
        self.thread = threading.Thread(target=self._run, args=(on_change,), daemon=True)
        self.thread.start()

    def pause(self, status=PAUSED):
        """status: PAUSED / STOPPED (kullanıcı) ya da QUEUED (kuyruk durduruldu)."""
        if self.status == DONE:
            return
        if status != QUEUED:
            self.in_queue = False
        if self.active:
            self._pause_status = status
            self._stop.set()
        elif status == QUEUED:
            pass
        elif status == PAUSED and self.status in (QUEUED, WAITING):
            self.status = PAUSED
        elif status == STOPPED and self.status in (QUEUED, WAITING, PAUSED, ERROR):
            self.status = STOPPED

    def reset(self):
        for p in (self.part_path, self.path):
            if self.filename and os.path.exists(p):
                os.remove(p)
        self.segments, self.size, self.error, self.extract = [], None, "", ""
        self.status, self.in_queue = QUEUED, True

    # ---- hız sınırı (dosya başına, tüm bağlantılar ortak) ----
    def _throttle(self, n):
        rate = LIVE["limit"]
        if not rate:
            self._bucket = None
            return
        now = time.monotonic()
        with self._lock:
            last, tokens = self._bucket or (now, 0.0)
            tokens = min(rate * 0.5, tokens + (now - last) * rate) - n
            self._bucket = (now, tokens)
        if tokens < 0:
            self._stop.wait(-tokens / rate)

    # ---- ana döngü ----
    def _run(self, on_change):
        try:
            self.status = CONNECTING
            if not self.segments or not os.path.exists(self.part_path):
                self.probe()
                os.makedirs(self.folder, exist_ok=True)
                preallocate(self.part_path, self.size)
                if self.resumable and self.size:
                    n = max(1, min(self.connections, self.size // (1024 * 1024) or 1))
                    step = self.size // n
                    self.segments = [[i * step, (self.size - 1) if i == n - 1 else (i + 1) * step - 1, 0]
                                     for i in range(n)]
                else:
                    self.segments = [[0, (self.size - 1) if self.size else -1, 0]]
            elif not self.resumable:
                self.segments = [[0, (self.size - 1) if self.size else -1, 0]]
            on_change()

            self.status = DOWNLOADING
            workers = [threading.Thread(target=self._worker, args=(seg,), daemon=True)
                       for seg in self.segments if seg[1] < 0 or seg[2] < seg[1] - seg[0] + 1]
            for w in workers:
                w.start()
            last_b, last_t = self.downloaded, time.time()
            while any(w.is_alive() for w in workers):
                time.sleep(0.5)
                now, b = time.time(), self.downloaded
                inst = (b - last_b) / max(now - last_t, 1e-6)
                self.speed = inst if self.speed == 0 else self.speed * 0.7 + inst * 0.3
                last_b, last_t = b, now
                on_change(save=False)
            self.speed = 0

            if self.error:
                self.status = ERROR
            elif self._stop.is_set():
                self.status = self._pause_status
            else:
                if self.size and self.downloaded < self.size:
                    raise RuntimeError("Eksik indirildi")
                os.replace(self.part_path, self.path)
                self.status = DONE
                self.in_queue = False
        except CloudflareBlocked:
            self.status = ERROR
            self.error = "Cloudflare doğrulaması istendi. Sayfayı eklentiyle yeniden gönderin."
        except Exception as e:  # noqa
            self.status = ERROR
            self.error = str(e) or e.__class__.__name__
        self.speed = 0
        on_change()

    def _worker(self, seg):
        retries = 0
        while not self._stop.is_set():
            start, end, done = seg
            if end >= 0 and done >= end - start + 1:
                return
            extra = {"Range": f"bytes={start + done}-{end}"} if self.resumable else {}
            try:
                r = self._open(extra, timeout=30)
                with r, open(self.part_path, "r+b") as f:
                    if self.resumable and r.status != 206:
                        raise RuntimeError("Sunucu Range isteğini reddetti")
                    if not self.resumable:
                        seg[2] = 0
                        f.truncate(0)
                    f.seek(start + seg[2])
                    while not self._stop.is_set():
                        rate = LIVE["limit"]
                        size = min(CHUNK, max(2048, rate // 8)) if rate else CHUNK
                        limit = size if end < 0 else min(size, end - start + 1 - seg[2])
                        if limit <= 0:
                            break
                        data = r.read(limit)
                        if not data:
                            break
                        f.write(data)
                        with self._lock:
                            seg[2] += len(data)
                        retries = 0
                        self._throttle(len(data))
                if end < 0 and not self._stop.is_set():
                    self.size = seg[2]
                    return
            except CloudflareBlocked:
                self.error = "Cloudflare doğrulaması istendi (link süresi dolmuş olabilir)."
                self._stop.set()
                return
            except Exception as e:  # noqa
                retries += 1
                if retries > 15:
                    self.error = f"Bağlantı hatası: {e}"
                    self._stop.set()
                    return
                self._stop.wait(min(2 * retries, 20))


# --------------------------------------------------------------------------- #
#  Arşiv çıkartıcı (tek tek, arka planda)
# --------------------------------------------------------------------------- #
class Extractor:
    def __init__(self, app):
        self.app = app
        self.q = queue.Queue()
        self.busy_keys = set()
        threading.Thread(target=self._loop, daemon=True).start()

    def submit(self, key, jobs):
        if key in self.busy_keys:
            return
        self.busy_keys.add(key)
        for j in jobs:
            j.extract = "Çıkartma sırada"
        self.q.put((key, jobs))

    def _loop(self):
        while True:
            key, jobs = self.q.get()
            try:
                self._extract(key, jobs)
            except Exception as e:  # noqa
                self._set(jobs, f"Çıkartma hatası: {e}")
            self.busy_keys.discard(key)
            self.app.changed()

    @staticmethod
    def _set(jobs, text):
        for j in jobs:
            j.extract = text

    def _run_tool(self, kind, tool, first, dest, pw, jobs):
        if kind == "rar" and os.path.basename(tool).lower() == "unrar.exe":
            cmd = [tool, "x", "-y", "-o+", f"-p{pw}" if pw else "-p-", first, dest + os.sep]
        else:
            cmd = [tool, "x", "-y", "-bsp1", f"-o{dest}", f"-p{pw or ''}", first]
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
        out = b""
        while True:
            chunk = p.stdout.read(512)
            if not chunk:
                break
            out = (out + chunk)[-8192:]
            pcts = re.findall(rb"(\d{1,3})%", chunk)
            if pcts:
                self._set(jobs, f"Çıkartılıyor %{int(pcts[-1])}")
        rc = p.wait()
        text = out.decode("cp857", "ignore")
        wrong_pw = rc == 11 or bool(re.search(r"wrong password|incorrect password|parola|şifre|encrypted",
                                              text, re.I))
        return rc, wrong_pw, text

    def _extract(self, key, jobs):
        folder, base, kind = key
        first = min(jobs, key=lambda j: archive_info(j.filename)[1])
        tool = find_tool(kind)
        if not tool:
            self._set(jobs, "Çıkartma hatası: WinRAR/7-Zip bulunamadı")
            return
        dest = os.path.join(folder, safe_name(base))
        os.makedirs(dest, exist_ok=True)
        self._set(jobs, "Çıkartılıyor %0")

        tried = [None] + [p for p in self.app.cfg.get("passwords", []) if p]
        asked_wrong = False
        while True:
            if tried:
                pw, from_user, save = tried.pop(0), False, False
            else:
                ans = self.app.ask_password(base, asked_wrong)
                if ans is None:
                    self._set(jobs, "Çıkartma iptal edildi (şifre girilmedi)")
                    return
                pw, save = ans
                from_user = True
            rc, wrong_pw, text = self._run_tool(kind, tool, first.path, dest, pw, jobs)
            if rc == 0:
                break
            if wrong_pw:
                asked_wrong = from_user
                continue
            msg = "eksik parça var" if re.search(r"cildi|volume", text, re.I) else f"kod {rc}"
            self._set(jobs, f"Çıkartma hatası: {msg}")
            return

        if pw and from_user and save:
            self.app.remember_password(pw)
        text = "Çıkartıldı" + (" (şifreli)" if pw else "")
        if self.app.cfg.get("delete_archives"):
            for j in jobs:
                try:
                    os.remove(j.path)
                except OSError:
                    pass
            text += ", arşiv silindi"
        self._set(jobs, text)


# --------------------------------------------------------------------------- #
#  Arayüz
# --------------------------------------------------------------------------- #
ICON_FONTS = ("Segoe Fluent Icons", "Segoe MDL2 Assets")
ICONS = {  # Segoe MDL2/Fluent glifleri; font yoksa yedek karakter
    "add": ("\uE710", "+"), "play": ("\uE768", "▶"), "pause": ("\uE769", "⏸"),
    "stop": ("\uE71A", "■"), "delete": ("\uE74D", "✕"), "clear": ("\uE75C", "⌫"),
    "settings": ("\uE713", "⚙"), "qstart": ("\uE896", "⇩"), "qstop": ("\uE8D8", "⏹"),
    "speed": ("\uEC4A", "⏲"), "archive": ("\uF012", "▤"), "folder": ("\uE838", "📂"),
}
BLUE = "#1a73e8"


class ToolButton(tk.Frame):
    """IDM tarzı: üstte simge, altta yazı; üzerine gelince vurgulanır."""

    def __init__(self, master, icon, text, command, icon_font, color=BLUE):
        super().__init__(master, bg="white", padx=8, pady=3, cursor="hand2")
        self.command, self.color, self.enabled = command, color, True
        glyph = ICONS[icon][0] if icon_font else ICONS[icon][1]
        self.i = tk.Label(self, text=glyph, bg="white", fg=color,
                          font=(icon_font or "Segoe UI Symbol", 18))
        self.t = tk.Label(self, text=text, bg="white", fg="#222", font=("Segoe UI", 8))
        self.i.pack()
        self.t.pack()
        for w in (self, self.i, self.t):
            w.bind("<Enter>", lambda e: self._bg("#e8f0fe"))
            w.bind("<Leave>", lambda e: self._bg("white"))
            w.bind("<Button-1>", lambda e: self.enabled and self.command())

    def _bg(self, c):
        if not self.enabled:
            c = "white"
        for w in (self, self.i, self.t):
            w.config(bg=c)

    def set_enabled(self, on):
        if on != self.enabled:
            self.enabled = on
            self.i.config(fg=self.color if on else "#b8b8b8")
            self.t.config(fg="#222" if on else "#a0a0a0")
            self.config(cursor="hand2" if on else "arrow")


class App:
    COLS = (("name", "Dosya Adı", 310, "w"), ("size", "Boyut", 70, "e"),
            ("prog", "İlerleme", 178, "w"), ("status", "Durum", 190, "w"),
            ("speed", "Aktarım hızı", 90, "e"), ("eta", "Kalan süre", 75, "e"))
    CATS = (("all", "Tüm İndirmeler"), ("unfinished", "Tamamlanmayan"), ("finished", "Tamamlanan"),
            ("queue", "Sıradakiler"), ("errors", "Hatalı"))
    DEFAULTS = {"folder": os.path.join(os.path.expanduser("~"), "Downloads", "FastDL"),
                "connections": 8, "parallel": 3, "auto_queue": True, "queue_running": True,
                "auto_extract": True, "delete_archives": False, "passwords": [],
                "speed_kb": 1024, "speed_on_startup": False, "dblclick": "props",
                "show_categories": True, "check_updates": True, "skip_version": ""}

    def __init__(self, root):
        self.root = root
        self.cfg = dict(self.DEFAULTS)
        self.cfg.update(self._load(CONFIG_PATH, {}))
        data = self._load(JOBS_PATH, [])
        legacy = isinstance(data, list)
        self.jobs = [Job.from_dict(d, legacy) for d in (data if legacy else data.get("jobs", []))]
        self.inbox = queue.Queue()
        self.ui_calls = queue.Queue()
        self._dirty = False
        self.filter = "all"
        self.extractor = Extractor(self)

        self.speed_on = tk.BooleanVar(value=self.cfg["speed_on_startup"])
        self.extract_var = tk.BooleanVar(value=self.cfg["auto_extract"])
        self.queue_var = tk.BooleanVar(value=self.cfg["queue_running"])
        self.dbl_var = tk.StringVar(value=self.cfg["dblclick"])
        self.cat_var = tk.BooleanVar(value=self.cfg["show_categories"])
        self.apply_speed()

        families = set(tkfont.families(root))
        self.icon_font = next((f for f in ICON_FONTS if f in families), None)

        root.title("FastDL İndirme Yöneticisi")
        root.geometry("1180x620")
        root.minsize(820, 400)
        root.configure(bg="white")
        self._style()
        self._build_menu()
        self._build()
        self.refresh()
        root.after(400, self.tick)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        if self.cfg["check_updates"]:
            root.after(3000, lambda: self.check_updates(silent=True))

    # ---------------- kalıcılık ----------------
    @staticmethod
    def _load(path, default):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa
            return default

    def save(self):
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        self.cfg.update(auto_extract=self.extract_var.get(), queue_running=self.queue_var.get(),
                        dblclick=self.dbl_var.get(), show_categories=self.cat_var.get())
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, ensure_ascii=False, indent=1)
            tmp = JOBS_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"version": 2, "jobs": [j.to_dict() for j in self.jobs]}, f, ensure_ascii=False)
            os.replace(tmp, JOBS_PATH)
        except Exception:  # noqa
            pass

    def changed(self, save=True):
        if save:
            self._dirty = True

    # ---------------- görünüm ----------------
    def _style(self):
        st = ttk.Style()
        try:
            st.theme_use("vista")
        except tk.TclError:
            pass
        st.configure("Treeview", rowheight=22, font=("Segoe UI", 9))
        st.configure("Treeview.Heading", font=("Segoe UI", 9))
        st.configure("Cat.Treeview", rowheight=24, font=("Segoe UI", 9))
        st.configure("Status.TLabel", font=("Segoe UI", 9))

    def _build_menu(self):
        mb = tk.Menu(self.root)
        self.root.config(menu=mb)

        m = tk.Menu(mb, tearoff=0)
        m.add_command(label="URL Ekle…", accelerator="Ctrl+N", command=self.add_dialog)
        m.add_command(label="Panodan Ekle", command=self.add_from_clipboard)
        m.add_separator()
        m.add_command(label="Kayıt klasörünü aç", command=lambda: self.open_folder(force_default=True))
        m.add_separator()
        m.add_command(label="Çıkış", command=self.on_close)
        mb.add_cascade(label="Görevler", menu=m)

        self.file_menu = m = tk.Menu(mb, tearoff=0, postcommand=self._update_file_menu)
        m.add_command(label="Dosyayı aç", command=self.open_file)
        m.add_command(label="Birlikte aç…", command=self.open_with)
        m.add_command(label="Klasörü aç", command=self.open_folder)
        m.add_separator()
        m.add_command(label="Taşı/Yeniden adlandır", accelerator="Ctrl+M", command=self.move_rename)
        m.add_command(label="Arşivden çıkart", command=self.extract_selected)
        m.add_separator()
        m.add_command(label="Kaldır", accelerator="Del", command=self.remove_sel)
        m.add_command(label="Özellikler", command=self.properties)
        mb.add_cascade(label="Dosya", menu=m)

        self.dl_menu = m = tk.Menu(mb, tearoff=0, postcommand=self._update_dl_menu)
        m.add_command(label="Başlat / Devam et", command=self.start_sel)
        m.add_command(label="Duraklat", command=self.pause_sel)
        m.add_command(label="Durdur", command=self.stop_sel)
        m.add_separator()
        m.add_command(label="Tümünü Başlat", command=self.start_all)
        m.add_command(label="Tümünü Duraklat", command=self.pause_all)
        m.add_command(label="Tümünü Durdur", command=self.stop_all)
        m.add_separator()
        m.add_command(label="Sıraya Al", command=self.queue_sel)
        m.add_command(label="Sıradan Çıkar", command=self.unqueue_sel)
        m.add_command(label="Tümünü Sıraya Al", command=self.queue_all)
        m.add_checkbutton(label="Kuyruk çalışıyor (Kuyruğu Başlat/Durdur)", variable=self.queue_var,
                          command=self.queue_toggled)
        m.add_separator()
        m.add_command(label="Tekrar indir", command=self.redownload)
        m.add_command(label="İndirme adresini yenile", command=self.refresh_url)
        m.add_command(label="Tamamlananların Tümünü Sil", command=self.clear_done)
        m.add_separator()
        sp = tk.Menu(m, tearoff=0)
        sp.add_radiobutton(label="Açık", variable=self.speed_on, value=True, command=self.apply_speed)
        sp.add_radiobutton(label="Kapalı", variable=self.speed_on, value=False, command=self.apply_speed)
        sp.add_separator()
        sp.add_command(label="Ayarlar…", command=self.speed_dialog)
        m.add_cascade(label="Hız Sınırlayıcı", menu=sp)
        m.add_checkbutton(label="İndirmeler bitince arşivden çıkart", variable=self.extract_var,
                          command=self.changed)
        m.add_separator()
        m.add_command(label="Seçenekler…", command=self.options_dialog)
        mb.add_cascade(label="İndirmeler", menu=m)

        m = tk.Menu(mb, tearoff=0)
        m.add_checkbutton(label="Kategoriler paneli", variable=self.cat_var, command=self._toggle_cats)
        dbl = tk.Menu(m, tearoff=0)
        for val, lab in (("open", "Dosyayı aç"), ("folder", "Klasörü aç"), ("props", "Özellikleri göster")):
            dbl.add_radiobutton(label=lab, value=val, variable=self.dbl_var, command=self.changed)
        m.add_cascade(label="Çift tıklandığında", menu=dbl)
        mb.add_cascade(label="Görünüm", menu=m)

        m = tk.Menu(mb, tearoff=0)
        m.add_command(label="Nasıl kullanılır?", command=self.help_dialog)
        m.add_command(label="Güncellemeleri denetle", command=lambda: self.check_updates(silent=False))
        m.add_command(label="GitHub sayfası", command=lambda: webbrowser.open(f"https://github.com/{REPO}"))
        m.add_separator()
        m.add_command(label="Hakkında", command=lambda: messagebox.showinfo(
            "FastDL", f"FastDL İndirme Yöneticisi v{VERSION}\nÇok bağlantılı, devam ettirilebilir indirme,\n"
                      f"hız sınırlayıcı ve otomatik arşiv çıkartma.\n\nAçık kaynak (MIT): github.com/{REPO}"))
        mb.add_cascade(label="Yardım", menu=m)

    def _build(self):
        bar = tk.Frame(self.root, bg="white")
        bar.pack(fill="x")
        f = self.icon_font
        self.btn = {}
        groups = ((("add", "URL Ekle", self.add_dialog),),
                  (("play", "Devam Et", self.start_sel), ("pause", "Duraklat", self.pause_sel),
                   ("stop", "Durdur", self.stop_sel)),
                  (("delete", "Sil", self.remove_sel), ("clear", "Tam. Sil", self.clear_done)),
                  (("qstart", "K. Başlat", self.start_queue), ("qstop", "K. Durdur", self.stop_queue)),
                  (("speed", "Hız Sınırı", self.toggle_speed), ("archive", "Çıkart", self.extract_selected),
                   ("settings", "Seçenekler", self.options_dialog)))
        for gi, group in enumerate(groups):
            if gi:
                tk.Frame(bar, bg="#e3e3e3", width=1).pack(side="left", fill="y", pady=8, padx=4)
            for icon, text, cmd in group:
                b = ToolButton(bar, icon, text, cmd, f)
                b.pack(side="left", pady=4)
                self.btn[icon] = b
        tk.Frame(self.root, bg="#d9d9d9", height=1).pack(fill="x")

        self.status_bar = tk.Frame(self.root, bg="#f3f3f3")
        self.status_bar.pack(fill="x", side="bottom")
        self.status_lbl = tk.Label(self.status_bar, bg="#f3f3f3", anchor="w", font=("Segoe UI", 9))
        self.status_lbl.pack(side="left", padx=8, pady=3)
        ttk.Checkbutton(self.status_bar, text="İndirmeler bitince arşivden çıkart",
                        variable=self.extract_var, command=self.changed).pack(side="right", padx=8)
        self.speed_lbl = tk.Label(self.status_bar, bg="#f3f3f3", font=("Segoe UI", 9), cursor="hand2")
        self.speed_lbl.pack(side="right", padx=12)
        self.speed_lbl.bind("<Button-1>", lambda e: self.speed_dialog())

        self.paned = ttk.PanedWindow(self.root, orient="horizontal")
        self.paned.pack(fill="both", expand=True)

        self.cat_frame = ttk.Frame(self.paned)
        ttk.Label(self.cat_frame, text=" Kategoriler", font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(4, 2))
        self.cats = ttk.Treeview(self.cat_frame, show="tree", style="Cat.Treeview", selectmode="browse")
        for key, label in self.CATS:
            self.cats.insert("", "end", iid=key, text=label)
        self.cats.selection_set("all")
        self.cats.pack(fill="both", expand=True)
        self.cats.bind("<<TreeviewSelect>>", self._cat_changed)

        main = ttk.Frame(self.paned)
        self.tree = ttk.Treeview(main, columns=[c[0] for c in self.COLS], show="headings")
        for key, title, w, anchor in self.COLS:
            self.tree.heading(key, text=title, anchor="w")
            self.tree.column(key, width=w, anchor=anchor, stretch=key == "name")
        sb = ttk.Scrollbar(main, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.tag_configure("done", foreground="#188038")
        self.tree.tag_configure("error", foreground="#c5221f")
        self.tree.tag_configure("active", foreground=BLUE)
        self.tree.tag_configure("idle", foreground="#5f6368")

        if self.cat_var.get():
            self.paned.add(self.cat_frame, weight=0)
        self.paned.add(main, weight=1)

        self.tree.bind("<Double-1>", self.on_double)
        self.tree.bind("<Delete>", lambda e: self.remove_sel())
        self.tree.bind("<Button-3>", self.context_menu)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._update_buttons())
        for seq, fn in (("<Control-a>", self.select_all), ("<Control-A>", self.select_all),
                        ("<Control-m>", lambda e: self.move_rename()), ("<Control-M>", lambda e: self.move_rename()),
                        ("<Control-n>", lambda e: self.add_dialog()), ("<Control-N>", lambda e: self.add_dialog())):
            self.root.bind_all(seq, fn)

    def _toggle_cats(self):
        panes = self.paned.panes()
        if self.cat_var.get() and str(self.cat_frame) not in map(str, panes):
            self.paned.insert(0, self.cat_frame, weight=0)
        elif not self.cat_var.get() and str(self.cat_frame) in map(str, panes):
            self.paned.forget(self.cat_frame)
        self.changed()

    def _cat_changed(self, _e=None):
        sel = self.cats.selection()
        self.filter = sel[0] if sel else "all"
        self.refresh()

    def _match(self, j):
        f = self.filter
        return (f == "all" or (f == "finished" and j.status == DONE)
                or (f == "unfinished" and j.status != DONE)
                or (f == "queue" and (j.status == QUEUED or (j.active and j.in_queue)))
                or (f == "errors" and (j.status == ERROR or "hata" in j.extract.lower())))

    # ---------------- yardımcılar ----------------
    def selected(self):
        ids = set(self.tree.selection())
        return [j for j in self.jobs if j.id in ids]

    def add_job(self, url, filename=None, headers=None):
        url = url.strip()
        if not re.match(r"https?://", url):
            return False
        if any(j.url == url and j.status != ERROR for j in self.jobs):
            return False
        if not filename:
            frag = urllib.parse.unquote(urllib.parse.urlparse(url).fragment)
            filename = frag if re.search(r"\.\w{2,5}$", frag) else None
        if filename:
            # Aynı dosya yarım duruyorsa yeni kayıt açma, adresini yenile (süresi dolan linkler için)
            for j in self.jobs:
                if j.filename == safe_name(filename) and j.status != DONE:
                    j.url, j.error = url, ""
                    if headers:
                        j.headers = headers
                    if j.status == ERROR:
                        j.status, j.in_queue = QUEUED, True
                    self._dirty = True
                    return True
        job = Job(url, self.cfg["folder"], safe_name(filename) if filename else None,
                  headers or {}, self.cfg["connections"])
        if not self.cfg["auto_queue"]:
            job.status, job.in_queue = WAITING, False
        self.jobs.append(job)
        # Aynı arşiv setine yeni parça geldiyse "eksik parça" hatasını sıfırla
        info = archive_info(job.filename)
        if info:
            for j in self._set_members(job.folder, info[0], info[2]):
                if j.extract.startswith("Çıkartma hatası"):
                    j.extract = ""
        self._dirty = True
        return True

    def _set_members(self, folder, base, kind):
        out = []
        for j in self.jobs:
            info = archive_info(j.filename)
            if info and j.folder == folder and info[0] == base and info[2] == kind:
                out.append(j)
        return out

    # ---------------- komutlar: indirme ----------------
    def _start_now(self, jobs):
        for j in jobs:
            if not j.active and j.status in STARTABLE:
                j.start(self.changed, by_queue=False)
        self._after_cmd()

    def _queue(self, jobs):
        for j in jobs:
            if not j.active and j.status in STARTABLE:
                j.status, j.in_queue, j.error = QUEUED, True, ""
        self._after_cmd()

    def _pause(self, jobs, status):
        for j in jobs:
            j.pause(status)
        self._after_cmd()

    def _after_cmd(self):
        self._dirty = True
        self.refresh()

    def start_sel(self):
        self._start_now(self.selected())

    def pause_sel(self):
        self._pause(self.selected(), PAUSED)

    def stop_sel(self):
        self._pause(self.selected(), STOPPED)

    def queue_sel(self):
        self._queue(self.selected())

    def unqueue_sel(self):
        for j in self.selected():
            if not j.active and j.status == QUEUED:
                j.status, j.in_queue = WAITING, False
        self._after_cmd()

    def start_all(self):
        """Tamamlanmayanların hepsini sıraya alır ve kuyruğu başlatır
        ('Aynı anda' sınırına uyar)."""
        self._queue([j for j in self.jobs if j.status != DONE])
        self.start_queue()

    def pause_all(self):
        self._pause([j for j in self.jobs if j.active or j.status == QUEUED], PAUSED)

    def stop_all(self):
        self._pause([j for j in self.jobs if j.status != DONE], STOPPED)

    def queue_all(self):
        self._queue([j for j in self.jobs if j.status != DONE])

    def start_queue(self):
        self.queue_var.set(True)
        self.queue_toggled()

    def stop_queue(self):
        self.queue_var.set(False)
        self.queue_toggled()

    def queue_toggled(self):
        if not self.queue_var.get():
            for j in self.jobs:
                if j.active and j.in_queue:
                    j.pause(QUEUED)  # kuyrukta kalır, kuyruk başlayınca devam eder
        self._after_cmd()

    def redownload(self):
        sel = [j for j in self.selected() if not j.active]
        if not sel or not messagebox.askyesno("Tekrar indir",
                                              f"{len(sel)} dosya silinip baştan indirilecek. Emin misiniz?"):
            return
        for j in sel:
            try:
                j.reset()
            except OSError as e:
                j.status, j.error = ERROR, str(e)
        self._after_cmd()

    def clear_done(self):
        self.jobs = [j for j in self.jobs if j.status != DONE or j.extract.startswith("Çıkartılıyor")]
        self._after_cmd()

    def remove_sel(self):
        sel = self.selected()
        if not sel:
            return
        del_files = messagebox.askyesnocancel(
            "Kaldır", f"{len(sel)} öğe listeden kaldırılacak.\n"
                      "Yarım kalan (.fdlpart) dosyalar da diskten silinsin mi?")
        if del_files is None:
            return
        for j in sel:
            j.pause(STOPPED)
            if j.thread:
                j.thread.join(timeout=3)
            if del_files and j.filename and os.path.exists(j.part_path):
                try:
                    os.remove(j.part_path)
                except OSError:
                    pass
            self.jobs.remove(j)
        self._after_cmd()

    # ---------------- hız sınırlayıcı ----------------
    def apply_speed(self):
        LIVE["limit"] = max(1, int(self.cfg["speed_kb"])) * 1024 if self.speed_on.get() else 0
        self._dirty = True

    def toggle_speed(self):
        self.speed_on.set(not self.speed_on.get())
        self.apply_speed()
        self.refresh()

    def speed_dialog(self):
        win = self._dialog("Hız Sınırlayıcı ayarları")
        frm = ttk.Frame(win, padding=14)
        frm.pack(fill="both")
        row = ttk.Frame(frm)
        row.pack(anchor="e", fill="x")
        ttk.Label(row, text="Tek dosya için azami indirme hızı").pack(side="left", padx=(40, 8))
        kb = tk.StringVar(value=str(self.cfg["speed_kb"]))
        ttk.Entry(row, textvariable=kb, width=8, justify="right").pack(side="left")
        ttk.Label(row, text="KBayt/san").pack(side="left", padx=6)
        ttk.Label(frm, wraplength=400, justify="left", padding=(0, 10),
                  text="Not: İnternet sunucuları hız fazla sınırlandırıldığında bağlantıyı kesebilir! "
                       "Bundan dolayı \"Devam etme özelliği\" desteklemeyen sunuculardan indirmeler "
                       "için Hız Sınırlayıcı kullanılması önerilmez.").pack(anchor="w")
        startup = tk.BooleanVar(value=self.cfg["speed_on_startup"])
        ttk.Checkbutton(frm, text="FastDL açıldığında Hız Sınırlayıcıyı aç", variable=startup).pack(anchor="w")

        def ok():
            try:
                v = int(kb.get().strip())
                if v < 1:
                    raise ValueError
            except ValueError:
                messagebox.showerror("Hız Sınırlayıcı", "Geçerli bir sayı girin (KBayt/san).", parent=win)
                return
            self.cfg["speed_kb"], self.cfg["speed_on_startup"] = v, startup.get()
            self.apply_speed()
            win.destroy()
            self.refresh()
        self._ok_cancel(frm, win, ok)

    # ---------------- arşiv ----------------
    def ask_password(self, name, wrong):
        """Çıkartma iş parçacığından çağrılır; diyaloğu ana iş parçacığında açar."""
        box, ev = {}, threading.Event()
        self.ui_calls.put(lambda: self._password_dialog(name, wrong, box, ev))
        ev.wait()
        return box.get("ans")

    def _password_dialog(self, name, wrong, box, ev):
        win = self._dialog("Arşiv şifresi")
        frm = ttk.Frame(win, padding=14)
        frm.pack(fill="both")
        msg = f"\"{name}\" arşivi şifreli."
        if wrong:
            msg = f"Girilen şifre yanlış. \"{name}\" için tekrar deneyin."
        ttk.Label(frm, text=msg, wraplength=380, foreground="#c5221f" if wrong else "").pack(anchor="w")
        ttk.Label(frm, text="Şifre:").pack(anchor="w", pady=(10, 2))
        pw = tk.StringVar()
        ent = ttk.Entry(frm, textvariable=pw, show="•", width=44)
        ent.pack(anchor="w")
        ent.focus_set()
        show = tk.BooleanVar()
        ttk.Checkbutton(frm, text="Şifreyi göster", variable=show,
                        command=lambda: ent.config(show="" if show.get() else "•")).pack(anchor="w", pady=(6, 0))
        save = tk.BooleanVar(value=True)
        ttk.Checkbutton(frm, text="Doğruysa şifre listesine ekle (sonraki arşivlerde otomatik denensin)",
                        variable=save).pack(anchor="w")

        def done(ans):
            box["ans"] = ans
            ev.set()
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", lambda: done(None))
        self._ok_cancel(frm, win, lambda: done((pw.get(), save.get())) if pw.get() else None,
                        cancel=lambda: done(None))
        win.bind("<Return>", lambda e: pw.get() and done((pw.get(), save.get())))
        win.deiconify()
        win.lift()
        win.focus_force()

    def remember_password(self, pw):
        self.ui_calls.put(lambda: (pw not in self.cfg["passwords"]
                                   and self.cfg["passwords"].append(pw), self.changed()))

    def _check_extract(self):
        """Seti tamamen inmiş arşivleri bul; onay kutusu işaretliyse çıkart."""
        if not self.extract_var.get():
            return
        seen = set()
        for j in self.jobs:
            info = archive_info(j.filename)
            if not info or j.status != DONE or j.extract != "":
                continue
            key = (j.folder, info[0], info[2])
            if key in seen or key in self.extractor.busy_keys:
                continue
            seen.add(key)
            members = self._set_members(*key)
            if any(m.status != DONE or m.extract == "-" for m in members):
                continue
            nums = sorted(archive_info(m.filename)[1] for m in members)
            if nums != list(range(1, len(nums) + 1)):
                continue  # ara parçalar listede yok
            self.extractor.submit(key, members)

    def extract_selected(self):
        keys = {}
        for j in self.selected():
            info = archive_info(j.filename)
            if info and j.status == DONE:
                keys[(j.folder, info[0], info[2])] = True
        if not keys:
            messagebox.showinfo("Çıkart", "Seçili tamamlanmış bir arşiv (.rar / .7z / .zip) yok.")
            return
        for key in keys:
            members = [m for m in self._set_members(*key) if m.status == DONE]
            self.extractor.submit(key, members)
        self.refresh()

    # ---------------- seçim / sağ tık ----------------
    def select_all(self, _e=None):
        w = self.root.focus_get()
        if isinstance(w, (tk.Entry, ttk.Entry, tk.Text, ttk.Spinbox)):
            return None
        self.tree.selection_set(self.tree.get_children())
        return "break"

    def on_double(self, event):
        if not self.tree.identify_row(event.y):
            return
        {"open": self.open_file, "folder": self.open_folder}.get(self.dbl_var.get(), self.properties)()

    def _sel_state(self):
        sel = self.selected()
        return dict(
            sel=sel, one=len(sel) == 1,
            done=bool(sel) and all(j.status == DONE for j in sel),
            active=any(j.active for j in sel),
            startable=any(not j.active and j.status in STARTABLE for j in sel),
            queued=any(not j.active and j.status == QUEUED for j in sel),
            archive=any(j.status == DONE and archive_info(j.filename) for j in sel))

    @staticmethod
    def _st(ok):
        return "normal" if ok else "disabled"

    def context_menu(self, event):
        row = self.tree.identify_row(event.y)
        if not row:
            return
        if row not in self.tree.selection():
            self.tree.selection_set(row)
        s, st = self._sel_state(), self._st
        j = s["sel"][0]
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label="Dosyayı aç", command=self.open_file, state=st(s["one"] and s["done"]))
        m.add_command(label="Birlikte aç…", command=self.open_with, state=st(s["one"] and s["done"]))
        m.add_command(label="Klasörü aç", command=self.open_folder)
        m.add_separator()
        m.add_command(label="Taşı/Yeniden adlandır", accelerator="Ctrl+M", command=self.move_rename,
                      state=st(s["one"] and not j.active and bool(j.filename)))
        m.add_command(label="Arşivden çıkart", command=self.extract_selected, state=st(s["archive"]))
        m.add_separator()
        m.add_command(label="Tekrar indir", command=self.redownload, state=st(not s["active"]))
        m.add_separator()
        m.add_command(label="İndirmeye devam et", command=self.start_sel, state=st(s["startable"]))
        m.add_command(label="İndirmeyi duraklat", command=self.pause_sel, state=st(s["active"] or s["queued"]))
        m.add_command(label="İndirmeyi durdur", command=self.stop_sel, state=st(s["active"] or s["queued"]))
        m.add_separator()
        m.add_command(label="İndirme adresini yenile", command=self.refresh_url, state=st(s["one"] and not s["done"]))
        m.add_separator()
        m.add_command(label="Kaldır", accelerator="Del", command=self.remove_sel)
        m.add_separator()
        m.add_command(label="Sıraya al", command=self.queue_sel, state=st(s["startable"]))
        m.add_command(label="Sıradan çıkar", command=self.unqueue_sel, state=st(s["queued"]))
        m.add_separator()
        sub = tk.Menu(m, tearoff=0)
        for val, lab in (("open", "Dosyayı aç"), ("folder", "Klasörü aç"), ("props", "Özellikleri göster")):
            sub.add_radiobutton(label=lab, value=val, variable=self.dbl_var, command=self.changed)
        m.add_cascade(label="Seçilen dosya çift tıklandığında", menu=sub)
        m.add_separator()
        m.add_command(label="Dosyanın özelliklerini görüntüle", command=self.properties, state=st(s["one"]))
        try:
            m.tk_popup(event.x_root, event.y_root)
        finally:
            m.grab_release()

    def _update_file_menu(self):
        s, st, m = self._sel_state(), self._st, self.file_menu
        j = s["sel"][0] if s["sel"] else None
        for label, ok in (("Dosyayı aç", s["one"] and s["done"]), ("Birlikte aç…", s["one"] and s["done"]),
                          ("Klasörü aç", True),
                          ("Taşı/Yeniden adlandır", s["one"] and j and not j.active and bool(j.filename)),
                          ("Arşivden çıkart", s["archive"]), ("Kaldır", bool(s["sel"])), ("Özellikler", s["one"])):
            m.entryconfig(label, state=st(ok))

    def _update_dl_menu(self):
        s, st, m = self._sel_state(), self._st, self.dl_menu
        for label, ok in (("Başlat / Devam et", s["startable"]), ("Duraklat", s["active"] or s["queued"]),
                          ("Durdur", s["active"] or s["queued"]), ("Sıraya Al", s["startable"]),
                          ("Sıradan Çıkar", s["queued"]), ("Tekrar indir", bool(s["sel"]) and not s["active"]),
                          ("İndirme adresini yenile", s["one"] and not s["done"])):
            m.entryconfig(label, state=st(ok))

    def _update_buttons(self):
        s = self._sel_state()
        self.btn["play"].set_enabled(s["startable"])
        self.btn["pause"].set_enabled(s["active"] or s["queued"])
        self.btn["stop"].set_enabled(s["active"] or s["queued"])
        self.btn["delete"].set_enabled(bool(s["sel"]))
        self.btn["clear"].set_enabled(any(j.status == DONE for j in self.jobs))
        self.btn["archive"].set_enabled(s["archive"])
        self.btn["qstart"].set_enabled(not self.queue_var.get())
        self.btn["qstop"].set_enabled(self.queue_var.get())
        on = self.speed_on.get()
        self.btn["speed"].color = "#e37400" if on else BLUE
        self.btn["speed"].i.config(fg=self.btn["speed"].color)
        self.btn["speed"].t.config(text="Hız Sınırı: Açık" if on else "Hız Sınırı")

    # ---------------- dosya komutları ----------------
    def open_file(self):
        sel = self.selected()
        if sel and os.path.exists(sel[0].path):
            os.startfile(sel[0].path)

    def open_with(self):
        sel = self.selected()
        if sel and os.path.exists(sel[0].path):
            subprocess.Popen(["rundll32.exe", "shell32.dll,OpenAs_RunDLL", sel[0].path])

    def open_folder(self, force_default=False):
        sel = [] if force_default else self.selected()
        folder = sel[0].folder if sel else self.cfg["folder"]
        os.makedirs(folder, exist_ok=True)
        if sel and os.path.exists(sel[0].path):
            subprocess.Popen(["explorer", "/select,", sel[0].path])
        else:
            os.startfile(folder)

    def move_rename(self):
        sel = self.selected()
        if len(sel) != 1 or not sel[0].filename:
            return
        j = sel[0]
        if j.active:
            messagebox.showinfo("Taşı/Yeniden adlandır", "Önce indirmeyi duraklatın.")
            return
        new = filedialog.asksaveasfilename(initialdir=j.folder, initialfile=j.filename,
                                           title="Taşı / Yeniden adlandır")
        if not new:
            return
        new = os.path.normpath(new)
        try:
            src = j.path if j.status == DONE else j.part_path
            dst = new if j.status == DONE else new + ".fdlpart"
            if os.path.exists(src):
                shutil.move(src, dst)
            j.folder, j.filename = os.path.dirname(new), os.path.basename(new)
            self._after_cmd()
        except OSError as e:
            messagebox.showerror("Taşı/Yeniden adlandır", str(e))

    def refresh_url(self):
        sel = self.selected()
        if len(sel) != 1:
            return
        j = sel[0]
        win = self._dialog("İndirme adresini yenile")
        frm = ttk.Frame(win, padding=12)
        frm.pack(fill="both")
        ttk.Label(frm, text=f"{j.filename or ''}\nYeni adres (indirilen kısım korunur). fuckingfast için "
                            "sayfayı eklentiyle yeniden göndermek de yeterli.", wraplength=560).pack(anchor="w")
        var = tk.StringVar(value=j.url)
        e = ttk.Entry(frm, textvariable=var, width=90)
        e.pack(fill="x", pady=8)
        e.focus_set()
        e.select_range(0, "end")

        def ok():
            url = var.get().strip()
            if re.match(r"https?://", url):
                j.url, j.error = url, ""
                if j.status == ERROR:
                    j.status = PAUSED
                self._after_cmd()
            win.destroy()
        self._ok_cancel(frm, win, ok)

    def properties(self):
        sel = self.selected()
        if len(sel) != 1:
            return
        j = sel[0]
        win = self._dialog("Dosya özellikleri")
        frm = ttk.Frame(win, padding=12)
        frm.pack(fill="both", expand=True)
        done = j.downloaded
        rows = [("Dosya", j.filename or "?"), ("Klasör", j.folder), ("Adres", j.url),
                ("Boyut", f"{human(j.size)} ({j.size or '?'} bayt)"),
                ("İndirilen", human(done) + (f"  (%{done * 100 / j.size:.1f})" if j.size else "")),
                ("Bağlantı sayısı", str(len(j.segments) or j.connections)),
                ("Devam ettirilebilir", "Evet" if j.resumable else "Hayır / bilinmiyor"),
                ("Durum", self._status_text(j)), ("Kuyrukta", "Evet" if j.in_queue else "Hayır")]
        for r, (k, v) in enumerate(rows):
            ttk.Label(frm, text=k + ":", font=("Segoe UI", 9, "bold")).grid(row=r, column=0, sticky="nw", pady=2)
            ttk.Label(frm, text=v, wraplength=480).grid(row=r, column=1, sticky="w", padx=8, pady=2)
        bar = ttk.Frame(frm)
        bar.grid(row=len(rows), column=0, columnspan=2, pady=(10, 0))

        def copy():
            self.root.clipboard_clear()
            self.root.clipboard_append(j.url)
        ttk.Button(bar, text="Adresi kopyala", command=copy).pack(side="left", padx=4)
        ttk.Button(bar, text="Kapat", command=win.destroy).pack(side="left", padx=4)

    # ---------------- ekleme ----------------
    def add_dialog(self):
        win = self._dialog("URL Ekle", resizable=True)
        win.geometry("640x340")
        frm = ttk.Frame(win, padding=10)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Doğrudan indirme linkleri (her satıra bir tane).\n"
                            "fuckingfast sayfa linkleri için tarayıcı eklentisini kullanın.").pack(anchor="w")
        txt = tk.Text(frm, height=12, font=("Consolas", 9))
        txt.pack(fill="both", expand=True, pady=6)
        try:
            clip = self.root.clipboard_get()
            if clip.strip().startswith("http"):
                txt.insert("1.0", clip.strip())
        except tk.TclError:
            pass
        txt.focus_set()
        queue_now = tk.BooleanVar(value=self.cfg["auto_queue"])
        ttk.Checkbutton(frm, text="Sıraya al (kuyruk sırası gelince indir)", variable=queue_now).pack(anchor="w")

        def ok():
            old = self.cfg["auto_queue"]
            self.cfg["auto_queue"] = queue_now.get()
            n = sum(self.add_job(line) for line in txt.get("1.0", "end").splitlines() if line.strip())
            self.cfg["auto_queue"] = old
            win.destroy()
            self.flash(f"{n} link eklendi.")
            self.refresh()
        self._ok_cancel(frm, win, ok, ok_text="Ekle")

    def add_from_clipboard(self):
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            text = ""
        n = sum(self.add_job(u) for u in re.findall(r"https?://\S+", text))
        self.flash(f"Panodan {n} link eklendi.")
        self.refresh()

    # ---------------- seçenekler ----------------
    def options_dialog(self):
        win = self._dialog("Seçenekler")
        nb = ttk.Notebook(win)
        nb.pack(fill="both", expand=True, padx=10, pady=10)

        g = ttk.Frame(nb, padding=12)
        nb.add(g, text="Genel")
        folder = tk.StringVar(value=self.cfg["folder"])
        ttk.Label(g, text="Kayıt klasörü:").grid(row=0, column=0, sticky="w")
        ttk.Entry(g, textvariable=folder, width=48).grid(row=0, column=1, padx=6, sticky="we")
        ttk.Button(g, text="Gözat…", command=lambda: folder.set(
            os.path.normpath(filedialog.askdirectory(initialdir=folder.get(), parent=win) or folder.get()))
        ).grid(row=0, column=2)
        conn = tk.IntVar(value=self.cfg["connections"])
        par = tk.IntVar(value=self.cfg["parallel"])
        ttk.Label(g, text="Dosya başına bağlantı:").grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Spinbox(g, from_=1, to=32, textvariable=conn, width=6).grid(row=1, column=1, sticky="w",
                                                                         padx=6, pady=(10, 0))
        ttk.Label(g, text="Aynı anda indirilecek dosya:").grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Spinbox(g, from_=1, to=10, textvariable=par, width=6).grid(row=2, column=1, sticky="w",
                                                                        padx=6, pady=(6, 0))
        aq = tk.BooleanVar(value=self.cfg["auto_queue"])
        ttk.Checkbutton(g, text="Yeni indirmeleri otomatik sıraya al", variable=aq).grid(
            row=3, column=0, columnspan=3, sticky="w", pady=(10, 0))
        cu = tk.BooleanVar(value=self.cfg["check_updates"])
        ttk.Checkbutton(g, text="Açılışta güncellemeleri denetle", variable=cu).grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(4, 0))

        a = ttk.Frame(nb, padding=12)
        nb.add(a, text="Arşiv")
        ax = tk.BooleanVar(value=self.extract_var.get())
        da = tk.BooleanVar(value=self.cfg["delete_archives"])
        ttk.Checkbutton(a, text="Bir arşivin tüm parçaları inince otomatik çıkart", variable=ax).pack(anchor="w")
        ttk.Checkbutton(a, text="Başarılı çıkartmadan sonra arşiv dosyalarını sil", variable=da).pack(anchor="w")
        tool = find_tool("rar")
        ttk.Label(a, foreground="#5f6368", text="Çıkartılan dosyalar arşiv adında bir klasöre konur.\n"
                  f"Kullanılan araç: {tool or 'bulunamadı (WinRAR veya 7-Zip kurun)'}").pack(anchor="w", pady=8)
        ttk.Label(a, text="Kayıtlı şifreler (şifreli arşivlerde önce bunlar denenir):").pack(anchor="w")
        lst = tk.Listbox(a, height=6)
        for p in self.cfg["passwords"]:
            lst.insert("end", p)
        lst.pack(fill="x", pady=4)
        prow = ttk.Frame(a)
        prow.pack(fill="x")
        newpw = tk.StringVar()
        ttk.Entry(prow, textvariable=newpw, width=30).pack(side="left")

        def add_pw():
            if newpw.get() and newpw.get() not in lst.get(0, "end"):
                lst.insert("end", newpw.get())
            newpw.set("")
        ttk.Button(prow, text="Ekle", command=add_pw).pack(side="left", padx=4)
        ttk.Button(prow, text="Seçileni sil", command=lambda: [lst.delete(i) for i in reversed(lst.curselection())]
                   ).pack(side="left")

        h = ttk.Frame(nb, padding=12)
        nb.add(h, text="Hız")
        ttk.Label(h, text="Hız sınırlayıcı ayarları için:").pack(anchor="w")
        ttk.Button(h, text="Hız Sınırlayıcı ayarları…", command=self.speed_dialog).pack(anchor="w", pady=6)

        def ok():
            try:
                c, p = max(1, min(32, int(conn.get()))), max(1, min(10, int(par.get())))
            except (tk.TclError, ValueError):
                messagebox.showerror("Seçenekler", "Sayı alanlarını kontrol edin.", parent=win)
                return
            self.cfg.update(folder=folder.get().strip() or self.cfg["folder"], connections=c, parallel=p,
                            auto_queue=aq.get(), delete_archives=da.get(), passwords=list(lst.get(0, "end")),
                            check_updates=cu.get())
            self.extract_var.set(ax.get())
            self._dirty = True
            win.destroy()
            self.refresh()
        self._ok_cancel(win, win, ok)

    def help_dialog(self):
        messagebox.showinfo("Nasıl kullanılır?", (
            "• Sırada: kuyruk çalışıyorsa sırası gelince iner ('Aynı anda' sınırına uyar).\n"
            "• Bekliyor: listede ama sırada değil; siz başlatana kadar inmez.\n"
            "• Duraklatıldı / Durduruldu: kendiliğinden devam etmez. 'Devam Et' ile ya da\n"
            "  sıraya alınca devam eder. 'Tümünü Başlat' hepsini sıraya alıp kuyruğu başlatır.\n"
            "• K. Başlat / K. Durdur: kuyruğu çalıştırır ya da bekletir.\n"
            "• Devam Et: seçili dosyayı sırayı beklemeden hemen başlatır.\n"
            "• Arşiv çıkartma: alttaki onay kutusu işaretliyse, bir arşivin tüm parçaları\n"
            "  inince çıkartılır. Şifreliyse sorulur."))

    # ---------------- otomatik güncelleme ----------------
    @property
    def installed(self):
        """Setup ile kurulduysa Inno Setup kaldırıcısı program klasöründedir."""
        return os.path.exists(os.path.join(APP_DIR, "unins000.exe"))

    def check_updates(self, silent=False):
        def work():
            try:
                req = urllib.request.Request(
                    f"https://api.github.com/repos/{REPO}/releases/latest",
                    headers={"User-Agent": f"FastDL/{VERSION}", "Accept": "application/vnd.github+json"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    rel = json.load(r)
            except Exception as e:  # noqa
                if not silent:
                    self.ui_calls.put(lambda: messagebox.showerror(
                        "Güncelleme", f"Güncelleme denetlenemedi:\n{e}"))
                return
            tag = rel.get("tag_name", "")
            if parse_version(tag) > parse_version(VERSION) and not (silent and tag == self.cfg["skip_version"]):
                self.ui_calls.put(lambda: self.update_dialog(rel))
            elif not silent:
                self.ui_calls.put(lambda: messagebox.showinfo("Güncelleme", f"FastDL güncel (v{VERSION})."))
        threading.Thread(target=work, daemon=True).start()

    def update_dialog(self, rel):
        tag = rel.get("tag_name", "")
        asset = next((a for a in rel.get("assets", []) if a.get("name", "").lower().endswith(".exe")), None)
        win = self._dialog("FastDL Güncelleme")
        frm = ttk.Frame(win, padding=16)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text=f"Yeni sürüm mevcut: {tag}", font=("Segoe UI", 12, "bold")).pack(anchor="w")
        ttk.Label(frm, text=f"Kullandığınız sürüm: v{VERSION}", foreground="#5f6368").pack(anchor="w", pady=(2, 8))
        ttk.Label(frm, text="Yenilikler:").pack(anchor="w")
        notes = tk.Text(frm, height=10, width=64, wrap="word", font=("Segoe UI", 9), relief="solid", bd=1)
        notes.insert("1.0", (rel.get("body") or "Sürüm notu yok.").strip())
        notes.config(state="disabled")
        notes.pack(fill="both", expand=True, pady=(2, 8))
        bar = ttk.Progressbar(frm, mode="determinate", maximum=100)
        info = ttk.Label(frm, text="İndirmeleriniz ve ayarlarınız güncellemeden etkilenmez.", foreground="#5f6368")
        info.pack(anchor="w")
        row = ttk.Frame(frm)
        row.pack(anchor="e", pady=(12, 0))

        def skip():
            self.cfg["skip_version"] = tag
            self._dirty = True
            win.destroy()

        def start():
            for b in row.winfo_children():
                b.config(state="disabled")
            bar.pack(fill="x", pady=(6, 0), before=row)
            threading.Thread(target=self._download_update, args=(asset, bar, info, win), daemon=True).start()

        if asset and self.installed:
            ttk.Button(row, text="Şimdi güncelle", command=start, width=16).pack(side="left", padx=4)
        else:
            ttk.Button(row, text="İndirme sayfasını aç", width=18, command=lambda: (
                webbrowser.open(rel.get("html_url", f"https://github.com/{REPO}/releases")), win.destroy())
            ).pack(side="left", padx=4)
        ttk.Button(row, text="Bu sürümü atla", command=skip, width=14).pack(side="left", padx=4)
        ttk.Button(row, text="Sonra", command=win.destroy, width=10).pack(side="left")

    def _download_update(self, asset, bar, info, win):
        import tempfile
        path = os.path.join(tempfile.gettempdir(), safe_name(asset["name"]))
        try:
            req = urllib.request.Request(asset["browser_download_url"], headers={"User-Agent": f"FastDL/{VERSION}"})
            with urllib.request.urlopen(req, timeout=30) as r, open(path, "wb") as f:
                total = int(r.headers.get("Content-Length") or asset.get("size") or 0)
                got = 0
                while True:
                    data = r.read(256 * 1024)
                    if not data:
                        break
                    f.write(data)
                    got += len(data)
                    if total:
                        pct, text = got * 100 / total, f"İndiriliyor: {human(got)} / {human(total)}"
                        self.ui_calls.put(lambda p=pct, t=text: (bar.config(value=p), info.config(text=t)))
            if asset.get("size") and os.path.getsize(path) != asset["size"]:
                raise RuntimeError("İndirilen dosya eksik")
        except Exception as e:  # noqa
            self.ui_calls.put(lambda: (messagebox.showerror("Güncelleme", f"Güncelleme indirilemedi:\n{e}",
                                                            parent=win), win.destroy()))
            return
        self.ui_calls.put(lambda: self._install_update(path))

    def _install_update(self, path):
        # Kurulum sessiz çalışır, bitince FastDL'i yeniden açar.
        subprocess.Popen([path, "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS"],
                         creationflags=0x00000008)  # DETACHED_PROCESS
        self.on_close()

    # ---------------- diyalog yardımcıları ----------------
    def _dialog(self, title, resizable=False):
        win = tk.Toplevel(self.root)
        win.title(title)
        win.transient(self.root)
        win.resizable(resizable, resizable)
        win.grab_set()
        return win

    @staticmethod
    def _ok_cancel(parent, win, ok, cancel=None, ok_text="Tamam"):
        row = ttk.Frame(parent)
        if parent is win:
            row.pack(anchor="e", pady=(0, 10), padx=10)
        else:
            row.pack(anchor="e", pady=(12, 0))
        ttk.Button(row, text=ok_text, command=ok, width=12).pack(side="left", padx=4)
        ttk.Button(row, text="İptal", command=cancel or win.destroy, width=12).pack(side="left")
        win.bind("<Escape>", lambda e: (cancel or win.destroy)())

    def flash(self, text):
        self._flash = (text, time.time() + 4)

    # ---------------- döngü ----------------
    def tick(self):
        while not self.ui_calls.empty():
            try:
                self.ui_calls.get()()
            except Exception:  # noqa
                pass
        while not self.inbox.empty():
            item = self.inbox.get()
            if self.add_job(item.get("url", ""), item.get("filename"), item.get("headers")):
                self.flash(f"Eklentiden alındı: {item.get('filename') or item.get('url')}")
        if self.queue_var.get():
            running = sum(1 for j in self.jobs if j.active)
            for j in self.jobs:
                if running >= self.cfg["parallel"]:
                    break
                if j.status == QUEUED and j.in_queue and not j.active:
                    if not j.segments:
                        j.connections = self.cfg["connections"]
                    j.start(self.changed, by_queue=True)
                    running += 1
        self._check_extract()
        self.refresh()
        if self._dirty:
            self._dirty = False
            self.save()
        self.root.after(500, self.tick)

    def _status_text(self, j):
        if j.status == DONE:
            return DONE + (f" · {j.extract}" if j.extract not in ("", "-") else "")
        if j.status == QUEUED and not self.queue_var.get():
            return "Sırada (kuyruk durduruldu)"
        return j.status + (f": {j.error}" if j.error else "")

    def refresh(self):
        visible = [j for j in self.jobs if self._match(j)]
        ids = [j.id for j in visible]
        existing = set(self.tree.get_children())
        for iid in existing - set(ids):
            self.tree.delete(iid)
        total_speed = 0
        for idx, j in enumerate(visible):
            done = j.downloaded
            if j.size:
                pct = done * 100 / j.size
                bar = "█" * int(pct / 10) + "░" * (10 - int(pct / 10))
                prog = f"{bar}  {pct:.1f}%"
            else:
                prog = human(done) if done else ""
            eta = (j.size - done) / j.speed if j.size and j.speed > 1 else None
            vals = (j.filename or j.url, human(j.size) if j.size else "", prog, self._status_text(j),
                    f"{human(j.speed)}/s" if j.speed else "", human_time(eta) if j.speed else "")
            tag = ("done" if j.status == DONE else "error" if j.status == ERROR
                   else "active" if j.active else "idle")
            total_speed += j.speed
            if j.id in existing:
                self.tree.item(j.id, values=vals, tags=(tag,))
                if self.tree.index(j.id) != idx:
                    self.tree.move(j.id, "", idx)
            else:
                self.tree.insert("", idx, iid=j.id, values=vals, tags=(tag,))

        counts = {k: sum(1 for j in self.jobs if self._match_cat(j, k)) for k, _ in self.CATS}
        for key, label in self.CATS:
            self.cats.item(key, text=f"{label} ({counts[key]})")

        active = sum(1 for j in self.jobs if j.active)
        queued = sum(1 for j in self.jobs if j.status == QUEUED)
        flash = getattr(self, "_flash", None)
        if flash and time.time() < flash[1]:
            text = flash[0]
        else:
            text = (f"İnen: {active}   Sırada: {queued}   Toplam hız: {human(total_speed)}/s   "
                    f"Kuyruk: {'çalışıyor' if self.queue_var.get() else 'durduruldu'}")
        self.status_lbl.config(text=text)
        self.speed_lbl.config(text=(f"Hız sınırı: {self.cfg['speed_kb']} KB/s (açık)" if self.speed_on.get()
                                    else "Hız sınırı: kapalı"),
                              fg="#e37400" if self.speed_on.get() else "#5f6368")
        self._update_buttons()

    def _match_cat(self, j, key):
        old, self.filter = self.filter, key
        try:
            return self._match(j)
        finally:
            self.filter = old

    def on_close(self):
        for j in self.jobs:
            if j.active:
                j._pause_status = QUEUED
                j._stop.set()
        for j in self.jobs:
            if j.thread:
                j.thread.join(timeout=3)
        self.save()
        self.root.destroy()


# --------------------------------------------------------------------------- #
#  Eklentiden link alan yerel sunucu (sadece 127.0.0.1)
# --------------------------------------------------------------------------- #
def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _allowed(self):
            # Web sayfaları özel başlık gönderemez (CORS preflight'a yanıt vermiyoruz).
            if self.headers.get("X-FastDL") != "1":
                return False
            origin = self.headers.get("Origin", "")
            return not origin or origin.startswith(("chrome-extension://", "moz-extension://"))

        def _reply(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/ping":
                return self._reply(200, {"ok": True, "app": "FastDL"})
            self._reply(404, {"ok": False})

        def do_POST(self):
            if self.path != "/add" or not self._allowed():
                return self._reply(403, {"ok": False})
            try:
                n = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(min(n, 1_000_000)))
                items = data if isinstance(data, list) else [data]
                for it in items:
                    headers = {}
                    if it.get("cookies"):
                        headers["Cookie"] = it["cookies"]
                    if it.get("userAgent"):
                        headers["User-Agent"] = it["userAgent"]
                    if it.get("referer"):
                        headers["Referer"] = it["referer"]
                    app.inbox.put({"url": it["url"], "filename": it.get("filename"), "headers": headers})
                self._reply(200, {"ok": True, "count": len(items)})
            except Exception as e:  # noqa
                self._reply(400, {"ok": False, "error": str(e)})
    return Handler


def main():
    migrate_data()
    root = tk.Tk()
    app = App(root)
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), make_handler(app))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    except OSError:
        messagebox.showwarning("FastDL", f"{PORT} portu kullanımda. FastDL zaten açık olabilir;\n"
                                         "eklenti entegrasyonu bu pencerede çalışmayacak.")
    root.mainloop()


if __name__ == "__main__":
    main()
