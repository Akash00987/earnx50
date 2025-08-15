# main.py
# Ready-to-run Telegram referral bot (sqlite) — reads secrets from env vars
# NOTE: set BOT_TOKEN and ADMIN_ID and deposit addresses in Render environment settings
import os
import logging
import sqlite3
import time
import threading
from math import floor
from datetime import datetime
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

### ---------------- CONFIG via ENV (do NOT hardcode token in repo) ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")  # set in Render / local env
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))  # set to your numeric Telegram id

# deposit addresses (set in env on Render)
DEPOSIT_ADDRESSES = {
    "SOL": os.getenv("SOL_ADDR", ""),
    "ERC20": os.getenv("ERC20_ADDR", ""),
    "BEP20": os.getenv("BEP20_ADDR", ""),
    "TRC20": os.getenv("TRC20_ADDR", ""),
}

MIN_DEPOSIT = float(os.getenv("MIN_DEPOSIT", "5.0"))
PAYOUT_SECONDS = int(os.getenv("PAYOUT_SECONDS", str(24*3600)))  # default 24h
PAYOUT_MULT_START = float(os.getenv("PAYOUT_MULT_START", "5.0"))
PAYOUT_MULT_MIN = float(os.getenv("PAYOUT_MULT_MIN", "2.0"))
PAYOUT_DECAY_PER_DAY = float(os.getenv("PAYOUT_DECAY_PER_DAY", "0.05"))
REF_BONUS_JOIN = float(os.getenv("REF_BONUS_JOIN", "1.0"))
REF_BONUS_DEPOSIT_PERCENT = float(os.getenv("REF_BONUS_DEPOSIT_PERCENT", "0.20"))
MIN_WITHDRAW = float(os.getenv("MIN_WITHDRAW", "45.0"))
MIN_COUNTED_REFERRALS_FOR_WITHDRAW = int(os.getenv("MIN_COUNTED_REFERRALS_FOR_WITHDRAW", "5"))

DB_FILE = os.getenv("DB_FILE", "bot_data.sqlite")
WORKER_INTERVAL = int(os.getenv("WORKER_INTERVAL", str(6*3600)))  # 6 hours

# safety checks
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN env var not set. Set BOT_TOKEN before running.")

# ---------------- logging ----------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------- DB init ----------------
conn = sqlite3.connect(DB_FILE, check_same_thread=False)
cur = conn.cursor()

# create tables if not exist
cur.execute("""CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)""")
cur.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    balance REAL DEFAULT 0,
    referred_by INTEGER,
    counted_for_referrer INTEGER DEFAULT 0,
    counted_referrals INTEGER DEFAULT 0,
    total_referrals INTEGER DEFAULT 0,
    has_deposited INTEGER DEFAULT 0
)
""")
cur.execute("""
CREATE TABLE IF NOT EXISTS deposits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    amount REAL,
    chain TEXT,
    txid TEXT,
    ts INTEGER,
    status TEXT DEFAULT 'PENDING',
    payout_mult REAL DEFAULT 0,
    payout_ts INTEGER DEFAULT 0
)
""")
cur.execute("""
CREATE TABLE IF NOT EXISTS withdrawals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    amount REAL,
    network TEXT,
    address TEXT,
    ts INTEGER,
    status TEXT DEFAULT 'PENDING'
)
""")
conn.commit()

# set launch timestamp if not present
cur.execute("SELECT v FROM meta WHERE k='launch_ts'")
r = cur.fetchone()
if r:
    launch_ts = int(r[0])
else:
    launch_ts = int(time.time())
    cur.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", ("launch_ts", str(launch_ts)))
    conn.commit()

# ---------------- helpers ----------------
def days_since_launch() -> int:
    return max(0, floor((int(time.time()) - launch_ts) / 86400))

def current_multiplier() -> float:
    mult = PAYOUT_MULT_START - (PAYOUT_DECAY_PER_DAY * days_since_launch())
    if mult < PAYOUT_MULT_MIN:
        mult = PAYOUT_MULT_MIN
    return round(mult, 4)

def db_user(user_id:int):
    cur.execute("SELECT user_id, username, balance, referred_by, counted_for_referrer, counted_referrals, total_referrals, has_deposited FROM users WHERE user_id=?", (user_id,))
    return cur.fetchone()

def db_create_user(user_id:int, username:str, referred_by: int|None):
    cur.execute("INSERT OR IGNORE INTO users (user_id, username, referred_by) VALUES (?,?,?)", (user_id, username, referred_by))
    if referred_by:
        cur.execute("UPDATE users SET total_referrals = total_referrals + 1 WHERE user_id=?", (referred_by,))
    conn.commit()

def db_update_username(user_id:int, username:str):
    cur.execute("UPDATE users SET username=? WHERE user_id=?", (username, user_id))
    conn.commit()

def db_add_balance(user_id:int, amount:float):
    cur.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (amount, user_id))
    conn.commit()

def db_set_counted_for_referrer(user_id:int):
    cur.execute("UPDATE users SET counted_for_referrer=1 WHERE user_id=?", (user_id,))
    conn.commit()

def db_inc_counted_referrals(ref_id:int, inc:int=1):
    cur.execute("UPDATE users SET counted_referrals = counted_referrals + ? WHERE user_id=?", (inc, ref_id))
    conn.commit()

def db_mark_deposited(user_id:int):
    cur.execute("UPDATE users SET has_deposited=1 WHERE user_id=?", (user_id,))
    conn.commit()

def db_insert_deposit(user_id:int, amount:float, chain:str, txid:str):
    ts = int(time.time())
    cur.execute("INSERT INTO deposits (user_id, amount, chain, txid, ts) VALUES (?,?,?,?,?)", (user_id, amount, chain, txid, ts))
    conn.commit()
    return cur.lastrowid

def db_get_pending_deposits():
    cur.execute("SELECT id, user_id, amount, chain, txid, ts FROM deposits WHERE status='PENDING'")
    return cur.fetchall()

def db_get_deposit(dep_id:int):
    cur.execute("SELECT id, user_id, amount, chain, txid, ts, status, payout_mult FROM deposits WHERE id=?", (dep_id,))
    return cur.fetchone()

def db_approve_deposit(dep_id:int, payout_mult:float):
    cur.execute("UPDATE deposits SET status='APPROVED', payout_mult=? WHERE id=?", (payout_mult, dep_id))
    conn.commit()

def db_reject_deposit(dep_id:int):
    cur.execute("UPDATE deposits SET status='REJECTED' WHERE id=?", (dep_id,))
    conn.commit()

def db_mark_deposit_paid(dep_id:int):
    ts = int(time.time())
    cur.execute("UPDATE deposits SET status='PAID', payout_ts=? WHERE id=?", (ts, dep_id))
    conn.commit()

def db_insert_withdrawal(user_id:int, amount:float, network:str, address:str):
    ts = int(time.time())
    cur.execute("INSERT INTO withdrawals (user_id, amount, network, address, ts) VALUES (?,?,?,?,?)", (user_id, amount, network, address, ts))
    conn.commit()
    return cur.lastrowid

def db_get_pending_withdrawals():
    cur.execute("SELECT id, user_id, amount, network, address, ts FROM withdrawals WHERE status='PENDING'")
    return cur.fetchall()

def db_update_withdrawal_status(req_id:int, status:str):
    cur.execute("UPDATE withdrawals SET status=? WHERE id=?", (status, req_id))
    conn.commit()

# ---------------- business logic ----------------
def handle_new_ref_join(referrer_id:int, new_user_id:int):
    ref = db_user(referrer_id)
    newu = db_user(new_user_id)
    if not ref or not newu:
        return None
    counted_referrals = ref[5]
    already_counted = newu[4]
    if counted_referrals < 3 and already_counted == 0:
        # immediate count + $1
        db_set_counted_for_referrer(new_user_id)
        db_inc_counted_referrals(referrer_id, 1)
        db_add_balance(referrer_id, REF_BONUS_JOIN)
        return ("counted", REF_BONUS_JOIN)
    return ("queued", 0.0)

def handle_deposit_approved(dep_id:int):
    dep = db_get_deposit(dep_id)
    if not dep:
        return None
    _, user_id, amount, chain, txid, ts, status, payout_mult = dep
    mult = current_multiplier()
    db_approve_deposit(dep_id, mult)
    db_mark_deposited(user_id)
    user = db_user(user_id)
    if user:
        ref = user[3]
        counted_flag = user[4]
        if ref:
            # if not counted earlier, count now
            if counted_flag == 0:
                db_set_counted_for_referrer(user_id)
                db_inc_counted_referrals(ref, 1)
            # give 20% bonus to referrer immediately
            bonus = round(amount * REF_BONUS_DEPOSIT_PERCENT, 6)
            db_add_balance(ref, bonus)
            return ("approved_ref_bonus", bonus, mult)
    return ("approved_no_ref", 0.0, mult)

# ---------------- payout worker (credits matured deposits) ----------------
def payout_worker(app: Application):
    while True:
        try:
            logger.info("Worker: checking matured deposits...")
            now = int(time.time())
            cutoff = now - PAYOUT_SECONDS
            cur.execute("SELECT id, user_id, amount, payout_mult, ts FROM deposits WHERE status='APPROVED' AND payout_ts=0 AND ts<=?", (cutoff,))
            rows = cur.fetchall()
            for dep in rows:
                dep_id, user_id, amount, payout_mult, ts = dep
                use_mult = payout_mult if payout_mult and payout_mult > 0 else current_multiplier()
                payout_amount = round(amount * use_mult, 6)
                db_add_balance(user_id, payout_amount)
                db_mark_deposit_paid(dep_id)
                try:
                    app.bot.send_message(user_id, f"✅ Your deposit ${amount:.2f} matured. ${payout_amount:.2f} credited (multiplier {use_mult}x).")
                except Exception as e:
                    logger.exception("Notify failed: %s", e)
        except Exception as e:
            logger.exception("Worker exception: %s", e)
        time.sleep(WORKER_INTERVAL)

# ---------------- BOT Handlers ----------------
# withdraw conversation states
W_ASK_AMOUNT, W_ASK_NETWORK, W_ASK_ADDRESS = range(3)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args
    refcode = None
    if len(args) >= 1:
        try:
            refcode = int(args[0])
        except:
            refcode = None
    db_create_user(user.id, user.username or user.full_name, refcode)
    db_update_username(user.id, user.username or user.full_name)
    if refcode:
        res = handle_new_ref_join(refcode, user.id)
    mult = current_multiplier()
    short = (
        "Welcome!\n\n"
        "Referral rules (short):\n"
        "• First 3 referrals count immediately (no deposit required) and give $1 each.\n"
        "• After first 3, new referrals count only when the referred user makes a $5+ deposit (admin approves).\n"
        "• For each approved deposit by your referral, you receive 20% of that deposit immediately.\n\n"
        f"Deposit & payout: current multiplier = {mult}x (starts 5x and decays 0.05/day down to 2x).\n\n"
        "Withdrawal rules (revealed after your first approved deposit):\n"
        f"• Minimum counted referrals: {MIN_COUNTED_REFERRALS_FOR_WITHDRAW}\n"
        f"• Minimum withdraw balance: ${MIN_WITHDRAW}\n"
    )
    text = f"Hello {user.first_name}!\n\n{short}"
    if refcode:
        if res and res[0] == "counted":
            text += f"\n🎉 Your join was counted & your referrer received ${res[1]:.2f}\n"
        else:
            text += "\nReferral recorded; it will count when deposit is approved (if needed).\n"
    kb = [
        [InlineKeyboardButton("💰 Deposit", callback_data="menu_deposit")],
        [InlineKeyboardButton("💸 Withdraw", callback_data="menu_withdraw")],
        [InlineKeyboardButton("🔎 Balance", callback_data="menu_balance"), InlineKeyboardButton("📜 History", callback_data="menu_history")],
        [InlineKeyboardButton("ℹ️ About Us", callback_data="menu_about")]
    ]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))

async def cb_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    if data == "menu_deposit":
        text = "💰 Deposit addresses (click button to receive the address message to copy):"
        kb = [[InlineKeyboardButton(k, callback_data=f"addr:{k}")] for k in DEPOSIT_ADDRESSES.keys()]
        await q.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))
    elif data == "menu_withdraw":
        await q.message.reply_text("Start withdraw with /withdraw")
    elif data == "menu_balance":
        row = db_user(q.from_user.id)
        if not row:
            await q.message.reply_text("Register first with /start")
            return
        await q.message.reply_text(f"💰 Balance: ${row[2]:.2f}\nCounted referrals: {row[5]}")
    elif data == "menu_history":
        uid = q.from_user.id
        cur.execute("SELECT id, amount, chain, txid, ts, status FROM deposits WHERE user_id=? ORDER BY ts DESC LIMIT 10", (uid,))
        deps = cur.fetchall()
        text = "Deposits:\n"
        if deps:
            for d in deps:
                t = datetime.fromtimestamp(d[4]).strftime("%Y-%m-%d %H:%M")
                text += f"#{d[0]} {d[2]} ${d[1]:.2f} TX:{d[3]} {t} {d[5]}\n"
        else:
            text += "None\n"
        cur.execute("SELECT id, amount, network, address, ts, status FROM withdrawals WHERE user_id=? ORDER BY ts DESC LIMIT 10", (uid,))
        wds = cur.fetchall()
        text += "\nWithdrawals:\n"
        if wds:
            for w in wds:
                t = datetime.fromtimestamp(w[4]).strftime("%Y-%m-%d %H:%M")
                text += f"#{w[0]} {w[2]} ${w[1]:.2f} {w[3]} {t} {w[5]}\n"
        else:
            text += "None\n"
        await q.message.reply_text(text)
    elif data == "menu_about":
        about = (
            "❤️ About Us\n"
            "We are a team of normal people, just like you, who started small but achieved big results.\n"
            "Our mission is simple — help others multiply their income in a safe and fair way.\n"
            "We value honesty, transparency, and timely payouts.\n"
            "Your trust is our most valuable asset — join us and grow together."
        )
        await q.message.reply_text(about)

async def cb_addr_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chain = q.data.split(":",1)[1]
    addr = DEPOSIT_ADDRESSES.get(chain)
    if addr:
        await q.message.reply_text(f"{chain} deposit address:\n`{addr}`\n\nLong-press to copy.", parse_mode="Markdown")
    else:
        await q.message.reply_text("Address not set by admin. Contact admin.")

# deposit short
async def cmd_deposit_short(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = context.args
    if len(parts) < 3:
        await update.message.reply_text("Usage: /deposit <amount> <chain> <txid>")
        return
    try:
        amount = float(parts[0])
    except:
        await update.message.reply_text("Invalid amount")
        return
    chain = parts[1].upper()
    txid = parts[2]
    if chain not in DEPOSIT_ADDRESSES:
        await update.message.reply_text(f"Chain must be one of: {', '.join(DEPOSIT_ADDRESSES.keys())}")
        return
    if amount < MIN_DEPOSIT:
        await update.message.reply_text(f"Min deposit is ${MIN_DEPOSIT:.2f}")
        return
    user = update.effective_user
    dep_id = db_insert_deposit(user.id, amount, chain, txid)
    await update.message.reply_text(f"Deposit recorded #{dep_id}. Wait for admin approval.")
    approve_btn = InlineKeyboardButton("✅ Approve", callback_data=f"approve_deposit:{dep_id}")
    reject_btn = InlineKeyboardButton("❌ Reject", callback_data=f"reject_deposit:{dep_id}")
    await context.bot.send_message(ADMIN_ID, f"New deposit #{dep_id}\nUser @{user.username} ({user.id})\n${amount:.2f} {chain}\nTX:{txid}", reply_markup=InlineKeyboardMarkup([[approve_btn, reject_btn]]))

# withdraw conv
async def cmd_withdraw_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    row = db_user(user.id)
    if not row:
        await update.message.reply_text("Register first with /start")
        return ConversationHandler.END
    if row[7] == 0:
        await update.message.reply_text("Withdrawal rules are revealed only after your first approved deposit.")
        return ConversationHandler.END
    await update.message.reply_text("Enter withdrawal amount (USD):")
    return W_ASK_AMOUNT

async def withdraw_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.strip())
    except:
        await update.message.reply_text("Invalid amount. Enter a number.")
        return W_ASK_AMOUNT
    user = update.effective_user
    row = db_user(user.id)
    if row[2] < MIN_WITHDRAW:
        await update.message.reply_text(f"Minimum withdraw balance: ${MIN_WITHDRAW:.2f}. Your balance: ${row[2]:.2f}")
        return ConversationHandler.END
    if row[5] < MIN_COUNTED_REFERRALS_FOR_WITHDRAW:
        await update.message.reply_text(f"Minimum counted referrals: {MIN_COUNTED_REFERRALS_FOR_WITHDRAW}. Your counted: {row[5]}")
        return ConversationHandler.END
    if amount > row[2]:
        await update.message.reply_text("Insufficient balance.")
        return ConversationHandler.END
    context.user_data["wd_amount"] = amount
    kb = [[InlineKeyboardButton("TRC20", callback_data="wd:TRC20")],
          [InlineKeyboardButton("ERC20", callback_data="wd:ERC20")],
          [InlineKeyboardButton("BEP20", callback_data="wd:BEP20")]]
    await update.message.reply_text("Choose network:", reply_markup=InlineKeyboardMarkup(kb))
    return W_ASK_NETWORK

async def withdraw_network(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    net = q.data.split(":",1)[1]
    context.user_data["wd_network"] = net
    await q.message.reply_text(f"Send your {net} withdrawal address:")
    return W_ASK_ADDRESS

async def withdraw_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    addr = update.message.text.strip()
    user = update.effective_user
    amount = context.user_data.get("wd_amount")
    net = context.user_data.get("wd_network")
    req_id = db_insert_withdrawal(user.id, amount, net, addr)
    await update.message.reply_text("✅ Withdrawal request submitted. Wait for admin approval.")
    approve_btn = InlineKeyboardButton("✅ Approve", callback_data=f"approve_withdraw:{req_id}")
    reject_btn = InlineKeyboardButton("❌ Decline", callback_data=f"decline_withdraw:{req_id}")
    await context.bot.send_message(ADMIN_ID, f"Withdrawal #{req_id}\nUser @{user.username} ({user.id})\n${amount:.2f} {net}\nAddr:{addr}", reply_markup=InlineKeyboardMarkup([[approve_btn, reject_btn]]))
    return ConversationHandler.END

# admin inline callbacks
async def cb_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    if data.startswith("approve_deposit:") or data.startswith("reject_deposit:"):
        cmd, depid = data.split(":",1)
        depid = int(depid)
        dep = db_get_deposit(depid)
        if not dep:
            await q.message.reply_text("Deposit not found.")
            return
        if cmd == "approve_deposit":
            res = handle_deposit_approved(depid)
            await q.message.reply_text(f"Deposit #{depid} approved: {res}")
            _, user_id, amount, chain, txid, ts, status, payout_mult = dep
            try:
                await context.bot.send_message(user_id, f"✅ Your deposit ${amount:.2f} approved. It will be paid after maturity. Current multiplier: {current_multiplier()}x")
            except:
                pass
        else:
            db_reject_deposit(depid)
            await q.message.reply_text(f"Deposit #{depid} rejected.")
            _, user_id, amount, chain, txid, ts, status, payout_mult = dep
            try:
                await context.bot.send_message(user_id, f"❌ Your deposit #{depid} rejected.")
            except:
                pass
    elif data.startswith("approve_withdraw:") or data.startswith("decline_withdraw:"):
        cmd, reqid = data.split(":",1)
        reqid = int(reqid)
        cur.execute("SELECT id, user_id, amount, network, address, status FROM withdrawals WHERE id=?", (reqid,))
        row = cur.fetchone()
        if not row:
            await q.message.reply_text("Request not found.")
            return
        if cmd == "approve_withdraw":
            uid = row[1]; amt = row[2]
            cur.execute("SELECT balance FROM users WHERE user_id=?", (uid,))
            rr = cur.fetchone()
            if not rr or rr[0] < amt:
                await q.message.reply_text("User has insufficient balance.")
                await context.bot.send_message(ADMIN_ID, f"Cannot approve #{reqid} - insufficient balance.")
                return
            cur.execute("UPDATE users SET balance = balance - ? WHERE user_id=?", (amt, uid))
            db_update_withdrawal_status(reqid, "PAID")
            conn.commit()
            await q.message.reply_text(f"Withdrawal #{reqid} approved and marked PAID.")
            try:
                await context.bot.send_message(uid, f"✅ Your withdrawal #{reqid} approved. Please check your wallet.")
            except:
                pass
        else:
            db_update_withdrawal_status(reqid, "REJECTED")
            await q.message.reply_text(f"Withdrawal #{reqid} declined.")
            uid = row[1]
            try:
                await context.bot.send_message(uid, f"❌ Your withdrawal #{reqid} declined.")
            except:
                pass

# admin command to list pending requests
async def cmd_requests(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Unauthorized.")
        return
    deps = db_get_pending_deposits()
    wds = db_get_pending_withdrawals()
    txt = "Pending Deposits:\n"
    if deps:
        for d in deps:
            t = datetime.fromtimestamp(d[5]).strftime("%Y-%m-%d %H:%M")
            txt += f"#{d[0]} User:{d[1]} ${d[2]:.2f} {d[3]} TX:{d[4]} {t}\n"
    else:
        txt += "None\n"
    txt += "\nPending Withdrawals:\n"
    if wds:
        for w in wds:
            t = datetime.fromtimestamp(w[5]).strftime("%Y-%m-%d %H:%M")
            txt += f"#{w[0]} User:{w[1]} ${w[2]:.2f} {w[3]} Addr:{w[4]} {t}\n"
    else:
        txt += "None\n"
    await update.message.reply_text(txt)

async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    row = db_user(uid)
    if not row:
        await update.message.reply_text("Use /start first")
        return
    await update.message.reply_text(f"Balance: ${row[2]:.2f}\nCounted referrals: {row[5]}")

async def cmd_addresses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = "Deposit addresses:\n"
    for k,v in DEPOSIT_ADDRESSES.items():
        txt += f"{k}: `{v}`\n"
    await update.message.reply_text(txt, parse_mode="Markdown")

async def cmd_about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    about = ("❤️ About Us\nWe are a team of normal people, just like you, who started small but achieved big results.\n"
             "Our mission is simple — help others multiply their income in a safe and fair way.\n"
             "We value honesty, transparency, and timely payouts.\nYour trust is our most valuable asset — join us and grow together.")
    await update.message.reply_text(about)

# ---------------- start app ----------------
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(cb_menu, pattern="^menu_"))
    app.add_handler(CallbackQueryHandler(cb_addr_click, pattern="^addr:"))
    app.add_handler(CommandHandler("deposit", cmd_deposit_short))
    app.add_handler(CommandHandler("requests", cmd_requests))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("addresses", cmd_addresses))
    app.add_handler(CommandHandler("about", cmd_about))

    conv_withdraw = ConversationHandler(
        entry_points=[CommandHandler("withdraw", cmd_withdraw_start)],
        states={
            W_ASK_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_amount)],
            W_ASK_NETWORK: [CallbackQueryHandler(withdraw_network, pattern="^wd:")],
            W_ASK_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_address)],
        },
        fallbacks=[]
    )
    app.add_handler(conv_withdraw)

    app.add_handler(CallbackQueryHandler(cb_admin, pattern="^(approve_deposit|reject_deposit|approve_withdraw|decline_withdraw):"))
    # worker thread
    worker = threading.Thread(target=payout_worker, args=(app,), daemon=True)
    worker.start()

    logger.info("Bot started")
    app.run_polling()

if __name__ == "__main__":
    main()
