#!/usr/bin/env python3
"""Fetch the latest nanophotonics papers and write data.json for the static site.

Sources: arXiv API + Crossref (journal list in config.json).
If ANTHROPIC_API_KEY is set, each new paper gets a one-sentence Chinese summary,
topic tags and a relevance score from Claude; results are cached in summaries.json
so each paper is only summarized once.

Journal abstracts are used only as model input and are NOT written to data.json
(publishers hold copyright on many of them); arXiv abstracts are published.

usage: python scripts/update.py            (run from the repository root)
"""
import os, sys, json, re, time, datetime as dt, urllib.request, urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
cfg = json.loads((ROOT / 'config.json').read_text(encoding='utf-8'))
UA = {'User-Agent': 'nano-radar/1.0 (+https://github.com; literature tracker)'}
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


def get(url, timeout=60, data=None, headers=None):
    err = None
    for i in range(3):
        try:
            req = urllib.request.Request(url, data=data, headers={**UA, **(headers or {})})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode('utf-8', 'replace')
        except Exception as e:
            err = e
            time.sleep(3 + 5 * i)
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
           + '&select=DOI,title,author,created,abstract,URL')
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


def main():
    results, meta = [], {}
    with ThreadPoolExecutor(6) as ex:
        for sid, lst, err in ex.map(job, cfg['sources']):
            lst = [p for p in lst if BROAD.search(p['title'] + ' ' + p['abs'])]
            meta[sid] = {'n': len(lst), 'err': err}
            results += lst
            print(f'{sid:10s} {len(lst):4d} {err or ""}', flush=True)

    prev_path = ROOT / 'data.json'
    prev = json.loads(prev_path.read_text(encoding='utf-8')) if prev_path.exists() else {}
    failed = {k for k, v in meta.items() if v['err']}
    if failed and prev.get('papers'):
        keep = [p for p in prev['papers'] if p['src'] in failed and NOW_MS - p['ts'] < WINDOW * 864e5]
        results += keep
        for k in failed:
            meta[k]['n'] = sum(1 for p in keep if p['src'] == k)
    if all(v['err'] for v in meta.values()):
        sys.exit('all sources failed; data.json left unchanged')

    # de-duplicate by key
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
        errors = 0

        def run(b):
            try:
                return summarize(b)
            except Exception as e:
                print('summary batch failed:', str(e)[:200], flush=True)
                return None
        with ThreadPoolExecutor(4) as ex:
            for r in ex.map(run, batches):
                if r is None:
                    errors += 1
                else:
                    cache.update(r)
        print(f'summary batches failed: {errors}', flush=True)
    else:
        print('ANTHROPIC_API_KEY not set: skipping Chinese summaries', flush=True)

    # prune cache to ~2x window
    cache = {k: v for k, v in cache.items() if NOW_MS - v.get('t', NOW_MS) < 2 * WINDOW * 864e5 or k in seen}
    cache_path.write_text(json.dumps(cache, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')

    papers = []
    for p in sorted(results, key=lambda p: -p['ts']):
        q = {k: p[k] for k in ('key', 'src', 'title', 'ts', 'url') if k in p}
        for k in ('sub', 'pdf'):
            if p.get(k):
                q[k] = p[k]
        a = p.get('authors', [])
        q['authors'] = a[:8] + ([f'+{len(a) - 8}'] if len(a) > 8 else [])
        if p['src'] == 'arxiv':
            q['abs'] = p['abs'][:1600]
        q['rt'] = rule_topics(p['title'] + ' ' + p['abs'])  # keyword tags, computed here so journal abstracts stay private
        s = cache.get(p['key'])
        if s:
            q.update({'zh': s['zh'], 'tp': s['tp'], 'rel': s['rel']})
        papers.append(q)

    data = {'generated': NOW_MS, 'model': MODEL if API_KEY else None, 'sources': meta, 'papers': papers}
    prev_path.write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    print('papers', len(papers), 'with summary', sum(1 for p in papers if 'zh' in p))


if __name__ == '__main__':
    main()
