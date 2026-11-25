#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fetch AI / AI4S / 科创机会 / 深圳政策 / 学术基金 news and write news/data.json.

Sources are configured in config.json -> "news" (feeds). Two feed types:
  - rss:     RSS 2.0 / Atom XML
  - gnews:   Google News RSS search (aggregator, works behind site anti-bot pages)
  - arxiv:   arXiv API, filtered to AI-for-science preprints

Pure stdlib. Failure of one feed never aborts the run; failed feeds are recorded
in `err`, and the previous window's items are kept in data.json (carry-over).

usage (repo root):
  python3 scripts/update_news.py
  python3 scripts/update_news.py --if-stale 20   # skip if data.json younger than 20 h
"""
import os, re, sys, json, time, gzip, hashlib, html, datetime as dt, urllib.request, urllib.parse, urllib.error
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
cfg = json.loads((ROOT / 'config.json').read_text(encoding='utf-8'))
NEWS = cfg.get('news', {})
CONTACT = cfg.get('contact_email', 'hectormeta@users.noreply.github.com')
UA = {'User-Agent': f'nano-radar/1.1 (https://github.com/hectormeta/research_follow; mailto:{CONTACT})'}
NOW = dt.datetime.now(dt.timezone.utc)
NOW_MS = int(NOW.timestamp() * 1000)
DAY_MS = 864e5
WINDOW = int(NEWS.get('window_days', 14)) * DAY_MS
MAX_PER_CAT = int(NEWS.get('max_per_cat', 40))
OUT = ROOT / 'news' / 'data.json'
RAW = ROOT / '.cache' / 'news'
RAW_TTL = 6 * 3600

AI_WORD = re.compile(r'neural network|machine learning|deep learning|large language model|\bAI\b|artificial intelligence|transformer|LLM|agent', re.I)
SCI_WORD = re.compile(NEWS.get('science_terms', 'protein|material|chemistry|physics|climate|fusion|genome|molecule|drug|biology|cell|crystal|catalyst|disease|patient'), re.I)
CJK = re.compile(r'[\u4e00-\u9fff]')

_FILTERS = NEWS.get('filters', {})
FILTER_RE = {}
for cid, f in _FILTERS.items():
    must = re.compile(f.get('must', ''), re.I) if f.get('must') else None
    excl = re.compile(f.get('exclude', ''), re.I) if f.get('exclude') else None
    FILTER_RE[cid] = (must, excl)


def pass_filter(cat, text, src=''):
    must, excl = FILTER_RE.get(cat, (None, None))
    blob = text + ' ' + (src or '')
    if excl and excl.search(blob):
        return False
    if must and not must.search(text):
        return False
    return True


def get(url, timeout=30, tries=4):
    err = None
    headers = {**UA, 'Accept-Encoding': 'identity'}
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                if raw[:2] == b'\x1f\x8b':
                    raw = gzip.decompress(raw)
                return raw.decode('utf-8', 'replace')
        except urllib.error.HTTPError as e:
            err = e
            if e.code not in (429, 500, 502, 503, 504):
                raise
            time.sleep(min(30, 5 * (i + 1)))
        except Exception as e:
            err = e
            time.sleep(3 * (i + 1))
    raise err


def cache_get(key, url):
    RAW.mkdir(parents=True, exist_ok=True)
    f = RAW / (hashlib.md5(key.encode()).hexdigest() + '.xml')
    if f.exists():
        age = NOW_MS - f.stat().st_mtime * 1000
        if age < RAW_TTL:
            return f.read_text(encoding='utf-8')
    body = get(url)
    f.write_text(body, encoding='utf-8')
    return body


def clean(s):
    s = html.unescape(re.sub(r'<[^>]+>', ' ', s or ''))
    return re.sub(r'\s+', ' ', s).strip()


def parse_date(s):
    if not s:
        return None
    s = s.strip()
    try:
        return int(parsedate_to_datetime(s).timestamp() * 1000)
    except Exception:
        pass
    try:
        return int(dt.datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp() * 1000)
    except Exception:
        return None


def norm_title(t):
    return re.sub(r'[\s\W_]+', '', (t or '').lower())


def make_item(feed, title, summary, src, url, ts, tags=None):
    title = clean(title)
    if not title or not url:
        return None
    summary = clean(summary)[:240]
    k = hashlib.md5(url.encode()).hexdigest()[:16]
    lang = 'zh' if CJK.search(title) else 'en'
    return {
        'k': k, 'cat': feed['cat'], 'title': title, 'summary': summary,
        'src': clean(src) or feed['name'], 'url': url, 'ts': ts or NOW_MS,
        'lang': lang, 'tags': tags or [],
    }


# ------------------------------------------------------------- feed parsers
def parse_rss(feed, body):
    out = []
    try:
        root = ET.fromstring(body)
    except Exception:
        # Atom or broken feed: try channel/entry anyway below
        root = ET.fromstring(body)
    # RSS 2.0
    for item in root.iter('item'):
        title = item.findtext('title')
        link = item.findtext('link')
        desc = item.findtext('description') or item.findtext('{http://purl.org/rss/1.0/modules/content/}encoded')
        pub = item.findtext('pubDate') or item.findtext('{http://purl.org/dc/elements/1.1/}date')
        src_el = item.find('source')
        src = src_el.text if src_el is not None else None
        it = make_item(feed, title, desc, src, link, parse_date(pub))
        if it:
            out.append(it)
    # Atom
    ns = '{http://www.w3.org/2005/Atom}'
    for entry in root.iter(ns + 'entry'):
        title = entry.findtext(ns + 'title')
        link = None
        for l in entry.findall(ns + 'link'):
            if l.get('rel') in (None, 'alternate'):
                link = l.get('href')
                break
        desc = entry.findtext(ns + 'summary') or entry.findtext(ns + 'content')
        pub = entry.findtext(ns + 'published') or entry.findtext(ns + 'updated')
        it = make_item(feed, title, desc, feed['name'], link, parse_date(pub))
        if it:
            out.append(it)
    return out


def gnews_url(feed):
    q = urllib.parse.quote(feed['q'])
    when = feed.get('when', '7d')
    params = {'hl': feed['hl'], 'gl': feed['gl'], 'ceid': feed['ceid'], 'q': f"{feed['q']} when:{when}"}
    return 'https://news.google.com/rss/search?' + urllib.parse.urlencode(params)


def parse_gnews(feed, body):
    out = []
    root = ET.fromstring(body)
    for item in root.iter('item'):
        title = item.findtext('title')
        link = item.findtext('link')
        pub = item.findtext('pubDate')
        desc = item.findtext('description')
        src_el = item.find('source')
        src = src_el.text if src_el is not None else feed['name']
        it = make_item(feed, title, desc, src, link, parse_date(pub))
        if it:
            out.append(it)
    return out


def parse_html(feed, body):
    out = []
    for li in re.findall(r'<li[^>]*>(.*?)</li>', body, re.S):
        m = re.search(r'<a[^>]*href="([^"]+)"[^>]*?(?:title="([^"]*)")?>(.*?)</a>', li, re.S)
        if not m:
            continue
        url = urllib.parse.urljoin(feed['url'], m.group(1))
        title = clean(m.group(2)) or clean(m.group(3))
        if not title:
            continue
        dm = re.search(r'(20\d{2})-(\d{2})-(\d{2})', li)
        ts = None
        if dm:
            ts = int(dt.datetime(int(dm[1]), int(dm[2]), int(dm[3]),
                                 tzinfo=dt.timezone(dt.timedelta(hours=8))).timestamp() * 1000)
        it = make_item(feed, title, '', feed['name'], url, ts)
        if it:
            out.append(it)
    return out


def parse_arxiv(feed, body):
    out = []
    ns = '{http://www.w3.org/2005/Atom}'
    root = ET.fromstring(body)
    for entry in root.iter(ns + 'entry'):
        title = entry.findtext(ns + 'title')
        summ = entry.findtext(ns + 'summary') or ''
        link = None
        for l in entry.findall(ns + 'link'):
            if l.get('rel') == 'alternate':
                link = l.get('href')
                break
        pub = entry.findtext(ns + 'published')
        text = (clean(title) + ' ' + clean(summ))
        if not (AI_WORD.search(text) and SCI_WORD.search(text)):
            continue
        authors = [a.findtext(ns + 'name') for a in entry.findall(ns + 'author')]
        authors = ', '.join(x for x in authors if x)[:80]
        it = make_item(feed, title, summ, 'arXiv', link, parse_date(pub), tags=[authors] if authors else [])
        if it:
            out.append(it)
    return out


def reclassify(feed, item):
    rules = feed.get('reclass')
    if not rules:
        return item['cat']
    text = (item['title'] + ' ' + item['summary']).lower()
    for cat, pattern in rules.items():
        if re.search(pattern, text, re.I):
            return cat
    return item['cat']


def fetch_feed(feed):
    if feed['type'] == 'rss':
        body = cache_get(feed['id'], feed['url'])
        return parse_rss(feed, body)
    if feed['type'] == 'gnews':
        body = cache_get(feed['id'], gnews_url(feed))
        return parse_gnews(feed, body)
    if feed['type'] == 'html':
        body = cache_get(feed['id'], feed['url'])
        return parse_html(feed, body)
    if feed['type'] == 'arxiv':
        q = urllib.parse.quote(feed['query'])
        url = f'https://export.arxiv.org/api/query?search_query={q}&sortBy=submittedDate&sortOrder=descending&max_results=40'
        body = cache_get(feed['id'], url)
        return parse_arxiv(feed, body)
    return []


def main():
    stale = None
    if '--if-stale' in sys.argv:
        i = sys.argv.index('--if-stale')
        stale = int(sys.argv[i + 1]) * 3600 * 1000
        if OUT.exists() and NOW_MS - OUT.stat().st_mtime * 1000 < stale:
            print('data.json fresh enough; skip fetch')
            return

    old = json.loads(OUT.read_text(encoding='utf-8')) if OUT.exists() else {}
    kept = [p for p in old.get('items', [])
            if NOW_MS - p.get('ts', 0) <= WINDOW
            and pass_filter(p['cat'], p['title'] + ' ' + p.get('summary', ''), p.get('src', ''))]
    by_key = {p['k']: p for p in kept}
    by_title = {norm_title(p['title']): p['k'] for p in kept}
    errs, fresh = [], []

    for feed in NEWS.get('feeds', []):
        try:
            items = fetch_feed(feed)
            # optional feed-level include / exclude regex
            inc = feed.get('include') or feed.get('re')
            exc = feed.get('exclude')
            kept = 0
            for it in items:
                ftext = it['title'] + ' ' + it['summary']
                if inc and not re.search(inc, ftext, re.I):
                    continue
                if exc and re.search(exc, ftext, re.I):
                    continue
                if NOW_MS - it['ts'] > WINDOW:
                    continue
                it['cat'] = reclassify(feed, it)
                text = it['title'] + ' ' + it['summary']
                if not pass_filter(it['cat'], text, it['src']):
                    continue
                tk = norm_title(it['title'])
                if tk in by_title and by_title[tk] != it['k']:
                    continue
                if it['k'] in by_key:
                    if it['ts'] > by_key[it['k']]['ts']:
                        by_key[it['k']] = it
                else:
                    by_key[it['k']] = it
                    by_title[tk] = it['k']
                fresh.append(it)
                kept += 1
            print(f"  {feed['id']:14s} {len(items):3d} fetched, {kept:3d} kept")
        except Exception as e:
            errs.append({'id': feed['id'], 'name': feed['name'], 'err': str(e)[:160]})
            print(f"  {feed['id']:14s} ERROR {e}")

    items = list(by_key.values())
    items.sort(key=lambda p: -p['ts'])
    # per-category cap
    capped = []
    seen_cat = {}
    for p in items:
        n = seen_cat.get(p['cat'], 0)
        if n < MAX_PER_CAT:
            capped.append(p)
            seen_cat[p['cat']] = n + 1

    payload = {
        'generated': NOW_MS,
        'window_days': NEWS.get('window_days', 14),
        'cats': NEWS.get('cats', []),
        'items': capped,
        'err': errs,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    counts = {}
    for p in capped:
        counts[p['cat']] = counts.get(p['cat'], 0) + 1
    print(f"total {len(capped)} items; by cat: {counts}; errs {len(errs)}")


if __name__ == '__main__':
    main()
