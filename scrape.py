"""Сборщик скидок для «Атласа стиля».

Раз в день:
  1. Проверяет вещи из плана (watch.json): цена, старая цена, есть ли нужный размер.
  2. Ищет новые уценки Zara Грузия в мужских категориях и отбирает по составу.
  3. Пишет data/latest.json (для сайта), data/history/ГГГГ-ММ.json (история цен).
  4. Если есть новости — отправляет одно сообщение в Telegram (TG_TOKEN, TG_CHAT_ID).

Только стандартная библиотека Python. Защиту от ботов не обходит: что закрыто — помечается.
"""
import json, os, re, sys, time, urllib.request, urllib.parse, urllib.error, datetime, html, random

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
CFG = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
WATCH = json.load(open(os.path.join(ROOT, "watch.json"), encoding="utf-8"))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
TZ = datetime.timezone(datetime.timedelta(hours=4))  # Тбилиси
NOW = datetime.datetime.now(TZ)
TODAY = NOW.strftime("%Y-%m-%d")
LOG = []


def log(*a):
    s = " ".join(str(x) for x in a)
    LOG.append(s)
    print(s, flush=True)


def get(url, tries=2):
    """GET JSON с паузой между запросами. Возвращает (json|None, статус)."""
    for i in range(tries):
        time.sleep(1.2 + random.random())
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json,text/plain,*/*", "Accept-Language": "en"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read().decode("utf-8", "replace")
            try:
                return json.loads(body), 200
            except ValueError:
                return None, "не JSON (вероятно, защита от ботов)"
        except urllib.error.HTTPError as e:
            if e.code == 429 and i + 1 < tries:
                time.sleep(15)
                continue
            return None, e.code
        except Exception as e:  # сеть, таймаут
            if i + 1 < tries:
                continue
            return None, type(e).__name__
    return None, "?"


def load(path, default):
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return default


def save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def want_sizes(cls, store=None):
    s = CFG["sizes"]
    if cls == "shoes" and store == "shopify-uk":
        return s["shoes_uk"]
    if cls == "shoes" and store == "shopify-us":
        return s["shoes_us"]
    return s.get(cls or "", [])


def size_ok(name, wanted):
    """'M' / '40' / 'EU 42' / 'UK 8 / E' / 'S-M' → совпадает ли с одним из нужных."""
    n = str(name).upper().replace(",", ".")
    toks = re.findall(r"\d+(?:\.\d)?|XXS|XS|XXL|XL|S|M|L", n)
    return any(w.upper() in toks for w in wanted)


# ---------------- Zara Грузия ----------------
Z = "https://www.zara.com/ge/en/"


def ajax(url):
    return url + ("&" if "?" in url else "?") + "ajax=true"


def natural_pct(comp, natural):
    """comp: [{'material':'cotton','percentage':'100%'}] → сумма % натуральных волокон."""
    tot = 0.0
    for c in comp:
        m = (c.get("material") or c.get("name") or "").lower()
        p = re.sub(r"[^\d.]", "", str(c.get("percentage") or c.get("description") or "0")) or "0"
        if any(k in m for k in natural) and "effect" not in m and "faux" not in m and "split" not in m:
            tot += float(p)
    return tot


def zara_comp(prod):
    """Читает detailedComposition → (строка, % натурального в основной ткани, % натурального в подкладке|None)."""
    dc = (prod.get("detail") or {}).get("detailedComposition") or {}
    parts, main, lining = [], None, None
    nat = CFG["markdown"]["natural"]
    for p in dc.get("parts", []):
        comps = p.get("components", [])
        if not comps:
            for a in p.get("areas", []):
                comps += a.get("components", [])
        d = (p.get("description") or "").upper()
        txt = ", ".join(f"{c.get('percentage','')} {c.get('material','')}".strip() for c in comps)
        if txt:
            parts.append(f"{d.title()}: {txt}" if d else txt)
        pct = natural_pct(comps, nat)
        if main is None and ("OUTER" in d or "MAIN" in d or "UPPER" in d or not d):
            main = pct
        if "LINING" in d and lining is None:
            lining = pct
    return "; ".join(parts), main, lining


def zara_color(prod, url, hint=None):
    colors = (prod.get("detail") or {}).get("colors") or []
    if not colors:
        return None
    m = re.search(r"[?&]v1=(\d+)", url or "")
    if m:
        for c in colors:
            if str(c.get("productId")) == m.group(1):
                return c
    if hint:
        h = hint.lower()
        for c in colors:  # сначала точное совпадение («White», а не «Blue/White»)
            if (c.get("name") or "").lower() == h:
                return c
        for c in colors:
            n = (c.get("name") or "").lower()
            if h in n and "/" not in n:
                return c
        for c in colors:
            if h in (c.get("name") or "").lower():
                return c
    return colors[0]


def zara_img(c):
    try:
        x = c["xmedia"][0]
        return x["url"].replace("{width}", "750")
    except Exception:
        return None


def zara_item(w):
    d, st = get(ajax(w["url"]))
    if not d or "product" not in d:
        return {"ok": False, "err": f"Zara: {st}"}
    prod = d["product"]
    c = zara_color(prod, w["url"], w.get("color"))
    if not c:
        return {"ok": False, "err": "Zara: нет цветов (товар снят?)"}
    wanted = want_sizes(w.get("size"))
    sizes = [(s.get("name"), s.get("availability")) for s in c.get("sizes", [])]
    avail = [n for n, a in sizes if a in ("in_stock", "low_on_stock")]
    mine = [n for n in avail if size_ok(n, wanted)] if wanted else avail
    comp, _, _ = zara_comp(prod)
    return {"ok": True, "price": (c.get("price") or 0) / 100, "old": (c.get("oldPrice") or 0) / 100 or None,
            "color": c.get("name"), "sizes_all": [n for n, _ in sizes], "sizes_avail": avail, "mine": mine,
            "need_size": bool(wanted), "comp": comp, "img": zara_img(c), "cur": "GEL"}


def zara_categories():
    d, st = get(Z + "categories?ajax=true")
    if not d:
        log("Zara: дерево категорий недоступно:", st)
        return []
    sec = [s.upper() for s in CFG["markdown"]["zara_sections"]]
    out = {}

    def walk(cs, path):
        for c in cs:
            name = (c.get("name") or "").strip()
            p = path + [name.upper()]
            if p and p[0] == "MAN":
                last = name.upper()
                parent = p[-2] if len(p) > 1 else ""
                # берём «VIEW ALL» внутри нужного раздела или сам раздел без подкатегорий
                hit = any(k in last for k in sec) or (last == "VIEW ALL" and any(k in parent for k in sec))
                if hit and not c.get("subcategories") and not any(x in " ".join(p) for x in ("ATHLETICZ", "SPECIAL EDITION", "ORIGINS")):
                    out[c["id"]] = " / ".join(p[1:])
            walk(c.get("subcategories") or [], p)

    walk(d.get("categories", []), [])
    return list(out.items())


def zara_markdowns(seen):
    mc = CFG["markdown"]
    found = {}
    cats = zara_categories()
    log(f"Zara: категорий для уценок — {len(cats)}")
    for cid, cname in cats:
        d, st = get(f"{Z}category/{cid}/products?ajax=true")
        if not d:
            log("  категория", cname, "→", st)
            continue
        for g in d.get("productGroups", []):
            for el in g.get("elements", []):
                for c in el.get("commercialComponents", []) or []:
                    old, price = c.get("oldPrice"), c.get("price")
                    if not old or not price or c.get("sectionName") != "MAN":
                        continue
                    disc = round((1 - price / old) * 100)
                    name = (c.get("name") or "").upper()
                    if disc < mc["min_discount"] or any(wd in name for wd in mc["skip_words"]):
                        continue
                    seo = c.get("seo") or {}
                    if not seo.get("keyword") or not seo.get("seoProductId"):
                        continue
                    url = f"{Z}{seo['keyword']}-p{seo['seoProductId']}.html?v1={c['id']}"
                    found[str(c["id"])] = {"pid": str(c["id"]), "name": c.get("name"), "url": url, "price": price / 100,
                                           "old": old / 100, "disc": disc, "cat": cname, "family": c.get("familyName")}
    log(f"Zara: уценок от {mc['min_discount']}% — {len(found)}")
    # подробности (размер, состав) — сначала новые, потом те, что давно не проверяли
    order = sorted(found.values(), key=lambda x: (x["pid"] in seen, seen.get(x["pid"], {}).get("checked", "")))
    out = []
    for x in order[: mc["max_checks_per_run"]]:
        d, st = get(ajax(x["url"]))
        if not d or "product" not in d:
            continue
        prod = d["product"]
        c = zara_color(prod, x["url"])
        comp, main, lining = zara_comp(prod)
        fam = (x["family"] or "").upper() + " " + x["cat"]
        cls = "shoes" if re.search(r"SHOE|BOOT|LOAFER|TRAINER|SANDAL", fam) else \
              "tailoring" if re.search(r"BLAZER|SUIT JACKET|AMERICANA", fam + " " + (x["name"] or "").upper()) else \
              "bottom" if re.search(r"TROUSER|JEAN|PANT|BERMUDA|SHORT", fam) else "top"
        wanted = want_sizes(cls)
        avail = [s.get("name") for s in (c or {}).get("sizes", []) if s.get("availability") in ("in_stock", "low_on_stock")]
        mine = [n for n in avail if size_ok(n, wanted)]
        verdict, reason = "ok", ""
        if main is None:
            verdict, reason = "skip", "состав не указан"
        elif main < mc["min_natural_pct"]:
            verdict, reason = "skip", f"натуральных волокон {main:.0f}% — меньше {mc['min_natural_pct']}%"
        elif cls == "shoes" and lining is not None and lining < 50:
            verdict, reason = "skip", "подкладка синтетическая"
        elif not mine:
            verdict, reason = "nosize", "твоего размера нет"
        x.update({"comp": comp, "natural": main, "img": zara_img(c) if c else None, "color": (c or {}).get("name"),
                  "mine": mine, "size_class": cls, "verdict": verdict, "reason": reason, "store": "Zara"})
        seen[x["pid"]] = {**seen.get(x["pid"], {}), "checked": TODAY}
        out.append(x)
    return out


# ---------------- Massimo Dutti Грузия (открытый каталог, только вещи из плана) ----------------
MD = "https://www.massimodutti.com/itxrest/3/catalog/store/35009523/30359534/productsArray?languageId=-1&appId=1&productIds="


def md_items(ws):
    ids = {}
    for w in ws:
        m = re.search(r"pelement=(\d+)", w["url"] or "")
        if m:
            ids[m.group(1)] = w
    res = {w["id"]: {"ok": False, "err": "Massimo Dutti: в ссылке нет номера товара (pelement)"} for w in ws}
    if not ids:
        return res
    d, st = get(MD + ",".join(ids))
    if not d:
        for w in ids.values():
            res[w["id"]] = {"ok": False, "err": f"Massimo Dutti: {st}"}
        return res
    for p in d.get("products", []):
        w = ids.get(str(p.get("id")))
        if not w:
            continue
        b = (p.get("bundleProductSummaries") or [p])[0]
        det = b.get("detail") or {}
        colors = det.get("colors") or []
        h = (w.get("color") or "").lower()
        c = next((c for c in colors if h and (c.get("name") or "").lower() == h), None) \
            or next((c for c in colors if h and h in (c.get("name") or "").lower()), None) \
            or (colors[0] if colors else None)
        if not c:
            continue
        sz = c.get("sizes") or []
        price = int(sz[0].get("price") or 0) / 100 if sz else 0
        old = int(sz[0].get("oldPrice") or 0) / 100 if sz and sz[0].get("oldPrice") else None
        avail = [s.get("name") for s in sz if s.get("visibilityValue") == "SHOW"]
        wanted = want_sizes(w.get("size"))
        comp = "; ".join(", ".join(f"{x.get('percentage')}% {x.get('name')}" for x in part.get("composition", [])) for part in det.get("composition", []))
        img = None
        try:
            xms = det.get("xmedia") or []
            xm = next((x for x in xms if str(x.get("colorCode")) == str(c.get("id"))), xms[0])
            img = xm["xmediaItems"][0]["medias"][0]["url"] + "&w=750"
        except Exception:
            pass
        res[w["id"]] = {"ok": True, "price": price, "old": old, "color": c.get("name"), "sizes_all": [s.get("name") for s in sz],
                        "sizes_avail": avail, "mine": [n for n in avail if size_ok(n, wanted)] if wanted else avail,
                        "need_size": bool(wanted), "comp": comp, "img": img, "cur": "GEL"}
    return res


# ---------------- Shopify (Meermin, Oliver Cabell, Johnstons) ----------------
def shopify_item(w):
    u = re.sub(r"[?#].*$", "", w["url"])
    d, st = get(u + ".js")
    if not d or "variants" not in d:
        return {"ok": False, "err": f"{urllib.parse.urlparse(u).netloc}: {st}"}
    host = urllib.parse.urlparse(u).netloc
    cart, _ = get(f"https://{host}/cart.js")  # валюта магазина
    cur = (cart or {}).get("currency") or ("EUR" if "meermin" in host else "USD")
    kind = "shopify-uk" if "meermin" in host else "shopify-us"
    wanted = want_sizes(w.get("size"), kind)
    vs = d["variants"]
    price = min(v["price"] for v in vs) / 100
    olds = [v.get("compare_at_price") for v in vs if v.get("compare_at_price") and v["compare_at_price"] > v["price"]]
    old = max(olds) / 100 if olds else None

    def vt(v):  # «BROWN / 8.5 / E» → «8.5 E», «Men's / White / 9» → «9»
        parts = [p.strip() for p in (v.get("title") or "").split("/")]
        keep = [p for p in parts if re.search(r"\d", p) or len(p) <= 2]
        return " ".join(keep) or v.get("title")
    for v in vs:
        v["title"] = vt(v)
    avail = [v.get("title") for v in vs if v.get("available")]
    rate = CFG["rates"].get(cur, 1)
    gel = price * rate
    if gel > 300:
        gel = gel * (1 + CFG["import_vat"]) + CFG["import_fee"]
    img = d.get("featured_image")
    if img and img.startswith("//"):
        img = "https:" + img
    return {"ok": True, "price_foreign": price, "old_foreign": old, "cur": cur, "price": round(gel),
            "old": round(old * rate * (1 + CFG["import_vat"]) + CFG["import_fee"]) if old else None,
            "sizes_all": [v.get("title") for v in vs], "sizes_avail": avail,
            "mine": [n for n in avail if size_ok(n, wanted)] if wanted else avail, "need_size": bool(wanted),
            "comp": "", "img": img, "note": f"Цена в {cur} пересчитана в ₾ с НДС 18% и сбором, без доставки"}


# ---------------- Telegram ----------------
def tg_send(text):
    tok, chat = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT_ID")
    if not tok or not chat:
        log("Telegram: TG_TOKEN или TG_CHAT_ID не заданы — сообщение не отправлено")
        return False
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) > 3800:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    chunks.append(cur)
    ok = True
    for ch in chunks:
        body = urllib.parse.urlencode({"chat_id": chat, "text": ch, "parse_mode": "HTML", "disable_web_page_preview": "true"}).encode()
        try:
            urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{tok}/sendMessage", data=body), timeout=30).read()
        except urllib.error.HTTPError as e:
            log("Telegram: ошибка", e.code, e.read().decode("utf-8", "replace")[:300])
            ok = False
        except Exception as e:
            log("Telegram: ошибка", type(e).__name__)
            ok = False
    return ok


def fm(x):
    return f"{x:,.0f}".replace(",", " ")


# ---------------- main ----------------
def main():
    dry = "--dry" in sys.argv
    state = load(os.path.join(DATA, "state.json"), {"plan": {}, "md_seen": {}, "first": True})
    first = state.get("first", True) or not state.get("plan")
    first = first or os.environ.get("FORCE_HELLO") == "true"
    hist_path = os.path.join(DATA, "history", NOW.strftime("%Y-%m") + ".json")
    hist = load(hist_path, {})

    # 1) вещи из плана
    plan = []
    md_ws = [w for w in WATCH if w["store"] == "md"]
    md_res = md_items(md_ws) if md_ws else {}
    for w in WATCH:
        if w["store"] == "zara":
            r = zara_item(w)
        elif w["store"] == "md":
            r = md_res.get(w["id"], {"ok": False, "err": "?"})
        elif w["store"] == "shopify":
            r = shopify_item(w)
        else:
            r = {"ok": False, "err": w.get("why_untracked", "не отслеживается")}
        plan.append({**{k: w.get(k) for k in ("id", "name", "wave", "store", "url", "plan_price", "size")}, **r,
                     "img": r.get("img") or w.get("img")})
        log(("✓" if r.get("ok") else "·"), w["id"], r.get("price"), r.get("old") or "", ("размер: " + ",".join(r.get("mine", []))) if r.get("ok") else r.get("err"))

    # 2) уценки Zara
    md = zara_markdowns(state.setdefault("md_seen", {}))

    # 3) новости: сравнение с прошлым днём
    news_plan, news_md = [], []
    for p in plan:
        if not p.get("ok"):
            continue
        prev = state["plan"].get(p["id"], {})
        reasons = []
        if prev.get("price") and p["price"] < prev["price"] - 0.5:
            reasons.append(f"цена {fm(prev['price'])} → {fm(p['price'])} ₾")
        elif not prev and p.get("old"):
            reasons.append(f"скидка: было {fm(p['old'])}, сейчас {fm(p['price'])} ₾")
        if p.get("need_size") and p.get("mine") and not prev.get("mine"):
            reasons.append("появился твой размер: " + ", ".join(p["mine"]))
        if p.get("need_size") and not p.get("mine") and prev.get("mine"):
            reasons.append("твой размер закончился")
        if reasons and not first:
            news_plan.append((p, reasons))
        state["plan"][p["id"]] = {"price": p["price"], "mine": p.get("mine", [])}
    for x in md:
        if x["verdict"] != "ok":
            continue
        prev = state["md_seen"].get(x["pid"], {})
        if not prev.get("told") or x["price"] < prev.get("told_price", 1e9) - 0.5:
            news_md.append(x)
            state["md_seen"][x["pid"]].update({"told": TODAY, "told_price": x["price"]})

    # одна модель в нескольких цветах — одной строкой
    def group(xs):
        g = {}
        for x in xs:
            k = x["url"].split("?")[0]
            if k not in g:
                g[k] = {**x, "colors": [], "mine": []}
            e = g[k]
            e["colors"].append({"color": x.get("color"), "url": x["url"], "img": x.get("img"), "mine": x["mine"], "price": x["price"]})
            e["mine"] = sorted(set(e["mine"]) | set(x["mine"]), key=lambda s: (len(s), s))
            e["price"] = min(e["price"], x["price"])
            e["disc"] = max(e["disc"], x["disc"])
        return list(g.values())
    news_md = group(news_md)

    # 4) история цен
    day = hist.setdefault(TODAY, {})
    for p in plan:
        if p.get("ok"):
            day[p["id"]] = p["price"]
    for x in md:
        day["z" + x["pid"]] = x["price"]

    # 5) файлы для сайта
    latest = {"updated": NOW.isoformat(timespec="minutes"), "sizes": CFG["sizes_note"], "plan": plan,
              "markdowns": sorted(group([x for x in md if x["verdict"] == "ok"]), key=lambda x: -x["disc"]),
              "markdowns_nosize": group([x for x in md if x["verdict"] == "nosize"]),
              "markdowns_rejected": [{k: x[k] for k in ("name", "url", "price", "old", "disc", "comp", "reason")} for x in md if x["verdict"] == "skip"],
              "log": LOG[-200:]}
    if not dry:
        save(os.path.join(DATA, "latest.json"), latest)
        save(hist_path, hist)
        state["first"] = False
        # чистим старые уценки, чтобы файл не рос
        cutoff = (NOW - datetime.timedelta(days=60)).strftime("%Y-%m-%d")
        state["md_seen"] = {k: v for k, v in state["md_seen"].items() if v.get("checked", TODAY) >= cutoff}
        save(os.path.join(DATA, "state.json"), state)

    # 6) Telegram
    lines = []
    if first:
        ok = sum(1 for p in plan if p.get("ok"))
        lines.append(f"<b>Сборщик скидок запущен</b> · {TODAY}")
        lines.append(f"Отслеживаю {ok} вещей из плана и уценки Zara от {CFG['markdown']['min_discount']}%. Дальше пишу только когда есть новости.")
    if news_plan:
        lines.append("\n<b>Вещи из плана</b>")
        for p, rs in news_plan:
            lines.append(f"• <a href=\"{html.escape(p['url'])}\">{html.escape(p['name'])}</a> — {'; '.join(rs)}")
    if news_md:
        lines.append(f"\n<b>Новые уценки по твоему размеру</b> ({len(news_md)})")
        for x in sorted(news_md, key=lambda x: -x["disc"])[:25]:
            lines.append(f"• <a href=\"{html.escape(x['url'])}\">{html.escape(x['name'].capitalize())}</a> — {fm(x['old'])} → <b>{fm(x['price'])} ₾</b> (−{x['disc']}%)\n   {html.escape(x['comp'][:120])} · размер {', '.join(x['mine'])}"
                         + (f" · цвета: {html.escape(', '.join(c['color'] or '' for c in x['colors']))}" if len(x["colors"]) > 1 else ""))
        if len(news_md) > 25:
            lines.append(f"…и ещё {len(news_md) - 25} — на сайте в разделе «Скидки»")
    if lines:
        lines.append("\nВсё подробно: https://looksmaxing-kappa.vercel.app/#sales")
        msg = "\n".join(lines)
        print("\n----- Telegram -----\n" + msg)
        if not dry:
            tg_send(msg)
    else:
        log("Новостей нет — в Telegram ничего не отправлено")


if __name__ == "__main__":
    main()
