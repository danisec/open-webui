# Read Aloud: pemilih chapter

## Tujuan

Read Aloud memutar pesan utuh. Untuk jawaban panjang, pengguna tidak bisa
melompat ke bagian tertentu, dan kalau berhenti di tengah harus mengulang dari
awal.

Fitur ini menambahkan daftar chapter di bawah tombol Read Aloud: setiap entri
bisa diklik untuk memutar bagian itu saja.

## Kenapa chapter, bukan per kalimat

Chunk asli (dengan `split_on = "punctuation"`) adalah **per kalimat** — tepat
untuk latensi streaming, tapi buruk sebagai daftar navigasi: jawaban panjang
menghasilkan 30+ entri berdurasi 3-4 detik.

`buildTtsChapters()` menggabungkan chunk berurutan sampai mencapai
`targetSeconds` (default **25 detik**). Hasilnya daftar pendek dengan durasi
yang berarti.

```
kalimat 1 (3.1s) + kalimat 2 (3.5s) + ... >= 25s  -> chapter 1
kalimat berikutnya ...                            -> chapter 2
```

## Estimasi durasi

Audio belum ada saat daftar dibangun (baru dibuat saat diputar), jadi durasi
diestimasi dari jumlah kata:

```ts
seconds = words / 2.7 + 0.4
```

- `2.7` kata/detik ≈ kecepatan bicara Supertonic (terukur ~39.6s audio untuk
  107 kata → 2.7 kata/s).
- `+0.4` biaya tetap per request: model warmup + encoder pass mendominasi chunk
  pendek, sehingga pembagian murni menyepelekan chunk pendek.

**Estimasi hanya dipakai untuk label dan pengelompokan** — tidak pernah untuk
kontrol playback. Kesalahan estimasi tidak mempengaruhi pemutaran.

## Perubahan

### `src/lib/utils/index.ts`

```ts
export type TtsChapter = {
    partIndexes: number[];   // indeks chunk asli
    preview: string;         // ~80 char untuk label
    seconds: number;         // estimasi durasi
    wordCount: number;
};

buildTtsChapters(parts, targetSeconds = 25): TtsChapter[]
estimateSpeechSeconds(text): number
formatSpeechDuration(seconds): string   // "1:42"
```

### `src/lib/components/chat/Messages/ResponseMessage.svelte`

- State: `showChapters`, `chapters`, `activeChapter`
- `toggleChapters()` — bangun daftar saat pertama dibuka
- `speakChapter(i)` — hitung rentang chunk, panggil `speak({fromPart, toPart})`
- `speak(range?)` — `allParts.slice(fromPart, toPart)` sehingga hanya chapter
  itu yang disintesis
- Tombol ikon daftar di toolbar (di samping tombol Read Aloud)
- Panel daftar disisipkan **di dalam** container toolbar

### i18n

`Play from chapter`, `Play all` di `en-US/translation.json`.

## Perilaku

| Aksi | Hasil |
|---|---|
| Klik "Play all" | Putar seluruh pesan (sama seperti tombol Read Aloud) |
| Klik chapter N | Putar dari awal chapter N **sampai akhir pesan** |
| Chapter sedang diputar | Baris disorot, dan sorotan **mengikuti** playback |
| Klik chapter lain saat memutar | Chapter sebelumnya berhenti, yang baru mulai |
| Tekan stop | `activeChapter` kembali -1 |

### Kenapa chapter diputar sampai akhir pesan

Versi pertama membatasi pemutaran pada batas chapter (`slice(fromPart, toPart)`).
Klik chapter 2 berhenti sendiri setelah ~25 detik. Itu terbaca sebagai bug, bukan
fitur: pengguna memilih titik mulai, bukan titik akhir.

Sekarang `speakChapter` hanya mengirim `fromPart`; `toPart` dihilangkan sehingga
`slice` berjalan sampai `allParts.length`.

### Sorotan chapter mengikuti playback

Karena playback melintasi batas chapter, `AudioQueue` melaporkan posisi chunk
setiap kali satu chunk mulai:

```ts
onChunkStarted: ((position: number) => void) | null = null;
private playedCount = 0;

// di next()
const position = this.playedCount++;
try { this.onChunkStarted?.(position); } catch {}
```

Komponen memetakan posisi relatif ke indeks absolut:

```ts
const absolute = (range?.fromPart ?? 0) + position;
const idx = chapters.findIndex((c) => c.partIndexes.includes(absolute));
if (idx !== -1) activeChapter = idx;
```

`playedCount` di-reset di `#halt()` agar tidak terbawa antar sesi. Callback
dibungkus `try/catch` supaya listener yang error tidak menghentikan playback.


## Batasan yang diketahui

1. **Belum ada resume presisi.** Kalau berhenti di tengah chapter, klik ulang
   memutar chapter itu dari awal — bukan dari detik terakhir. Resume presisi
   butuh menyimpan `{chunkIndex, offset}` dan tidak me-revoke blob URL.
2. **Durasi estimasi**, bukan terukur. Bisa meleset ±15% untuk teks dengan
   banyak angka/singkatan.
3. **Daftar dibangun ulang setiap kali dibuka** (`toggleChapters`), karena
   konten pesan bisa berubah (edit/regenerate). Murah — hanya regex + hitung
   kata.
4. **Tidak ada penanda di badan teks.** Navigasi hanya lewat panel daftar.

## Catatan build

Saat menyisipkan panel, blok `{#if}`/`{/if}` sempat tidak seimbang dan build
gagal (`Unexpected block closing tag`). Panel harus berada **di dalam**
container toolbar (indentasi 7 tab, sebelum `</div>` penutup toolbar), bukan
setelahnya.

Verifikasi cepat keseimbangan blok sebelum build:

```python
lines = open('ResponseMessage.svelte').read().split('\n')
d = 0
for l in lines:
    s = l.strip()
    d += s.count('{#if') + s.count('{#each') + s.count('{#key') + s.count('{#await')
    d -= s.count('{/if}') + s.count('{/each}') + s.count('{/key}') + s.count('{/await')
print(d)   # harus 0
```
