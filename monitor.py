import json
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from html.parser import HTMLParser

import requests
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

BASE_URL = "https://bainbridgegrand.com/floorplans"
B_PLANS = ("B1", "B2")
A_PLANS = ("A1", "A2", "A3", "A4", "A5", "A6")
ALL_PLANS = B_PLANS + A_PLANS

MAX_RENT = int(os.getenv("MAX_RENT", "2700"))
A_MAX_RENT = int(os.getenv("A_MAX_RENT", "1800"))
STATE_FILE = Path("state.json")
LOCAL_TZ = ZoneInfo("America/New_York")
TEST_MODE = os.getenv("TEST_MODE", "").strip().lower() in {"1", "true", "yes", "on"}
ERROR_FAILURE_THRESHOLD = 2

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

_DRIVER = None


def utc_now():
    return datetime.now(timezone.utc)


def local_time_text():
    return datetime.now(LOCAL_TZ).strftime("%I:%M %p").lstrip("0")


def money(value):
    return f"${int(value):,.0f}"


def clean_error(exc):
    text = str(exc or "").strip()
    if not text:
        return exc.__class__.__name__
    first = text.splitlines()[0].strip() or exc.__class__.__name__
    return first[:350]


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
    normalized = " ".join((text or "").split())
    units = {}

    for match in UNIT_PATTERN.finditer(normalized):
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


def page_is_valid_zero_availability(plan, text):
    """Aceita zero unidades somente quando a página específica diz Contact Us."""
    normalized = " ".join((text or "").split())
    lower = normalized.lower()
    plan_present = re.search(rf"\b{re.escape(plan.lower())}\b", lower) is not None
    disclaimer_present = "floorplans are artist" in lower
    contact_us = "contact us" in lower
    check_availability = "check availability" in lower
    return plan_present and disclaimer_present and contact_us and not check_availability


class _VisibleTextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag.lower() in {"script", "style", "noscript", "svg"}:
            self.hidden_depth += 1

    def handle_endtag(self, tag):
        if tag.lower() in {"script", "style", "noscript", "svg"} and self.hidden_depth:
            self.hidden_depth -= 1

    def handle_data(self, data):
        if self.hidden_depth == 0 and data and data.strip():
            self.parts.append(data.strip())


def html_visible_text(html):
    parser = _VisibleTextParser()
    parser.feed(html or "")
    parser.close()
    return " ".join(parser.parts)


def fetch_floorplan_http(plan):
    """Primeira fonte: HTML direto. É rápido e hoje o Bainbridge já entrega as unidades no HTML."""
    url = f"{BASE_URL}/{plan.lower()}/"
    response = requests.get(
        url,
        params={"_monitor_ts": int(time.time() * 1000)},
        headers=HEADERS,
        timeout=20,
    )
    response.raise_for_status()
    text = html_visible_text(response.text)
    units = parse_floorplan_text(plan, text)

    if units:
        print(
            f"{plan}: HTTP OK ({len(units)} unidades): "
            + ", ".join(sorted(units))
        )
        return units

    if page_is_valid_zero_availability(plan, text):
        print(f"{plan}: HTTP OK, sem unidades disponíveis (Contact Us).")
        return {}

    raise RuntimeError(
        f"HTML de {plan} abriu, mas não trouxe uma disponibilidade validável."
    )


def get_browser():
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
    options.page_load_strategy = "eager"
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
    options.add_argument("--disable-renderer-backgrounding")
    options.add_argument("--disable-background-timer-throttling")
    options.add_argument("--disable-backgrounding-occluded-windows")
    options.add_argument(
        "--disable-features=Translate,BackForwardCache,MediaRouter,"
        "OptimizationHints,AutofillServerCommunication"
    )
    options.add_experimental_option(
        "prefs",
        {
            "profile.managed_default_content_settings.images": 2,
            "profile.default_content_setting_values.notifications": 2,
            "profile.default_content_setting_values.popups": 0,
        },
    )

    if chrome_binary:
        options.binary_location = chrome_binary

    try:
        if chromedriver:
            driver = webdriver.Chrome(service=Service(chromedriver), options=options)
        else:
            driver = webdriver.Chrome(options=options)
    except Exception as exc:
        raise RuntimeError(
            "Não consegui iniciar o Chrome do GitHub Actions. "
            f"Erro: {clean_error(exc)}"
        ) from exc

    driver.set_page_load_timeout(22)
    driver.set_script_timeout(15)

    try:
        driver.execute_cdp_cmd("Network.enable", {})
        driver.execute_cdp_cmd("Network.setCacheDisabled", {"cacheDisabled": True})
        driver.execute_cdp_cmd("Network.clearBrowserCache", {})
        driver.execute_cdp_cmd(
            "Network.setBlockedURLs",
            {
                "urls": [
                    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp", "*.avif",
                    "*.mp4", "*.webm", "*.woff", "*.woff2",
                ]
            },
        )
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


def fetch_floorplan_browser(plan):
    """Fallback: Chrome real, usado apenas se o HTML direto não vier completo."""
    driver = get_browser()
    url = f"{BASE_URL}/{plan.lower()}/?_monitor_ts={int(time.time() * 1000)}"

    try:
        driver.execute_cdp_cmd("Network.clearBrowserCache", {})
    except Exception:
        pass

    try:
        driver.get(url)
    except TimeoutException as exc:
        print(
            f"{plan}: Chrome atingiu timeout; usando o DOM já carregado: {clean_error(exc)}"
        )
        try:
            driver.execute_script("window.stop();")
        except Exception:
            pass

    WebDriverWait(driver, 10).until(
        lambda d: len(d.find_elements(By.TAG_NAME, "body")) > 0
    )

    best_text = ""
    best_units = {}
    previous_signature = None
    stable_rounds = 0
    zero_valid_rounds = 0

    # Não encerramos depois de 2-3 segundos. Algumas unidades aparecem mais tarde.
    # Esperamos pelo menos ~8 s antes de aceitar estabilidade.
    for round_no in range(12):
        try:
            if round_no in {1, 4, 7, 10}:
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            elif round_no in {3, 6, 9}:
                driver.execute_script("window.scrollTo(0, 0);")
        except Exception:
            pass

        current = driver.find_element(By.TAG_NAME, "body").text or ""
        units = parse_floorplan_text(plan, current)

        if len(units) > len(best_units) or (len(units) == len(best_units) and len(current) > len(best_text)):
            best_units = units
            best_text = current

        signature = tuple(sorted(units))
        if units:
            if signature == previous_signature:
                stable_rounds += 1
            else:
                stable_rounds = 0
            previous_signature = signature

            if round_no >= 8 and stable_rounds >= 2:
                print(
                    f"{plan}: Chrome OK ({len(best_units)} unidades): "
                    + ", ".join(sorted(best_units))
                )
                return best_units
        else:
            if page_is_valid_zero_availability(plan, current):
                zero_valid_rounds += 1
                if round_no >= 6 and zero_valid_rounds >= 2:
                    print(f"{plan}: Chrome OK, sem unidades disponíveis (Contact Us).")
                    return {}
            else:
                zero_valid_rounds = 0

        time.sleep(1)

    if best_units:
        print(
            f"{plan}: Chrome OK ao final ({len(best_units)} unidades): "
            + ", ".join(sorted(best_units))
        )
        return best_units

    if page_is_valid_zero_availability(plan, best_text):
        print(f"{plan}: Chrome OK ao final, sem unidades disponíveis.")
        return {}

    raise RuntimeError(
        f"Chrome abriu {plan}, mas a disponibilidade não ficou validável."
    )


def fetch_floorplan(plan):
    """HTTP primeiro; Chrome como fallback. Só falha se as duas fontes falharem."""
    try:
        return fetch_floorplan_http(plan)
    except Exception as http_exc:
        print(
            f"{plan}: HTTP não foi suficiente ({clean_error(http_exc)}). Tentando Chrome...",
            file=sys.stderr,
        )

    try:
        return fetch_floorplan_browser(plan)
    except Exception as browser_exc:
        close_browser()
        raise RuntimeError(
            f"Falha ao consultar {plan} por HTTP e Chrome: {clean_error(browser_exc)}"
        ) from browser_exc


def unit_qualifies(unit):
    plan = unit.get("plan", "")
    price = int(unit.get("price", 10**9))
    if plan in A_PLANS:
        return price < A_MAX_RENT
    if plan in B_PLANS:
        return price <= MAX_RENT
    return False


def qualifying(units):
    return {k: v for k, v in units.items() if unit_qualifies(v)}


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


def current_summary(units, a_available=True):
    q = qualifying(units)
    b_units = sorted(
        [u for u in q.values() if u["plan"] in B_PLANS],
        key=lambda x: (x["plan"], x["floor"], int(x["unit"])),
    )
    a_units = sorted(
        [u for u in q.values() if u["plan"] in A_PLANS],
        key=lambda x: (x["plan"], x["floor"], int(x["unit"])),
    )

    b1 = sum(1 for u in b_units if u["plan"] == "B1")
    b2 = sum(1 for u in b_units if u["plan"] == "B2")

    if b_units:
        b_section = (
            f"🏠 2 QUARTOS | B1/B2 | até {money(MAX_RENT)}\n"
            f"B1={b1} | B2={b2}\n"
            + "\n".join(unit_line(u) for u in b_units)
        )
    else:
        b_section = (
            f"🏠 2 QUARTOS | B1/B2 | até {money(MAX_RENT)}\n"
            "Nenhuma unidade no filtro neste momento."
        )

    if a_units:
        counts = []
        for plan in A_PLANS:
            count = sum(1 for u in a_units if u["plan"] == plan)
            if count:
                counts.append(f"{plan}={count}")
        a_section = (
            f"🛏️ 1 QUARTO | A1-A6 | menos de {money(A_MAX_RENT)}\n"
            + (" | ".join(counts) + "\n" if counts else "")
            + "\n".join(unit_line(u) for u in a_units)
        )
    else:
        a_section = (
            f"🛏️ 1 QUARTO | A1-A6 | menos de {money(A_MAX_RENT)}\n"
            "Nenhuma unidade no filtro neste momento."
        )

    if not a_available:
        a_section += (
            "\n⚠️ Uma ou mais plantas A não puderam ser atualizadas nesta execução; "
            "o monitor preservou o último estado conhecido dessas plantas."
        )

    return b_section + "\n\n" + a_section


def event_group(unit):
    return "A" if unit.get("plan") in A_PLANS else "B"


def detect_changes(old_units, new_units):
    events = {"B": [], "A": []}
    old_q = qualifying(old_units)
    new_q = qualifying(new_units)

    for key in sorted(set(new_q) - set(old_q)):
        new = new_q[key]
        group = event_group(new)
        if is_priority(new):
            heading = "🔥🔥 PRIORIDADE: B2 NO 5º ANDAR"
        elif key in old_units:
            heading = "💰 ENTROU NO SEU LIMITE"
        else:
            heading = "🏠 NOVA UNIDADE NO SEU FILTRO"

        if key in old_units:
            events[group].append(
                f"{heading}\n{unit_line(new)}\nAntes: {money(old_units[key]['price'])}"
            )
        else:
            events[group].append(f"{heading}\n{unit_line(new)}")

    for key in sorted(set(old_q) - set(new_q)):
        old = old_q[key]
        group = event_group(old)
        if key in new_units:
            new = new_units[key]
            limit_text = (
                f"menos de {money(A_MAX_RENT)}"
                if old.get("plan") in A_PLANS
                else f"até {money(MAX_RENT)}"
            )
            events[group].append(
                "⬆️ SAIU DO SEU LIMITE\n"
                f"{old['plan']} #{old['unit']} | {old['floor']}º andar\n"
                f"Antes: {money(old['price'])} | Agora: {money(new['price'])}\n"
                f"Filtro: {limit_text}\n"
                f"Disponibilidade atual: {new['availability']}"
            )
        else:
            events[group].append(
                f"❌ NÃO APARECE MAIS COMO DISPONÍVEL\n{unit_line(old)}"
            )

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
            events[event_group(new)].append(
                f"{heading}\n{unit_line(new)}\n" + "\n".join(changes)
            )

    return events


def has_events(events):
    return bool(events.get("B") or events.get("A"))


def format_event_sections(events):
    sections = []
    if events.get("B"):
        sections.append(
            f"🏠 2 QUARTOS | B1/B2 | até {money(MAX_RENT)}\n\n"
            + "\n\n".join(events["B"])
        )
    if events.get("A"):
        sections.append(
            f"🛏️ 1 QUARTO | A1-A6 | menos de {money(A_MAX_RENT)}\n\n"
            + "\n\n".join(events["A"])
        )
    return "\n\n".join(sections)


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
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
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
        raise RuntimeError("Telegram sendMessage retornou erro.")


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
    """
    Lê atualizações do Telegram.

    Importante: qualquer callback_data='check_now' emitido por ESTE bot é aceito.
    Não descartamos o clique por divergência silenciosa de chat_id. O resultado
    continua sendo enviado somente ao TELEGRAM_CHAT_ID configurado.
    """
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
            if callback.get("data") == "check_now":
                manual_requested = True
                print("Telegram: callback 'check_now' recebido e aceito.")
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
            print(f"Telegram: comando manual '{command}' recebido e aceito.")

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


def legacy_error_was_notified(state):
    if "error_notified" in state:
        return bool(state.get("error_notified"))
    return state.get("monitor_status") == "error"


def save_success_state(units, previous_state):
    now = utc_now().isoformat()
    payload = {
        "updated_at_utc": now,
        "last_success_utc": now,
        "monitor_status": "ok",
        "consecutive_failures": 0,
        "error_notified": False,
        "max_rent": MAX_RENT,
        "a_max_rent": A_MAX_RENT,
        "units": dict(sorted(units.items())),
    }

    for field in (
        "telegram_update_id",
        "manual_pending",
        "a_consecutive_failures",
        "a_error_notified",
        "a_last_error_message",
        "a_last_error_utc",
    ):
        if field in previous_state:
            payload[field] = previous_state[field]

    write_state(payload)


def update_a_health(state, error=None):
    updated = dict(state)

    if error is None:
        was_notified = bool(updated.get("a_error_notified", False))
        had_failures = int(updated.get("a_consecutive_failures", 0) or 0) > 0

        updated["a_consecutive_failures"] = 0
        updated["a_error_notified"] = False
        updated.pop("a_last_error_message", None)
        updated.pop("a_last_error_utc", None)

        if was_notified:
            send_telegram(
                "✅ FILTRO DE 1 QUARTO VOLTOU AO NORMAL\n\n"
                f"Horário: {local_time_text()}\n"
                "A1-A6 voltaram a ser consultados normalmente. "
                "B1/B2 permaneceram ativos durante o problema."
            )
        elif had_failures:
            print("Falha isolada do filtro A1-A6 resolvida silenciosamente.")

        return updated

    count = int(updated.get("a_consecutive_failures", 0) or 0) + 1
    updated["a_consecutive_failures"] = count
    updated["a_last_error_message"] = clean_error(error)
    updated["a_last_error_utc"] = utc_now().isoformat()
    already_notified = bool(updated.get("a_error_notified", False))

    if count >= ERROR_FAILURE_THRESHOLD and not already_notified:
        send_telegram(
            "⚠️ FILTRO DE 1 QUARTO TEMPORARIAMENTE INDISPONÍVEL\n\n"
            f"Horário: {local_time_text()}\n"
            "Uma ou mais plantas A falharam em 2 execuções consecutivas.\n"
            "B1/B2 continuam funcionando normalmente.\n\n"
            "Vou continuar tentando e aviso quando A1-A6 voltarem."
        )
        updated["a_error_notified"] = True
    elif count < ERROR_FAILURE_THRESHOLD:
        print("Falha isolada no filtro A1-A6; nenhum alerta enviado.")

    return updated


def record_failure(exc):
    state = load_state()
    error_text = clean_error(exc)
    legacy_notified = legacy_error_was_notified(state)

    try:
        previous_count = int(state.get("consecutive_failures", 0) or 0)
    except Exception:
        previous_count = 0

    if legacy_notified and previous_count == 0:
        previous_count = ERROR_FAILURE_THRESHOLD

    failure_count = previous_count + 1
    state["consecutive_failures"] = failure_count
    state["last_error_utc"] = utc_now().isoformat()
    state["last_error_message"] = error_text
    state["updated_at_utc"] = utc_now().isoformat()
    already_notified = bool(state.get("error_notified", legacy_notified))

    if failure_count >= ERROR_FAILURE_THRESHOLD:
        state["monitor_status"] = "error"
        if not already_notified:
            try:
                send_telegram(
                    "⚠️ MONITOR BAINBRIDGE COM PROBLEMA\n\n"
                    f"Falha confirmada em {failure_count} execuções consecutivas.\n"
                    f"Horário: {local_time_text()}\n"
                    f"Última leitura bem-sucedida: {state.get('last_success_utc', 'desconhecida')}\n\n"
                    "Vou continuar tentando automaticamente e não repetirei este alerta "
                    "até o monitor voltar ao normal."
                )
                state["error_notified"] = True
            except Exception as telegram_exc:
                print(f"Também não consegui enviar o alerta: {telegram_exc}", file=sys.stderr)
        else:
            state["error_notified"] = True
    else:
        state["monitor_status"] = "degraded"
        state["error_notified"] = False
        print("Falha isolada; vou confirmar na próxima execução antes de alertar.")

    write_state(state)


def fetch_all_a_plans(old_units):
    """Consulta A1-A6 diretamente. Sem página-index intermediária e sem regex de cards."""
    units = {}
    errors = []

    for plan in A_PLANS:
        try:
            units.update(fetch_floorplan(plan))
        except Exception as exc:
            errors.append(f"{plan}: {clean_error(exc)}")
            print(f"{plan}: falha no filtro A: {exc}", file=sys.stderr)
            # Preserva apenas o estado antigo da planta que falhou, evitando falso desaparecimento.
            for key, unit in old_units.items():
                if unit.get("plan") == plan:
                    units[key] = unit

    return units, errors


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
                "Vou consultar B1/B2 e A1-A6 agora. "
                "O resultado chega assim que esta execução terminar."
            )
        except Exception as exc:
            print(f"Não consegui enviar confirmação do botão: {exc}", file=sys.stderr)

    # B1/B2 são o monitor principal. Se uma dessas duas falhar por HTTP e Chrome,
    # a execução é considerada falha e só alertamos após 2 falhas consecutivas.
    new_units = {}
    for plan in B_PLANS:
        new_units.update(fetch_floorplan(plan))

    # A1-A6 têm saúde independente para nunca derrubar B1/B2.
    a_units, a_errors = fetch_all_a_plans(old_units)
    new_units.update(a_units)
    a_available = not a_errors

    if a_errors:
        a_error = RuntimeError("; ".join(a_errors))
        old_state = update_a_health(old_state, error=a_error)
    else:
        old_state = update_a_health(old_state, error=None)

    old_state["updated_at_utc"] = utc_now().isoformat()
    write_state(old_state)

    if not old_units:
        message = (
            "✅ MONITOR BAINBRIDGE ATIVADO\n\n"
            "Filtros:\n"
            f"• B1/B2: até {money(MAX_RENT)}\n"
            f"• A1-A6: menos de {money(A_MAX_RENT)}\n"
            "Verificação: aproximadamente a cada 5 minutos\n\n"
            + current_summary(new_units, a_available=a_available)
        )
        send_telegram(message)
        save_success_state(new_units, old_state)
        print(message)
        return

    was_error_notified = legacy_error_was_notified(old_state)
    had_failed_check = old_state.get("monitor_status") in {"degraded", "error"}
    events = detect_changes(old_units, new_units)

    if was_error_notified:
        recovery = (
            "✅ MONITOR BAINBRIDGE VOLTOU AO NORMAL\n\n"
            f"Horário: {local_time_text()}\n"
            "B1/B2 voltaram a ser consultados normalmente."
        )
        send_telegram(recovery)
        print(recovery)

    if manual_request:
        message = (
            "🔎 CONSULTA MANUAL BAINBRIDGE\n\n"
            f"Horário: {local_time_text()}\n"
            "Consulta concluída.\n\n"
            + current_summary(new_units, a_available=a_available)
        )
        send_telegram(message)
        print(message)
        old_state.pop("manual_pending", None)
        old_state["updated_at_utc"] = utc_now().isoformat()
        write_state(old_state)

    if has_events(events):
        message = (
            "🚨 BAINBRIDGE THE GRAND\n\n"
            + format_event_sections(events)
            + "\n\n📋 SITUAÇÃO ATUAL\n\n"
            + current_summary(new_units, a_available=a_available)
        )
        send_telegram(message)
        print(message)

    if not has_events(events) and not manual_request and not was_error_notified:
        if had_failed_check:
            print("Falha isolada anterior resolvida silenciosamente.")
        else:
            print("Sem mudanças relevantes. Nenhuma notificação enviada.")

    # A saúde do filtro A já foi persistida acima. O estado completo só precisa
    # ser regravado quando o inventário mudou, houve recuperação ou pedido manual.
    if old_units != new_units or had_failed_check or manual_request:
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
