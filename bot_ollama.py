import asyncio
import json
import os
import re
import time

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
COOLDOWN_SEC = int(os.getenv("COOLDOWN_SECONDS", "3"))
MAX_HISTORY  = int(os.getenv("MAX_HISTORY", "20"))

# State in-memory
conversation_history: dict[int, list[dict]]  = {}
chat_channel_ids:     set[int]               = set()
user_last_msg:        dict[int, float]       = {}
_server_snapshot_cache: dict[int, tuple[float, str]] = {}  # guild_id → (timestamp, snapshot)
SNAPSHOT_TTL = 300  # cache snapshot selama 5 menit

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
) -> str:
    """
    Kirim request ke Ollama dengan streaming + auto-retry (3x, exponential backoff).
    - use_thinking=True  → thinking mode ON, num_ctx=4096 (tugas kompleks)
    - use_thinking=False → thinking mode OFF, num_ctx=2048 (chat biasa, lebih cepat)
    """
    MAX_RETRIES  = 3
    RETRY_DELAYS = [3, 6, 12]

    # Pilih system prompt: dengan atau tanpa <|think|>
    effective_system = system if use_thinking else system.replace("<|think|>\n", "").replace("<|think|>", "")

    all_messages: list[dict] = []
    if effective_system:
        all_messages.append({"role": "system", "content": effective_system})
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
            cleaned = re.sub(r"<\|channel\>thought.*?<channel\|>", "", raw, flags=re.DOTALL)
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
        if not any("gemma4" in m for m in models):
            print("⚠️  Jalankan: ollama pull gemma4")
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
    ch = discord.utils.get(member.guild.text_channels, name="👋・welcome")
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

    if not content:
        await message.reply(YUI_EMPTY_MSG)
        return

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

    # Smart thinking: task mode HANYA untuk admin
    is_task    = needs_thinking(content) and admin
    mode_label = "🧠 Task Mode" if is_task else ("⚡ Fast Mode" if admin else "💬 Chat Mode")

    # Kirim placeholder Yui langsung — tidak akan timeout
    thinking_label = YUI_TASK if is_task else YUI_THINKING
    placeholder = await message.reply(embed=discord.Embed(
        description=thinking_label,
        color=YUI_COLOR_TASK if is_task else YUI_COLOR,
    ).set_author(name=f"🌸 {bot.user.display_name}", icon_url=bot.user.display_avatar.url))

    # Pesan pertama: sertakan snapshot server sebagai konteks (hasil dari cache)
    enriched = content
    if not conversation_history.get(uid):
        snap = server_snapshot(message.guild)
        enriched = f"[Struktur server saat ini]\n{snap}\n\n[Pesan]\n{content}"

    add_to_history(uid, "user", enriched)

    # Inject role context agar Yui tahu siapa yang bicara
    role_ctx = (
        "[INFO] User ini adalah ADMIN. Yui boleh membantu mengatur server.\n"
        if admin else
        "[INFO] User ini adalah MEMBER BIASA. Yui HANYA boleh ngobrol, tidak eksekusi perubahan server.\n"
    )
    # Panggil AI
    try:
        ai_text = await ask_ollama(
            conversation_history[uid],
            system=role_ctx + SYSTEM_CHAT,
            temperature=0.65 if is_task else 0.75,
            use_thinking=is_task,
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
    embed.set_footer(text=f"Yui • {mode_label} • reply atau sebut nama Yui untuk lanjut~")

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
        ai_text = await ask_ollama(conversation_history[uid], system=system, temperature=0.6)
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
        msg = f"🌸 Yui akan diam dulu di {interaction.channel.mention}~ Tapi kalau di-mention, Yui tetap muncul ya!"
    else:
        chat_channel_ids.add(cid)
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
        if not any("gemma4" in m for m in models):
            embed.add_field(name="⚠️", value="```\nollama pull gemma4\n```", inline=False)
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