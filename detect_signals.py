import json
import os
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests


# Umbrales mínimos
MIN_BOOKS = 4
MIN_EDGE = 0.05
MIN_EV = 0.05
MIN_Z = 2.0
MAX_MARGIN = 0.10
MIN_ODDS = 1.30
MAX_ODDS = 4.00

# Archivos de memoria
STATE_FILE = Path("sent_signals.json")
BOT_STATE_FILE = Path("bot_state.json")
DEDUP_HOURS = 6


def load_json_file(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json_file(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def no_vig_probs(odds_list):
    inverse_sum = sum(1.0 / odds for odds in odds_list)
    if inverse_sum == 0:
        return []
    return [(1.0 / odds) / inverse_sum for odds in odds_list]


def market_margin(odds_list):
    inverse_sum = sum(1.0 / odds for odds in odds_list)
    return inverse_sum - 1.0


def extract_event_data(event):
    h2h_data = {}
    totals_data = {}

    for bm in event.get("bookmakers", []):
        book_name = bm.get("title", bm.get("key", "Desconocida"))

        for mkt in bm.get("markets", []):
            market_key = mkt.get("key")

            if market_key == "h2h":
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

                h2h_data[book_name] = {"odds": odds_map, "margin": margin, "no_vig": no_vig_map}

            elif market_key == "totals":
                outcomes = mkt.get("outcomes", [])
                if len(outcomes) < 2:
                    continue

                points = set()
                for o in outcomes:
                    if o.get("point") is not None:
                        points.add(float(o["point"]))

                for point in points:
                    odds_map = {}
                    for o in outcomes:
                        if o.get("point") is None or float(o["point"]) != point:
                            continue
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

                    if point not in totals_data:
                        totals_data[point] = {}

                    totals_data[point][book_name] = {"odds": odds_map, "margin": margin, "no_vig": no_vig_map}

    return h2h_data, totals_data


def detect_signals_for_market(books_data, min_books):
    if len(books_data) < min_books:
        return []

    outcome_probs = {}
    outcome_odds = {}
    outcome_margins = {}

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
        if len(probs) < min_books:
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


def detect_all_signals(event):
    h2h_data, totals_data = extract_event_data(event)
    all_signals = []

    h2h_signals = detect_signals_for_market(h2h_data, MIN_BOOKS)
    for s in h2h_signals:
        s["market"] = "Ganador"
        s["line"] = ""
    all_signals.extend(h2h_signals)

    for point, books_data in totals_data.items():
        totals_signals = detect_signals_for_market(books_data, MIN_BOOKS)
        for s in totals_signals:
            s["market"] = "Totales"
            s["line"] = f"{s['outcome']} {point}"
        all_signals.extend(totals_signals)

    return all_signals

def format_date_spanish(utc_date_str):
    """Convierte fecha UTC a hora española."""
    try:
        dt = datetime.fromisoformat(utc_date_str.replace("Z", "+00:00"))
        madrid_tz = ZoneInfo("Europe/Madrid")
        dt_local = dt.astimezone(madrid_tz)
        return dt_local.strftime("%d/%m/%Y %H:%M")
    except Exception:
        return utc_date_str


def get_edge_icon(edge):
    if edge >= 0.15:
        return "🔥🔥🔥"
    elif edge >= 0.10:
        return "🔥🔥"
    else:
        return "🔥"


def get_ev_icon(ev):
    if ev >= 0.30:
        return "💰💰💰"
    elif ev >= 0.15:
        return "💰💰"
    else:
        return "💰"


def get_zscore_icon(z):
    if z >= 3.0:
        return "⭐⭐⭐"
    elif z >= 2.5:
        return "⭐⭐"
    else:
        return "⭐"


def get_margin_icon(margin):
    if margin <= 0.05:
        return "✅"
    elif margin <= 0.08:
        return "⚠️"
    else:
        return "🔴"

def format_signal_message(s, index):
    """
    Formatea una señal como texto para Telegram.
    """
    fecha_local = format_date_spanish(s['commence_time'])
    edge_icon = get_edge_icon(s['edge'])
    ev_icon = get_ev_icon(s['ev'])
    z_icon = get_zscore_icon(s['z_score'])
    margin_icon = get_margin_icon(s['margin'])

    if s["market"] == "Totales":
        market_line = f"📈 Mercado: {s['market']}\n📏 Línea: {s['line']}"
        selection = s["outcome"]
    else:
        market_line = f"📈 Mercado: {s['market']}"
        selection = s["outcome"]

    return f"""🎯 SEÑAL {index}

📅 Fecha: {fecha_local}
⚽ Liga: {s['sport_key']}
🏟️ Evento: {s['home_team']} vs {s['away_team']}
{market_line}

🎯 Selección: {selection}
🏦 Casa: {s['book']}
💶 Cuota: {s['odd']:.2f}

📊 Prob. casa (sin margen): {s['book_prob']:.2%}
📊 Prob. consenso: {s['consensus_prob']:.2%}

{edge_icon} Edge: {s['edge']:+.2%}
{ev_icon} EV teórico: {s['ev']:+.2%}
{z_icon} Z-score: {s['z_score']:.2f}
{margin_icon} Margen casa: {s['margin']:.2%}
👥 Casas comparadas: {s['books_count']}

⚠️ Señal estadística. No garantiza resultados."""

Liga: {s['sport_key']}
Evento: {s['home_team']} vs {s['away_team']}
Fecha: {s['commence_time']}
{market_line}

Selección: {selection}
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


def process_telegram_commands(token, chat_id, bot_state):
    """
    Lee los mensajes recientes de Telegram y procesa comandos.
    """
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    try:
        response = requests.get(url, params={"timeout": 10}, timeout=15)
        if not response.ok:
            return bot_state
        updates = response.json().get("result", [])
    except Exception as e:
        print(f"Error leyendo Telegram: {e}")
        return bot_state

    commands_found = []
    max_update_id = -1

    for update in updates:
        update_id = update.get("update_id", 0)
        if update_id > max_update_id:
            max_update_id = update_id

        message = update.get("message")
        if not message:
            continue

        from_chat = str(message.get("chat", {}).get("id", ""))
        text = message.get("text", "")

        # Solo procesar mensajes de nuestro chat_id que empiecen con /
        if from_chat == str(chat_id) and text.startswith("/"):
            # Limpiar el comando (quitar @nombrebot si lo hay)
            clean_cmd = text.split()[0].split('@')[0].lower()
            commands_found.append(clean_cmd)

    # Ejecutar el último comando encontrado
    if commands_found:
        last_command = commands_found[-1]

        if last_command == "/stop":
            bot_state["paused"] = True
            send_telegram_message(token, chat_id, "🔴 Bot PAUSADO.\nNo se gastarán créditos de la API hasta que envíes /start.")
        elif last_command == "/start":
            bot_state["paused"] = False
            send_telegram_message(token, chat_id, "🟢 Bot REACTIVADO.\nVolveré a escanear cuotas en la próxima ejecución.")
        elif last_command == "/status":
            status_text = "🔴 PAUSADO" if bot_state.get("paused") else "🟢 ACTIVO"
            send_telegram_message(token, chat_id, f"Estado actual del bot: {status_text}")

    # Confirmar a Telegram que hemos leído los mensajes para que no los vuelva a enviar
    if max_update_id > -1:
        try:
            requests.get(url, params={"offset": max_update_id + 1}, timeout=10)
        except Exception:
            pass

    return bot_state


def main():
    api_key = os.getenv("ODDS_API_KEY")
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not api_key or not telegram_token or not telegram_chat_id:
        print("ERROR: Faltan variables de entorno.")
        sys.exit(1)

    # 1. Cargar estado del bot
    bot_state = load_json_file(BOT_STATE_FILE, {"paused": False})

    # 2. Procesar comandos de Telegram
    bot_state = process_telegram_commands(telegram_token, telegram_chat_id, bot_state)
    save_json_file(BOT_STATE_FILE, bot_state)

    # 3. Comprobar si está pausado
    if bot_state.get("paused"):
        print("El bot está pausado por comando de Telegram (/stop).")
        print("No se llamará a The Odds API para no gastar créditos.")
        return

    # 4. Cargar memoria de señales enviadas
    sent_state = load_json_file(STATE_FILE, {})
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=DEDUP_HOURS)

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
        params = {
            "apiKey": api_key,
            "regions": "eu",
            "markets": "h2h,totals",
            "oddsFormat": "decimal",
        }
        print(f"Escaneando: {sport_key}")
        try:
            response = requests.get(url, params=params, timeout=30)
            if not response.ok:
                print(f"  ERROR: {response.status_code}")
                continue
            events = response.json()
            if not events:
                continue
            total_events += len(events)
            for event in events:
                event_signals = detect_all_signals(event)
                for s in event_signals:
                    s["home_team"] = event.get("home_team", "?")
                    s["away_team"] = event.get("away_team", "?")
                    s["commence_time"] = event.get("commence_time", "?")
                    s["sport_key"] = sport_key
                    all_signals.append(s)
        except Exception as e:
            print(f"  Error: {e}")

    print(f"Total eventos: {total_events}")

    if not all_signals:
        print("No hay señales.")
        save_json_file(STATE_FILE, sent_state)
        return

    all_signals.sort(key=lambda s: s["ev"], reverse=True)
    print(f"Señales detectadas: {len(all_signals)}")

    sent_count = 0
    for i, s in enumerate(all_signals[:3], start=1):
        if s["market"] == "Totales":
            signal_id = f"{s['sport_key']}|{s['home_team']}|{s['away_team']}|{s['market']}|{s['line']}|{s['book']}"
        else:
            signal_id = f"{s['sport_key']}|{s['home_team']}|{s['away_team']}|{s['market']}|{s['outcome']}|{s['book']}"

        if signal_id in sent_state:
            print(f"Señal {i} ignorada (ya enviada): {signal_id}")
            continue

        message = format_signal_message(s, i)
        if send_telegram_message(telegram_token, telegram_chat_id, message):
            print(f"Señal {i} enviada a Telegram.")
            sent_state[signal_id] = now.isoformat()
            sent_count += 1
        else:
            print(f"Error enviando señal {i}.")

    print(f"Enviadas {sent_count} señales nuevas.")
    save_json_file(STATE_FILE, sent_state)


if __name__ == "__main__":
    main()
