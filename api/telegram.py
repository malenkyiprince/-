import json
import os
import re
import hmac
from http.server import BaseHTTPRequestHandler
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class TelegramError(Exception):
    def __init__(self, code, retry_after=0):
        self.code = code
        self.retry_after = retry_after


def telegram(token, method, **payload):
    request = Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=8) as response:
            result = json.load(response)
    except HTTPError as error:
        with error:
            try:
                result = json.load(error)
            except ValueError:
                raise TelegramError(error.code) from None
    except (URLError, OSError, ValueError):
        raise TelegramError(0) from None

    if result.get("ok"):
        return result["result"]
    raise TelegramError(
        result.get("error_code", 0),
        result.get("parameters", {}).get("retry_after", 0),
    )


MODERATION_BUTTONS = {
    "inline_keyboard": [[
        {"text": "Опублікувати", "callback_data": "publish"},
        {"text": "Відхилити", "callback_data": "reject"},
    ]]
}

HEADER = "📩 Анонімне звернення"


def handle_message(api, admin_id, message):
    chat = message.get("chat", {})
    if chat.get("type") != "private" or message.get("from", {}).get("is_bot"):
        return

    body = (message.get("text") or "").strip()
    caption = (message.get("caption") or "").strip()
    command = body.split(maxsplit=1)[0].split("@", 1)[0] if body else ""
    method = None
    payload = {}
    if command in {"/start", "/help"}:
        reply = (
            "Надішли текст, фото або голосове повідомлення. Бот передасть його "
            "адміністратору без твого імені та посилання на профіль. "
            "Якщо адміністратор схвалить звернення, воно з’явиться в каналі."
        )
    elif command.startswith("/"):
        reply = "Щоб надіслати звернення, напиши текст або надішли фото чи голосове повідомлення."
    elif body:
        method = "sendMessage"
        payload = {
            "text": f"{HEADER}\n\n{body}",
            "link_preview_options": {"is_disabled": True},
        }
    elif message.get("photo"):
        method = "sendPhoto"
        payload = {"photo": message["photo"][-1]["file_id"]}
    elif message.get("voice"):
        method = "sendVoice"
        payload = {"voice": message["voice"]["file_id"]}
    else:
        reply = "Бот приймає текст, фото та голосові повідомлення."

    if method in {"sendPhoto", "sendVoice"}:
        payload["caption"] = f"{HEADER}\n\n{caption}" if caption else HEADER

    if method:
        field = "text" if method == "sendMessage" else "caption"
        limit = 4096 if field == "text" else 1024
        if len(payload[field].encode("utf-16-le")) // 2 > limit:
            reply = "Звернення або підпис задовгі. Скороти текст і надішли ще раз."
        else:
            try:
                api(method, chat_id=admin_id, reply_markup=MODERATION_BUTTONS, **payload)
            except TelegramError as error:
                print(f"Помилка надсилання адміністратору (код {error.code}).", flush=True)
                if error.code == 0 or error.code >= 500:
                    reply = "Telegram не підтвердив надсилання звернення адміністратору."
                else:
                    reply = "Не вдалося передати звернення адміністратору. Спробуй пізніше."
            else:
                reply = "Звернення надіслано адміністратору на розгляд."

    try:
        api("sendMessage", chat_id=chat["id"], text=reply)
    except TelegramError as error:
        print(f"Помилка надсилання відповіді (код {error.code}).", flush=True)


def moderate(api, admin_id, channel, callback):
    message = callback.get("message", {})
    action = callback.get("data")
    status = "Ця дія недоступна."
    if (
        callback.get("from", {}).get("id") == admin_id
        and message.get("chat", {}).get("id") == admin_id
        and message.get("reply_markup") == MODERATION_BUTTONS
        and action in {"publish", "reject"}
    ):
        message_id = message["message_id"]
        try:
            api(
                "editMessageReplyMarkup",
                chat_id=admin_id,
                message_id=message_id,
                reply_markup={"inline_keyboard": []},
            )
        except TelegramError:
            status = "Рішення вже прийнято або Telegram не підтвердив дію."
        else:
            if action == "reject":
                status = "Звернення відхилено."
            else:
                try:
                    api("copyMessage", chat_id=channel, from_chat_id=admin_id, message_id=message_id)
                except TelegramError as error:
                    status = "Перевір канал. Telegram не підтвердив публікацію."
                    if 400 <= error.code < 500:
                        status = "Не вдалося опублікувати. Перевір канал і права бота."
                        try:
                            api(
                                "editMessageReplyMarkup",
                                chat_id=admin_id,
                                message_id=message_id,
                                reply_markup=MODERATION_BUTTONS,
                            )
                        except TelegramError:
                            status += " Кнопки не вдалося відновити."
                else:
                    status = "Звернення опубліковано."

    try:
        api("answerCallbackQuery", callback_query_id=callback["id"], text=status, show_alert=True)
    except TelegramError as error:
        print(f"Помилка відповіді адміністратору (код {error.code}).", flush=True)


class handler(BaseHTTPRequestHandler):
    def respond(self, code):
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        self.respond(200)

    def do_POST(self):
        token = os.environ.get("BOT_TOKEN", "").strip()
        channel = os.environ.get("CHANNEL_ID", "").strip()
        admin = os.environ.get("ADMIN_ID", "").strip()
        secret = os.environ.get("WEBHOOK_SECRET", "").strip()
        if (
            not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token)
            or not re.fullmatch(r"@[A-Za-z0-9_]+|-100[0-9]+", channel)
            or not re.fullmatch(r"[1-9][0-9]{0,15}", admin)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", secret)
        ):
            self.respond(503)
            return

        supplied_secret = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(secret.encode(), supplied_secret.encode()):
            self.respond(403)
            return

        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 1048576:
                self.respond(400)
                return
            update = json.loads(self.rfile.read(size))
        except (ValueError, UnicodeError):
            self.respond(400)
            return

        if not isinstance(update, dict) or not isinstance(update.get("update_id"), int):
            self.respond(400)
            return

        def api(method, **payload):
            return telegram(token, method, **payload)

        if isinstance(update.get("callback_query"), dict):
            destination = int(channel) if channel.startswith("-") else channel
            moderate(api, int(admin), destination, update["callback_query"])
        elif isinstance(update.get("message"), dict):
            handle_message(api, int(admin), update["message"])
        self.respond(200)
