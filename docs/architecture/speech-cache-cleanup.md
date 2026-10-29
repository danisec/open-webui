# Speech cache: pertumbuhan dan pembersihan

## Masalah

Open WebUI menyimpan **setiap** hasil sintesis TTS ke disk, permanen:

```
/app/backend/data/cache/audio/speech/
  <sha256>.mp3     ← audio
  <sha256>.json    ← metadata request
```

Tidak ada TTL, tidak ada batas ukuran, tidak ada endpoint untuk membersihkan.
Kode hanya menulis dan membaca — tidak pernah menghapus:

| Baris (`routers/audio/__init__.py`) | Operasi |
|---|---|
| 83-84 | deklarasi + `mkdir` |
| 459-480 | `_write_tts_cache` (tulis) |
| 907-908 | susun path |
| 917 | baca (cache hit) |
| 527, 697, 733, 808 | panggil tulis |

Empat periodic task di `main.py` tidak satu pun menyentuh cache audio:
`socket_authentication`, `usage_pool_cleanup` (token usage), `session_pool_cleanup`
(koneksi HTTP), `scheduler_worker_loop` (automation).

## Kondisi terukur (25 Okt 2026)

```
106 MB · 3.098 file (1.549 MP3 + 1.549 JSON)
tertua 17 Okt · terbaru 25 Okt
```

Pertumbuhan per hari (MP3 saja):

| Tanggal | Ukuran | File |
|---|---|---|
| 17 Okt | 11,7 MB | 212 |
| 18 Okt | 2,0 MB | 35 |
| 19 Okt | 2,7 MB | 53 |
| 23 Okt | 2,6 MB | 53 |
| **24 Okt** | **50,4 MB** | **763** |
| **25 Okt** | **26,9 MB** | **433** |

Pemakaian normal ~2–3 MB/hari. Read Aloud intensif: 25–50 MB/hari.

## Supertonic tidak menyimpan apa pun

Diperiksa ketiga pod: `/tmp` masing-masing 8 KB. Supertonic mengembalikan audio
di memori lewat HTTP response, tanpa menulis ke disk. **Seluruh akumulasi ada di
OWUI.**

## Kenapa cache ini jangan dihapus sembarangan

```
tanpa cache : ~1,9 s per chunk
dengan cache: ~0,015 s per chunk   (127× lebih cepat)
```

Menghapus cache hanya memperlambat Read Aloud sekali; tidak ada data pengguna
yang hilang.

---

## Dua lapis pembersihan

### Lapis 1 — cron systemd (sudah terpasang)

`prune-speech-cache.timer` → harian 03:30, hapus file `-mtime +14`.

```bash
systemctl --user list-timers prune-speech-cache.timer
journalctl --user -u prune-speech-cache --since -7d
```

Skrip: `~/.local/bin/prune-speech-cache.py`

Aman karena:
- Hanya menyentuh `cache/audio/speech`, bukan data pengguna
- Menghapus berpasangan (mp3 + json); menyisakan salah satu = file tak terpakai
- `Nice=10` + `IOSchedulingClass=idle` agar tidak mengganggu layanan

**Batas:** tidak ada plafon ukuran. Kalau pemakaian melonjak, 14 hari tetap bisa
besar.

### Lapis 2 — periodic cleanup di OWUI (v42)

`open_webui/utils/speech_cache.py`, dijalankan tiap jam dari lifespan.

Dua batas, diterapkan berurutan dari yang tertua:

1. **age** — hapus entri lebih tua dari `audio.tts.cache_max_age_days` (default 14)
2. **size** — selama direktori melebihi `audio.tts.cache_max_size_mb`
   (default 500 MB), hapus entri tertua

Nilai `0` menonaktifkan batas terkait. Keduanya `0` = cache tidak pernah dibersihkan.

Env override:

```
AUDIO_TTS_CACHE_MAX_AGE_DAYS=14
AUDIO_TTS_CACHE_MAX_SIZE_MB=500
SPEECH_CACHE_CLEANUP_INTERVAL=3600
SPEECH_CACHE_CLEANUP_INITIAL_DELAY=300
```

#### Penghapusan berpasangan

Entri dikelompokkan berdasarkan nama tanpa ekstensi, lalu **semua** file dalam
grup dihapus bersama. Alasannya:

- Hapus `.mp3` saja → `.json` jadi orphan
- Hapus `.json` saja → `.mp3` tak bisa dilayani, karena lookup menghitung ulang
  key dari request dan butuh keduanya

Umur entri diambil dari **sibling tertua**, supaya `.json` yang ditulis ulang
tidak membuat audio lama tetap hidup.

#### Hasil uji

| Skenario | Hasil |
|---|---|
| age=14d, entri 20d/5d/1d | entri 20d terhapus, 5d & 1d bertahan ✅ |
| size=3MB dari 5×1MB | sisa 2 entri terbaru, tanpa orphan ✅ |
| age=0, size=0 | tidak menghapus apa pun (opt-out) ✅ |

---

## Verifikasi

```bash
# cron
systemctl --user list-timers prune-speech-cache.timer
journalctl --user -u prune-speech-cache -n 5

# periodic OWUI (v42)
podman exec open-webui grep -c periodic_speech_cache_cleanup \
  /app/backend/open_webui/utils/speech_cache.py
journalctl --user -u open-webui | grep -i "speech cache"

# ukuran sekarang
podman exec open-webui du -sm /app/backend/data/cache/audio/speech
```

## Rollback

**Cron:**
```bash
systemctl --user disable --now prune-speech-cache.timer
```

**Periodic OWUI:** deploy image v41 (perubahan ini backend-only, frontend sama).
```bash
sed -i 's|speech-cache-prune-arm64-v42|chapters-continue-arm64-v41|' \
  ~/.config/containers/systemd/open-webui.container
systemctl --user daemon-reload && systemctl --user restart open-webui
```

**Mematikan tanpa rollback:** set kedua batas ke `0` lewat Settings → Audio → TTS.

## Catatan

Cache ini **bukan** satu-satunya yang tumbuh. Di mesin yang sama:

```
983 MB  cache/embedding
605 MB  cache/whisper
1,7 MB  cache/tiktoken
```

Ketiganya belum punya pembersihan otomatis. `embedding` yang paling besar dan
layak diperiksa berikutnya.
