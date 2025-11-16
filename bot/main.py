# bot/main.py
import os
import logging
import sqlite3
from datetime import datetime, timedelta, date
import io
from pathlib import Path

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
)
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters
)
import pandas as pd
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

# ---------- CONFIG ----------
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data.db"

LOCATIONS = ["شعبه ۱", "شعبه ۲", "شعبه ۳", "انبار ۱", "انبار ۲", "دفتر شهرک"]
# ----------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- DB helpers ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """CREATE TABLE IF NOT EXISTS users (
               user_id INTEGER PRIMARY KEY,
               username TEXT,
               first_name TEXT,
               last_name TEXT)"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS attendance (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               user_id INTEGER,
               location TEXT,
               entry_time TEXT,
               exit_time TEXT,
               auto_created INTEGER DEFAULT 0)"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS daily_notes (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               user_id INTEGER,
               note_date TEXT,
               time TEXT,
               message TEXT)"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS note_sessions (
               user_id INTEGER PRIMARY KEY,
               active INTEGER DEFAULT 0)"""
    )
    conn.commit()
    conn.close()


def db_execute(query, params=(), fetch=False):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(query, params)
    if fetch:
        rows = c.fetchall()
        conn.commit()
        conn.close()
        return rows
    conn.commit()
    conn.close()


# --- Utility ---
def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def today_str():
    return date.today().isoformat()


def time_str():
    return datetime.now().strftime("%H:%M:%S")


# --- Start ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    # save user
    db_execute(
        "INSERT OR REPLACE INTO users(user_id, username, first_name, last_name) VALUES (?, ?, ?, ?)",
        (user.id, user.username or "", user.first_name or "", user.last_name or ""),
    )
    keyboard = [[KeyboardButton(loc)] for loc in LOCATIONS]
    keyboard.append([KeyboardButton("گزارش‌ها"), KeyboardButton("یادداشت روزانه")])
    reply = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    if update.message:
        await update.message.reply_text(
            "سلام! لوکیشن خود را انتخاب کن یا یکی از دکمه‌ها را بزن:",
            reply_markup=reply,
        )


# --- core actions: entry / exit ---
async def handle_entry(query, context, location: str):
    user = query.from_user
    ts = now_iso()
    # Check last attendance for same user+location with NULL exit_time
    rows = db_execute(
        "SELECT id, entry_time FROM attendance "
        "WHERE user_id=? AND location=? AND exit_time IS NULL "
        "ORDER BY id DESC LIMIT 1",
        (user.id, location),
        fetch=True,
    )
    if rows:
        await query.message.reply_text(
            "به نظر می‌رسد هنوز برای این لوکیشن خروج ثبت نکرده‌ای. "
            "اگر می‌خواهی ورود جدید ثبت شود، ابتدا خروج قبلی را ثبت کن."
        )
        return
    db_execute(
        "INSERT INTO attendance(user_id, location, entry_time, exit_time, auto_created) "
        "VALUES (?, ?, ?, NULL, 0)",
        (user.id, location, ts),
    )
    await query.message.reply_text(f"✅ ورود به {location} در {ts} ثبت شد.")


async def handle_exit(query, context, location: str):
    user = query.from_user
    ts = now_iso()
    rows = db_execute(
        "SELECT id, entry_time FROM attendance "
        "WHERE user_id=? AND location=? AND exit_time IS NULL "
        "ORDER BY id DESC LIMIT 1",
        (user.id, location),
        fetch=True,
    )
    if rows:
        rid, entry_time = rows[0]
        db_execute("UPDATE attendance SET exit_time=? WHERE id=?", (ts, rid))
        # compute duration
        try:
            start = datetime.fromisoformat(entry_time)
            end = datetime.fromisoformat(ts)
            delta = end - start
            human = str(delta).split(".")[0]
        except Exception:
            human = "—"
        await query.message.reply_text(
            f"✅ خروج از {location} در {ts} ثبت شد.\n"
            f"مدت زمان این بازه: {human}"
        )
    else:
        # no open entry -> ask to auto-create
        kb = [
            [
                InlineKeyboardButton(
                    "ثبت ورود و خروج خودکار",
                    callback_data=f"confirm:auto_entry|{user.id}|{location}",
                )
            ],
            [InlineKeyboardButton("لغو", callback_data="action:back")],
        ]
        await query.message.reply_text(
            "برای این لوکیشن ورود ثبت نشده است. "
            "می‌خواهی یک ورود خودکار همان لحظه ساخته شود و سپس خروج ثبت شود؟",
            reply_markup=InlineKeyboardMarkup(kb),
        )


async def confirm_auto_entry(query, context, user_id: int, location: str):
    ts = now_iso()
    db_execute(
        "INSERT INTO attendance(user_id, location, entry_time, exit_time, auto_created) "
        "VALUES (?, ?, ?, ?, 1)",
        (user_id, location, ts, ts),
    )
    await query.message.reply_text(
        f"ورود و خروج خودکار برای {location} در {ts} ثبت شد (auto_created)."
    )


# --- Notes session start/stop ---
async def start_note_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_execute(
        "INSERT OR REPLACE INTO note_sessions(user_id, active) VALUES (?,1)",
        (user.id,),
    )
    kb = [
        [KeyboardButton("پایان یادداشت")],
        [KeyboardButton("گزارش یادداشت روز")],
    ]
    await update.message.reply_text(
        "حالت یادداشت فعال شد — هر پیامی که الان ارسال کنی "
        "به عنوان یادداشت روزانه ذخیره می‌شود.\n"
        "برای خروج از حالت یادداشت، دکمه «پایان یادداشت» را بزن.",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )


async def end_note_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_execute(
        "INSERT OR REPLACE INTO note_sessions(user_id, active) VALUES (?,0)",
        (user.id,),
    )
    await update.message.reply_text(
        "حالت یادداشت غیرفعال شد.",
        reply_markup=ReplyKeyboardRemove(),
    )


async def handle_note_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """اگر حالت یادداشت فعال باشد، متن را به عنوان یادداشت ذخیره می‌کند."""
    user = update.effective_user
    rows = db_execute(
        "SELECT active FROM note_sessions WHERE user_id=?",
        (user.id,),
        fetch=True,
    )
    active = bool(rows and rows[0][0] == 1)
    if active:
        msg = update.message.text
        db_execute(
            "INSERT INTO daily_notes(user_id, note_date, time, message) "
            "VALUES (?,?,?,?)",
            (user.id, today_str(), time_str(), msg),
        )
        await update.message.reply_text("یادداشت ذخیره شد.")
    else:
        await update.message.reply_text(
            "برای ذخیره یادداشت ابتدا دکمه «یادداشت روزانه» را بزن."
        )


# --- Reports menu ---
async def send_report_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [KeyboardButton("گزارش روزانه"), KeyboardButton("گزارش هفتگی")],
        [KeyboardButton("گزارش ماهانه"), KeyboardButton("خروجی Excel/PDF")],
    ]
    await update.message.reply_text(
        "کدام گزارش را می‌خواهی؟",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )


def format_duration(td: timedelta):
    total_seconds = int(td.total_seconds())
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    return f"{hours} ساعت و {minutes} دقیقه"


def calc_stats_for_period(user_id, start_date: date, end_date: date):
    s = start_date.isoformat()
    e = (end_date + timedelta(days=1)).isoformat()
    rows = db_execute(
        "SELECT location, entry_time, exit_time FROM attendance "
        "WHERE user_id=? AND entry_time>=? AND entry_time<?",
        (user_id, s, e),
        fetch=True,
    )
    stats = {}
    total = timedelta()
    for loc in LOCATIONS:
        stats[loc] = {"intervals": [], "total": timedelta()}

    for loc, ent, ex in rows:
        if ex is None:
            ex_dt = datetime.fromisoformat(end_date.isoformat() + "T23:59:59")
        else:
            ex_dt = datetime.fromisoformat(ex)
        ent_dt = datetime.fromisoformat(ent)
        dur = ex_dt - ent_dt
        if loc not in stats:
            stats[loc] = {"intervals": [], "total": timedelta()}
        stats[loc]["intervals"].append((ent_dt, ex_dt, dur))
        stats[loc]["total"] += dur
        total += dur
    return stats, total


async def generate_text_report(
    update: Update, context: ContextTypes.DEFAULT_TYPE, period="daily"
):
    user = update.effective_user
    if period == "daily":
        sd = date.today()
        ed = sd
        title = f"گزارش روزانه — {sd.isoformat()}"
    elif period == "weekly":
        today = date.today()
        sd = today - timedelta(days=today.weekday())
        ed = sd + timedelta(days=6)
        title = f"گزارش هفتگی — {sd.isoformat()} تا {ed.isoformat()}"
    else:
        today = date.today()
        sd = today.replace(day=1)
        if sd.month == 12:
            ed = sd.replace(year=sd.year + 1, month=1, day=1) - timedelta(days=1)
        else:
            ed = sd.replace(month=sd.month + 1, day=1) - timedelta(days=1)
        title = f"گزارش ماهانه — {sd.isoformat()} تا {ed.isoformat()}"

    stats, total = calc_stats_for_period(user.id, sd, ed)
    lines = [title, ""]
    for loc in LOCATIONS:
        loc_total = stats.get(loc, {}).get("total", timedelta())
        lines.append(f"• {loc}: {format_duration(loc_total)}")
        intervals = stats.get(loc, {}).get("intervals", [])
        for ent, ex, dur in intervals:
            lines.append(
                f"    {ent.strftime('%H:%M:%S')} → "
                f"{ex.strftime('%H:%M:%S')} = {format_duration(dur)}"
            )
    lines.append("")
    lines.append(f"⏱ مجموع کل حضور در بازه: {format_duration(total)}")
    await update.message.reply_text(
        "\n".join(lines), reply_markup=ReplyKeyboardRemove()
    )


async def generate_excel_report(
    update: Update, context: ContextTypes.DEFAULT_TYPE, period="daily"
):
    user = update.effective_user
    if period == "daily":
        sd = date.today()
        ed = sd
        fname = f"report_daily_{sd.isoformat()}.xlsx"
    elif period == "weekly":
        today = date.today()
        sd = today - timedelta(days=today.weekday())
        ed = sd + timedelta(days=6)
        fname = f"report_weekly_{sd.isoformat()}_to_{ed.isoformat()}.xlsx"
    else:
        today = date.today()
        sd = today.replace(day=1)
        if sd.month == 12:
            ed = sd.replace(year=sd.year + 1, month=1, day=1) - timedelta(days=1)
        else:
            ed = sd.replace(month=sd.month + 1, day=1) - timedelta(days=1)
        fname = f"report_monthly_{sd.isoformat()}_to_{ed.isoformat()}.xlsx"

    s = sd.isoformat()
    e = (ed + timedelta(days=1)).isoformat()
    rows = db_execute(
        "SELECT location, entry_time, exit_time FROM attendance "
        "WHERE user_id=? AND entry_time>=? AND entry_time<?",
        (user.id, s, e),
        fetch=True,
    )
    df = pd.DataFrame(rows, columns=["location", "entry_time", "exit_time"])

    def comp(row):
        try:
            if row["exit_time"]:
                a = datetime.fromisoformat(row["entry_time"])
                b = datetime.fromisoformat(row["exit_time"])
            else:
                a = datetime.fromisoformat(row["entry_time"])
                b = datetime.fromisoformat(row["entry_time"])
            return (b - a).total_seconds() / 3600
        except Exception:
            return 0

    if not df.empty:
        df["hours"] = df.apply(comp, axis=1)
    else:
        df = pd.DataFrame(columns=["location", "entry_time", "exit_time", "hours"])

    bio = io.BytesIO()
    with pd.ExcelWriter(bio, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="attendance")
    bio.seek(0)
    await update.message.reply_document(document=bio, filename=fname)


async def generate_pdf_report(
    update: Update, context: ContextTypes.DEFAULT_TYPE, period="daily"
):
    user = update.effective_user
    if period == "daily":
        sd = date.today()
        ed = sd
        title = f"گزارش روزانه — {sd.isoformat()}"
        fname = f"report_daily_{sd.isoformat()}.pdf"
    elif period == "weekly":
        today = date.today()
        sd = today - timedelta(days=today.weekday())
        ed = sd + timedelta(days=6)
        title = f"گزارش هفتگی — {sd.isoformat()} تا {ed.isoformat()}"
        fname = f"report_weekly_{sd.isoformat()}_to_{ed.isoformat()}.pdf"
    else:
        today = date.today()
        sd = today.replace(day=1)
        if sd.month == 12:
            ed = sd.replace(year=sd.year + 1, month=1, day=1) - timedelta(days=1)
        else:
            ed = sd.replace(month=sd.month + 1, day=1) - timedelta(days=1)
        title = f"گزارش ماهانه — {sd.isoformat()} تا {ed.isoformat()}"
        fname = f"report_monthly_{sd.isoformat()}_to_{ed.isoformat()}.pdf"

    stats, total = calc_stats_for_period(user.id, sd, ed)
    bio = io.BytesIO()
    c = canvas.Canvas(bio, pagesizes=A4)
    w, h = A4
    y = h - 50
    c.setFont("Helvetica-Bold", 14)
    c.drawString(50, y, title)
    y -= 25
    c.setFont("Helvetica", 11)
    for loc in LOCATIONS:
        loc_total = stats.get(loc, {}).get("total", timedelta())
        line = f"{loc}: {format_duration(loc_total)}"
        c.drawString(60, y, line)
        y -= 18
        intervals = stats.get(loc, {}).get("intervals", [])
        for ent, ex, dur in intervals:
            sline = (
                f"   {ent.strftime('%H:%M:%S')} -> "
                f"{ex.strftime('%H:%M:%S')} = {format_duration(dur)}"
            )
            c.drawString(70, y, sline)
            y -= 14
            if y < 80:
                c.showPage()
                y = h - 50
    c.drawString(50, y - 10, f"مجموع کل: {format_duration(total)}")
    c.save()
    bio.seek(0)
    await update.message.reply_document(document=bio, filename=fname)


async def send_daily_notes_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    d = today_str()
    rows = db_execute(
        "SELECT time, message FROM daily_notes "
        "WHERE user_id=? AND note_date=? ORDER BY id",
        (user.id, d),
        fetch=True,
    )
    if not rows:
        await update.message.reply_text("هیچ یادداشتی برای امروز ثبت نشده است.")
        return
    lines = [f"گزارش یادداشت‌های روز — {d}", ""]
    for t, m in rows:
        lines.append(f"{t} — {m}")
    await update.message.reply_text("\n".join(lines))


# --- Quick text handler (reports, exports, notes) ---
async def handle_quick_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    if text == "گزارش روزانه":
        await generate_text_report(update, context, period="daily")
    elif text == "گزارش هفتگی":
        await generate_text_report(update, context, period="weekly")
    elif text == "گزارش ماهانه":
        await generate_text_report(update, context, period="monthly")
    elif text == "خروجی Excel/PDF":
        kb = [
            [KeyboardButton("Excel روزانه"), KeyboardButton("PDF روزانه")],
            [KeyboardButton("Excel هفتگی"), KeyboardButton("PDF هفتگی")],
            [KeyboardButton("بازگشت")],
        ]
        await update.message.reply_text(
            "فرمت گزارش را انتخاب کن:",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
        )
    elif text == "Excel روزانه":
        await generate_excel_report(update, context, period="daily")
    elif text == "PDF روزانه":
        await generate_pdf_report(update, context, period="daily")
    elif text == "Excel هفتگی":
        await generate_excel_report(update, context, period="weekly")
    elif text == "PDF هفتگی":
        await generate_pdf_report(update, context, period="weekly")
    elif text == "گزارش یادداشت روز":
        await send_daily_notes_report(update, context)
    elif text == "پایان یادداشت":
        await end_note_session(update, context)
    elif text == "بازگشت":
        # برگشت به منوی اصلی
        keyboard = [[KeyboardButton(loc)] for loc in LOCATIONS]
        keyboard.append([KeyboardButton("گزارش‌ها"), KeyboardButton("یادداشت روزانه")])
        reply = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        await update.message.reply_text("بازگشت به منوی اصلی.", reply_markup=reply)


# --- text router ---
async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    # 1) اگر یکی از لوکیشن‌ها بود → inline دکمه ورود/خروج
    if text in LOCATIONS:
        kb = [
            [InlineKeyboardButton("ورود", callback_data=f"action:entry|{text}")],
            [InlineKeyboardButton("خروج", callback_data=f"action:exit|{text}")],
            [InlineKeyboardButton("بازگشت", callback_data="action:back")],
        ]
        await update.message.reply_text(
            f"لوکیشن: {text}\nعملیات مورد نظر را انتخاب کن:",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    # 2) دکمه‌های منو
    if text == "گزارش‌ها":
        await send_report_menu(update, context)
        return
    if text == "یادداشت روزانه":
        await start_note_session(update, context)
        return

    # 3) دکمه‌های گزارش/Excel/PDF/پایان یادداشت/گزارش یادداشت
    quick_buttons = {
        "گزارش روزانه",
        "گزارش هفتگی",
        "گزارش ماهانه",
        "خروجی Excel/PDF",
        "Excel روزانه",
        "PDF روزانه",
        "Excel هفتگی",
        "PDF هفتگی",
        "گزارش یادداشت روز",
        "پایان یادداشت",
        "بازگشت",
    }
    if text in quick_buttons:
        await handle_quick_text(update, context, text)
        return

    # 4) هر متن دیگر → اگر حالت یادداشت فعاله به عنوان note ذخیره می‌شود
    await handle_note_message(update, context)


# --- Callback queries ---
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("action:entry"):
        _, payload = data.split(":", 1)
        _, location = payload.split("|", 1)
        await handle_entry(query, context, location)

    elif data.startswith("action:exit"):
        _, payload = data.split(":", 1)
        _, location = payload.split("|", 1)
        await handle_exit(query, context, location)

    elif data == "action:back":
        keyboard = [[KeyboardButton(loc)] for loc in LOCATIONS]
        keyboard.append([KeyboardButton("گزارش‌ها"), KeyboardButton("یادداشت روزانه")])
        reply = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        await query.message.reply_text("بازگشت به منو اصلی:", reply_markup=reply)

    elif data.startswith("confirm:auto_entry"):
        # data: confirm:auto_entry|user_id|location
        _, rest = data.split(":", 1)  # 'auto_entry|user_id|location'
        parts = rest.split("|", 2)
        if len(parts) == 3:
            _, user_id_s, location = parts
            user_id = int(user_id_s)
            await confirm_auto_entry(query, context, user_id, location)


# --- main for Koyeb / local ---
def main():
    init_db()

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN env var is not set")

    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    webhook_url = os.environ.get("WEBHOOK_URL")
    port = int(os.environ.get("PORT", "8080"))

    if webhook_url:
        logger.info("Starting bot in WEBHOOK mode...")
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=token,
            webhook_url=f"{webhook_url.rstrip('/')}/{token}",
        )
    else:
        logger.info("Starting bot in POLLING mode...")
        app.run_polling()


if __name__ == "__main__":
    main()
