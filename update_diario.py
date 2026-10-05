#!/usr/bin/env python3
"""
Updater for AnalisisDiary (XAUUSD AMT daily analysis).
Fetches fresh data via TradingView MCP server (stdio, same venv Hermes uses)
+ Marketaux news, then regenerates content blocks of index.html in place,
preserving the page structure, and pushes to GitHub Pages (SSH).

Run: /home/hermes/.hermes/tools/tradingview-mcp-venv/bin/python update_diario.py
"""
import asyncio, json, re, subprocess, sys, urllib.request, urllib.parse
from datetime import datetime, timezone
from pathlib import Path

REPO = Path("/home/hermes/.hermes/workspace/AnalisisDiary")
INDEX = REPO / "index.html"
VENV_PY = "/home/hermes/.hermes/tools/tradingview-mcp-venv/bin/python"
ENVIRON_FILE = Path.home() / ".hermes" / "config.yaml"

def get_marketaux_token():
    m = re.search(r"MARKETAUX_API_TOKEN[\"']?\s*:\s*[\"']?([^\"'\n\s]+)", ENVIRON_FILE.read_text())
    return m.group(1) if m else None

# ---------- TradingView MCP over stdio ----------
async def tv_calls(calls, env=None):
    sys.path.insert(0, "/home/hermes/.hermes/tools/tradingview-mcp-venv/lib/python3.11/site-packages")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    srv = StdioServerParameters(command="/home/hermes/.hermes/tools/tradingview-mcp-venv/bin/tradingview-mcp",
                                args=[], env=env or {})
    out = {}
    async with stdio_client(srv) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            for name, args in calls:
                try:
                    res = await s.call_tool(name, args)
                    txt = "".join(getattr(c, "text", "") for c in res.content)
                    out[name] = json.loads(txt) if txt.strip().startswith(("{", "[")) else txt
                except Exception as e:
                    out[name] = {"error": str(e)}
    return out

def marketaux_news(query, token, limit=8):
    if not token:
        return []
    q = urllib.parse.urlencode({"api_token": token, "search": query, "language": "en",
                                "limit": limit, "sort": "published_desc"})
    try:
        with urllib.request.urlopen(f"https://api.marketaux.com/v1/news/all?{q}", timeout=20) as r:
            return json.loads(r.read())["data"]
    except Exception:
        return []

def sentiment_score(items):
    vals = []
    for it in items:
        es = it.get("entities") or []
        for e in es:
            if isinstance(e, dict) and e.get("sentiment_score") is not None:
                vals.append(e["sentiment_score"])
    return (sum(vals) / len(vals)) if vals else 0.0

def news_html(items, max_n=8):
    rows = []
    for it in items[:max_n]:
        title = it.get("title", "").strip()
        url = it.get("url", "#")
        src = urllib.parse.urlparse(url).netloc
        date = (it.get("published_at") or "")[:10]
        desc = re.sub(r"\s+", " ", (it.get("description") or it.get("snippet") or ""))[:220]
        rows.append(
            f'    <div class="news-item">\n'
            f'      <a href="{url}" target="_blank">{title}</a>\n'
            f'      <div class="news-time">{date} — {src}</div>\n'
            f'      <div style="font-size:12px;color:#868993;margin-top:2px">{desc}</div>\n'
            f'    </div>\n')
    return "".join(rows)

def swap_block(html, marker_start, marker_end, new_content):
    """Replace text between two unique markers (markers kept)."""
    i = html.index(marker_start)
    j = html.index(marker_end, i)
    return html[:i] + marker_start + new_content + html[j:]

async def main():
    token = get_marketaux_token()
    env = {"MARKETAUX_API_TOKEN": token} if token else {}
    data = await tv_calls([
        ("combined_analysis", {"symbol": "GC=F", "options": {"include_news": True, "max_news_items": 5}}),
        ("multi_timeframe_analysis", {"symbol": "GC=F"}),
        ("market_snapshot", {}),
        ("bitcoin_market_pulse", {}),
        ("futures_category_snapshot", {"category": "metals"}),
    ], env=env)
    ca = data.get("combined_analysis", {})
    mta = data.get("multi_timeframe_analysis", {})
    snap = data.get("market_snapshot", {})
    pulse = data.get("bitcoin_market_pulse", {})

    # price: try pulse.gold, then spot market_snapshot commodities, then yahoo_price tool data embedded in combined
    price = None
    pct = None
    g = pulse.get("gold") or {}
    if isinstance(g, dict) and g.get("price"):
        price = g["price"]; pct = g.get("change_24h") or g.get("change_percent")
    if price is None:
        for etf in snap.get("etfs", []):
            if etf.get("symbol") == "GLD":
                pct = etf.get("change_pct")
    if price is None:
        ws = pulse.get("weekly_summary") or {}
        # last resort: keep existing DOM price
    sent = ca.get("sentiment", {}) or {}
    sent_lbl = sent.get("sentiment_label", "Neutral")

    gold_news = marketaux_news("gold OR bullion OR \"gold price\" OR XAUUSD", token, 10)
    geo_news = marketaux_news("geopolitics war central bank fed rate", token, 6)
    gs = sentiment_score(gold_news)

    # ---- patch index.html ----
    html = INDEX.read_text()
    today = datetime.now(timezone.utc).strftime("%d %b %Y")
    # header price
    if isinstance(price, (int, float)):
        dir_col = "#f23645" if (pct or -1) < 0 else "#26a69a"
        html = re.sub(
            r'<div class="price">.*?</div>',
            f'<div class="price">{price:,.1f} <span style="font-size:13px;color:{dir_col}">{(pct or 0):+.2f}%</span></div>',
            html, count=1, flags=re.S)
    # header subtitle sentiment label
    html = re.sub(r"(Sentimiento noticias: )[A-Za-z ]+", rf"\g<1>{sent_lbl}", html)
    html = re.sub(r"(News sentiment: )[A-Za-z ]+", rf"\g<1>{sent_lbl}", html)
    # sentiment table score
    if sent:
        sc = sent.get("sentiment_score", gs)
        html = re.sub(r"<td>0\.\d+</td>", f"<td>{sc:.3f}</td>", html, count=1)
        bull = sent.get("bullish_count", 0); bear = sent.get("bearish_count", 0)
        neu = sent.get("neutral_count", 0)
        html = re.sub(r"<td>\d+\s*/\s*\d+\s*/\s*\d+</td>", f"<td>{bull} / {bear} / {neu}</td>", html, count=1)
        cls = "buy" if "Bull" in sent_lbl else ("sell" if "Bear" in sent_lbl else "mixed")
        html = re.sub(r'<td><span class="badge badge-(buy|sell|mixed)">[A-Za-z ]+</span></td>',
                      f'<td><span class="badge badge-{cls}">{sent_lbl}</span></td>',
                      html, count=1)
    # replace gold news card content between its h3 and footer
    frag = news_html(gold_news + geo_news, 10)
    frag = frag if frag else "    <div class=\"news-item\">Sin noticias nuevas.</div>\n"
    html = swap_block(html,
                      '<span class="en">📰 Gold & mining news — past week</span></h3>\n',
                      '<div class="footer">Fuente:',
                      frag + "    ")
    # update timestamp footers
    stamp = datetime.now(timezone.utc).strftime("%d %b %Y ~%H:%M UTC")
    html = re.sub(r"Actualizado [^<]* UTC", f"Actualizado {stamp}", html)
    INDEX.write_text(html)

    # ---- git push ----
    diff = subprocess.run(["git", "diff", "--quiet"], cwd=REPO).returncode
    if diff:
        subprocess.run(["git", "add", "-A"], cwd=REPO, check=True)
        subprocess.run(["git", "commit", "-m", f"auto-update {today}: fresh price/sentiment/news via tradingview-mcp"],
                       cwd=REPO, check=True)
        env = {"GIT_SSH_COMMAND": "ssh -i /home/hermes/.ssh/Gitlab.aena"}
        subprocess.run(["git", "push", "origin", "main"], cwd=REPO, check=True,
                       env={**__import__("os").environ, **env})
        print(f"[update] pushed: price={price}, sentiment={sent_lbl}, news={len(gold_news)}+{len(geo_news)}")
    else:
        print("[update] no changes")

if __name__ == "__main__":
    asyncio.run(main())
