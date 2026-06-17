from __future__ import annotations
import hashlib, hmac, time, uuid, logging, re, secrets
from collections import defaultdict
import requests
from flask import request, jsonify
from config import UPIGATEWAY_API_KEY, UPIGATEWAY_SECRET, WEBHOOK_URL, MIN_RECHARGE_PAISA, MAX_SINGLE_RECHARGE_PAISA, RECHARGE_RATE_LIMIT_PER_HOUR, MAX_WALLET_BALANCE_PAISA, MANUAL_UPI_ID, MANUAL_UPI_NAME, MANUAL_RECHARGE_EXPIRE_MINUTES, MANUAL_RECHARGE_DAILY_LIMIT
from db import get_user, log_order, confirm_order, credit_wallet, create_manual_recharge, count_manual_approved_today, get_latest_waiting_manual
logger=logging.getLogger("GodModeV3")
_timestamps=defaultdict(list)

def verify_signature(payload: bytes, signature: str) -> bool:
    if not UPIGATEWAY_SECRET: return True
    expected=hmac.new(UPIGATEWAY_SECRET.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)

def rate_limit(user_id:int) -> bool:
    now=time.time(); hour=now-3600
    _timestamps[user_id]=[x for x in _timestamps[user_id] if x>hour]
    if len(_timestamps[user_id])>=RECHARGE_RATE_LIMIT_PER_HOUR: return False
    _timestamps[user_id].append(now); return True

def create_order(user_id:int, amount_rs: float) -> dict:
    amount=int(round(amount_rs*100))
    if amount < MIN_RECHARGE_PAISA: return {"ok":False,"error":f"Minimum recharge is ₹{MIN_RECHARGE_PAISA/100:.0f}"}
    if amount > MAX_SINGLE_RECHARGE_PAISA: return {"ok":False,"error":"Amount too high"}
    if not UPIGATEWAY_API_KEY: return {"ok":False,"error":"Payment service unavailable"}
    if not rate_limit(user_id): return {"ok":False,"error":"Too many recharge attempts"}
    u=get_user(user_id)
    if u["balance_paisa"]+amount > MAX_WALLET_BALANCE_PAISA: return {"ok":False,"error":"Wallet limit exceeded"}
    txn=f"GMB-{user_id}-{uuid.uuid4().hex[:10].upper()}"
    log_order(txn,user_id,amount)
    payload={"key":UPIGATEWAY_API_KEY,"client_txn_id":txn,"amount":f"{amount_rs:.2f}","p_info":"GodMode Credits","customer_name":f"User{user_id}","customer_email":f"{user_id}@godmodebot.in","customer_mobile":"9999999999","redirect_url":f"{WEBHOOK_URL}/webhook/payment" if WEBHOOK_URL else "","udf1":str(user_id)}
    try:
        r=requests.post("https://api.upigateway.com/v1/create_order",json=payload,timeout=15)
        data=r.json()
        if data.get("status"):
            return {"ok":True,"url":data["data"]["payment_url"],"txn_id":txn}
        return {"ok":False,"error":data.get("msg","Payment gateway error")}
    except Exception as e:
        logger.exception("UPI create order failed")
        return {"ok":False,"error":"Payment service unavailable"}

def handle_payment_webhook() -> tuple:
    raw=request.get_data(); sig=request.headers.get("X-Signature","")
    if not verify_signature(raw,sig): return jsonify({"error":"invalid_signature"}),401
    data=request.get_json(silent=True) or request.form.to_dict()
    logger.info("payment webhook: %s", {k:v for k,v in data.items() if k.lower() not in ("key","signature")})
    status=str(data.get("status","")).upper(); txstatus=str(data.get("txStatus","")).upper()
    if status not in ("SUCCESS","TRUE") and txstatus != "SUCCESS": return jsonify({"result":"ignored"}),200
    txn=str(data.get("client_txn_id", data.get("clientTxnId", "")))
    ref=str(data.get("orderId", data.get("utr", data.get("gateway_ref", ""))))
    if not txn: return jsonify({"error":"missing_txn"}),400
    order=confirm_order(txn,ref)
    if not order: return jsonify({"result":"already_processed"}),200
    newbal=credit_wallet(order["user_id"], order["amount_paisa"], f"recharge:{txn}")
    return jsonify({"result":"credited","user_id":order["user_id"],"balance":newbal}),200


# ─────────────────────────────────────────────────────────────────────────────
# Manual UPI recharge helper functions
# ─────────────────────────────────────────────────────────────────────────────

def normalize_utr(text: str) -> str:
    """Extract a likely UPI UTR/RRN from user text."""
    raw = str(text or "").upper().replace(" ", "").replace("-", "")
    # Prefer 12 digit UPI RRN/UTR if present
    m = re.search(r"\b\d{12}\b", raw)
    if m:
        return m.group(0)
    # fallback 10-22 alphanumeric references
    m = re.search(r"\b[A-Z0-9]{10,22}\b", raw)
    return m.group(0) if m else raw[:32]

def validate_utr_format(utr: str) -> tuple[bool, str]:
    """Soft UTR validation. Does NOT prove payment happened."""
    utr = normalize_utr(utr)
    if not utr:
        return False, "Empty UTR"
    if utr.isdigit() and len(utr) == 12:
        return True, utr
    # Many apps show alphanumeric transaction IDs; allow for manual admin review
    if re.fullmatch(r"[A-Z0-9]{10,22}", utr):
        return True, utr
    return False, "UTR/RRN should be 12 digits or 10-22 alphanumeric characters"

def create_manual_recharge_request(user_id: int, amount_rs: float) -> dict:
    amount_paisa = int(round(amount_rs * 100))
    if not MANUAL_UPI_ID:
        return {"ok": False, "error": "Manual UPI is not configured. Set MANUAL_UPI_ID in env."}
    if count_manual_approved_today(user_id) >= MANUAL_RECHARGE_DAILY_LIMIT:
        return {"ok": False, "error": f"Daily manual recharge limit reached ({MANUAL_RECHARGE_DAILY_LIMIT}/day)."}
    existing = get_latest_waiting_manual(user_id)
    if existing:
        return {"ok": False, "error": f"You already have a pending recharge request: {existing['request_id']}. Complete it or contact admin."}
    req = "MR-" + secrets.token_hex(3).upper()
    code = "PAY" + secrets.token_hex(3).upper()
    # SQLite datetime string, local-ish relative expiry handled by DB text for now
    expires_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + MANUAL_RECHARGE_EXPIRE_MINUTES * 60))
    create_manual_recharge(req, user_id, amount_paisa, code, expires_at)
    note = code
    upi_url = f"upi://pay?pa={MANUAL_UPI_ID}&pn={MANUAL_UPI_NAME.replace(' ', '%20')}&am={amount_rs:.2f}&tn={note}"
    return {"ok": True, "request_id": req, "secret_code": code, "amount_paisa": amount_paisa, "upi_url": upi_url, "upi_id": MANUAL_UPI_ID, "expires_at": expires_at}
