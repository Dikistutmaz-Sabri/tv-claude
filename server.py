import json
import logging
import os
import re
import subprocess
import threading
import time
import tempfile
from collections import OrderedDict
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import anthropic

BASE = Path(__file__).resolve().parent
TOKEN = os.environ["TV_CLAUDE_TOKEN"]
BOX = os.environ.get("TV_BOX", "192.168.1.190:5555")
ALLOWED = {BOX.split(":")[0], "127.0.0.1"}
PORT = int(os.environ.get("TV_CLAUDE_PORT", "8790"))
MODEL = os.environ.get("TV_CLAUDE_MODEL", "claude-haiku-5-5")
DAILY_TOKEN_LIMIT = int(os.environ.get("TV_CLAUDE_DAILY_TOKENS", "300000"))
APPS_FILE = BASE / "apps.json"
USAGE_FILE = BASE / "usage.jsonl"
PIPER_DIR = Path(os.environ.get("TV_CLAUDE_PIPER", "/home/dietpi/apps/piper"))
VOICE = os.environ.get("TV_CLAUDE_VOICE", "tr_TR-99eren99")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tv-claude")

client = anthropic.Anthropic(max_retries=1, timeout=anthropic.Timeout(20.0, connect=3.0))
lock = threading.Lock()
history: list[dict] = []
history_at = 0.0
usage_today = {"day": date.today().isoformat(), "tokens": 0}


def load_apps() -> dict[str, str]:
    try:
        return json.loads(APPS_FILE.read_text())
    except (OSError, ValueError):
        return {}


apps = load_apps()


def norm(s: str) -> str:
    s = s.replace("İ", "i").replace("I", "ı").lower()
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s)).strip()


def find_app(name: str) -> str | None:
    n = norm(name)
    if not n:
        return None
    for label, pkg in apps.items():
        if norm(label) == n:
            return pkg
    for label, pkg in apps.items():
        nl = norm(label)
        if n in nl or nl in n:
            return pkg
    return None


def act(kind: str, **kw) -> dict:
    return {"type": kind, **kw}


NUM = {"bir": 1, "iki": 2, "üç": 3, "dört": 4, "beş": 5, "altı": 6, "yedi": 7, "sekiz": 8, "dokuz": 9, "on": 10}


def steps_in(t: str, default: int) -> int:
    m = re.search(r"\b(\d{1,2})\b", t)
    if m:
        return max(1, min(15, int(m.group(1))))
    for w, v in NUM.items():
        if re.search(rf"\b{w}\b", t):
            return v
    if re.search(r"\b(biraz|azıcık|az)\b", t):
        return 2
    if re.search(r"\b(çok|epey|bayağı)\b", t):
        return 6
    return default


# "kapat", "box'ı kapat", "android'i kapat", "televizyonu kapat", "TV box'u kapat", "stand by'a al"...
_DEVICE = r"(tv ?box|tv ?boks|tivi ?box|box|boks|kutu|android( tv)?|tv|tivi|televizyon|cihaz|ekran)( ?(u|ü|ı|i|yu|yü|yı|yi|nu|nü))?"
_OFF = (r"(kapat|kapa|kapan|kapansın|kapatır mısın|kapat lütfen|lütfen kapat|uyut|"
        r"(beklemeye|bekleme moduna|uyku moduna|stand ?by( a)?|standby( a)?) (al|geç))")
SLEEP_RE = re.compile(rf"(({_DEVICE} )?{_OFF}|her şeyi kapat|stand ?by|standby|uyku modu|bekleme modu)")


def fast_path(text: str) -> dict | None:
    t = norm(text)
    if not t:
        return None
    if re.fullmatch(r"(sesi? )?(sessize al|sustur|sessiz( ol)?)|sesi? kapat|kapat sesi?", t):
        return {"say": "", "actions": [act("volume", direction="mute")]}
    if re.fullmatch(r"(sesi )?(aç|geri aç)", t) and "ses" in t:
        return {"say": "", "actions": [act("volume", direction="unmute")]}
    if re.search(r"\bses(i)?\b", t) and re.search(r"\b(kıs|azalt|düşür|alçalt)", t):
        return {"say": "", "actions": [act("volume", direction="down", steps=steps_in(t, 3))]}
    if re.search(r"\bses(i)?\b", t) and re.search(r"\b(aç|arttır|artır|yükselt)", t):
        return {"say": "", "actions": [act("volume", direction="up", steps=steps_in(t, 3))]}
    if re.fullmatch(r"(durdur|dur|duraklat|pause|beklet)", t):
        return {"say": "", "actions": [act("media", key="pause")]}
    if re.fullmatch(r"(oynat|devam( et)?|başlat|play)", t):
        return {"say": "", "actions": [act("media", key="play")]}
    if re.fullmatch(r"(sonraki|ileri|sıradaki)( bölüm| şarkı| video)?", t):
        return {"say": "", "actions": [act("media", key="next")]}
    if re.fullmatch(r"(önceki|geri)( bölüm| şarkı| video)?", t):
        return {"say": "", "actions": [act("media", key="previous")]}
    if re.fullmatch(r"(ana ?ekran(a dön| aç)?|ana sayfa(ya dön)?|eve dön)", t):
        return {"say": "", "actions": [act("nav", key="home")]}
    if re.fullmatch(r"(geri dön|geri git)", t):
        return {"say": "", "actions": [act("nav", key="back")]}
    if SLEEP_RE.fullmatch(t):
        return {"say": "İyi geceler.", "actions": [act("sleep")]}
    m = re.fullmatch(r"youtube ?(da|ta|de|te) (.+?) (aç|ara|bul|oynat|izle|göster|çal)", t)
    if m:
        return {"say": "", "actions": [act("youtube_search", query=m.group(2))]}
    m = re.fullmatch(r"(.+?) (aç|başlat|çalıştır)", t)
    if m:
        name = m.group(1)
        stripped = re.sub(r" ?(yı|yi|yu|yü|nı|ni|nu|nü|ı|i|u|ü)$", "", name)
        pkg = find_app(stripped) or find_app(name)
        if pkg:
            return {"say": "", "actions": [act("open_app", package=pkg)]}
    return None


SYSTEM = """Sen bir Android TV kutusunun sesli asistanısın. Kullanıcı Türkçe konuşur, sesi yazıya çevrilmiş olarak gelir (yazım hataları olabilir).
Görevin: isteği anla, gerekirse `tv` aracını TEK SEFERDE çağır ve `say` alanına TV'nin sesli okuyacağı kısa Türkçe cevabı yaz.
Kurallar:
- TV'de bir işlem gerekiyorsa mutlaka `tv` aracını çağır; işlem yoksa (soru, sohbet) araç çağırma, düz metinle cevap ver.
- Cevaplar sesli okunur: en fazla 2 kısa cümle, liste, emoji, markdown yok.
- İşlem için onay cümlesi gerekmez; `say` boş bırakılabilir. Belirsizse en olası işlemi yap.
- Uygulama açarken yalnızca aşağıdaki listedeki paket adlarını kullan. Listede yoksa bunu söyle.
- TV'yi, televizyonu, kutuyu, box'ı, Android'i kapatma ya da beklemeye/uyku moduna alma istekleri `sleep` işlemidir.
- Dizi/film/müzik/video istekleri YouTube'da aranır (youtube_search), kullanıcı başka uygulama demediyse.

Kurulu uygulamalar (etiket: paket):
{apps}"""

TV_TOOL = {
    "name": "tv",
    "description": "TV kutusunda bir veya birkaç işlemi sırayla uygular ve `say` metnini sesli okur.",
    "input_schema": {
        "type": "object",
        "properties": {
            "say": {"type": "string", "description": "Sesli okunacak kısa Türkçe cevap; gerekmiyorsa boş."},
            "actions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": ["open_app", "youtube_search", "volume", "media", "nav", "sleep"]},
                        "package": {"type": "string", "description": "open_app için paket adı"},
                        "query": {"type": "string", "description": "youtube_search için arama metni"},
                        "direction": {"type": "string", "enum": ["up", "down", "mute", "unmute"]},
                        "steps": {"type": "integer", "minimum": 1, "maximum": 15},
                        "key": {"type": "string", "enum": ["play", "pause", "play_pause", "next", "previous", "fast_forward", "rewind", "stop", "home", "back"]},
                    },
                    "required": ["type"],
                },
            },
        },
        "required": ["say", "actions"],
    },
}

VALID = {
    "open_app": lambda a: a.get("package") in apps.values(),
    "youtube_search": lambda a: bool(a.get("query")),
    "volume": lambda a: a.get("direction") in {"up", "down", "mute", "unmute"},
    "media": lambda a: a.get("key") in {"play", "pause", "play_pause", "next", "previous", "fast_forward", "rewind", "stop"},
    "nav": lambda a: a.get("key") in {"home", "back"},
    "sleep": lambda a: True,
}


def record_usage(u, text: str, ms: int) -> None:
    total = u.input_tokens + u.output_tokens + (u.cache_read_input_tokens or 0) + (u.cache_creation_input_tokens or 0)
    today = date.today().isoformat()
    if usage_today["day"] != today:
        usage_today.update(day=today, tokens=0)
    usage_today["tokens"] += total
    with USAGE_FILE.open("a") as f:
        f.write(json.dumps({
            "ts": int(time.time()), "model": MODEL, "ms": ms, "in": u.input_tokens, "out": u.output_tokens,
            "cache_read": u.cache_read_input_tokens, "cache_write": u.cache_creation_input_tokens, "chars": len(text),
        }) + "\n")


def ask_claude(text: str) -> dict:
    global history, history_at
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {"say": "API anahtarı henüz girilmemiş.", "actions": []}
    if usage_today["day"] == date.today().isoformat() and usage_today["tokens"] > DAILY_TOKEN_LIMIT:
        return {"say": "Bugünkü kullanım sınırına ulaşıldı.", "actions": []}
    with lock:
        if time.time() - history_at > 180:
            history = []
        messages = history[-6:] + [{"role": "user", "content": text}]
        app_lines = "\n".join(f"- {k}: {v}" for k, v in sorted(apps.items()))
        started = time.monotonic()
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=600,
                system=SYSTEM.format(apps=app_lines or "- (liste henüz gelmedi)"),
                tools=[TV_TOOL],
                tool_choice={"type": "auto"},
                thinking={"type": "disabled"},
                output_config={"effort": "low"},
                cache_control={"type": "ephemeral"},
                messages=messages,
            )
        except anthropic.AuthenticationError:
            log.error("API anahtarı geçersiz")
            return {"say": "API anahtarı geçersiz.", "actions": []}
        except anthropic.RateLimitError:
            return {"say": "Şu an çok yoğun, biraz sonra tekrar dene.", "actions": []}
        except anthropic.APIStatusError as e:
            log.error("API hatası %s %s", e.status_code, e.message)
            return {"say": "Bir hata oldu.", "actions": []}
        except anthropic.APIConnectionError:
            return {"say": "İnternete ulaşamıyorum.", "actions": []}
        ms = int((time.monotonic() - started) * 1000)
        record_usage(resp.usage, text, ms)
        if resp.stop_reason == "refusal":
            return {"say": "Bunu yapamam.", "actions": []}
        say, actions = "", []
        for block in resp.content:
            if block.type == "tool_use" and block.name == "tv":
                inp = block.input if isinstance(block.input, dict) else {}
                say = str(inp.get("say") or "")
                raw = inp.get("actions") if isinstance(inp.get("actions"), list) else []
                actions = [a for a in raw if isinstance(a, dict) and a.get("type") in VALID and VALID[a["type"]](a)]
            elif block.type == "text" and block.text.strip():
                say = say or block.text.strip()
        history = messages + [{"role": "assistant", "content": say or "Tamam."}]
        history_at = time.time()
        log.info("claude %dms in=%s out=%s -> %s %s", ms, resp.usage.input_tokens, resp.usage.output_tokens, say, actions)
        return {"say": say, "actions": actions}


def run_server_side(actions: list[dict]) -> list[dict]:
    rest = []
    for a in actions:
        if a["type"] == "sleep":
            # Kısa bekleme: kutu uyumadan önce "İyi geceler" sesli okunabilsin.
            threading.Timer(4.0, lambda: subprocess.Popen(
                ["adb", "-s", BOX, "shell", "input", "keyevent", "KEYCODE_SLEEP"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)).start()
        else:
            rest.append(a)
    return rest


class Piper:
    """Ses modelini bellekte tutan kalıcı Piper süreci; her satır bir wav dosyası üretir."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.out = Path(tempfile.mkdtemp(prefix="tv-claude-tts-"))
        self.cache: OrderedDict[str, bytes] = OrderedDict()

    def _start(self) -> subprocess.Popen:
        if self.proc is None or self.proc.poll() is not None:
            model = PIPER_DIR / "voices" / f"{VOICE}.onnx"
            self.proc = subprocess.Popen(
                [str(PIPER_DIR / "piper" / "piper"), "-m", str(model), "--output_dir", str(self.out), "-q"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1,
            )
            log.info("piper başladı (%s)", VOICE)
        return self.proc

    def wav(self, text: str) -> bytes | None:
        text = " ".join(text.split())[:400]
        if not text:
            return None
        with self.lock:
            if text in self.cache:
                self.cache.move_to_end(text)
                return self.cache[text]
            started = time.monotonic()
            try:
                proc = self._start()
                proc.stdin.write(text + "\n")
                proc.stdin.flush()
                path = Path(proc.stdout.readline().strip())
                data = path.read_bytes()
                path.unlink(missing_ok=True)
            except (OSError, ValueError) as e:
                log.warning("piper hatası: %s", e)
                if self.proc:
                    self.proc.kill()
                self.proc = None
                return None
            log.info("ses %dms, %d KB: %s", int((time.monotonic() - started) * 1000), len(data) // 1024, text[:60])
            self.cache[text] = data
            if len(self.cache) > 40:
                self.cache.popitem(last=False)
            return data


piper = Piper()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def reply(self, code: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            return self.reply(200, {"ok": True, "model": MODEL, "apps": len(apps)})
        self.reply(404, {})

    def do_POST(self):
        global apps
        if self.client_address[0] not in ALLOWED or self.headers.get("X-Token") != TOKEN:
            return self.reply(403, {})
        try:
            body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 65536)) or b"{}")
        except ValueError:
            return self.reply(400, {})
        if self.path == "/apps" and isinstance(body.get("apps"), dict):
            apps = {str(k)[:60]: str(v)[:120] for k, v in list(body["apps"].items())[:200]}
            APPS_FILE.write_text(json.dumps(apps, ensure_ascii=False, indent=1))
            log.info("uygulama listesi: %d", len(apps))
            return self.reply(200, {"ok": True})
        if self.path == "/ask":
            text = str(body.get("text", ""))[:500].strip()
            if not text:
                return self.reply(200, {"say": "Seni duyamadım.", "actions": []})
            started = time.monotonic()
            result = fast_path(text)
            source = "hızlı"
            if result is None:
                result = ask_claude(text)
                source = "claude"
            result["actions"] = run_server_side(result["actions"])
            log.info("%s [%s] %dms: %s", source, text, int((time.monotonic() - started) * 1000), result)
            return self.reply(200, result)
        if self.path == "/tts":
            data = piper.wav(str(body.get("text", "")))
            if data is None:
                return self.reply(503, {})
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.reply(404, {})


def warm_up() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log.warning("ANTHROPIC_API_KEY boş; yalnızca hızlı yol çalışır")
        return
    try:
        client.models.retrieve(MODEL)
        log.info("API bağlantısı hazır (%s)", MODEL)
    except anthropic.APIError as e:
        log.warning("ısınma başarısız: %s", e)


if __name__ == "__main__":
    threading.Thread(target=warm_up, daemon=True).start()
    threading.Thread(target=lambda: [piper.wav(t) for t in ("Seni duyamadım.", "Tamam.")], daemon=True).start()
    log.info("dinleniyor :%d", PORT)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
