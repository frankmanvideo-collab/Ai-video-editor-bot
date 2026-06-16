from __future__ import annotations
import asyncio, logging, shutil, uuid
from pathlib import Path
from config import DOWNLOADS_DIR, MAX_QUEUE_SIZE, PRICE_MINI_AUDIT, PRICE_FULL_ROAST, PRICE_EDIT_60, PRICE_EDIT_120, FREE_SAMPLE_SECONDS, MAX_VIDEO_DURATION_SEC, MIN_VIDEO_DURATION_SEC
from db import create_job, update_job, debit_wallet, refund_wallet, set_free_sample_used, get_user
from renderer_ffmpeg import render_video_ffmpeg, audit_video
from utils import ffprobe_duration, rupees_str, escape_md, check_disk_space
from telegram.constants import ParseMode

logger=logging.getLogger("GodModeV3")
video_queue: asyncio.Queue|None=None
active_users:set[int]=set()

SERVICE_PRICE={"mini_audit":PRICE_MINI_AUDIT,"full_roast":PRICE_FULL_ROAST,"edit60":PRICE_EDIT_60,"edit120":PRICE_EDIT_120,"free_sample":0}
SERVICE_MAX={"mini_audit":60.0,"full_roast":120.0,"edit60":60.0,"edit120":120.0,"free_sample":FREE_SAMPLE_SECONDS}

def service_price(kind:str)->int: return SERVICE_PRICE.get(kind,0)
def service_max(kind:str)->float: return SERVICE_MAX.get(kind,MAX_VIDEO_DURATION_SEC)
def is_audit(kind:str)->bool: return kind in ("mini_audit","full_roast")
def is_edit(kind:str)->bool: return kind in ("edit60","edit120","free_sample")

async def download_file(bot, file_id: str, dest: Path):
    f=await bot.get_file(file_id)
    await f.download_to_drive(str(dest))

async def enqueue(bot, user_id:int, kind:str, config:dict) -> tuple[bool,str]:
    global video_queue
    if video_queue is None: return False,"Worker not ready"
    if video_queue.qsize() >= MAX_QUEUE_SIZE: return False,"Queue full"
    if user_id in active_users: return False,"You already have an active job"
    job_id=uuid.uuid4().hex[:12]
    price=service_price(kind)
    create_job(job_id,user_id,kind,price,config)
    active_users.add(user_id)
    await video_queue.put({"job_id":job_id,"user_id":user_id,"kind":kind,"price":price,"config":config,"bot":bot})
    return True,job_id

def format_report(strategy:dict) -> str:
    scores=strategy.get("scores",{})
    lines=[f"📊 *Viral Score:* {strategy.get('viral_score',70)}/100\n"]
    lines.append(f"🎯 Hook: {scores.get('hook',7)}/10 | Pacing: {scores.get('pacing',7)}/10 | Clarity: {scores.get('clarity',7)}/10 | CTA: {scores.get('cta',6)}/10\n")
    lines.append("❌ *Top Mistakes:*\n"+"\n".join([f"• {escape_md(x)}" for x in strategy.get("mistakes",[])[:5]])+"\n")
    lines.append("✅ *Fixes:*\n"+"\n".join([f"• {escape_md(x)}" for x in strategy.get("fixes",[])[:5]])+"\n")
    lines.append("🧲 *Hook Ideas:*\n"+"\n".join([f"{i+1}\. {escape_md(x)}" for i,x in enumerate(strategy.get("hook_options",[])[:3])])+"\n")
    lines.append(f"📌 *CTA:* {escape_md((strategy.get('cta_suggestions') or [''])[0])}\n")
    lines.append(f"📝 *Caption:*\n{escape_md(strategy.get('post_caption',''))}\n")
    lines.append("🏷 " + escape_md(" ".join(strategy.get("hashtags",[])[:15])))
    return "\n".join(lines)

async def worker_loop():
    assert video_queue is not None
    while True:
        job=await video_queue.get()
        user_id=job["user_id"]; job_id=job["job_id"]; bot=job["bot"]; kind=job["kind"]; price=job["price"]; config=job["config"]
        charged=0; workdir=DOWNLOADS_DIR/job_id
        try:
            update_job(job_id,"PROCESSING")
            workdir.mkdir(parents=True,exist_ok=True)
            if not check_disk_space(DOWNLOADS_DIR, 1000): raise RuntimeError("Server storage is low. Try later.")
            await bot.send_message(user_id,"📥 Downloading your video…")
            input_path=workdir/"input.mp4"; output_path=workdir/"output.mp4"
            await download_file(bot, config["video_file_id"], input_path)
            dur=ffprobe_duration(str(input_path))
            if dur < MIN_VIDEO_DURATION_SEC: raise ValueError(f"Video too short. Minimum {MIN_VIDEO_DURATION_SEC:.0f}s.")
            maxdur=service_max(kind)
            if dur > maxdur: raise ValueError(f"This pack supports max {maxdur:.0f}s video.")
            if kind == "free_sample":
                u=get_user(user_id)
                if u["free_sample_used"]: raise ValueError("Free sample already used.")
                set_free_sample_used(user_id)
            elif price>0:
                ok,_=debit_wallet(user_id,price,f"job:{kind}:{job_id}",job_id)
                if not ok: raise ValueError(f"Insufficient balance. Need {rupees_str(price)}.")
                charged=price
            await bot.send_message(user_id,"🧠 Analyzing hook, pacing and content…")
            if is_audit(kind):
                result=await asyncio.get_running_loop().run_in_executor(None, audit_video, str(input_path), {**config,"kind":kind}, workdir)
                await bot.send_message(user_id, format_report(result["strategy"]), parse_mode=ParseMode.MARKDOWN_V2)
            else:
                await bot.send_message(user_id,"🎬 Rendering your viral reel…")
                result=await asyncio.get_running_loop().run_in_executor(None, render_video_ffmpeg, str(input_path), str(output_path), {**config,"kind":kind}, workdir)
                await bot.send_message(user_id,"📤 Uploading final video…")
                with open(output_path,"rb") as vf:
                    await bot.send_video(user_id, vf, caption=f"✅ Done\nDuration: {result['duration']:.1f}s\nCost: {rupees_str(charged) if charged else 'FREE SAMPLE'}", supports_streaming=True)
                await bot.send_message(user_id, format_report(result["strategy"]), parse_mode=ParseMode.MARKDOWN_V2)
            update_job(job_id,"COMPLETED")
        except Exception as e:
            logger.exception("job failed %s", job_id)
            if charged: refund_wallet(user_id,charged,f"refund:{job_id}",job_id)
            update_job(job_id,"FAILED",str(e)[:300])
            await bot.send_message(user_id, f"❌ Job failed\n\n{str(e)[:250]}\n\nYou have not been charged." + (" Refund issued." if charged else ""))
        finally:
            active_users.discard(user_id)
            shutil.rmtree(workdir, ignore_errors=True)
            video_queue.task_done()
