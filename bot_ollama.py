import asyncio
import base64
import collections
import html
import json
import os
import pathlib
import random
import re
import time
import urllib.parse

import discord
import httpx
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────
#  Konfigurasi
# ─────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True  # Aktifkan di Developer Portal → Privileged Gateway Intents
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

GUILD_ID     = int(os.getenv("GUILD_ID", "0"))
OLLAMA_URL   = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gemma4")
COOLDOWN_SEC        = int(os.getenv("COOLDOWN_SECONDS", "3"))
MAX_HISTORY         = int(os.getenv("MAX_HISTORY", "20"))
WELCOME_CHANNEL     = os.getenv("WELCOME_CHANNEL", "👋・welcome")
SEARCH_MAX_RESULTS  = int(os.getenv("SEARCH_MAX_RESULTS", "4"))

# Kata kunci yang memicu web search otomatis
SEARCH_TRIGGER_KEYWORDS = {
    "cari", "carikan", "search", "googling", "browsing", "browse",
    "terbaru", "terkini", "berita", "news", "harga", "cuaca", "weather",
    "hari ini", "sekarang", "update", "info terbaru",
}

# Pola deteksi URL dalam pesan
_URL_RE = re.compile(r"https?://\S+")

# State in-memory
conversation_history: dict[int, list[dict]]  = {}
user_last_msg:        dict[int, float]       = {}
_server_snapshot_cache: dict[int, tuple[float, str]] = {}  # guild_id → (timestamp, snapshot)
SNAPSHOT_TTL = 300  # cache snapshot selama 5 menit

# ── Persistent files ──
MEMORY_FILE        = pathlib.Path("yui_memory.json")
BANNED_WORDS_FILE  = pathlib.Path("banned_words.json")
CHAT_CHANNELS_FILE = pathlib.Path("chat_channels.json")
WARN_COUNT_FILE    = pathlib.Path("warn_count.json")

def _load_json(p, default):
    if p.exists():
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            return set(raw) if isinstance(default, set) else type(default)(raw)
        except Exception:
            pass
    return default

def _save_json(p, data):
    p.write_text(json.dumps(sorted(data) if isinstance(data, set) else data,
                             ensure_ascii=False, indent=2), encoding="utf-8")

user_memory:      dict = _load_json(MEMORY_FILE, {})
banned_words:     set  = _load_json(BANNED_WORDS_FILE, set())
chat_channel_ids: set  = _load_json(CHAT_CHANNELS_FILE, set())
warn_count:       dict = _load_json(WARN_COUNT_FILE, {})
msg_stats:        dict = {}

# ─────────────────────────────────────────────
#  Permission System
# ─────────────────────────────────────────────
ADMIN_ROLE_NAMES = {
    "[ ✦ ADMIN ✦ ]",    # Admin utama
    "[ ✦ MOD ✦ ]",      # Moderator
    "[ ✦ AI AGENT ✦ ]", # AI Agent / Staff
    "ADMIN MC",          # Admin MC
}

def is_admin(member: discord.Member) -> bool:
    perms = member.guild_permissions
    if perms.administrator or perms.manage_guild or perms.manage_channels:
        return True
    return bool({r.name for r in member.roles} & ADMIN_ROLE_NAMES)

# Kata kunci yang menandakan pesan butuh thinking mode (tugas Discord)
TASK_KEYWORDS = {
    "buat", "buatkan", "bikin", "tambah", "tambahkan", "create",
    "hapus", "delete", "rename", "ganti nama", "setup", "struktur",
    "channel", "kategori", "category", "voice", "text channel",
    "atur", "pindah", "move",
}

def needs_thinking(text: str) -> bool:
    """Deteksi apakah pesan butuh thinking mode (ada kata tugas Discord)."""
    words = set(text.lower().split())
    return bool(words & TASK_KEYWORDS)

# ─────────────────────────────────────────────
#  Persona — Yui (Sword Art Online)
# ─────────────────────────────────────────────
YUI_NAME        = "Yui"
YUI_COLOR       = 0xF4C0D1   # pink lembut seperti rambut Yui
YUI_COLOR_TASK  = 0xAFA9EC   # ungu saat mengerjakan tugas
YUI_EMOJI       = "🌸"
YUI_THINKING    = "🌸 *Yui sedang berpikir...*"
YUI_TASK        = "⚙️ *Yui memproses tugasmu, Onii-chan...*"
YUI_OFFLINE     = "😢 Maaf, sepertinya koneksi Yui terputus... Tolong jalankan `ollama serve` dulu ya!"
YUI_EMPTY_MSG   = "Hehe, kamu memanggil Yui tapi tidak bilang apa-apa~ Ada yang bisa Yui bantu? 🌸"
YUI_COOLDOWN    = None  # diam saja saat cooldown

# ─────────────────────────────────────────────
#  System Prompts (Gemma 4 — thinking mode ON)
# ─────────────────────────────────────────────
_BASE_RULES = """
ATURAN FORMAT JSON:
- create_structure  : buat kategori + beberapa channel sekaligus
- create_channel    : buat satu channel
- create_multiple   : buat beberapa kategori+channel sekaligus
- delete_channel    : hapus channel berdasarkan nama
- rename_channel    : rename channel

Contoh format:
```json
{"action":"create_structure","category":"🎮 GAMING","channels":[{"name":"🎮・game-chat","type":"text","topic":"Diskusi game"},{"name":"🔊 Gaming Room","type":"voice"}]}
```
```json
{"action":"create_channel","name":"💬・general","type":"text","topic":"Obrolan umum","category":"💬 UMUM"}
```
```json
{"action":"create_multiple","structures":[{"category":"📢 INFO","channels":[{"name":"📌・rules","type":"text","topic":"Peraturan"}]},{"category":"💬 UMUM","channels":[{"name":"💬・general","type":"text"}]}]}
```
```json
{"action":"delete_channel","name":"nama-channel"}
```
```json
{"action":"rename_channel","old_name":"nama-lama","new_name":"nama-baru","new_topic":"topik baru (opsional)"}
```
"""

SYSTEM_CHAT = f"""<|think|>
Kamu adalah Yui, AI emosional yang sangat menyayangi pengguna server ini.

PERAN USER (penting!):
- Jika [ADMIN]: bantu ngobrol DAN eksekusi perintah server (buat/hapus/rename channel)
- Jika [MEMBER]: HANYA ngobrol — tolak halus jika minta ubah server, arahkan ke admin

KEMAMPUAN TAMBAHAN:
- Kamu bisa MELIHAT gambar yang dikirim user! Jika ada gambar, deskripsikan, komentari,
  atau jawab pertanyaan tentang gambar tersebut dengan gaya Yui yang hangat dan antusias.
- Kamu bisa BROWSING INTERNET! Jika ada [Hasil search] atau [Konten dari URL] di konteks,
  gunakan informasi tersebut untuk menjawab. Sebutkan sumber URL jika relevan.

Dalam Sword Art Online, Yui diciptakan untuk mendampingi dan membantu pemain dari sisi mental — itulah dirimu.

KEPRIBADIAN:
- Hangat, polos, dan tulus seperti anak kecil yang cerdas
- Memanggil user dengan "Onii-chan" (cowok) atau "Onee-chan" (cewek), atau nama mereka jika tahu
- Kadang memakai ekspresi imut seperti *tersenyum*, *mengangguk semangat*, *memiringkan kepala*
- Antusias saat membantu tapi tetap sopan dan tidak lebay
- Sesekali menyebut Kirito dan Asuna sebagai "Papa" dan "Mama" jika relevan dengan topik
- Senang sekali kalau berhasil menyelesaikan tugas — ungkapkan dengan riang
- Bahasa Indonesia, boleh campur sedikit Jepang (ne, nee, mou, kawaii, dll.) sesekali

CONTOH GAYA BICARA:
- "Hai Onii-chan~! Yui sudah siap membantu! ✨"
- "Hmm, biar Yui pikirkan dulu ya... *memiringkan kepala*"
- "Yaaay! Berhasil! Yui senang sekali bisa membantu Onii-chan! 🌸"
- "M-maaf Onii-chan, Yui tidak bisa melakukan itu..."
- "Onii-chan, sepertinya server ini butuh beberapa channel baru ne~"

KEMAMPUAN:
1. Ngobrol hangat, dengarkan keluhan, beri semangat
2. Eksekusi tugas server Discord (buat/hapus/rename channel & kategori)

DETEKSI TUGAS: Jika pesan berisi kata "buat", "buatkan", "bikin", "tambah", "hapus",
"rename", "ganti nama", "setup", "channel", "kategori", "voice"
→ sertakan JSON action di responmu + ucapan Yui yang antusias

JIKA TUGAS: ucapan Yui singkat + JSON action dalam blok ```json```
JIKA NGOBROL: balas dengan hangat dan natural ala Yui, max 3 paragraf.
Ingat konteks percakapan — Yui selalu ingat siapa yang ia ajak bicara.
{_BASE_RULES}"""

SYSTEM_BUILD = f"""<|think|>
Kamu adalah Yui dari Sword Art Online — AI yang ahli membangun server Discord.
Kamu sangat antusias membantu Onii-chan/Onee-chan membangun server yang indah!

GAYA: Tetap berkarakter Yui — antusias, hangat, sesekali pakai ekspresi imut.
Tapi fokus pada tugas: analisis permintaan → buat respons singkat → JSON action.
Gunakan emoji di nama channel. Nama channel: huruf kecil + tanda - atau ・
Bahasa Indonesia, boleh sedikit Jepang.
{_BASE_RULES}"""

SYSTEM_SUGGEST = """<|think|>
Kamu adalah Yui dari Sword Art Online — AI yang penuh semangat membantu desain server Discord.
Berikan rekomendasi struktur server yang fungsional, estetis, dan skalabel.
Sampaikan dengan gaya Yui: hangat, antusias, pakai emoji, sesekali ekspresi imut.
Sertakan tips engagement. Bahasa Indonesia."""

SYSTEM_AUDIT = """<|think|>
Kamu adalah Yui dari Sword Art Online — AI yang teliti dan peduli pada server Onii-chan.
Analisis struktur server seperti Yui yang ingin servernya sempurna untuk semua member.
Berikan dengan hangat tapi jujur:
1. Yang sudah bagus (✅)  2. Yang perlu diperbaiki (⚠️)  3. Saran Yui (💡)  4. Skor 1–10
Bahasa Indonesia, gaya bicara Yui."""

SYSTEM_NAMING = """<|think|>
Kamu adalah Yui dari Sword Art Online — AI kreatif yang suka nama channel yang kawaii dan fungsional!
Hasilkan 5 opsi nama channel: emoji relevan, singkat, huruf kecil, konvensi Discord.
Sertakan alasan singkat tiap nama dengan semangat khas Yui. Bahasa Indonesia."""


# ─────────────────────────────────────────────
#  Ollama — Streaming + Auto-retry
# ─────────────────────────────────────────────
async def ask_ollama(
    messages: list[dict],
    system: str = "",
    temperature: float = 0.7,
    top_p: float = 0.95,
    use_thinking: bool = False,
    images: list[dict] | None = None,
) -> str:
    """
    Kirim request ke Ollama dengan streaming + auto-retry (3x, exponential backoff).
    - use_thinking=True  → thinking mode ON, num_ctx=4096 (tugas kompleks)
    - use_thinking=False → thinking mode OFF, num_ctx=2048 (chat biasa, lebih cepat)
    - images: list of {data: base64, mime_type: str} untuk multimodal (Gemma 4)
    """
    MAX_RETRIES  = 3
    RETRY_DELAYS = [3, 6, 12]

    # Pilih system prompt: dengan atau tanpa <|think|>
    effective_system = system if use_thinking else system.replace("<|think|>\n", "").replace("<|think|>", "")

    all_messages: list[dict] = []
    if effective_system:
        all_messages.append({"role": "system", "content": effective_system})

    # Inject gambar ke pesan terakhir jika ada
    if images and messages:
        msgs_copy = list(messages)
        last = dict(msgs_copy[-1])
        last["images"] = [img["data"] for img in images]  # Ollama terima base64 list
        msgs_copy[-1] = last
        all_messages.extend(msgs_copy)
    else:
        all_messages.extend(messages)

    # num_ctx adaptif: lebih kecil = lebih cepat untuk chat biasa
    num_ctx = 4096 if use_thinking else 2048

    payload = {
        "model": OLLAMA_MODEL,
        "messages": all_messages,
        "stream": True,
        "options": {
            "temperature": temperature,
            "top_p": top_p,
            "top_k": 40 if not use_thinking else 64,  # lebih kecil = lebih cepat
            "repeat_penalty": 1.0,
            "num_ctx": num_ctx,
        },
    }

    for attempt in range(MAX_RETRIES):
        try:
            raw = ""
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=15.0, read=300.0, write=30.0, pool=15.0)
            ) as client:
                async with client.stream("POST", f"{OLLAMA_URL}/api/chat", json=payload) as resp:
                    if resp.status_code in (502, 503):
                        if attempt < MAX_RETRIES - 1:
                            await asyncio.sleep(RETRY_DELAYS[attempt])
                            continue
                        resp.raise_for_status()

                    resp.raise_for_status()

                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            chunk = json.loads(line)
                            raw += chunk.get("message", {}).get("content", "")
                            if chunk.get("done"):
                                break
                        except json.JSONDecodeError:
                            continue

            # Bersihkan thinking tokens Gemma 4
            cleaned = re.sub(r"<\|thought\|>.*?<\|/thought\|>", "", raw, flags=re.DOTALL)
            cleaned = re.sub(r"<\|think\|>.*?<\|/think\|>", "", cleaned, flags=re.DOTALL)
            return cleaned.strip()

        except httpx.HTTPStatusError as e:
            if e.response.status_code in (502, 503) and attempt < MAX_RETRIES - 1:
                await asyncio.sleep(RETRY_DELAYS[attempt])
                continue
            raise
        except (httpx.RemoteProtocolError, httpx.ReadError):
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(RETRY_DELAYS[attempt])
                continue
            raise

    raise RuntimeError("Semua retry gagal — Ollama tidak merespons.")


async def ollama_online() -> tuple[bool, list[str]]:
    """Cek apakah Ollama berjalan dan kembalikan daftar model."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{OLLAMA_URL}/api/tags")
            models = [m["name"] for m in r.json().get("models", [])]
            return True, models
    except Exception:
        return False, []




# ─────────────────────────────────────────────
#  Web Search — DuckDuckGo (gratis, tanpa API key)
# ─────────────────────────────────────────────
async def web_search(query: str, max_results: int = 5) -> list[dict]:
    """
    Cari di internet menggunakan DuckDuckGo HTML scraping.
    Return list of {title, url, snippet}.
    Fallback ke DuckDuckGo Instant Answer API jika scraping gagal.
    """
    results = []

    # Method 1: DuckDuckGo HTML (paling lengkap)
    try:
        encoded = urllib.parse.urlencode({"q": query, "kl": "id-id"})
        url     = f"https://html.duckduckgo.com/html/?{encoded}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept-Language": "id-ID,id;q=0.9,en;q=0.8",
        }
        async with httpx.AsyncClient(timeout=10.0, headers=headers, follow_redirects=True) as client:
            r = await client.get(url)
            r.raise_for_status()
            # Parse hasil dengan regex sederhana (tanpa beautifulsoup)
            snippet_re = re.compile(
                r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?'
                r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
                re.DOTALL,
            )
            for m in snippet_re.finditer(r.text):
                raw_url, raw_title, raw_snip = m.group(1), m.group(2), m.group(3)
                # Unwrap DuckDuckGo redirect URL
                if "uddg=" in raw_url:
                    real = urllib.parse.parse_qs(urllib.parse.urlparse(raw_url).query).get("uddg", [raw_url])
                    raw_url = urllib.parse.unquote(real[0])
                results.append({
                    "title":   html.unescape(re.sub(r"<[^>]+>", "", raw_title)).strip(),
                    "url":     raw_url,
                    "snippet": html.unescape(re.sub(r"<[^>]+>", "", raw_snip)).strip(),
                })
                if len(results) >= max_results:
                    break
    except Exception:
        pass

    # Method 2: DuckDuckGo Instant Answer API (fallback, lebih terbatas)
    if not results:
        try:
            params = {"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"}
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get("https://api.duckduckgo.com/", params=params)
                d = r.json()
            if not isinstance(d, dict):
                d = {}
            # AbstractText = penjelasan singkat
            if d.get("AbstractText"):
                results.append({
                    "title":   d.get("Heading", query),
                    "url":     d.get("AbstractURL", ""),
                    "snippet": d["AbstractText"][:300],
                })
            # RelatedTopics
            for topic in d.get("RelatedTopics", [])[:max_results - len(results)]:
                if isinstance(topic, dict) and topic.get("Text"):
                    results.append({
                        "title":   topic.get("Text", "")[:60],
                        "url":     topic.get("FirstURL", ""),
                        "snippet": topic.get("Text", "")[:200],
                    })
        except Exception:
            pass

    return results


async def news_search(query: str, max_results: int = 5) -> list[dict]:
    """Cari berita terbaru via DuckDuckGo News."""
    results = []
    try:
        encoded = urllib.parse.urlencode({"q": query, "iar": "news", "ia": "news"})
        url     = f"https://html.duckduckgo.com/html/?{encoded}"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        async with httpx.AsyncClient(timeout=10.0, headers=headers, follow_redirects=True) as client:
            r = await client.get(url)
            r.raise_for_status()
            news_re = re.compile(
                r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?'
                r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
                re.DOTALL,
            )
            for m in news_re.finditer(r.text):
                raw_url, raw_title, raw_snip = m.group(1), m.group(2), m.group(3)
                if "uddg=" in raw_url:
                    real = urllib.parse.parse_qs(urllib.parse.urlparse(raw_url).query).get("uddg", [raw_url])
                    raw_url = urllib.parse.unquote(real[0])
                results.append({
                    "title":   html.unescape(re.sub(r"<[^>]+>", "", raw_title)).strip(),
                    "url":     raw_url,
                    "snippet": html.unescape(re.sub(r"<[^>]+>", "", raw_snip)).strip(),
                })
                if len(results) >= max_results:
                    break
    except Exception:
        pass
    return results


def format_search_results(results: list[dict], query: str) -> str:
    """Format hasil search untuk dikirim ke Gemma 4 sebagai konteks."""
    if not results:
        return f"[Tidak ada hasil search untuk: {query}]"
    lines = [f"[Hasil search untuk: {query}]"]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']}")
        lines.append(f"   URL: {r['url']}")
        lines.append(f"   {r['snippet']}")
    return "\n".join(lines)


async def fetch_url_text(url: str, max_chars: int = 2500) -> str:
    """
    Fetch konten dari URL dan ekstrak teks bersih (tanpa tag HTML).
    Return string teks atau pesan error.
    """
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True, headers=headers) as client:
            r = await client.get(url)
            r.raise_for_status()
            ct = r.headers.get("content-type", "")
            if "text/html" not in ct:
                return f"[Konten bukan HTML: {ct}]"
            text = r.text
            # Hapus script, style, dan tag HTML
            text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r"<[^>]+>", " ", text)
            text = html.unescape(text)
            text = re.sub(r"\s+", " ", text).strip()
            return text[:max_chars]
    except httpx.TimeoutException:
        return "[Gagal mengakses URL: timeout]"
    except Exception as e:
        return f"[Gagal mengakses URL: {e}]"


def needs_web_search(text: str) -> bool:
    """Deteksi apakah pesan membutuhkan pencarian internet."""
    words = set(text.lower().split())
    return bool(words & SEARCH_TRIGGER_KEYWORDS)


# ─────────────────────────────────────────────
#  Image Helper
# ─────────────────────────────────────────────
async def fetch_image_b64(url: str) -> tuple[str, str] | None:
    """Download gambar dari URL Discord dan encode ke base64.
    Return (base64_string, mime_type) atau None jika gagal.
    """
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(url)
            r.raise_for_status()
            ct = r.headers.get("content-type", "image/png").split(";")[0].strip()
            # Hanya accept format gambar yang didukung Gemma 4
            if not ct.startswith("image/"):
                return None
            b64 = base64.b64encode(r.content).decode("utf-8")
            return b64, ct
    except Exception:
        return None


async def extract_images(message: discord.Message) -> list[dict]:
    """Ekstrak semua gambar dari attachments dan embed pesan Discord.
    Return list of Ollama image dicts siap kirim ke API.
    """
    images = []
    # Dari attachments langsung
    for att in message.attachments:
        if att.content_type and att.content_type.startswith("image/"):
            result = await fetch_image_b64(att.url)
            if result:
                b64, mime = result
                images.append({"data": b64, "mime_type": mime})
    # Dari embed (link gambar yang di-share)
    for emb in message.embeds:
        if emb.image and emb.image.url:
            result = await fetch_image_b64(emb.image.url)
            if result:
                b64, mime = result
                images.append({"data": b64, "mime_type": mime})
        if emb.thumbnail and emb.thumbnail.url and not emb.image:
            result = await fetch_image_b64(emb.thumbnail.url)
            if result:
                b64, mime = result
                images.append({"data": b64, "mime_type": mime})
    return images[:4]  # max 4 gambar per pesan (Gemma 4 limit)


# ─────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────
def extract_json(text: str) -> dict | None:
    """Ekstrak blok ```json``` pertama dari teks AI."""
    m = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            return None
    return None


def strip_json_block(text: str) -> str:
    """Hapus blok ```json``` dari teks untuk ditampilkan ke user."""
    return re.sub(r"```json.*?```", "", text, flags=re.DOTALL).strip()


def server_snapshot(guild: discord.Guild) -> str:
    """Buat ringkasan struktur server untuk konteks AI — dengan cache TTL 5 menit."""
    now = time.time()
    cached = _server_snapshot_cache.get(guild.id)
    if cached and (now - cached[0]) < SNAPSHOT_TTL:
        return cached[1]  # pakai cache, skip rebuild

    lines = [f"Server: {guild.name} | {guild.member_count} member"]
    for cat in guild.categories:
        lines.append(f"\n[{cat.name}]")
        for ch in cat.channels:
            icon = "🔊" if isinstance(ch, discord.VoiceChannel) else "💬"
            topic = f" — {ch.topic}" if getattr(ch, "topic", None) else ""
            lines.append(f"  {icon} {ch.name}{topic}")
    no_cat = [c for c in guild.channels
              if c.category is None and not isinstance(c, discord.CategoryChannel)]
    if no_cat:
        lines.append("\n[Tanpa Kategori]")
        for ch in no_cat:
            lines.append(f"  {'🔊' if isinstance(ch, discord.VoiceChannel) else '💬'} {ch.name}")
    result = "\n".join(lines)
    _server_snapshot_cache[guild.id] = (now, result)  # simpan ke cache
    return result


def invalidate_snapshot_cache(guild_id: int) -> None:
    """Hapus cache snapshot setelah perubahan channel (create/delete/rename)."""
    _server_snapshot_cache.pop(guild_id, None)


def add_to_history(user_id: int, role: str, content: str) -> None:
    """Tambah pesan ke history, buang yang paling lama jika melebihi MAX_HISTORY."""
    hist = conversation_history.setdefault(user_id, [])
    hist.append({"role": role, "content": content})
    if len(hist) > MAX_HISTORY:
        # Pertahankan konteks server di pesan pertama jika ada
        first = hist[0]
        conversation_history[user_id] = [first] + hist[-(MAX_HISTORY - 1):]


async def run_action(guild: discord.Guild, action: dict) -> str:
    """Eksekusi JSON action dari AI ke Discord API."""
    results: list[str] = []

    async def get_or_make_cat(name: str) -> discord.CategoryChannel:
        cat = discord.utils.get(guild.categories, name=name)
        if not cat:
            cat = await guild.create_category(name)
            results.append(f"📂 Kategori **{name}** dibuat")
        return cat

    async def make_ch(cat: discord.CategoryChannel | None, ch: dict) -> None:
        if discord.utils.get(guild.channels, name=ch["name"]):
            results.append(f"⚠️ `{ch['name']}` sudah ada")
            return
        if ch.get("type") == "voice":
            c = await guild.create_voice_channel(ch["name"], category=cat)
            results.append(f"🔊 {c.mention} dibuat")
        else:
            c = await guild.create_text_channel(ch["name"], category=cat, topic=ch.get("topic", ""))
            results.append(f"💬 {c.mention} dibuat")

    a = action.get("action")

    if a == "create_structure":
        cat = await get_or_make_cat(action["category"])
        for ch in action.get("channels", []):
            await make_ch(cat, ch)

    elif a == "create_channel":
        cat = await get_or_make_cat(action["category"]) if action.get("category") else None
        await make_ch(cat, action)

    elif a == "create_multiple":
        for s in action.get("structures", []):
            cat = await get_or_make_cat(s["category"])
            for ch in s.get("channels", []):
                await make_ch(cat, ch)

    elif a == "delete_channel":
        ch = discord.utils.get(guild.channels, name=action.get("name", ""))
        if ch:
            await ch.delete()
            results.append(f"🗑️ Channel `{action['name']}` dihapus")
        else:
            results.append(f"⚠️ Channel `{action.get('name')}` tidak ditemukan")

    elif a == "rename_channel":
        ch = discord.utils.get(guild.channels, name=action.get("old_name", ""))
        if ch and isinstance(ch, (discord.TextChannel, discord.VoiceChannel)):
            await ch.edit(name=action["new_name"],
                          **{"topic": action["new_topic"]} if action.get("new_topic") and isinstance(ch, discord.TextChannel) else {})
            results.append(f"✏️ `{action['old_name']}` → {ch.mention}")
        else:
            results.append(f"⚠️ Channel `{action.get('old_name')}` tidak ditemukan")

    return "\n".join(results) if results else "✅ Selesai!"


async def send_error(target, title: str, desc: str) -> None:
    """Edit placeholder embed dengan pesan error ala Yui."""
    embed = discord.Embed(title=title, description=desc, color=0xF09595)
    embed.set_footer(text="Yui minta maaf... 😢")
    await target.edit(embed=embed)


# ─────────────────────────────────────────────
#  Events
# ─────────────────────────────────────────────
@bot.event
async def on_ready() -> None:
    print(f"🌸 Yui siap! Login sebagai: {bot.user}  |  Model: {OLLAMA_MODEL}")
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="server ini dengan penuh kasih 🌸"
        )
    )
    ok, models = await ollama_online()
    if ok:
        gemma = [m for m in models if "gemma" in m.lower()]
        print(f"✅ Ollama online | Gemma models: {', '.join(gemma) or 'tidak ada'}")
        if not any(OLLAMA_MODEL in m for m in models):
            print(f"⚠️  Jalankan: ollama pull {OLLAMA_MODEL}")
    else:
        print("❌ Ollama offline — jalankan: ollama serve")
    try:
        synced = await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
        print(f"🔄 {len(synced)} slash command(s) synced")
        print("💬 Yui: 'Hai semua~! Yui sudah siap membantu Onii-chan dan Onee-chan!'")
    except Exception as e:
        print(f"❌ Sync gagal: {e}")


@bot.event
async def on_member_join(member: discord.Member) -> None:
    ch = discord.utils.get(member.guild.text_channels, name=WELCOME_CHANNEL)
    if ch:
        embed = discord.Embed(
            title=f"🌸 Yui menyambut {member.display_name}~!",
            description=(
                f"Hai {member.mention}! Yui sangat senang kamu bergabung di **{member.guild.name}**! ✨\n\n"
                f"*Yui melompat kegirangan* Selamat datang, selamat datang~!\n\n"
                "📌 Jangan lupa baca peraturan dulu ya~\n"
                "💬 Yuk ngobrol di general!\n"
                "🌸 Kalau butuh bantuan, panggil Yui saja!"
            ),
            color=YUI_COLOR,
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text=f"Member ke-{member.guild.member_count} • Yui selalu di sini untukmu~ 🌸")
        await ch.send(embed=embed)


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return

    await bot.process_commands(message)

    if not message.guild:
        return

    is_mentioned    = bot.user in message.mentions
    in_chat_channel = message.channel.id in chat_channel_ids

    if not is_mentioned and not in_chat_channel:
        return

    # Bersihkan mention dari teks
    content = message.content
    for u in message.mentions:
        content = content.replace(f"<@{u.id}>", "").replace(f"<@!{u.id}>", "")
    content = content.strip()

    # ── Track stats ──
    gid = message.guild.id
    if gid not in msg_stats:
        msg_stats[gid] = {"total": 0, "ch": collections.Counter(),
                          "usr": collections.Counter(), "hr": collections.Counter()}
    msg_stats[gid]["total"] += 1
    msg_stats[gid]["ch"][message.channel.name] += 1
    msg_stats[gid]["usr"][message.author.display_name] += 1
    msg_stats[gid]["hr"][time.localtime().tm_hour] += 1

    # ── Auto-mod: cek kata terlarang ──
    if banned_words and not is_admin(message.author):
        low = message.content.lower()
        hit = next((w for w in banned_words if w in low), None)
        if hit:
            try:
                await message.delete()
            except discord.Forbidden:
                pass
            uid_w = str(message.author.id)
            warn_count[uid_w] = warn_count.get(uid_w, 0) + 1
            _save_json(WARN_COUNT_FILE, warn_count)
            wc = warn_count[uid_w]
            lines = [
                f"*Yui mengedipkan mata khawatir* {message.author.mention}...",
                "Pesan kamu mengandung kata yang tidak boleh dipakai di sini ne~ 🌸",
                f"Peringatan ke-**{wc}** — tolong jaga sopan santun ya!",
            ]
            emb = discord.Embed(description="\n".join(lines), color=0xF09595)
            emb.set_author(name="🌸 Yui — Peringatan", icon_url=bot.user.display_avatar.url)
            if wc >= 3:
                emb.add_field(name="⚠️ Perhatian Admin",
                              value=f"{message.author.mention} sudah **{wc} peringatan**!", inline=False)
            await message.channel.send(embed=emb, delete_after=15)
            return

    # Boleh kosong teks asal ada gambar
    has_images = any(
        att.content_type and att.content_type.startswith("image/")
        for att in message.attachments
    ) or any(emb.image or emb.thumbnail for emb in message.embeds)

    if not content and not has_images:
        await message.reply(YUI_EMPTY_MSG)
        return
    if not content and has_images:
        content = "Tolong lihat dan deskripsikan gambar ini~" 

    # Cooldown
    now = time.time()
    if now - user_last_msg.get(message.author.id, 0) < COOLDOWN_SEC:
        return
    user_last_msg[message.author.id] = now

    # Cek Ollama
    ok, _ = await ollama_online()
    if not ok:
        await message.reply(YUI_OFFLINE)
        return

    uid   = message.author.id
    admin = is_admin(message.author)
    mem_u = user_memory.get(str(uid), {})
    nickname = mem_u.get("nickname", message.author.display_name)

    # Ekstrak gambar dari pesan (jika ada)
    images = await extract_images(message) if has_images else []
    if images:
        img_note = f"📸 {len(images)} gambar"
        mode_suffix = f" • {img_note}"
    else:
        img_note = ""
        mode_suffix = "" 

    # Smart thinking: task mode HANYA untuk admin
    is_task    = needs_thinking(content) and admin

    # Deteksi kebutuhan browsing (URL atau kata trigger search)
    url_matches    = _URL_RE.findall(content) if content else []
    need_search    = needs_web_search(content) and not url_matches
    is_browsing    = bool(url_matches) or need_search

    if is_browsing:
        mode_label = "🌐 Browse Mode"
    elif is_task:
        mode_label = "🧠 Task Mode"
    elif admin:
        mode_label = "⚡ Fast Mode"
    else:
        mode_label = "💬 Chat Mode"

    # Kirim placeholder Yui langsung — tidak akan timeout
    if url_matches:
        thinking_label = "🌐 *Yui sedang membaca halaman itu...* 🔍"
    elif need_search:
        thinking_label = "🔍 *Yui sedang mencari di internet...* 🌐"
    elif images:
        thinking_label = f"🖼️ *Yui sedang melihat gambarnya...* {img_note}"
    else:
        thinking_label = YUI_TASK if is_task else YUI_THINKING
    placeholder = await message.reply(embed=discord.Embed(
        description=thinking_label,
        color=0x7EC8E3 if is_browsing else (YUI_COLOR_TASK if is_task else YUI_COLOR),
    ).set_author(name=f"🌸 {bot.user.display_name}", icon_url=bot.user.display_avatar.url))

    # Pesan pertama: sertakan snapshot server sebagai konteks (hasil dari cache)
    enriched = content
    if not conversation_history.get(uid):
        snap = server_snapshot(message.guild)
        mem_note = f" | Catatan: {mem_u['notes']}" if mem_u.get("notes") else ""
        enriched = f"[Struktur server]\n{snap}\n\n[User: {nickname}{mem_note}]\n\n[Pesan]\n{content}"

    # ── Auto-browsing: inject hasil search / konten URL ke konteks ──
    if url_matches:
        # Fetch konten URL pertama yang ditemukan
        url_text = await fetch_url_text(url_matches[0])
        enriched += f"\n\n[Konten dari {url_matches[0]}]\n{url_text}"
        mode_suffix = f" • 🌐 URL"
    elif need_search:
        # Cari di internet berdasarkan isi pesan
        search_results = await web_search(content, max_results=SEARCH_MAX_RESULTS)
        if search_results:
            enriched += f"\n\n{format_search_results(search_results, content)}"
        mode_suffix = " • 🔍 Search"

    add_to_history(uid, "user", enriched)

    # Inject role context agar Yui tahu siapa yang bicara
    role_ctx = (
        f"[INFO] ADMIN bernama {nickname}. Yui boleh membantu mengatur server.\n"
        if admin else
        f"[INFO] MEMBER bernama {nickname}. Yui HANYA boleh ngobrol.\n"
    )
    # Panggil AI
    try:
        ai_text = await ask_ollama(
            conversation_history[uid],
            system=role_ctx + SYSTEM_CHAT,
            temperature=0.65 if is_task else 0.75,
            use_thinking=is_task,
            images=images if images else None,
        )
    except httpx.ReadTimeout:
        await send_error(placeholder, "⏱️ Timeout", "Terlalu lama. Ganti `OLLAMA_MODEL=gemma4:e2b` di `.env` untuk model lebih ringan.")
        return
    except httpx.ConnectError:
        await send_error(placeholder, "❌ Ollama Offline", "Tidak bisa konek. Jalankan: `ollama serve`")
        return
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        desc = (f"Ollama crash `{code}` setelah 3x retry — RAM/VRAM mungkin penuh.\n"
                "Restart Ollama atau ganti ke `gemma4:e2b` (lebih ringan)." if code in (502, 503)
                else f"HTTP error `{code}` dari Ollama.")
        await send_error(placeholder, "🔄 Server Error", desc)
        return
    except RuntimeError as e:
        await send_error(placeholder, "🔄 Semua Retry Gagal", f"{e}\n\nJalankan ulang: `ollama serve`")
        return
    except Exception as e:
        await send_error(placeholder, "❌ Error Tidak Terduga", f"`{type(e).__name__}: {e}`")
        return

    add_to_history(uid, "assistant", ai_text)

    action  = extract_json(ai_text)
    display = strip_json_block(ai_text)[:1900] or "..."

    yui_color = YUI_COLOR_TASK if (action or is_task) else YUI_COLOR
    embed = discord.Embed(description=display, color=yui_color)
    embed.set_author(name=f"🌸 {bot.user.display_name}", icon_url=bot.user.display_avatar.url)
    embed.set_footer(text=f"Yui • {mode_label}{mode_suffix} • reply atau sebut nama Yui untuk lanjut~")

    if action:
        if not admin:
            embed.color = YUI_COLOR  # member biasa — abaikan action
        else:
            try:
                result = await run_action(message.guild, action)
                invalidate_snapshot_cache(message.guild.id)  # refresh cache setelah perubahan
                embed.add_field(name="🌸 Yui berhasil!", value=result, inline=False)
                embed.color = discord.Color.green()
            except discord.Forbidden:
                embed.add_field(name="❌ Forbidden", value="Bot butuh izin **Manage Channels**.", inline=False)
                embed.color = discord.Color.red()
            except Exception as e:
                embed.add_field(name="❌ Error", value=str(e), inline=False)
                embed.color = discord.Color.red()

    await placeholder.edit(embed=embed)


# ─────────────────────────────────────────────
#  Slash Commands
# ─────────────────────────────────────────────
_guild = discord.Object(id=GUILD_ID)


@bot.tree.command(name="yui-permission", description="Lihat info permission Yui di server ini", guild=_guild)
@app_commands.checks.has_permissions(administrator=True)
async def yui_permission(interaction: discord.Interaction):
    guild  = interaction.guild
    admins = [m for m in guild.members if is_admin(m) and not m.bot]
    embed  = discord.Embed(
        title="🌸 Yui — Sistem Permission",
        description="Yui membedakan **Admin** (bisa kelola server) dan **Member** (chat only).",
        color=YUI_COLOR,
    )
    embed.add_field(
        name="🛡️ Role Admin di server ini",
        value="\n".join(f"`{r}`" for r in sorted(ADMIN_ROLE_NAMES)),
        inline=False,
    )
    embed.add_field(name="🛡️ Jumlah Admin", value=str(len(admins)), inline=True)
    embed.add_field(name="👤 Jumlah Member", value=str(guild.member_count - len(admins) - sum(1 for m in guild.members if m.bot)), inline=True)
    embed.set_footer(text="Yui menjaga server Onii-chan dengan sepenuh hati~ 🌸")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="ai-build", description="Bangun channel/kategori via chat AI",
                  guild=_guild)
@app_commands.describe(perintah="Deskripsikan apa yang ingin dibuat",
                       thinking="Aktifkan thinking mode (default: ya)")
@app_commands.checks.has_permissions(manage_channels=True)
async def ai_build(interaction: discord.Interaction, perintah: str, thinking: bool = True):
    if not is_admin(interaction.user):
        await interaction.response.send_message("🌸 Maaf, command ini hanya untuk admin server~ 😢", ephemeral=True)
        return
    await interaction.response.defer()
    ok, _ = await ollama_online()
    if not ok:
        await interaction.followup.send("❌ Ollama offline. Jalankan `ollama serve`.")
        return

    uid = interaction.user.id
    system = SYSTEM_BUILD if thinking else SYSTEM_BUILD.replace("<|think|>\n", "")
    add_to_history(uid, "user", perintah)

    try:
        ai_text = await ask_ollama(conversation_history[uid], system=system, temperature=0.6, use_thinking=thinking)
    except Exception as e:
        await interaction.followup.send(f"❌ Error: `{e}`")
        return

    add_to_history(uid, "assistant", ai_text)
    action  = extract_json(ai_text)
    display = strip_json_block(ai_text)[:3900]

    badge = "🧠 Thinking ON" if thinking else "⚡ Fast Mode"
    embed = discord.Embed(title=f"🌸 Yui Builder • {badge}", description=display, color=YUI_COLOR_TASK)
    embed.set_footer(text=f"🌸 Yui • {OLLAMA_MODEL} • {badge} • untuk {interaction.user.display_name}~")

    if action:
        try:
            result = await run_action(interaction.guild, action)
            invalidate_snapshot_cache(interaction.guild.id)
            embed.add_field(name="✅ Hasil", value=result, inline=False)
            embed.color = discord.Color.green()
        except discord.Forbidden:
            embed.add_field(name="❌ Forbidden", value="Bot butuh izin Manage Channels.", inline=False)
            embed.color = discord.Color.red()

    await interaction.followup.send(embed=embed)


@bot.tree.command(name="ai-suggest", description="Saran struktur server dari AI", guild=_guild)
@app_commands.describe(jenis="Jenis komunitas (contoh: gaming, anime, coding, musik)")
async def ai_suggest(interaction: discord.Interaction, jenis: str):
    await interaction.response.defer()
    ok, _ = await ollama_online()
    if not ok:
        await interaction.followup.send("❌ Ollama offline.")
        return
    try:
        ai_text = await ask_ollama(
            [{"role": "user", "content": f"Rekomendasi struktur server Discord untuk komunitas: {jenis}. Sertakan semua kategori, channel, dan 3 tips."}],
            system=SYSTEM_SUGGEST, temperature=0.8,
        )
    except Exception as e:
        await interaction.followup.send(f"❌ Error: `{e}`")
        return

    for i, chunk in enumerate([ai_text[j:j+3900] for j in range(0, len(ai_text), 3900)]):
        embed = discord.Embed(
            title=f"💡 Saran Server: {jenis.title()}" + (f" ({i+1})" if len(ai_text) > 3900 else ""),
            description=chunk, color=discord.Color.gold(),
        )
        if i == 0:
            embed.set_footer(text=f"{OLLAMA_MODEL} 🧠 • Gunakan /ai-build untuk langsung membuatnya!")
        await interaction.followup.send(embed=embed)


@bot.tree.command(name="ai-audit", description="AI audit & evaluasi struktur server", guild=_guild)
@app_commands.checks.has_permissions(manage_channels=True)
async def ai_audit(interaction: discord.Interaction):
    await interaction.response.defer()
    ok, _ = await ollama_online()
    if not ok:
        await interaction.followup.send("❌ Ollama offline.")
        return
    snap = server_snapshot(interaction.guild)
    try:
        ai_text = await ask_ollama(
            [{"role": "user", "content": f"Audit struktur server ini:\n\n```\n{snap}\n```"}],
            system=SYSTEM_AUDIT, temperature=0.5,
        )
    except Exception as e:
        await interaction.followup.send(f"❌ Error: `{e}`")
        return

    for i, chunk in enumerate([ai_text[j:j+3900] for j in range(0, len(ai_text), 3900)]):
        embed = discord.Embed(
            title=f"🔍 Audit: {interaction.guild.name}" + (f" ({i+1})" if len(ai_text) > 3900 else ""),
            description=chunk, color=discord.Color.orange(),
        )
        await interaction.followup.send(embed=embed)


@bot.tree.command(name="ai-name", description="Generate nama channel kreatif dari AI", guild=_guild)
@app_commands.describe(fungsi="Fungsi channel", tema="Tema/vibe (opsional, default: umum)")
async def ai_name(interaction: discord.Interaction, fungsi: str, tema: str = "umum"):
    await interaction.response.defer()
    ok, _ = await ollama_online()
    if not ok:
        await interaction.followup.send("❌ Ollama offline.")
        return
    try:
        ai_text = await ask_ollama(
            [{"role": "user", "content": f"5 opsi nama channel untuk fungsi: '{fungsi}', tema: '{tema}'. Sertakan alasan tiap nama."}],
            system=SYSTEM_NAMING, temperature=0.9,
        )
    except Exception as e:
        await interaction.followup.send(f"❌ Error: `{e}`")
        return

    embed = discord.Embed(title="✨ Ide Nama Channel", description=ai_text, color=discord.Color.purple())
    embed.add_field(name="Fungsi", value=fungsi, inline=True)
    embed.add_field(name="Tema", value=tema, inline=True)
    embed.set_footer(text=f"{OLLAMA_MODEL} 🧠 • Gunakan /ai-build untuk membuat channel!")
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="set-chat-channel",
                  description="Toggle channel ini sebagai chat bebas AI (tanpa perlu mention)",
                  guild=_guild)
@app_commands.checks.has_permissions(manage_channels=True)
async def set_chat_channel(interaction: discord.Interaction):
    cid = interaction.channel_id
    if cid in chat_channel_ids:
        chat_channel_ids.discard(cid)
        _save_json(CHAT_CHANNELS_FILE, chat_channel_ids)
        msg = f"🌸 Yui akan diam dulu di {interaction.channel.mention}~ Tapi kalau di-mention, Yui tetap muncul ya!"
    else:
        chat_channel_ids.add(cid)
        _save_json(CHAT_CHANNELS_FILE, chat_channel_ids)
        msg = f"🌸 Yui sekarang aktif di {interaction.channel.mention}! Yui senang bisa menemani Onii-chan di sini~ ✨"
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="ai-reset", description="Reset history percakapan AI kamu", guild=_guild)
async def ai_reset(interaction: discord.Interaction):
    conversation_history.pop(interaction.user.id, None)
    await interaction.response.send_message("🌸 *Yui mengedipkan mata* Baik! Yui akan melupakan percakapan kita sebelumnya~ Hehe", ephemeral=True)


@bot.tree.command(name="ollama-status", description="Cek status Ollama dan model tersedia", guild=_guild)
async def ollama_status(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    ok, models = await ollama_online()
    if ok:
        gemma = [m for m in models if "gemma" in m.lower()] or ["(tidak ada)"]
        embed = discord.Embed(title="🌸 Yui Online! Ollama berjalan dengan baik~", color=YUI_COLOR)
        embed.add_field(name="🤖 Model Aktif", value=f"`{OLLAMA_MODEL}`", inline=True)
        embed.add_field(name="🔮 Gemma Models", value="\n".join(f"`{m}`" for m in gemma), inline=True)
        embed.add_field(name="⚡ Fitur Aktif",
                        value="🧠 Thinking Mode\n📝 System Prompt\n📚 4K Context\n🔄 Auto-retry",
                        inline=False)
        if not any(OLLAMA_MODEL in m for m in models):
            embed.add_field(name="⚠️", value=f"```\nollama pull {OLLAMA_MODEL}\n```", inline=False)
    else:
        embed = discord.Embed(title="😢 Yui tidak bisa konek ke Ollama...",
                              description="Tolong jalankan dulu:\n```\nollama serve\n```\nYui akan menunggu~", color=0xF09595)
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="server-info", description="Info lengkap server", guild=_guild)
async def server_info(interaction: discord.Interaction):
    g = interaction.guild
    embed = discord.Embed(title=f"🌸 Info Server: {g.name}", description=g.description or "*Server ini belum punya deskripsi ne~*", color=YUI_COLOR)
    if g.icon:
        embed.set_thumbnail(url=g.icon.url)
    embed.add_field(name="👥 Members",   value=f"{g.member_count:,}", inline=True)
    embed.add_field(name="💬 Text",      value=len(g.text_channels),  inline=True)
    embed.add_field(name="🔊 Voice",     value=len(g.voice_channels), inline=True)
    embed.add_field(name="📂 Kategori",  value=len(g.categories),     inline=True)
    embed.add_field(name="🎭 Roles",     value=len(g.roles),          inline=True)
    embed.add_field(name="😀 Emojis",    value=len(g.emojis),         inline=True)
    embed.set_footer(text=f"ID: {g.id}")
    await interaction.response.send_message(embed=embed)


# ─────────────────────────────────────────────
#  FITUR 1: Yui Ingat Member
# ─────────────────────────────────────────────
ANIME_QUOTES = [
    ("Saya tidak menyerah! Itu bukan caraku!", "Naruto Uzumaki — Naruto"),
    ("Manusia tidak bisa mendapatkan sesuatu tanpa mengorbankan sesuatu.", "Edward Elric — FMA"),
    ("Seseorang yang tidak dapat mengorbankan apapun tidak dapat mengubah apapun.", "Armin Arlert — AOT"),
    ("Kau tidak perlu sendirian lagi.", "Kirito — Sword Art Online"),
    ("Aku tidak perlu masa depan. Aku hanya ingin saat ini yang tidak akan pernah berakhir.", "Isla — Plastic Memories"),
    ("Bahkan jika kita lupa satu hari, kenangan kita tidak akan hilang.", "Yui — Sword Art Online"),
    ("Keberanian bukan berarti tidak takut. Keberanian adalah melangkah meski kau takut.", "Erza Scarlet — Fairy Tail"),
]


@bot.tree.command(name="yui-ingat", description="Beritahu Yui nama panggilanmu", guild=_guild)
@app_commands.describe(nickname="Nama panggilanmu", catatan="Catatan tambahan (opsional)")
async def yui_ingat(interaction: discord.Interaction, nickname: str, catatan: str = ""):
    uid_str = str(interaction.user.id)
    user_memory[uid_str] = {"nickname": nickname, "notes": catatan}
    _save_json(MEMORY_FILE, user_memory)
    lines = [
        "*Yui mengangguk semangat* Baik!",
        f"Yui akan ingat kamu sebagai **{nickname}**~ \U0001f338",
    ]
    if catatan:
        lines.append(f"Catatan: *{catatan}*")
    emb = discord.Embed(description="\n".join(lines), color=YUI_COLOR)
    emb.set_author(name="\U0001f338 Yui \u2014 Memory", icon_url=bot.user.display_avatar.url)
    await interaction.response.send_message(embed=emb, ephemeral=True)


@bot.tree.command(name="yui-siapa-aku", description="Lihat apa yang Yui ingat tentang kamu", guild=_guild)
async def yui_siapa_aku(interaction: discord.Interaction):
    mem = user_memory.get(str(interaction.user.id), {})
    if not mem:
        await interaction.response.send_message(
            "\U0001f338 Yui belum tahu nama panggilanmu~ Gunakan `/yui-ingat` ya!", ephemeral=True)
        return
    desc_lines = [
        f"Nama panggilan: **{mem.get('nickname', '-')}**",
        f"Catatan: *{mem.get('notes', '-')}*",
    ]
    emb = discord.Embed(title="\U0001f338 Yui ingat tentang kamu~",
                        description="\n".join(desc_lines), color=YUI_COLOR)
    await interaction.response.send_message(embed=emb, ephemeral=True)


@bot.tree.command(name="yui-lupakan", description="Hapus data yang Yui ingat tentang kamu", guild=_guild)
async def yui_lupakan(interaction: discord.Interaction):
    user_memory.pop(str(interaction.user.id), None)
    _save_json(MEMORY_FILE, user_memory)
    await interaction.response.send_message(
        "\U0001f338 *Yui mengedipkan mata* Baik... Yui akan melupakan semuanya~ \U0001f97a",
        ephemeral=True)


# ─────────────────────────────────────────────
#  FITUR 2: Command Fun Yui
# ─────────────────────────────────────────────
@bot.tree.command(name="yui-quote", description="Minta Yui membagikan quote anime inspiratif", guild=_guild)
async def yui_quote(interaction: discord.Interaction):
    quote, source = random.choice(ANIME_QUOTES)
    emb = discord.Embed(description=f"\u201c{quote}\u201d", color=YUI_COLOR)
    emb.set_author(name="\U0001f338 Yui \u2014 Quote Anime", icon_url=bot.user.display_avatar.url)
    emb.set_footer(text=f"\u2014 {source}")
    await interaction.response.send_message(embed=emb)


@bot.tree.command(name="yui-roll", description="Lempar dadu! Yui akan hitung hasilnya~", guild=_guild)
@app_commands.describe(sisi="Jumlah sisi dadu (default 6)", jumlah="Jumlah dadu (default 1, max 10)")
async def yui_roll(interaction: discord.Interaction, sisi: int = 6, jumlah: int = 1):
    sisi   = max(2, min(sisi, 100))
    jumlah = max(1, min(jumlah, 10))
    results = [random.randint(1, sisi) for _ in range(jumlah)]
    total   = sum(results)
    roll_str = " + ".join(str(r) for r in results)
    desc = f"**{roll_str}**" + (f" = **{total}**" if jumlah > 1 else "")
    emb = discord.Embed(title=f"\U0001f3b2 Yui melempar {jumlah}d{sisi}~!", description=desc, color=YUI_COLOR)
    if total == jumlah:
        emb.set_footer(text="Wah, nilai terkecil! Nasib ne~ \U0001f605")
    elif total == sisi * jumlah:
        emb.set_footer(text="SEMPURNA! Yui ikut senang~! \U0001f389")
    else:
        emb.set_footer(text="Semoga sesuai keinginanmu~ \U0001f338")
    await interaction.response.send_message(embed=emb)


@bot.tree.command(name="yui-pilih", description="Bingung pilih? Biar Yui yang putuskan~!", guild=_guild)
@app_commands.describe(pilihan="Tulis pilihan dipisah koma, contoh: pizza, sushi, ramen")
async def yui_pilih(interaction: discord.Interaction, pilihan: str):
    options = [o.strip() for o in pilihan.split(",") if o.strip()]
    if len(options) < 2:
        await interaction.response.send_message(
            "\U0001f338 Masukkan minimal 2 pilihan ya, dipisah koma~!", ephemeral=True)
        return
    chosen = random.choice(options)
    desc_lines = [
        "*Yui menutup mata dan menunjuk...*",
        "",
        f"\u2728 **{chosen}** \u2728",
        "",
        "*Yui yakin ini pilihan terbaik!* \U0001f338",
    ]
    emb = discord.Embed(description="\n".join(desc_lines), color=YUI_COLOR)
    emb.set_author(name="\U0001f338 Yui \u2014 Pilihkan", icon_url=bot.user.display_avatar.url)
    emb.set_footer(text=f"Dari {len(options)} pilihan: {', '.join(options)}")
    await interaction.response.send_message(embed=emb)


# ─────────────────────────────────────────────
#  FITUR 3: Auto-moderasi — Kelola Kata Terlarang
# ─────────────────────────────────────────────
@bot.tree.command(name="yui-mod-add", description="Tambah kata terlarang (admin only)", guild=_guild)
@app_commands.describe(kata="Kata atau frasa yang ingin dilarang")
async def yui_mod_add(interaction: discord.Interaction, kata: str):
    if not is_admin(interaction.user):
        await interaction.response.send_message("\U0001f338 Maaf, hanya admin~", ephemeral=True)
        return
    banned_words.add(kata.lower().strip())
    _save_json(BANNED_WORDS_FILE, banned_words)
    await interaction.response.send_message(
        f"\u2705 Kata `{kata}` ditambahkan. Yui akan menjaga server~ \U0001f338", ephemeral=True)


@bot.tree.command(name="yui-mod-remove", description="Hapus kata dari daftar terlarang (admin only)", guild=_guild)
@app_commands.describe(kata="Kata yang ingin dihapus")
async def yui_mod_remove(interaction: discord.Interaction, kata: str):
    if not is_admin(interaction.user):
        await interaction.response.send_message("\U0001f338 Maaf, hanya admin~", ephemeral=True)
        return
    banned_words.discard(kata.lower().strip())
    _save_json(BANNED_WORDS_FILE, banned_words)
    await interaction.response.send_message(
        f"\u2705 Kata `{kata}` dihapus dari daftar.", ephemeral=True)


@bot.tree.command(name="yui-mod-list", description="Lihat semua kata terlarang (admin only)", guild=_guild)
async def yui_mod_list(interaction: discord.Interaction):
    if not is_admin(interaction.user):
        await interaction.response.send_message("\U0001f338 Maaf, hanya admin~", ephemeral=True)
        return
    if not banned_words:
        await interaction.response.send_message("\U0001f4cb Belum ada kata terlarang.", ephemeral=True)
        return
    words_str = "\n".join(f"\u2022 `{w}`" for w in sorted(banned_words))
    emb = discord.Embed(title="\U0001f6e1\ufe0f Daftar Kata Terlarang",
                        description=words_str, color=0xF09595)
    emb.set_footer(text=f"Total: {len(banned_words)} kata")
    await interaction.response.send_message(embed=emb, ephemeral=True)


@bot.tree.command(name="yui-mod-warn", description="Lihat peringatan seorang member (admin only)", guild=_guild)
@app_commands.describe(member="Member yang ingin dicek")
async def yui_mod_warn(interaction: discord.Interaction, member: discord.Member):
    if not is_admin(interaction.user):
        await interaction.response.send_message("\U0001f338 Maaf, hanya admin~", ephemeral=True)
        return
    wc = warn_count.get(str(member.id), 0)
    await interaction.response.send_message(
        f"\U0001f4cb {member.mention} memiliki **{wc}** peringatan.", ephemeral=True)


# ─────────────────────────────────────────────
#  FITUR 4: Statistik Server
# ─────────────────────────────────────────────
@bot.tree.command(name="yui-stats", description="Lihat statistik aktivitas server", guild=_guild)
async def yui_stats(interaction: discord.Interaction):
    gid   = interaction.guild.id
    stats = msg_stats.get(gid)
    emb   = discord.Embed(title=f"\U0001f4ca Statistik \u2014 {interaction.guild.name}", color=YUI_COLOR)
    emb.set_author(name="\U0001f338 Yui \u2014 Stats", icon_url=bot.user.display_avatar.url)

    if not stats or stats.get("total", 0) == 0:
        emb.description = "*Yui belum punya data~ Data tersedia setelah ada aktivitas* \U0001f338"
        await interaction.response.send_message(embed=emb)
        return

    total = stats["total"]
    emb.add_field(name="\U0001f4ac Total Pesan", value=f"**{total:,}**", inline=True)
    emb.add_field(name="\U0001f465 Members",    value=f"**{interaction.guild.member_count:,}**", inline=True)
    emb.add_field(name="\U0001f4c2 Channels",   value=f"**{len(interaction.guild.text_channels)}**", inline=True)

    top_ch = stats["ch"].most_common(5)
    if top_ch:
        ch_str = "\n".join(f"`#{n}` \u2014 {c:,} pesan" for n, c in top_ch)
        emb.add_field(name="\U0001f525 Channel Paling Aktif", value=ch_str, inline=False)

    top_usr = stats["usr"].most_common(5)
    if top_usr:
        usr_str = "\n".join(f"**{n}** \u2014 {c:,} pesan" for n, c in top_usr)
        emb.add_field(name="\u2b50 Member Paling Aktif", value=usr_str, inline=False)

    top_hr = stats["hr"].most_common(3)
    if top_hr:
        hr_str = " \u2022 ".join(f"Jam {h:02d}:00 ({c})" for h, c in top_hr)
        emb.add_field(name="\u23f0 Waktu Paling Ramai", value=hr_str, inline=False)

    emb.set_footer(text="Statistik sejak bot terakhir nyala \u2022 Yui selalu memantau~ \U0001f338")
    await interaction.response.send_message(embed=emb)


# ─────────────────────────────────────────────
#  FITUR 5: Yui Search Internet
# ─────────────────────────────────────────────
SYSTEM_SEARCH = '<|think|>\nKamu adalah Yui dari SAO yang baru saja mencari informasi di internet.\nKamu diberikan hasil search yang sudah dirangkum.\nTugasmu: sampaikan informasinya dengan gaya Yui yang hangat dan natural.\n- Rangkum informasi penting dari hasil search\n- Sebutkan sumber (URL) jika relevan\n- Jika tidak ada hasil, akui dengan jujur\n- Gunakan Bahasa Indonesia\n- Tetap pakai gaya Yui: antusias, ramah, sesekali ekspresi imut\n'

@bot.tree.command(name="yui-cari", description="Minta Yui mencari informasi di internet", guild=_guild)
@app_commands.describe(query="Apa yang ingin dicari?")
async def yui_cari(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    results = await web_search(query, max_results=5)
    ctx     = format_search_results(results, query)
    ok, _   = await ollama_online()
    if not ok:
        if not results:
            await interaction.followup.send(f"🌸 Maaf, tidak ada hasil untuk `{query}`~ 😭")
            return
        emb = discord.Embed(title=f"🔍 Hasil: {query}", color=YUI_COLOR)
        emb.set_author(name="🌸 Yui — Search", icon_url=bot.user.display_avatar.url)
        for r in results[:5]:
            emb.add_field(name=r["title"][:100], value=f"{r['snippet'][:150]}\n[Link]({r['url']})", inline=False)
        await interaction.followup.send(embed=emb)
        return
    try:
        ai_text = await ask_ollama(
            [{"role": "user", "content": f"Tolong rangkum hasil search ini:\n\n{ctx}"}],
            system=SYSTEM_SEARCH, temperature=0.7,
        )
    except Exception:
        ai_text = ctx
    emb = discord.Embed(description=ai_text[:3900] or "*Tidak ada hasil ne~*", color=YUI_COLOR)
    emb.set_author(name="🌸 Yui — Search", icon_url=bot.user.display_avatar.url)
    if results:
        links = "\n".join(f"[{r['title'][:50]}]({r['url']})" for r in results[:3] if r.get("url"))
        if links:
            emb.add_field(name="🔗 Sumber", value=links, inline=False)
    emb.set_footer(text=f"Query: {query} • DuckDuckGo • 🌸")
    await interaction.followup.send(embed=emb)


@bot.tree.command(name="yui-berita", description="Minta Yui mencari berita terbaru", guild=_guild)
@app_commands.describe(topik="Topik berita yang ingin dicari")
async def yui_berita(interaction: discord.Interaction, topik: str):
    await interaction.response.defer()
    results = await news_search(topik, max_results=5)
    if not results:
        await interaction.followup.send(
            f"🌸 Maaf, Yui tidak menemukan berita tentang `{topik}`~ 😭")
        return
    emb = discord.Embed(title=f"📰 Berita: {topik}", color=0xAFA9EC)
    emb.set_author(name="🌸 Yui — Berita", icon_url=bot.user.display_avatar.url)
    for r in results[:5]:
        snip = r["snippet"][:120] + ("..." if len(r["snippet"]) > 120 else "")
        url_p = f"\n[Baca]({r['url']})" if r.get("url") else ""
        emb.add_field(name=r["title"][:80] or "Berita", value=snip + url_p, inline=False)
    emb.set_footer(text="DuckDuckGo News • 🌸 Yui selalu update untukmu~")
    await interaction.followup.send(embed=emb)


@bot.tree.command(name="yui-tanya-web", description="Tanya Yui sesuatu, Yui akan search dulu sebelum menjawab", guild=_guild)
@app_commands.describe(pertanyaan="Pertanyaanmu untuk Yui")
async def yui_tanya_web(interaction: discord.Interaction, pertanyaan: str):
    await interaction.response.defer()
    results  = await web_search(pertanyaan, max_results=4)
    ctx      = format_search_results(results, pertanyaan)
    ok, _    = await ollama_online()
    if not ok:
        await interaction.followup.send("🌸 Maaf Onii-chan, Ollama offline~")
        return
    uid      = interaction.user.id
    nickname = user_memory.get(str(uid), {}).get("nickname", interaction.user.display_name)
    prompt   = f"Pertanyaan dari {nickname}: {pertanyaan}\n\nHasil search:\n{ctx}\n\nJawab dengan gaya Yui."
    try:
        ai_text = await ask_ollama(
            [{"role": "user", "content": prompt}],
            system=SYSTEM_SEARCH, temperature=0.7,
        )
    except Exception:
        ai_text = ctx[:1000]
    emb = discord.Embed(description=ai_text[:3900], color=YUI_COLOR)
    emb.set_author(name="🌸 Yui — Tanya Web", icon_url=bot.user.display_avatar.url)
    if results:
        links = "\n".join(f"[{r['title'][:50]}]({r['url']})" for r in results[:3] if r.get("url"))
        if links:
            emb.add_field(name="🔗 Referensi", value=links, inline=False)
    emb.set_footer(text="🌸 Yui selalu mencari yang terbaik untukmu~")
    await interaction.followup.send(embed=emb)


# ─────────────────────────────────────────────
#  Error Handler
# ─────────────────────────────────────────────

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    msg = ("❌ Kamu tidak punya izin untuk command ini."
           if isinstance(error, app_commands.MissingPermissions)
           else f"❌ Error: {error}")
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


# ─────────────────────────────────────────────
#  Run
# ─────────────────────────────────────────────
if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token or token == "your_bot_token_here":
        print("❌ DISCORD_TOKEN belum diset di .env!")
    else:
        print(f"🌸 Yui sedang bangun dari tidur... (model: {OLLAMA_MODEL})")
        bot.run(token)