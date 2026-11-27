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
import os, re, sys, json, time, gzip, hashlib, html, subprocess, tempfile, shutil, datetime as dt, urllib.request, urllib.parse, urllib.error
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

# --- 主题相关度：复用 config.topics 的正则，命中即视为与微纳光学方向相关 ---
TOPIC_RES = []
for _t in cfg.get('topics', []):
    if _t.get('id') == 'gen':
        continue
    try:
        TOPIC_RES.append((_t['id'], _t['name'], re.compile(_t['re'], re.I)))
    except re.error:
        pass


def relevance(text):
    """返回命中的主题名列表（与本人微纳光学/光子学方向相关）。"""
    hit = []
    for _tid, name, rx in TOPIC_RES:
        if rx.search(text or ''):
            hit.append(name)
    if not hit and _OPTICAL_FALLBACK.search(text or ''):
        hit.append('光学')
    return hit


_OPTICAL_FALLBACK = re.compile(
    r'超构|超表面|超材料|拓扑光子|非厄米|偏振|光场|结构光|光子晶体|光子器件|'
    r'计算成像|光学传感|光电|微纳光学|纳米光子|超透镜|涡旋光|全息|微波|'
    r'photonics?|optical|optic|laser|terahertz|\bthz\b|polariton|metasurf|metamat', re.I)


# --- 截止日期解析：只在含“截止/申报/ deadline”等语义词的句子里取日期 ---
DDL_WORD = re.compile(
    r'截止|报名|申报|申请|投递|提交|截止日期|deadline|due (date|on)|'
    r'applications? (close|open)|closed?|报名时间|申报时间', re.I)
_CN_FULL = re.compile(r'(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?')
_CN_MD = re.compile(r'(\d{1,2})\s*月\s*(\d{1,2})\s*日')
_ISO = re.compile(r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})')
_MON = {'jan':1,'feb':2,'mar':3,'apr':4,'may':5,'jun':6,'jul':7,'aug':8,
        'sep':9,'oct':10,'nov':11,'dec':12,
        'january':1,'february':2,'march':3,'april':4,'june':6,'july':7,
        'august':8,'september':9,'october':10,'november':11,'december':12}
_EN_DATE = re.compile(
    r'(\d{1,2})\s+(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|'
    r'Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|'
    r'Nov(?:ember)?|Dec(?:ember)?)\s+(\d{4})'
    r'|(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|'
    r'Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|'
    r'Dec(?:ember)?)\s+(\d{1,2}),?\s+(\d{4})', re.I)
CST = dt.timezone(dt.timedelta(hours=8))


def _ymd(y, m, d):
    y, m, d = int(y), int(m), int(d)
    if not (1 <= m <= 12 and 1 <= d <= 31):
        return None
    try:
        return int(dt.datetime(int(y), int(m), int(d), 23, 59, 59,
                               tzinfo=CST).timestamp() * 1000)
    except ValueError:
        return None


def parse_deadline(text, now=None):
    """从文本中提取截止日期（ms, +08:00 当天结束）；无明确 ddl 语境返回 None。"""
    if not text:
        return None
    if now is None:
        now = NOW_MS
    elif hasattr(now, 'timestamp'):
        now = now.timestamp() * 1000
    now_ms = now
    for sent in re.split(r'[。；;\n！!？?]', text):
        if not DDL_WORD.search(sent):
            continue
        m = _CN_FULL.search(sent) or _ISO.search(sent)
        if m:
            y, mo, d = m.group(1), m.group(2), m.group(3)
            v = _ymd(y, mo, d)
            if v:
                return v
        m = _CN_MD.search(sent)
        if m:
            mo, d = int(m.group(1)), int(m.group(2))
            y = dt.datetime.fromtimestamp(now_ms / 1000, CST).year
            v = _ymd(y, mo, d)
            if v and v < now_ms - 30 * DAY_MS:  # 月日已过则归到下一年
                v = _ymd(y + 1, mo, d)
            if v:
                return v
        m = _EN_DATE.search(sent)
        if m:
            if m.group(1):  # D Mon YYYY
                d, mon, y = m.group(1), m.group(2).lower(), m.group(3)
                v = _ymd(y, _MON.get(mon[:3], 0), d)
            else:           # Mon D, YYYY
                mon, d, y = m.group(4).lower(), m.group(5), m.group(6)
                v = _ymd(y, _MON.get(mon[:3], 0), d)
            if v:
                return v
    return None


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
    # url 与标题共同入键：列表卡片常回退同一 feed url，仅按 url 哈希会互相覆盖
    k = hashlib.md5((url + '|' + norm_title(title)).encode()).hexdigest()[:16]
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


# ------------------------------------------------------------- portal (校内门户, 需登录)
# 勘察自门户登录页 JS 与无头浏览器实测：
#   POST /authModule/authenticate  form: username, password=RSA(pubkey, pw), reamId=1002
#   code 100000001 成功（返回本人 JWT）；100000004/100000005 累积 1 次出图形验证码；短信 OTP 为 reamId 1006
#   实测未触发图形验证码/短信 OTP。认证后前端调 deviceInfo.action（返回 100000002/success=false 仅为设备
#   信任提醒，不阻塞后端授权）；直跳 redirectUri 仍可经 SSO authorize 换 code 进入门户首页。
#   首页卡片：标题 + MM-DD 发布日期；申请/项目类截止日期在详情正文；详情站 portal-info.sigs.tsinghua.edu.cn
#   同样走 SSO（APP054）。列表/详情字段、ddl 位置待登录样本校准。
# 待办：Python 侧 RSA PKCS#1 v1.5 加密与 WebVPN cookie 代理尚未实现；cron 环境库/网络可达性待验证。
# 未配置凭据或适配器未就绪时整源跳过，保留上一版条目，异常记 err 不拖累其他源。
PORTAL_BASE = os.environ.get('PORTAL_BASE', 'https://webvpn.sigs.tsinghua.edu.cn')


def _load_portal_credentials():
    """环境变量优先；缺失时回退到持久凭据文件（仓库外，权限 600）。不回显到日志。"""
    user = os.environ.get('PORTAL_USER', '').strip()
    pw = os.environ.get('PORTAL_PASS', '').strip()
    if user and pw:
        return user, pw
    f = Path.home() / '.config' / 'research_follow' / 'portal.json'
    try:
        d = json.loads(f.read_text(encoding='utf-8'))
        return str(d.get('username', '')).strip(), str(d.get('password', '')).strip()
    except Exception:
        return '', ''


PORTAL_USER, PORTAL_PASS = _load_portal_credentials()
PORTAL_HOME = os.environ.get(
    'PORTAL_HOME',
    PORTAL_BASE + '/https/77726476706e69737468656265737421e0f85388263c2643770f9ae28c462a3b9c03027879bfa2a49d2feb/sigsPortal/home?org=&page=page_group_teacher_home&tab=news&top_external_id=top_portal_manage&type=fs_info&lang=zh')


_PORTAL_CARD = re.compile(
    r'<a class="LpEu0_0e"><span>([^<]*)</span></a>.*?(\d{1,2})-(\d{1,2})', re.S)


def _portal_md_ts(mo, d):
    y = dt.datetime.now(CST).year
    v = _ymd(y, mo, d)
    if v and v < NOW_MS - 30 * DAY_MS:
        v = _ymd(y + 1, mo, d)
    return v


def parse_portal_list(feed, body):
    """从门户首页 HTML 提取卡片标题与发布日期；详情 ddl 待详情接口校准。"""
    out = []
    for title, mo, d in _PORTAL_CARD.findall(body):
        title = clean(title)
        if not title:
            continue
        ts = _portal_md_ts(int(mo), int(d))
        it = make_item(feed, title, '', feed['name'], feed['url'], ts)
        if it:
            it['ddl'] = parse_deadline(title)
            out.append(it)
    return out


def _portal_node_bin():
    return os.environ.get('PORTAL_NODE') or shutil.which('node') or shutil.which('nodejs')


def _portal_env_status():
    """门户子进程前置能力自检：node、脚本、凭据是否就绪（playwright/浏览器由子进程退出码反映）。"""
    return {
        'node': bool(_portal_node_bin()),
        'script': (ROOT / 'scripts' / 'portal_fetch.js').exists(),
        'cred': bool(PORTAL_USER and PORTAL_PASS),
    }


def fetch_portal(feed):
    """经 node+playwright 子进程登录抓首页 HTML，回交解析。子进程不可用/失败抛错由主流程记 err。"""
    if not PORTAL_USER or not PORTAL_PASS:
        print(f"  {feed['id']:14s} skipped: 未配置 PORTAL_USER/PORTAL_PASS")
        return []
    node = _portal_node_bin()
    script = ROOT / 'scripts' / 'portal_fetch.js'
    if not node or not script.exists():
        raise RuntimeError('门户子进程不可用: node 或 portal_fetch.js 缺失')

    fd, tmp = tempfile.mkstemp(suffix='.html', prefix='portal_')
    os.close(fd)
    env = dict(os.environ)
    env.update({
        'PORTAL_USER': PORTAL_USER,
        'PORTAL_PASS': PORTAL_PASS,
        'PORTAL_HOME': feed.get('url') or PORTAL_HOME,
    })
    _np = [os.environ.get('NODE_PATH', ''), '/opt/vm/preinstall/npm-global/lib/node_modules']
    env['NODE_PATH'] = ':'.join(p for p in _np if p and Path(p).exists())
    try:
        try:
            proc = subprocess.run([node, str(script), tmp], env=env,
                                   capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired:
            raise RuntimeError('门户子进程超时(180s)')
        if proc.returncode == 2:
            print(f"  {feed['id']:14s} skipped: 子进程报告缺凭据")
            return []
        if proc.returncode == 3:
            raise RuntimeError('门户认证失败: 账密错误 / 图形验证码 / 短信 OTP')
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or '')[:140]
            raise RuntimeError(f'门户子进程退出 {proc.returncode}: {err}')
        body = Path(tmp).read_text(encoding='utf-8', errors='replace') if Path(tmp).exists() else ''
        return parse_portal_list(feed, body)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


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
    if feed['type'] == 'portal':
        return fetch_portal(feed)
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
        if feed.get('enabled') is False:
            print(f"  {feed['id']:14s} disabled (待命)")
            continue
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
                hit = relevance(text)
                it['rtopics'] = hit
                it['rel'] = len(hit)
                it['ddl'] = parse_deadline(text)
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
    items.sort(key=lambda p: (-(p.get('rel') or 0), -p['ts']))
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
        'portal_env': _portal_env_status(),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    counts = {}
    for p in capped:
        counts[p['cat']] = counts.get(p['cat'], 0) + 1
    print(f"total {len(capped)} items; by cat: {counts}; errs {len(errs)}")


if __name__ == '__main__':
    main()
