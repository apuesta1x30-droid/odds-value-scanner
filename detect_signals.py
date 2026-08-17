import json
import os
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests


# Umbrales mínimos
MIN_BOOKS = 4
MIN_EDGE = 0.05
MIN_EV = 0.05
MIN_Z = 2.0
MAX_MARGIN = 0.10
MIN_ODDS = 1.30
MAX_ODDS = 4.00

# Archivo de memoria para no repetir señales
STATE_FILE = Path("sent_signals.json")
DEDUP_HOURS = 6  # No repetir la misma señal en 6 horas


def load_sent_signals():
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_sent_signals(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def no_vig_probs(odds_list):
    inverse_sum = sum(1.0 / odds for odds in odds_list)
    if inverse_sum == 0:
        return []
    return [(1.0 / odds) / inverse_sum for odds in odds_list]


def market_margin(odds_list):
    inverse_sum = sum(1.0 / odds for odds in odds_list)
    return inverse_sum - 1.0


def extract_event_data(event):
    books_data = {}
    for bm in event.get("bookmakers", []):
        book_name = bm.get("title", bm.get("key", "Desconocida"))
        for mkt in bm.get("markets", []):
            if mkt.get("key") != "h2h":
                continue
            outcomes = mkt.get("outcomes", [])
            if len(outcomes) < 2:
                continue
            odds_map = {}
            for o in outcomes:
                name = o.get("name")
                price = o.get("price")
                if name and price and float(price) > 1.0:
                    odds_map[name] = float(price)
            if len(odds_map) < 2:
                continue
            odds_list = list(odds_map.values())
            margin = market_margin(odds_list)
            no_vig = no_vig_probs(odds_list)
            no_vig_map = {name: prob for name, prob in zip(odds_map.keys(), no_vig)}
            books_data[book_name] = {"odds": odds_map, "margin": margin, "no_vig": no_vig_map}
            break
    return books_data


def detect_signals(event):
    books_data = extract_event_data(event)
    if len(books_data) < MIN_BOOKS:
        return []
    outcome_probs, outcome_odds, outcome_margins = {}, {}, {}
    for book_name, data in books_data.items():
        for outcome, prob in data["no_vig"].items():
            if outcome not in outcome_probs:
                outcome_probs[outcome] = []
                outcome_odds[outcome] = {}
                outcome_margins[outcome] = {}
            outcome_probs[outcome].append(prob)
            outcome_odds[outcome][book_name] = data["odds"][outcome]
            outcome_margins[outcome][book_name] = data["margin"]
    signals = []
    for outcome, probs in outcome_probs.items():
        if len(probs) < MIN_BOOKS:
            continue
        consensus_prob = statistics.median(probs)
        dispersion = statistics.pstdev(probs) if len(probs) > 1 else 0.0
        dispersion = max(dispersion, 0.005)
        for book_name, book_prob in zip(
            [b for b, _ in sorted(zip(outcome_odds[outcome].keys(), probs))], sorted(probs)
        ):
            odd = outcome_odds[outcome].get(book_name)
            margin = outcome_margins[outcome].get(book_name)
            if odd is None or margin is None:
                continue
            if margin > MAX_MARGIN or odd < MIN_ODDS or odd > MAX_ODDS:
                continue
            edge = consensus_prob - book_prob
            ev = consensus_prob * odd - 1.0
            z_score = edge / dispersion
            if edge < MIN_EDGE or ev < MIN_EV or z_score < MIN_Z:
                continue
            signals.append({
                "book": book_name, "outcome": outcome, "odd": odd,
                "book_prob": book_prob, "consensus_prob": consensus_prob,
                "edge": edge, "ev": ev, "z_score": z_score,
                "margin": margin, "books_count": len(probs),
            })
    return signals


def format_signal_message(s, index):
    return f"""🎯 SEÑAL {index}

Liga: {s['sport_key']}
Evento: {s['home_team']} vs {s['away_team']}
Fecha: {s['commence_time']}

Selección: {s['outcome']}
Casa: {s['book']}
Cuota: {s['odd']:.2f}

Prob. casa (sin margen): {s['book_prob']:.2%}
Prob. consenso: {s['consensus_prob']:.2%}
Edge: {s['edge']:+.2%}
EV teórico: {s['ev']:+.2%}
Z-score: {s['z_score']:.2f}
Margen casa: {s['margin']:.2%}
Casas comparadas: {s['books_count']}

⚠️ Señal estadística. No garantiza resultados."""


def send_telegram_message(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    try:
        response = requests.post(url, json=payload, timeout=30)
        return response.ok
    except Exception as e:
        print(f"Error enviando a Telegram: {e}")
        return False


def main():
    api_key = os.getenv("ODDS_API_KEY")
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not api_key or not telegram_token or not telegram_chat_id:
        print("ERROR: Faltan variables de entorno.")
        sys.exit(1)

    # Cargar memoria
    sent_state = load_sent_signals()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=DEDUP_HOURS)

    # Limpiar memoria antigua (mayor a 6 horas)
    sent_state = {
        k: v for k, v in sent_state.items() 
        if datetime.fromisoformat(v) > cutoff
    }

    sport_keys = [
        "soccer_epl", "soccer_spain_la_liga", "soccer_germany_bundesliga",
        "soccer_france_ligue_one", "soccer_italy_serie_a",
    ]

    all_signals = []
    total_events = 0

    for sport_key in sport_keys:
        url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds"
        params = {"apiKey": api_key, "regions": "eu", "markets": "h2h", "oddsFormat": "decimal"}
        print(f"Escaneando: {sport_key}")
        try:
            response = requests.get(url, params=params, timeout=30)
            if not response.ok:
                continue
            events = response.json()
            if not events:
                continue
            total_events += len(events)
            for event in events:
                event_signals = detect_signals(event)
                for s in event_signals:
                    s["home_team"] = event.get("home_team", "?")
                    s["away_team"] = event.get("away_team", "?")
                    s["commence_time"] = event.get("commence_time", "?")
                    s["sport_key"] = sport_key
                    all_signals.append(s)
        except Exception:
            pass

    print(f"Total eventos: {total_events}")

    if not all_signals:
        print("No hay señales.")
        save_sent_signals(sent_state)
        return

    all_signals.sort(key=lambda s: s["ev"], reverse=True)

    sent_count = 0
    for i, s in enumerate(all_signals[:3], start=1):
        # Crear ID único para esta señal
        signal_id = f"{s['sport_key']}|{s['home_team']}|{s['away_team']}|{s['outcome']}|{s['book']}"
        
        # Comprobar si ya la enviamos en las últimas 6 horas
        if signal_id in sent_state:
            print(f"Señal {i} ignorada (ya enviada recientemente): {signal_id}")
            continue

        message = format_signal_message(s, i)
        if send_telegram_message(telegram_token, telegram_chat_id, message):
            print(f"Señal {i} enviada a Telegram.")
            sent_state[signal_id] = now.isoformat()
            sent_count += 1
        else:
            print(f"Error enviando señal {i}.")

    print(f"Enviadas {sent_count} señales nuevas.")
    save_sent_signals(sent_state)


if __name__ == "__main__":
    main()
