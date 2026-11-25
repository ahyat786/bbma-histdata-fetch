#!/usr/bin/env python3
"""
Resumable HistData.com tick downloader for the BBMA public GitHub Action.

- Downloads Generic ASCII TICK quotes for EURUSD/GBPUSD/AUDUSD/XAUUSD.
- Converts every month into canonical H4/H1/M15 Mid/Bid/Ask candle JSON.
- Caches per-month parts in <out>/parts so scheduled reruns only fetch new months.
- HistData timestamps are EST fixed (UTC-5); +5h is applied to produce UTC.
- No credentials are required or stored.
"""
import argparse
import datetime as dt
import http.cookiejar
import io
import json
import re
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

BASE = 'https://www.histdata.com'
UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36'
TFS = {'M15': 900, 'H1': 3600, 'H4': 14400}
SIDES = ['Mid', 'Bid', 'Ask']
EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)


def empty_rows():
    return {tf: {s: [] for s in SIDES} for tf in TFS}


def build_opener():
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    op.addheaders = [('User-Agent', UA), ('Accept-Language', 'en-US,en;q=0.9')]
    return op


def get_page(op, url, referer=None):
    req = urllib.request.Request(url)
    if referer:
        req.add_header('Referer', referer)
    with op.open(req, timeout=90) as r:
        return r.read().decode('utf8', 'replace')


def download_zip(op, pair, year, month):
    url = '%s/download-free-forex-historical-data/?/ascii/tick-data-quotes/%s/%d/%d' % (
        BASE, pair.lower(), year, month)
    html = get_page(op, url)
    fields = {}
    for name in ['tk', 'date', 'datemonth', 'platform', 'timeframe', 'fxpair']:
        m = re.search(r'name="%s" id="%s" value="([^"]*)"' % (name, name), html)
        if m:
            fields[name] = m.group(1)
    if len(fields) < 6 or len(fields.get('tk', '')) != 32:
        raise RuntimeError('HistData form token/fields not found for %s %d-%d' % (pair, year, month))
    req = urllib.request.Request(BASE + '/get.php', data=urllib.parse.urlencode(fields).encode())
    req.add_header('Referer', url)
    req.add_header('Content-Type', 'application/x-www-form-urlencoded')
    with op.open(req, timeout=240) as r:
        blob = r.read()
    if not blob.startswith(b'PK'):
        raise RuntimeError('download was not a ZIP (%d bytes)' % len(blob))
    return blob


def parse_ts(raw):
    s = raw.strip().replace(' ', '')
    y, mo, d = int(s[0:4]), int(s[4:6]), int(s[6:8])
    h, mi, sec = int(s[8:10]), int(s[10:12]), int(s[12:14])
    milli = int(s[14:17]) if len(s) >= 17 else 0
    # EST fixed -> UTC (+5h), no DST.
    t = dt.datetime(y, mo, d, h, mi, sec, milli * 1000, tzinfo=dt.timezone.utc) + dt.timedelta(hours=5)
    return int((t - EPOCH).total_seconds())


def iso(sec):
    return dt.datetime.fromtimestamp(sec, dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def process_zip(blob, rows, states, stats, from_s, to_s):
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        name = next((n for n in z.namelist() if n.lower().endswith('.csv')), None)
        if not name:
            raise RuntimeError('no CSV inside ZIP')
        with z.open(name) as f:
            for line in f:
                parts = re.split(r'[;,]', line.decode('ascii', 'replace').strip())
                if len(parts) < 3:
                    continue
                try:
                    sec = parse_ts(parts[0])
                    bid, ask = float(parts[1]), float(parts[2])
                except (ValueError, IndexError):
                    continue
                if bid <= 0 or ask <= 0 or ask < bid:
                    continue
                stats['ticks'] += 1
                stats['spread_sum'] += ask - bid
                stats['first'] = sec if stats['first'] is None else min(stats['first'], sec)
                stats['last'] = sec if stats['last'] is None else max(stats['last'], sec)
                if sec < from_s or sec > to_s:
                    continue
                for tf, step in TFS.items():
                    bucket = sec - (sec % step)
                    st = states[tf]
                    if st.get('start') != bucket:
                        flush(st, rows[tf])
                        st['start'] = bucket
                        st['b'] = [bid, bid, bid, bid]
                        st['a'] = [ask, ask, ask, ask]
                    else:
                        b, a = st['b'], st['a']
                        b[1] = max(b[1], bid); b[2] = min(b[2], bid); b[3] = bid
                        a[1] = max(a[1], ask); a[2] = min(a[2], ask); a[3] = ask
    for tf, st in states.items():
        flush(st, rows[tf])


def flush(st, target):
    if not st.get('b'):
        return
    b, a = st['b'], st['a']
    mid = [(b[i] + a[i]) / 2 for i in range(4)]
    sec = st['start']
    target['Mid'].append([iso(sec)] + [round(x, 8) for x in mid])
    target['Bid'].append([iso(sec)] + [round(x, 8) for x in b])
    target['Ask'].append([iso(sec)] + [round(x, 8) for x in a])


def merge_part(rows, part):
    for tf in TFS:
        for side in SIDES:
            rows[tf][side].extend(part['rows'][tf][side])


def stats_zero():
    return {'ticks': 0, 'spread_sum': 0.0, 'first': None, 'last': None}


def merge_stats(total, part):
    total['ticks'] += part['ticks']
    total['spread_sum'] += part['spread_sum']
    for k in ['first', 'last']:
        v = part.get(k)
        if v is not None:
            total[k] = v if total[k] is None else min(total[k], v) if k == 'first' else max(total[k], v)


def dedupe(rows):
    """Deduplicate by timestamp, merging same-bucket fragments across month ZIPs.

    HistData months are EST-fixed: after +5 h UTC conversion a month starts at
    05:00 UTC and ends at 04:59:59 UTC on the 1st of the next month. An H4
    bucket at 04:00 UTC therefore appears in two consecutive monthly parts.
    Keep the first Open, maximum High, minimum Low and last Close so the
    canonical candles match a continuous (non-reset) tick aggregation.
    """
    out = {}
    for tf in TFS:
        out[tf] = {}
        for side in SIDES:
            merged = {}
            for r in rows[tf][side]:
                ts = r[0]
                if ts not in merged:
                    merged[ts] = list(r)
                else:
                    cur = merged[ts]
                    cur[2] = max(cur[2], r[2])
                    cur[3] = min(cur[3], r[3])
                    cur[4] = r[4]
            out[tf][side] = [merged[k] for k in sorted(merged)]
    return out


def save_final(out, pair, rows, stats, months_ok, failed, skipped=0):
    rows = dedupe(rows)
    for tf in TFS:
        for side in SIDES:
            (out / ('%s_%s_%s.json' % (pair, tf, side))).write_text(
                json.dumps(rows[tf][side], separators=(',', ':')))
    summary = {
        'source': 'HistData.com Generic ASCII Tick',
        'months_ok': months_ok,
        'months_cached': skipped,
        'failed': failed,
        'ticks': stats['ticks'],
        'avg_spread': round(stats['spread_sum'] / max(stats['ticks'], 1), 8),
        'first_utc': iso(stats['first']) if stats['first'] else None,
        'last_utc': iso(stats['last']) if stats['last'] else None,
        'bars': {tf: len(rows[tf]['Mid']) for tf in TFS}
    }
    (out / ('%s_summary.json' % pair)).write_text(json.dumps(summary, indent=2))
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', required=True)
    ap.add_argument('--pairs', default='EURUSD,GBPUSD,AUDUSD,XAUUSD')
    ap.add_argument('--start-year', type=int, default=2021)
    ap.add_argument('--end-year', type=int, default=2026)
    ap.add_argument('--end-month', type=int, default=11)
    ap.add_argument('--delay', type=float, default=0.25)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    parts_root = out / 'parts'
    parts_root.mkdir(parents=True, exist_ok=True)
    pairs = [p.strip().upper() for p in args.pairs.split(',') if p.strip()]

    from_s = int(dt.datetime(args.start_year, 1, 1, tzinfo=dt.timezone.utc).timestamp())
    to_s = int(dt.datetime(args.end_year, args.end_month, 28, 23, 59, 59, tzinfo=dt.timezone.utc).timestamp())
    op = build_opener()
    all_summary = {}

    for pair in pairs:
        rows = empty_rows()
        stats = stats_zero()
        months_ok, failed, skipped = 0, [], 0
        pdir = parts_root / pair
        pdir.mkdir(parents=True, exist_ok=True)
        for year in range(args.start_year, args.end_year + 1):
            months = range(1, 13 if year != args.end_year else args.end_month + 1)
            for month in months:
                part_path = pdir / ('%04d%02d.json' % (year, month))
                if part_path.exists():
                    part = json.loads(part_path.read_text())
                    merge_part(rows, part)
                    merge_stats(stats, part['stats'])
                    skipped += 1
                    continue
                piece = empty_rows()
                states = {tf: {} for tf in TFS}
                pstats = stats_zero()
                ok = False
                for attempt in range(3):
                    try:
                        blob = download_zip(op, pair, year, month)
                        process_zip(blob, piece, states, pstats, from_s, to_s)
                        ok = True
                        break
                    except Exception as e:
                        print('%s %04d-%02d attempt %d failed: %s' % (pair, year, month, attempt + 1, e), flush=True)
                        time.sleep(3 * (attempt + 1))
                if ok:
                    part_path.write_text(json.dumps({'rows': piece, 'stats': pstats}, separators=(',', ':')))
                    merge_part(rows, {'rows': piece})
                    merge_stats(stats, pstats)
                    months_ok += 1
                    print('fetched %s %04d-%02d ticks=%d' % (pair, year, month, pstats['ticks']), flush=True)
                else:
                    failed.append('%04d-%02d' % (year, month))
                time.sleep(args.delay)
        all_summary[pair] = save_final(out, pair, rows, stats, months_ok, failed, skipped)
        print('DONE %s %s' % (pair, json.dumps(all_summary[pair])), flush=True)

    (out / 'histdata_tick_summary.json').write_text(json.dumps(all_summary, indent=2))
    print('ALL DONE', flush=True)


if __name__ == '__main__':
    main()
