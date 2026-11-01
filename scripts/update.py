#!/usr/bin/env python3
"""Fetch the latest nanophotonics papers and write data.json for the static site.

Sources: arXiv API + Crossref (journal list in config.json).
If ANTHROPIC_API_KEY is set, each new paper gets a one-sentence Chinese summary,
topic tags and a relevance score from Claude; results are cached in summaries.json
so each paper is only summarized once.

Journal abstracts are used only as model input and are NOT written to data.json
(publishers hold copyright on many of them); arXiv abstracts are published.

usage (run from the repository root):
  python scripts/update.py                       fetch everything, write data.json (+ API summaries if key set)
  python scripts/update.py --budget 150 --todo todo.json
        resumable fetch: raw results are cached in .cache/ for 6 h; if the time budget runs out
        the script exits with code 3 and can simply be run again. When finished it writes
        todo.json = papers that still need a Chinese summary (key, title, abstract).
  python scripts/update.py --apply new.json      merge summaries [{"k","zh","tp":[names],"rel"}] into
                                                 summaries.json and data.json (no network)
  python scripts/update.py --if-stale 20         skip fetching if data.json is younger than 20 h
"""
import os, sys, json, re, time, datetime as dt, urllib.request, urllib.parse, urllib.error
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
cfg = json.loads((ROOT / 'config.json').read_text(encoding='utf-8'))
CONTACT = cfg.get('contact_email', 'hectormeta@users.noreply.github.com')
UA = {'User-Agent': f'nano-radar/1.0 (https://github.com/hectormeta/research_follow; mailto:{CONTACT})'}
NOW = dt.datetime.now(dt.timezone.utc)
NOW_MS = int(NOW.timestamp() * 1000)
WINDOW = int(cfg.get('window_days', 31))
BROAD = re.compile(cfg['prefilter'], re.I)
API_KEY = os.environ.get('ANTHROPIC_API_KEY', '').strip()
MODEL = os.environ.get('CLAUDE_MODEL', '').strip() or 'claude-haiku-4-5'
MAX_SUMMARIES = int(os.environ.get('MAX_SUMMARIES', '1200'))
TOPIC_IDS = {t['name']: t['id'] for t in cfg['topics']}
CTX = re.compile(cfg['context'], re.I)
TOPIC_RE = [(t, re.compile(t['re'], re.I)) for t in cfg['topics']]


def rule_topics(text):
    ctx = bool(CTX.search(text))
    prim = [t['id'] for t, rx in TOPIC_RE if not t.get('secondary') and rx.search(text) and (not t.get('ctx') or ctx)]
    if prim:
        prim += [t['id'] for t, rx in TOPIC_RE if t.get('secondary') and rx.search(text)]
    return prim


def get(url, timeout=60, data=None, headers=None, tries=5):
    err = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers={**UA, **(headers or {})})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode('utf-8', 'replace')
        except urllib.error.HTTPError as e:
            err = e
            if e.code not in (429, 500, 502, 503, 504):
                raise
            ra = e.headers.get('Retry-After') if e.headers else None
            time.sleep(min(120, float(ra)) if ra and ra.replace('.', '').isdigit() else 8 * (i + 1))
        except Exception as e:
            err = e
            time.sleep(5 * (i + 1))
    raise err


def clean(s):
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', s or '')).strip()


def iso_ms(s):
    return int(dt.datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp() * 1000)


def fetch_arxiv():
    ns = {'a': 'http://www.w3.org/2005/Atom', 'x': 'http://arxiv.org/schemas/atom'}
    base = ('https://export.arxiv.org/api/query?search_query=' + urllib.parse.quote(cfg['arxiv_query'])
            + '&sortBy=submittedDate&sortOrder=descending&max_results=100&start=')
    out = []
    for start in range(0, 600, 100):
        root = ET.fromstring(get(base + str(start), 90))
        es = root.findall('a:entry', ns)
        for e in es:
            aid = re.sub(r'v\d+$', '', e.findtext('a:id', '', ns).split('/abs/')[-1])
            if not aid:
                continue
            pc = e.find('x:primary_category', ns)
            out.append({'key': 'arxiv:' + aid, 'src': 'arxiv',
                        'sub': pc.get('term') if pc is not None else '',
                        'title': clean(e.findtext('a:title', '', ns)),
                        'abs': clean(e.findtext('a:summary', '', ns)),
                        'authors': [clean(a.findtext('a:name', '', ns)) for a in e.findall('a:author', ns)],
                        'ts': iso_ms(e.findtext('a:published', '', ns)),
                        'url': f'https://arxiv.org/abs/{aid}', 'pdf': f'https://arxiv.org/pdf/{aid}'})
        if len(es) < 100 or (out and NOW_MS - out[-1]['ts'] > WINDOW * 864e5):
            break
        time.sleep(3)  # arXiv API etiquette
    return out


def fetch_crossref(s):
    frm = (NOW - dt.timedelta(days=WINDOW)).date().isoformat()
    url = ('https://api.crossref.org/works?'
           + f'filter=issn:{s["issn"]},from-created-date:{frm},type:journal-article&sort=created&order=desc&rows=1000'
           + '&select=DOI,title,author,created,abstract,URL&mailto=' + urllib.parse.quote(CONTACT))
    items = json.loads(get(url, 150))['message']['items']
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


def job(s):
    try:
        return s['id'], (fetch_arxiv() if s['id'] == 'arxiv' else fetch_crossref(s)), None
    except Exception as e:
        return s['id'], [], f'{type(e).__name__}: {e}'[:160]


PROMPT = (
    '你是微纳光学领域的研究助理。输入是一个 JSON 数组，每项含 k（键）、t（标题）、a（摘要，可能为空）。'
    '对每一项输出：zh = 一句中文要点，不超过 60 字，说清楚做了什么和关键结果或数值，专业缩写保留英文（如 BIC、OAM、Q 值）；'
    '摘要为空时依据标题概括，不要编造数值。'
    'tp = 主题标签数组，只能取自 ["超表面","超材料","拓扑光学","光场调控","微纳光学","智能设计"]，'
    '其中“拓扑光学”含非厄米与奇异点，“光场调控”含涡旋、OAM、偏振与波前调控、结构光，'
    '“微纳光学”含等离激元、Mie 共振、光子晶体、BIC、极化激元，“智能设计”指逆向设计或 AI 方法。'
    'rel = 与微纳光学、超表面、超材料、拓扑光学、光场调控这一研究方向的相关度，'
    '0 表示无关（如电子或磁性拓扑、无线通信算法、纯生物化学），1 表示部分相关，2 表示核心相关。'
    '只输出 JSON 数组，形如 [{"k":"...","zh":"...","tp":["..."],"rel":2}]，不要输出任何其他文字。'
)


def summarize(batch):
    body = json.dumps({
        'model': MODEL, 'max_tokens': 4096, 'system': PROMPT,
        'messages': [{'role': 'user', 'content': json.dumps(
            [{'k': p['key'], 't': p['title'], 'a': p['abs'][:900]} for p in batch], ensure_ascii=False)}],
    }).encode()
    txt = get('https://api.anthropic.com/v1/messages', 120, body,
              {'x-api-key': API_KEY, 'anthropic-version': '2023-06-01', 'content-type': 'application/json'})
    out = ''.join(c.get('text', '') for c in json.loads(txt).get('content', []))
    arr = json.loads(out[out.index('['): out.rindex(']') + 1])
    res = {}
    for o in arr:
        if isinstance(o, dict) and o.get('k'):
            try:
                rel = int(o.get('rel', 1))
            except Exception:
                rel = 1
            res[o['k']] = {'zh': str(o.get('zh', '')).strip(),
                           'tp': [TOPIC_IDS[n] for n in o.get('tp', []) if n in TOPIC_IDS],
                           'rel': rel, 't': NOW_MS}
    return res


RAW = ROOT / '.cache'
RAW_TTL = 6 * 3600


def norm_summary(o):
    try:
        rel = int(o.get('rel', 1))
    except Exception:
        rel = 1
    tp = [TOPIC_IDS.get(n, n) for n in o.get('tp', [])]
    return {'zh': str(o.get('zh', '')).strip(), 'tp': [t for t in tp if t in TOPIC_IDS.values()], 'rel': rel, 't': NOW_MS}


def apply_summaries(path):
    new = json.loads(Path(path).read_text(encoding='utf-8'))
    if isinstance(new, dict):
        new = [dict(v, k=k) for k, v in new.items()]
    cache_path = ROOT / 'summaries.json'
    cache = json.loads(cache_path.read_text(encoding='utf-8')) if cache_path.exists() else {}
    got = {o['k']: norm_summary(o) for o in new if isinstance(o, dict) and o.get('k') and o.get('zh')}
    cache.update(got)
    cache_path.write_text(json.dumps(cache, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    data = json.loads((ROOT / 'data.json').read_text(encoding='utf-8'))
    n = 0
    for q in data['papers']:
        s = got.get(q['key'])
        if s:
            q.update({'zh': s['zh'], 'tp': s['tp'], 'rel': s['rel']}); n += 1
    data['summarized_by'] = data.get('summarized_by') or 'Claude (Cowork)'
    (ROOT / 'data.json').write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    print(f'applied {len(got)} summaries, {n} matched papers in data.json')


def fetch_all(budget, threads):
    """Fetch every source, using .cache/<id>.json when fresh. Returns outs or None if budget ran out."""
    RAW.mkdir(exist_ok=True)
    t0 = time.time()
    outs, pending = {}, []
    for s in cfg['sources']:
        f = RAW / f"{s['id']}.json"
        if f.exists() and time.time() - f.stat().st_mtime < RAW_TTL:
            d = json.loads(f.read_text(encoding='utf-8'))
            outs[s['id']] = (s['id'], d['papers'], d['err'])
        else:
            pending.append(s)

    def run(s):
        if budget and time.time() - t0 > budget:
            return None
        r = job(s)
        if not r[2] or not (RAW / f"{s['id']}.json").exists():  # don't overwrite a good cache with a failure
            (RAW / f"{s['id']}.json").write_text(json.dumps({'papers': r[1], 'err': r[2]}, ensure_ascii=False), encoding='utf-8')
        return r
    ax = [s for s in pending if s['id'] == 'arxiv']
    cr = [s for s in pending if s['id'] != 'arxiv']
    with ThreadPoolExecutor(1) as ax_pool, ThreadPoolExecutor(threads) as cr_pool:
        futs = [ax_pool.submit(run, s) for s in ax] + [cr_pool.submit(run, s) for s in cr]
        for f in futs:
            r = f.result()
            if r:
                outs[r[0]] = r
    missing = [s['id'] for s in cfg['sources'] if s['id'] not in outs]
    if missing:
        print('time budget used up, still to fetch:', ' '.join(missing), flush=True)
        return None
    return [outs[s['id']] for s in cfg['sources']]


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--budget', type=float, default=0, help='seconds; 0 = unlimited')
    ap.add_argument('--threads', type=int, default=int(os.environ.get('CR_THREADS', '2')))
    ap.add_argument('--todo', help='write papers still lacking a summary to this JSON file')
    ap.add_argument('--apply', help='merge summaries from this JSON file and exit')
    ap.add_argument('--if-stale', type=float, default=0, help='skip if data.json is younger than N hours')
    args = ap.parse_args()

    if args.apply:
        return apply_summaries(args.apply)

    prev_path = ROOT / 'data.json'
    prev = json.loads(prev_path.read_text(encoding='utf-8')) if prev_path.exists() else {}
    if args.if_stale and prev.get('generated') and NOW_MS - prev['generated'] < args.if_stale * 3600e3:
        print(f"data.json is {(NOW_MS - prev['generated']) / 3600e3:.1f} h old; skipping fetch")
        return

    outs = fetch_all(args.budget, args.threads)
    if outs is None:
        sys.exit(3)

    results, meta = [], {}
    for sid, lst, err in outs:
        lst = [p for p in lst if BROAD.search(p['title'] + ' ' + p.get('abs', ''))]
        meta[sid] = {'n': len(lst), 'err': err}
        results += lst
        print(f'{sid:10s} {len(lst):4d} {err or ""}', flush=True)

    failed = {k for k, v in meta.items() if v['err']}
    if failed and prev.get('papers'):
        keep = [p for p in prev['papers'] if p['src'] in failed and NOW_MS - p['ts'] < WINDOW * 864e5]
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

    cache_path = ROOT / 'summaries.json'
    cache = json.loads(cache_path.read_text(encoding='utf-8')) if cache_path.exists() else {}
    if API_KEY:
        todo = sorted([p for p in results if p['key'] not in cache and 'abs' in p], key=lambda p: -p['ts'])[:MAX_SUMMARIES]
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

    cache = {k: v for k, v in cache.items() if NOW_MS - v.get('t', NOW_MS) < 2 * WINDOW * 864e5 or k in seen}
    cache_path.write_text(json.dumps(cache, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')

    papers, todo = [], []
    for p in sorted(results, key=lambda p: -p['ts']):
        q = {k: p[k] for k in ('key', 'src', 'title', 'ts', 'url') if k in p}
        for k in ('sub', 'pdf'):
            if p.get(k):
                q[k] = p[k]
        a = p.get('authors', [])
        q['authors'] = a[:8] + ([f'+{len(a) - 8}'] if len(a) > 8 else [])
        if p['src'] == 'arxiv' and p.get('abs'):
            q['abs'] = p['abs'][:1600]
        # keyword tags computed here so journal abstracts never need to be published
        q['rt'] = p['rt'] if 'abs' not in p and 'rt' in p else rule_topics(p['title'] + ' ' + p.get('abs', ''))
        s = cache.get(p['key'])
        if s:
            q.update({'zh': s['zh'], 'tp': s['tp'], 'rel': s['rel']})
        elif 'abs' in p and q['rt']:
            todo.append({'k': p['key'], 't': p['title'], 'a': p['abs'][:700]})
        papers.append(q)

    data = {'generated': NOW_MS, 'sources': meta, 'papers': papers}
    prev_path.write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    if args.todo:
        Path(args.todo).write_text(json.dumps(todo, ensure_ascii=False, indent=0), encoding='utf-8')
    print('papers', len(papers), 'with summary', sum(1 for q in papers if 'zh' in q), 'todo', len(todo))


if __name__ == '__main__':
    main()
