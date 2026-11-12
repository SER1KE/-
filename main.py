import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import Bot
from dtek_client import DtekClient
from dtek_client.browser_auth import get_cleared_cookies
from curl_cffi.requests import AsyncSession

from config import CITY, STREET, HOUSE_NUMBER, EXPECTED_GROUP, WARNING_MINUTES, TIMEZONE

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dtek-notifier")

STATE_PATH = Path("state.json")
TZ = ZoneInfo(TIMEZONE)

BASE_URL = "https://www.dtek-oem.com.ua"
SCHEDULE_URL = f"{BASE_URL}/ua/shutdowns"
AJAX_URL = f"{BASE_URL}/ua/ajax"


def load_state():
    if not STATE_PATH.exists():
        return {"warned": [], "emergency_active": None, "initialized": False}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        data.setdefault("warned", [])
        data.setdefault("emergency_active", None)
        data.setdefault("initialized", False)
        return data
    except (OSError, json.JSONDecodeError):
        log.exception("Не удалось прочитать state.json; создаю новое состояние")
        return {"warned": [], "emergency_active": None, "initialized": False}


def save_state(state):
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def normalize(value):
    return re.sub(r"[^0-9а-яіїєґ]", "", str(value).lower())


def find_house_entry(response):
    houses = getattr(response, "houses", None)
    if houses is None and isinstance(response, dict):
        houses = response.get("houses") or response.get("data")
    if not isinstance(houses, dict):
        return None, None

    wanted = normalize(HOUSE_NUMBER)
    for house, entry in houses.items():
        candidate = normalize(house)
        if candidate == wanted or candidate in (wanted + "1", wanted + "корпус1"):
            return str(house), entry
    return None, None


def read_field(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def get_group(entry):
    group_ids = read_field(entry, "group_ids")
    primary = read_field(entry, "primary_group")
    if primary:
        return str(primary)
    if group_ids:
        return ", ".join(map(str, group_ids))
    reasons = read_field(entry, "sub_type_reason")
    if reasons:
        return ", ".join(map(str, reasons))
    return None


def has_current_outage(entry):
    value = read_field(entry, "has_current_outage")
    return value if isinstance(value, bool) else None


def slot_is_outage(status):
    # The client documents YES as no outage, NO/FIRST/SECOND as outage statuses.
    value = getattr(status, "value", status)
    value = str(value).upper()
    if value in {"NO", "FIRST", "SECOND", "MFIRST", "MSECOND"}:
        return True
    if value in {"YES", "MAYBE", "UNKNOWN"}:
        return False
    prop = getattr(status, "has_outage", None)
    return prop if isinstance(prop, bool) else False


def slot_start(key, now):
    """Accept explicit HH:MM keys or 0..47 half-hour slot indices."""
    text = str(key)
    match = re.search(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)", text)
    if match:
        return now.replace(
            hour=int(match.group(1)),
            minute=int(match.group(2)),
            second=0,
            microsecond=0,
        )
    if text.isdigit():
        index = int(text)
        if 0 <= index <= 47:
            start = datetime.combine(now.date(), datetime.min.time(), TZ)
            return start + timedelta(minutes=30 * index)
    return None


def get_slots_for_today(client):
    # The package resolves the address/group internally and returns a mapping
    # of time-slot keys to SlotStatus values.
    return client.get_today_schedule(CITY, STREET, HOUSE_NUMBER)


async def send_message(bot, chat_id, text):
    await bot.send_message(chat_id=chat_id, text=text)


async def run():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        raise RuntimeError(
            "Не заданы TELEGRAM_BOT_TOKEN и/или TELEGRAM_CHAT_ID в GitHub Secrets"
        )

    state = load_state()
    now = datetime.now(TZ)

    log.info("Получаю браузерную сессию ДТЭК")
    cookies, csrf_token = await get_cleared_cookies(SCHEDULE_URL)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "uk,en-US;q=0.9,en;q=0.8",
        "Origin": BASE_URL,
        "Referer": SCHEDULE_URL,
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }

    if csrf_token:
        headers["X-CSRF-Token"] = csrf_token

    session = AsyncSession(
        timeout=20.0,
        headers=headers,
        cookies=cookies,
        impersonate="chrome120",
    )

    bot = Bot(token=token)

    try:
        await bot.initialize()

        async with DtekClient(
            "oem",
            ajax_url=AJAX_URL,
            session=session,
        ) as client:

            # Read all houses for the street so we can inspect the house's live flag.
            log.info(
    "ДТЭК: варианты номеров домов, начинающихся на 20: %s",
    [h for h in response.houses if normalize(h).startswith("20")]
)
            house_key, entry = find_house_entry(response)

            if entry is None:
                raise RuntimeError(
                    f"Дом {HOUSE_NUMBER} не найден в данных ДТЭК для {STREET}. "
                    "Не отправляю уведомления, чтобы не отслеживать чужой адрес."
                )

            group = get_group(entry)

            log.info(
                "ДТЭК: дом=%s, группа=%s, ожидаемая=%s",
                house_key,
                group,
                EXPECTED_GROUP,
            )

            if group and EXPECTED_GROUP not in group:
                raise RuntimeError(
                    f"ДТЭК показывает для дома группу {group}, а настроена {EXPECTED_GROUP}. "
                    "Проверьте адрес/корпус/очередь."
                )

            # Emergency/restoration messages only on an explicit boolean state.
            current = has_current_outage(entry)
            previous = state.get("emergency_active")

            if current is True and previous is not True:
                await send_message(
                    bot,
                    chat_id,
                    f"🚨 ДТЭК сообщает об отключении электричества.\n"
                    f"Адрес: Одесса, Щоглова, 20/1\nОчередь: {EXPECTED_GROUP}\n"
                    f"Время обнаружения: {now:%d.%m.%Y %H:%M}",
                )
                state["emergency_active"] = True

            elif current is False and previous is True:
                await send_message(
                    bot,
                    chat_id,
                    f"💡 ДТЭК больше не показывает активное аварийное отключение.\n"
                    f"Адрес: Одесса, Щоглова, 20/1\n"
                    f"Время обнаружения восстановления: {now:%d.%m.%Y %H:%M}",
                )
                state["emergency_active"] = False

            elif current is not None and previous is None:
                # Baseline only: do not send a false "restored" alert on first run.
                state["emergency_active"] = current

            slots = await get_slots_for_today(client)

            if slots:
                for key, status in slots.items():
                    if not slot_is_outage(status):
                        continue

                    start = slot_start(key, now)

                    if start is None:
                        log.warning(
                            "Не распознал время слота %r; пропускаю предупреждение",
                            key,
                        )
                        continue

                    if start.date() != now.date():
                        continue

                    minutes = (start - now).total_seconds() / 60
                    event_id = f"{start.isoformat()}|{EXPECTED_GROUP}"

                    if 0 < minutes <= WARNING_MINUTES and event_id not in state["warned"]:
                        await send_message(
                            bot,
                            chat_id,
                            f"⚠️ Плановое отключение по графику ДТЭК примерно через {round(minutes)} мин.\n"
                            f"Адрес: Одесса, Щоглова, 20/1\nОчередь: {EXPECTED_GROUP}\n"
                            f"Начало: {start:%H:%M}\n"
                            "График может измениться.",
                        )
                        state["warned"].append(event_id)

            # Keep state bounded to the recent past/future.
            cutoff = now - timedelta(days=2)
            kept = []

            for item in state["warned"]:
                try:
                    stamp = datetime.fromisoformat(item.split("|", 1)[0])
                    if stamp >= cutoff:
                        kept.append(item)
                except ValueError:
                    continue

            state["warned"] = kept[-200:]
            state["initialized"] = True
            save_state(state)

    finally:
        await session.close()
        await bot.shutdown()


if __name__ == "__main__":
    asyncio.run(run())
