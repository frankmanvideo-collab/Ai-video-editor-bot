from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from config import PLATFORMS, NICHES, GOALS, STYLES

def service_keyboard(free_available=True):
    rows=[]
    if free_available: rows.append([InlineKeyboardButton("🎁 Free 15s Sample", callback_data="svc_free_sample")])
    rows += [
        [InlineKeyboardButton("📊 Mini Audit ₹19", callback_data="svc_mini_audit"), InlineKeyboardButton("🔥 Full Roast ₹39", callback_data="svc_full_roast")],
        [InlineKeyboardButton("🎬 Edit 60s ₹49", callback_data="svc_edit60"), InlineKeyboardButton("🚀 Viral Pack 2min ₹79", callback_data="svc_edit120")],
    ]
    return InlineKeyboardMarkup(rows)

def list_keyboard(prefix: str, items: list[str], per_row=2, skip=True):
    rows=[]
    for i in range(0,len(items),per_row):
        rows.append([InlineKeyboardButton(x, callback_data=f"{prefix}_{x.lower().replace(' ','_').replace('/','_')[:30]}") for x in items[i:i+per_row]])
    if skip: rows.append([InlineKeyboardButton("⏭ Skip / Auto", callback_data=f"{prefix}_auto")])
    return InlineKeyboardMarkup(rows)

def platform_keyboard(): return list_keyboard("platform", PLATFORMS)
def niche_keyboard(): return list_keyboard("niche", NICHES)
def goal_keyboard(): return list_keyboard("goal", GOALS)
def style_keyboard(): return list_keyboard("style", STYLES)

def lang_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🇬🇧 English", callback_data="lang_English"), InlineKeyboardButton("🇮🇳 Hindi→EN", callback_data="lang_Hindi_to_English")],[InlineKeyboardButton("🌐 Auto", callback_data="lang_Auto")]])

def placement_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬆️ Top", callback_data="place_top"), InlineKeyboardButton("⬛ Center", callback_data="place_center"), InlineKeyboardButton("⬇️ Bottom", callback_data="place_bottom")]])

def hook_keyboard(hooks: list[str]):
    rows=[]
    for i,h in enumerate(hooks[:3]): rows.append([InlineKeyboardButton(f"{i+1}. {h[:42]}", callback_data=f"hook_{i}")])
    rows.append([InlineKeyboardButton("🤖 Auto choose", callback_data="hook_auto"), InlineKeyboardButton("⏭ Skip hook", callback_data="hook_skip")])
    return InlineKeyboardMarkup(rows)

def cta_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('DM "START"', callback_data="cta_DM_START"), InlineKeyboardButton('DM "LINK"', callback_data="cta_DM_LINK")],
        [InlineKeyboardButton('Comment "YES"', callback_data="cta_COMMENT_YES"), InlineKeyboardButton("💾 Save this reel", callback_data="cta_SAVE")],
        [InlineKeyboardButton("✍️ Custom CTA", callback_data="cta_custom"), InlineKeyboardButton("⏭ Skip CTA", callback_data="cta_skip")]
    ])

def confirm_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Confirm", callback_data="confirm_job"), InlineKeyboardButton("❌ Cancel", callback_data="cancel_job")]])

def recharge_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("₹49", callback_data="pay_49"), InlineKeyboardButton("₹99", callback_data="pay_99"), InlineKeyboardButton("₹199", callback_data="pay_199")],
        [InlineKeyboardButton("₹499", callback_data="pay_499"), InlineKeyboardButton("₹999", callback_data="pay_999")]
    ])
