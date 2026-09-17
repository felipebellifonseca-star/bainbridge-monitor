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

BASE_URL = "https://bainbridgegrand.com/floorplans"
B_PLANS = ("B1", "B2")
A_PLANS = ("A1", "A2", "A3", "A4", "A5", "A6")
MAX_RENT = int(os.getenv("MAX_RENT", "2700"))
A_MAX_RENT = int(os.getenv("A_MAX_RENT", "1800"))
STATE_FILE = Path("state.json")
LOCAL_TZ = ZoneInfo("America/New_York")
TEST_MODE = os.getenv("TEST_MODE", "").strip().lower() in {"1", "true", "yes", "on"}

ERROR_FAILURE_THRESHOLD = 2
PLAN_ATTEMPTS = 2
RENDER_MIN_SECONDS = 9
RENDER_MAX_SECONDS = 18
STABLE_READS = 3
POLL_SECONDS = 1

UNIT_PATTERN = re.compile(
    r"#\s*(?P<unit>\d{3,6})\s+"
    r"Floor\s*:?[ \t]*(?P<floor>\d+)\s+"
    r"(?P<sqft>[\d,]+)\s*sq\.?\s*ft\.?\s+"
    r"Starting\s+at\s+\$(?P<price>[\d,]+)(?:\.\d{2})?\s+"
    r"Available\s+(?:from\s+)?"
    r"(?P<availability>Now|[A-Za-z]{3,9}\s+\d{1,2}(?:,\s*\d{4})?|\d{1,2}/\d{1,2}/\d{2,4})",
    re.IGNORECASE,
)

PLAN_SUMMARY_PATTERN = re.compile(
    r"\b(?P<plan>[A-Z]\d+)\b\s+"
    r"(?P<beds>\d+)\s*bed\s+"
    r"(?P<baths>\d+)\s*bath\s+"
    r"(?P<sqft>[\d,]+)\s*sq\.?\s*ft\.?\s+"
    r"(?:Only\s+\d+\s+left!\s+)?"
    r"(?P<status>Contact\s+Us|Starting\s+at\s+\$[\d,]+(?:\.\d{2})?)",
    re.IGNORECASE,
)

MANUAL_COMMANDS = {"/check", "/status", "/buscar", "/verificar", "/teste"}


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def local_time_text():
    return datetime.now(LOCAL_TZ).strftime("%I:%M %p").lstrip("0")


def money(value):
    return f"${int(value):,.0f}"


def clean_error(exc):
    text = str(exc or "").strip()
    if not text:
        return exc.__class__.__name__
    return (text.splitlines()[0].strip() or exc.__class__.__name__)[:350]


def load_state():
    if not STATE_FILE.exists():
        return {}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def save_state_if_changed(state, previous_state):
    """Evita um commit no GitHub a cada 5 minutos quando nada mudou."""
    if state == previous_state:
        return False
    state["updated_at_utc"] = utc_now_iso()
    save_state(state)
    return True


def parse_floorplan_text(plan, text):
    """Extrai unidades do texto VISÍVEL de uma página específica de planta."""
    normalized = " ".join((text or "").split())
    units = {}

    for match in UNIT_PATTERN.finditer(normalized):
        data = match.groupdict()
        unit_number = data["unit"]
        units[f"{plan}-{unit_number}"] = {
            "plan": plan,
            "unit": unit_number,
            "floor": int(data["floor"]),
            "sqft": int(data["sqft"].replace(",", "")),
            "price": int(data["price"].replace(",", "")),
            "availability": " ".join(data["availability"].split()),
            "url": f"{BASE_URL}/{plan.lower()}/",
        }

    return units


def plan_summary_status(plan, text):
    """Retorna 'contact', 'available' ou None usando o cabeçalho da própria planta."""
    normalized = " ".join((text or "").split())
    for match in PLAN_SUMMARY_PATTERN.finditer(normalized):
        if match.group("plan").upper() != plan:
            continue
        status = match.group("status").lower()
        return "contact" if "contact" in status else "available"
    return None


def units_signature(units):
    """Assinatura completa. Preço/data também precisam estabilizar, não só os IDs."""
    return tuple(
        (
            key,
            unit["floor"],
            unit["sqft"],
            unit["price"],
            unit["availability"],
        )
        for key, unit in sorted(units.items())
    )


class BrowserScraper:
    """
    Uma única fonte autoritativa: o DOM VISÍVEL depois do JavaScript.

    Não usamos o HTML bruto como fonte de disponibilidade porque ele já provou
    ficar defasado em relação ao que o usuário vê no site.
    """

    def __init__(self):
        self.driver = None

    def _new_driver(self):
        chrome_binary = (
            shutil.which("google-chrome")
            or shutil.which("google-chrome-stable")
            or shutil.which("chromium")
            or shutil.which("chromium-browser")
        )
        chromedriver = shutil.which("chromedriver")

        options = Options()
        # 'none' evita travar esperando imagens/analytics/evento load. Nós mesmos
        # decidimos quando o DOM de disponibilidade ficou estável.
        options.page_load_strategy = "none"
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1440,2200")
        options.add_argument("--disable-extensions")
        options.add_argument("--no-first-run")
        options.add_argument("--disable-sync")
        options.add_argument("--incognito")
        options.add_experimental_option(
            "prefs",
            {
                "profile.managed_default_content_settings.images": 2,
                "profile.default_content_setting_values.notifications": 2,
            },
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
                driver = webdriver.Chrome(options=options)
        except Exception as exc:
            raise RuntimeError(
                f"Não consegui iniciar o Chrome: {clean_error(exc)}"
            ) from exc

        driver.set_script_timeout(10)

        try:
            driver.execute_cdp_cmd("Network.enable", {})
            driver.execute_cdp_cmd("Network.setCacheDisabled", {"cacheDisabled": True})
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

        return driver

    def get_driver(self):
        if self.driver is None:
            self.driver = self._new_driver()
        return self.driver

    def close(self):
        if self.driver is not None:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None

    def fetch_plan(self, plan):
        """Tenta a planta até duas vezes, reiniciando o Chrome entre tentativas."""
        errors = []

        for attempt in range(1, PLAN_ATTEMPTS + 1):
            try:
                units = self._fetch_plan_once(plan)
                if units:
                    print(
                        f"{plan}: OK ({len(units)} unidades visíveis): "
                        + ", ".join(sorted(units))
                    )
                else:
                    print(f"{plan}: OK, sem unidades disponíveis (Contact Us).")
                return units
            except Exception as exc:
                errors.append(clean_error(exc))
                print(
                    f"{plan}: tentativa {attempt}/{PLAN_ATTEMPTS} falhou: {exc}",
                    file=sys.stderr,
                )
                self.close()
                if attempt < PLAN_ATTEMPTS:
                    time.sleep(1)

        raise RuntimeError(
            f"Falha ao consultar {plan} após {PLAN_ATTEMPTS} tentativas: "
            + " | ".join(errors[-2:])
        )

    def _fetch_plan_once(self, plan):
        driver = self.get_driver()
        url = f"{BASE_URL}/{plan.lower()}/?_monitor_ts={int(time.time() * 1000)}"

        try:
            driver.execute_cdp_cmd("Network.clearBrowserCache", {})
        except Exception:
            pass

        driver.get(url)
        started = time.monotonic()
        last_signature = None
        stable_reads = 0
        last_diagnostic = "página ainda sem conteúdo validável"
        scrolled_marks = set()

        while True:
            elapsed = time.monotonic() - started
            if elapsed > RENDER_MAX_SECONDS:
                break

            # Scrolls espaçados acionam conteúdo lazy-loaded sem ficar mexendo na
            # página a cada leitura.
            second = int(elapsed)
            for mark in (2, 5, 8):
                if second >= mark and mark not in scrolled_marks:
                    try:
                        if mark == 5:
                            driver.execute_script("window.scrollTo(0, 0);")
                        else:
                            driver.execute_script(
                                "window.scrollTo(0, document.body.scrollHeight);"
                            )
                    except Exception:
                        pass
                    scrolled_marks.add(mark)

            try:
                bodies = driver.find_elements(By.TAG_NAME, "body")
                if not bodies:
                    time.sleep(POLL_SECONDS)
                    continue
                text = bodies[0].text or ""
            except Exception as exc:
                # Com page_load_strategy='none' o navegador pode estar no meio da
                # troca de documento. Isso é "ainda carregando", não falha final.
                last_diagnostic = f"DOM ainda navegando: {clean_error(exc)}"
                time.sleep(POLL_SECONDS)
                continue

            units = parse_floorplan_text(plan, text)
            summary_status = plan_summary_status(plan, text)

            if units:
                signature = ("units", units_signature(units))
                last_diagnostic = f"vi {len(units)} unidade(s), aguardando estabilizar"
            elif summary_status == "contact":
                signature = ("contact", plan)
                last_diagnostic = "cabeçalho da planta mostra Contact Us"
            else:
                signature = None
                last_diagnostic = (
                    f"cabeçalho={summary_status or 'não reconhecido'}, sem unidades visíveis"
                )

            # Não aceitamos o snapshot inicial. Isso é deliberado: o HTML inicial
            # do Bainbridge já mostrou unidade antiga antes do JavaScript terminar.
            if elapsed >= RENDER_MIN_SECONDS and signature is not None:
                if signature == last_signature:
                    stable_reads += 1
                else:
                    last_signature = signature
                    stable_reads = 1

                if stable_reads >= STABLE_READS:
                    return units
            elif elapsed >= RENDER_MIN_SECONDS:
                last_signature = None
                stable_reads = 0

            time.sleep(POLL_SECONDS)

        raise RuntimeError(
            f"a página abriu, mas o estado visível não ficou estável: {last_diagnostic}"
        )


def unit_qualifies(unit):
    plan = unit.get("plan", "")
    price = int(unit.get("price", 10**9))
    if plan in A_PLANS:
        return price < A_MAX_RENT
    if plan in B_PLANS:
        return price <= MAX_RENT
    return False


def qualifying(units):
    return {key: unit for key, unit in units.items() if unit_qualifies(unit)}


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


def current_summary(units, failed_a_plans=None):
    failed_a_plans = failed_a_plans or []
    q = qualifying(units)

    b_units = sorted(
        (u for u in q.values() if u["plan"] in B_PLANS),
        key=lambda u: (u["plan"], u["floor"], int(u["unit"])),
    )
    a_units = sorted(
        (u for u in q.values() if u["plan"] in A_PLANS),
        key=lambda u: (u["plan"], u["floor"], int(u["unit"])),
    )

    b1 = sum(u["plan"] == "B1" for u in b_units)
    b2 = sum(u["plan"] == "B2" for u in b_units)
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
            count = sum(u["plan"] == plan for u in a_units)
            if count:
                counts.append(f"{plan}={count}")
        a_section = (
            f"🛏️ 1 QUARTO | A1-A6 | menos de {money(A_MAX_RENT)}\n"
            + (" | ".join(counts) + "\n" if counts else "")
            + "\n".join(unit_line(u) for u in a_units)
        )
    elif failed_a_plans:
        a_section = (
            f"🛏️ 1 QUARTO | A1-A6 | menos de {money(A_MAX_RENT)}\n"
            "Nenhuma unidade confirmada no filtro, mas a consulta ficou parcial."
        )
    else:
        a_section = (
            f"🛏️ 1 QUARTO | A1-A6 | menos de {money(A_MAX_RENT)}\n"
            "Nenhuma unidade no filtro neste momento."
        )

    if failed_a_plans:
        a_section += (
            "\n⚠️ Sem atualização nesta execução: "
            + ", ".join(sorted(failed_a_plans))
            + ". Mantive o último estado conhecido dessas plantas."
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
        if is_priority(new):
            heading = "🔥🔥 PRIORIDADE: B2 NO 5º ANDAR"
        elif key in old_units:
            heading = "💰 ENTROU NO SEU LIMITE"
        else:
            heading = "🏠 NOVA UNIDADE NO SEU FILTRO"

        extra = f"\nAntes: {money(old_units[key]['price'])}" if key in old_units else ""
        events[event_group(new)].append(f"{heading}\n{unit_line(new)}{extra}")

    for key in sorted(set(old_q) - set(new_q)):
        old = old_q[key]
        if key in new_units:
            new = new_units[key]
            limit_text = (
                f"menos de {money(A_MAX_RENT)}"
                if old.get("plan") in A_PLANS
                else f"até {money(MAX_RENT)}"
            )
            text = (
                "⬆️ SAIU DO SEU LIMITE\n"
                f"{old['plan']} #{old['unit']} | {old['floor']}º andar\n"
                f"Antes: {money(old['price'])} | Agora: {money(new['price'])}\n"
                f"Filtro: {limit_text}\n"
                f"Disponibilidade atual: {new['availability']}"
            )
        else:
            text = f"❌ NÃO APARECE MAIS COMO DISPONÍVEL\n{unit_line(old)}"
        events[event_group(old)].append(text)

    for key in sorted(set(old_q) & set(new_q)):
        old, new = old_q[key], new_q[key]
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
    return bool(events["B"] or events["A"])


def format_event_sections(events):
    sections = []
    if events["B"]:
        sections.append(
            f"🏠 2 QUARTOS | B1/B2 | até {money(MAX_RENT)}\n\n"
            + "\n\n".join(events["B"])
        )
    if events["A"]:
        sections.append(
            f"🛏️ 1 QUARTO | A1-A6 | menos de {money(A_MAX_RENT)}\n\n"
            + "\n\n".join(events["A"])
        )
    return "\n\n".join(sections)


def telegram_token():
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Secret TELEGRAM_BOT_TOKEN não configurado.")
    return token


def telegram_chat_id():
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not chat_id:
        raise RuntimeError("Secret TELEGRAM_CHAT_ID não configurado.")
    return chat_id


def telegram_keyboard():
    return {
        "inline_keyboard": [
            [
                {"text": "🔎 Verificar agora", "callback_data": "check_now"},
                {"text": "📋 Opções atuais", "callback_data": "show_current"},
            ],
            [
                {"text": "🏠 Abrir B1", "url": f"{BASE_URL}/b1/"},
                {"text": "🏠 Abrir B2", "url": f"{BASE_URL}/b2/"},
            ],
        ]
    }


def telegram_request(method, *, params=None, json_body=None, timeout=20):
    token = telegram_token()
    url = f"https://api.telegram.org/bot{token}/{method}"
    if json_body is not None:
        response = requests.post(url, json=json_body, timeout=timeout)
    else:
        response = requests.get(url, params=params, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method} retornou erro.")
    return data.get("result")


def send_telegram(text, *, buttons=True):
    payload = {
        "chat_id": telegram_chat_id(),
        "text": text,
        "disable_web_page_preview": True,
    }
    if buttons:
        payload["reply_markup"] = telegram_keyboard()
    telegram_request("sendMessage", json_body=payload)


def get_updates(offset=None):
    params = {
        "timeout": 0,
        "allowed_updates": json.dumps(["message", "callback_query"]),
    }
    if offset is not None:
        params["offset"] = offset
    return telegram_request("getUpdates", params=params) or []


def normalize_command(text):
    first = (text or "").strip().lower().split(maxsplit=1)
    if not first:
        return ""
    command = first[0]
    return command.split("@", 1)[0] if command.startswith("/") else command


def answer_callback(callback_id, text):
    try:
        telegram_request(
            "answerCallbackQuery",
            json_body={
                "callback_query_id": callback_id,
                "text": text,
            },
            timeout=10,
        )
    except Exception:
        # O clique pode ter alguns minutos quando o GitHub acordar; nesse caso a
        # confirmação visual do Telegram pode expirar, mas o pedido continua válido.
        pass


def poll_telegram(state):
    last_id = state.get("telegram_update_id")
    offset = last_id + 1 if isinstance(last_id, int) else None
    updates = get_updates(offset)
    newest_id = last_id
    manual_requested = False
    current_requested = False
    expected_chat_id = telegram_chat_id()

    for update in updates:
        update_id = update.get("update_id")
        if isinstance(update_id, int):
            newest_id = update_id if newest_id is None else max(newest_id, update_id)

        callback = update.get("callback_query")
        if callback:
            action = callback.get("data")
            if action == "check_now":
                manual_requested = True
                print("Telegram: botão 'Verificar agora' recebido.")
                if callback.get("id"):
                    answer_callback(
                        callback["id"],
                        "Pedido recebido. Vou fazer uma nova consulta.",
                    )
            elif action == "show_current":
                current_requested = True
                print("Telegram: botão 'Opções atuais' recebido.")
                if callback.get("id"):
                    answer_callback(
                        callback["id"],
                        "Vou mostrar o último estado confirmado.",
                    )
            continue

        message = update.get("message")
        if not message:
            continue

        chat = message.get("chat") or {}
        source_chat_id = str(chat.get("id", ""))
        command = normalize_command(message.get("text", ""))

        if command == "/id":
            try:
                payload = {
                    "chat_id": source_chat_id,
                    "text": f"🔐 Chat ID: {source_chat_id}",
                    "disable_web_page_preview": True,
                }
                telegram_request("sendMessage", json_body=payload)
            except Exception as exc:
                print(f"Não consegui responder /id: {exc}", file=sys.stderr)
            continue

        if source_chat_id == expected_chat_id and command in MANUAL_COMMANDS:
            manual_requested = True
            print(f"Telegram: comando {command} recebido.")

    return manual_requested, current_requested, newest_id


def save_pending_requests(state, manual_requested, current_requested, newest_id):
    """Persiste os cliques ANTES de acessar o site, para nenhum pedido se perder."""
    changed = False
    if newest_id is not None and newest_id != state.get("telegram_update_id"):
        state["telegram_update_id"] = newest_id
        changed = True
    if manual_requested and not state.get("manual_pending"):
        state["manual_pending"] = True
        changed = True
    if current_requested and not state.get("current_pending"):
        state["current_pending"] = True
        changed = True
    if changed:
        state["updated_at_utc"] = utc_now_iso()
        save_state(state)


def send_saved_current_options(state, old_units):
    """Reenvia o último estado confirmado sem depender de uma nova leitura do site."""
    if not state.get("current_pending"):
        return

    failed_a = state.get("a_failed_plans", [])
    if not isinstance(failed_a, list):
        failed_a = []

    if old_units:
        message = (
            "📋 OPÇÕES ATUAIS DENTRO DOS FILTROS\n\n"
            "Último estado confirmado pelo monitor.\n\n"
            + current_summary(old_units, failed_a)
        )
    else:
        message = (
            "📋 OPÇÕES ATUAIS DENTRO DOS FILTROS\n\n"
            "Ainda não existe uma leitura confirmada salva. "
            "Use 🔎 Verificar agora para fazer a primeira consulta."
        )

    try:
        send_telegram(message)
        state.pop("current_pending", None)
        state["updated_at_utc"] = utc_now_iso()
        save_state(state)
    except Exception as exc:
        print(
            f"Falha ao enviar opções atuais; pedido continuará pendente: {exc}",
            file=sys.stderr,
        )


def preserve_old_plan(old_units, target, plan):
    for key, unit in old_units.items():
        if unit.get("plan") == plan:
            target[key] = unit


def scrape_all(scraper, old_units):
    """
    B1/B2 são obrigatórios. A1-A6 são independentes: se uma A falhar, apenas
    aquela planta preserva o último estado e o restante do monitor continua.
    """
    units = {}

    for plan in B_PLANS:
        units.update(scraper.fetch_plan(plan))

    failed_a = []
    for plan in A_PLANS:
        try:
            units.update(scraper.fetch_plan(plan))
        except Exception as exc:
            failed_a.append(plan)
            preserve_old_plan(old_units, units, plan)
            print(f"{plan}: preservando último estado. Erro: {exc}", file=sys.stderr)

    return units, failed_a


def apply_main_success_health(state):
    was_notified = bool(state.get("error_notified", False))
    state["consecutive_failures"] = 0
    state["monitor_status"] = "ok"
    state.pop("last_error_message", None)
    state.pop("last_error_utc", None)
    return was_notified


def apply_a_health(state, failed_a):
    was_notified = bool(state.get("a_error_notified", False))

    if not failed_a:
        state["a_consecutive_failures"] = 0
        state["a_failed_plans"] = []
        state.pop("a_last_error_message", None)
        state.pop("a_last_error_utc", None)
        return {
            "alert": False,
            "recovery": was_notified,
            "failed": [],
        }

    count = int(state.get("a_consecutive_failures", 0) or 0) + 1
    state["a_consecutive_failures"] = count
    state["a_failed_plans"] = sorted(failed_a)
    state["a_last_error_utc"] = utc_now_iso()
    state["a_last_error_message"] = ", ".join(sorted(failed_a))

    return {
        "alert": count >= ERROR_FAILURE_THRESHOLD and not was_notified,
        "recovery": False,
        "failed": sorted(failed_a),
    }


def record_main_failure(state, exc):
    count = int(state.get("consecutive_failures", 0) or 0) + 1
    already_notified = bool(state.get("error_notified", False))

    state["consecutive_failures"] = count
    state["monitor_status"] = "error" if count >= ERROR_FAILURE_THRESHOLD else "degraded"
    state["last_error_utc"] = utc_now_iso()
    state["last_error_message"] = clean_error(exc)
    state["updated_at_utc"] = utc_now_iso()

    if count >= ERROR_FAILURE_THRESHOLD and not already_notified:
        try:
            send_telegram(
                "⚠️ MONITOR BAINBRIDGE COM PROBLEMA\n\n"
                f"B1/B2 falharam em {count} execuções consecutivas.\n"
                f"Horário: {local_time_text()}\n\n"
                "Vou continuar tentando automaticamente e não repetirei este alerta "
                "até a leitura voltar ao normal."
            )
            state["error_notified"] = True
        except Exception as telegram_exc:
            print(f"Falhou também o alerta do Telegram: {telegram_exc}", file=sys.stderr)
    elif count < ERROR_FAILURE_THRESHOLD:
        print("Falha isolada em B1/B2; nenhum alerta enviado.")

    save_state(state)


def send_health_notifications(state, main_recovery, a_health):
    """Falha no Telegram aqui não deve transformar uma leitura boa em falha do site."""
    if main_recovery:
        try:
            send_telegram(
                "✅ MONITOR BAINBRIDGE VOLTOU AO NORMAL\n\n"
                f"Horário: {local_time_text()}\n"
                "B1/B2 voltaram a ser consultados normalmente."
            )
            state["error_notified"] = False
        except Exception as exc:
            print(f"Não consegui enviar recuperação B1/B2: {exc}", file=sys.stderr)

    if a_health["recovery"]:
        try:
            send_telegram(
                "✅ FILTRO DE 1 QUARTO VOLTOU AO NORMAL\n\n"
                f"Horário: {local_time_text()}\n"
                "A1-A6 voltaram a ser consultados normalmente."
            )
            state["a_error_notified"] = False
        except Exception as exc:
            print(f"Não consegui enviar recuperação A1-A6: {exc}", file=sys.stderr)

    if a_health["alert"]:
        try:
            send_telegram(
                "⚠️ FILTRO DE 1 QUARTO TEMPORARIAMENTE INDISPONÍVEL\n\n"
                f"Horário: {local_time_text()}\n"
                "Sem atualização: " + ", ".join(a_health["failed"]) + ".\n"
                "B1/B2 continuam funcionando normalmente.\n\n"
                "Vou continuar tentando e aviso quando A1-A6 voltarem."
            )
            state["a_error_notified"] = True
        except Exception as exc:
            print(f"Não consegui enviar alerta A1-A6: {exc}", file=sys.stderr)


def main():
    if TEST_MODE:
        send_telegram(
            "🧪 TESTE DO MONITOR BAINBRIDGE: OK\n\n"
            f"Horário: {local_time_text()}\n"
            "Telegram: OK\n"
            "Este teste não consulta o site do Bainbridge."
        )
        print("Teste do Telegram concluído.")
        return

    state = load_state()
    old_units = state.get("units", {}) if isinstance(state.get("units", {}), dict) else {}

    manual_now, current_now, newest_id = poll_telegram(state)
    save_pending_requests(state, manual_now, current_now, newest_id)
    manual_pending = bool(state.get("manual_pending"))

    # "Opções atuais" não espera o Bainbridge: reenvia o último snapshot já
    # confirmado assim que esta execução do GitHub processa o clique.
    send_saved_current_options(state, old_units)

    persisted_state = json.loads(json.dumps(state))

    if manual_now:
        try:
            send_telegram(
                "⏳ PEDIDO RECEBIDO\n\n"
                "Vou consultar B1/B2 e A1-A6. "
                "O resultado chega após esta execução."
            )
        except Exception as exc:
            print(f"Não consegui confirmar o pedido: {exc}", file=sys.stderr)

    scraper = BrowserScraper()
    try:
        try:
            new_units, failed_a = scrape_all(scraper, old_units)
        except Exception as exc:
            record_main_failure(state, exc)
            return
    finally:
        scraper.close()

    main_recovery = apply_main_success_health(state)
    a_health = apply_a_health(state, failed_a)
    send_health_notifications(state, main_recovery, a_health)

    events = detect_changes(old_units, new_units)

    # Primeira execução de um repositório vazio: estabelece baseline sem fingir
    # que todas as unidades atuais acabaram de aparecer.
    first_run = not old_units
    if first_run:
        message = (
            "✅ MONITOR BAINBRIDGE ATIVADO\n\n"
            f"• B1/B2: até {money(MAX_RENT)}\n"
            f"• A1-A6: menos de {money(A_MAX_RENT)}\n"
            "• Verificação: aproximadamente a cada 5 minutos\n\n"
            + current_summary(new_units, failed_a)
        )
        send_telegram(message)
        state["units"] = dict(sorted(new_units.items()))
        state["manual_pending"] = False
        state["updated_at_utc"] = utc_now_iso()
        state["max_rent"] = MAX_RENT
        state["a_max_rent"] = A_MAX_RENT
        save_state(state)
        return

    # Consulta manual usa o snapshot atual mesmo quando uma planta A falhou; o
    # texto deixa explícito quais A foram preservadas.
    if manual_pending:
        try:
            send_telegram(
                "🔎 CONSULTA MANUAL BAINBRIDGE\n\n"
                f"Horário: {local_time_text()}\n"
                "Consulta concluída.\n\n"
                + current_summary(new_units, failed_a)
            )
            state["manual_pending"] = False
        except Exception as exc:
            print(f"Falha ao enviar consulta manual; pedido continuará pendente: {exc}", file=sys.stderr)

    # Se uma notificação de mudança falhar, NÃO avançamos o baseline. Assim a
    # mesma mudança será tentada novamente na próxima execução, em vez de sumir.
    event_sent = True
    if has_events(events):
        try:
            send_telegram(
                "🚨 BAINBRIDGE THE GRAND\n\n"
                + format_event_sections(events)
                + "\n\n📋 SITUAÇÃO ATUAL\n\n"
                + current_summary(new_units, failed_a)
            )
        except Exception as exc:
            event_sent = False
            print(f"Falha ao enviar alerta de mudança; baseline preservado: {exc}", file=sys.stderr)

    if event_sent:
        state["units"] = dict(sorted(new_units.items()))

    state["max_rent"] = MAX_RENT
    state["a_max_rent"] = A_MAX_RENT
    save_state_if_changed(state, persisted_state)

    if not has_events(events) and not manual_pending and not main_recovery:
        print("Sem mudanças relevantes. Nenhuma notificação enviada.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Erros inesperados (Telegram, JSON, etc.) aparecem no log. Falhas de
        # B1/B2 já são tratadas dentro de main() para manter o contador correto.
        print(f"ERRO INESPERADO: {exc}", file=sys.stderr)
        raise
