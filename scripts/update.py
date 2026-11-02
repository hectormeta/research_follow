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
PHYS_CTX = re.compile(cfg.get('physics_context', '(?!)'), re.I)
SKIP_TITLE = re.compile(cfg.get('skip_title', '(?!)'), re.I)
OA_KEY = os.environ.get('OPENALEX_API_KEY', '').strip() or cfg.get('openalex_key', '')
TOPIC_RE = [(t, re.compile(t['re'], re.I)) for t in cfg['topics']]
SRC = {s['id']: s for s in cfg['sources']}
KW_RE = [(k['id'], re.compile(k['re'], re.I)) for k in cfg.get('keywords', [])]
TZ = dt.timezone(dt.timedelta(hours=8))  # day boundaries in Beijing time
RAW = ROOT / '.cache'
RAW_TTL = 6 * 3600
AX_MAX = int(cfg.get('arxiv_max', 2500))
DEADLINE = None
START = cfg.get('start_date', '2026-01-01')
ARCH = ROOT / 'archive'


class OutOfTime(Exception):
    pass


def check_time():
    if DEADLINE and time.time() > DEADLINE:
        raise OutOfTime()


def keywords(text):
    return [i for i, rx in KW_RE if rx.search(text)]


def rule_topics(text, physics=False):
    # physics journals: general wave physics (BIC, EPs, non-Hermitian, ...) counts even without explicit optics words
    ctx = bool(CTX.search(text)) or (physics and bool(PHYS_CTX.search(text)))
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
def parse_arxiv(xml):
    ns = {'a': 'http://www.w3.org/2005/Atom', 'x': 'http://arxiv.org/schemas/atom'}
    root = ET.fromstring(xml)
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
    return entries


def fetch_arxiv(month=None):
    """Recent window (month=None) or one calendar month 'YYYY-MM' (history backfill). Pages are cached
    in .cache so an interrupted run resumes."""
    q = cfg['arxiv_query']
    if month:
        d0, d1 = month_bounds(month)
        q = f"({q}) AND submittedDate:[{d0:%Y%m%d}0000 TO {d1:%Y%m%d}2359]"
        pf, ttl = RAW / 'hist' / f'arxiv_{month}.json', 10 ** 9
    else:
        pf, ttl = RAW / 'arxiv_pages.json', RAW_TTL
    base = ('https://export.arxiv.org/api/query?search_query=' + urllib.parse.quote(q)
            + '&sortBy=submittedDate&sortOrder=descending&max_results=100&start=')
    pf.parent.mkdir(parents=True, exist_ok=True)
    st = jload(pf, {})
    if time.time() - st.get('t0', 0) > ttl or 'pages' not in st:
        st = {'t0': time.time(), 'pages': {}}
    out = []
    for start in range(0, 10000 if month else AX_MAX, 100):
        entries = st['pages'].get(str(start))
        if entries is None:
            check_time()
            entries = parse_arxiv(get(base + str(start), 90))
            st['pages'][str(start)] = entries
            jsave(pf, st)
            time.sleep(3)  # arXiv API etiquette
        out += entries
        if len(entries) < 100 or (not month and out and NOW_MS - out[-1]['ts'] > WINDOW * DAY_MS):
            break
    return out if month else [p for p in out if NOW_MS - p['ts'] <= WINDOW * DAY_MS]


def fetch_crossref(s, frm=None, until=None):
    frm = frm or (NOW - dt.timedelta(days=WINDOW)).date().isoformat()
    flt = (f"prefix:{s['prefix']},type:posted-content" if s.get('prefix') else f"issn:{s['issn']},type:journal-article")
    flt += f",from-created-date:{frm}" + (f",until-created-date:{until}" if until else '')
    # page cache so large sources (e.g. Research Square) resume across time-limited runs
    pc = RAW / 'pages' / f"cr_{s['id']}_{frm}_{until or 'now'}.json"
    pc.parent.mkdir(parents=True, exist_ok=True)
    st = jload(pc, {})
    if time.time() - st.get('t0', 0) > RAW_TTL:
        st = {'t0': time.time(), 'items': [], 'cursor': '*', 'done': False}
    while not st['done']:
        check_time()
        url = ('https://api.crossref.org/works?filter=' + flt + '&sort=created&order=desc&rows=1000'
               + '&select=DOI,title,author,created,abstract,URL&mailto=' + urllib.parse.quote(CONTACT)
               + '&cursor=' + urllib.parse.quote(st['cursor']))
        m = json.loads(get(url, 150))['message']
        page = m.get('items', [])
        st['raw'] = st.get('raw', 0) + len(page)
        if s.get('prefix'):   # big preprint servers: keep only candidate papers in the page cache
            page = [i for i in page if BROAD.search(clean((i.get('title') or [''])[0]) + ' ' + clean(i.get('abstract')))]
        st['items'] += page
        st['done'] = len(m.get('items', [])) < 1000 or not m.get('next-cursor') or st['raw'] >= m.get('total-results', 0)
        st['cursor'] = m.get('next-cursor') or st['cursor']
        jsave(pc, st)
    items = st['items']
    out = []
    for it in items:
        t = clean((it.get('title') or [''])[0])
        if not t:
            continue
        doi = it['DOI'].lower()
        out.append({'key': 'doi:' + re.sub(r'/v\d+$|\.v\d+$', '', doi) if s.get('prefix') else 'doi:' + doi,
                    'src': s['id'], 'title': t,
                    'abs': re.sub(r'^Abstract\s*', '', clean(it.get('abstract')), flags=re.I),
                    'authors': [' '.join(x for x in (a.get('given'), a.get('family')) if x) or a.get('name', '')
                                for a in it.get('author', [])],
                    'ts': it['created']['timestamp'], 'url': it.get('URL') or 'https://doi.org/' + it['DOI']})
    return out


def month_list():
    y, m = map(int, START[:7].split('-'))
    out = []
    while (y, m) <= (NOW.year, NOW.month):
        out.append(f'{y:04d}-{m:02d}')
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def month_bounds(month):
    y, m = map(int, month.split('-'))
    d0 = dt.date(y, m, 1)
    d1 = (dt.date(y + 1, 1, 1) if m == 12 else dt.date(y, m + 1, 1)) - dt.timedelta(days=1)
    return d0, d1


def month_of(ts):
    return dt.datetime.fromtimestamp(ts / 1000, TZ).strftime('%Y-%m')


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
        # papers without an abstract (APS, Science, ...) are kept for now and filtered after OpenAlex enrichment
        lst = [p for p in lst if not p.get('abs') or s.get('fallback') or BROAD.search(p['title'] + ' ' + p['abs'])]
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


def oa_abstract(inv):
    if not inv:
        return ''
    pos = {}
    for w, idx in inv.items():
        for i in idx:
            pos[i] = w
    return ' '.join(pos[i] for i in sorted(pos))


def enrich_authors(papers, threads=4):
    """OpenAlex: corresponding authors -> authors.json {key: {"au": [[name, 0/1], ...]} | {"x": ts}, "ab": 1, "rt", "kw"}
    and abstracts for papers that came without one -> .cache/oa_abs.json (session only, never published)."""
    path = ROOT / 'authors.json'
    cache = jload(path, {})
    abs_path = RAW / 'oa_abs.json'
    oabs = jload(abs_path, {})
    todo = []
    for p in papers:
        c = cache.get(p['key'])
        age = NOW_MS - p['ts']
        need_abs = not p.get('abs') and p['key'] not in oabs and not (c or {}).get('ab')
        if c is None or need_abs or ('x' in c and age < 21 * DAY_MS and NOW_MS - c['x'] > 2 * DAY_MS):
            todo.append(p)
    todo.sort(key=lambda p: -p['ts'])
    by_doi = {paper_doi(p).lower(): p['key'] for p in todo}
    keys = list(by_doi)
    batches = [keys[i:i + 25] for i in range(0, len(keys), 25)]
    done = 0

    def q(batch):
        if DEADLINE and time.time() > DEADLINE:
            return None
        url = ('https://api.openalex.org/works?per-page=50&select=doi,authorships,abstract_inverted_index,cited_by_count,best_oa_location&mailto=' + urllib.parse.quote(CONTACT)
               + ('&api_key=' + OA_KEY if OA_KEY else '')
               + '&filter=doi:' + '|'.join(urllib.parse.quote(d, safe='/.') for d in batch))
        try:
            return batch, json.loads(get(url, 20, tries=1)).get('results', [])
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
                old = cache.get(k, {})
                cache[k] = {'au': au} if any(f for _, f in au) else {'x': NOW_MS}
                cache[k]['ab'] = 1
                cache[k]['cc'] = w.get('cited_by_count') or 0
                bo = w.get('best_oa_location') or {}
                if bo.get('pdf_url') and 'arxiv.org' not in (bo.get('pdf_url') or ''):
                    cache[k]['oa'] = bo['pdf_url']
                for f in ('rt', 'kw'):
                    if f in old:
                        cache[k][f] = old[f]
                ab = oa_abstract(w.get('abstract_inverted_index'))
                if ab:
                    oabs[k] = ab[:1500]
            for d in batch:
                if d not in seen:
                    cache[by_doi[d]] = dict(cache.get(by_doi[d], {}), x=NOW_MS, ab=1)
            done += len(batch)
            nb = done // len(batch)
            if nb % 8 == 0:  # save progress so an interrupted run can resume
                jsave(path, cache); RAW.mkdir(exist_ok=True); jsave(abs_path, oabs)
    keep = {p['key'] for p in papers}
    cache = {k: v for k, v in cache.items() if k in keep}
    jsave(path, cache)
    RAW.mkdir(exist_ok=True)
    jsave(abs_path, {k: v for k, v in oabs.items() if k in keep})
    print(f'openalex: checked {done}/{len(keys)}, abstracts known for {len(oabs)}', flush=True)
    return cache, oabs, done < len(keys)


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
    '0 表示无关（如电子或磁性拓扑材料、无线通信算法、纯生物化学），1 表示部分相关，2 表示核心相关；非厄米、奇异点、BIC、拓扑能带等对光学有借鉴意义的通用波动物理理论至少记为 1。'
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
    print(f'applied {len(got)} summaries, {n} matched papers in data.json')


SLIM = ('key', 'src', 'sub', 'title', 'ts', 'url', 'pdf', 'oa', 'au', 'na', 'img', 'rt', 'ta', 'tk', 'kw', 'zh', 'tp', 'rel', 'ak', 'cc')


def publish(p, authors, thumbs, cache, slim=False):
    """raw/carried paper -> published dict (None if filtered out). Journal abstracts never leave this function."""
    k = p['key']
    if p.get('_pub'):                      # already published earlier (carried over); refresh summary only
        q = {f: p[f] for f in SLIM if f in p}
    else:
        phys = bool(SRC.get(p['src'], {}).get('physics'))
        fb = SRC.get(p['src'], {}).get('fallback')   # optics journals: every research paper counts
        a = authors.get(k, {})
        text = p['title'] + ' ' + (p.get('abs') or '')
        if SKIP_TITLE.search(p['title'].strip()):
            return None
        if not p.get('abs') and 'rt' in a:
            rt, kw = a['rt'], a.get('kw', [])          # derived earlier from an OpenAlex abstract
        else:
            if not BROAD.search(text) and not (phys and PHYS_CTX.search(text)) and not fb:
                return None
            rt = rule_topics(text, phys or bool(fb)) or ([fb] if fb else [])
            kw = keywords(text) if rt else []
            if p.get('_oa') and k in authors:
                authors[k]['rt'], authors[k]['kw'] = rt, kw
        ta = None
        if p.get('_oa') or ('rt' in a and not p.get('abs')):
            # abstract came from OpenAlex: also keep title-only topics, which is what the history archive uses,
            # so trend charts compare like with like
            t0 = p['title']
            ta = (rule_topics(t0, phys or bool(fb)) or ([fb] if fb else [])) if (BROAD.search(t0) or (phys and PHYS_CTX.search(t0)) or fb) else []
        q = {f: p[f] for f in ('key', 'src', 'title', 'ts', 'url') if f in p}
        for f in ('sub', 'pdf'):
            if p.get(f):
                q[f] = p[f]
        if a.get('au'):
            q['au'], q['na'] = compact_authors([x[0] for x in a['au']], [x[1] for x in a['au']])
        elif p.get('authors'):
            q['au'], q['na'] = compact_authors(p['authors'], None)
        for f in ('cc', 'oa'):
            if a.get(f):
                q[f] = a[f]
        img = thumbs.get(k) if p['src'] == 'arxiv' else thumb_static(p)
        if img:
            q['img'] = img
        if p['src'] == 'arxiv' and p.get('abs') and not slim:
            q['abs'] = p['abs'][:1100]
        q['rt'] = rt
        if kw:
            q['kw'] = kw
        if ta is not None and ta != rt:
            q['ta'] = ta
            q['tk'] = keywords(p['title']) if ta else []
    s = cache.get(k)
    if s:
        q.update({'zh': s['zh'], 'tp': s['tp'], 'rel': s['rel']})
        if s.get('kw'):
            q['ak'] = s['kw']
    if slim:
        q = {f: q[f] for f in SLIM if f in q}
    return q


def write_archive(papers_by_month, replace_after=None):
    """Merge published papers into archive/YYYY-MM.json. Entries newer than replace_after (ms) are replaced
    by the new set (so papers that dropped out of the window's filters disappear); older ones are kept."""
    ARCH.mkdir(exist_ok=True)
    for m, lst in papers_by_month.items():
        f = ARCH / f'{m}.json'
        old = jload(f, {}).get('papers', [])
        new = {q['key']: q for q in lst if q.get('rt')}
        keep = [q for q in old if q['key'] not in new and (replace_after is None or q['ts'] < replace_after)]
        allp = sorted(keep + list(new.values()), key=lambda q: -q['ts'])
        jsave(f, {'month': m, 'papers': allp})
    idx = []
    for f in sorted(ARCH.glob('20??-??.json')):
        n = len(jload(f, {}).get('papers', []))
        idx.append({'m': f.stem, 'n': n, 'bytes': f.stat().st_size})
    jsave(ARCH / 'index.json', {'generated': NOW_MS, 'start': START, 'months': idx})


def build_trends():
    """trends.json: daily counts per topic / keyword / source group over the whole archive."""
    days = {}
    for f in sorted(ARCH.glob('20??-??.json')):
        for q in jload(f, {}).get('papers', []):
            tt = q.get('ta', q.get('rt'))   # rule-based (title-only where the archive had no abstract)
            if not tt or SRC.get(q['src'], {}).get('trend') is False:
                continue
            d = dt.datetime.fromtimestamp(q['ts'] / 1000, TZ).date().isoformat()
            if d < START:
                continue
            e = days.setdefault(d, {'n': 0, 'ax': 0, 't': {}, 'k': {}, 'g': {}})
            e['n'] += 1
            if q['src'] == 'arxiv':
                e['ax'] += 1
            for t in set(tt):
                e['t'][t] = e['t'].get(t, 0) + 1
            for k in (q['tk'] if 'tk' in q else q.get('kw', [])):
                e['k'][k] = e['k'].get(k, 0) + 1
            g = SRC.get(q['src'], {}).get('group', '其他')
            e['g'][g] = e['g'].get(g, 0) + 1
    days = dict(sorted(days.items()))
    jsave(ROOT / 'trends.json', {'generated': NOW_MS, 'start': START, 'days': days})
    print('trends days', len(days), 'papers', sum(e['n'] for e in days.values()))


def build_explore():
    """Compact indexes for explore.html: x/recent.json (window) and x/hist.json (older archive).
    Row = [source index, minutes since start, topic bitmask, [keyword ints], title, key, zh, claude keywords]."""
    xdir = ROOT / 'x'
    xdir.mkdir(exist_ok=True)
    src_i = {s['id']: i for i, s in enumerate(cfg['sources'])}
    top_i = {t['id']: i for i, t in enumerate(cfg['topics'])}
    t0 = int(dt.datetime.fromisoformat(START + 'T00:00:00+08:00').timestamp() * 1000)
    win = NOW_MS - WINDOW * DAY_MS

    def row(q):
        tt = q.get('ta', q.get('rt')) or []
        if not tt or q['src'] not in src_i or SRC.get(q['src'], {}).get('trend') is False:
            return None
        mask = 0
        for t in tt:
            if t in top_i:
                mask |= 1 << top_i[t]
        kws = [int(k[1:]) for k in (q['tk'] if 'tk' in q else q.get('kw', [])) if k[1:].isdigit()]
        r = [src_i[q['src']], max(0, (q['ts'] - t0) // 60000), mask, kws, q['title'], q['key']]
        if q.get('zh') or q.get('ak'):
            r += [q.get('zh', ''), ' '.join(q.get('ak', []))]
        return r
    recent, hist, seen = [], [], set()
    for q in jload(ROOT / 'data.json', {}).get('papers', []):
        r = row(q)
        if r:
            recent.append(r); seen.add(q['key'])
    for f in sorted(ARCH.glob('20??-??.json')):
        for q in jload(f, {}).get('papers', []):
            if q['key'] in seen or q['ts'] >= win:
                continue
            r = row(q)
            if r:
                hist.append(r); seen.add(q['key'])
    meta = {'start': START, 't0': t0, 'generated': NOW_MS, 'sources': [s['id'] for s in cfg['sources']]}
    jsave(xdir / 'recent.json', dict(meta, rows=recent))
    jsave(xdir / 'hist.json', dict(meta, rows=hist))
    print('explore index', len(recent), len(hist), (xdir / 'recent.json').stat().st_size, (xdir / 'hist.json').stat().st_size, flush=True)


def digest_input(path, days=3):
    """Material for the daily highlights ticker: notable recent papers and keyword momentum."""
    d = jload(ROOT / 'data.json', {})
    ps = d.get('papers', [])
    recent = [q for q in ps if NOW_MS - q['ts'] < days * DAY_MS and q.get('rel', 1) > 0 and q.get('rt')]
    def score(q):
        return (3 if SRC.get(q['src'], {}).get('top') else 0) + (2 if q.get('rel') == 2 else 0) + (1 if q.get('zh') else 0)
    recent.sort(key=lambda q: (-score(q), -q['ts']))
    def last_author(q):
        au = [x for x in q.get('au', []) if len(x) > 1]
        cor = [x[0] for x in au if x[1] == 1] or [x[0] for x in au if x[1] == 2] or [x[0] for x in au[-1:]]
        return cor[0] if cor else ''
    cand = [{'key': q['key'], 'venue': SRC.get(q['src'], {}).get('name', q['src']), 'top': bool(SRC.get(q['src'], {}).get('top')),
             'title': q['title'], 'zh': q.get('zh', ''), 'corresponding': last_author(q), 'n_authors': q.get('na')}
            for q in recent[:40]]
    def kc(lo, hi):
        c = {}
        for q in ps:
            if lo <= NOW_MS - q['ts'] < hi:
                for k in q.get('kw', []):
                    c[k] = c.get(k, 0) + 1
        return c
    cur, prev = kc(0, 7 * DAY_MS), kc(7 * DAY_MS, 28 * DAY_MS)
    names = {k['id']: k['name'] for k in cfg.get('keywords', [])}
    rise = sorted(((cur[k] / (prev.get(k, 0) / 3 + 1), k) for k in cur if cur[k] >= 4), reverse=True)[:10]
    out = {'date': dt.datetime.fromtimestamp(NOW_MS / 1000, TZ).date().isoformat(), 'papers_last_days': len(recent),
           'candidates': cand,
           'rising_keywords_7d_vs_prev3w': [{'keyword': names.get(k, k), 'last7d': cur[k], 'prev_weekly_avg': round(prev.get(k, 0) / 3, 1)} for _, k in rise]}
    Path(path).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding='utf-8')
    print('digest input', len(cand), 'candidates')


def sync_archive_summaries():
    """Copy summaries/topics into archive entries (used after --apply)."""
    cache = jload(ROOT / 'summaries.json', {})
    for f in sorted(ARCH.glob('20??-??.json')):
        d = jload(f, {})
        ch = False
        for q in d.get('papers', []):
            s = cache.get(q['key'])
            if s and q.get('zh') != s['zh']:
                q.update({'zh': s['zh'], 'tp': s['tp'], 'rel': s['rel']})
                if s.get('kw'):
                    q['ak'] = s['kw']
                ch = True
        if ch:
            jsave(f, d)


def crossref_count(s, frm, until):
    flt = (f"prefix:{s['prefix']},type:posted-content" if s.get('prefix') else f"issn:{s['issn']},type:journal-article")
    url = (f'https://api.crossref.org/works?filter={flt},from-created-date:{frm},until-created-date:{until}&rows=0&mailto='
           + urllib.parse.quote(CONTACT))
    return json.loads(get(url, 60))['message']['total-results']


def backfill(threads):
    """Fetch history from START up to the start of the recent window into archive/. Resumable: every job
    (a whole source, or one month of a large source) is cached in .cache/hist/."""
    hist = RAW / 'hist'
    hist.mkdir(parents=True, exist_ok=True)
    win_start = (NOW - dt.timedelta(days=WINDOW)).date()
    months = [m for m in month_list() if month_bounds(m)[0] <= win_start]
    skip = set(cfg.get('history_skip', []))
    srcs = [s for s in cfg['sources'] if s['id'] not in skip]
    cpath = hist / 'counts.json'
    counts = jload(cpath, {})
    todo_c = [s for s in srcs if s['id'] != 'arxiv' and s['id'] not in counts]
    with ThreadPoolExecutor(threads) as ex:
        for s, n in zip(todo_c, ex.map(lambda s: crossref_count(s, START, win_start.isoformat()), todo_c)):
            counts[s['id']] = n
    jsave(cpath, counts)
    jobs = []
    for s in srcs:
        if s['id'] == 'arxiv' or counts.get(s['id'], 0) > 2500:
            jobs += [(s, m) for m in months]
        else:
            jobs.append((s, None))

    def run(job):
        s, m = job
        f = hist / f"src_{s['id']}_{m or 'all'}.json"
        if f.exists():
            return True
        if DEADLINE and time.time() > DEADLINE:
            return False
        try:
            if s['id'] == 'arxiv':
                lst = fetch_arxiv(m)
            elif m:
                d0, d1 = month_bounds(m)
                lst = fetch_crossref(s, d0.isoformat(), min(d1, win_start).isoformat())
            else:
                lst = fetch_crossref(s, START, win_start.isoformat())
        except OutOfTime:
            return False
        except Exception as e:
            print('backfill failed', s['id'], m, str(e)[:100], flush=True)
            return False
        phys = SRC.get(s['id'], {}).get('physics')
        lst = [p for p in lst if s.get('fallback') or (not p.get('abs') and (BROAD.search(p['title']) or (phys and PHYS_CTX.search(p['title']))))
               or (p.get('abs') and BROAD.search(p['title'] + ' ' + p['abs']))]
        for p in lst:   # journal abstracts are only needed for tagging; keep them short in the local cache
            if p['src'] != 'arxiv':
                p['abs'] = (p.get('abs') or '')[:1500]
        jsave(f, lst)
        return True
    ax = [j for j in jobs if j[0]['id'] == 'arxiv']
    cr = sorted([j for j in jobs if j[0]['id'] != 'arxiv'], key=lambda j: -counts.get(j[0]['id'], 0))
    with ThreadPoolExecutor(1) as axp, ThreadPoolExecutor(threads) as crp:
        fa = [axp.submit(run, j) for j in ax]; fc = [crp.submit(run, j) for j in cr]
        res = [f.result() for f in fa + fc]
    missing = res.count(False)
    print(f'backfill: {len(jobs) - missing}/{len(jobs)} jobs fetched', flush=True)
    if missing:
        return False
    authors = jload(ROOT / 'authors.json', {}); thumbs = jload(ROOT / 'thumbs.json', {}); cache = jload(ROOT / 'summaries.json', {})
    win_ms = NOW_MS - WINDOW * DAY_MS
    out = {m: {} for m in months}
    cfg_mtime = (ROOT / 'config.json').stat().st_mtime
    for s, m in jobs:
        sf = hist / f"src_{s['id']}_{m or 'all'}.json"
        pf = hist / f"pub_{s['id']}_{m or 'all'}.json"
        # published rows are cached per job file; recomputed only when the source file or config changes
        if pf.exists() and pf.stat().st_mtime > max(sf.stat().st_mtime, cfg_mtime):
            pubs = jload(pf, [])
        else:
            pubs = [q for q in (publish(p, authors, thumbs, cache, slim=True) for p in jload(sf, [])) if q and q.get('rt')]
            jsave(pf, pubs)
        for q in pubs:
            if q['ts'] >= win_ms or month_of(q['ts']) not in out:
                continue            # the recent window is owned by the daily run
            s2 = cache.get(q['key'])
            if s2:
                q.update({'zh': s2['zh'], 'tp': s2['tp'], 'rel': s2['rel']})
            out[month_of(q['ts'])][q['key']] = q
    for m, d in out.items():
        write_archive({m: list(d.values())}, replace_after=None)
        print('archive', m, len(d), flush=True)
    build_trends()
    build_explore()
    return True


# ---------------------------------------------------------------- main
def main():
    global DEADLINE
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--budget', type=float, default=0, help='seconds; 0 = unlimited')
    ap.add_argument('--threads', type=int, default=int(os.environ.get('CR_THREADS', '2')))  # Crossref polite pool allows 3 concurrent requests
    ap.add_argument('--todo', help='write papers still lacking a summary to this JSON file')
    ap.add_argument('--todo-days', type=float, default=10)
    ap.add_argument('--apply', help='merge summaries from this JSON file and exit')
    ap.add_argument('--if-stale', type=float, default=0, help='skip if data.json is younger than N hours')
    ap.add_argument('--no-enrich', action='store_true')
    ap.add_argument('--backfill', action='store_true', help='fetch history since start_date into archive/')
    ap.add_argument('--digest-in', help='write material for the daily highlights (digest.json) to this file and exit')
    args = ap.parse_args()

    if args.digest_in:
        return digest_input(args.digest_in)
    if args.apply:
        apply_summaries(args.apply)
        sync_archive_summaries()
        build_explore()
        return
    if args.budget:
        DEADLINE = time.time() + args.budget
    if args.backfill:
        sys.exit(0 if backfill(args.threads) else 3)

    prev_path = ROOT / 'data.json'
    prev = jload(prev_path, {})
    if args.if_stale and prev.get('generated') and NOW_MS - prev['generated'] < args.if_stale * 3600e3:
        print(f"data.json is {(NOW_MS - prev['generated']) / 3600e3:.1f} h old; skipping fetch")
        return

    outs = fetch_all(args.threads)
    if outs is None:
        sys.exit(3)
    results, meta = [], {}
    for sid, lst, err in outs:
        meta[sid] = {'n': len(lst), 'err': err}
        results += lst
    failed = {k for k, v in meta.items() if v['err']}
    if failed and prev.get('papers'):
        results += [dict(q, _pub=1) for q in prev['papers'] if q['src'] in failed and NOW_MS - q['ts'] < WINDOW * DAY_MS]
    if all(v['err'] for v in meta.values()):
        sys.exit('all sources failed; data.json left unchanged')
    seen, uniq = set(), []
    for p in results:
        if p['key'] not in seen:
            seen.add(p['key']); uniq.append(p)
    results = uniq

    authors, oabs, thumbs, incomplete = jload(ROOT / 'authors.json', {}), {}, jload(ROOT / 'thumbs.json', {}), False
    if not args.no_enrich:
        authors, oabs, incomplete = enrich_authors([p for p in results if not p.get('_pub')])
        if not meta.get('arxiv', {}).get('err'):
            thumbs, missing = enrich_thumbs(results)
            incomplete = incomplete or missing > 0
    for p in results:
        if not p.get('abs') and oabs.get(p['key']):
            p['abs'] = oabs[p['key']]; p['_oa'] = 1

    cache = jload(ROOT / 'summaries.json', {})
    if API_KEY:
        todo = sorted([p for p in results if p['key'] not in cache and not p.get('_pub') and p.get('abs') is not None], key=lambda p: -p['ts'])[:MAX_SUMMARIES]
        batches = [todo[i:i + 12] for i in range(0, len(todo), 12)]
        print(f'summarizing up to {len(todo)} papers with {MODEL}', flush=True)

        def run(b):
            try:
                return summarize(b)
            except Exception as e:
                print('summary batch failed:', str(e)[:200], flush=True)
                return {}
        with ThreadPoolExecutor(4) as ex:
            for r in ex.map(run, batches):
                cache.update(r)
        jsave(ROOT / 'summaries.json', cache)

    papers, todo = [], []
    for p in sorted(results, key=lambda p: -p['ts']):
        q = publish(p, authors, thumbs, cache)
        if not q or not q.get('rt'):
            continue
        papers.append(q)
        if 'zh' not in q and q['rt'] and not p.get('_pub') and NOW_MS - p['ts'] < args.todo_days * DAY_MS:
            todo.append({'k': p['key'], 't': p['title'], 'a': (p.get('abs') or '')[:700]})
    for sid in meta:
        meta[sid]['n'] = sum(1 for q in papers if q['src'] == sid)
    for sid, v in meta.items():
        print(f'{sid:10s} {v["n"]:4d} {v["err"] or ""}', flush=True)
    jsave(ROOT / 'authors.json', authors)

    data = {'generated': NOW_MS, 'start': START, 'sources': meta, 'papers': papers}
    jsave(prev_path, data)
    # archive: replace this window's entries month by month
    win_ms = NOW_MS - WINDOW * DAY_MS
    by_m = {}
    for q in papers:
        by_m.setdefault(month_of(q['ts']), []).append({f: q[f] for f in SLIM if f in q})
    for m in by_m:
        pass
    write_archive(by_m, replace_after=win_ms)
    build_trends()
    build_explore()
    if args.todo:
        Path(args.todo).write_text(json.dumps(todo, ensure_ascii=False, indent=0), encoding='utf-8')
    print('papers', len(papers), 'with summary', sum(1 for q in papers if 'zh' in q), 'todo', len(todo),
          'with thumb', sum(1 for q in papers if 'img' in q), 'size', prev_path.stat().st_size)
    if incomplete and args.budget:
        print('enrichment incomplete (time budget); run again to continue', flush=True)
        sys.exit(3)


if __name__ == '__main__':
    main()
