# BBMA HistData Fetcher (public)

Repo **public** ini hanya berisi kode pengambil data. Ia tidak menyimpan kredensial, tidak menyimpan data hasil unduhan, dan tidak menjalankan order trading.

Tujuannya: mengambil **Generic ASCII Tick Data** dari HistData.com untuk `EURUSD`, `GBPUSD`, `AUDUSD`, dan `XAUUSD` mulai **2021-01-01 sampai bulan berjalan**, lalu menulis hasil agregasi ke repo **private** `ahyat786/bbma-ea-analysis` pada folder `data/histdata_ticks/`.

## Kenapa public?

GitHub Actions hanya gratis/unlimited untuk repo public pada akun Free. Kode fetcher aman dipublikasikan karena HistData tidak memerlukan login. Repo private tetap dipakai untuk data dan backtest BBMA.

## Yang dihasilkan

Untuk tiap simbol:

- `SYMBOL_H4_Mid.json`, `SYMBOL_H4_Bid.json`, `SYMBOL_H4_Ask.json`
- `SYMBOL_H1_*.json`
- `SYMBOL_M15_*.json`
- `SYMBOL_summary.json` (jumlah tick, spread rata-rata, first/last UTC, jumlah bar)
- `histdata_tick_summary.json`

Format candle identik dengan cache XBTFX, sehingga `engine.js` di repo private tidak perlu diubah.

Catatan timezone: HistData memakai EST tetap tanpa DST. Fetcher menambah +5 jam supaya `open_time` menjadi UTC.

## Setup (sekali)

1. Di repo public ini, buat Actions secret `PRIVATE_REPO_TOKEN`.
   - Isi: GitHub classic PAT dengan scope `repo` (atau fine-grained token dengan akses Contents=write ke `bbma-ea-analysis`).
   - Token dipakai hanya oleh `actions/checkout` untuk repo private dan `git push`.
2. Jalankan **Actions → Fetch HistData 2021 → now → Run workflow**.
3. Workflow juga jalan otomatis tiap Minggu 03:17 UTC.

## Perilaku

- Per-month parts disimpan di Actions cache, jadi run berikutnya hanya mengambil bulan baru.
- Jika HistData belum merilis bulan berjalan, fetcher tetap menulis data sampai bulan terakhir yang tersedia dan mencatat `last_utc` di summary.
- Data mentah ZIP tidak disimpan; stream dan langsung diagregasi ke H4/H1/H4 Bid/Ask.

## Sumber

- HistData download page: https://www.histdata.com/download-free-forex-data/
- Referensi format/endpoint: https://github.com/rpitchumani/histdata
