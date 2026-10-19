#!/usr/bin/env python3
"""
每日更新 index.html 內的股價與新聞資料（由 GitHub Actions 執行）。

- 台股：臺灣證券交易所、證券櫃檯買賣中心 OpenAPI（官方收盤資料），失敗時改用 Yahoo Finance
- 海外股：Yahoo Finance
- 新聞：Google News RSS，只取 Yahoo股市（tw.stock.yahoo.com）的文章，每家公司 3 則

任何來源抓取失敗時，保留 index.html 中原本的資料，不會讓網站壞掉。
只使用 Python 標準函式庫，不需要安裝套件。
"""
import json
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML_PATH = ROOT / "index.html"
TW_TZ = timezone(timedelta(hours=8))
NEWS_SITE = "tw.stock.yahoo.com"
NEWS_COUNT = 3
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}
LIVE_RE = re.compile(r"/\*LIVE-START\*/.*?/\*LIVE-END\*/", re.S)
COMPANY_RE = re.compile(
    r'\{\s*id:\s*"(?P<id>[^"]+)".*?ticker:\s*"(?P<ticker>[^"]+)".*?yf:\s*"(?P<yf>[^"]+)"'
    r'.*?mkt:\s*"(?P<mkt>[^"]+)".*?q:\s*"(?P<q>[^"]+)"'
)


def fetch(url, timeout=20, tries=3):
    last_err = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as err:  # noqa: BLE001
            last_err = err
            time.sleep(1.5 * (i + 1))
    raise last_err


def to_num(value):
    """把 '1,085.00'、'+5.0000'、'-0.50' 之類的字串轉成數字；無法轉換時回傳 None。"""
    if value is None:
        return None
    m = re.search(r"[-+]?\d+(?:\.\d+)?", str(value).replace(",", ""))
    return float(m.group()) if m else None


def roc_to_iso(value):
    """民國日期 1151016 → 2026-10-16。"""
    s = re.sub(r"\D", "", str(value or ""))
    if len(s) == 7:
        return f"{int(s[:3]) + 1911}-{s[3:5]}-{s[5:]}"
    if len(s) == 8:
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return ""


def make_quote(close, change, date, cur, src):
    prev = close - change
    pct = change / prev * 100 if prev else 0.0
    return {"close": round(close, 2), "change": round(change, 2), "pct": round(pct, 2),
            "date": date, "cur": cur, "src": src}


def tw_official():
    """回傳 {股票代號: quote}，資料來自證交所與櫃買中心的每日收盤行情。"""
    out = {}
    sources = [
        ("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",
         "Code", "ClosingPrice", "Change", "Date", "臺灣證券交易所"),
        ("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes",
         "SecuritiesCompanyCode", "Close", "Change", "Date", "證券櫃檯買賣中心"),
    ]
    for url, k_code, k_close, k_chg, k_date, name in sources:
        try:
            rows = json.loads(fetch(url, timeout=40))
            for r in rows:
                close, chg = to_num(r.get(k_close)), to_num(r.get(k_chg))
                if close is None or chg is None or close <= 0:
                    continue
                out[str(r.get(k_code)).strip()] = make_quote(close, chg, roc_to_iso(r.get(k_date)), "TWD", name)
            print(f"[price] {name}: {len(rows)} rows")
        except Exception as err:  # noqa: BLE001
            print(f"[price] {name} failed: {err}")
    return out


def yahoo_quote(symbol):
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{urllib.parse.quote(symbol)}?range=10d&interval=1d")
    res = json.loads(fetch(url))["chart"]["result"][0]
    meta = res["meta"]
    offset = meta.get("gmtoffset", 0)

    def day(ts):
        return datetime.fromtimestamp(ts + offset, tz=timezone.utc).strftime("%Y-%m-%d")

    bars = [(day(t), c) for t, c in zip(res.get("timestamp") or [],
                                       res["indicators"]["quote"][0].get("close") or []) if c is not None]
    price = meta.get("regularMarketPrice") or (bars[-1][1] if bars else None)
    mdate = day(meta["regularMarketTime"]) if meta.get("regularMarketTime") else (bars[-1][0] if bars else "")
    prev_list = [c for d, c in bars if d < mdate]
    prev = prev_list[-1] if prev_list else meta.get("chartPreviousClose")
    if price is None or not prev:
        raise ValueError("no price data")
    return make_quote(price, price - prev, mdate, meta.get("currency") or "", "Yahoo Finance")


def yahoo_news(query):
    """用 Google News RSS 搜尋 Yahoo股市 的相關新聞，回傳最新幾則。"""
    items = []
    for extra in (" when:7d", ""):
        q = f"{query} site:{NEWS_SITE}{extra}"
        url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
            {"q": q, "hl": "zh-TW", "gl": "TW", "ceid": "TW:zh-Hant"})
        root = ET.fromstring(fetch(url))
        items = []
        seen = set()
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip()
            if " - " in title and "Yahoo" in title.rsplit(" - ", 1)[1]:
                title = title.rsplit(" - ", 1)[0].strip()
            link = (it.findtext("link") or "").strip()
            if not title or not link or title in seen:
                continue
            seen.add(title)
            try:
                dt = parsedate_to_datetime(it.findtext("pubDate")).astimezone(TW_TZ)
            except Exception:  # noqa: BLE001
                dt = None
            items.append({"title": title, "link": link,
                          "time": dt.strftime("%Y-%m-%d %H:%M") if dt else "",
                          "_ts": dt.timestamp() if dt else 0})
        if len(items) >= 2:
            break
        time.sleep(0.5)
    items.sort(key=lambda x: -x["_ts"])
    for x in items:
        x.pop("_ts", None)
    return items[:NEWS_COUNT]


def main():
    html = HTML_PATH.read_text(encoding="utf-8")
    m = LIVE_RE.search(html)
    if not m:
        raise SystemExit("找不到 /*LIVE-START*/ ... /*LIVE-END*/ 標記")
    old = {"updated": "", "prices": {}, "news": {}}
    om = re.search(r"const LIVE = (\{.*\});", m.group(0), re.S)
    if om:
        try:
            old = json.loads(om.group(1))
        except json.JSONDecodeError:
            pass

    companies = [mm.groupdict() for mm in COMPANY_RE.finditer(html)]
    print(f"companies: {len(companies)}")

    prices = dict(old.get("prices", {}))
    news = dict(old.get("news", {}))
    official = tw_official()
    ok_price = ok_news = 0

    for c in companies:
        cid = c["id"]
        quote = official.get(c["ticker"]) if c["mkt"] == "TW" else None
        if quote is None:
            try:
                quote = yahoo_quote(c["yf"])
            except Exception as err:  # noqa: BLE001
                print(f"[price] {cid} ({c['yf']}) failed: {err}")
            time.sleep(0.3)
        if quote:
            prices[cid] = quote
            ok_price += 1
        try:
            items = yahoo_news(c["q"])
            if items:
                news[cid] = items
                ok_news += 1
        except Exception as err:  # noqa: BLE001
            print(f"[news] {cid} failed: {err}")
        time.sleep(0.8)

    live = {"updated": datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M"), "prices": prices, "news": news}
    payload = json.dumps(live, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    new_html = html[:m.start()] + f"/*LIVE-START*/const LIVE = {payload};/*LIVE-END*/" + html[m.end():]
    HTML_PATH.write_text(new_html, encoding="utf-8")
    print(f"done: prices {ok_price}/{len(companies)}, news {ok_news}/{len(companies)}")


if __name__ == "__main__":
    main()
