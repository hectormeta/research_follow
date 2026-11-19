#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""永久投资组合·中国版 —— 每交易日重建静态页。
数据源：天天基金（东方财富）pingzhongdata 里的基金净值 + 官方日增长率。
日增长率已含分红/拆分调整，累乘即得总收益指数，避免用行情前复权价造成失真。
"""
import json, os, re, sys, datetime, urllib.request

CODES = ["510300", "510880", "513100", "513500", "511010", "518880", "511880"]
START = "2013-01-01"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
      "Referer": "https://fund.eastmoney.com/"}
HERE = os.path.dirname(os.path.abspath(__file__))


def fetch(code):
    url = "https://fund.eastmoney.com/pingzhongdata/%s.js" % code
    req = urllib.request.Request(url, headers=UA)
    txt = urllib.request.urlopen(req, timeout=45).read().decode("utf-8", "replace")
    m = re.search(r"var Data_netWorthTrend\s*=\s*(\[.*?\]);", txt, re.S)
    if not m:
        raise RuntimeError("no netWorthTrend for " + code)
    arr = json.loads(m.group(1))
    idx, out = 1.0, []
    for a in arr:
        r = a.get("equityReturn")
        if r is None:
            continue
        idx *= 1 + r / 100.0
        d = datetime.datetime.fromtimestamp(a["x"] / 1000, datetime.timezone.utc)
        out.append((d.strftime("%Y-%m-%d"), round(idx, 4)))
    if not out:
        raise RuntimeError("empty series for " + code)
    return out


def main():
    series = {}
    for c in CODES:
        series[c] = fetch(c)
        print("  %s  %d 点  %s → %s" % (c, len(series[c]), series[c][0][0], series[c][-1][0]))
    by = {c: dict(series[c]) for c in CODES}
    alld = sorted(d for d in set().union(*[set(by[c]) for c in CODES]) if d >= START)
    end = min(max(by[c]) for c in CODES)              # 取所有标的都有数据的最后一天
    dates = [d for d in alld if d <= end]
    px = {c: [(by[c].get(d)) for d in dates] for c in CODES}
    payload = json.dumps({"dates": dates, "px": px}, separators=(",", ":"), ensure_ascii=False)
    tpl = open(os.path.join(HERE, "template.html"), encoding="utf-8").read()
    html = tpl.replace("__DATA__", payload)
    out = os.path.join(HERE, "index.html")
    old = open(out, encoding="utf-8").read() if os.path.exists(out) else ""
    open(out, "w", encoding="utf-8").write(html)
    print("dates %d  %s → %s  html %d bytes  (changed: %s)"
          % (len(dates), dates[0], dates[-1], len(html), "yes" if html != old else "no"))


if __name__ == "__main__":
    main()
