# Read Aloud: streaming per-kalimat (fix latensi)

## Masalah

Read Aloud menunggu **seluruh** pesan selesai disintesis sebelum memutar
audio apa pun. Untuk jawaban 6 kalimat, tidak ada suara selama ~12.8 detik.

## Akar masalah

1. `ResponseMessage.svelte` mengumpulkan semua chunk ke array `urls`, lalu
   meng-enqueue setelah loop selesai. `AudioQueue` sebenarnya sudah siap
   streaming (`minBuffer`, `waiting`, resume otomatis) tetapi tidak dipakai.
2. `audio.tts.split_on = "paragraph"` membuat satu paragraf panjang menjadi
   **satu** request sintesis. Paragraf 489 karakter butuh ~7.3 detik dalam
   satu request — tidak ada yang bisa diputar lebih awal.

## Perubahan

### 1. Config (tanpa build)

```
audio.tts.split_on: "paragraph" -> "punctuation"
```

Di-set langsung di tabel `config` OWUI. Nilai disimpan sebagai JSON, jadi
string harus di-quote: `json.dumps("punctuation")`.

### 2. `src/lib/components/chat/Messages/ResponseMessage.svelte`

Enqueue dipindah ke dalam loop sintesis:

```diff
-const urls: string[] = [];
+let enqueued = 0;
 for (const [, sentence] of messageContentParts.entries()) {
     ...
-    if (blob.size) urls.push(URL.createObjectURL(blob));
+    if (!blob.size) continue;
+    const url = URL.createObjectURL(blob);
+    if (signal.aborted) { URL.revokeObjectURL(url); break; }
+    $audioQueue.enqueue(url);
+    enqueued++;
 }
 ...
-if (urls.length === 0) { ... }
+if (enqueued === 0) { ... }
-for (const url of urls) $audioQueue.enqueue(url);
 $audioQueue?.markProducerDone();
```

Blok `browser-kokoro` (engine lain) **tidak** disentuh.

### 3. `src/lib/utils/audio.ts`

```diff
-minBuffer = 2;
+minBuffer = 1;

-maxWait = 45000;
+maxWait = 15000;
```

`minBuffer = 1` aman karena sintesis berjalan ~3x lebih cepat daripada
kecepatan putar (RTF ~0.3): kalimat 41 karakter menghasilkan 3.13 s audio
dalam 1.54 s, jadi buffer terisi jauh sebelum chunk pertama habis diputar.
Menunggu 2 chunk hanya menambah ~1.6 s TTFA tanpa manfaat robustness.

`maxWait` diturunkan karena dengan streaming, producer yang macet terlihat
sebagai keheningan di tengah kalimat. 15 detik cukup untuk melewati sintesis
lambat, tidak terlalu lama membiarkan user menunggu.

## Hasil terukur

6 kalimat, 489 karakter, 3 worker k3s:

| Skenario | TTFA | Total |
|---|---|---|
| Sebelum (paragraph, tunggu semua) | 12.8 s | 12.8 s |
| Sesudah (punctuation, streaming) | **~1.9–3.8 s** | ~12.8 s |

TTFA = time to first audio. `minBuffer = 2` berarti audio mulai setelah 2
chunk; kalau ingin lebih cepat bisa set `minBuffer = 1` (aman karena margin
buffer chunk pertama 2.72 s).

## Kenapa TIDAK paralel

Diukur: paralel 3 worker membuat TTFA **lebih lambat** (3.18 s vs 1.90 s),
karena chunk #3 menyerobot slot worker padahal yang dibutuhkan baru #1 dan #2.

Selain itu buffer tidak pernah khabis — audio dihasilkan ~3x lebih cepat
daripada kecepatan putar (RTF 0.3), margin tumbuh dari 2.72 s ke 26.51 s.

3 worker lebih berguna untuk melayani **3 user berbeda**, bukan 1 user dengan
3 request.

## Robustness

| Risiko | Penanganan |
|---|---|
| 1 chunk gagal | `catch` + `continue`; chunk lain tetap diputar |
| Semua chunk gagal | `enqueued === 0` -> toast error |
| Batal saat sintesis | `signal.aborted` dicek sebelum & sesudah enqueue |
| Object URL bocor saat batal | `URL.revokeObjectURL(url)` eksplisit |
| Producer macet | `maxWait` 15 s lalu berhenti bersih |
| Buffer habis sementara | `AudioQueue.waiting` + resume otomatis |

## Rollback

```bash
cd /home/dani/AI-agent/open-webui
git checkout src/lib/components/chat/Messages/ResponseMessage.svelte src/lib/utils/audio.ts
# atau dari backup:
cp /tmp/ResponseMessage.svelte.bak.1792804665 src/lib/components/chat/Messages/ResponseMessage.svelte
cp /tmp/audio.ts.bak.1792804665 src/lib/utils/audio.ts
```

Config:

```sql
UPDATE config SET value='"paragraph"' WHERE key='audio.tts.split_on';
```

Lalu rebuild frontend + deploy image sebelumnya.

## Catatan

- Semua perubahan fork tetap **uncommitted** di branch `custom-v0.12.0`.
- `svelte-check` melaporkan ~6064 error **pre-existing** di codebase
  (mayoritas `routes/s/[id]`, `routes/watch`), bukan dari patch ini.
- Build frontend: `npm run build` di `/home/dani/AI-agent/open-webui`.
- Image derived bersifat COPY-only: `rm -rf /app/build && COPY build /app/build`.
