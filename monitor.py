import json
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

BASE_URL = "https://bainbridgegrand.com/floorplans"
FLOORPLANS = ("B1", "B2")
MAX_RENT = int(os.getenv("MAX_RENT", "2700"))
STATE_FILE = Path("state.json")
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
    "Connection": "close",
}

UNIT_PATTERN = re.compile(
    r"#\s*(?P<unit>\d{3,6})\s+"
    r"Floor\s+(?P<floor>\d+)\s+"
    r"(?P<sqft>[\d,]+)\s+sq\.?\s*ft\.?\s+"
    r"Starting\s+at\s+\$(?P<price>[\d,]+)\s+"
    r"Available\s+(?P<availability>Now|[A-Za-z]{3,9}\s+\d{1,2}(?:,\s*\d{4})?)",
    re.IGNORECASE,
)

MANUAL_COMMANDS = {"/check", "/status", "/buscar", "/verificar", "/teste"}


def utc_now():
    return datetime.now(timezone.utc)


def local_time_text():
    return datetime.now(LOCAL_TZ).strftime("%I:%M %p").lstrip("0")


def money(value):
    return f"${value:,.0f}"


def load_state():
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_state(state):
    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def parse_floorplan_text(plan, text):
    """Converte o TEXTO VISÍVEL do navegador em unidades."""
    normalized = " ".join((text or "").split())
    matches = list(UNIT_PATTERN.finditer(normalized))

    units = {}
    for match in matches:
        data = match.groupdict()
        number = data["unit"]
        units[f"{plan}-{number}"] = {
            "plan": plan,
            "unit": number,
            "floor": int(data["floor"]),
            "sqft": int(data["sqft"].replace(",", "")),
            "price": int(data["price"].replace(",", "")),
            "availability": " ".join(data["availability"].split()),
            "url": f"{BASE_URL}/{plan.lower()}/",
        }

    return units


_DRIVER = None


def get_browser():
    """Abre um Chrome real em modo headless e o reutiliza para B1 e B2."""
    global _DRIVER
    if _DRIVER is not None:
        return _DRIVER

    chrome_binary = (
        shutil.which("google-chrome")
        or shutil.which("google-chrome-stable")
        or shutil.which("chromium")
        or shutil.which("chromium-browser")
    )
    chromedriver = shutil.which("chromedriver")

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1440,2200")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-background-networking")
    options.add_argument("--disable-default-apps")
    options.add_argument("--disable-sync")
    options.add_argument("--no-first-run")
    options.add_argument("--incognito")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/151.0.0.0 Safari/537.36"
    )

    if chrome_binary:
        options.binary_location = chrome_binary

    try:
        if chromedriver:
            driver = webdriver.Chrome(
                service=Service(chromedriver),
                options=options,
            )
        else:
            # Selenium Manager tenta resolver o driver compatível.
            driver = webdriver.Chrome(options=options)
    except Exception as exc:
        raise RuntimeError(
            "Não consegui iniciar o Chrome do GitHub Actions. "
            f"Erro: {exc}"
        ) from exc

    driver.set_page_load_timeout(45)
    driver.set_script_timeout(20)

    try:
        driver.execute_cdp_cmd("Network.enable", {})
        driver.execute_cdp_cmd("Network.setCacheDisabled", {"cacheDisabled": True})
        driver.execute_cdp_cmd("Network.clearBrowserCache", {})
    except Exception:
        pass

    _DRIVER = driver
    return driver


def close_browser():
    global _DRIVER
    if _DRIVER is not None:
        try:
            _DRIVER.quit()
        except Exception:
            pass
        _DRIVER = None


def rendered_body_text(driver, url, plan):
    """
    Abre a página como um usuário de verdade, espera JavaScript terminar e
    devolve somente o texto VISÍVEL. Elementos ocultados pelo site não entram.
    """
    separator = "&" if "?" in url else "?"
    fresh_url = f"{url}{separator}_monitor_ts={int(time.time() * 1000)}"

    try:
        driver.execute_cdp_cmd("Network.clearBrowserCache", {})
    except Exception:
        pass

    driver.get(fresh_url)

    WebDriverWait(driver, 20).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )

    # Alguns componentes de disponibilidade carregam depois do evento load.
    # Rolamos a página para disparar widgets lazy-load e esperamos o texto estabilizar.
    try:
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
    except Exception:
        pass
    time.sleep(2)

    previous = None
    stable_rounds = 0
    best_text = ""

    for _ in range(8):
        body = driver.find_element(By.TAG_NAME, "body")
        current = body.text or ""
        if len(current) > len(best_text):
            best_text = current

        if current == previous and len(current) > 500:
            stable_rounds += 1
        else:
            stable_rounds = 0

        if stable_rounds >= 2:
            best_text = current
            break

        previous = current
        time.sleep(1.5)

    if len(best_text) < 500:
        raise RuntimeError(
            f"A página {plan} abriu, mas o conteúdo visível parece incompleto."
        )

    return best_text


def fetch_floorplan(plan):
    """
    Fonte principal: DOM VISÍVEL de um Chrome real, depois do JavaScript.

    Isso é proposital: o HTML cru do Bainbridge pode continuar contendo uma
    unidade antiga mesmo depois de a interface real já ter removido a unidade.
    """
    driver = get_browser()
    slug = plan.lower()
    urls = [
        f"https://bainbridgegrand.com/floorplans/{slug}/",
        f"https://www.bainbridgegrand.com/floorplans/{slug}/",
    ]

    errors = []

    for n, url in enumerate(urls, start=1):
        try:
            print(f"Consultando {plan} no Chrome: tentativa {n}/{len(urls)}")
            text = rendered_body_text(driver, url, plan)
            units = parse_floorplan_text(plan, text)

            # Se houver cards de unidades, usamos somente os cards VISÍVEIS.
            if units:
                print(
                    f"{plan}: Chrome OK ({len(units)} unidades visíveis): "
                    + ", ".join(sorted(units))
                )
                return units

            # Zero unidades pode ser legítimo, mas só aceitamos isso quando a
            # própria página terminou de carregar e mostra o shell do floorplan.
            normalized = " ".join(text.split()).lower()
            loaded_markers = [
                plan.lower(),
                "floorplans are artist",
            ]
            if all(marker in normalized for marker in loaded_markers):
                print(f"{plan}: Chrome OK, nenhuma unidade visível disponível.")
                return {}

            raise RuntimeError(
                f"Chrome abriu {plan}, mas não consegui validar o conteúdo final."
            )

        except Exception as exc:
            errors.append(str(exc))
            print(f"{plan}: Chrome tentativa {n} falhou: {exc}", file=sys.stderr)
            time.sleep(2)

    raise RuntimeError(
        f"Falha ao consultar {plan} com o navegador real após {len(urls)} tentativas. "
        f"Últimos erros: {' | '.join(errors[-2:])}"
    )


def qualifying(units):
    return {k: v for k, v in units.items() if v["price"] <= MAX_RENT}


def is_priority(unit):
    return (
        unit.get("plan") == "B2"
        and int(unit.get("floor", 0)) == 5
        and int(unit.get("price", MAX_RENT + 1)) <= MAX_RENT
    )


def unit_line(unit):
    prefix = "🔥 PRIORIDADE | " if is_priority(unit) else ""
    return (
        f"{prefix}{unit['plan']} #{unit['unit']} | "
        f"{unit['floor']}º andar | {money(unit['price'])} | {unit['availability']}"
    )


def current_summary(units):
    q = qualifying(units)
    if not q:
        return f"Nenhuma B1/B2 até {money(MAX_RENT)} neste momento."

    ordered = sorted(q.values(), key=lambda x: (x["plan"], x["floor"], int(x["unit"])))
    b1 = sum(1 for u in ordered if u["plan"] == "B1")
    b2 = sum(1 for u in ordered if u["plan"] == "B2")
    return (
        f"Unidades até {money(MAX_RENT)}: B1={b1} | B2={b2}\n"
        + "\n".join(unit_line(u) for u in ordered)
    )


def detect_changes(old_units, new_units):
    events = []
    old_q = qualifying(old_units)
    new_q = qualifying(new_units)

    for key in sorted(set(new_q) - set(old_q)):
        new = new_q[key]
        if is_priority(new):
            heading = "🔥🔥 PRIORIDADE: B2 NO 5º ANDAR"
        elif key in old_units:
            heading = "💰 ENTROU NO SEU LIMITE"
        else:
            heading = "🏠 NOVA UNIDADE NO SEU FILTRO"

        if key in old_units:
            events.append(
                f"{heading}\n{unit_line(new)}\nAntes: {money(old_units[key]['price'])}"
            )
        else:
            events.append(f"{heading}\n{unit_line(new)}")

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
            events.append(f"❌ NÃO APARECE MAIS COMO DISPONÍVEL\n{unit_line(old)}")

    for key in sorted(set(old_q) & set(new_q)):
        old, new = old_q[key], new_q[key]
        changes = []
        if old["price"] != new["price"]:
            changes.append(f"Preço: {money(old['price'])} → {money(new['price'])}")
        if old["availability"] != new["availability"]:
            changes.append(f"Disponibilidade: {old['availability']} → {new['availability']}")
        if old["floor"] != new["floor"]:
            changes.append(f"Andar: {old['floor']} → {new['floor']}")
        if old["sqft"] != new["sqft"]:
            changes.append(f"Área: {old['sqft']} → {new['sqft']} sq. ft.")

        if changes:
            heading = (
                "🔥🔥 PRIORIDADE: ALTERAÇÃO EM B2 NO 5º ANDAR"
                if is_priority(new)
                else "🔄 ALTERAÇÃO"
            )
            events.append(f"{heading}\n{unit_line(new)}\n" + "\n".join(changes))

    return events


def telegram_token():
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Secret TELEGRAM_BOT_TOKEN não configurado no GitHub.")
    return token


def get_updates(token, offset=None):
    params = {"timeout": 0}
    if offset is not None:
        params["offset"] = offset
    response = requests.get(
        f"https://api.telegram.org/bot{token}/getUpdates",
        params=params,
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError("Telegram getUpdates retornou erro.")
    return data.get("result", [])


def configured_chat_id(token):
    explicit = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if explicit:
        return explicit

    for update in reversed(get_updates(token)):
        message = update.get("message") or update.get("edited_message")
        if message and (message.get("chat") or {}).get("id") is not None:
            return str(message["chat"]["id"])
    raise RuntimeError("Não encontrei um chat configurado. Configure TELEGRAM_CHAT_ID.")


def telegram_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "🔎 Verificar agora", "callback_data": "check_now"}],
            [
                {"text": "🏠 Abrir B1", "url": f"{BASE_URL}/b1/"},
                {"text": "🏠 Abrir B2", "url": f"{BASE_URL}/b2/"},
            ],
        ]
    }


def send_to_chat(chat_id, text, buttons=True):
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if buttons:
        payload["reply_markup"] = telegram_keyboard()

    response = requests.post(
        f"https://api.telegram.org/bot{telegram_token()}/sendMessage",
        json=payload,
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram retornou erro: {data}")


def send_telegram(text, include_chat_id=False):
    token = telegram_token()
    chat_id = configured_chat_id(token)
    if include_chat_id:
        text += f"\n\n🔐 Chat ID configurado:\n{chat_id}"
    send_to_chat(chat_id, text, buttons=True)
    return chat_id


def normalize_command(text):
    parts = (text or "").strip().lower().split(maxsplit=1)
    if not parts:
        return ""
    command = parts[0]
    if command.startswith("/") and "@" in command:
        command = command.split("@", 1)[0]
    return command


def reply_with_chat_id(chat):
    chat_id = str(chat.get("id", ""))
    if not chat_id:
        return
    if chat.get("type") in {"group", "supergroup"}:
        title = chat.get("title") or ""
        title_line = f"Grupo: {title}\n" if title else ""
        text = (
            "🔐 CHAT ID DO GRUPO\n\n"
            f"{title_line}Chat ID: {chat_id}\n\n"
            "Use este número no secret TELEGRAM_CHAT_ID do GitHub."
        )
    else:
        text = f"🔐 SEU TELEGRAM CHAT ID\n\nChat ID: {chat_id}"
    send_to_chat(chat_id, text, buttons=False)


def answer_callback(token, callback_id):
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/answerCallbackQuery",
            json={
                "callback_query_id": callback_id,
                "text": "Pedido recebido. Vou verificar na próxima execução.",
            },
            timeout=10,
        )
    except Exception:
        pass


def poll_telegram_requests(state):
    token = telegram_token()
    expected_chat_id = configured_chat_id(token)
    last_update_id = state.get("telegram_update_id")
    offset = last_update_id + 1 if isinstance(last_update_id, int) else None
    updates = get_updates(token, offset=offset)

    manual_requested = False
    newest_id = last_update_id

    for update in updates:
        update_id = update.get("update_id")
        if isinstance(update_id, int):
            newest_id = update_id if newest_id is None else max(newest_id, update_id)

        callback = update.get("callback_query")
        if callback:
            message = callback.get("message") or {}
            source_chat_id = str((message.get("chat") or {}).get("id", ""))
            if source_chat_id == expected_chat_id and callback.get("data") == "check_now":
                manual_requested = True
                if callback.get("id"):
                    answer_callback(token, callback["id"])
            continue

        message = update.get("message") or update.get("edited_message")
        if not message:
            continue

        chat = message.get("chat", {})
        source_chat_id = str(chat.get("id", ""))
        command = normalize_command(message.get("text", ""))

        if command == "/id":
            try:
                reply_with_chat_id(chat)
            except Exception as exc:
                print(f"Não foi possível responder ao /id: {exc}", file=sys.stderr)
            continue

        if source_chat_id == expected_chat_id and command in MANUAL_COMMANDS:
            manual_requested = True

    return manual_requested, newest_id


def save_telegram_offset(state, newest_update_id, manual_requested=False):
    updated = dict(state)
    changed = False

    if newest_update_id is not None and newest_update_id != state.get("telegram_update_id"):
        updated["telegram_update_id"] = newest_update_id
        changed = True

    if manual_requested:
        updated["manual_pending"] = True
        changed = True

    if changed:
        updated["updated_at_utc"] = utc_now().isoformat()
        write_state(updated)

    return updated

def save_success_state(units, previous_state):
    now = utc_now().isoformat()
    payload = {
        "updated_at_utc": now,
        "last_success_utc": now,
        "monitor_status": "ok",
        "max_rent": MAX_RENT,
        "units": dict(sorted(units.items())),
    }
    if previous_state.get("telegram_update_id") is not None:
        payload["telegram_update_id"] = previous_state["telegram_update_id"]
    if previous_state.get("manual_pending"):
        payload["manual_pending"] = True
    write_state(payload)


def record_failure(exc):
    state = load_state()
    already_in_error = state.get("monitor_status") == "error"
    error_text = str(exc)[:900]

    # As tentativas já aconteceram dentro de fetch_floorplan().
    # Portanto uma falha final já merece um alerta.
    if not already_in_error:
        try:
            send_telegram(
                "⚠️ ERRO NO MONITOR BAINBRIDGE\n\n"
                "Tentei várias vezes nesta mesma execução e não consegui concluir a consulta.\n"
                f"Horário: {local_time_text()}\n"
                f"Erro: {error_text}\n\n"
                "Não vou repetir este alerta enquanto o problema continuar. "
                "Avisarei quando o monitor voltar ao normal."
            )
        except Exception as telegram_exc:
            print(f"Também não consegui enviar o alerta: {telegram_exc}", file=sys.stderr)

        state["monitor_status"] = "error"
        state["last_error_utc"] = utc_now().isoformat()
        state["last_error_message"] = error_text
        state["updated_at_utc"] = utc_now().isoformat()
        write_state(state)
    else:
        print("Monitor já está em erro; alerta repetido suprimido.")


def main():
    if TEST_MODE:
        send_telegram(
            "🧪 TESTE DO MONITOR BAINBRIDGE: OK\n\n"
            f"Horário: {local_time_text()}\n"
            "Telegram: OK\n"
            "Este teste não consulta o site do Bainbridge.",
            include_chat_id=True,
        )
        print("Teste do Telegram concluído com sucesso.")
        return

    old_state = load_state()
    old_units = old_state.get("units", {})

    new_manual_request, newest_update_id = poll_telegram_requests(old_state)
    old_state = save_telegram_offset(
        old_state,
        newest_update_id,
        manual_requested=new_manual_request,
    )
    manual_request = bool(old_state.get("manual_pending"))

    if new_manual_request:
        try:
            send_telegram(
                "⏳ PEDIDO RECEBIDO\n\n"
                "Vou consultar B1 e B2 agora. "
                "O resultado chega assim que esta execução terminar."
            )
        except Exception as exc:
            print(f"Não consegui enviar confirmação do botão: {exc}", file=sys.stderr)

    new_units = {}
    for plan in FLOORPLANS:
        new_units.update(fetch_floorplan(plan))

    if not old_units:
        message = (
            "✅ MONITOR BAINBRIDGE ATIVADO\n\n"
            "Plantas: B1 e B2\n"
            f"Preço máximo: {money(MAX_RENT)}\n"
            "Verificação: aproximadamente a cada 5 minutos\n\n"
            + current_summary(new_units)
        )
        send_telegram(message)
        save_success_state(new_units, old_state)
        print(message)
        return

    was_in_error = old_state.get("monitor_status") == "error"
    events = detect_changes(old_units, new_units)

    if was_in_error:
        recovery = (
            "✅ MONITOR BAINBRIDGE VOLTOU AO NORMAL\n\n"
            f"Horário: {local_time_text()}\n"
            "A consulta de B1 e B2 voltou a funcionar normalmente."
        )
        send_telegram(recovery)
        print(recovery)

    if manual_request:
        message = (
            "🔎 CONSULTA MANUAL BAINBRIDGE\n\n"
            f"Horário: {local_time_text()}\n"
            "Consulta concluída com sucesso.\n\n"
            + current_summary(new_units)
        )
        send_telegram(message)
        print(message)
        old_state.pop("manual_pending", None)
        old_state["updated_at_utc"] = utc_now().isoformat()
        write_state(old_state)

    if events:
        message = (
            "🚨 BAINBRIDGE THE GRAND\n\n"
            + "\n\n".join(events)
            + "\n\n"
            + current_summary(new_units)
        )
        send_telegram(message)
        print(message)

    if not events and not manual_request and not was_in_error:
        print("Sem mudanças relevantes. Nenhuma notificação enviada.")

    # Sem heartbeat: o Telegram só recebe mudança, consulta manual, erro ou recuperação.
    if old_units != new_units or was_in_error:
        save_success_state(new_units, old_state)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        record_failure(exc)
        raise
    finally:
        close_browser()
