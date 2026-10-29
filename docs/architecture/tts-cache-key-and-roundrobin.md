# TTS: cache key stabil + round-robin upstream

Image: `v0.12.0-tts-cache-rr-arm64-v39` (backend-only di atas v38).
**Belum di-deploy** — dibangun saat Read Aloud sedang dipakai.

## Masalah 1: Read Aloud ulang selalu generate ulang

### Gejala

Klik Read Aloud dua kali pada pesan yang sama tetap lambat seperti pertama.
Cache backend sehat kalau diuji langsung:

```
panggilan identik 3x:  2.244s -> 0.014s -> 0.012s   (cache hit)
```

Tapi di pemakaian nyata:

```
total transcode 40 menit: 305
hash unik               : 305     <-- nol pengulangan
```

### Akar masalah

`speech()` menghitung cache key dari **byte mentah** request:

```python
name = hashlib.sha256(
    body                                       # <-- raw HTTP body
    + str(engine).encode()
    + str(await Config.get('audio.tts.model')).encode()
    + (b':slim' if USE_SLIM else b'')
).hexdigest()
```

Byte mentah berubah karena urutan key, spasi, atau field opsional yang
muncul/hilang. Setiap variasi menghasilkan digest berbeda untuk audio yang
**identik**.

Bukti: dua file cache berisi JSON sama persis tapi nama hash berbeda:

```
6e5140aa....json -> {"input":"Uji cache...","voice":"F1",...}
a66c6179....json -> {"input":"Uji cache...","voice":"F1",...}
```

### Perbaikan

Parse body, ambil hanya field yang **benar-benar mempengaruhi audio**, lalu
serialisasi deterministik:

```python
key_material = {
    'input':  parsed_body.get('input', ''),
    'voice':  parsed_body.get('voice', '') or config_voice,
    'model':  config_model,
    'params': config_params,      # response_format dsb dari config, bukan request
    'engine': engine,
    'slim':   USE_SLIM,
}
name = sha256(JSONCodec.dumps(_canonical(key_material)))
```

`_canonical()` mengurutkan key secara rekursif karena `JSONCodec`
(orjson/stdlib) mempertahankan insertion order — dua dict dengan data sama
bisa menghasilkan byte berbeda.

### Efek samping yang disengaja

`response_format` sekarang diambil dari **config**, bukan dari request. Ini
membuat kunci tidak bisa dipengaruhi klien, dan mencegah dua permintaan
identik menghasilkan audio berbeda hanya karena klien mengirim field tambahan.

### Hasil uji logika

```
2 panggilan identik        : sama
params urutan beda         : sama
teks beda                  : beda  (benar)
voice beda                 : beda  (benar)
params beda (wav vs mp3)   : beda  (benar)
```

## Masalah 2: trafik menumpuk di satu pod
### Gejala

```
instabox    (vkg29) POST=293   <-- 97%
instabox2   (9wzcv) POST=12
instabox3   (f58qr) POST=6
```

Pod `vkg29` memakai 700-880m CPU sementara dua lainnya idle 2-3m.

Dikirim 9 request ke **tiga node berbeda** — semuanya mendarat di pod yang sama.

### Akar masalah

OWUI hanya punya satu base URL (`http://100.106.8.115:30788/v1`). NodePort di
cluster ini berbasis **iptables** (bukan IPVS), yang memilih endpoint per
koneksi dan tidak merata untuk trafik rendah.

### Perbaikan

`audio.tts.openai.api_base_url` kini menerima **daftar dipisah koma**:

```
http://100.106.8.115:30788/v1,http://100.86.76.1:30788/v1,http://100.97.236.81:30788/v1
```

`_next_tts_base_url()` merotasi kursor per request. `_tts_base_url_candidates()`
memberi urutan coba mulai dari kursor, sehingga satu node mati hanya
memperlambat request, bukan menggagalkannya.

Satu URL tetap bekerja seperti sebelumnya (tanpa rotasi).

### Hasil uji round-robin

```
input   : 'http://a:7788/v1, http://b:7788/v1 ,http://c:7788/v1'
split   : ['http://a:7788/v1', 'http://b:7788/v1', 'http://c:7788/v1']
rotasi 6x: a, b, c, a, b, c
```

Normalisasi bekerja: spasi dipangkas, urutan dipertahankan, duplikat dibuang.

## Robustness

| Risiko | Penanganan |
|---|---|
| Satu node mati | failover ke kandidat berikutnya, `log.warning` per kegagalan |
| Semua node mati | `_raise_tts_error` dengan exception terakhir |
| Config kosong | `_split_base_urls` kembalikan `[]`, request gagal jelas |
| URL duplikat/spasi | dinormalisasi & di-dedup |
| Race pada kursor | `threading.Lock` |
| Body bukan JSON | `parsed_body = {}` (tidak crash) |

## Rollback

```bash
# image
cp ~/backups/open-webui.container.bak.<ts> ~/.config/containers/systemd/open-webui.container
systemctl --user daemon-reload && systemctl --user restart open-webui
```

Source: `/tmp/audio_backend.bak.1792807812`

Kembalikan base URL ke satu nilai bila ingin mematikan round-robin saja:

```
audio.tts.openai.api_base_url = "http://100.106.8.115:30788/v1"
```

## Catatan

- Perubahan **backend saja**; frontend tidak berubah dari v38.
- `Dockerfile.v39` memverifikasi patch di dalam image sebelum commit
  (`grep` + `ast.parse`).
- Semua perubahan uncommitted di branch `custom-v0.12.0`.
