import json
import os
import statistics
import sys

import requests


# Umbrales mínimos para considerar una señal como válida
MIN_BOOKS = 4          # Mínimo de casas comparadas
MIN_EDGE = 0.05        # Edge mínimo: 5%
MIN_EV = 0.05          # EV mínimo: 5%
MIN_Z = 2.0            # Z-score mínimo
MAX_MARGIN = 0.10      # Margen máximo de la casa: 10%
MIN_ODDS = 1.30        # Cuota mínima
MAX_ODDS = 4.00        # Cuota máxima


def no_vig_probs(odds_list):
    """
    Convierte cuotas en probabilidades sin margen.
    odds_list: [2.10, 3.40, 3.60]
    Devuelve: [0.454, 0.281, 0.265]
    """
    inverse_sum = sum(1.0 / odds for odds in odds_list)
    if inverse_sum == 0:
        return []
    return [(1.0 / odds) / inverse_sum for odds in odds_list]


def market_margin(odds_list):
    """
    Calcula el margen de la casa.
    Si la suma de probabilidades implícitas es 1.05, margen es 0.05 = 5%.
    """
    inverse_sum = sum(1.0 / odds for odds in odds_list)
    return inverse_sum - 1.0


def extract_event_data(event):
    """
    Extrae por cada casa: cuotas, márgenes y probabilidades sin margen.
    Devuelve un diccionario con la estructura:
        {
            book_name: {
                "odds": {outcome: odd},
                "margin": float,
                "no_vig": {outcome: prob},
            }
        }
    """
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

            no_vig_map = {}
            for name, prob in zip(odds_map.keys(), no_vig):
                no_vig_map[name] = prob

            books_data[book_name] = {
                "odds": odds_map,
                "margin": margin,
                "no_vig": no_vig_map,
            }
            break

    return books_data


def detect_signals(event):
    """
    Detecta cuotas mal colocadas en un evento.
    Devuelve una lista de señales candidatas.
    """
    books_data = extract_event_data(event)

    if len(books_data) < MIN_BOOKS:
        return []

    # Agrupamos por outcome todas las probabilidades sin margen
    # y las cuotas correspondientes.
    outcome_probs = {}   # outcome -> lista de probabilidades
    outcome_odds = {}    # outcome -> {book: odd}
    outcome_margins = {} # outcome -> {book: margin}

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

        # Probabilidad de consenso (mediana)
        consensus_prob = statistics.median(probs)

        # Dispersión (desviación estándar)
        if len(probs) > 1:
            dispersion = statistics.pstdev(probs)
        else:
            dispersion = 0.0

        dispersion = max(dispersion, 0.005)  # Evitar división por cero

        # Comparamos cada casa contra el consenso
        for book_name, book_prob in zip(
            [b for b, _ in sorted(zip(outcome_odds[outcome].keys(), probs))],
            sorted(probs)
        ):
            # Obtenemos cuota y margen de esta casa para esta selección
            odd = outcome_odds[outcome].get(book_name)
            margin = outcome_margins[outcome].get(book_name)

            if odd is None or margin is None:
                continue

            # Filtros básicos
            if margin > MAX_MARGIN:
                continue
            if odd < MIN_ODDS or odd > MAX_ODDS:
                continue

            edge = consensus_prob - book_prob
            ev = consensus_prob * odd - 1.0
            z_score = edge / dispersion

            if edge < MIN_EDGE:
                continue
            if ev < MIN_EV:
                continue
            if z_score < MIN_Z:
                continue

            # Señal válida
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


def main():
    api_key = os.getenv("ODDS_API_KEY")

    if not api_key:
        print("ERROR: Falta la variable ODDS_API_KEY")
        sys.exit(1)

    sport_key = "soccer_epl"

    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds"

    params = {
        "apiKey": api_key,
        "regions": "eu",
        "markets": "h2h",
        "oddsFormat": "decimal",
    }

    print(f"Escaneando cuotas para: {sport_key}")
    print("-" * 60)

    response = requests.get(url, params=params, timeout=30)

    if not response.ok:
        print(f"ERROR en la API. Status: {response.status_code}")
        print("Respuesta:", response.text)
        sys.exit(1)

    events = response.json()

    if not events:
        print("No hay eventos disponibles en este momento.")
        sys.exit(0)

    print(f"Eventos recibidos: {len(events)}")
    print("-" * 60)

    all_signals = []

    for event in events:
        home_team = event.get("home_team", "?")
        away_team = event.get("away_team", "?")
        commence_time = event.get("commence_time", "?")

        event_signals = detect_signals(event)

        for s in event_signals:
            s["home_team"] = home_team
            s["away_team"] = away_team
            s["commence_time"] = commence_time
            all_signals.append(s)

    if not all_signals:
        print("No se detectaron cuotas mal colocadas en este escaneo.")
        return

    # Ordenamos por EV (mayor primero)
    all_signals.sort(key=lambda s: s["ev"], reverse=True)

    print(f"Señales detectadas: {len(all_signals)}")
    print("=" * 60)

    # Mostramos las 3 mejores
    for i, s in enumerate(all_signals[:3], start=1):
        print(f"\n🎯 SEÑAL {i}")
        print(f"Evento:       {s['home_team']} vs {s['away_team']}")
        print(f"Fecha:        {s['commence_time']}")
        print(f"Selección:    {s['outcome']}")
        print(f"Casa:         {s['book']}")
        print(f"Cuota:        {s['odd']:.2f}")
        print("-" * 60)
        print(f"Prob. casa (sin margen): {s['book_prob']:.2%}")
        print(f"Prob. consenso:          {s['consensus_prob']:.2%}")
        print(f"Edge:                    {s['edge']:+.2%}")
        print(f"EV teórico:              {s['ev']:+.2%}")
        print(f"Z-score:                 {s['z_score']:.2f}")
        print(f"Margen casa:             {s['margin']:.2%}")
        print(f"Casas comparadas:        {s['books_count']}")


if __name__ == "__main__":
    main()
