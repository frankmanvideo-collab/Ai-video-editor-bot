from __future__ import annotations

import asyncio
import logging
import threading
import time
from urllib.parse import quote

import qrcode
from flask import Flask, jsonify, redirect
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from config import *
from db import (
    approve_manual_recharge_and_credit,
    clear_session,
    fail_manual_recharge,
    get_manual_recharge,
    get_user,
    increment_manual_attempt,
    init_db,
    load_session,
    manual_utr_exists,
    mark_manual_submitted,
    reject_manual_recharge,
    save_session,
    set_balance,
    stats,
    submit_manual_utr,
)
from jobs import enqueue, service_max, service_price, worker_loop
import jobs
from keyboards import *
from payments import create_manual_recharge_request, create_order, handle_payment_webhook, validate_utr_format
from utils import cleanup_old_files, rupees_str

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(name)s | %(message)s", level=logging.INFO)
logger = logging.getLogger("GodModeV3")
flask_app = Flask(__name__)


@flask_app.route("/health")
def health():
    return jsonify({"status": "ok", "version": BOT_VERSION}), 200


@flask_app.route("/webhook/payment", methods=["POST"])
def payment_webhook():
    return handle_payment_webhook()


@flask_app.route("/upi/<request_id>")
def upi_deeplink(request_id: str):
    req = get_manual_recharge(request_id)
    if not req:
        return "Invalid or expired recharge request", 404
    amount_rs = float(req["amount_paisa"]) / 100
    upi_url = (
        f"upi://pay?pa={MANUAL_UPI_ID}"
        f"&pn={quote(MANUAL_UPI_NAME)}"
        f"&am={amount_rs:.2f}"
        f"&tn={quote(str(req['secret_code']))}"
    )
    return redirect(upi_url, code=302)


def run_flask():
    try:
        from waitress import serve
        serve(flask_app, host="0.0.0.0", port=FLASK_PORT)
    except Exception:
        flask_app.run(host="0.0.0.0", port=FLASK_PORT, debug=False, use_reloader=False)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    get_user(u.id, u.username or "")
    text = (
        "🎬 AI Reel Growth Editor\n\n"
        "Not just captions. I help you fix reels, create customer-getting content, "
        "and edit videos faster.\n\n"
        "Choose what you want to do:"
    )
    await update.message.reply_text(text, reply_markup=main_menu_keyboard())


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "❓ How It Works\n\n"
        "1️⃣ Send a raw video (max 2 min / 150MB)\n"
        "2️⃣ Choose: Roast, Edit, Free Sample or Customer Pack\n"
        "3️⃣ Pick platform, niche, goal, hook, CTA and style\n"
        "4️⃣ Confirm order\n"
        "5️⃣ Bot shows progress until your output/report is ready\n\n"
        "You get: viral score, mistakes, hooks, caption, hashtags, CTA and edited reel."
    )
    await update.message.reply_text(text, reply_markup=main_menu_keyboard())


async def support_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"📞 Support\n\nEmail: {SUPPORT_EMAIL}\n\n"
        "For payment issue send:\n• Telegram ID\n• Recharge ID\n• UTR/RRN\n• Screenshot if available",
        reply_markup=main_menu_keyboard(),
    )


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    d = get_user(u.id)
    await update.message.reply_text(
        f"👛 Balance: {rupees_str(d['balance_paisa'])}\n"
        f"🎁 Free Sample: {'Used' if d['free_sample_used'] else 'Available'}"
    )


async def recharge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("💳 Choose recharge amount:", reply_markup=recharge_keyboard())


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        return
    s = stats()
    q = jobs.video_queue.qsize() if jobs.video_queue else 0
    await update.message.reply_text(
        f"Users: {s['users']}\nRevenue: {rupees_str(s['revenue'])}\n"
        f"Wallets: {rupees_str(s['wallet'])}\nDB Jobs: {s['jobs']}\nQueue: {q}"
    )


async def setbalance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /setbalance user_id amount_rs")
        return
    uid = int(context.args[0])
    amt = int(float(context.args[1]) * 100)
    set_balance(uid, amt, "admin")
    await update.message.reply_text(f"Set {uid} to {rupees_str(amt)}")


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    msg = update.message
    vid = msg.video or msg.document
    if not vid:
        return
    size = (vid.file_size or 0) / (1024 * 1024)
    if size > MAX_FILE_SIZE_MB:
        await msg.reply_text(
            f"❌ Video too large\nMax allowed: {MAX_FILE_SIZE_MB}MB\n"
            f"Best: MP4 under {MAX_FILE_SIZE_MB}MB, up to 2 minutes."
        )
        return
    mime = getattr(vid, "mime_type", "") or ""
    if mime and mime not in ALLOWED_VIDEO_MIMES:
        await msg.reply_text("❌ Unsupported video format. Please send MP4/MOV/WebM.")
        return
    sess = {"video_file_id": vid.file_id, "screenshots": [], "awaiting_custom_cta": False}
    save_session(u.id, sess)
    d = get_user(u.id)
    await msg.reply_text(
        "🎬 Video received!\n\n✅ File accepted\n⏱ Max allowed: 2 min\n📦 Max size: 150MB\n\nStep 1/9 — Choose service:",
        reply_markup=service_keyboard(not d["free_sample_used"]),
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    sess = load_session(u.id)
    if not sess:
        await update.message.reply_text("Send video first.")
        return
    shots = sess.get("screenshots", [])
    if len(shots) >= 3:
        await update.message.reply_text("Max 3 screenshots.")
        return
    shots.append(update.message.photo[-1].file_id)
    sess["screenshots"] = shots
    save_session(u.id, sess)
    await update.message.reply_text(f"Screenshot {len(shots)}/3 added.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    sess = load_session(u.id) or {}
    text = (update.message.text or "").strip()

    if sess.get("awaiting_manual_utr"):
        req_id = sess.get("manual_recharge_request_id")
        ok, val = validate_utr_format(text)
        if not ok:
            attempts = increment_manual_attempt(req_id) if req_id else 0
            if attempts >= 5:
                if req_id:
                    fail_manual_recharge(req_id, "FAILED")
                sess.pop("awaiting_manual_utr", None)
                save_session(u.id, sess)
                await update.message.reply_text("❌ Too many wrong attempts. Recharge request cancelled. Contact support.")
                return
            await update.message.reply_text(f"❌ Invalid UTR/RRN: {val}\nAttempts left: {5-attempts}")
            return

        req = get_manual_recharge(req_id) if req_id else None
        if not req:
            await update.message.reply_text("❌ Recharge session expired. Please start /recharge again.")
            clear_session(u.id)
            return
        try:
            if time.time() > time.mktime(time.strptime(req["expires_at"], "%Y-%m-%d %H:%M:%S")):
                fail_manual_recharge(req_id, "EXPIRED")
                clear_session(u.id)
                await update.message.reply_text("❌ Recharge request expired. Please start /recharge again.")
                return
        except Exception:
            pass
        utr = val
        if manual_utr_exists(utr):
            attempts = increment_manual_attempt(req_id) if req_id else 0
            await update.message.reply_text(f"❌ This UTR/RRN is already used. Attempts left: {max(0,5-attempts)}")
            return
        submit_manual_utr(req_id, utr)
        sess["awaiting_manual_utr"] = False
        sess["awaiting_manual_code"] = True
        sess["manual_utr"] = utr
        save_session(u.id, sess)
        await update.message.reply_text(
            "✅ UTR saved. Now send the payment note/code shown in your UPI receipt. "
            "It starts with PAY. Check transaction details/remarks in your UPI app."
        )
        return

    if sess.get("awaiting_manual_code"):
        req_id = sess.get("manual_recharge_request_id")
        req = get_manual_recharge(req_id) if req_id else None
        if not req:
            await update.message.reply_text("❌ Recharge session expired. Please start /recharge again.")
            clear_session(u.id)
            return
        if text.strip().upper() != str(req["secret_code"]).upper():
            attempts = increment_manual_attempt(req_id)
            if attempts >= 5:
                fail_manual_recharge(req_id, "FAILED")
                clear_session(u.id)
                await update.message.reply_text("❌ Wrong code too many times. Recharge request cancelled.")
                return
            await update.message.reply_text(f"❌ Wrong code. Attempts left: {5-attempts}")
            return
        mark_manual_submitted(req_id)
        clear_session(u.id)
        admin_text = (
            f"💳 Manual Recharge Approval Needed\n\n"
            f"User: {u.id} @{u.username or ''}\n"
            f"Amount: {rupees_str(req['amount_paisa'])}\n"
            f"Request ID: {req_id}\n"
            f"UTR/RRN: {req['utr']}\n"
            f"Code matched: YES\n\n"
            f"Verify payment in your UPI/FamPay app, then approve."
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Approve", callback_data=f"manualapprove_{req_id}"), InlineKeyboardButton("❌ Reject", callback_data=f"manualreject_{req_id}")]])
        await context.bot.send_message(ADMIN_USER_ID, admin_text, reply_markup=kb)
        await update.message.reply_text("✅ Submitted for approval. Balance will be added after admin verifies payment.")
        return

    if sess.get("awaiting_custom_cta"):
        sess["cta"] = text[:120]
        sess["awaiting_custom_cta"] = False
        save_session(u.id, sess)
        await update.message.reply_text("✅ Custom CTA saved. Choose style:", reply_markup=style_keyboard())


async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    uid = q.from_user.id

    if data == "menu_main":
        await q.edit_message_text("🎬 AI Reel Growth Editor\n\nChoose what you want to do:", reply_markup=main_menu_keyboard())
        return
    if data == "menu_how":
        await q.edit_message_text(
            "❓ How It Works\n\n"
            "1️⃣ Send video (max 2 min / 150MB)\n"
            "2️⃣ Choose service: Audit, Roast, Edit or Viral Pack\n"
            "3️⃣ Choose platform, niche, goal, hook, CTA and style\n"
            "4️⃣ Confirm and watch live progress\n"
            "5️⃣ Get edited reel/report + caption + hashtags\n\n"
            "Tip: Send vertical MP4 with clear audio for best result.",
            reply_markup=back_main_keyboard(),
        )
        return
    if data == "menu_support":
        await q.edit_message_text(
            f"📞 Support\n\nEmail: {SUPPORT_EMAIL}\n\nFor payment issue send:\n• Telegram ID\n• Recharge ID\n• UTR/RRN\n• Screenshot if available",
            reply_markup=back_main_keyboard(),
        )
        return
    if data == "menu_wallet":
        d = get_user(uid)
        await q.edit_message_text(
            f"👛 Wallet\n\nBalance: {rupees_str(d['balance_paisa'])}\nFree Sample: {'Used' if d['free_sample_used'] else 'Available'}\n\nChoose recharge amount:",
            reply_markup=recharge_keyboard(),
        )
        return
    if data == "menu_get_customers":
        await q.edit_message_text(
            "🎯 Get Customers Pack\n\nFor local businesses/coaches/shops.\n\nYou will get:\n• Customer-getting reel idea\n• Sales hook\n• Caption + hashtags\n• WhatsApp CTA\n• Follow-up message\n\nSend your business details after sending a video, or use Roast/Edit flow first.",
            reply_markup=back_main_keyboard(),
        )
        return
    if data == "menu_copy_edit":
        await q.edit_message_text(
            "🎯 Copy This Edit\n\nUpload your raw video and a reference edited video. The bot will match captions, pacing, motion and style as closely as possible.\n\nThis premium mode will be enabled after final testing.",
            reply_markup=back_main_keyboard(),
        )
        return
    if data in ("menu_start_edit", "menu_audit", "menu_free_sample"):
        if data == "menu_audit":
            await q.edit_message_text("🔥 Send your reel/video now. I will roast it, score it and tell exact mistakes.", reply_markup=back_main_keyboard())
        elif data == "menu_free_sample":
            await q.edit_message_text("🎁 Send a video up to 15 seconds to use your free sample edit.", reply_markup=back_main_keyboard())
        else:
            await q.edit_message_text("🎬 Send your raw video now. Max 2 minutes / 150MB. Best format: vertical MP4.", reply_markup=back_main_keyboard())
        return

    if data.startswith("manualapprove_") or data.startswith("manualreject_"):
        if uid != ADMIN_USER_ID:
            await q.edit_message_text("❌ Admin only.")
            return
        req_id = data.split("_", 1)[1]
        if data.startswith("manualapprove_"):
            result, error = approve_manual_recharge_and_credit(req_id, uid)
            if not result:
                await q.edit_message_text(f"❌ Approval failed: {error}")
                return
            await q.edit_message_text(
                f"✅ Approved {result['request_id']}\n"
                f"Credited {rupees_str(result['amount_paisa'])} to user {result['user_id']}.\n"
                f"New balance: {rupees_str(result['new_balance'])}"
            )
            try:
                await context.bot.send_message(result["user_id"], f"✅ Recharge approved!\n\nCredited: {rupees_str(result['amount_paisa'])}\nNew balance: {rupees_str(result['new_balance'])}")
            except Exception:
                pass
        else:
            req = reject_manual_recharge(req_id, uid)
            if not req:
                await q.edit_message_text("❌ Request already processed or not found.")
                return
            await q.edit_message_text(f"❌ Rejected {req_id}")
            try:
                await context.bot.send_message(req["user_id"], "❌ Recharge rejected. If this is a mistake, contact support.")
            except Exception:
                pass
        return

    if data.startswith("pay_"):
        amount = int(data.split("_")[1])
        if UPIGATEWAY_API_KEY:
            res = create_order(uid, amount)
            if res["ok"]:
                await q.edit_message_text(f"Pay ₹{amount}:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💳 Pay Now", url=res["url"])]]) )
            else:
                await q.edit_message_text("❌ " + res["error"])
            return

        res = create_manual_recharge_request(uid, amount)
        if not res["ok"]:
            await q.edit_message_text("❌ " + res["error"])
            return
        sess = load_session(uid) or {}
        sess["manual_recharge_request_id"] = res["request_id"]
        sess["awaiting_manual_utr"] = True
        sess["awaiting_manual_code"] = False
        save_session(uid, sess)
        public_pay_url = f"{WEBHOOK_URL}/upi/{res['request_id']}" if WEBHOOK_URL else ""
        caption = (
            f"💳 Manual UPI Recharge\n\nAmount: ₹{amount}\nRecharge ID: {res['request_id']}\n\n"
            "Step 1: Scan this QR and pay exact amount.\n"
            "Step 2: After payment, reply with your UTR/RRN number.\n"
            "Step 3: Bot will ask for the payment note/code shown in your UPI receipt.\n"
            "Step 4: Admin verifies and approves balance.\n\n"
            f"Expires at: {res['expires_at']}"
        )
        qr_path = DOWNLOADS_DIR / f"qr_{res['request_id']}.png"
        try:
            qrcode.make(res["upi_url"]).save(qr_path)
            buttons = []
            if public_pay_url:
                buttons.append([InlineKeyboardButton("📲 Open UPI App", url=public_pay_url)])
            await q.message.reply_photo(photo=open(qr_path, "rb"), caption=caption, reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)
            await q.edit_message_text("✅ Recharge QR generated. Please follow the instructions above.")
        except Exception as e:
            logger.warning("QR generation/send failed: %s", e)
            fallback = caption + (f"\n\nOpen payment link:\n{public_pay_url}" if public_pay_url else f"\n\nUPI Link:\n{res['upi_url']}")
            await q.edit_message_text(fallback)
        return

    sess = load_session(uid) or {}
    if data.startswith("svc_"):
        kind = data.replace("svc_", "")
        sess["kind"] = kind
        save_session(uid, sess)
        await q.edit_message_text("Step 2/9 — Choose Platform\n\nWhere will you post this video?", reply_markup=platform_keyboard())
        return
    if data.startswith("platform_"):
        sess["platform"] = data.replace("platform_", "").replace("_", " ").title()
        save_session(uid, sess)
        await q.edit_message_text("Step 3/9 — Choose Niche\n\nWhat is this content about?", reply_markup=niche_keyboard())
        return
    if data.startswith("niche_"):
        sess["niche"] = data.replace("niche_", "").replace("_", " ").title()
        save_session(uid, sess)
        await q.edit_message_text("Step 4/9 — Choose Goal\n\nWhat result do you want from this reel?", reply_markup=goal_keyboard())
        return
    if data.startswith("goal_"):
        sess["goal"] = data.replace("goal_", "").replace("_", " ").title()
        from ai_director import generic_hooks
        hooks = generic_hooks(sess)
        sess["hook_options"] = hooks
        save_session(uid, sess)
        if sess.get("kind") in ("mini_audit", "full_roast"):
            await show_confirm(q, uid, sess)
            return
        await q.edit_message_text("Step 5/9 — Choose Hook\n\nPick the strongest opening line or use Auto.", reply_markup=hook_keyboard(hooks))
        return
    if data.startswith("hook_"):
        v = data.replace("hook_", "")
        if v.isdigit():
            sess["selected_hook"] = sess.get("hook_options", [])[int(v)]
        else:
            sess["selected_hook"] = v
        save_session(uid, sess)
        await q.edit_message_text("Step 6/9 — Choose CTA\n\nWhat should viewers do after watching?", reply_markup=cta_keyboard())
        return
    if data.startswith("cta_"):
        v = data.replace("cta_", "")
        if v == "custom":
            sess["awaiting_custom_cta"] = True
            save_session(uid, sess)
            await q.edit_message_text('Type your custom CTA, e.g. DM "LINK" for details')
            return
        mapping = {"DM_START": 'DM "START" for details', "DM_LINK": 'DM "LINK" for details', "COMMENT_YES": 'Comment "YES" for part 2', "SAVE": "Save this reel", "skip": "skip"}
        sess["cta"] = mapping.get(v, v)
        save_session(uid, sess)
        await q.edit_message_text("Step 7/9 — Choose Style\n\nSelect editing vibe.", reply_markup=style_keyboard())
        return
    if data.startswith("style_"):
        sess["style"] = data.replace("style_", "").replace("_", " ").title()
        save_session(uid, sess)
        await q.edit_message_text("Step 8/9 — Choose Language\n\nSelect output caption language.", reply_markup=lang_keyboard())
        return
    if data.startswith("lang_"):
        sess["lang"] = data.replace("lang_", "").replace("_", " ")
        save_session(uid, sess)
        await q.edit_message_text("Step 9/9 — Caption Placement\n\nWhere should captions appear?", reply_markup=placement_keyboard())
        return
    if data.startswith("place_"):
        sess["placement"] = data.replace("place_", "")
        save_session(uid, sess)
        await show_confirm(q, uid, sess)
        return
    if data == "cancel_job":
        clear_session(uid)
        await q.edit_message_text("Cancelled.")
        return
    if data == "confirm_job":
        kind = sess.get("kind", "edit120")
        ok, jobid = await enqueue(context.bot, uid, kind, sess)
        if not ok:
            await q.edit_message_text("❌ " + jobid)
            return
        clear_session(uid)
        pos = jobs.video_queue.qsize() if jobs.video_queue else 1
        await q.edit_message_text(f"📋 Queue Position: #{pos}\nI’ll notify you when processing starts.")
        return


async def show_confirm(q, uid: int, sess: dict):
    kind = sess.get("kind", "edit120")
    price = service_price(kind)
    maxd = service_max(kind)
    u = get_user(uid)
    txt = (
        f"📋 Confirm Your Order\n\n"
        f"Service: {kind}\n"
        f"Platform: {sess.get('platform','Auto')}\n"
        f"Niche: {sess.get('niche','Auto')}\n"
        f"Goal: {sess.get('goal','Auto')}\n"
        f"Style: {sess.get('style','Auto')}\n"
        f"Captions: {sess.get('placement','bottom')}\n\n"
        f"Max duration: {maxd:.0f}s\n"
        f"Price: {rupees_str(price) if price else 'FREE'}\n"
        f"Wallet: {rupees_str(u['balance_paisa'])}\n\n"
        f"Estimated time: ~2-8 min depending on video size.\n\nProceed?"
    )
    await q.edit_message_text(txt, reply_markup=confirm_keyboard())


async def post_init(app: Application):
    cleanup_old_files(DOWNLOADS_DIR, 3600)
    jobs.video_queue = asyncio.Queue()
    for _ in range(WORKER_COUNT):
        asyncio.create_task(worker_loop())
    logger.info("GodMode V3 started")


def main():
    init_db()
    cleanup_old_files(DOWNLOADS_DIR, 3600)
    threading.Thread(target=run_flask, daemon=True).start()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("support", support_cmd))
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CommandHandler("recharge", recharge))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("setbalance", setbalance))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
