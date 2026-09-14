import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://bainbridgegrand.com/floorplans"
FLOORPLANS = ("B1", "B2")
MAX_RENT = int(os.getenv("MAX_RENT", "2700"))
STATE_FILE = Path("state.json")
KEEPALIVE_DAYS = 30

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/151.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

UNIT_PATTERN = re.compile(
    r"#\s*(?P<unit>\d{3,6})\s+"
    r"Floor\s+(?P<floor>\d+)\s+"
    r"(?P<sqft>[\d,]+)\s+sq\.?\s*ft\.?\s+"
    r"Starting\s+at\s+\$(?P<price>[\d,]+)\s+"
    r"Available\s+(?P<availability>"
    r"Now|"
    r"[A-Za-z]{3,9}\s+\d{1,2}(?:,\s*\d{4})?"
    r")",
    re.IGNORECASE,
)


def fetch_floorplan(plan: str) -> dict:
    url = f"{BASE_URL}/{plan.lower()}/"
    last_error = None

    for attempt in range(3):
        try:
            response = requests.get(url, headers=HEADERS, timeout=25)
            response.raise_for_status()

            soup = BeautifulSoup(response.text, "html.parser")
            text = " ".join(soup.stripped_strings)

            matches = list(UNIT_PATTERN.finditer(text))
            if not matches:
                raise RuntimeError(
                    f"Nenhuma unidade foi reconhecida em {plan}. "
                    "O site pode ter mudado de estrutura."
                )

            units = {}
            for match in matches:
                data = match.groupdict()
                unit_number = data["unit"]
                key = f"{plan}-{unit_number}"
                units[key] = {
                    "plan": plan,
                    "unit": unit_number,
                    "floor": int(data["floor"]),
                    "sqft": int(data["sqft"].replace(",", "")),
                    "price": int(data["price"].replace(",", "")),
                    "availability": " ".join(data["availability"].split()),
                    "url": url,
                }

            return units

        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(4 * (attempt + 1))

    raise RuntimeError(f"Falha ao consultar {plan}: {last_error}")


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(units: dict) -> None:
    payload = {
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        "max_rent": MAX_RENT,
        "units": dict(sorted(units.items())),
    }
    STATE_FILE.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def keepalive_due(state: dict) -> bool:
    stamp = state.get("updated_at_utc")
    if not stamp:
        return True
    try:
        previous = datetime.fromisoformat(stamp)
        if previous.tzinfo is None:
            previous = previous.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - previous
        return age.days >= KEEPALIVE_DAYS
    except Exception:
        return True


def qualifying(units: dict) -> dict:
    return {key: value for key, value in units.items() if value["price"] <= MAX_RENT}


def money(value: int) -> str:
    return f"${value:,.0f}"


def unit_line(unit: dict) -> str:
    return (
        f"{unit['plan']} #{unit['unit']} | "
        f"{unit['floor']}º andar | {money(unit['price'])} | "
        f"{unit['availability']}"
    )


def current_summary(units: dict) -> str:
    q = qualifying(units)
    if not q:
        return f"Nenhuma B1/B2 até {money(MAX_RENT)} neste momento."

    ordered = sorted(
        q.values(),
        key=lambda x: (x["plan"], x["floor"], int(x["unit"])),
    )
    lines = [unit_line(unit) for unit in ordered]
    b1 = sum(1 for unit in ordered if unit["plan"] == "B1")
    b2 = sum(1 for unit in ordered if unit["plan"] == "B2")
    return (
        f"Unidades até {money(MAX_RENT)}: B1={b1} | B2={b2}\n"
        + "\n".join(lines)
    )


def detect_changes(old_units: dict, new_units: dict) -> list[str]:
    events = []

    old_q = qualifying(old_units)
    new_q = qualifying(new_units)

    # Entrou no filtro: unidade nova ou preço caiu para dentro do limite.
    for key in sorted(set(new_q) - set(old_q)):
        new = new_q[key]
        if key in old_units:
            old = old_units[key]
            events.append(
                "💰 ENTROU NO SEU LIMITE\n"
                f"{unit_line(new)}\n"
                f"Antes: {money(old['price'])}"
            )
        else:
            events.append(
                "🏠 NOVA UNIDADE NO SEU FILTRO\n"
                f"{unit_line(new)}"
            )

    # Saiu do filtro: ficou acima do limite ou desapareceu do site.
    for key in sorted(set(old_q) - set(new_q)):
        old = old_q[key]
        if key in new_units:
            new = new_units[key]
            events.append(
                "⬆️ SAIU DO SEU LIMITE\n"
                f"{old['plan']} #{old['unit']} | {old['floor']}º andar\n"
                f"Antes: {money(old['price'])} | Agora: {money(new['price'])}\n"
                f"Disponibilidade atual: {new['availability']}"
            )
        else:
            events.append(
                "❌ NÃO APARECE MAIS COMO DISPONÍVEL\n"
                f"{unit_line(old)}"
            )

    # Alterações em unidades que continuam dentro do filtro.
    for key in sorted(set(old_q) & set(new_q)):
        old = old_q[key]
        new = new_q[key]
        changes = []

        if old["price"] != new["price"]:
            changes.append(f"Preço: {money(old['price'])} → {money(new['price'])}")
        if old["availability"] != new["availability"]:
            changes.append(
                f"Disponibilidade: {old['availability']} → {new['availability']}"
            )
        if old["floor"] != new["floor"]:
            changes.append(f"Andar: {old['floor']} → {new['floor']}")
        if old["sqft"] != new["sqft"]:
            changes.append(f"Área: {old['sqft']} → {new['sqft']} sq. ft.")

        if changes:
            events.append(
                "🔄 ALTERAÇÃO\n"
                f"{new['plan']} #{new['unit']}\n"
                + "\n".join(changes)
            )

    return events


def telegram_chat_id(token: str) -> str:
    explicit = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if explicit:
        return explicit

    # Modo simples: se TELEGRAM_CHAT_ID não estiver configurado,
    # pega o chat privado mais recente que mandou mensagem para o bot.
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    response = requests.get(url, timeout=20)
    response.raise_for_status()
    data = response.json()

    if not data.get("ok"):
        raise RuntimeError("Telegram getUpdates retornou erro.")

    for update in reversed(data.get("result", [])):
        message = update.get("message") or update.get("edited_message")
        if not message:
            continue
        chat = message.get("chat", {})
        if chat.get("id") is not None and chat.get("type") == "private":
            return str(chat["id"])

    raise RuntimeError(
        "Não encontrei seu chat no Telegram. Abra o bot, toque em Start "
        "e envie uma mensagem (por exemplo: /start), depois rode novamente."
    )


def send_telegram(text: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "Secret TELEGRAM_BOT_TOKEN não configurado no GitHub."
        )

    chat_id = telegram_chat_id(token)
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    response = requests.post(
        url,
        json={
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        },
        timeout=20,
    )
    response.raise_for_status()

    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram retornou erro: {payload}")


def main() -> None:
    old_state = load_state()
    old_units = old_state.get("units", {})

    new_units = {}
    for plan in FLOORPLANS:
        new_units.update(fetch_floorplan(plan))

    # Segurança: só substitui o estado depois que B1 e B2 foram lidos com sucesso.
    if not old_units:
        message = (
            "✅ MONITOR BAINBRIDGE ATIVADO\n\n"
            "Plantas: B1 e B2\n"
            f"Preço máximo: {money(MAX_RENT)}\n"
            "Verificação: a cada 5 minutos\n\n"
            + current_summary(new_units)
            + "\n\n"
            + f"B1: {BASE_URL}/b1/\n"
            + f"B2: {BASE_URL}/b2/"
        )
        send_telegram(message)
        save_state(new_units)
        print(message)
        return

    events = detect_changes(old_units, new_units)

    if events:
        message = (
            "🚨 BAINBRIDGE THE GRAND\n\n"
            + "\n\n".join(events)
            + "\n\n"
            + current_summary(new_units)
            + "\n\n"
            + f"B1: {BASE_URL}/b1/\n"
            + f"B2: {BASE_URL}/b2/"
        )
        send_telegram(message)
        print(message)
    else:
        print("Sem mudanças relevantes.")

    # Guarda também unidades acima do limite, para perceber quando caírem para <= MAX_RENT.
    # Só altera o arquivo quando os dados mudarem ou a cada 30 dias como keepalive do repo.
    if old_units != new_units or keepalive_due(old_state):
        save_state(new_units)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        raise
