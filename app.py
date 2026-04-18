from flask import Flask, request, jsonify
from flask_cors import CORS
import asyncio
import aiohttp
from datetime import datetime
from difflib import SequenceMatcher
import re
import hmac
import hashlib
import base64
import os

app = Flask(__name__)
CORS(app, resources={
    r"/*": {
        "origins": "*",
        "methods": ["GET", "POST", "OPTIONS", "PUT", "DELETE"],
        "allow_headers": "*",
        "supports_credentials": False
    }
})

settings_store = {
    "kalshi_api_key": "",
    "kalshi_api_secret": "",
    "polymarket_api_key": "",
    "polymarket_api_secret": "",
    "polymarket_wallet": "",
    "kimi_api_key": ""
}

last_scan = {
    "opportunities": [],
    "kalshi_markets": 0,
    "polymarket_markets": 0,
    "last_scan_time": None,
    "is_scanning": False,
    "matches": []
}

def generate_kalshi_signature(key_id, key_secret, timestamp, method, path, body=""):
    message = f"{key_id}{timestamp}{method}{path}{body}"
    signature = hmac.new(
        key_secret.encode('utf-8'),
        message.encode('utf-8'),
        hashlib.sha256
    ).digest()
    return base64.b64encode(signature).decode('utf-8')

def get_polymarket_title(market):
    question = market.get("question", "")
    if question and len(question) > 5:
        return question
    slug = market.get("slug", "")
    if slug:
        return slug.replace("-", " ").replace("_", " ")
    return market.get("market_id", "Unknown Market")

async def fetch_kalshi_events():
    try:
        key_id = settings_store.get("kalshi_api_key", "")
        key_secret = settings_store.get("kalshi_api_secret", "")
        base_url = "https://api.elections.kalshi.com/trade-api/v2"
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        
        if key_id and key_secret:
            timestamp = str(int(datetime.now().timestamp()))
            path = "/trade-api/v2/events?status=open&limit=100"
            signature = generate_kalshi_signature(key_id, key_secret, timestamp, "GET", path)
            headers.update({
                "KALSHI-ACCESS-KEY": key_id,
                "KALSHI-ACCESS-TIMESTAMP": timestamp,
                "KALSHI-ACCESS-SIGNATURE": signature
            })
        
        async with aiohttp.ClientSession() as s:
            url = f"{base_url}/events?status=open&limit=100"
            async with s.get(url, headers=headers, timeout=30) as r:
                if r.status == 200:
                    data = await r.json()
                    return data.get("events", [])
                return []
    except Exception as e:
        print(f"Kalshi error: {e}")
        return []

def get_kalshi_event_title(event):
    title = event.get("title", "")
    if title and len(title) > 5:
        return title
    ticker = event.get("ticker", "")
    if ticker:
        parts = ticker.split("-")
        return f"{parts[1]} vs {parts[2]}" if len(parts) >= 3 else ticker
    return "Unknown Event"

async def fetch_polymarket():
    try:
        async with aiohttp.ClientSession() as s:
            url = "https://clob.polymarket.com/markets?active=true&limit=100"
            async with s.get(url, timeout=30) as r:
                if r.status == 200:
                    data = await r.json()
                    return data if isinstance(data, list) else data.get("data", [])
    except Exception as e:
        print(f"Polymarket error: {e}")
        return []

async def kimi_batch_match(kalshi_events, polymarket_markets, kimi_key, threshold=0.25):
    if not kimi_key:
        return []
    matches = []
    comparison_list = []
    for k_event in kalshi_events[:15]:
        k_title = get_kalshi_event_title(k_event)
        for p_market in polymarket_markets[:15]:
            p_title = get_polymarket_title(p_market)
            comparison_list.append({
                "kalshi_title": k_title,
                "polymarket_title": p_title,
                "kalshi_event": k_event,
                "polymarket_market": p_market
            })
    if not comparison_list:
        return []
    try:
        async with aiohttp.ClientSession() as s:
            url = "https://api.moonshot.cn/v1/chat/completions"
            headers = {"Authorization": f"Bearer {kimi_key}", "Content-Type": "application/json"}
            comparisons_text = "\n".join([f"{i+1}. Kalshi: \"{c['kalshi_title']}\" vs Polymarket: \"{c['polymarket_title']}\"" for i, c in enumerate(comparison_list[:15])])
            prompt = f"""You are an expert at matching prediction markets. Compare each pair and identify which are the SAME event.

{comparisons_text}

For each pair, respond in this exact format:
NUMBER: SAME or DIFFERENT (confidence 0-100)

Example:
1: SAME (95)
2: DIFFERENT (10)

Only mark as SAME if you're confident they're the same event."""
            payload = {
                "model": "kimi-k2.5",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": 500
            }
            async with s.post(url, headers=headers, json=payload, timeout=60) as r:
                if r.status == 200:
                    data = await r.json()
                    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                    for i, item in enumerate(comparison_list[:15]):
                        pattern = rf"{i+1}[\.:\s]+(SAME|DIFFERENT)[\s\(]*(\d+)"
                        match = re.search(pattern, content, re.IGNORECASE)
                        if match:
                            is_same = match.group(1).upper() == "SAME"
                            confidence = int(match.group(2))
                            if is_same and confidence >= threshold * 100:
                                matches.append({
                                    "kalshi_title": item["kalshi_title"],
                                    "polymarket_title": item["polymarket_title"],
                                    "similarity": round(confidence / 100, 2),
                                    "kalshi_event": item["kalshi_event"],
                                    "polymarket_market": item["polymarket_market"],
                                    "matched_by": "kimi"
                                })
    except Exception as e:
        print(f"Kimi error: {e}")
    return matches

def normalize_text(text):
    if not text:
        return ""
    text = text.lower()
    text = re.sub(r'[-_/:]', ' ', text)
    text = re.sub(r'[^\w\s]', ' ', text)
    text = ' '.join(text.split())
    stop_words = {
        'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for',
        'of', 'with', 'by', 'will', 'be', 'is', 'are', 'as', 'this', 'that',
        'market', 'event', 'yes', 'no', 'vs', 'versus', 'win', 'wins', 'by',
        'over', 'under', 'points', 'scored', 'have', 'has', 'had',
        '2023', '2024', '2025', '2026', 'nba', 'nhl', 'nfl', 'ncaab', 'mlb'
    }
    return ' '.join([w for w in text.split() if w not in stop_words and len(w) > 2])

def jaccard_similarity(s1, s2):
    set1, set2 = set(s1.split()), set(s2.split())
    if not set1 or not set2:
        return 0.0
    return len(set1 & set2) / len(set1 | set2)

def combined_similarity(k_text, p_text):
    k_norm = normalize_text(k_text)
    p_norm = normalize_text(p_text)
    if not k_norm or not p_norm:
        return 0.0
    return jaccard_similarity(k_norm, p_norm)

def find_matches_basic(kalshi_events, polymarket_markets, threshold=0.15):
    matches = []
    for k_event in kalshi_events:
        k_title = get_kalshi_event_title(k_event)
        for p_market in polymarket_markets:
            p_title = get_polymarket_title(p_market)
            similarity = combined_similarity(k_title, p_title)
            if similarity >= threshold:
                matches.append({
                    "kalshi_title": k_title,
                    "polymarket_title": p_title,
                    "similarity": round(similarity, 3),
                    "kalshi_event": k_event,
                    "polymarket_market": p_market,
                    "matched_by": "basic"
                })
    matches.sort(key=lambda x: x["similarity"], reverse=True)
    return matches

@app.route('/api/status')
def status():
    return jsonify({
        "status": "running",
        "is_scanning": last_scan["is_scanning"],
        "opportunities_found": len(last_scan["opportunities"]),
        "kalshi_markets": last_scan["kalshi_markets"],
        "polymarket_markets": last_scan["polymarket_markets"]
    })

@app.route('/api/settings', methods=['GET', 'POST'])
def settings():
    if request.method == 'POST':
        data = request.get_json()
        settings_store.update(data)
        return jsonify({"status": "success"})
    return jsonify({"is_configured": bool(settings_store["kalshi_api_key"] and settings_store["polymarket_api_key"])})

@app.route('/api/trades')
def trades():
    return jsonify([])

@app.route('/api/trades/pending')
def pending_trades():
    return jsonify([])

@app.route('/api/matches', methods=['POST'])
def get_matches():
    data = request.get_json() or {}
    use_kimi = data.get("use_kimi", True)
    min_sim = data.get("min_similarity", 0.25)
    kimi_key = settings_store.get("kimi_api_key", "")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    kalshi_events, polymarket_markets = loop.run_until_complete(asyncio.gather(fetch_kalshi_events(), fetch_polymarket()))
    loop.close()

    all_matches = []
    if use_kimi and kimi_key:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        kimi_matches = loop.run_until_complete(kimi_batch_match(kalshi_events, polymarket_markets, kimi_key, threshold=min_sim))
        loop.close()
        all_matches.extend(kimi_matches)

    basic_matches = find_matches_basic(kalshi_events, polymarket_markets, threshold=min_sim)
    existing_pairs = {(m["kalshi_title"], m["polymarket_title"]) for m in all_matches}
    for bm in basic_matches:
        if (bm["kalshi_title"], bm["polymarket_title"]) not in existing_pairs:
            all_matches.append(bm)

    all_matches.sort(key=lambda x: x["similarity"], reverse=True)

    last_scan.update({
        "matches": all_matches,
        "kalshi_markets": len(kalshi_events),
        "polymarket_markets": len(polymarket_markets)
    })

    return jsonify({
        "status": "success",
        "matches_found": len(all_matches),
        "kalshi_count": len(kalshi_events),
        "polymarket_count": len(polymarket_markets),
        "matches": all_matches[:50],
        "kimi_used": bool(use_kimi and kimi_key)
    })

@app.route('/api/scan', methods=['POST'])
def scan():
    data = request.get_json() or {}
    budget = data.get("budget", 100)
    min_profit = data.get("min_profit_percent", 0.5)
    last_scan["is_scanning"] = True

    kimi_key = settings_store.get("kimi_api_key", "")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    kalshi_events, polymarket_markets = loop.run_until_complete(asyncio.gather(fetch_kalshi_events(), fetch_polymarket()))
    loop.close()

    if kimi_key:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        matches = loop.run_until_complete(kimi_batch_match(kalshi_events, polymarket_markets, kimi_key, threshold=0.25))
        loop.close()
        if not matches:
            matches = find_matches_basic(kalshi_events, polymarket_markets, threshold=0.15)
    else:
        matches = find_matches_basic(kalshi_events, polymarket_markets, threshold=0.15)

    opportunities = []
    for match in matches[:30]:
        k_event = match["kalshi_event"]
        p_market = match["polymarket_market"]
        k_markets = k_event.get("markets", [])
        k_price = k_markets[0].get("yes_ask", 50) / 100 if k_markets else 0.5
        p_prices = p_market.get("outcomePrices", [0.5])
        p_price = float(p_prices[0]) if isinstance(p_prices, list) and p_prices else 0.5
        if k_price <= 0 or p_price <= 0:
            continue
        k_odds = 1 / k_price
        p_odds = 1 / p_price
        total_prob =