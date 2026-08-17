import json
import os
import sys

import requests


def calculate_no_vig_probs(odds_list):
    """
    odds_list: lista de cuotas, por ejemplo [2.10, 3.40, 3.60]
    Devuelve: lista de probabilidades sin margen, por ejemplo [0.454, 0.281, 0.265]
    """
    inverse_sum = sum(1.0 / odds for odds in odds_list)
    no_vig_probs = [(1.0 / odds) / inverse_sum for odds in odds_list]
    return no_vig_probs


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

    print(f"Consultando cuotas para: {sport_key}")
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

    # Tomamos el primer evento disponible.
    event = events[0]

    home_team = event.get("home_team", "?")
    away_team = event.get("away_team", "?")
    commence_time = event.get("commence_time", "?")

    print(f"Evento seleccionado:")
    print(f"  {home_team} vs {away_team}")
    print(f"  Fecha: {commence_time}")
    print("-" * 60)

    # Buscamos la primera casa de apuestas que tenga mercado h2h.
    bookmaker = None
    market = None

    for bm in event.get("bookmakers", []):
        for mkt in bm.get("markets", []):
            if mkt.get("key") == "h2h":
                bookmaker = bm
                market = mkt
                break
        if bookmaker:
            break

    if not bookmaker or not market:
        print("No se encontró ninguna casa con mercado h2h para este evento.")
        sys.exit(0)

    bookmaker_name = bookmaker.get("title", bookmaker.get("key", "Desconocida"))

    print(f"Casa de apuestas: {bookmaker_name}")
    print("-" * 60)

    # Extraemos las cuotas.
    outcomes = market.get("outcomes", [])

    if len(outcomes) < 2:
        print("No hay suficientes resultados en el mercado.")
        sys.exit(0)

    names = [o.get("name", "?") for o in outcomes]
    odds = [o.get("price") for o in outcomes]

    print("Cuotas originales:")
    for name, odd in zip(names, odds):
        print(f"  {name}: {odd}")

    print("-" * 60)

    # Calculamos probabilidades implícitas.
    implied_probs = [1.0 / odd for odd in odds]
    implied_sum = sum(implied_probs)

    print("Probabilidades implícitas (con margen):")
    for name, prob in zip(names, implied_probs):
        print(f"  {name}: {prob:.4f}  ({prob * 100:.2f}%)")

    print("-" * 60)
    print(f"Suma de probabilidades implícitas: {implied_sum:.4f}  ({implied_sum * 100:.2f}%)")
    print(f"Margen de la casa: {(implied_sum - 1.0) * 100:.2f}%")
    print("-" * 60)

    # Calculamos probabilidades sin margen.
    no_vig_probs = calculate_no_vig_probs(odds)

    print("Probabilidades sin margen (probabilidades reales):")
    for name, prob in zip(names, no_vig_probs):
        print(f"  {name}: {prob:.4f}  ({prob * 100:.2f}%)")

    print("-" * 60)
    print(f"Suma de probabilidades sin margen: {sum(no_vig_probs):.4f}")


if __name__ == "__main__":
    main()
