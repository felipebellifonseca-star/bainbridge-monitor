import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://bainbridgegrand.com/floorplans"
FLOORPLANS = ("B1", "B2")
MAX_RENT = int(os.getenv("MAX_RENT", "2700"))
STATE_FILE = Path("state.json")
KEEPALIVE_DAYS = 30
HEARTBEAT_MINUTES = 60
LOCAL_TZ = ZoneInfo("America/New_York")
TEST_MODE = os.getenv("TEST_MODE", "").strip().lower() in {"1", "true", "yes", "on"}

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

MANUAL_COMMANDS = {
    "/check",
    "/status",
    "/buscar",
    "/verificar",
    "/teste",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def local_time_text() -> str:
    return datetime.now(LOCAL_TZ).strftime("%I:%M %p").lstrip("0")


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


def write_state(payload: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def save_success_state(
    units: dict,
    previous_state: dict | None = None,
    notification_sent: bool = False,
) -> None:
    now = utc_now()
    previous_state = previous_state or {}

    last_heartbeat = previous_state.get("last_heartbeat_utc")

    if notification_sent:
        last_heartbeat = now.isoformat()

    payload = {
        "updated_at_utc": now.isoformat(),
        "last_success_utc": now.isoformat(),
        "monitor_status": "ok",
        "max_rent": MAX_RENT,
        "units": dict(sorted(units.items())),
    }

    if last_heartbeat:
        payload["last_heartbeat_utc"] = last_heartbeat

    if previous_state.get("telegram_update_id") is not None:
        payload["telegram_update_id"] = previous_state["telegram_update_id"]

    write_state(payload)


def record_failure(exc: Exception) -> None:
    state = load_state()
    already_in_error = state.get("monitor_status") == "error"
    error_text = str(exc)[:500]

    if not already_in_error:
        try:
            send_telegram(
                "⚠️ ERRO NO MONITOR BAINBRIDGE\n\n"
                "Não consegui concluir a verificação automática.\n"
                f"Horário: {local_time_text()}\n"
                f"Erro: {error_text}\n\n"
                "Não vou repetir este alerta a cada 5 minutos. "
                "Avisarei quando o monitor voltar ao normal."
            )

        except Exception as telegram_exc:
            print(
                f"Também não foi possível enviar o alerta no Telegram: {telegram_exc}",
                file=sys.stderr,
            )

    payload = dict(state)
    payload["monitor_status"] = "error"
    payload["last_error_utc"] = utc_now().isoformat()
    payload["last_error_message"] = error_text
    payload["updated_at_utc"] = utc_now().isoformat()

    write_state(payload)


def keepalive_due(state: dict) -> bool:
    stamp = state.get("updated_at_utc")

    if not stamp:
        return True

    try:
        previous = datetime.fromisoformat(stamp)

        if previous.tzinfo is None:
            previous = previous.replace(tzinfo=timezone.utc)

        return (utc_now() - previous).days >= KEEPALIVE_DAYS

    except Exception:
        return True


def heartbeat_due(state: dict) -> bool:
    stamp = state.get("last_heartbeat_utc") or state.get("updated_at_utc")

    if not stamp:
        return True

    try:
        previous = datetime.fromisoformat(stamp)

        if previous.tzinfo is None:
            previous = previous.replace(tzinfo=timezone.utc)

        return (
            utc_now() - previous
        ).total_seconds() >= HEARTBEAT_MINUTES * 60

    except Exception:
        return True


def qualifying(units: dict) -> dict:
    return {
        key: value
        for key, value in units.items()
        if value["price"] <= MAX_RENT
    }


def qualifying_counts(units: dict) -> tuple[int, int]:
    q = qualifying(units)

    b1 = sum(
        1
        for unit in q.values()
        if unit["plan"] == "B1"
    )

    b2 = sum(
        1
        for unit in q.values()
        if unit["plan"] == "B2"
    )

    return b1, b2


def money(value: int) -> str:
    return f"${value:,.0f}"


def unit_line(unit: dict) -> str:
    return (
        f"{unit['plan']} #{unit['unit']} | "
        f"{unit['floor']}º andar | "
        f"{money(unit['price'])} | "
        f"{unit['availability']}"
    )


def current_summary(units: dict) -> str:
    q = qualifying(units)

    if not q:
        return (
            f"Nenhuma B1/B2 até "
            f"{money(MAX_RENT)} neste momento."
        )

    ordered = sorted(
        q.values(),
        key=lambda x: (
            x["plan"],
            x["floor"],
            int(x["unit"]),
        ),
    )

    lines = [
        unit_line(unit)
        for unit in ordered
    ]

    b1 = sum(
        1
        for unit in ordered
        if unit["plan"] == "B1"
    )

    b2 = sum(
        1
        for unit in ordered
        if unit["plan"] == "B2"
    )

    return (
        f"Unidades até {money(MAX_RENT)}: "
        f"B1={b1} | B2={b2}\n"
        + "\n".join(lines)
    )


def detect_changes(
    old_units: dict,
    new_units: dict,
) -> list[str]:

    events = []

    old_q = qualifying(old_units)
    new_q = qualifying(new_units)

    for key in sorted(
        set(new_q) - set(old_q)
    ):
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

    for key in sorted(
        set(old_q) - set(new_q)
    ):
        old = old_q[key]

        if key in new_units:
            new = new_units[key]

            events.append(
                "⬆️ SAIU DO SEU LIMITE\n"
                f"{old['plan']} #{old['unit']} | "
                f"{old['floor']}º andar\n"
                f"Antes: {money(old['price'])} | "
                f"Agora: {money(new['price'])}\n"
                f"Disponibilidade atual: "
                f"{new['availability']}"
            )

        else:
            events.append(
                "❌ NÃO APARECE MAIS COMO DISPONÍVEL\n"
                f"{unit_line(old)}"
            )

    for key in sorted(
        set(old_q) & set(new_q)
    ):
        old = old_q[key]
        new = new_q[key]

        changes = []

        if old["price"] != new["price"]:
            changes.append(
                f"Preço: "
                f"{money(old['price'])} → "
                f"{money(new['price'])}"
            )

        if (
            old["availability"]
            != new["availability"]
        ):
            changes.append(
                f"Disponibilidade: "
                f"{old['availability']} → "
                f"{new['availability']}"
            )

        if old["floor"] != new["floor"]:
            changes.append(
                f"Andar: "
                f"{old['floor']} → "
                f"{new['floor']}"
            )

        if old["sqft"] != new["sqft"]:
            changes.append(
                f"Área: "
                f"{old['sqft']} → "
                f"{new['sqft']} sq. ft."
            )

        if changes:
            events.append(
                "🔄 ALTERAÇÃO\n"
                f"{new['plan']} "
                f"#{new['unit']}\n"
                + "\n".join(changes)
            )

    return events


def telegram_token() -> str:
    token = os.getenv(
        "TELEGRAM_BOT_TOKEN",
        "",
    ).strip()

    if not token:
        raise RuntimeError(
            "Secret TELEGRAM_BOT_TOKEN "
            "não configurado no GitHub."
        )

    return token


def get_telegram_updates(
    token: str,
    offset: int | None = None,
) -> list[dict]:

    params = {
        "timeout": 0,
    }

    if offset is not None:
        params["offset"] = offset

    response = requests.get(
        f"https://api.telegram.org/bot"
        f"{token}/getUpdates",
        params=params,
        timeout=20,
    )

    response.raise_for_status()

    data = response.json()

    if not data.get("ok"):
        raise RuntimeError(
            "Telegram getUpdates retornou erro."
        )

    return data.get("result", [])


def telegram_chat_id(
    token: str,
) -> str:

    explicit = os.getenv(
        "TELEGRAM_CHAT_ID",
        "",
    ).strip()

    if explicit:
        return explicit

    updates = get_telegram_updates(token)

    for update in reversed(updates):
        message = (
            update.get("message")
            or update.get("edited_message")
        )

        if not message:
            continue

        chat = message.get(
            "chat",
            {},
        )

        if (
            chat.get("id") is not None
            and chat.get("type") == "private"
        ):
            return str(chat["id"])

    raise RuntimeError(
        "Não encontrei seu chat no Telegram. "
        "Envie uma mensagem para o bot "
        "ou configure TELEGRAM_CHAT_ID "
        "nos Secrets do GitHub."
    )


def telegram_keyboard() -> dict:
    return {
        "inline_keyboard": [
            [
                {
                    "text": "🔎 Verificar agora",
                    "callback_data": "check_now",
                }
            ]
        ]
    }


def send_telegram(
    text: str,
    include_chat_id: bool = False,
) -> str:

    token = telegram_token()

    chat_id = telegram_chat_id(
        token
    )

    if include_chat_id:
        text += (
            "\n\n🔐 Seu Telegram Chat ID é:\n"
            f"{chat_id}\n\n"
            "Salve esse número no GitHub "
            "como secret TELEGRAM_CHAT_ID."
        )

    response = requests.post(
        f"https://api.telegram.org/bot"
        f"{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
            "reply_markup": telegram_keyboard(),
        },
        timeout=20,
    )

    response.raise_for_status()

    payload = response.json()

    if not payload.get("ok"):
        raise RuntimeError(
            f"Telegram retornou erro: "
            f"{payload}"
        )

    return chat_id


def answer_callback(
    token: str,
    callback_id: str,
) -> None:

    try:
        requests.post(
            f"https://api.telegram.org/bot"
            f"{token}/answerCallbackQuery",
            json={
                "callback_query_id": callback_id,
                "text": (
                    "Pedido recebido. "
                    "Vou verificar na próxima execução."
                ),
            },
            timeout=10,
        )

    except Exception:
        pass


def poll_manual_request(
    state: dict,
) -> tuple[bool, int | None]:

    token = telegram_token()

    expected_chat_id = telegram_chat_id(
        token
    )

    last_update_id = state.get(
        "telegram_update_id"
    )

    offset = (
        last_update_id + 1
        if isinstance(last_update_id, int)
        else None
    )

    updates = get_telegram_updates(
        token,
        offset=offset,
    )

    requested = False

    newest_id = last_update_id

    for update in updates:

        update_id = update.get(
            "update_id"
        )

        if isinstance(
            update_id,
            int,
        ):
            if newest_id is None:
                newest_id = update_id
            else:
                newest_id = max(
                    newest_id,
                    update_id,
                )

        callback = update.get(
            "callback_query"
        )

        if callback:

            callback_message = (
                callback.get("message")
                or {}
            )

            chat = callback_message.get(
                "chat",
                {},
            )

            chat_id = str(
                chat.get("id", "")
            )

            if (
                chat.get("type") == "private"
                and chat_id == expected_chat_id
                and callback.get("data")
                == "check_now"
            ):
                requested = True

                callback_id = callback.get(
                    "id"
                )

                if callback_id:
                    answer_callback(
                        token,
                        callback_id,
                    )

            continue

        message = (
            update.get("message")
            or update.get("edited_message")
        )

        if not message:
            continue

        chat = message.get(
            "chat",
            {},
        )

        chat_id = str(
            chat.get("id", "")
        )

        if chat.get("type") != "private":
            continue

        if chat_id != expected_chat_id:
            continue

        text = (
            message.get("text")
            or ""
        ).strip().lower()

        if text in MANUAL_COMMANDS:
            requested = True

    return (
        requested,
        newest_id,
    )


def heartbeat_message(
    units: dict,
) -> str:

    b1, b2 = qualifying_counts(
        units
    )

    return (
        "✅ MONITOR BAINBRIDGE ATIVO\n\n"
        f"Horário: {local_time_text()}\n"
        "Nenhuma mudança relevante "
        "desde o último alerta.\n"
        f"B1 até {money(MAX_RENT)}: "
        f"{b1} unidade(s)\n"
        f"B2 até {money(MAX_RENT)}: "
        f"{b2} unidade(s)\n"
        "Última consulta: OK"
    )


def main() -> None:

    if TEST_MODE:
        send_telegram(
            "🧪 TESTE DO MONITOR BAINBRIDGE: OK\n\n"
            f"Horário: {local_time_text()}\n"
            "Telegram: OK\n"
            "Este teste não depende "
            "do site do Bainbridge.",
            include_chat_id=True,
        )

        print(
            "Teste do Telegram "
            "concluído com sucesso."
        )

        return

    old_state = load_state()

    old_units = old_state.get(
        "units",
        {},
    )

    (
        manual_request,
        newest_update_id,
    ) = poll_manual_request(
        old_state
    )

    new_units = {}

    for plan in FLOORPLANS:
        new_units.update(
            fetch_floorplan(plan)
        )

    if newest_update_id is not None:
        old_state[
            "telegram_update_id"
        ] = newest_update_id

    if not old_units:

        message = (
            "✅ MONITOR BAINBRIDGE ATIVADO\n\n"
            "Plantas: B1 e B2\n"
            f"Preço máximo: "
            f"{money(MAX_RENT)}\n"
            "Verificação: "
            "aproximadamente "
            "a cada 5 minutos\n"
            "Confirmação de funcionamento: "
            "aproximadamente "
            "a cada 1 hora\n\n"
            + current_summary(new_units)
            + "\n\n"
            + f"B1: {BASE_URL}/b1/\n"
            + f"B2: {BASE_URL}/b2/"
        )

        send_telegram(message)

        save_success_state(
            new_units,
            old_state,
            notification_sent=True,
        )

        print(message)

        return

    was_in_error = (
        old_state.get(
            "monitor_status"
        )
        == "error"
    )

    events = detect_changes(
        old_units,
        new_units,
    )

    notification_sent = False

    if was_in_error:

        recovery = (
            "✅ MONITOR BAINBRIDGE "
            "VOLTOU AO NORMAL\n\n"
            f"Horário: "
            f"{local_time_text()}\n"
            "A consulta de B1 e B2 "
            "foi concluída com sucesso "
            "novamente.\n"
            "O monitor voltou "
            "a acompanhar normalmente."
        )

        send_telegram(
            recovery
        )

        notification_sent = True

        print(
            recovery
        )

    if manual_request:

        manual_message = (
            "🔎 CONSULTA MANUAL "
            "BAINBRIDGE\n\n"
            f"Horário: "
            f"{local_time_text()}\n"
            "Consulta concluída "
            "com sucesso.\n\n"
            + current_summary(
                new_units
            )
        )

        send_telegram(
            manual_message
        )

        notification_sent = True

        print(
            manual_message
        )

    if events:

        message = (
            "🚨 BAINBRIDGE THE GRAND\n\n"
            + "\n\n".join(events)
            + "\n\n"
            + current_summary(
                new_units
            )
            + "\n\n"
            + f"B1: {BASE_URL}/b1/\n"
            + f"B2: {BASE_URL}/b2/"
        )

        send_telegram(
            message
        )

        notification_sent = True

        print(
            message
        )

    elif (
        not manual_request
        and not was_in_error
        and heartbeat_due(
            old_state
        )
    ):

        message = heartbeat_message(
            new_units
        )

        send_telegram(
            message
        )

        notification_sent = True

        print(
            message
        )

    elif (
        not manual_request
        and not was_in_error
    ):

        print(
            "Sem mudanças relevantes. "
            "Heartbeat ainda não venceu."
        )

    if (
        old_units != new_units
        or notification_sent
        or was_in_error
        or newest_update_id is not None
        or keepalive_due(
            old_state
        )
    ):

        save_success_state(
            new_units,
            old_state,
            notification_sent=notification_sent,
        )


if __name__ == "__main__":

    try:
        main()

    except Exception as exc:

        print(
            f"ERRO: {exc}",
            file=sys.stderr,
        )

        record_failure(
            exc
        )

        raise
