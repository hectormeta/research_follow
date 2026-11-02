#!/usr/bin/env python3
"""Fetch the latest nanophotonics papers and write data.json for the static site.

Sources: arXiv API + Crossref (journal list in config.json). Enrichment: OpenAlex for
corresponding authors, figure thumbnails (arXiv HTML first figure, Springer Nature Fig. 1,
APS key images; images are hot-linked, never copied).
If ANTHROPIC_API_KEY is set, new papers get Chinese summaries from the Claude API;
otherwise the local Cowork task writes them via --todo / --apply.

Journal abstracts are used only for tagging/summarizing and are NOT written to data.json.

usage (run from the repository root):
  python scripts/update.py                         fetch everything (no time limit)
  python scripts/update.py --budget 140 --todo t.json
        resumable: fetched pages are cached in .cache/ for 6 h. If the time budget runs out the
        script exits with code 3; just run it again. When done it writes t.json = papers from the
        last --todo-days days that still need a Chinese summary.
  python scripts/update.py --apply new.json        merge summaries [{"k","zh","tp","rel"}] (no network)
  python scripts/update.py --if-stale 20           skip fetching if data.json is younger than 20 h
"""
import os, sys, json, re, time, datetime as dt, urllib.request, urllib.parse, urllib.error
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
cfg = json.loads((ROOT / 'config.json').read_text(encoding='utf-8'))
CONTACT = cfg.get('contact_email', 'hectormeta@users.noreply.github.com')
UA = {'User-Agent': f'nano-radar/1.1 (https://github.com/hectormeta/research_follow; mailto:{CONTACT})'}
NOW = dt.datetime.now(dt.timezone.utc)
NOW_MS = int(NOW.timestamp() * 1000)
DAY_MS = 864e5
WINDOW = int(cfg.get('window_days', 31))
BROAD = re.compile(cfg['prefilter'], re.I)
API_KEY = os.environ.get('ANTHROPIC_API_KEY', '').strip()
MODEL = os.environ.get('CLAUDE_MODEL', '').strip() or 'claude-haiku-4-5'
MAX_SUMMARIES = int(os.environ.get('MAX_SUMMARIES', '1500'))
TOPIC_IDS = {t['name']: t['id'] for t in cfg['topics']}
CTX = re.compile(cfg['context'], re.I)
TOPIC_RE = [(t, re.compile(t['re'], re.I)) for t in cfg['topics']]
SRC = {s['id']: s for s in cfg['sources']}
KW_RE = [(k['id'], re.compile(k['re'], re.I)) for k in cfg.get('keywords', [])]
TZ = dt.timezone(dt.timedelta(hours=8))  # day boundaries in Beijing time
RAW = ROOT / '.cache'
RAW_TTL = 6 * 3600
AX_MAX = int(cfg.get('arxiv_max', 2500))
DEADLINE = None


class OutOfTime(Exception):
    pass


def check_time():
    if DEADLINE and time.time() > DEADLINE:
        raise OutOfTime()


def keywords(text):
    return [i for i, rx in KW_RE if rx.search(text)]


def rule_topics(text):
    ctx = bool(CTX.search(text))
    prim = [t['id'] for t, rx in TOPIC_RE if not t.get('secondary') and rx.search(text) and (not t.get('ctx') or ctx)]
    if prim:
        prim += [t['id'] for t, rx in TOPIC_RE if t.get('secondary') and rx.search(text)]
    return prim


def get(url, timeout=60, data=None, headers=None, tries=5, want_url=False):
    err = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers={**UA, **(headers or {})})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read().decode('utf-8', 'replace')
                return (body, r.geturl()) if want_url else body
        except urllib.error.HTTPError as e:
            err = e
            if e.code not in (429, 500, 502, 503, 504):
                raise
            ra = e.headers.get('Retry-After') if e.headers else None
            time.sleep(min(60, float(ra)) if ra and ra.replace('.', '').isdigit() else 6 * (i + 1))
        except Exception as e:
            err = e
            time.sleep(4 * (i + 1))
    raise err


def clean(s):
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', s or '')).strip()


def iso_ms(s):
    return int(dt.datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp() * 1000)


def jload(path, default):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except Exception:
        return default


def jsave(path, obj):
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')


# ---------------------------------------------------------------- sources
def fetch_arxiv():
    ns = {'a': 'http://www.w3.org/2005/Atom', 'x': 'http://arxiv.org/schemas/atom'}
    base = ('https://export.arxiv.org/api/query?search_query=' + urllib.parse.quote(cfg['arxiv_query'])
            + '&sortBy=submittedDate&sortOrder=descending&max_results=100&start=')
    pf = RAW / 'arxiv_pages.json'
    st = jload(pf, {})
    if time.time() - st.get('t0', 0) > RAW_TTL:
        st = {'t0': time.time(), 'pages': {}}
    out = []
    for start in range(0, AX_MAX, 100):
        entries = st['pages'].get(str(start))
        if entries is None:
            check_time()
            root = ET.fromstring(get(base + str(start), 90))
            entries = []
            for e in root.findall('a:entry', ns):
                aid = re.sub(r'v\d+$', '', e.findtext('a:id', '', ns).split('/abs/')[-1])
                if not aid:
                    continue
                pc = e.find('x:primary_category', ns)
                entries.append({'key': 'arxiv:' + aid, 'src': 'arxiv',
                                'sub': pc.get('term') if pc is not None else '',
                                'title': clean(e.findtext('a:title', '', ns)),
                                'abs': clean(e.findtext('a:summary', '', ns)),
                                'authors': [clean(a.findtext('a:name', '', ns)) for a in e.findall('a:author', ns)],
                                'ts': iso_ms(e.findtext('a:published', '', ns)),
                                'url': f'https://arxiv.org/abs/{aid}', 'pdf': f'https://arxiv.org/pdf/{aid}'})
            st['pages'][str(start)] = entries
            jsave(pf, st)
            time.sleep(3)  # arXiv API etiquette
        out += entries
        if len(entries) < 100 or (out and NOW_MS - out[-1]['ts'] > WINDOW * DAY_MS):
            break
    return [p for p in out if NOW_MS - p['ts'] <= WINDOW * DAY_MS]


def fetch_crossref(s):
    frm = (NOW - dt.timedelta(days=WINDOW)).date().isoformat()
    items, cursor = [], '*'
    while True:
        check_time()
        url = ('https://api.crossref.org/works?'
               + f'filter=issn:{s["issn"]},from-created-date:{frm},type:journal-article&sort=created&order=desc&rows=1000'
               + '&select=DOI,title,author,created,abstract,URL&mailto=' + urllib.parse.quote(CONTACT)
               + '&cursor=' + urllib.parse.quote(cursor))
        m = json.loads(get(url, 150))['message']
        items += m['items']
        if len(m['items']) < 1000 or not m.get('next-cursor'):
            break
        cursor = m['next-cursor']
    out = []
    for it in items:
        t = clean((it.get('title') or [''])[0])
        if not t:
            continue
        out.append({'key': 'doi:' + it['DOI'].lower(), 'src': s['id'], 'title': t,
                    'abs': re.sub(r'^Abstract\s*', '', clean(it.get('abstract')), flags=re.I),
                    'authors': [' '.join(x for x in (a.get('given'), a.get('family')) if x) or a.get('name', '')
                                for a in it.get('author', [])],
                    'ts': it['created']['timestamp'], 'url': it.get('URL') or 'https://doi.org/' + it['DOI']})
    return out


def fetch_all(threads):
    RAW.mkdir(exist_ok=True)
    outs = {}

    def run(s):
        f = RAW / f"src_{s['id']}.json"
        d = jload(f, None)
        if d and time.time() - d.get('t', 0) < RAW_TTL:
            return s['id'], d['papers'], d['err']
        try:
            lst = fetch_arxiv() if s['id'] == 'arxiv' else fetch_crossref(s)
            err = None
        except OutOfTime:
            return None
        except Exception as e:
            lst, err = [], f'{type(e).__name__}: {e}'[:160]
        lst = [p for p in lst if BROAD.search(p['title'] + ' ' + p.get('abs', ''))]
        if not err:  # never cache failures, so the next run retries
            jsave(f, {'t': time.time(), 'papers': lst, 'err': err})
        return s['id'], lst, err

    ax = [s for s in cfg['sources'] if s['id'] == 'arxiv']
    cr = [s for s in cfg['sources'] if s['id'] != 'arxiv']
    with ThreadPoolExecutor(1) as ax_pool, ThreadPoolExecutor(threads) as cr_pool:
        futs = [ax_pool.submit(run, s) for s in ax] + [cr_pool.submit(run, s) for s in cr]
        for f in futs:
            r = f.result()
            if r:
                outs[r[0]] = r
    missing = [s['id'] for s in cfg['sources'] if s['id'] not in outs]
    if missing:
        print('time budget used up; still to fetch:', ' '.join(missing), flush=True)
        return None
    return [outs[s['id']] for s in cfg['sources']]


# ---------------------------------------------------------------- enrichment
def paper_doi(p):
    if p['key'].startswith('doi:'):
        return p['key'][4:]
    return '10.48550/arxiv.' + p['key'][6:]


def enrich_authors(papers, threads=4):
    """OpenAlex is_corresponding flags -> authors.json {key: {"au": [[name, 0/1], ...]} | {"x": ts}}"""
    path = ROOT / 'authors.json'
    cache = jload(path, {})
    todo = []
    for p in papers:
        c = cache.get(p['key'])
        age = NOW_MS - p['ts']
        if c is None or ('x' in c and age < 21 * DAY_MS and NOW_MS - c['x'] > 2 * DAY_MS):
            todo.append(p)
    todo.sort(key=lambda p: -p['ts'])
    by_doi = {paper_doi(p).lower(): p['key'] for p in todo}
    keys = list(by_doi)
    batches = [keys[i:i + 40] for i in range(0, len(keys), 40)]
    done = 0

    def q(batch):
        if DEADLINE and time.time() > DEADLINE:
            return None
        url = ('https://api.openalex.org/works?per-page=50&select=doi,authorships&mailto=' + urllib.parse.quote(CONTACT)
               + '&filter=doi:' + '|'.join(urllib.parse.quote(d, safe='/.') for d in batch))
        try:
            return batch, json.loads(get(url, 25, tries=2)).get('results', [])
        except Exception as e:
            print('openalex batch failed:', str(e)[:120], flush=True)
            return None
    with ThreadPoolExecutor(threads) as ex:
        for r in ex.map(q, batches):
            if not r:
                continue
            batch, res = r
            seen = set()
            for w in res:
                d = (w.get('doi') or '').lower().replace('https://doi.org/', '')
                k = by_doi.get(d)
                if not k:
                    continue
                seen.add(d)
                au = [[(a.get('author') or {}).get('display_name') or a.get('raw_author_name') or '',
                       1 if a.get('is_corresponding') else 0] for a in w.get('authorships', [])]
                cache[k] = {'au': au} if any(f for _, f in au) else {'x': NOW_MS}
            for d in batch:
                if d not in seen:
                    cache[by_doi[d]] = {'x': NOW_MS}
            done += len(batch)
    keep = {p['key'] for p in papers}
    cache = {k: v for k, v in cache.items() if k in keep}
    jsave(path, cache)
    print(f'openalex: checked {done}/{len(keys)}', flush=True)
    return cache, done < len(keys)


ARXIV_IMG = re.compile(r'<figure[^>]*>.*?<img[^>]+src="([^"]+)"', re.S | re.I)


def thumb_static(p):
    """Deterministic figure URLs that need no page fetch (the browser hides them if missing)."""
    k = p['key']
    m = re.match(r'doi:10\.(1038|1186)/s(\d{5})-0?(\d{2,3})-(\d+)-\w$', k)
    if m:
        yr = 2000 + int(m.group(3)) % 100
        doi = k[4:]
        return (f"https://media.springernature.com/lw685/springer-static/image/art%3A{urllib.parse.quote(doi, safe='')}"
                f"/MediaObjects/{m.group(2)}_{yr}_{int(m.group(4))}_Fig1_HTML.png")
    code = SRC.get(p['src'], {}).get('aps')
    if code and k.startswith('doi:10.1103/'):
        return f"https://cdn.journals.aps.org/journals/{code}/key_images/10.1103/{k.split('/', 1)[1]}.png"
    return None


def enrich_thumbs(papers, limit=150, threads=1):
    """arXiv: first figure of the HTML version. thumbs.json {key: url | ""}."""
    path = ROOT / 'thumbs.json'
    cache = jload(path, {})
    todo = [p for p in papers if p['src'] == 'arxiv' and p['key'] not in cache]
    todo.sort(key=lambda p: -p['ts'])
    todo = todo[:limit]

    def one(p):
        if DEADLINE and time.time() > DEADLINE:
            return None
        aid = p['key'][6:]
        time.sleep(1.5)  # arxiv.org shares one rate limit with the API
        try:
            html, final = get(f'https://arxiv.org/html/{aid}', 20, tries=1, want_url=True)
        except urllib.error.HTTPError as e:
            return (p['key'], '') if e.code == 404 else None
        except Exception:
            return None
        m = ARXIV_IMG.search(html)
        if not m:
            return p['key'], ''
        return p['key'], urllib.parse.urljoin(final, m.group(1))
    n = 0
    with ThreadPoolExecutor(threads) as ex:
        for r in ex.map(one, todo):
            if r:
                cache[r[0]] = r[1]; n += 1
    keep = {p['key'] for p in papers}
    cache = {k: v for k, v in cache.items() if k in keep}
    jsave(path, cache)
    recent = sum(1 for p in papers if p['src'] == 'arxiv' and p['key'] not in cache and NOW_MS - p['ts'] < 3 * DAY_MS)
    print(f'thumbs: looked up {n}/{len(todo)} arXiv figures, {recent} recent still missing', flush=True)
    return cache, recent


def compact_authors(names, flags):
    """Keep first 3, last 2 and corresponding authors; '…' marks gaps.
    flag 1 = corresponding (OpenAlex), 2 = last author presumed corresponding."""
    n = len(names)
    if flags and any(flags):
        fl = [1 if f else 0 for f in flags]
    else:
        fl = [0] * n
        if n > 1:
            fl[-1] = 2
    keep = set(range(min(3, n))) | set(range(max(0, n - 2), n)) | set([i for i in range(n) if fl[i] == 1][:4])
    out, prev = [], -1
    for i in sorted(keep):
        if i - prev > 1:
            out.append(['…'])
        out.append([names[i], fl[i]])
        prev = i
    return out, n


# ---------------------------------------------------------------- summaries
PROMPT = (
    '你是微纳光学领域的研究助理。输入是一个 JSON 数组，每项含 k（键）、t（标题）、a（摘要，可能为空）。'
    '对每一项输出：zh = 一句中文要点，不超过 60 字，说清楚做了什么和关键结果或数值，专业缩写保留英文（如 BIC、OAM、Q 值）；'
    '摘要为空时依据标题概括，不要编造数值。'
    'tp = 主题标签数组，只能取自 ' + json.dumps([t['name'] for t in cfg['topics']], ensure_ascii=False) + '，通常 1–3 个。'
    'rel = 与微纳光学、超表面、超材料、拓扑光学、光场调控、光学传感、光计算、二维材料光子学、片上光学、计算成像、非线性与量子光子学这一研究方向的相关度，'
    '0 表示无关（如电子或磁性拓扑、无线通信算法、纯生物化学），1 表示部分相关，2 表示核心相关。'
    'kw = 2–4 个最能代表这篇工作的具体英文关键词（材料、结构、效应或方法，如 "quasi-BIC"、"thin-film lithium niobate"、"diffractive neural network"；不要用 "metasurface"、"optics" 这类泛词）。'
    '只输出 JSON 数组，形如 [{"k":"...","zh":"...","tp":["..."],"rel":2,"kw":["..."]}]，不要输出任何其他文字。'
)


def norm_summary(o):
    try:
        rel = int(o.get('rel', 1))
    except Exception:
        rel = 1
    ids = set(TOPIC_IDS.values())
    tp = [TOPIC_IDS.get(n, n) for n in o.get('tp', [])]
    kw = [str(x).strip()[:40] for x in (o.get('kw') or []) if str(x).strip()][:5]
    return {'zh': str(o.get('zh', '')).strip(), 'tp': [t for t in tp if t in ids], 'rel': rel, 'kw': kw, 't': NOW_MS}


def summarize(batch):
    body = json.dumps({
        'model': MODEL, 'max_tokens': 4096, 'system': PROMPT,
        'messages': [{'role': 'user', 'content': json.dumps(
            [{'k': p['key'], 't': p['title'], 'a': p['abs'][:900]} for p in batch], ensure_ascii=False)}],
    }).encode()
    txt = get('https://api.anthropic.com/v1/messages', 120, body,
              {'x-api-key': API_KEY, 'anthropic-version': '2023-06-01', 'content-type': 'application/json'}, tries=3)
    out = ''.join(c.get('text', '') for c in json.loads(txt).get('content', []))
    arr = json.loads(out[out.index('['): out.rindex(']') + 1])
    return {o['k']: norm_summary(o) for o in arr if isinstance(o, dict) and o.get('k')}


def apply_summaries(path):
    new = jload(path, [])
    if isinstance(new, dict):
        new = [dict(v, k=k) for k, v in new.items()]
    cache = jload(ROOT / 'summaries.json', {})
    got = {o['k']: norm_summary(o) for o in new if isinstance(o, dict) and o.get('k') and o.get('zh')}
    cache.update(got)
    jsave(ROOT / 'summaries.json', cache)
    data = jload(ROOT / 'data.json', {})
    n = 0
    for q in data.get('papers', []):
        s = got.get(q['key'])
        if s:
            q.update({'zh': s['zh'], 'tp': s['tp'], 'rel': s['rel']}); n += 1
            if s.get('kw'):
                q['ak'] = s['kw']
    jsave(ROOT / 'data.json', data)
    build_trends(data)
    print(f'applied {len(got)} summaries, {n} matched papers in data.json')


def build_trends(data):
    """trends.json: daily counts per topic / keyword / source group. Days inside the current window
    are recomputed from data.json; older days are kept, so history grows day by day."""
    path = ROOT / 'trends.json'
    old = jload(path, {}).get('days', {})
    days = {}
    for q in data['papers']:
        if not q.get('rt'):  # rule-based relevance/topics only, so days with and without Claude labels stay comparable
            continue
        d = dt.datetime.fromtimestamp(q['ts'] / 1000, TZ).date().isoformat()
        e = days.setdefault(d, {'n': 0, 'ax': 0, 't': {}, 'k': {}, 'g': {}})
        e['n'] += 1
        if q['src'] == 'arxiv':
            e['ax'] += 1
        for t in set(q['rt']):
            e['t'][t] = e['t'].get(t, 0) + 1
        for k in q.get('kw', []):
            e['k'][k] = e['k'].get(k, 0) + 1
        g = SRC.get(q['src'], {}).get('group', '其他')
        e['g'][g] = e['g'].get(g, 0) + 1
    oldest = min(days) if days else None
    for d, e in old.items():
        if oldest is None or d < oldest:
            days[d] = e
    # the oldest window day is only partially covered once it slides; keep the larger count
    if oldest and oldest in old and old[oldest]['n'] > days[oldest]['n']:
        days[oldest] = old[oldest]
    days = dict(sorted(days.items())[-730:])
    jsave(path, {'generated': data.get('generated', NOW_MS), 'days': days})
    print('trends days', len(days))


# ---------------------------------------------------------------- main
def main():
    global DEADLINE
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--budget', type=float, default=0, help='seconds; 0 = unlimited')
    ap.add_argument('--threads', type=int, default=int(os.environ.get('CR_THREADS', '2')))
    ap.add_argument('--todo', help='write papers still lacking a summary to this JSON file')
    ap.add_argument('--todo-days', type=float, default=10)
    ap.add_argument('--apply', help='merge summaries from this JSON file and exit')
    ap.add_argument('--if-stale', type=float, default=0, help='skip if data.json is younger than N hours')
    ap.add_argument('--no-enrich', action='store_true')
    args = ap.parse_args()

    if args.apply:
        return apply_summaries(args.apply)

    prev_path = ROOT / 'data.json'
    prev = jload(prev_path, {})
    if args.if_stale and prev.get('generated') and NOW_MS - prev['generated'] < args.if_stale * 3600e3:
        print(f"data.json is {(NOW_MS - prev['generated']) / 3600e3:.1f} h old; skipping fetch")
        return
    if args.budget:
        DEADLINE = time.time() + args.budget

    outs = fetch_all(args.threads)
    if outs is None:
        sys.exit(3)

    results, meta = [], {}
    for sid, lst, err in outs:
        meta[sid] = {'n': len(lst), 'err': err}
        results += lst
        print(f'{sid:10s} {len(lst):4d} {err or ""}', flush=True)

    failed = {k for k, v in meta.items() if v['err']}
    if failed and prev.get('papers'):
        keep = [p for p in prev['papers'] if p['src'] in failed and NOW_MS - p['ts'] < WINDOW * DAY_MS]
        results += keep
        for k in failed:
            meta[k]['n'] = sum(1 for p in keep if p['src'] == k)
    if all(v['err'] for v in meta.values()):
        sys.exit('all sources failed; data.json left unchanged')

    seen, uniq = set(), []
    for p in results:
        if p['key'] not in seen:
            seen.add(p['key']); uniq.append(p)
    results = uniq
    for p in results:
        if 'rt' not in p or 'abs' in p:
            p['rt'] = rule_topics(p['title'] + ' ' + p.get('abs', ''))

    authors, thumbs, incomplete = {}, {}, False
    if not args.no_enrich:
        authors, incomplete = enrich_authors(results)
        thumbs, missing = enrich_thumbs(results) if not meta['arxiv']['err'] else (jload(ROOT / 'thumbs.json', {}), 0)
        incomplete = incomplete or missing > 0

    cache = jload(ROOT / 'summaries.json', {})
    if API_KEY:
        todo = sorted([p for p in results if p['key'] not in cache and p['rt'] and 'abs' in p], key=lambda p: -p['ts'])[:MAX_SUMMARIES]
        batches = [todo[i:i + 12] for i in range(0, len(todo), 12)]
        print(f'summarizing {len(todo)} papers in {len(batches)} batches with {MODEL}', flush=True)

        def run(b):
            try:
                return summarize(b)
            except Exception as e:
                print('summary batch failed:', str(e)[:200], flush=True)
                return {}
        with ThreadPoolExecutor(4) as ex:
            for r in ex.map(run, batches):
                cache.update(r)
    cache = {k: v for k, v in cache.items() if NOW_MS - v.get('t', NOW_MS) < 2 * WINDOW * DAY_MS or k in seen}
    jsave(ROOT / 'summaries.json', cache)

    papers, todo = [], []
    for p in sorted(results, key=lambda p: -p['ts']):
        q = {k: p[k] for k in ('key', 'src', 'title', 'ts', 'url') if k in p}
        for k in ('sub', 'pdf'):
            if p.get(k):
                q[k] = p[k]
        a = authors.get(p['key'], {}).get('au')
        if a:
            q['au'], q['na'] = compact_authors([x[0] for x in a], [x[1] for x in a])
        elif p.get('au'):                       # carried over from previous data.json
            q['au'], q['na'] = p['au'], p.get('na', len(p['au']))
        elif p.get('authors'):
            q['au'], q['na'] = compact_authors(p['authors'], None)
        img = thumbs.get(p['key']) if p['src'] == 'arxiv' else thumb_static(p)
        img = img or p.get('img')
        if img:
            q['img'] = img
        if p['src'] == 'arxiv' and p.get('abs'):
            q['abs'] = p['abs'][:1100]
        q['rt'] = p['rt']
        if q['rt']:
            kk = keywords(p['title'] + ' ' + p.get('abs', '')) if 'abs' in p else p.get('kw', [])
            if kk:
                q['kw'] = kk
        s = cache.get(p['key'])
        if s:
            q.update({'zh': s['zh'], 'tp': s['tp'], 'rel': s['rel']})
            if s.get('kw'):
                q['ak'] = s['kw']
        elif 'abs' in p and q['rt'] and NOW_MS - p['ts'] < args.todo_days * DAY_MS:
            todo.append({'k': p['key'], 't': p['title'], 'a': p['abs'][:700]})
        papers.append(q)

    data = {'generated': NOW_MS, 'sources': meta, 'papers': papers}
    jsave(prev_path, data)
    build_trends(data)
    if args.todo:
        Path(args.todo).write_text(json.dumps(todo, ensure_ascii=False, indent=0), encoding='utf-8')
    print('papers', len(papers), 'with summary', sum(1 for q in papers if 'zh' in q), 'todo', len(todo),
          'with thumb', sum(1 for q in papers if 'img' in q), 'size', prev_path.stat().st_size)
    if incomplete and args.budget:
        print('enrichment incomplete (time budget); run again to continue', flush=True)
        sys.exit(3)


if __name__ == '__main__':
    main()
