#!/usr/bin/env python3
"""
Perfume price comparison for 5 Bangladeshi stores.

Setup:   pip install flask requests beautifulsoup4
Run:     python perfume_compare.py          -> open http://127.0.0.1:5055
Test:    python perfume_compare.py --test "hawas ice"   (prints results + errors in the terminal)
"""
import html as htmllib
import json
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template_string, request

SITES = [
    {"name": "ScentZone BD", "base": "https://scentzonebd.com", "kind": "woo"},
    {"name": "Fragrantica BD", "base": "https://fragranticabangladesh.com", "kind": "woo"},
    {"name": "Budget Perfumes BD", "base": "https://budgetperfumes.com.bd", "kind": "woo"},
    {"name": "Signature Fragrance", "base": "https://signaturefrag.com", "kind": "signature"},
    {"name": "Fragwave BD", "base": "https://fragwavebd.com", "kind": "woo"},
]
HEADERS = {"User-Agent": "Mozilla/5.0 (personal price-comparison tool)", "Accept-Language": "en"}
TIMEOUT = 15
MAX_PRODUCTS = 8          # max matching products examined per site
CACHE_SECONDS = 600       # repeat searches within 10 minutes reuse results (keeps load on the shops low)
_cache = {}

# ---------- text helpers ----------
BRANDS = sorted([
    "ahmed al maghribi", "arabiyat prestige", "maison alhambra", "french avenue", "swiss arabian",
    "al haramain", "arabian oud", "jean paul gaultier", "carolina herrera", "giorgio armani",
    "paco rabanne", "dolce and gabbana", "dolce gabbana", "hugo boss", "ralph lauren", "calvin klein",
    "lattafa", "rasasi", "armaf", "afnan", "rayhaan", "khadlaj", "risala", "asdaaf", "hamidi", "mancera",
    "amouage", "dior", "versace", "creed", "ajmal", "bentley", "azzaro", "davidoff", "jaguar", "nusuk",
    "riiffs", "caneza", "atralia", "paris corner", "gulf orchid", "ysl", "adidas",
], key=len, reverse=True)
STOP = {"edp", "edt", "extrait", "de", "parfum", "perfume", "perfumes", "eau", "for", "by", "and", "him",
        "her", "men", "man", "women", "woman", "everyone", "unisex", "ml", "spray", "the", "pour", "homme",
        "femme", "full", "presentation", "bottle", "tester", "new", "original", "with", "limited", "edition"}


def norm(text):
    return " " + re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (text or "").lower())).strip() + " "


def key_of(title):
    """Brand-less, order-free key so 'Rasasi Hawas Ice EDP 100ml' == 'Hawas Ice'."""
    t = norm(title)
    for b in BRANDS:
        t = t.replace(" " + b + " ", " ")
    toks = [w for w in t.split() if w not in STOP and not re.fullmatch(r"\d+(ml)?", w)]
    return " ".join(sorted(set(toks)))


def matches(title, query):
    t = norm(title)
    toks = [w for w in norm(query).split() if not re.fullmatch(r"\d+(ml)?", w)]
    return bool(toks) and all(w in t for w in toks)


def size_ml(text):
    m = re.search(r"(\d+(?:\.\d+)?)\s*ml", text or "", re.I)
    return float(m.group(1)) if m else None


def numbers(text):
    return [float(x.replace(",", "")) for x in re.findall(r"\d[\d,]*(?:\.\d+)?", text or "")]


def get(url, **kw):
    return requests.get(url, headers=HEADERS, timeout=TIMEOUT, **kw)


def offer(site, title, url, price, in_stock, size=None, note=""):
    return {"site": site, "title": title, "url": url, "price": price,
            "in_stock": in_stock, "size": size, "note": note}


# ---------- WooCommerce stores (4 of the 5) ----------
def woo_search_api(base, q):
    r = get(f"{base}/wp-json/wc/store/v1/products", params={"search": q, "per_page": MAX_PRODUCTS * 2})
    if r.status_code != 200 or "json" not in r.headers.get("content-type", ""):
        raise ValueError(f"Store API unavailable (HTTP {r.status_code})")
    out = []
    for p in r.json():
        pr = p.get("prices") or {}
        div = 10 ** int(pr.get("currency_minor_unit", 2))
        rng = pr.get("price_range") or {}
        lo = float(rng.get("min_amount") or pr.get("price") or 0) / div or None
        hi = float(rng.get("max_amount") or pr.get("price") or 0) / div or None
        out.append({"title": htmllib.unescape(p.get("name", "")), "url": p.get("permalink"),
                    "lo": lo, "hi": hi, "in_stock": p.get("is_in_stock"),
                    "variable": p.get("type") == "variable" or bool(p.get("variations"))})
    return out


def woo_search_html(base, q):
    r = get(f"{base}/", params={"s": q, "post_type": "product"})
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for li in soup.select("li.product, div.product, article.product"):
        a = li.select_one("a[href]")
        ttl = li.select_one(".woocommerce-loop-product__title, h2, h3")
        price = li.select_one(".price")
        if not (a and ttl and price):
            continue
        node = price.select_one("ins") or price
        amts = [numbers(e.get_text())[0] for e in node.select(".woocommerce-Price-amount") if numbers(e.get_text())]
        amts = amts or sorted(set(numbers(node.get_text())))
        if not amts:
            continue
        classes = " ".join(li.get("class", []))
        oos = "outofstock" in classes or "out of stock" in li.get_text().lower()
        out.append({"title": ttl.get_text(strip=True), "url": a["href"], "lo": min(amts), "hi": max(amts),
                    "in_stock": not oos, "variable": min(amts) != max(amts)})
    return out


def woo_variations(url):
    soup = BeautifulSoup(get(url).text, "html.parser")
    form = soup.select_one("form.variations_form[data-product_variations]")
    if not form:
        return []
    try:
        data = json.loads(form["data-product_variations"])
    except ValueError:
        return []
    return data if isinstance(data, list) else []


def woo_offers(site, q):
    base = site["base"]
    try:
        prods = woo_search_api(base, q)
    except Exception:
        prods = woo_search_html(base, q)
    prods = [p for p in prods if matches(p["title"], q)][:MAX_PRODUCTS]

    def build(p):
        title, url = p["title"], p["url"]
        if p["variable"]:
            vs = woo_variations(url)
            if vs:
                res = []
                for v in vs:
                    label = " / ".join(str(x) for x in (v.get("attributes") or {}).values() if x)
                    res.append(offer(site["name"], title, url, v.get("display_price"),
                                     v.get("is_in_stock"), size_ml(label), label))
                return res
            return [offer(site["name"], title, url, p["hi"], p["in_stock"], size_ml(title),
                          f"Sizes vary (৳{p['lo']:,.0f}–৳{p['hi']:,.0f}); showing top of range as the full-bottle estimate")]
        note = "" if size_ml(title) else "Size not stated"
        return [offer(site["name"], title, url, p["lo"], p["in_stock"], size_ml(title), note)]

    with ThreadPoolExecutor(4) as ex:
        return [o for group in ex.map(build, prods) for o in group]


# ---------- Signature Fragrance (custom platform, parsed from HTML) ----------
def signature_parse(page_html, base):
    soup = BeautifulSoup(page_html, "html.parser")
    found, seen = [], set()
    for a in soup.select('a[href*="/products/"]'):
        href = a["href"]
        if href.rstrip("/").endswith("/products") or "?" in href or href in seen:
            continue
        title = a.get_text(" ", strip=True)
        if len(title) < 3 or title.lower() in ("out of stock", "select options"):
            continue
        card = a
        for _ in range(5):
            if "৳" in card.get_text():
                break
            card = card.parent
            if card is None:
                break
        text = card.get_text(" ", strip=True) if card is not None else ""
        amts = [float(x.replace(",", "")) for x in re.findall(r"৳\s*([\d,]+(?:\.\d+)?)", text)]
        if not amts:
            continue
        seen.add(href)
        url = href if href.startswith("http") else base + href
        found.append({"title": title, "url": url, "lo": min(amts), "hi": max(amts),
                      "in_stock": "out of stock" not in text.lower(), "variable": min(amts) != max(amts)})
    return found


def signature_offers(site, q):
    base, items = site["base"], []
    for path in ("/products?search=", "/products?q=", "/products?query=", "/search?q="):
        r = get(base + path + quote_plus(q))
        if r.status_code != 200:
            continue
        items = [p for p in signature_parse(r.text, base) if matches(p["title"], q)]
        if items:
            break
    res = []
    for p in items[:MAX_PRODUCTS]:
        note = ""
        if p["variable"]:
            note = (f"Sizes vary (৳{p['lo']:,.0f}–৳{p['hi']:,.0f}); "
                    "showing top of range as the full-bottle estimate. Open the page to confirm.")
        res.append(offer(site["name"], p["title"], p["url"], p["hi"], p["in_stock"], size_ml(p["title"]), note))
    return res


# ---------- search + comparison ----------
def fetch_all(q):
    key = norm(q)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1], hit[2]

    def run(site):
        try:
            fn = signature_offers if site["kind"] == "signature" else woo_offers
            return site["name"], fn(site, q), None
        except Exception as e:
            return site["name"], [], f"{type(e).__name__}: {e}"

    with ThreadPoolExecutor(len(SITES)) as ex:
        results = list(ex.map(run, SITES))
    offers = [o for _, os_, _ in results for o in os_]
    errors = {n: err for n, _, err in results if err}
    if len(errors) < len(SITES):
        _cache[key] = (time.time(), offers, errors)
    return offers, errors


def size_ok(o, mode):
    if mode == "any":
        return True
    if mode == "full":
        return o["size"] is None or o["size"] >= 50
    try:
        return o["size"] is not None and abs(o["size"] - float(mode)) < 0.5
    except ValueError:
        return True


def compare(offers, errors, mode):
    groups = {}
    for o in offers:
        if o["price"] and size_ok(o, mode):
            g = groups.setdefault(key_of(o["title"]), {"titles": Counter(), "offers": {}})
            g["titles"][o["title"]] += 1
            g["offers"].setdefault(o["site"], []).append(o)
    out = []
    for g in groups.values():
        rows, best = [], None
        for s in SITES:
            n = s["name"]
            if n in errors:
                rows.append({"site": n, "status": "error", "detail": errors[n]})
                continue
            cands = g["offers"].get(n)
            if not cands:
                rows.append({"site": n, "status": "not_listed"})
                continue
            o = sorted(cands, key=lambda c: (c["in_stock"] is False, c["price"]))[0]
            row = dict(o, status="out_of_stock" if o["in_stock"] is False else "in_stock",
                       per_ml=round(o["price"] / o["size"], 1) if o["size"] else None)
            rows.append(row)
            if row["status"] == "in_stock" and (best is None or row["price"] < best["price"]):
                best = row
        if best:
            best["best"] = True
        out.append({"title": max(g["titles"], key=len), "rows": rows, "has_best": best is not None,
                    "sites_listed": sum(r["status"] in ("in_stock", "out_of_stock") for r in rows)})
    out.sort(key=lambda g: (-g["sites_listed"], g["title"]))
    return out


# ---------- web UI ----------
app = Flask(__name__)


@app.route("/api/search")
def api_search():
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify({"groups": [], "errors": {}, "q": q})
    t0 = time.time()
    offers, errors = fetch_all(q)
    return jsonify({"q": q, "groups": compare(offers, errors, request.args.get("size", "full")),
                    "errors": errors, "seconds": round(time.time() - t0, 1)})


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Perfume price check</title>
<style>
:root{--bg:#f2f4f3;--ink:#17201c;--mute:#5b6761;--line:#d5dbd8;--card:#fff;--best:#0f6b4a;--bestbg:#e2f3ea;--warn:#9a5a06;--bad:#a3281f}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:860px;margin:0 auto;padding:28px 16px 60px}
h1{font-size:1.5rem;margin:0 0 4px}p.sub{margin:0 0 18px;color:var(--mute)}
form{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}
input,select,button{font:inherit;padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:#fff;color:var(--ink)}
input{flex:1 1 260px}button{background:var(--ink);color:#fff;border-color:var(--ink);cursor:pointer}
:focus-visible{outline:3px solid #7aa8ff;outline-offset:2px}
#status{color:var(--mute);margin:10px 0}.warn{background:#fff4e0;border:1px solid #f0d3a0;border-radius:8px;padding:10px 12px;margin:10px 0;font-size:.92rem}
section.g{background:var(--card);border:1px solid var(--line);border-radius:10px;margin:14px 0;overflow:hidden}
section.g h2{font-size:1.05rem;margin:0;padding:12px 14px;border-bottom:1px solid var(--line)}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
td{padding:9px 14px;border-bottom:1px solid #eceeed;vertical-align:top;font-size:.95rem}tr:last-child td{border-bottom:0}
td.p{text-align:right;white-space:nowrap}small{display:block;color:var(--mute);font-size:.8rem}
tr.best{background:var(--bestbg)}tr.best td.p{color:var(--best);font-weight:700}
.b{display:inline-block;background:var(--best);color:#fff;border-radius:4px;padding:0 6px;font-size:.75rem;margin-left:6px}
.in{color:var(--best)}.out{color:var(--warn)}.none{color:var(--mute)}.err{color:var(--bad)}
a{color:inherit}@media(max-width:560px){td{padding:8px 10px}}
</style></head><body><main>
<h1>Perfume price check</h1><p class="sub">Searches 5 Bangladeshi stores at once. Prices are in ৳.</p>
<form id="f"><input id="q" placeholder="e.g. Hawas Ice, Club de Nuit Intense, Yara" autofocus required>
<select id="size" aria-label="Bottle size"><option value="full">Full bottles (50ml+)</option><option value="any">Any size</option>
<option value="100">100ml</option><option value="50">50ml</option><option value="30">30ml</option><option value="15">15ml</option>
<option value="10">10ml</option><option value="5">5ml</option><option value="3">3ml</option></select><button>Compare prices</button></form>
<div id="status"></div><div id="out"></div></main>
<script>
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const tk=n=>'৳'+Math.round(n).toLocaleString('en-US');
function row(r){
 if(r.status==='error')return `<tr><td>${esc(r.site)}</td><td colspan="2" class="err">Could not check this site (${esc(r.detail)}). This does not mean it is unavailable.</td></tr>`;
 if(r.status==='not_listed')return `<tr><td>${esc(r.site)}</td><td colspan="2" class="none">Not available on this site</td></tr>`;
 const st=r.status==='in_stock'?'<span class="in">In stock</span>':'<span class="out">Out of stock</span>';
 const sz=[r.size?r.size+'ml':'', r.per_ml?tk(r.per_ml)+'/ml':''].filter(Boolean).join(' · ');
 return `<tr class="${r.best?'best':''}"><td><a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.site)}</a>${r.best?'<span class="b">Best price</span>':''}${r.note?`<small>${esc(r.note)}</small>`:''}</td>
 <td>${st}${sz?`<small>${esc(sz)}</small>`:''}</td><td class="p">${tk(r.price)}</td></tr>`;
}
document.getElementById('f').addEventListener('submit',async e=>{
 e.preventDefault();const q=document.getElementById('q').value.trim(),size=document.getElementById('size').value;
 const st=document.getElementById('status'),out=document.getElementById('out');out.innerHTML='';st.textContent='Checking all 5 stores…';
 try{const d=await (await fetch(`/api/search?q=${encodeURIComponent(q)}&size=${size}`)).json();
  st.textContent=d.groups.length?`${d.groups.length} perfume${d.groups.length>1?'s':''} found in ${d.seconds}s`:'';
  const names=Object.keys(d.errors);let html='';
  if(names.length)html+=`<div class="warn">Could not reach: ${names.map(esc).join(', ')}. Their results are missing, not "unavailable".</div>`;
  if(!d.groups.length&&names.length<5)html+=`<div class="warn">No matching perfume found on the stores that responded. Try a shorter name, or "Any size".</div>`;
  for(const g of d.groups)html+=`<section class="g"><h2>${esc(g.title)}${g.has_best?'':' <small>(none in stock)</small>'}</h2><table>${g.rows.map(row).join('')}</table></section>`;
  out.innerHTML=html;}catch(err){st.textContent='';out.innerHTML=`<div class="warn">Something went wrong: ${esc(err)}</div>`}
});
</script></body></html>"""


@app.route("/")
def index():
    return render_template_string(PAGE)


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--test":
        offers, errors = fetch_all(sys.argv[2])
        print("Raw offers found:", len(offers))
        for n, e in errors.items():
            print(f"  ERROR {n}: {e}")
        for g in compare(offers, errors, "any"):
            print("\n" + g["title"])
            for r in g["rows"]:
                print("  ", r["site"], "|", r["status"], "|", r.get("price"), "|", r.get("size"), "|", r.get("url", r.get("detail", "")))
    else:
        print("Open http://127.0.0.1:5055 in your browser")
        app.run(host="127.0.0.1", port=5055)
