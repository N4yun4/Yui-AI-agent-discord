# Yui AI Agent Discord

Bot Discord berbasis AI lokal menggunakan **Ollama** — tanpa API key, tanpa biaya. Bot ini memiliki persona **Yui dari Sword Art Online (SAO)**. Yui bisa menemani ngobrol member biasa dengan gaya bahasanya yang imut, dan dapat membantu Admin membangun serta mengelola struktur server Discord secara otomatis melalui chat.

## Fitur & Slash Commands

- `/yui-permission` — Lihat info permission Yui di server ini (Hanya Admin)
- `/ai-build` — Chat dengan AI untuk membuat channel & kategori secara otomatis (Hanya Admin)
- `/ai-suggest` — Minta saran struktur server untuk jenis komunitas tertentu
- `/ai-audit` — AI audit & evaluasi struktur server (Hanya Admin)
- `/ai-name` — Generate opsi nama channel kreatif dari AI
- `/set-chat-channel` — Toggle channel saat ini menjadi tempat chat bebas dengan Yui tanpa menyebut (mention) Yui (Hanya Admin)
- `/ai-reset` — Reset history percakapan AI kamu
- `/ollama-status` — Cek status Ollama dan ketersediaan model 
- `/server-info` — Tampilkan informasi lengkap server

## Cara Kerja

Bot mengirim perintah pengguna ke model AI lokal via Ollama API (`/api/chat`). AI merespons dengan teks penjelasan dan JSON action yang kemudian dieksekusi langsung di Discord (membuat channel, kategori, dll).

## Prasyarat

- Python 3.10+
- [Ollama](https://ollama.com) terinstall dan berjalan (`ollama serve`)
- Model sudah di-pull, direkomendasikan: `ollama pull gemma4`
- Discord bot token dengan permission **Manage Channels**

## Instalasi

1. Clone repo ini:
   ```bash
   git clone https://github.com/N4yun4/Yui-AI-agent-discord.git
   cd Yui-AI-agent-discord
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Copy dan isi file konfigurasi:
   ```bash
   cp .env.example .env
   ```
   Edit `.env`:
   ```
   DISCORD_TOKEN=token_bot_discord_kamu
   GUILD_ID=id_server_discord_kamu
   OLLAMA_URL=http://localhost:11434
   OLLAMA_MODEL=gemma4
   ```

4. Jalankan bot:
   ```bash
   python bot_ollama.py
   ```

## Konfigurasi

| Variabel | Keterangan | Default |
|---|---|---|
| `DISCORD_TOKEN` | Token bot Discord | — |
| `GUILD_ID` | ID server Discord | — |
| `OLLAMA_URL` | Alamat Ollama lokal | `http://localhost:11434` |
| `OLLAMA_MODEL` | Model yang digunakan | `gemma4` |
| `COOLDOWN_SECONDS` | Jeda antar perintah (detik) | `3` |
| `MAX_HISTORY` | Maks riwayat percakapan per user | `20` |

## Mendapatkan Token Discord

1. Buka [Discord Developer Portal](https://discord.com/developers/applications)
2. Buat aplikasi baru → Bot → Reset Token
3. Aktifkan intent: `Message Content`, `Server Members`
4. Invite bot ke server dengan permission `Manage Channels`

## Mendapatkan Guild ID

Aktifkan Developer Mode di Discord → klik kanan server → Copy Server ID.

## Lisensi

MIT
