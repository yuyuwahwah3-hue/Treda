"""
============================================================
BOT SUPPORT & RESISTANCE + EMA4/10 CROSS + TEST1/TEST2 ENGULFING (H1)
============================================================
Strategi final hasil riset & backtest (backtest_snr.py, 1 tahun H1, 45
koin -> 23 koin dgn ROI% positif yang dipakai bot ini).

RINGKASAN STRATEGI
------------------
1. DETEKSI LEVEL (H1, basis body candle):
   - Support: c1 bearish (close<open) lalu c2 bullish (close>open).
     Resistance: c1 bullish lalu c2 bearish. Level = close[c1].
   - KANAN: 1 candle setelah c1,c2 (c3) -- wick-nya tidak boleh menyentuh
     level sama sekali. TANPA syarat kiri, TANPA syarat wick c1/c2.
   - SYARAT EMA CROSS: salah satu dari candle c2, c3, atau c4 WAJIB jadi
     penyebab cross EMA4/EMA10 (dari close H1) yang searah -- Support ->
     GOLDEN CROSS, Resistance -> DEATH CROSS. Kalau tidak ada satupun di
     c2-c4, level gugur dari awal.

2. TEST1 + TEST2 (engulfing), setelah level terbentuk:
   - TEST1: candle pertama SETELAH c3 yang wick/body-nya menyentuh ATAU
     melebihi level (patokan = level itu sendiri). Tidak ada syarat arah
     candle.
   - TEST2: candle TEPAT SETELAH TEST1, harus ENGULFING -- Support: ujung
     body TEST2 harus lebih TINGGI dari high candle TEST1. Resistance:
     ujung body TEST2 harus lebih RENDAH dari low candle TEST1. Kalau
     gagal, level gugur (hanya dicoba sekali, tidak dicari TEST1
     berikutnya lagi).

3. ENTRY -- LIMIT di UJUNG WICK candle TEST1 (Long -> high candle TEST1,
   Short -> low candle TEST1). Limit BARU DIPASANG NYATA di Bybit begitu
   harga sudah masuk radius APPROACH_PCT (default 2%) dari entry_price --
   sebelum itu sinyal cuma "menunggu" (belum ada order terpasang sama
   sekali, supaya margin tidak nyangkut lama di order yang masih jauh).
   Kalau setelah armed harga menjauh lagi >2% sebelum sempat fill, order
   DIBATALKAN (balik ke menunggu, tetap hidup, bisa armed lagi kalau
   mendekat lagi). Tiap level HANYA dipakai 1x (tidak ada re-entry).
   SL = ADAPTIF, di ujung wick candle TEST2 (candle engulfing), floor
   minimum SL_MIN_PCT dari entry kalau wick-nya sempit.

4. TRAILING STOP native Bybit:
   - Aktif otomatis setelah profit mencapai TRAIL_ACT_R x jarak(entry,SL).
   - Lebar trailing = TRAIL_STOP x jarak.
============================================================
"""

import pandas as pd
import numpy as np
from pybit.unified_trading import HTTP
import os
import time
import sys
import threading
import json
from http.server import HTTPServer, BaseHTTPRequestHandler

# ============================================================
# LOG SERVER — akses via https://xxx.up.railway.app/logs /entries /view /ohlc
# ============================================================
LOG_FILE   = "bot.log"
ENTRY_FILE = "entries.log"

def log_entry(text):
    import datetime
    ts = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=7)).strftime('[%Y-%m-%d %H:%M:%S] ')
    try:
        with open(ENTRY_FILE, 'a', encoding='utf-8') as f:
            f.write(ts + text.replace('\n', '\n' + ' ' * len(ts)) + '\n')
    except Exception:
        pass
    print(text)

def _fmt_wib(ts_ms):
    """epoch ms (UTC, dari kline Bybit) -> 'YYYY-MM-DD HH:MM' (WIB, UTC+7)."""
    import datetime
    dt = datetime.datetime.fromtimestamp(int(ts_ms) / 1000, tz=datetime.timezone.utc) + datetime.timedelta(hours=7)
    return dt.strftime('%Y-%m-%d %H:%M')

class _Tee:
    def __init__(self):
        self._out     = sys.__stdout__
        self._file    = open(LOG_FILE, 'a', buffering=1, encoding='utf-8')
        self._newline = True
    def write(self, msg):
        import datetime
        out = ''
        for ch in msg:
            if self._newline and ch != '\n':
                out += (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=7)).strftime('[%H:%M:%S] ')
                self._newline = False
            out += ch
            if ch == '\n':
                self._newline = True
        self._out.write(out)
        self._file.write(out)
    def flush(self):
        self._out.flush()
        self._file.flush()

sys.stdout = _Tee()

LAST_OHLC = {}

def _parse_log_blocks(text):
    import re
    ts_re   = re.compile(r'^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] ?')
    coin_re = re.compile(r'\b([A-Z0-9]{2,15}USDT)\b')
    blocks, cur = [], None
    for line in text.split('\n'):
        m = ts_re.match(line)
        if m:
            if cur is not None:
                blocks.append(cur)
            cur = {'ts': m.group(1), 'lines': [line]}
        elif cur is not None:
            cur['lines'].append(line)
    if cur is not None:
        blocks.append(cur)
    out = []
    for b in blocks:
        block_text = '\n'.join(b['lines']).rstrip('\n')
        cm = coin_re.search(block_text)
        out.append({'ts': b['ts'], 'coin': (cm.group(1) if cm else None), 'text': block_text})
    return out

class _LogHandler(BaseHTTPRequestHandler):
    def _send(self, body, ctype='text/plain; charset=utf-8', extra=None):
        if isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Access-Control-Allow-Origin', '*')
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        import datetime as _dt
        path = self.path.split('?', 1)[0]
        query = {}
        if '?' in self.path:
            for kv in self.path.split('?', 1)[1].split('&'):
                if '=' in kv:
                    k, v = kv.split('=', 1); query[k] = v

        if path == '/entries':
            try:
                with open(ENTRY_FILE, 'r', encoding='utf-8') as f:
                    data = f.read()
            except Exception:
                data = '(belum ada entry)'
            return self._send(data)

        if path == '/view':
            try:
                with open(ENTRY_FILE, 'r', encoding='utf-8') as f:
                    raw = f.read()
            except Exception:
                raw = ''
            blocks = _parse_log_blocks(raw)
            coin_last_ts = {}
            for b in blocks:
                if b['coin']:
                    coin_last_ts[b['coin']] = b['ts']
            coins_sorted = sorted(coin_last_ts.keys(), key=lambda c: coin_last_ts[c], reverse=True)
            html = ("<!doctype html><html><head><meta charset='utf-8'>"
                    "<meta name='viewport' content='width=device-width, initial-scale=1, maximum-scale=1'>"
                    "<title>Bot Log</title>"
                    "<style>"
                    "*{box-sizing:border-box}"
                    "html,body{width:100%;overflow-x:hidden}"
                    "body{font-family:'Courier New',monospace;background:#0d0d0d;color:#ddd;margin:0;padding:0;"
                    "font-size:13px}"
                    ".topbar{display:flex;flex-wrap:wrap;align-items:center;gap:6px;padding:8px 10px;"
                    "background:#181818;border-bottom:1px solid #333;position:sticky;top:0;z-index:2}"
                    ".tabbtn{background:#222;color:#ccc;border:1px solid #444;border-radius:6px;padding:8px 14px;"
                    "cursor:pointer;font-size:13px;flex:0 0 auto}"
                    ".tabbtn.active{background:#2a6;color:#fff;border-color:#2a6}"
                    ".minilinks{display:flex;gap:10px;margin-left:auto;flex-wrap:wrap}"
                    "a.mini{color:#7ad;text-decoration:none;font-size:12px;white-space:nowrap}"
                    ".wrap{display:flex;flex-direction:column;min-height:calc(100vh - 48px)}"
                    "@media(min-width:700px){.wrap{flex-direction:row;height:calc(100vh - 48px)}}"
                    ".sidebar{display:none;border-bottom:1px solid #333;background:#151515;"
                    "max-height:38vh;overflow-y:auto}"
                    "@media(min-width:700px){.sidebar{max-height:none;height:100%;width:180px;"
                    "border-bottom:none;border-right:1px solid #333;flex:0 0 180px}}"
                    ".sidebar.show{display:block}"
                    ".coinbtn{display:block;width:100%;text-align:left;background:none;border:none;color:#ccc;"
                    "padding:10px 14px;cursor:pointer;font-size:13px;border-bottom:1px solid #222}"
                    ".coinbtn:active,.coinbtn:hover{background:#222}"
                    ".coinbtn.active{background:#26a;color:#fff}"
                    ".main{flex:1;overflow-y:auto;overflow-x:hidden;padding:8px 10px;white-space:pre-wrap;"
                    "word-break:break-word;font-size:12px;line-height:1.5;-webkit-overflow-scrolling:touch}"
                    ".blk{padding:5px 0;border-bottom:1px solid #1c1c1c}"
                    "@media(min-width:700px){.main{font-size:13px;padding:10px 16px}}"
                    "</style></head><body>"
                    "<div class='topbar'>"
                    "<button id='tab-semua' class='tabbtn active' onclick=\"setTab('semua')\">Semua</button>"
                    "<button id='tab-percoin' class='tabbtn' onclick=\"setTab('percoin')\">Per Koin</button>"
                    "<div class='minilinks'>"
                    "<a class='mini' href='/entries'>raw</a>"
                    "<a class='mini' href='/logs'>console</a>"
                    "<a class='mini' href='/ohlc'>ohlc</a>"
                    "</div></div>"
                    "<div class='wrap'>"
                    "<div id='sidebar' class='sidebar'></div>"
                    "<div id='main' class='main'></div>"
                    "</div>"
                    "<script>"
                    f"const BLOCKS = {json.dumps(blocks)};"
                    f"const COINS = {json.dumps(coins_sorted)};"
                    "let mode='semua', selCoin=null;"
                    "function render(){"
                    "  const main=document.getElementById('main');"
                    "  const sidebar=document.getElementById('sidebar');"
                    "  document.getElementById('tab-semua').className='tabbtn'+(mode==='semua'?' active':'');"
                    "  document.getElementById('tab-percoin').className='tabbtn'+(mode==='percoin'?' active':'');"
                    "  if(mode==='semua'){"
                    "    sidebar.className='sidebar';"
                    "    main.innerHTML=BLOCKS.map(b=>'<div class=\"blk\">'+esc(b.text)+'</div>').join('');"
                    "  } else {"
                    "    sidebar.className='sidebar show';"
                    "    sidebar.innerHTML=COINS.map(c=>'<button class=\"coinbtn'+(c===selCoin?' active':'')+'\" "
                    "onclick=\"selectCoin(\\''+c+'\\')\">'+c+'</button>').join('');"
                    "    if(!selCoin){main.innerHTML='<i>Pilih koin di atas/kiri.</i>';}"
                    "    else{"
                    "      const filtered=BLOCKS.filter(b=>b.coin===selCoin);"
                    "      main.innerHTML=filtered.length?filtered.map(b=>'<div class=\"blk\">'+esc(b.text)+'</div>').join('')"
                    "        :'<i>Belum ada log untuk '+selCoin+'.</i>';"
                    "    }"
                    "  }"
                    "  main.scrollTop=main.scrollHeight;"
                    "}"
                    "function esc(s){const d=document.createElement('div');d.innerText=s;return d.innerHTML;}"
                    "function setTab(m){mode=m;render();}"
                    "function selectCoin(c){selCoin=c;render();}"
                    "render();"
                    "</script></body></html>")
            return self._send(html, 'text/html; charset=utf-8')

        if path == '/logs':
            try:
                with open(LOG_FILE, 'r', encoding='utf-8') as f:
                    data = ''.join(f.readlines()[-200:])
            except Exception:
                data = ''
            return self._send(data)

        if path == '/ohlc':
            sym = query.get('symbol'); tf = query.get('tf', '60')
            if sym:
                df = LAST_OHLC.get((sym, str(tf)))
                if df is None:
                    return self._send(f"(data {sym} tf{tf} belum ada — tunggu bot scan dulu)")
                rows = ["ts_ms,waktu_WIB,open,high,low,close,volume"]
                for _, r in df.iterrows():
                    t = _dt.datetime.utcfromtimestamp(int(r['ts']) / 1000) + _dt.timedelta(hours=7)
                    rows.append(f"{int(r['ts'])},{t:%Y-%m-%d %H:%M:%S},"
                                f"{r['open']:.10g},{r['high']:.10g},{r['low']:.10g},{r['close']:.10g},{r.get('vol',0):.10g}")
                csv = "\n".join(rows)
                fname = f"{sym}_tf{tf}_{_dt.datetime.utcnow():%Y%m%d_%H%M}.csv"
                return self._send(csv, 'text/csv; charset=utf-8',
                                  {'Content-Disposition': f'attachment; filename="{fname}"'})
            keys = sorted(LAST_OHLC.keys())
            if not keys:
                return self._send("<h3>Belum ada data. Tunggu bot scan beberapa detik lalu refresh.</h3>"
                                  "<a href='/ohlc'>refresh</a>", 'text/html; charset=utf-8')
            syms = sorted({k[0] for k in keys})
            html = ["<html><head><meta charset='utf-8'><title>Unduh OHLC</title>",
                    "<style>body{font-family:sans-serif;background:#111;color:#eee;padding:16px}"
                    "a.btn{display:inline-block;margin:3px;padding:6px 10px;background:#2a6;color:#fff;"
                    "text-decoration:none;border-radius:5px}h4{margin:14px 0 4px}</style></head><body>",
                    "<h2>Unduh OHLC (data yg dilihat bot)</h2>",
                    "<p><a href='/logs'>/logs</a> · <a href='/entries'>/entries</a> · <a href='/view'>/view</a> · <a href='/ohlc'>refresh</a></p>"]
            for s in syms:
                html.append(f"<h4>{s}</h4>")
                if (s, '60') in LAST_OHLC:
                    html.append(f"<a class='btn' href='/ohlc?symbol={s}&tf=60'>⬇ H1 (60m)</a>")
            html.append("</body></html>")
            return self._send("\n".join(html), 'text/html; charset=utf-8')

        if path == '/':
            return self._send("<html><body style='font-family:sans-serif;background:#111;color:#eee;padding:16px'>"
                              "<h2>Bot EMA-Cross Reversal + Flip Protection</h2>"
                              "<p><a href='/view' style='color:#6cf'>/view</a> · "
                              "<a href='/logs' style='color:#6cf'>/logs</a> · "
                              "<a href='/entries' style='color:#6cf'>/entries</a> · "
                              "<a href='/ohlc' style='color:#6cf'><b>/ohlc — unduh data OHLC</b></a></p></body></html>",
                              'text/html; charset=utf-8')

        self.send_response(404); self.end_headers()

    def log_message(self, *a):
        pass

PORT = int(os.environ.get('PORT', 8080))
threading.Thread(
    target=lambda: HTTPServer(('0.0.0.0', PORT), _LogHandler).serve_forever(),
    daemon=True
).start()
print(f"📡 Log server jalan di port {PORT} → /logs")

# ============================================================
# CONFIG
# ============================================================
API_KEY    = os.environ.get('API_KEY', '')
API_SECRET = os.environ.get('API_SECRET', '')
CATEGORY   = "linear"
TESTNET    = os.environ.get('TESTNET', 'false').lower() == 'true'

if not API_KEY or not API_SECRET:
    raise ValueError("❌ API_KEY dan API_SECRET belum diset!")

session = HTTP(testnet=TESTNET, api_key=API_KEY, api_secret=API_SECRET)

# ── Strategy params (Support & Resistance + EMA cross, hasil backtest_snr.py) ──
TIMEFRAME        = "60"    # H1 saja
EMA_FAST         = int(os.environ.get('EMA_FAST', 4))
EMA_SLOW         = int(os.environ.get('EMA_SLOW', 10))
APPROACH_PCT     = float(os.environ.get('APPROACH_PCT', 0.02))   # limit baru dipasang nyata kalau harga dlm radius 2% dari entry_price
TRAIL_ACT_R      = float(os.environ.get('TRAIL_ACT_R', 3.0))   # trailing aktif di rasio 1:TRAIL_ACT_R dari SL (disamakan dgn backtest TRAIL_ACTIVATE_R)
TRAIL_STOP       = float(os.environ.get('TRAIL_STOP', 1.0))    # lebar trailing = TRAIL_STOP x jarak(entry,SL)
TRAIL_TIMEOUT_DAYS = 3      # safety net: force-close kalau peak macet N hari (None = matikan)
RISK_PCT         = float(os.environ.get('RISK_PCT', 0.01))     # risk per trade = 1% equity
LEVERAGE         = int(os.environ.get('LEVERAGE', 50))   # disamakan dgn backtest
MIN_ORDER_USD    = 5.0
ORDER_BUMP_FLOOR = 4.0
MAX_CONCURRENT   = int(os.environ.get('MAX_CONCURRENT', 10))   # sengaja beda dari backtest (unlimited) -- manual override risk management modal live
MIN_DIST_PCT     = float(os.environ.get('MIN_DIST_PCT', 0.01))   # floor keamanan SL minimum dari entry
                             # -- disamakan dgn SL_MIN_PCT bot live (1%), dipakai
                             # place_limit_order sbg pengaman terakhir kalau somehow
                             # dist yg terkirim masih < floor ini
SL_MIN_PCT       = float(os.environ.get('SL_MIN_PCT', 0.01))   # SL ADAPTIF: dipasang di wick candle TEST2 (engulfing),
                             # floor minimum 1% dari entry (BEDA dari backtest yg 0.3%)
EXPIRE_CANDLES   = 4   # level kadaluarsa kalau limit tak tersentuh dlm N candle H1 setelah TEST2 (disamakan dgn backtest)
ENABLE_TEST3     = os.environ.get('ENABLE_TEST3', 'true').lower() == 'true'   # AKTIF (default, disamakan dgn backtest): entry pindah ke wick TEST3 kalau TEST1 blm fill 1 candle H1 setelah TEST2

ALLOW_HEDGE = os.environ.get('ALLOW_HEDGE', 'true').lower() == 'true'
def _pidx(side):
    return (1 if side == "Buy" else 2) if ALLOW_HEDGE else 0
def _akey(coin, direction):
    return f"{coin}|{direction}" if ALLOW_HEDGE else coin

# Disinkronkan dengan SYMBOLS di backtest_web.py (semua koin yang dipakai
# backtest, bukan cuma subset WR>=50%).
SYMBOLS = [
    'PENDLEUSDT', 'PYTHUSDT', 'BLURUSDT', '1000BONKUSDT', 'WUSDT',
    'USUALUSDT', 'ETHFIUSDT', 'LABUSDT', 'IOTAUSDT', '1000FLOKIUSDT',
    'HBARUSDT', 'PLUMEUSDT', 'BERAUSDT', 'MASKUSDT', 'ESPORTSUSDT',
    'IMXUSDT', 'CRVUSDT', 'ACHUSDT', 'FARTCOINUSDT', 'AEVOUSDT',
    'ICPUSDT', 'ENAUSDT', 'ADAUSDT', 'WIFUSDT', 'DYDXUSDT',
    'BATUSDT', 'LRCUSDT', 'IOUSDT', 'BOMEUSDT', 'OPUSDT',
    'POPCATUSDT', 'WOOUSDT', 'STORJUSDT', 'ROSEUSDT', 'POWRUSDT',
    'JUPUSDT', 'HUSDT', 'STRKUSDT', 'IOTXUSDT', 'MEWUSDT',
    'VIRTUALUSDT', 'POLUSDT', 'CKBUSDT', 'ASTRUSDT'
]

bot_start_ts      = 0
waiting_signals   = {}   # _akey -> {'coin','direction','entry','kind','level'} -- TEST1+TEST2 lolos, BELUM ada order nyata (masih di luar radius 2%)
pending           = {}   # _akey -> {'coin','direction','entry','sl','dist','order_id'} -- order NYATA sudah terpasang (armed), menunggu fill
active_positions  = {}   # _akey -> {'coin','side','entry','sl','dist','trail_dist','trail_set',...}
last_seen         = {}   # coin -> ready_ts (TEST2 confirm) TERAKHIR yg sudah diproses (dedup)

instrument_cache = {}

# ============================================================
# STATE PERSISTENCE
# ============================================================
STATE_FILE = os.environ.get("STATE_FILE_PATH", "bot_state.json")

def save_state():
    try:
        data = {
            "waiting_signals": waiting_signals, "pending": pending,
            "active_positions": active_positions, "last_seen": last_seen,
        }
        tmp_path = STATE_FILE + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(data, f)
        os.replace(tmp_path, STATE_FILE)
    except Exception as e:
        print(f"⚠️ save_state gagal: {e}")

def load_state():
    global waiting_signals, pending, active_positions, last_seen
    if not os.path.exists(STATE_FILE):
        print(f"ℹ️ {STATE_FILE} belum ada — mulai dari kosong (normal di run pertama).")
        return
    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
        waiting_signals   = data.get("waiting_signals", {})
        pending           = data.get("pending", {})
        active_positions  = data.get("active_positions", {})
        last_seen         = data.get("last_seen", {})
        # MIGRASI: sinyal lama (tersimpan sebelum expire_ts dihitung dari
        # waktu) mungkin punya expire_ts None/hilang -- kalau dibiarkan,
        # sinyal itu TIDAK PERNAH expire selamanya (lihat detect_snr_events).
        # Paksa expire_ts = 0 supaya langsung ke-expire di scan pertama
        # setelah restart, bukan nyangkut permanen.
        fixed = 0
        for d in (waiting_signals, pending):
            for st in d.values():
                if st.get('expire_ts') is None:
                    st['expire_ts'] = 0
                    fixed += 1
        if fixed:
            print(f"🔧 Migrasi state: {fixed} sinyal lama expire_ts kosong -> "
                  f"dipaksa expire di scan berikutnya.")
        print(f"✅ State dimuat: {len(waiting_signals)} menunggu, {len(pending)} armed/pending, "
              f"{len(active_positions)} posisi aktif.")
    except Exception as e:
        print(f"⚠️ load_state gagal ({e}) — mulai dari kosong.")

# ============================================================
# FUNGSI DATA
# ============================================================

def get_data(symbol, interval, limit=200):
    try:
        res = session.get_kline(category=CATEGORY, symbol=symbol, interval=interval, limit=limit)
        if res['retCode'] == 0:
            df = pd.DataFrame(res['result']['list'], columns=['ts','open','high','low','close','vol','turnover'])
            df[['open','high','low','close','vol','turnover','ts']] = \
                df[['open','high','low','close','vol','turnover','ts']].apply(pd.to_numeric)
            df = df.iloc[::-1].reset_index(drop=True)
            LAST_OHLC[(symbol, str(interval))] = df
            return df
        print(f"⚠️ get_data {symbol} {interval}: {res.get('retMsg','')}")
        return None
    except Exception as e:
        print(f"⚠️ get_data {symbol} {interval}: {e}")
        return None


def get_instrument_info(symbol):
    if symbol in instrument_cache:
        return instrument_cache[symbol]
    try:
        res = session.get_instruments_info(category=CATEGORY, symbol=symbol)
        if res['retCode'] == 0:
            info = res['result']['list'][0]
            lot  = info['lotSizeFilter']
            data = {
                'min_qty'     : float(lot['minOrderQty']),
                'qty_step'    : float(lot['qtyStep']),
                'tick_size'   : float(info['priceFilter']['tickSize']),
                'max_leverage': float(info.get('leverageFilter', {}).get('maxLeverage', 10)),
            }
            instrument_cache[symbol] = data
            return data
    except Exception as e:
        print(f"⚠️ instrument_info {symbol}: {e}")
    return {'min_qty': 0.01, 'qty_step': 0.01, 'tick_size': 0.0001, 'max_leverage': 10}


def round_qty(qty, step):
    step_str  = f'{step:.10f}'.rstrip('0')
    precision = len(step_str.split('.')[-1]) if '.' in step_str else 0
    return round(int(qty / step) * step, precision)


def round_price(price, tick):
    tick_str  = f'{tick:.10f}'.rstrip('0')
    precision = len(tick_str.split('.')[-1]) if '.' in tick_str else 0
    return round(round(price / tick) * tick, precision)


# ============================================================
# DETEKSI SUPPORT & RESISTANCE + EMA CROSS + TEST1/TEST2 (engulfing)
# port dari backtest_snr.py.
# ============================================================

N_RIGHT  = 1      # candle kanan (c3) yang wajib bersih (wick tidak boleh menyentuh level)
WICK_EPS = 1e-9

def find_levels(df):
    """Deteksi level Support & Resistance dari candle H1 (basis body candle).
    TANPA syarat kiri, TANPA syarat wick c1/c2.
    Syarat kanan: N_RIGHT candle setelah c1,c2 (c3) -- wick tidak boleh
                  menyentuh level sama sekali.
    Syarat EMA CROSS di RENTANG c2-c4: cukup SALAH SATU dari candle c2, c3,
                  ATAU c4 yang menjadi penyebab cross EMA_FAST/EMA_SLOW
                  (dari close H1) yang searah -- Support -> GOLDEN CROSS,
                  Resistance -> DEATH CROSS. Kalau tidak ada satupun cross
                  yang sesuai di c2-c4, level gugur dari awal.
    'patokan' = LEVEL itu sendiri (sama persis dengan 'level').
    Return list dict: {'type','level','patokan','c1','c2','c_right'}."""
    o = df['open'].values; h = df['high'].values; l = df['low'].values; c = df['close'].values
    n = len(df)
    ema_fast = pd.Series(c).ewm(span=EMA_FAST, adjust=False).mean().values
    ema_slow = pd.Series(c).ewm(span=EMA_SLOW, adjust=False).mean().values

    def golden_cross_at(j):   # cross TEPAT di index j (dibanding j-1)
        return ema_fast[j - 1] <= ema_slow[j - 1] and ema_fast[j] > ema_slow[j]

    def death_cross_at(j):
        return ema_fast[j - 1] >= ema_slow[j - 1] and ema_fast[j] < ema_slow[j]

    levels = []
    for i in range(0, n - 3):   # perlu c1,c2,c3,c4 (i..i+3) semua ada di dalam data
        golden_cross_c2_c4 = any(golden_cross_at(j) for j in (i + 1, i + 2, i + 3))
        death_cross_c2_c4  = any(death_cross_at(j) for j in (i + 1, i + 2, i + 3))
        if c[i] < o[i] and c[i + 1] > o[i + 1]:          # bearish lalu bullish -> support
            S = c[i]
            right_ok = all(l[i + 2 + k] > S + 1e-9 for k in range(N_RIGHT))
            if right_ok and golden_cross_c2_c4:
                levels.append({'type': 'support', 'level': S, 'patokan': S,
                                'c1': i, 'c2': i + 1, 'c_right': [i + 2 + k for k in range(N_RIGHT)]})
        if c[i] > o[i] and c[i + 1] < o[i + 1]:          # bullish lalu bearish -> resistance
            R = c[i]
            right_ok = all(h[i + 2 + k] < R - 1e-9 for k in range(N_RIGHT))
            if right_ok and death_cross_c2_c4:
                levels.append({'type': 'resistance', 'level': R, 'patokan': R,
                                'c1': i, 'c2': i + 1, 'c_right': [i + 2 + k for k in range(N_RIGHT)]})
    return levels


def detect_snr_events(df):
    """TEST1+TEST2 (engulfing) utk tiap level yang terbentuk. Entry = LIMIT,
    di ujung wick candle TEST1.
    TEST1: candle pertama SETELAH c3 yang wick/body-nya menyentuh ATAU
           melebihi patokan (level itu sendiri). Tidak ada syarat arah candle.
    TEST2: candle TEPAT SETELAH TEST1, harus ENGULFING (Support: ujung body
           TEST2 > high candle TEST1. Resistance: ujung body TEST2 < low
           candle TEST1) DAN body candle TEST2 (|close-open|) harus lebih
           BESAR (ukuran, wick tidak dihitung) daripada body candle TEST1
           DAN body candle TEST2 harus lebih BESAR daripada TOTAL WICK
           candle TEST2 itu sendiri (candle TEST2 harus "solid", body
           dominan). Kalau salah satu gagal -> level gugur (dicoba sekali saja).
    entry_price = ujung wick candle TEST1 (Long->high, Short->low).
    sl_price = ADAPTIF, di ujung wick candle TEST2 (Long->low, Short->high),
               dengan floor minimum SL_MIN_PCT dari entry (kalau wick TEST2
               lebih sempit dari itu, diperbesar jadi SL_MIN_PCT).
    ready_ts = waktu (ts) candle TEST2 -- sinyal baru boleh diproses SETELAH
               candle ini closed.
    Return list dict: {'kind','type','level','patokan','direction',
    'entry_price','sl_price','ready_ts','test1_ts','confirm_ts','c1_ts',
    'expire_ts','c1','c2'}."""
    o = df['open'].values; h = df['high'].values; l = df['low'].values; c = df['close'].values
    ts = df['ts'].values
    n = len(df)
    levels = find_levels(df)
    events = []

    for lv in levels:
        level = lv['level']
        ty = lv['type']
        patokan = lv['patokan']
        c1 = lv['c1']
        last_right_i = lv['c_right'][-1]

        test1_i = None
        for k in range(last_right_i + 1, n - 1):
            if ty == 'support':
                touch = l[k] <= patokan + WICK_EPS
            else:
                touch = h[k] >= patokan - WICK_EPS
            if touch:
                test1_i = k
                break
        if test1_i is None:
            continue

        t2 = test1_i + 1
        if ty == 'support':
            body_top_t2 = max(o[t2], c[t2])
            engulf_ok = body_top_t2 > h[test1_i] + WICK_EPS
        else:
            body_bottom_t2 = min(o[t2], c[t2])
            engulf_ok = body_bottom_t2 < l[test1_i] - WICK_EPS
        if not engulf_ok:
            continue

        # Syarat tambahan: body candle TEST2 harus lebih besar (ukuran, wick
        # tidak dihitung) daripada body candle TEST1.
        body_size_t1 = abs(c[test1_i] - o[test1_i])
        body_size_t2 = abs(c[t2] - o[t2])
        if not (body_size_t2 > body_size_t1 + WICK_EPS):
            continue

        # Syarat tambahan lagi: body candle TEST2 harus lebih besar daripada
        # TOTAL WICK candle TEST2 itu sendiri (wick atas + wick bawah) --
        # memastikan candle TEST2 benar-benar candle "solid" (body dominan),
        # bukan candle dengan body kecil tapi wick panjang di kedua sisi.
        body_top_t2_own = max(o[t2], c[t2])
        body_bottom_t2_own = min(o[t2], c[t2])
        wick_total_t2 = (h[t2] - body_top_t2_own) + (body_bottom_t2_own - l[t2])
        if not (body_size_t2 > wick_total_t2 + WICK_EPS):
            continue

        kind = 'SNR_SUPPORT' if ty == 'support' else 'SNR_RESISTANCE'
        direction = 'Long' if ty == 'support' else 'Short'
        entry_price = float(h[test1_i]) if direction == 'Long' else float(l[test1_i])
        # TEST3 (entry cadangan): kalau limit TEST1 belum fill sampai 1
        # candle H1 setelah TEST2 closed, entry dipindah ke ujung wick
        # candle TEST3 (Long->low, Short->high -- kebalikan arah TEST1).
        # Kalau data belum sampai candle TEST3 (ujung histori yg di-fetch),
        # TEST3 belum bisa ditentukan -> entry tetap di TEST1 (dicek ulang
        # scan berikutnya begitu candle TEST3 sudah closed & ke-fetch).
        t3 = t2 + 1
        if t3 < n:
            entry_price_t3 = float(l[t3]) if direction == 'Long' else float(h[t3])
            test3_ts = int(ts[t3])
        else:
            entry_price_t3 = None
            test3_ts = None
        # SL ADAPTIF: ujung wick candle TEST2, dengan floor SL_MIN_PCT.
        if direction == 'Long':
            sl_raw = float(l[t2])
            min_sl_dist = entry_price * SL_MIN_PCT
            sl_price = min(sl_raw, entry_price - min_sl_dist)
        else:
            sl_raw = float(h[t2])
            min_sl_dist = entry_price * SL_MIN_PCT
            sl_price = max(sl_raw, entry_price + min_sl_dist)
        # expire_ts dihitung dari WAKTU (ts candle TEST2 + EXPIRE_CANDLES jam
        # H1 dalam ms), BUKAN dari index array data yang di-fetch. Kalau
        # dihitung dari index (ts[t2+N]), saat TEST2 muncul di ujung data yg
        # baru di-fetch (candle ke-N setelahnya belum ada di array), hasilnya
        # None -> sinyal jadi TIDAK PERNAH expire selamanya walau kenyataannya
        # candle H1 tsb sudah lewat di waktu nyata. Dgn dihitung dari waktu,
        # expire_ts selalu pasti ada sejak awal, tidak bergantung panjang data.
        # Catatan: begitu entry pindah ke TEST3, expire_ts DIHITUNG ULANG
        # dari test3_ts (bukan dari sini lagi) -- lihat process_waiting_signals.
        H1_MS = 3600 * 1000
        expire_ts = int(ts[t2]) + EXPIRE_CANDLES * H1_MS
        events.append({
            'kind': kind, 'type': ty, 'level': level, 'patokan': patokan,
            'direction': direction,
            'entry_price': entry_price, 'sl_price': sl_price, 'ready_ts': int(ts[t2]),
            'entry_price_t3': entry_price_t3, 'test3_ts': test3_ts,
            'test1_ts': int(ts[test1_i]),
            'confirm_ts': int(ts[last_right_i]),
            'c1_ts': int(ts[c1]),
            'expire_ts': expire_ts,
            'c1': lv['c1'], 'c2': lv['c2'],
        })

    events.sort(key=lambda e: e['ready_ts'])
    return events


# ============================================================
# FUNGSI ORDER
# ============================================================

def place_limit_order(symbol, side, entry_p, sl_p):
    """Limit order GTC di entry_p (ujung wick c1, level resistance), SL + trailing native Bybit
    langsung terpasang. Trailing aktif setelah profit +TRAIL_ACT_R x dist (rasio 1:TRAIL_ACT_R)."""
    try:
        info    = get_instrument_info(symbol)
        res_bal = session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
        acct    = res_bal['result']['list'][0]
        balance = float(acct['totalEquity'])
        avail   = float(acct.get('totalAvailableBalance') or balance)
        risk_usd = balance * RISK_PCT
        dist     = abs(entry_p - sl_p)
        if dist == 0:
            print(f"⚠️ {symbol}: dist entry-SL = 0, skip.")
            return None

        min_dist = entry_p * MIN_DIST_PCT
        if dist < min_dist:
            dist  = min_dist
            sl_p  = entry_p - dist if side == "Buy" else entry_p + dist

        raw_qty = risk_usd / dist
        qty     = round_qty(raw_qty, info['qty_step'])
        if qty < info['min_qty']:
            print(f"⚠️ {symbol}: Qty {qty} < minOrderQty {info['min_qty']}, skip.")
            return None

        order_value = qty * entry_p
        if order_value < MIN_ORDER_USD:
            if order_value >= ORDER_BUMP_FLOOR:
                old_ov = order_value
                qty = round_qty(MIN_ORDER_USD / entry_p, info['qty_step'])
                if qty * entry_p < MIN_ORDER_USD:
                    qty = round_qty(qty + info['qty_step'], info['qty_step'])
                order_value = qty * entry_p
                new_risk = qty * dist
                print(f"⬆️ {symbol}: order ${old_ov:.2f}->${order_value:.2f} "
                      f"(risk ${new_risk:.2f} ~ {new_risk/risk_usd:.2f}x target).")
            else:
                print(f"⚠️ {symbol}: Order ~${order_value:.2f} < ${ORDER_BUMP_FLOOR:.0f}, skip.")
                return None

        entry_r  = round_price(entry_p, info['tick_size'])
        sl_r     = round_price(sl_p,    info['tick_size'])
        trail_r  = round_price(TRAIL_STOP * dist, info['tick_size'])
        active_r = round_price(
            entry_p + TRAIL_ACT_R * dist if side == "Buy"
            else entry_p - TRAIL_ACT_R * dist, info['tick_size'])

        lev_int = 10
        try:
            max_lev = float(info.get('max_leverage', 10))
            lev_int = int(min(LEVERAGE, max_lev))
            res_lev = session.set_leverage(category=CATEGORY, symbol=symbol,
                                           buyLeverage=str(lev_int), sellLeverage=str(lev_int))
            if res_lev.get('retCode', -1) not in (0, 110043):
                print(f"   ⚠️ {symbol}: set_leverage gagal: {res_lev.get('retMsg','')} — coba lanjut")
        except Exception as e:
            if '110043' not in str(e):
                print(f"   ⚠️ {symbol}: set_leverage error: {e} — coba lanjut")

        required_margin = (qty * entry_p) / lev_int
        if required_margin > avail * 0.9:
            print(f"⚠️ {symbol}: Margin tidak cukup — butuh ~${required_margin:.2f}, avail ${avail:.2f}. Skip.")
            return None

        print(f"   Balance:{balance:.2f} Avail:{avail:.2f} Risk:{risk_usd:.2f} Dist:{dist:.6f} "
              f"Trail:{trail_r} ActiveP:{active_r} Qty:{qty} Entry:{entry_r} SL:{sl_r} "
              f"Lev:{lev_int}x Margin:~${required_margin:.2f}")

        res = session.place_order(
            category=CATEGORY, symbol=symbol, side=side,
            orderType="Limit", qty=str(qty), price=str(entry_r),
            stopLoss=str(sl_r), trailingStop=str(trail_r), activePrice=str(active_r),
            positionIdx=_pidx(side), timeInForce="GTC")
        if res['retCode'] == 0:
            return res['result']['orderId'], qty, entry_r, sl_r, dist
        print(f"⚠️ {symbol}: Limit order ditolak → {res.get('retMsg','')} (code:{res['retCode']})")
        return None
    except Exception as e:
        print(f"⚠️ {symbol}: place_limit_order error → {e}")
        return None


def cancel_order(symbol, order_id):
    try:
        res = session.cancel_order(category=CATEGORY, symbol=symbol, orderId=order_id)
        if res['retCode'] == 0:
            print(f"   ✅ {symbol}: Order {order_id[:8]}… dibatalkan.")
        else:
            print(f"   ⚠️ {symbol}: Cancel gagal → {res.get('retMsg','')} (code:{res['retCode']})")
    except Exception as e:
        print(f"   ⚠️ {symbol}: cancel_order error → {e}")


def _order_exists(symbol, order_id):
    try:
        res = session.get_open_orders(category=CATEGORY, symbol=symbol, orderId=order_id)
        if res['retCode'] == 0:
            for o in res['result']['list']:
                if o.get('orderId') == order_id and \
                        o.get('orderStatus') in ('New', 'PartiallyFilled', 'Untriggered'):
                    return True
            return False
    except Exception:
        pass
    return False


def get_open_position(symbol, want_side=None):
    try:
        res = session.get_positions(category=CATEGORY, symbol=symbol)
        if res['retCode'] == 0:
            for pos in res['result']['list']:
                if float(pos['size']) <= 0:
                    continue
                if ALLOW_HEDGE and want_side is not None and pos.get('side') != want_side:
                    continue
                return pos
        return None
    except Exception:
        return None


def close_position(symbol, side, qty_str, reason="manual"):
    """Force-close posisi dengan market order reduceOnly (dipakai FLIP protection & trail timeout)."""
    try:
        close_side = 'Sell' if side == 'Buy' else 'Buy'
        info  = get_instrument_info(symbol)
        qty_r = round_qty(float(qty_str), info['qty_step'])
        if qty_r <= 0:
            return False
        res = session.place_order(
            category=CATEGORY, symbol=symbol, side=close_side, orderType="Market",
            qty=str(qty_r), reduceOnly=True, positionIdx=_pidx(side), timeInForce="IOC"
        )
        if res.get('retCode') == 0:
            print(f"⏹️  {symbol}: Posisi ditutup market ({reason})")
            return True
        print(f"⚠️ {symbol}: close_position gagal → {res.get('retMsg','')} (code:{res.get('retCode')})")
        return False
    except Exception as e:
        print(f"⚠️ {symbol}: close_position error → {e}")
        return False


def _get_actual_exit_price(symbol):
    try:
        res = session.get_closed_pnl(category=CATEGORY, symbol=symbol, limit=1)
        if res['retCode'] == 0 and res['result']['list']:
            exit_p = float(res['result']['list'][0].get('avgExitPrice', 0))
            if exit_p > 0:
                return exit_p
    except Exception as e:
        print(f"⚠️ {symbol}: get_closed_pnl error: {e}")
    return None


# ============================================================
# TRAILING STOP (fallback pemasangan + deteksi posisi closed)
# ============================================================

def check_trailing_sl(key):
    if key not in active_positions:
        return
    p    = active_positions[key]
    coin = p.get('coin', key)
    side = p.get('side')
    pos  = get_open_position(coin, side)

    if pos is None:
        actual_exit = _get_actual_exit_price(coin)
        exit_str    = f"{actual_exit:.6f}" if actual_exit else "?"
        log_entry(f"📭 {coin} [{p.get('direction','')}]: Posisi tutup @ {exit_str} "
                  f"(entry {p.get('entry',0):.6g} SL {p.get('sl',0):.6g}).")
        del active_positions[key]
        return

    try:
        curr_price = float(pos['markPrice'])
        entry = p['entry']; dist = p.get('dist', 0); side = p['side']

        peak      = p.get('peak', entry)
        peak_time = p.get('peak_time', p.get('entry_time', time.time()))
        new_peak  = max(peak, curr_price) if side == 'Buy' else min(peak, curr_price)
        if new_peak != peak:
            active_positions[key]['peak']      = new_peak
            active_positions[key]['peak_time'] = time.time()
            peak_time = time.time()

        if TRAIL_TIMEOUT_DAYS:
            timeout_sec = TRAIL_TIMEOUT_DAYS * 24 * 3600
            if time.time() - peak_time > timeout_sec:
                qty_pos = pos.get('size', '0')
                hours_stuck = (time.time() - peak_time) / 3600
                print(f"⏰ {coin}: Trail timeout {TRAIL_TIMEOUT_DAYS} hari (peak stuck {hours_stuck:.1f}h)")
                if close_position(coin, side, qty_pos, reason="trail timeout"):
                    log_entry(f"⏰ {coin} [{p.get('direction','')}]: Ditutup paksa (trail timeout).")
                    del active_positions[key]
                return

        if dist > 0 and not p.get('trail_set', False):
            trail_dist = p.get('trail_dist', TRAIL_STOP * dist)
            info       = get_instrument_info(coin)
            tick       = info.get('tick_size', 0.0001)
            trail_r    = round_price(trail_dist, tick)
            active_p   = round_price(entry + TRAIL_ACT_R * dist if side == "Buy" else entry - TRAIL_ACT_R * dist, tick)
            if trail_r > 0 and active_p > 0:
                try:
                    res_ts = session.set_trading_stop(
                        category=CATEGORY, symbol=coin, trailingStop=str(trail_r),
                        activePrice=str(active_p), positionIdx=_pidx(side))
                    if res_ts['retCode'] == 0:
                        active_positions[key]['trail_set'] = True
                        print(f"📍 {coin}: Trailing stop {trail_r} dipasang (aktif @ {active_p} = entry±{TRAIL_ACT_R}R)")
                    else:
                        print(f"⚠️ {coin}: Gagal set trailing stop: {res_ts.get('retMsg','')} (code:{res_ts['retCode']})")
                except Exception as e:
                    print(f"⚠️ {coin}: set_trading_stop error: {e}")
    except Exception:
        pass


# ============================================================
# KONEKSI
# ============================================================

def test_connection():
    try:
        res = session.get_server_time()
        if res['retCode'] == 0:
            print(f"✅ Koneksi Bybit OK | Server time: {res['result']['timeSecond']}")
            return True
        print(f"❌ Bybit error: {res}")
        return False
    except Exception as e:
        print(f"❌ Gagal konek: {e}")
        return False


# ============================================================
# LOGIKA UTAMA per koin: deteksi resistance baru -> langsung pasang limit Short
# ============================================================

def _count_slots():
    return len(active_positions) + len(pending)


def _touched_since(df_closed, ready_ts, entry):
    """True kalau ada candle H1 SETELAH ready_ts (exclusive) sampai candle
    terakhir closed yang menyentuh harga entry (l<=entry<=h)."""
    ts = df_closed['ts'].values
    h = df_closed['high'].values; l = df_closed['low'].values
    n = len(df_closed)
    idx = int(np.searchsorted(ts, ready_ts))   # posisi candle ready_ts (TEST2 atau TEST3)
    for k in range(idx + 1, n):
        if l[k] <= entry <= h[k]:
            return True
    return False


def _resolve_fresh_entry(df_closed, ev, now_ts):
    """Tentukan entry mana yang berlaku & apakah levelnya masih FRESH, sadar
    TEST3 (disamakan dgn keputusan switch di _maybe_switch_to_test3):
    - Kalau now_ts BELUM lewat test3_ts (atau TEST3 tdk tersedia/tdk valid):
      entry = TEST1, fresh = belum pernah tersentuh sejak TEST2 (candle
      TEST2 sendiri tidak dihitung -- itu breakout, bukan retest).
    - Kalau now_ts SUDAH lewat test3_ts dan TEST3 valid (tdk nembus SL):
      entry LANGSUNG dianggap TEST3 (used_t3=True sejak awal, sesuai TEST1
      dianggap "sudah kadaluarsa 1 candle" begitu level ini pertama kali
      dilihat bot). fresh = belum pernah tersentuh sejak TEST3 (candle
      TEST3 sendiri tidak dihitung).
    Return (entry_price, is_fresh, used_t3)."""
    t3_price = ev.get('entry_price_t3')
    t3_ts = ev.get('test3_ts')
    sl = ev['sl_price']
    t3_available = (t3_price is not None and t3_ts is not None
                     and now_ts is not None and now_ts >= t3_ts)
    if t3_available:
        t3_valid = (t3_price > sl) if ev['direction'] == 'Long' else (t3_price < sl)
        if t3_valid:
            fresh = not _touched_since(df_closed, t3_ts, t3_price)
            return t3_price, fresh, True
    # TEST3 belum waktunya / tidak tersedia / tidak valid -> pakai TEST1
    fresh = not _touched_since(df_closed, ev['ready_ts'], ev['entry_price'])
    return ev['entry_price'], fresh, False


def process_new_signals(coin, df_closed, now_ts=None):
    """Cek sinyal baru (TEST1+TEST2 lolos, ready_ts > last_seen[coin]) ->
    masukkan ke waiting_signals (BELUM ada order nyata di Bybit). Dedup:
    kalau sudah ada waiting/pending/posisi utk arah yang sama, skip (tiap
    level dipakai PERSIS SEKALI).
    FRESHNESS CHECK (sadar TEST3): kalau now_ts sudah lewat test3_ts saat
    level ini PERTAMA KALI dilihat bot, TEST1 dianggap sudah kadaluarsa 1
    candle -> freshness & entry langsung dicek terhadap TEST3 (bukan TEST1).
    Kalau entry yg berlaku (TEST1 atau TEST3) SUDAH PERNAH tersentuh oleh
    data historis SEBELUM sinyal ini sempat diproses (mis. bot baru pertama
    kali deploy, atau abis mati beberapa jam/hari) -> level dianggap GUGUR,
    TIDAK dimasukkan ke waiting_signals. Ini yang mencegah bot "menghidupkan
    lagi" level basi dari masa lalu yang seharusnya sudah tidak valid."""
    events = detect_snr_events(df_closed)
    newest_seen = last_seen.get(coin, 0)

    for ev in events:
        if ev['ready_ts'] <= newest_seen:
            continue
        newest_seen = ev['ready_ts']   # tandai diproses APAPUN hasilnya (tidak diulang lagi)

        direction = ev['direction']
        key = _akey(coin, direction)
        if key in waiting_signals or key in pending or key in active_positions:
            print(f"⏭️  {coin} [{direction}]: sinyal baru muncul tp sudah ada "
                  f"menunggu/armed/posisi searah, skip.")
            continue

        entry, is_fresh, used_t3 = _resolve_fresh_entry(df_closed, ev, now_ts)
        level_label = 'Support level' if direction == 'Long' else 'Resistance level'
        test_label = 'TEST3' if used_t3 else 'TEST1'
        if not is_fresh:
            log_entry(f"⏭️  {coin} [{direction}]: {ev['kind']} — wick {test_label} {entry:.6g} sudah "
                      f"pernah TERSENTUH data historis sebelum sempat diproses -> GUGUR, dilewati.\n"
                      f"    {level_label}   : {ev['level']:.6g}\n"
                      f"    C1 terbentuk   : {_fmt_wib(ev['c1_ts'])}\n"
                      f"    Test1 tersentuh: {_fmt_wib(ev['test1_ts'])}\n"
                      f"    TEST2 (ready)  : {_fmt_wib(ev['ready_ts'])}"
                      + (f"\n    TEST3          : {_fmt_wib(ev['test3_ts'])}" if used_t3 else ""))
            continue

        waiting_signals[key] = {
            'coin': coin, 'direction': direction, 'entry': entry,
            'sl': ev['sl_price'], 'kind': ev['kind'], 'level': ev['level'],
            'expire_ts': (ev['test3_ts'] + EXPIRE_CANDLES * 3600 * 1000) if used_t3 else ev.get('expire_ts'),
            'entry_price_t3': ev.get('entry_price_t3'), 'test3_ts': ev.get('test3_ts'),
            'used_t3': used_t3, 'ready_ts': ev['ready_ts'],
        }
        log_entry(f"👀 {coin} [{direction}]: {ev['kind']} TEST1+TEST2 lolos (c1 @ {ev['c1_ts']}), "
                  f"MASIH FRESH (belum pernah tersentuh) — "
                  f"menunggu harga masuk radius {APPROACH_PCT*100:.1f}% dari wick {test_label} "
                  f"{entry:.6g}\n"
                  f"    {level_label}   : {ev['level']:.6g}\n"
                  f"    C1 terbentuk   : {_fmt_wib(ev['c1_ts'])}\n"
                  f"    Test1 tersentuh: {_fmt_wib(ev['test1_ts'])}"
                  + (f"\n    Entry langsung TEST3 (TEST1 sudah lewat 1 candle saat pertama terdeteksi)" if used_t3 else ""))

    last_seen[coin] = newest_seen


def _lookup_test3_from_cache(coin, direction, ready_ts):
    """Cari harga wick candle TEST3 (1 candle H1 SETELAH TEST2/ready_ts) dari
    cache OHLC H1 terakhir (LAST_OHLC), dipakai saat entry_price_t3 belum
    sempat dihitung di detect_snr_events (krn candle TEST3 belum closed
    saat level pertama kali terdeteksi -> tersimpan None PERMANEN di
    waiting_signals/pending kalau tidak di-refresh di sini). Return
    (entry_price_t3, test3_ts) atau (None, None) kalau candle TEST3 belum
    ada di cache (msh berjalan / belum ke-fetch lagi)."""
    if ready_ts is None:
        return None, None
    df = LAST_OHLC.get((coin, '60'))
    if df is None or len(df) == 0:
        return None, None
    test3_ts_target = ready_ts + 3600 * 1000
    ts = df['ts'].values
    idx = np.where(ts == test3_ts_target)[0]
    if len(idx) == 0:
        return None, None   # candle TEST3 msh berjalan / belum ke-fetch
    i = int(idx[0])
    if direction == 'Long':
        return float(df['low'].values[i]), int(test3_ts_target)
    else:
        return float(df['high'].values[i]), int(test3_ts_target)


def _maybe_switch_to_test3(coin, direction, sig, now_ts):
    """Cek & lakukan switch entry TEST1 -> TEST3 (disamakan dgn backtest):
    kalau ENABLE_TEST3 aktif, belum pernah di-switch, data TEST3 tersedia,
    dan now_ts sudah lewat test3_ts (1 candle H1 setelah TEST2 closed) ->
    entry dipindah ke wick TEST3 (Long->low, Short->high). Guard: kalau
    entry TEST3 ada di sisi SALAH dari SL (mis. Long tapi entry<=SL), TEST3
    dianggap tidak valid, tetap pakai TEST1. Return True kalau switch terjadi
    (sig dimodifikasi in-place).
    entry_price_t3/test3_ts bisa None kalau candle TEST3 belum closed saat
    level PERTAMA KALI terdeteksi -- kalau begitu, coba REFRESH dari cache
    OHLC terbaru (_lookup_test3_from_cache) sebelum menyerah, supaya tidak
    tersangkut None permanen walau candle TEST3 sebenarnya sudah closed."""
    if not ENABLE_TEST3 or sig.get('used_t3') or now_ts is None:
        return False
    t3_price = sig.get('entry_price_t3')
    t3_ts = sig.get('test3_ts')
    if t3_price is None or t3_ts is None:
        t3_price, t3_ts = _lookup_test3_from_cache(coin, direction, sig.get('ready_ts'))
        if t3_price is None:
            return False   # candle TEST3 msh blm tersedia sama sekali, coba lagi scan berikutnya
        sig['entry_price_t3'] = t3_price
        sig['test3_ts'] = t3_ts
    if now_ts < t3_ts:
        return False
    sig['used_t3'] = True
    sl = sig['sl']
    t3_valid = (t3_price > sl) if direction == 'Long' else (t3_price < sl)
    if not t3_valid:
        return False   # TEST3 tidak valid (nembus SL) -> tetap TEST1
    sig['entry'] = t3_price
    sig['expire_ts'] = t3_ts + EXPIRE_CANDLES * 3600 * 1000
    log_entry(f"🔀 {coin} [{direction}]: TEST1 belum fill 1 candle H1 setelah TEST2 — "
              f"entry dipindah ke wick TEST3 {t3_price:.6g} (expire dihitung ulang).")
    return True


def process_waiting_signals(coin, current_price, now_ts=None):
    """Sinyal yg masih menunggu (belum ada order nyata): begitu harga masuk
    radius APPROACH_PCT dari entry_price -> pasang LIMIT order NYATA di
    Bybit (armed), pindah ke 'pending'.
    TEST3: kalau TEST1 blm fill 1 candle H1 setelah TEST2, entry dipindah
    ke wick TEST3 (lihat _maybe_switch_to_test3).
    KADALUARSA (disamakan dgn backtest): kalau now_ts >= expire_ts
    (EXPIRE_CANDLES candle H1 setelah TEST2, atau setelah TEST3 kalau sudah
    di-switch) dan belum sempat armed -> sinyal dibuang permanen, tidak
    pernah dipasang."""
    for direction in ('Long', 'Short'):
        key = _akey(coin, direction)
        sig = waiting_signals.get(key)
        if sig is None:
            continue
        _maybe_switch_to_test3(coin, direction, sig, now_ts)
        expire_ts = sig.get('expire_ts')
        if now_ts is not None and expire_ts is not None and now_ts >= expire_ts:
            log_entry(f"⌛ {coin} [{direction}]: sinyal kadaluarsa ({EXPIRE_CANDLES} candle H1 sejak "
                      f"TEST2/TEST3 lewat tanpa masuk radius {APPROACH_PCT*100:.1f}%) — dibuang permanen.")
            del waiting_signals[key]
            continue
        entry = sig['entry']
        dist_pct = abs(current_price - entry) / entry
        if dist_pct > APPROACH_PCT:
            continue   # masih jauh, tetap menunggu

        sl = sig['sl']   # SL ADAPTIF (wick candle TEST2, floor SL_MIN_PCT) -- sudah dihitung saat deteksi event
        dist = abs(entry - sl)
        if dist <= 0:
            del waiting_signals[key]
            continue

        if _count_slots() >= MAX_CONCURRENT:
            print(f"⏭️  {coin} [{direction}]: harga sudah dekat tp slot penuh ({MAX_CONCURRENT}), "
                  f"tetap menunggu.")
            continue

        side = "Buy" if direction == "Long" else "Sell"
        result = place_limit_order(coin, side, entry, sl)
        if result is not None:
            order_id, qty, entry_r, sl_r, dist_r = result
            pending[key] = {'coin': coin, 'direction': direction,
                             'entry': entry_r, 'sl': sl_r, 'dist': dist_r, 'order_id': order_id,
                             'kind': sig['kind'], 'level': sig['level'], 'expire_ts': sig.get('expire_ts'),
                             'entry_price_t3': sig.get('entry_price_t3'), 'test3_ts': sig.get('test3_ts'),
                             'used_t3': sig.get('used_t3', False), 'ready_ts': sig.get('ready_ts')}
            del waiting_signals[key]
            log_entry(f"📌 {coin} [{direction}]: harga masuk radius {APPROACH_PCT*100:.1f}% — "
                      f"LIMIT {side.upper()} dipasang NYATA @ wick {entry_r:.6g} SL {sl_r:.6g}")


def process_armed_distance(coin, current_price, now_ts=None):
    """Order yg SUDAH armed (nyata terpasang, blm fill): kalau harga menjauh
    lagi > APPROACH_PCT, BATALKAN order (disarm), balik ke waiting_signals
    (level tetap hidup, bisa armed lagi kalau mendekat lagi).
    TEST3: kalau TEST1 blm fill 1 candle H1 setelah TEST2, order LAMA (di
    harga TEST1) dibatalkan, entry dipindah ke wick TEST3, balik ke
    waiting_signals (perlu di-armed ulang dari entry baru).
    KADALUARSA (disamakan dgn backtest): kalau now_ts >= expire_ts dan order
    belum sempat fill -> order dibatalkan permanen (bukan disarm)."""
    for direction in ('Long', 'Short'):
        key = _akey(coin, direction)
        st = pending.get(key)
        if st is None:
            continue

        switched = _maybe_switch_to_test3(coin, direction, st, now_ts)
        if switched:
            cancel_order(coin, st['order_id'])
            waiting_signals[key] = {'coin': coin, 'direction': direction, 'entry': st['entry'],
                                     'sl': st.get('sl'), 'kind': st.get('kind', ''),
                                     'level': st.get('level', st['entry']), 'expire_ts': st.get('expire_ts'),
                                     'entry_price_t3': st.get('entry_price_t3'), 'test3_ts': st.get('test3_ts'),
                                     'used_t3': st.get('used_t3', False), 'ready_ts': st.get('ready_ts')}
            del pending[key]
            log_entry(f"🔀 {coin} [{direction}]: order lama (wick TEST1) dibatalkan krn beralih ke TEST3, "
                      f"balik menunggu dari entry baru.")
            continue

        expire_ts = st.get('expire_ts')
        if now_ts is not None and expire_ts is not None and now_ts >= expire_ts:
            cancel_order(coin, st['order_id'])
            del pending[key]
            log_entry(f"⌛ {coin} [{direction}]: order armed kadaluarsa ({EXPIRE_CANDLES} candle H1 sejak "
                      f"TEST2/TEST3 lewat tanpa fill) — dibatalkan permanen.")
            continue
        entry = st['entry']
        dist_pct = abs(current_price - entry) / entry
        if dist_pct <= APPROACH_PCT:
            continue   # masih dlm radius, biarkan armed

        cancel_order(coin, st['order_id'])
        waiting_signals[key] = {'coin': coin, 'direction': direction, 'entry': entry,
                                 'sl': st.get('sl'), 'kind': st.get('kind', ''),
                                 'level': st.get('level', entry), 'expire_ts': st.get('expire_ts'),
                                 'entry_price_t3': st.get('entry_price_t3'), 'test3_ts': st.get('test3_ts'),
                                 'used_t3': st.get('used_t3', False), 'ready_ts': st.get('ready_ts')}
        del pending[key]
        log_entry(f"🔙 {coin} [{direction}]: harga menjauh lagi (> {APPROACH_PCT*100:.1f}%) sebelum fill — "
                  f"limit dibatalkan, balik menunggu.")


def manage_pending(coin):
    """Cek tiap pending order (armed) utk coin ini: sudah fill? order masih ada di exchange?"""
    for direction in ('Long', 'Short'):
        key = _akey(coin, direction)
        st = pending.get(key)
        if st is None:
            continue
        side = 'Buy' if direction == 'Long' else 'Sell'

        pos = get_open_position(coin, side)
        if pos is not None:
            entry_actual = float(pos.get('avgPrice') or st['entry'])
            dist_actual  = abs(entry_actual - st['sl'])
            active_positions[key] = {
                'coin': coin, 'side': side, 'direction': direction,
                'entry': entry_actual, 'sl': st['sl'], 'dist': dist_actual,
                'trail_dist': TRAIL_STOP * dist_actual, 'trail_set': False,
                'peak': entry_actual, 'peak_time': time.time(), 'entry_time': time.time(),
            }
            log_entry(f"✅ {coin} [{direction}]: LIMIT FILLED @ {entry_actual:.6g} SL {st['sl']:.6g}")
            del pending[key]
            continue

        if not _order_exists(coin, st['order_id']):
            print(f"⚠️ {coin} [{direction}]: order {st['order_id'][:8]}… tak ditemukan lagi — dibuang dari pending.")
            del pending[key]


# ============================================================
# MAIN LOOP
# ============================================================

def run_bot():
    global bot_start_ts
    bot_start_ts = time.time()
    load_state()
    print("BOT SUPPORT & RESISTANCE + EMA CROSS + TEST1/TEST2 ENGULFING — H1")
    print(f"CONFIG | EMA{EMA_FAST}/{EMA_SLOW} cross wajib di c2-c4 | approach {APPROACH_PCT*100:.1f}% | "
          f"trail aktif 1:{TRAIL_ACT_R:.0f} | trail width {TRAIL_STOP:.1f}x | "
          f"risk {RISK_PCT*100:.0f}%/trade | lev {LEVERAGE}x | slot max {MAX_CONCURRENT} | "
          f"HEDGE {'ON' if ALLOW_HEDGE else 'off'} | SL adaptif wick TEST2, min {SL_MIN_PCT*100:.2f}% dari entry | "
          f"body TEST2>body TEST1, body TEST2>wick TEST2 | "
          f"expire {EXPIRE_CANDLES} candle H1 | TEST3 {'AKTIF' if ENABLE_TEST3 else 'nonaktif'} | {len(SYMBOLS)} koin")
    if not test_connection():
        print("⛔ Tidak bisa konek ke Bybit.")
        return
    if ALLOW_HEDGE:
        try:
            r = session.switch_position_mode(category=CATEGORY, coin="USDT", mode=3)
            rc = r.get('retCode', -1)
            if rc == 0:
                print("🔀 Hedge mode AKTIF.")
            elif rc == 110025:
                print("🔀 Hedge mode sudah aktif.")
            else:
                print(f"⚠️ switch_position_mode: {r.get('retMsg','')} (code:{rc})")
        except Exception as e:
            print(f"⚠️ switch_position_mode error: {e}")

    while True:
        now = time.time()
        wait_sec = 300 - (now % 300) + 2
        if wait_sec > 300:
            wait_sec = 2
        print(f"⏱️  Tunggu {wait_sec:.0f} detik...")
        time.sleep(wait_sec)

        for _k in list(active_positions.keys()):
            try:
                check_trailing_sl(_k)
            except Exception as e:
                print(f"⚠️ Trailing SL {_k}: {e}")

        n_active, n_pending, n_waiting = len(active_positions), len(pending), len(waiting_signals)
        print(f"\n{'='*55}")
        print(f"📊 SLOT: {n_active + n_pending}/{MAX_CONCURRENT} (posisi:{n_active} | armed:{n_pending}) | menunggu:{n_waiting}")
        for k, p in active_positions.items():
            print(f"   POSISI {p.get('coin')} [{p.get('direction')}] @ {p.get('entry',0):.6g} SL:{p.get('sl',0):.6g}")
        for k, s in pending.items():
            print(f"   ARMED  {s.get('coin')} [{s.get('direction')}] @ {s.get('entry',0):.6g} SL:{s.get('sl',0):.6g}")
        for k, s in waiting_signals.items():
            print(f"   TUNGGU {s.get('coin')} [{s.get('direction')}] @ {s.get('entry',0):.6g}")
        print(f"{'='*55}")

        for coin in SYMBOLS:
            try:
                time.sleep(2)
                df_all = get_data(coin, TIMEFRAME, limit=200)
                if df_all is None or len(df_all) < (2 + N_RIGHT + 2):
                    continue
                df_closed = df_all.iloc[:-1].reset_index(drop=True)   # buang candle yg masih berjalan
                current_price = float(df_all['close'].iloc[-1])       # candle yg lagi berjalan -> proxy harga live
                now_ts = int(df_closed['ts'].iloc[-1]) if len(df_closed) else None   # ts candle H1 terakhir closed, utk cek expire

                # Tidak ada lagi perlakuan khusus "run pertama" -- process_new_signals
                # sendiri sudah otomatis membuang level yang basi (freshness check),
                # jadi baik pertama kali deploy maupun redeploy, hasilnya sama: hanya
                # sinyal yang MASIH FRESH (belum pernah tersentuh) yang dipantau.
                manage_pending(coin)
                process_armed_distance(coin, current_price, now_ts)
                process_waiting_signals(coin, current_price, now_ts)
                process_new_signals(coin, df_closed, now_ts)

            except Exception as e:
                print(f"⚠️ Error {coin}: {e}")
                continue

        save_state()


if __name__ == "__main__":
    run_bot()
