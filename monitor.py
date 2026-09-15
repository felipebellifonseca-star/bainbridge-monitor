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
    r"Now|[A-Za-z]{3,9}\s+\d{1,2}(?:,\s*\d{4})?"
    r")",
    re.IGNORECASE,
)

MANUAL_COMMANDS = {"/check", "/status", "/buscar", "/verificar", "/teste"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def local_time_text() -> str:
    return datetime.now(LOCAL_TZ).strftime("%I:%M %p").lstrip("0")


def money(value: int) -> str:
    return f"${value:,.0f}"


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}

    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def fetch_floorplan(plan: str) -> dict:
    url = f"{BASE_URL}/{plan.lower()}/"
    last_error = None

    for attempt in range(3):
        try:
            response = requests.get(url, headers=HEADERS, timeout=25)
            response.raise_for_status()

            text = " ".join(
                BeautifulSoup(
                    response.text,
                    "html.parser"
                ).stripped_strings
            )

            matches = list(
                UNIT_PATTERN.finditer(text)
            )

            if not matches:
                raise RuntimeError(
                    f"Nenhuma unidade foi reconhecida em {plan}. "
                    "O site pode ter mudado de estrutura."
                )

            units = {}

            for match in matches:
                data = match.groupdict()
                number = data["unit"]

                units[f"{plan}-{number}"] = {
                    "plan": plan,
                    "unit": number,
                    "floor": int(data["floor"]),
                    "sqft": int(
                        data["sqft"].replace(",", "")
                    ),
                    "price": int(
                        data["price"].replace(",", "")
                    ),
                    "availability": " ".join(
                        data["availability"].split()
                    ),
                    "url": url,
                }

            return units

        except Exception as exc:
            last_error = exc

            if attempt < 2:
                time.sleep(
                    4 * (attempt + 1)
                )

    raise RuntimeError(
        f"Falha ao consultar {plan}: {last_error}"
    )


def qualifying(units: dict) -> dict:
    return {
        key: value
        for key, value in units.items()
        if value["price"] <= MAX_RENT
    }


def is_priority(unit: dict) -> bool:
    return (
        unit.get("plan") == "B2"
        and int(
            unit.get("floor", 0)
        ) == 5
        and int(
            unit.get(
                "price",
                MAX_RENT + 1
            )
        ) <= MAX_RENT
    )


def unit_line(unit: dict) -> str:
    prefix = (
        "🔥 PRIORIDADE | "
        if is_priority(unit)
        else ""
    )

    return (
        f"{prefix}"
        f"{unit['plan']} "
        f"#{unit['unit']} | "
        f"{unit['floor']}º andar | "
        f"{money(unit['price'])} | "
        f"{unit['availability']}"
    )


def current_summary(units: dict) -> str:
    filtered = qualifying(units)

    if not filtered:
        return (
            f"Nenhuma B1/B2 até "
            f"{money(MAX_RENT)} "
            "neste momento."
        )

    ordered = sorted(
        filtered.values(),
        key=lambda x: (
            x["plan"],
            x["floor"],
            int(x["unit"]),
        ),
    )

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
        f"Unidades até "
        f"{money(MAX_RENT)}: "
        f"B1={b1} | B2={b2}\n"
        + "\n".join(
            unit_line(unit)
            for unit in ordered
        )
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

        if is_priority(new):
            heading = (
                "🔥🔥 PRIORIDADE: "
                "B2 NO 5º ANDAR"
            )

        elif key in old_units:
            heading = (
                "💰 ENTROU NO SEU LIMITE"
            )

        else:
            heading = (
                "🏠 NOVA UNIDADE "
                "NO SEU FILTRO"
            )

        if key in old_units:
            events.append(
                f"{heading}\n"
                f"{unit_line(new)}\n"
                f"Antes: "
                f"{money(old_units[key]['price'])}"
            )

        else:
            events.append(
                f"{heading}\n"
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
                f"{old['plan']} "
                f"#{old['unit']} | "
                f"{old['floor']}º andar\n"
                f"Antes: "
                f"{money(old['price'])} | "
                f"Agora: "
                f"{money(new['price'])}\n"
                f"Disponibilidade atual: "
                f"{new['availability']}"
            )

        else:
            events.append(
                "❌ NÃO APARECE MAIS "
                "COMO DISPONÍVEL\n"
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
            heading = (
                "🔥🔥 PRIORIDADE: "
                "ALTERAÇÃO EM B2 NO 5º ANDAR"
                if is_priority(new)
                else
                "🔄 ALTERAÇÃO"
            )

            events.append(
                f"{heading}\n"
                f"{unit_line(new)}\n"
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


def get_updates(
    token: str,
    offset: int | None = None,
) -> list[dict]:

    params = {
        "timeout": 0
    }

    if offset is not None:
        params["offset"] = offset

    response = requests.get(
        f"https://api.telegram.org/"
        f"bot{token}/getUpdates",
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


def configured_chat_id(
    token: str,
) -> str:

    explicit = os.getenv(
        "TELEGRAM_CHAT_ID",
        "",
    ).strip()

    if explicit:
        return explicit

    for update in reversed(
        get_updates(token)
    ):
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
            return str(
                chat["id"]
            )

    raise RuntimeError(
        "Não encontrei um chat configurado. "
        "Configure TELEGRAM_CHAT_ID "
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
            ],
            [
                {
                    "text": "🏠 Abrir B1",
                    "url": (
                        f"{BASE_URL}/b1/"
                    ),
                },
                {
                    "text": "🏠 Abrir B2",
                    "url": (
                        f"{BASE_URL}/b2/"
                    ),
                },
            ],
        ]
    }


def send_to_chat(
    chat_id: str,
    text: str,
    buttons: bool = True,
) -> None:

    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }

    if buttons:
        payload[
            "reply_markup"
        ] = telegram_keyboard()

    response = requests.post(
        f"https://api.telegram.org/"
        f"bot{telegram_token()}/sendMessage",
        json=payload,
        timeout=20,
    )

    response.raise_for_status()

    data = response.json()

    if not data.get("ok"):
        raise RuntimeError(
            f"Telegram retornou erro: "
            f"{data}"
        )


def send_telegram(
    text: str,
    include_chat_id: bool = False,
) -> str:

    token = telegram_token()

    chat_id = configured_chat_id(
        token
    )

    if include_chat_id:
        text += (
            "\n\n🔐 Chat ID configurado:\n"
            f"{chat_id}"
        )

    send_to_chat(
        chat_id,
        text,
        buttons=True,
    )

    return chat_id


def normalize_command(
    text: str,
) -> str:

    parts = (
        text or ""
    ).strip().lower().split(
        maxsplit=1
    )

    if not parts:
        return ""

    command = parts[0]

    if (
        command.startswith("/")
        and "@" in command
    ):
        command = command.split(
            "@",
            1
        )[0]

    return command


def reply_with_chat_id(
    chat: dict,
) -> None:

    chat_id = str(
        chat.get("id", "")
    )

    chat_type = chat.get(
        "type",
        "",
    )

    title = chat.get(
        "title"
    ) or ""

    if not chat_id:
        return

    if chat_type in {
        "group",
        "supergroup",
    }:
        title_line = (
            f"Grupo: {title}\n"
            if title
            else ""
        )

        text = (
            "🔐 CHAT ID DO GRUPO\n\n"
            f"{title_line}"
            f"Chat ID: {chat_id}\n\n"
            "Agora substitua o secret "
            "TELEGRAM_CHAT_ID no GitHub "
            "por este número."
        )

    else:
        text = (
            "🔐 SEU TELEGRAM CHAT ID\n\n"
            f"Chat ID: {chat_id}"
        )

    send_to_chat(
        chat_id,
        text,
        buttons=False,
    )


def answer_callback(
    token: str,
    callback_id: str,
) -> None:

    try:
        requests.post(
            f"https://api.telegram.org/"
            f"bot{token}/answerCallbackQuery",
            json={
                "callback_query_id":
                    callback_id,
                "text": (
                    "Pedido recebido. "
                    "Vou verificar "
                    "na próxima execução."
                ),
            },
            timeout=10,
        )

    except Exception:
        pass


def poll_telegram_requests(
    state: dict,
) -> tuple[bool, int | None]:

    token = telegram_token()

    expected_chat_id = (
        configured_chat_id(token)
    )

    last_update_id = state.get(
        "telegram_update_id"
    )

    offset = (
        last_update_id + 1
        if isinstance(
            last_update_id,
            int,
        )
        else None
    )

    updates = get_updates(
        token,
        offset=offset,
    )

    manual_requested = False
    newest_id = last_update_id

    for update in updates:

        update_id = update.get(
            "update_id"
        )

        if isinstance(
            update_id,
            int,
        ):
            newest_id = (
                update_id
                if newest_id is None
                else max(
                    newest_id,
                    update_id,
                )
            )

        callback = update.get(
            "callback_query"
        )

        if callback:
            message = (
                callback.get("message")
                or {}
            )

            source_chat_id = str(
                (
                    message.get("chat")
                    or {}
                ).get("id", "")
            )

            if (
                source_chat_id
                == expected_chat_id
                and callback.get("data")
                == "check_now"
            ):
                manual_requested = True

                if callback.get("id"):
                    answer_callback(
                        token,
                        callback["id"],
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

        source_chat_id = str(
            chat.get("id", "")
        )

        command = normalize_command(
            message.get("text", "")
        )

        # /id funciona em privado ou grupo,
        # mesmo antes de o grupo virar
        # o chat oficial do monitor.
        if command == "/id":
            try:
                reply_with_chat_id(
                    chat
                )

            except Exception as exc:
                print(
                    f"Não foi possível "
                    f"responder ao /id: {exc}",
                    file=sys.stderr,
                )

            continue

        # Os demais comandos só funcionam
        # no chat ou grupo configurado.
        if (
            source_chat_id
            == expected_chat_id
            and command
            in MANUAL_COMMANDS
        ):
            manual_requested = True

    return (
        manual_requested,
        newest_id,
    )


def heartbeat_due(
    state: dict,
) -> bool:

    stamp = (
        state.get("last_heartbeat_utc")
        or state.get("last_success_utc")
    )

    if not stamp:
        return True

    try:
        previous = datetime.fromisoformat(
            stamp
        )

        if previous.tzinfo is None:
            previous = previous.replace(
                tzinfo=timezone.utc
            )

        return (
            utc_now() - previous
        ).total_seconds() >= (
            HEARTBEAT_MINUTES * 60
        )

    except Exception:
        return True


def heartbeat_message(
    units: dict,
) -> str:

    filtered = qualifying(
        units
    )

    b1 = sum(
        1
        for unit in filtered.values()
        if unit["plan"] == "B1"
    )

    b2 = sum(
        1
        for unit in filtered.values()
        if unit["plan"] == "B2"
    )

    return (
        "✅ MONITOR BAINBRIDGE ATIVO\n\n"
        f"Horário: "
        f"{local_time_text()}\n"
        "Nenhuma mudança relevante "
        "desde o último alerta.\n"
        f"B1 até {money(MAX_RENT)}: "
        f"{b1} unidade(s)\n"
        f"B2 até {money(MAX_RENT)}: "
        f"{b2} unidade(s)\n"
        "Última consulta: OK"
    )


def save_success_state(
    units: dict,
    previous_state: dict,
    notification_sent: bool = False,
) -> None:

    now = utc_now()

    last_heartbeat = (
        previous_state.get(
            "last_heartbeat_utc"
        )
    )

    if notification_sent:
        last_heartbeat = now.isoformat()

    state = {
        "updated_at_utc":
            now.isoformat(),
        "last_success_utc":
            now.isoformat(),
        "monitor_status":
            "ok",
        "failure_count":
            0,
        "max_rent":
            MAX_RENT,
        "units":
            dict(
                sorted(
                    units.items()
                )
            ),
    }

    if last_heartbeat:
        state[
            "last_heartbeat_utc"
        ] = last_heartbeat

    if (
        previous_state.get(
            "telegram_update_id"
        )
        is not None
    ):
        state[
            "telegram_update_id"
        ] = previous_state[
            "telegram_update_id"
        ]

    write_state(
        state
    )


def record_failure(
    exc: Exception,
) -> None:

    state = load_state()

    error_text = str(
        exc
    )[:500]

    try:
        failure_count = (
            int(
                state.get(
                    "failure_count",
                    0,
                )
            )
            + 1
        )

    except Exception:
        failure_count = 1

    already_alerted = (
        state.get(
            "monitor_status"
        )
        == "error"
    )

    state[
        "failure_count"
    ] = failure_count

    state[
        "last_error_utc"
    ] = utc_now().isoformat()

    state[
        "last_error_message"
    ] = error_text

    state[
        "updated_at_utc"
    ] = utc_now().isoformat()

    if failure_count >= 2:

        state[
            "monitor_status"
        ] = "error"

        if not already_alerted:
            try:
                send_telegram(
                    "⚠️ ERRO NO MONITOR "
                    "BAINBRIDGE\n\n"
                    "A verificação falhou "
                    "em 2 execuções "
                    "consecutivas.\n"
                    f"Horário: "
                    f"{local_time_text()}\n"
                    f"Erro: {error_text}\n\n"
                    "Não vou repetir este "
                    "alerta a cada 5 minutos. "
                    "Avisarei quando o monitor "
                    "voltar ao normal."
                )

            except Exception as telegram_exc:
                print(
                    "Também não foi possível "
                    "enviar o alerta no Telegram: "
                    f"{telegram_exc}",
                    file=sys.stderr,
                )

    else:
        state[
            "monitor_status"
        ] = state.get(
            "monitor_status",
            "ok",
        )

        print(
            "Primeira falha consecutiva. "
            "Vou confirmar na próxima "
            "execução antes de alertar."
        )

    write_state(
        state
    )


def main() -> None:

    if TEST_MODE:
        send_telegram(
            "🧪 TESTE DO MONITOR "
            "BAINBRIDGE: OK\n\n"
            f"Horário: "
            f"{local_time_text()}\n"
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
    ) = poll_telegram_requests(
        old_state
    )

    if (
        newest_update_id is not None
        and newest_update_id
        != old_state.get(
            "telegram_update_id"
        )
    ):
        old_state[
            "telegram_update_id"
        ] = newest_update_id

        write_state(
            old_state
        )

    new_units = {}

    for plan in FLOORPLANS:
        new_units.update(
            fetch_floorplan(
                plan
            )
        )

    if not old_units:

        message = (
            "✅ MONITOR BAINBRIDGE "
            "ATIVADO\n\n"
            "Plantas: B1 e B2\n"
            f"Preço máximo: "
            f"{money(MAX_RENT)}\n"
            "Verificação: aproximadamente "
            "a cada 5 minutos\n"
            "Confirmação de funcionamento: "
            "aproximadamente "
            "a cada 1 hora\n\n"
            + current_summary(
                new_units
            )
        )

        send_telegram(
            message
        )

        save_success_state(
            new_units,
            old_state,
            notification_sent=True,
        )

        print(
            message
        )

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
            "O monitor voltou a acompanhar "
            "normalmente."
        )

        send_telegram(
            recovery
        )

        notification_sent = True

        print(
            recovery
        )

    if manual_request:

        message = (
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
            message
        )

        notification_sent = True

        print(
            message
        )

    if events:

        message = (
            "🚨 BAINBRIDGE THE GRAND\n\n"
            + "\n\n".join(
                events
            )
            + "\n\n"
            + current_summary(
                new_units
            )
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
        or int(
            old_state.get(
                "failure_count",
                0,
            )
            or 0
        ) > 0
    ):

        save_success_state(
            new_units,
            old_state,
            notification_sent=
                notification_sent,
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
