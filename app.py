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

# ========== SPORTS FILTERING ==========

SPORTS_KEYWORDS = [
    'nba', 'nfl', 'nhl', 'mlb', 'ncaab', 'ncaa', 'ufc', 'mma', 'boxing',
    'formula 1', 'f1', 'nascar', 'motogp', 'tennis', 'golf', 'pga',
    'soccer', 'premier league', 'la liga', 'bundesliga', 'serie a', 'champions league',
    'world cup', 'olympics', 'super bowl', 'stanley cup', 'world series',
    'basketball', 'football', 'hockey', 'baseball', 'vs', 'versus', 'wins',
    'champions', 'tournament', 'playoff', 'final', 'grand slam',
    'verstappen', 'hamilton', 'lebron', 'mahomes', 'brady', 'messi', 'ronaldo',
    'lakers', 'celtics', 'warriors', 'chiefs', 'eagles', 'cowboys',
    'manchest', 'arsenal', 'liverpool', 'barcelona', 'real madrid',
    'point', 'score', 'over', 'under', 'spread', 'run', 'goal'
]

POLITICAL_KEYWORDS = [
    'president', 'election', 'vote', 'senate', 'congress', 'parliament',
    'prime minister', 'ipo', 'mars', 'volcano', 'climate', 'temperature',
    'pope', 'oscar', 'academy award', 'fusion', 'nuclear', 'brex', 'ramp',
    'anthropic', 'openai ipo', 'zelensky', 'putin', 'xi jinping',
    'netanyahu', 'musk mars', 'supervolcano', 'earthquake'
]


def is_sports_market(title):
    if not title:
        return False
    title_lower = title.lower()
    for keyword in POLITICAL_KEYWORDS:
        if keyword in title_lower:
            return False
    for keyword in SPORTS_KEYWORDS:
        if keyword in title_lower:
            return True
    if re.search(r'\b\w+\s+(?:vs\.?|versus|@)\s+\w+\b', title_lower):
        return True
    if re.search(r'\w+\s+\w+:\s*\d+', title_lower):
        return True
    return False


def filter_sports_markets(markets, is_kalshi=True):
    sports = []
    for m in markets:
        if is_kalshi:
            title = m.get("title", "") or m.get("ticker", "")
        else:
            title = m.get("question", "") or m.get("slug", "")
        if is_sports_market(title):
            sports.append(m)
    return sports


# ========== HELPER FUNCTIONS ==========

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


def get_kalshi_event_title(event):
    title = event.get("title", "")
    if title and len(title) > 5:
        return title
    ticker = event.get("ticker", "")
    if ticker:
        parts = ticker.split("-")
        return f"{parts[1]} vs {parts[2]}" if len(parts) >= 3 else ticker
    return "Unknown Event"


# ========== FETCH MARKETS ==========

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
                    events = data.get("events", [])
                    sports_events = filter_sports_markets(events, is_kalshi=True)
                    return sports_events
                return []
    except Exception as e:
        print(f"Kalshi error: {e}")
        return []


async def fetch_polymarket():
    try:
        async with aiohttp.ClientSession() as s:
            url = "https://clob.polymarket.com/markets?active=true&limit=100"
            async with s.get(url, timeout=30) as r:
                if r.status == 200:
                    data = await r.json()
                    markets = data if isinstance(data, list) else data.get("data", [])
                    sports_markets = filter_sports_markets(markets, is_kalshi=False)
                    return sports_markets
    except Exception as e:
        print(f"Polymarket error: {e}")
    return []


# ========== KIMI AI MATCHING ==========

async def kimi_batch_match(kalshi_events, polymarket_markets, kimi_key, threshold=0.5):
    if not kimi_key:
        return []

    matches = []
    comparison_list = []

    for k_event in kalshi_events[:20]:
        k_title = get_kalshi_event_title(k_event)
        for p_market in polymarket_markets[:20]:
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
            headers = {
                "Authorization": f"Bearer {kimi_key}",
                "Content-Type": "application/json"
            }

            comparisons_text = "\n".join([
                f"{i+1}. Kalshi: \"{c['kalshi_title']}\" vs Polymarket: \"{c['polymarket_title']}\""
                for i, c in enumerate(comparison_list)
            ])

            prompt = f"""You are an expert sports betting analyst. Match the SAME sports events between two platforms.

{comparisons_text}

Rules:
- SAME = Same game (e.g., "Lakers vs Celtics" = "NBA: Lakers vs Celtics")
- DIFFERENT = Different events
- Be STRICT - only mark SAME if 100% certain

Format: NUMBER: SAME or DIFFERENT (confidence 0-100)"""

            payload = {
                "model": "kimi-k2.5",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.05,
                "max_tokens": 1000
            }

            async with s.post(url, headers=headers, json=payload, timeout=90) as r:
                if r.status == 200:
                    data = await r.json()
                    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")

                    for i, item in enumerate(comparison_list):
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


# ========== BASIC MATCHING ==========

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


def team_overlap_similarity(k_text, p_text):
    k_lower = k_text.lower()
    p_lower = p_text.lower()
    k_teams = re.findall(r'(\w+(?:\s+\w+){0,2})\s+(?:vs\.?|@|versus)', k_lower)
    p_teams = re.findall(r'(\w+(?:\s+\w+){0,2})\s+(?:vs\.?|@|versus)', p_lower)
    if not k_teams or not p_teams:
        return 0.0
    matches = 0
    for kt in k_teams:
        for pt in p_teams:
            if kt in pt or pt in kt or SequenceMatcher(None, kt, pt).ratio() > 0.6:
                matches += 1
                break
    return matches / max(len(k_teams), len(p_teams))


def find_matches_basic(kalshi_events, polymarket_markets, threshold=0.3):
    matches = []
    for k_event in kalshi_events:
        k_title = get_kalshi_event_title(k_event)
        k_norm = normalize_text(k_title)
        for p_market in polymarket_markets:
            p_title = get_polymarket_title(p_market)
            p_norm = normalize_text(p_title)
            text_sim = jaccard_similarity(k_norm, p_norm)
            team_sim = team_overlap_similarity(k_title, p_title)
            similarity = (text_sim * 0.4) + (team_sim * 0.6)
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


# ========== ROUTES ==========

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
    sports_only = data.get("sports_only", True)
    kimi_key = settings_store.get("kimi_api_key", "")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    kalshi_events, polymarket_markets = loop.run_until_complete(
        asyncio.gather(fetch_kalshi_events(), fetch_polymarket())
    )
    loop.close()

    if sports_only:
        kalshi_events = filter_sports_markets(kalshi_events, is_kalshi=True)
        polymarket_markets = filter_sports_markets(polymarket_markets, is_kalshi=False)

    all_matches = []

    if use_kimi and kimi_key:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        kimi_matches = loop.run_until_complete(
            kimi_batch_match(kalshi_events, polymarket_markets, kimi_key, threshold=min_sim)
        )
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
        "matches": all_matches[:200],
        "kimi_used": bool(use_kimi and kimi_key),
        "sports_only": sports_only
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
    kalshi_events, polymarket_markets = loop.run_until_complete(
        asyncio.gather(fetch_kalshi_events(), fetch_polymarket())
    )
    loop.close()

    kalshi_events = filter_sports_markets(kalshi_events, is_kalshi=True)
    polymarket_markets = filter_sports_markets(polymarket_markets, is_kalshi=False)

    if kimi_key:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        matches = loop.run_until_complete(
            kimi_batch_match(kalshi_events, polymarket_markets, kimi_key, threshold=0.5)
        )
        loop.close()
        if not matches:
            matches = find_matches_basic(kalshi_events, polymarket_markets, threshold=0.3)
    else:
        matches = find_matches_basic(kalshi_events, polymarket_markets, threshold=0.3)

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
        total_prob = (1 / k_odds) + (1 / p_odds)

        if total_prob < 1:
            stake_a = budget * (1 / k_odds) / total_prob
            stake_b = budget * (1 / p_odds) / total_prob
            k_return = stake_a * k_odds * (1 - 0.005)
            p_return = stake_b * p_odds * (1 - 0.02)
            total_stake = stake_a + stake_b
            profit = min(k_return, p_return) - total_stake
            profit_pct = (profit / total_stake) * 100 if total_stake > 0 else 0

            if profit_pct >= min_profit:
                opportunities.append({
                    "id": str(hash(match["kalshi_title"])),
                    "event_name": match["kalshi_title"][:60],
                    "similarity": match["similarity"],
                    "profit_percent": round(profit_pct, 2),
                    "profit_amount": round(profit, 2),
                    "total_investment": round(total_stake, 2),
                    "kalshi": {
                        "stake": round(stake_a, 2),
                        "odds": round(k_odds, 2),
                        "price": k_price,
                        "side": "YES"
                    },
                    "polymarket": {
                        "stake": round(stake_b, 2),
                        "odds": round(p_odds, 2),
                        "price": p_price,
                        "side": "NO"
                    }
                })

    opportunities.sort(key=lambda x: x["profit_percent"], reverse=True)

    last_scan.update({
        "opportunities": opportunities,
        "last_scan_time": datetime.now().isoformat(),
        "is_scanning": False
    })

    return jsonify({
        "status": "success",
        "opportunities_found": len(opportunities),
        "budget": budget,
        "data": opportunities
    })


@app.route('/api/opportunities')
def get_opportunities():
    return jsonify(last_scan["opportunities"])


@app.route('/api/debug')
def debug():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    kalshi_events, polymarket_markets = loop.run_until_complete(
        asyncio.gather(fetch_kalshi_events(), fetch_polymarket())
    )
    loop.close()

    kalshi_sports = filter_sports_markets(kalshi_events, is_kalshi=True)
    polymarket_sports = filter_sports_markets(polymarket_markets, is_kalshi=False)

    return jsonify({
        "kalshi_count": len(kalshi_events),
        "kalshi_sports_count": len(kalshi_sports),
        "polymarket_count": len(polymarket_markets),
        "polymarket_sports_count": len(polymarket_sports),
        "kalshi_sample_titles": [get_kalshi_event_title(e)[:80] for e in kalshi_events[:10]],
        "polymarket_sample_titles": [get_polymarket_title(m)[:80] for m in polymarket_markets[:10]],
        "kalshi_sports_sample": [get_kalshi_event_title(e)[:80] for e in kalshi_sports[:10]],
        "polymarket_sports_sample": [get_polymarket_title(m)[:80] for m in polymarket_sports[:10]],
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)