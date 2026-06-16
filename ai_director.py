from __future__ import annotations
import json, logging, os
from openai import OpenAI
from config import AICREDITS_API_KEY, AICREDITS_BASE_URL, AI_DIRECTOR_MODEL, WHISPER_MODEL
from utils import safe_json_loads

logger = logging.getLogger("GodModeV3")
ai_client = OpenAI(api_key=AICREDITS_API_KEY, base_url=AICREDITS_BASE_URL)

def _fake_word_timestamps(text: str, duration_sec: float) -> list[dict]:
    words=[w for w in str(text or "").split() if w.strip()]
    if not words: return []
    step=max(float(duration_sec or 1),1)/len(words)
    return [{"word":w,"start":i*step,"end":min(float(duration_sec or 1), i*step+max(.2,step*.85))} for i,w in enumerate(words)]

def transcribe_audio(audio_path: str, duration_sec: float) -> dict:
    timeout=max(60,int(duration_sec*2))
    try:
        with open(audio_path,"rb") as f:
            resp=ai_client.audio.transcriptions.create(model=WHISPER_MODEL,file=f,response_format="verbose_json",timestamp_granularities=["word"],timeout=timeout)
        data=resp.model_dump() if hasattr(resp,"model_dump") else dict(resp)
        data["words"]=data.get("words") or _fake_word_timestamps(data.get("text",""), duration_sec)
        return data
    except Exception as e:
        logger.warning("word timestamp transcription failed: %s", e)
    try:
        with open(audio_path,"rb") as f:
            resp=ai_client.audio.transcriptions.create(model=WHISPER_MODEL,file=f,response_format="verbose_json",timeout=timeout)
        data=resp.model_dump() if hasattr(resp,"model_dump") else dict(resp)
        data["words"]=data.get("words") or _fake_word_timestamps(data.get("text",""), duration_sec)
        return data
    except Exception as e:
        logger.warning("verbose transcription failed: %s", e)
    try:
        with open(audio_path,"rb") as f:
            resp=ai_client.audio.transcriptions.create(model=WHISPER_MODEL,file=f,response_format="text",timeout=timeout)
        text=str(resp)
        return {"text":text,"words":_fake_word_timestamps(text,duration_sec)}
    except Exception as e:
        logger.warning("text transcription failed: %s", e)
    return {"text":"","words":[]}

def ai_strategy(transcript: str, config: dict, mode: str="edit") -> dict:
    system = """You are an elite short-form video growth strategist and AI reel editor. Return ONLY compact JSON.
Required keys:
hook_options: 3 short viral hooks, 4-9 words each
best_hook_index: 0-2
punchwords: array of ALL CAPS important words
post_caption: Instagram/YouTube caption, short
hashtags: 8-15 relevant hashtags
cta_suggestions: 3 CTA lines
viral_score: integer 1-100
scores: object {hook,pacing,clarity,cta,retention} each 1-10
mistakes: array of top mistakes
fixes: array of practical fixes
broll_timestamps: array {image_index:int, at_sec:float} max 3
mood: one of motivational,luxury,tech,urgent,neutral
"""
    payload={"transcript":transcript[:5000],"platform":config.get("platform"),"niche":config.get("niche"),"goal":config.get("goal"),"style":config.get("style"),"mode":mode}
    try:
        resp=ai_client.chat.completions.create(model=AI_DIRECTOR_MODEL,messages=[{"role":"system","content":system},{"role":"user","content":json.dumps(payload)}],response_format={"type":"json_object"},max_tokens=1200,timeout=35)
        data=safe_json_loads(resp.choices[0].message.content or "{}", {})
    except Exception as e:
        logger.warning("AI strategy fallback: %s", e); data={}
    hooks=data.get("hook_options") or ["Stop scrolling for this", "This changes everything", "Watch this before you decide"]
    return {
        "hook_options": hooks[:3],
        "best_hook_index": int(data.get("best_hook_index",0) or 0) % 3,
        "punchwords": data.get("punchwords") or ["STOP","SECRET","NOW","WIN"],
        "post_caption": data.get("post_caption") or "Save this reel and use it before your next post.",
        "hashtags": data.get("hashtags") or ["#reelsindia","#contentcreator","#growthtips","#viralreels"],
        "cta_suggestions": data.get("cta_suggestions") or ['DM "START" for details','Save this reel','Follow for more'],
        "viral_score": int(data.get("viral_score",72) or 72),
        "scores": data.get("scores") or {"hook":7,"pacing":7,"clarity":7,"cta":6,"retention":7},
        "mistakes": data.get("mistakes") or ["Hook can be stronger", "CTA can be clearer", "Pacing can be tighter"],
        "fixes": data.get("fixes") or ["Start with a stronger promise", "Cut dead air", "Add a clear CTA"],
        "broll_timestamps": data.get("broll_timestamps") or [],
        "mood": data.get("mood") or "neutral",
    }

def generic_hooks(config: dict) -> list[str]:
    niche=config.get("niche","creator")
    goal=config.get("goal","growth")
    return [f"Stop making this {niche} mistake", "This one trick changes everything", f"Watch this before your next {goal}"][:3]
