# bot/main.py
import os
import json
from pathlib import Path
from datetime import datetime, timedelta

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# ---------- تنظیم مسیر فایل‌ها ----------
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "database.json"
NOTES_PATH = BASE_DIR / "daily_notes.json"


# ---------- توابع کمکی برای JSON ----------
def load_json(path: Path):
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        # اگر خراب شد، خالی برمی‌گردونیم که ربات نخوابه
        return {}


def save_json(path: Path, data: dict):
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def parse_iso(dt_str: str) -> datetime:
    return datetime.fromisoformat(dt_str)


def get_user(db: dict, telegram_id: int) -> dict:
    """
    ساخت/گرفتن ساختار کاربر داخل database.json
    ساختار:
    {
      "users": {
        "<telegram_id>": {
          "sessions": [
            {"location": "...", "start": "...", "end": "..."},
            ...
          ]
        }
      }
    }
    """
    users = db.setdefault("users", {})
    user = users.setdefault(str(telegram_id), {})
    user.setdefault("sessions", [])
    return user


# ---------- دستورات اصلی ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (
        f"سلام {user.first_name or ''} 👋\n\n"
        "من ربات ثبت حضور در لوکیشن‌ها و یادداشت‌های روزانه‌ام.\n\n"
        "دستورات اصلی:\n"
        "• /in <نام لوکیشن>  → ثبت ورود ✅\n"
        "• /out <نام لوکیشن> → ثبت خروج ⛔\n"
        "• /report today|week|month → گزارش حضور\n"
        "• /note <متن> → ثبت یادداشت امروز\n"
        "• /notes today|week|month → دیدن یادداشت‌ها\n"
    )
    if update.message:
        await update.message.reply_text(text)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


# ---------- ثبت ورود ----------

async def in_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return
    user = message.from_user

    if not context.args:
        await message.reply_text(
            "بعد از /in نام لوکیشن را بنویس.\nمثال:\n`/in شعبه ۱`\n`/in انبار`",
            parse_mode="Markdown",
        )
        return

    location = " ".join(context.args)
    db = load_json(DB_PATH)
    user_data = get_user(db, user.id)

    # اگر سشن باز برای همین لوکیشن هست، خودکار می‌بندیم (جلوگیری از باز موندن)
    now = now_iso()
    for session in user_data["sessions"]:
        if session.get("end") is None and session.get("location") == location:
            session["end"] = now
            session["closed_by"] = "auto_on_new_in"

    # سشن جدید (ورود)
    user_data["sessions"].append(
        {
            "location": location,
            "start": now,
            "end": None,
        }
    )
    save_json(DB_PATH, db)

    await message.reply_text(
        f"✅ ورود ثبت شد.\n"
        f"📍 لوکیشن: {location}\n"
        f"⏰ زمان: {now}"
    )


# ---------- ثبت خروج ----------

async def out_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return
    user = message.from_user

    if not context.args:
        await message.reply_text(
            "بعد از /out نام لوکیشن را بنویس.\nمثال:\n`/out شعبه ۱`\n`/out انبار`",
            parse_mode="Markdown",
        )
        return

    location = " ".join(context.args)
    db = load_json(DB_PATH)
    user_data = get_user(db, user.id)

    # پیدا کردن سشن‌های باز برای این لوکیشن
    open_sessions = [
        s for s in user_data["sessions"]
        if s.get("end") is None and s.get("location") == location
    ]

    now = now_iso()
    if not open_sessions:
        await message.reply_text(
            "برای این لوکیشن سشن بازی پیدا نکردم.\n"
            "اگر اشتباهی خروج زدی، اول /in بزن و بعد دوباره /out."
        )
        return

    # آخرین سشن باز را می‌بندیم
    session = open_sessions[-1]
    session["end"] = now
    save_json(DB_PATH, db)

    await message.reply_text(
        f"⛔ خروج ثبت شد.\n"
        f"📍 لوکیشن: {location}\n"
        f"⏰ زمان: {now}"
    )


# ---------- بازه زمانی برای گزارش ----------

def _get_period_range(period: str):
    now = datetime.now()
    if period == "today":
        start = datetime(now.year, now.month, now.day)
        end = now
        title = "امروز"
    elif period == "week":
        start = now - timedelta(days=7)
        end = now
        title = "۷ روز اخیر"
    elif period == "month":
        start = now - timedelta(days=30)
        end = now
        title = "۳۰ روز اخیر"
    else:
        raise ValueError("invalid period")
    return start, end, title


# ---------- گزارش حضور ----------

async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return
    user = message.from_user

    if not context.args:
        await message.reply_text(
            "دوره گزارش را مشخص کن:\n"
            "`/report today`\n"
            "`/report week`\n"
            "`/report month`",
            parse_mode="Markdown",
        )
        return

    period = context.args[0].lower()
    try:
        start_dt, end_dt, title = _get_period_range(period)
    except ValueError:
        await message.reply_text("دوره نامعتبر است. از today, week, month استفاده کن.")
        return

    db = load_json(DB_PATH)
    user_data = get_user(db, user.id)
    sessions = user_data["sessions"]

    # فیلتر سشن‌هایی که زمان شروع‌شان در بازه است
    filtered = []
    for s in sessions:
        try:
            st = parse_iso(s["start"])
        except Exception:
            continue
        if start_dt <= st <= end_dt:
            en = parse_iso(s["end"]) if s.get("end") else None
            filtered.append((st, en, s["location"]))

    if not filtered:
        await message.reply_text(f"📭 هیچ رکوردی برای {title} پیدا نشد.")
        return

    filtered.sort(key=lambda x: x[0])

    lines = [f"📊 گزارش حضور - {title}"]
    total_minutes = 0
    per_location = {}

    for st, en, location in filtered:
        st_str = st.strftime("%Y-%m-%d %H:%M")
        if en:
            en_str = en.strftime("%Y-%m-%d %H:%M")
            minutes = int((en - st).total_seconds() // 60)
        else:
            # اگر هنوز خروج ثبت نشده → تا انتهای بازه حساب می‌کنیم
            en = end_dt
            en_str = "در حال حاضر"
            minutes = int((en - st).total_seconds() // 60)

        total_minutes += minutes
        per_location[location] = per_location.get(location, 0) + minutes

        lines.append(
            f"\n📍 {location}\n"
            f"   ⏰ ورود: {st_str}\n"
            f"   🚪 خروج: {en_str}\n"
            f"   ⌛ مدت: {minutes} دقیقه"
        )

    lines.append("\n———————————————")
    lines.append(f"⌛ جمع کل مدت حضور: {total_minutes} دقیقه")

    if per_location:
        lines.append("\n📍 جمع مدت حضور به تفکیک لوکیشن:")
        for loc, mins in per_location.items():
            lines.append(f"  • {loc}: {mins} دقیقه")

    await message.reply_text("\n".join(lines))


# ---------- یادداشت روزانه ----------

async def note_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return
    user = message.from_user

    if not context.args:
        await message.reply_text(
            "بعد از /note متن یادداشتت رو بنویس.\n"
            "مثال:\n"
            "/note امروز شعبه خیلی شلوغ بود."
        )
        return

    note_text = " ".join(context.args)
    today = datetime.now().date().isoformat()
    now = now_iso()

    notes = load_json(NOTES_PATH)
    users = notes.setdefault("users", {})
    user_notes = users.setdefault(str(user.id), {})
    day_list = user_notes.setdefault(today, [])
    day_list.append(
        {
            "timestamp": now,
            "text": note_text,
        }
    )
    save_json(NOTES_PATH, notes)

    await message.reply_text(
        f"📝 یادداشتت برای امروز ({today}) ذخیره شد.\n"
        f"متن: {note_text}"
    )


async def notes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return
    user = message.from_user

    if not context.args:
        await message.reply_text(
            "لطفاً دوره را مشخص کن:\n"
            "/notes today\n"
            "/notes week\n"
            "/notes month"
        )
        return

    period = context.args[0].lower()
    try:
        start_dt, end_dt, title = _get_period_range(period)
    except ValueError:
        await message.reply_text("دوره نامعتبر است. از today, week, month استفاده کن.")
        return

    notes = load_json(NOTES_PATH)
    users = notes.get("users", {})
    user_notes = users.get(str(user.id), {})

    start_date = start_dt.date()
    end_date = end_dt.date()

    lines = [f"📝 یادداشت‌ها - {title}"]
    has_any = False

    for i in range((end_date - start_date).days + 1):
        day = start_date + timedelta(days=i)
        day_str = day.isoformat()
        day_list = user_notes.get(day_str, [])
        if not day_list:
            continue
        has_any = True
        lines.append(f"\n📅 {day_str}:")
        for item in day_list:
            try:
                ts = parse_iso(item["timestamp"])
                t_str = ts.strftime("%H:%M")
            except Exception:
                t_str = "?"
            lines.append(f"  • ({t_str}) {item['text']}")

    if not has_any:
        await message.reply_text(f"📭 هیچ یادداشتی برای {title} ثبت نشده.")
        return

    await message.reply_text("\n".join(lines))


# ---------- راه‌اندازی اپ برای Koyeb (Webhook) یا لوکال (Polling) ----------

def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    # مطمئن می‌شیم پوشه وجود داره
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    if not DB_PATH.exists():
        save_json(DB_PATH, {})
    if not NOTES_PATH.exists():
        save_json(NOTES_PATH, {})

    app = Application.builder().token(token).build()

    # ثبت هندلرها
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("in", in_cmd))
    app.add_handler(CommandHandler("out", out_cmd))
    app.add_handler(CommandHandler("report", report_cmd))
    app.add_handler(CommandHandler("note", note_cmd))
    app.add_handler(CommandHandler("notes", notes_cmd))

    # اگر WEBHOOK_URL ست شده → حالت Koyeb/Webhook
    webhook_url = os.environ.get("WEBHOOK_URL")
    port = int(os.environ.get("PORT", "8080"))

    if webhook_url:
        print("Starting bot in WEBHOOK mode...")
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=token,
            webhook_url=f"{webhook_url.rstrip('/')}/{token}",
        )
    else:
        print("Starting bot in POLLING mode...")
        app.run_polling()


if __name__ == "__main__":
    main()
