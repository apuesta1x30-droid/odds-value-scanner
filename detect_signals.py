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
MAX_EDGE = 0.20  # NUEVO: edge máximo 20% (por encima es sospechoso)
MIN_MINUTES_BEFORE = 30  # NUEVO: mínimo 30 minutos antes del inicio

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


def no_vig_probs(odds_map):
    """
    Convierte cuotas en probabilidades sin margen.
    odds_map: {"outcome": cuota, ...}
    Devuelve: {"outcome": prob, ...}
    """
    inverse_sum = sum(1.0 / odds for odds in odds_map.values())
    if inverse_sum == 0:
        return {}
    return {name: (1.0 / odds) / inverse_sum for name, odds in odds_map.items()}


def market_margin(odds_list):
    inverse_sum = sum(1.0 / odds for odds in odds_list)
    return inverse_sum - 1.0


def extract_event_data(event):
    """
    Extrae datos usando diccionarios por nombre (no por posición).
    """
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
                no_vig_map = no_vig_probs(odds_map)

                h2h_data[book_name] = {
                    "odds": odds_map,
                    "margin": margin,
                    "no_vig": no_vig_map,
                }

            elif market_key == "totals":
                outcomes = mkt.get("outcomes", [])
                if len(outcomes) < 2:
                    continue

                # Agrupar por línea (point)
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
                    no_vig_map = no_vig_probs(odds_map)

                    if point not in totals_data:
                        totals_data[point] = {}

                    totals_data[point][book_name] = {
                        "odds": odds_map,
                        "margin": margin,
                        "no_vig": no_vig_map,
                    }

    return h2h_data, totals_data


def detect_signals_for_market(books_data, min_books):
    if len(books_data) < min_books:
        return []

    # Agrupar por outcome usando el nombre como clave
    outcome_data = {}  # outcome -> {book: {prob, odd, margin}}

    for book_name, data in books_data.items():
        for outcome, prob in data["no_vig"].items():
            if outcome not in outcome_data:
                outcome_data[outcome] = {}

            outcome_data[outcome][book_name] = {
                "prob": prob,
                "odd": data["odds"][outcome],
                "margin": data["margin"],
            }

    signals = []

    for outcome, book_info in outcome_data.items():
        if len(book_info) < min_books:
            continue

        probs = [info["prob"] for info in book_info.values()]
        consensus_prob = statistics.median(probs)
        dispersion = statistics.pstdev(probs) if len(probs) > 1 else 0.0
        dispersion = max(dispersion, 0.005)

        for book_name, info in book_info.items():
            book_prob = info["prob"]
            odd = info["odd"]
            margin = info["margin"]

            if margin > MAX_MARGIN or odd < MIN_ODDS or odd > MAX_ODDS:
                continue

            edge = consensus_prob - book_prob
            ev = consensus_prob * odd - 1.0
            z_score = edge / dispersion

            # Filtros
            if edge < MIN_EDGE or ev < MIN_EV or z_score < MIN_Z:
                continue
            
            # NUEVO: filtro de sentido común
            if edge > MAX_EDGE:
                continue

            signals.append({
                "book": book_name,
                "outcome": outcome,
                "odd": odd,
                "book_prob": book_prob,
                "consensus_prob": consensus_prob,
                "edge": edge,
                "ev": ev,
                "z_score": z_score,
                "margin": margin,
                "books_count": len(probs),
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
    try:
        dt = datetime.fromisoformat(utc_date_str.replace("Z", "+00:00"))
        madrid_tz = ZoneInfo("Europe/Madrid")
        dt_local = dt.astimezone(madrid_tz)
        return dt_local.strftime("%d/%m/%Y %H:%M")
    except Exception:
        return utc_date_str


def get_edge_icon(edge):
    if edge >= 0.15:
        return "🔥🔥"
    elif edge >= 0.10:
        return "🔥🔥"
    else:
        return "🔥"


def get_ev_icon(ev):
    if ev >= 0.30:
        return "💰💰"
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
    fecha_local = format_date_spanish(s['commence_time'])
    edge_icon = get_edge_icon(s['edge'])
    ev_icon = get_ev_icon(s['ev'])
    z_icon = get_zscore_icon(s['z_score'])
    margin_icon = get_margin_icon(s['margin'])

    if s["market"] == "Totales":
        market_line = f"Mercado: {s['market']}\nLinea: {s['line']}"
        selection = s["outcome"]
    else:
        market_line = f"Mercado: {s['market']}"
        selection = s["outcome"]

    return (
        f"SEÑAL {index}\n"
        f"\n"
        f"Fecha: {fecha_local}\n"
        f"Liga: {s['sport_key']}\n"
        f"Evento: {s['home_team']} vs {s['away_team']}\n"
        f"{market_line}\n"
        f"\n"
        f"Seleccion: {selection}\n"
        f"Casa: {s['book']}\n"
        f"Cuota: {s['odd']:.2f}\n"
        f"\n"
        f"Prob. casa (sin margen): {s['book_prob']:.2%}\n"
        f"Prob. consenso: {s['consensus_prob']:.2%}\n"
        f"\n"
        f"{edge_icon} Edge: {s['edge']:+.2%}\n"
        f"{ev_icon} EV teorico: {s['ev']:+.2%}\n"
        f"{z_icon} Z-score: {s['z_score']:.2f}\n"
        f"{margin_icon} Margen casa: {s['margin']:.2%}\n"
        f"Casas comparadas: {s['books_count']}\n"
        f"\n"
        f"Senal estadistica. No garantiza resultados."
    )


def send_telegram_message(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    try:
        response = requests.post(url, json=payload, timeout=30)
        return response.ok
    except Exception as e:
        print(f"Error enviando a Telegram: {e}")
        return False


def get_current_alive_hour(now_local):
    """Devuelve la hora de estado más reciente que ya pasó hoy, o None."""
    alive_hours = [6, 13, 20]
    current = None
    for h in alive_hours:
        if now_local.hour >= h:
            current = h
    return current


def check_and_send_alive_message(token, chat_id, bot_state):
    """
    Envia el mensaje de estado de la ventana mas reciente (6, 13 o 20),
    una sola vez por ventana, aunque el cron se retrase.
    """
    madrid_tz = ZoneInfo("Europe/Madrid")
    now_local = datetime.now(madrid_tz)

    alive_hour = get_current_alive_hour(now_local)

    if alive_hour is None:
        print("No es hora de enviar mensaje de estado.")
        return bot_state

    # Clave unica para hoy + ventana
    alive_key = f"{now_local.date().isoformat()}-{alive_hour}"

    if bot_state.get("last_alive") == alive_key:
        print("Mensaje de estado ya enviado para esta ventana.")
        return bot_state

    msg = (
        f"✅ Bot activo.\n"
        f"Hora: {now_local.strftime('%d/%m/%Y %H:%M')} (Madrid)\n"
        f"He escaneado todas las ligas de fútbol disponibles "
        f"pero no he localizado cuotas desajustadas de momento.\n"
        f"Seguiré vigilando."
    )

    if send_telegram_message(token, chat_id, msg):
        bot_state["last_alive"] = alive_key
        save_json_file(BOT_STATE_FILE, bot_state)
        print("Mensaje de estado enviado.")
    else:
        print("Error enviando mensaje de estado.")

    return bot_state


def process_telegram_commands(token, chat_id, bot_state):
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

        if from_chat == str(chat_id) and text.startswith("/"):
            clean_cmd = text.split()[0].split('@')[0].lower()
            commands_found.append(clean_cmd)

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

    # 3.5 Comprobar horario de sueño (22:00 - 06:00 Madrid)
    madrid_tz = ZoneInfo("Europe/Madrid")
    now_local = datetime.now(madrid_tz)
    if now_local.hour >= 22 or now_local.hour < 6:
        print(f"Horario de sueño ({now_local.strftime('%H:%M')} Madrid). No se escanea.")
        return

    # 4. Cargar memoria de señales enviadas
    sent_state = load_json_file(STATE_FILE, {})
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=DEDUP_HOURS)

    sent_state = {
        k: v for k, v in sent_state.items()
        if datetime.fromisoformat(v) > cutoff
    }

    all_signals = []
    total_events = 0
    soccer_events = 0

    # Una sola llamada al endpoint "upcoming" (todos los deportes)
    url = "https://api.the-odds-api.com/v4/sports/upcoming/odds"
    params = {
        "apiKey": api_key,
        "regions": "eu",
        "markets": "h2h,totals",
        "oddsFormat": "decimal",
    }
    print("Escaneando todos los deportes (endpoint upcoming)...")
    try:
        response = requests.get(url, params=params, timeout=60)
        if not response.ok:
            print(f"ERROR: {response.status_code}")
            print(response.text)
            save_json_file(STATE_FILE, sent_state)
            return

        events = response.json()
        if not events:
            print("No hay eventos disponibles.")
            check_and_send_alive_message(telegram_token, telegram_chat_id, bot_state)
            save_json_file(STATE_FILE, sent_state)
            return

        total_events = len(events)
        print(f"Total eventos recibidos (todos los deportes): {total_events}")

        # Filtrar solo eventos de fútbol
        for event in events:
            sport_key = event.get("sport_key", "")
            if not sport_key.startswith("soccer_"):
                continue

            # NUEVO: filtrar eventos que empiezan en menos de MIN_MINUTES_BEFORE
            commence_time = event.get("commence_time")
            if commence_time:
                try:
                    commence_dt = datetime.fromisoformat(commence_time.replace("Z", "+00:00"))
                    minutes_until_start = (commence_dt - now).total_seconds() / 60.0
                    if minutes_until_start < MIN_MINUTES_BEFORE:
                        continue  # Saltar eventos que ya han empezado o empiezan pronto
                except Exception:
                    pass

            soccer_events += 1
            event_signals = detect_all_signals(event)
            for s in event_signals:
                s["home_team"] = event.get("home_team", "?")
                s["away_team"] = event.get("away_team", "?")
                s["commence_time"] = event.get("commence_time", "?")
                s["sport_key"] = sport_key
                all_signals.append(s)

        print(f"Eventos de fútbol procesados: {soccer_events}")

    except Exception as e:
        print(f"Error: {e}")
        save_json_file(STATE_FILE, sent_state)
        return

    print(f"Total eventos: {total_events}")

    if not all_signals:
        print("No hay señales.")
        check_and_send_alive_message(telegram_token, telegram_chat_id, bot_state)
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
