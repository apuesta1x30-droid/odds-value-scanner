import os
import sys

import requests


def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token:
        print("ERROR: Falta la variable TELEGRAM_BOT_TOKEN")
        sys.exit(1)

    if not chat_id:
        print("ERROR: Falta la variable TELEGRAM_CHAT_ID")
        sys.exit(1)

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    payload = {
        "chat_id": chat_id,
        "text": "✅ Fase 1: prueba de Telegram correcta desde GitHub.",
    }

    response = requests.post(url, json=payload, timeout=30)

    if response.ok:
        print("OK: mensaje enviado. Revisa Telegram.")
    else:
        print("ERROR enviando mensaje a Telegram.")
        print("Status code:", response.status_code)
        print("Respuesta:", response.text)
        sys.exit(1)


if __name__ == "__main__":
    main()
