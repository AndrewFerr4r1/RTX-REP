"""
Monitor de preço de RTX 2060 / RTX 3060.
Busca na KaBuM e no Mercado Livre e avisa no Telegram quando achar
uma placa abaixo do preço definido.
"""
import html as html_lib
import json
import os
import re
from urllib.parse import quote, quote_plus

import requests

# ---------------- CONFIGURAÇÃO ----------------
PRECO_MAX = float(os.getenv("PRECO_MAX", "1800"))  # avisa se custar até isso
PRECO_MIN = float(os.getenv("PRECO_MIN", "600"))   # ignora abaixo disso (golpe/acessório)
TERMOS_BUSCA = ["rtx 3060", "rtx 2060"]
EXCLUIR = ["notebook", "laptop", "pc gamer", "computador", "desktop", "cooler",
           "suporte", "cabo", "adesivo", "backplate", "water", "kit", "mini pc"]

TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID")
ARQ_VISTOS = "vistos.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "pt-BR,pt;q=0.9",
}


def titulo_ok(titulo: str) -> bool:
    t = titulo.lower()
    if not re.search(r"rtx\s*-?\s*(2060|3060|3050|2070|2080|2090)", t):
        return False
    return not any(p in t for p in EXCLUIR)


def andar(obj):
    """Percorre qualquer JSON aninhado (inclusive JSON dentro de strings)."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from andar(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from andar(v)
    elif isinstance(obj, str) and len(obj) > 50 and obj[:1] in "{[":
        try:
            yield from andar(json.loads(obj))
        except ValueError:
            pass


def buscar_kabum(termo):
    url = f"https://www.kabum.com.br/busca/{termo.replace(' ', '-')}"
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
    if not m:
        return []
    resultados = []
    for item in andar(json.loads(m.group(1))):
        nome, codigo = item.get("name"), item.get("code")
        preco = item.get("priceWithDiscount") or item.get("price")
        if nome and codigo and isinstance(preco, (int, float)) and preco > 0:
            slug = item.get("friendlyName") or ""
            resultados.append({
                "loja": "KaBuM",
                "nome": nome,
                "preco": float(preco),
                "link": f"https://www.kabum.com.br/produto/{codigo}/{slug}".rstrip("/"),
            })
    return resultados


def buscar_mercadolivre(termo):
    pagina = baixar("https://lista.mercadolivre.com.br/" + quote(termo.replace(" ", "-")))
    resultados = []
    for bloco in re.split(r'class="(?:poly-card|ui-search-result)[ "]', pagina)[1:]:
        lk = re.search(r'href="(https://[^"]*mercadolivre\.com\.br/[^"]*)"', bloco)
        if not lk:
            continue
        link = lk.group(1).split("#")[0].split("?")[0]
        t = re.search(r'<a[^>]*href="[^"]*"[^>]*>([^<]{10,})</a>', bloco) or \
            re.search(r'title="([^"]{10,})"', bloco)
        if not t:
            continue
        sem_preco_antigo = re.sub(r'<s\b.*?</s>', '', bloco, flags=re.S)
        p = re.search(r'andes-money-amount__fraction[^>]*>([\d.]+)<', sem_preco_antigo)
        if not p:
            continue
        loja = "Mercado Livre" + (" (USADO)" if "Usado" in bloco else "")
        resultados.append({"loja": loja, "nome": html_lib.unescape(t.group(1).strip()),
                           "preco": preco_br(p.group(1) + ",00"), "link": link})
    return resultados


def baixar(url):
    """Baixa a página fingindo ser o Chrome (ajuda a passar pelo Cloudflare)."""
    try:
        from curl_cffi import requests as cr
        r = cr.get(url, headers=HEADERS, impersonate="chrome", timeout=30)
    except ImportError:
        r = requests.get(url, headers=HEADERS, timeout=30)
    if r.status_code in (403, 429, 503):
        raise RuntimeError(f"bloqueado pelo site (HTTP {r.status_code})")
    r.raise_for_status()
    return r.text


def preco_br(txt):
    return float(txt.replace(".", "").replace(",", "."))


def produtos_jsonld(pagina, loja):
    """Lê os dados de produto que muitos sites publicam no padrão schema.org."""
    out = []
    for bloco in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', pagina, re.S):
        try:
            dados = json.loads(bloco)
        except ValueError:
            continue
        for d in andar(dados):
            if d.get("@type") != "Product" or not d.get("name"):
                continue
            ofertas = d.get("offers") or {}
            if isinstance(ofertas, list):
                ofertas = ofertas[0] if ofertas else {}
            link = d.get("url") or ofertas.get("url")
            try:
                preco = float(ofertas.get("lowPrice") or ofertas.get("price"))
            except (TypeError, ValueError):
                continue
            if link:
                out.append({"loja": loja, "nome": html_lib.unescape(d["name"]),
                            "preco": preco, "link": link})
    return out


def buscar_pichau(termo):
    pagina = baixar("https://www.pichau.com.br/search?q=" + quote(termo))
    out = produtos_jsonld(pagina, "Pichau")
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', pagina, re.S)
    if m:
        for item in andar(json.loads(m.group(1))):
            nome, slug = item.get("name"), item.get("url_key")
            if not (nome and slug):
                continue
            preco = None
            pp = item.get("pichau_prices")
            if isinstance(pp, dict):
                preco = pp.get("avista")
            if preco is None:
                try:
                    preco = item["price_range"]["minimum_price"]["final_price"]["value"]
                except (KeyError, TypeError):
                    pass
            if isinstance(preco, (int, float)) and preco > 0:
                out.append({"loja": "Pichau", "nome": nome, "preco": float(preco),
                            "link": f"https://www.pichau.com.br/{slug}"})
    return out


def buscar_terabyte(termo):
    pagina = baixar("https://www.terabyteshop.com.br/busca?str=" + quote_plus(termo))
    out = produtos_jsonld(pagina, "Terabyte")
    if out:
        return out
    for bloco in pagina.split("product-item")[1:]:
        lk = re.search(r'href="(https://www\.terabyteshop\.com\.br/produto/\d+/[^"]+)"', bloco)
        if not lk:
            continue
        t = re.search(r'title="([^"]+)"', bloco)
        nome = html_lib.unescape(t.group(1)) if t else lk.group(1).rsplit("/", 1)[-1].replace("-", " ")
        avista = re.search(r'R\$\s*([\d.]+,\d{2})[^R]{0,80}vista', bloco)
        precos = [p for p in (preco_br(x) for x in re.findall(r'R\$\s*([\d.]+,\d{2})', bloco))
                  if p >= PRECO_MIN]
        preco = preco_br(avista.group(1)) if avista else (min(precos) if precos else None)
        if preco:
            out.append({"loja": "Terabyte", "nome": nome, "preco": preco, "link": lk.group(1)})
    return out


def enviar_telegram(texto):
    if not (TG_TOKEN and TG_CHAT):
        print("[aviso] Telegram não configurado. Mensagem:\n" + texto)
        return
    requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                  data={"chat_id": TG_CHAT, "text": texto,
                        "disable_web_page_preview": "true"}, timeout=30)


def main():
    if os.getenv("TESTE") == "1":
        enviar_telegram("✅ Monitor de RTX funcionando!")

    try:
        with open(ARQ_VISTOS, encoding="utf-8") as f:
            vistos = json.load(f)
    except (FileNotFoundError, ValueError):
        vistos = {}

    achados = {}
    for termo in TERMOS_BUSCA:
        lojas = [("KaBuM", buscar_kabum), ("Mercado Livre", buscar_mercadolivre),
                 ("Pichau", buscar_pichau), ("Terabyte", buscar_terabyte)]
        for nome_loja, func in lojas:
            try:
                itens = func(termo)
                validos = [i for i in itens if titulo_ok(i["nome"]) and i["preco"] >= PRECO_MIN]
                msg = f"{nome_loja} / {termo}: {len(itens)} itens"
                if validos:
                    barata = min(validos, key=lambda x: x["preco"])
                    msg += f" | mais barata: R$ {barata['preco']:.2f} - {barata['nome'][:60]}"
                print(msg)
            except Exception as e:  # uma loja falhar não derruba as outras
                print(f"[erro] {nome_loja} / {termo}: {e}")
                continue
            for it in itens:
                if titulo_ok(it["nome"]) and PRECO_MIN <= it["preco"] <= PRECO_MAX:
                    achados[it["link"]] = it

    novos = [it for link, it in achados.items()
             if link not in vistos or it["preco"] < vistos[link]]

    for it in sorted(novos, key=lambda x: x["preco"]):
        enviar_telegram(f"🔥 {it['nome']}\n💰 R$ {it['preco']:.2f} — {it['loja']}\n🔗 {it['link']}")
        vistos[it["link"]] = it["preco"]

    print(f"{len(achados)} dentro do preço, {len(novos)} avisos enviados.")
    with open(ARQ_VISTOS, "w", encoding="utf-8") as f:
        json.dump(vistos, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
