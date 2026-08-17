import json
import os
import sys

import requests


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
    print(f"URL: {url}")
    print("-" * 60)

    response = requests.get(url, params=params, timeout=30)

    print(f"Status code: {response.status_code}")

    if not response.ok:
        print("ERROR en la respuesta de la API.")
        print("Respuesta:", response.text)
        sys.exit(1)

    data = response.json()

    print(f"Eventos recibidos: {len(data)}")
    print("-" * 60)

    print(json.dumps(data, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
