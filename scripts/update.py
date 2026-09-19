"""items.json を更新する。GitHub Actions から環境変数で呼ばれる。

ACTION: add | delete | edit | refresh
URL:    add のときの商品URL
ID:     delete / edit の対象ID
PATCH:  edit のときの JSON（title, price, brand, sizes, soldout, sourceImage など）
"""
import datetime
import html
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parent.parent
ITEMS = ROOT / "items.json"
IMG = ROOT / "img"
JST = datetime.timezone(datetime.timedelta(hours=9))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
SHOPS = {"zozo.jp": "ZOZOTOWN", "devirockstore.net": "devirock", "nonnon.jp": "moononnon"}
ID_PREFIX = {"ZOZOTOWN": "zozo"}

# ブラウザで開いたページから情報を抜き出す（ZOZOなど、静的取得できないサイト用）
BROWSER_JS = r"""
() => {
  const lines = document.body.innerText.split('\n').map(s => s.replace(/\s+/g, ' ').trim()).filter(Boolean);
  const meta = p => document.querySelector(`meta[property="${p}"]`)?.content || null;
  const lds = [...document.querySelectorAll('script[type="application/ld+json"]')].flatMap(s => {
    try { const j = JSON.parse(s.textContent); return Array.isArray(j) ? j : [j]; } catch (e) { return []; }
  }).filter(j => j['@type'] === 'Product');
  const p = lds[0] || {};
  const re = /^(.+?)\s*\/\s*(在庫あり|在庫なし|残り.*|予約.*|在庫わずか.*)$/;
  const sizes = {}, colors = new Set();
  let color = null;
  lines.forEach((l, i) => {
    const m = l.match(re);
    if (m) {
      const ok = m[2] !== '在庫なし';
      sizes[m[1]] = sizes[m[1]] || ok;
      if (ok && color) colors.add(color);
    } else if (lines[i + 1] && re.test(lines[i + 1]) && !/サイズ相当|カート|完売/.test(l)) {
      color = l;
    }
  });
  const priceIdx = lines.findIndex(l => /^¥[\d,]+$/.test(l));
  const listLine = lines.find(l => /^¥[\d,]+（.*時点の価格）$/.test(l));
  const toNum = s => s ? Number(s.replace(/[^\d]/g, '')) : null;
  return {
    title: p.name || meta('og:title'),
    image: (Array.isArray(p.image) ? p.image[0] : p.image) || meta('og:image'),
    brand: p.brand?.name || null,
    price: priceIdx >= 0 ? toNum(lines[priceIdx]) : Number(p.offers?.price) || null,
    listPrice: listLine ? toNum(listLine.split('（')[0]) : null,
    sizes: Object.keys(sizes).filter(k => sizes[k]),
    soldout: Object.keys(sizes).filter(k => !sizes[k]),
    colors: [...colors],
  };
}
"""


def now():
    return datetime.datetime.now(JST).isoformat(timespec="seconds")


def load():
    return json.loads(ITEMS.read_text(encoding="utf-8")) if ITEMS.exists() else []


def save(items):
    ITEMS.write_text(json.dumps(items, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def key_of(url):
    p = urlparse(url)
    return (p.hostname.replace("www.", "") + p.path).rstrip("/")


def shop_of(url):
    h = urlparse(url).hostname.replace("www.", "")
    return SHOPS.get(h, h)


def id_of(url, items):
    shop = shop_of(url)
    last = [s for s in urlparse(url).path.split("/") if s][-1] if urlparse(url).path.strip("/") else "item"
    base = re.sub(r"[^a-z0-9-]", "-", (ID_PREFIX.get(shop, shop) + "-" + last).lower())[:60].strip("-")
    ids = {i["id"] for i in items}
    cand, n = base, 2
    while cand in ids:
        cand, n = f"{base}-{n}", n + 1
    return cand


def size_label(s):
    m = re.match(r"\s*(\d+)", str(s))
    return m.group(1) if m else str(s).strip()


def clean_title(t):
    return re.split(r"\s*[|｜]\s*|（[^（）]*）｜", t or "")[0].strip()


def fetch_static(url):
    """og:タグと JSON-LD（itembox 系ショップの ProductGroup）から取得。"""
    r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "ja"}, timeout=30)
    if r.status_code != 200:
        return None
    s = r.content.decode("utf-8", "replace")

    def meta(p):
        m = re.search(r'<meta[^>]+property="' + re.escape(p) + r'"[^>]+content="([^"]*)"', s)
        return html.unescape(m.group(1)) if m else None

    d = {"title": meta("og:title"), "image": meta("og:image"), "brand": None, "listPrice": None}
    price = meta("product:price:amount")
    d["price"] = int(float(price)) if price else None
    sizes, colors = {}, set()
    for m in re.finditer(r"<script[^>]*ld\+json[^>]*>(.*?)</script>", s, re.S):
        try:
            j = json.loads(m.group(1))
        except ValueError:
            continue
        if isinstance(j, dict) and j.get("@type") == "ProductGroup":
            d["brand"] = (j.get("brand") or {}).get("name")
            for v in j.get("hasVariant", []):
                sz = v.get("size")
                ok = "InStock" in ((v.get("offers") or {}).get("availability") or "")
                if sz:
                    sizes[sz] = sizes.get(sz, False) or ok
                if ok and v.get("color"):
                    colors.add(v["color"])
    d["sizes"] = [k for k, v in sizes.items() if v]
    d["soldout"] = [k for k, v in sizes.items() if not v]
    d["colors"] = sorted(colors)
    if not d["title"] or not d["price"] or not sizes:
        return None
    return d


def fetch_browser(url):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(user_agent=UA, locale="ja-JP", viewport={"width": 1280, "height": 900})
        try:
            pg.goto(url, wait_until="domcontentloaded", timeout=60000)
            pg.wait_for_timeout(4000)
            d = pg.evaluate(BROWSER_JS)
        finally:
            b.close()
    if not d.get("title") or not d.get("price"):
        return None
    return d


def scrape(url):
    for f in (fetch_static, fetch_browser):
        try:
            d = f(url)
        except Exception as e:  # noqa: BLE001
            print(f"{f.__name__} failed: {e}", file=sys.stderr)
            d = None
        if d:
            d["title"] = clean_title(d["title"])
            d["sizes"] = [size_label(x) for x in d["sizes"]]
            d["soldout"] = [size_label(x) for x in d["soldout"]]
            return d
    return None


def download_image(src, item_id, referer):
    if not src:
        return None
    try:
        r = requests.get(src, headers={"User-Agent": UA, "Referer": referer}, timeout=30)
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001
        print(f"image failed: {e}", file=sys.stderr)
        return None
    ext = {"image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}.get(r.headers.get("content-type", "").split(";")[0], ".jpg")
    IMG.mkdir(exist_ok=True)
    for old in IMG.glob(item_id + ".*"):
        old.unlink()
    (IMG / (item_id + ext)).write_bytes(r.content)
    return f"img/{item_id}{ext}"


def apply(item, d):
    for k in ("title", "brand", "price", "listPrice", "sizes", "soldout", "colors"):
        if d.get(k) is not None:
            item[k] = d[k]
    if d.get("image") and d["image"] != item.get("sourceImage"):
        path = download_image(d["image"], item["id"], item["url"])
        if path:
            item["image"], item["sourceImage"] = path, d["image"]
    item["status"] = "ok"
    item["checkedAt"] = now()


def main():
    action = os.environ.get("ACTION", "").strip()
    items = load()

    if action == "add":
        url = os.environ.get("URL", "").strip()
        if not re.match(r"^https?://", url):
            sys.exit("URL が正しくありません")
        if any(i["key"] == key_of(url) for i in items):
            print("すでに登録済み")
            return
        item = {"id": id_of(url, items), "url": url, "key": key_of(url), "shop": shop_of(url), "brand": "", "title": "",
                "price": None, "listPrice": None, "sizes": [], "soldout": [], "colors": [], "image": None,
                "sourceImage": None, "status": "pending", "addedAt": now()}
        d = scrape(url)
        if d:
            apply(item, d)
        items.append(item)

    elif action == "delete":
        iid = os.environ.get("ID", "").strip()
        for old in IMG.glob(iid + ".*"):
            old.unlink()
        items = [i for i in items if i["id"] != iid]

    elif action == "edit":
        iid = os.environ.get("ID", "").strip()
        patch = json.loads(os.environ.get("PATCH") or "{}")
        item = next((i for i in items if i["id"] == iid), None)
        if not item:
            sys.exit("対象が見つかりません")
        for k in ("title", "brand"):
            if k in patch:
                item[k] = str(patch[k] or "")
        for k in ("price", "listPrice"):
            if k in patch:
                item[k] = int(patch[k]) if patch[k] else None
        for k in ("sizes", "soldout"):
            if k in patch:
                item[k] = [str(x) for x in patch[k]]
        if patch.get("sourceImage"):
            path = download_image(patch["sourceImage"], item["id"], item["url"])
            if path:
                item["image"], item["sourceImage"] = path, patch["sourceImage"]
        if patch.get("rescrape"):
            d = scrape(item["url"])
            if d:
                apply(item, d)
        elif item.get("title") and item.get("price"):
            item["status"] = "ok"

    elif action == "refresh":
        # 毎日の在庫・価格チェック。取れなかった商品は前の情報のまま残す
        for item in items:
            d = scrape(item["url"])
            if d:
                apply(item, d)
            else:
                print(f"skip {item['id']}", file=sys.stderr)

    else:
        print("no action")
        return

    save(items)


if __name__ == "__main__":
    main()
