# Support & Resistance + EMA Cross Bot (H1)

Bot trading otomatis untuk Bybit Futures (USDT Perpetual), timeframe H1 saja.
Strategi: **Support & Resistance + EMA4/10 cross + TEST1/TEST2 (engulfing)**,
hasil riset & backtest `backtest_snr.py` (45 koin, 1 tahun H1) — koin
dengan Win Rate ≥ 50% yang dipakai (15 koin dari 45 koin yang dites).

⚠️ Backtest ≠ jaminan hasil live. Selalu tes di **Testnet** dulu sebelum live.

## Cara kerja strategi

1. **Deteksi level** (basis body candle H1):
   - Support: c1 bearish (close<open) → c2 bullish (close>open). Resistance:
     c1 bullish → c2 bearish. Level = close[c1].
   - **KANAN**: 1 candle setelah c1,c2 (c3) — wick-nya tidak boleh menyentuh
     level sama sekali. Tidak ada syarat kiri, tidak ada syarat wick c1/c2.
   - **EMA CROSS**: salah satu dari candle c2, c3, atau c4 wajib jadi
     penyebab cross EMA4/EMA10 (dari close H1) yang searah — Support →
     GOLDEN CROSS, Resistance → DEATH CROSS. Kalau tidak ada satupun di
     c2-c4, level gugur dari awal.

2. **TEST1 + TEST2 (engulfing)**:
   - TEST1: candle pertama setelah c3 yang wick/body-nya menyentuh atau
     melebihi level. Tidak ada syarat arah candle.
   - TEST2: candle tepat setelah TEST1, harus **engulfing** — Support: ujung
     body TEST2 harus lebih tinggi dari high candle TEST1. Resistance: ujung
     body TEST2 harus lebih rendah dari low candle TEST1. Kalau gagal, level
     gugur (dicoba sekali saja, tidak dicari TEST1 berikutnya).

3. **Entry — LIMIT** di ujung wick candle TEST1 (Long → high candle TEST1,
   Short → low candle TEST1). Limit **baru dipasang nyata** di Bybit begitu
   harga sudah masuk radius `APPROACH_PCT` (default 2%) dari harga itu —
   sebelum itu sinyal cuma "menunggu" (belum ada order sama sekali, supaya
   margin tidak nyangkut lama di order yang masih jauh). Kalau setelah armed
   harga menjauh lagi >2% sebelum sempat fill, order dibatalkan (balik
   menunggu, tetap hidup). Tiap level hanya dipakai 1x.

4. **Trailing stop native Bybit**: aktif otomatis setelah profit mencapai
   rasio `TRAIL_ACT_R` dari jarak entry–SL, lebar trailing `TRAIL_STOP` ×
   jarak.

## Setup & Deploy (Railway)

1. Push folder ini ke repo GitHub kamu.
2. Buat project baru di [Railway](https://railway.app), connect ke repo tersebut.
3. Railway otomatis pakai `railway.toml` / `Procfile` → menjalankan `python bot_ema_flip.py`.
4. Di tab **Variables**, isi minimal:
   - `API_KEY` — API key Bybit
   - `API_SECRET` — API secret Bybit
   - `TESTNET` — `true` untuk testnet, `false` untuk live
5. Deploy. Bot langsung jalan begitu deploy selesai.
6. Buka `https://<project-kamu>.up.railway.app/view` untuk lihat log entry per koin, atau `/logs` untuk log mentah realtime, `/ohlc` untuk unduh data candle yang sedang dilihat bot (diagnostik).

## Environment Variables

| Var | Default | Keterangan |
|---|---|---|
| `API_KEY` / `API_SECRET` | — | **wajib** |
| `TESTNET` | `false` | `true` untuk testnet |
| `EMA_FAST` | `4` | EMA cepat, dicek cross-nya di candle c2 |
| `EMA_SLOW` | `10` | EMA lambat |
| `APPROACH_PCT` | `0.02` | limit baru dipasang nyata kalau harga dlm radius ini (2%) dari entry (wick TEST1) |
| `SL_PCT` | `0.02` | jarak SL dari entry |
| `TRAIL_ACT_R` | `4.0` | trailing aktif di rasio 1:N dari SL |
| `TRAIL_STOP` | `1.0` | lebar trailing = N × jarak(entry,SL) |
| `RISK_PCT` | `0.01` | risk per trade (1% equity) |
| `LEVERAGE` | `25` | leverage |
| `MAX_CONCURRENT` | `10` | slot maksimum (posisi + armed) |
| `MIN_DIST_PCT` | `0.002` | floor keamanan SL minimum dari entry |
| `ALLOW_HEDGE` | `true` | wajib `true` kalau mau Long & Short bareng |

Beberapa var Railway lain yang mungkin masih ada dari setup sebelumnya
(`RSI_GATE_*`, `SWING_GATE_ENABLED`, `FLIP_MIN_R`, `FEE_ENTRY_PCT`,
`FEE_EXIT_PCT`, `FILTER_*`, `BACKTEST_DAYS`, `CACHE_DIR`, `INITIAL_BALANCE`)
**tidak dipakai lagi** oleh bot ini — aman dibiarkan atau dihapus dari
Railway, tidak akan menyebabkan error.

⚠️ **Hedge Mode** diaktifkan otomatis bot saat start (dibutuhkan karena
strategi ini punya 2 arah, Long dan Short, bisa aktif bersamaan di koin
berbeda maupun koin yang sama).

## Menjalankan lokal (opsional)

```bash
pip install -r requirements.txt
cp .env.example .env   # lalu isi API_KEY & API_SECRET
export $(cat .env | xargs)   # linux/mac
python bot_ema_flip.py
```

## State & restart

Bot menyimpan progress (sinyal yang sudah diproses per koin, sinyal yang
masih menunggu radius 2%, order yang sudah armed, posisi terbuka) ke
`bot_state.json`. Kalau Railway redeploy/restart, bot akan lanjut dari
state terakhir, bukan mulai dari nol — dan **tidak** akan membanjiri order
dari sinyal historis lama (ada mekanisme inisialisasi sekali di run pertama
yang menandai histori tanpa entry).

## Peringatan

- Selalu mulai dengan `RISK_PCT` kecil dan `MAX_CONCURRENT` terbatas saat pertama kali live.
- Backtest dilakukan di 45 koin, 1 tahun H1 — hanya koin dengan Win Rate ≥ 50% yang masuk `SYMBOLS` di kode (15 koin). Performa live bisa berbeda dari backtest.
