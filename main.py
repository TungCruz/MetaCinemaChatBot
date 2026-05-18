from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timezone, timedelta
import logging
import os
import time
import threading
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

from db import get_movie_context, get_food_context, get_knowledge_context
from gemini import call_gemini, build_system_prompt
from intent_router import try_build_routed_reply
from admin_router import try_build_admin_reply
from staff_router import try_build_staff_reply
import session_store

load_dotenv()

_INTERNAL_SECRET = os.getenv("CHATBOT_INTERNAL_SECRET", "")

_MAX_HISTORY = 20  # Gemini token budget: keep last 10 turns

# TTL cache for Gemini context (avoids 3 DB hits per fallback message)
_ctx_cache: dict[str, tuple[float, str]] = {}
_ctx_lock = threading.Lock()

def _cached_ctx(key: str, fn, ttl: int) -> str:
    with _ctx_lock:
        ts, val = _ctx_cache.get(key, (0.0, ""))
        if time.monotonic() - ts < ttl:
            return val
    # Compute outside lock so slow DB calls don't block other requests
    val = fn()
    with _ctx_lock:
        _ctx_cache[key] = (time.monotonic(), val)
    return val

app = FastAPI(title="MetaCinema Chatbot Service", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)


class HistoryItem(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    message: str
    history: list[HistoryItem] = []
    user_id: Optional[int] = None
    session_token: Optional[str] = None   # anonymous session key (UUID from frontend localStorage)
    role: Optional[str] = None
    page_context: Optional[dict] = None


class ChatResponse(BaseModel):
    reply: str
    actions: list[dict] = []


@app.get("/")
def health():
    return {"status": "ok", "service": "MetaCinema Chatbot"}


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, request: Request):
    if _INTERNAL_SECRET:
        if request.headers.get("X-Internal-Secret", "") != _INTERNAL_SECRET:
            raise HTTPException(status_code=403, detail="forbidden")

    message = req.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")

    now_vn = datetime.now(timezone(timedelta(hours=7))).replace(tzinfo=None)
    page_context = req.page_context or {}

    # Inject per-user session (last_movie_id, last_movie_title) into page_context
    sess = session_store.get(user_id=req.user_id, session_token=req.session_token)
    if sess:
        page_context = {**page_context, **sess}

    # Admin / Staff chatbot — runs before customer routing
    area = (page_context.get("area") or "").lower()
    mode = (page_context.get("mode") or "").lower()
    if area == "admin" or mode == "admin":
        admin_reply = try_build_admin_reply(message, req.role, now_vn, user_id=req.user_id)
        if admin_reply is not None:
            return ChatResponse(reply=admin_reply["reply"], actions=admin_reply.get("actions", []))
    if area == "staff" or mode == "staff":
        staff_reply = try_build_staff_reply(message, req.role, now_vn)
        if staff_reply is not None:
            return ChatResponse(reply=staff_reply["reply"], actions=staff_reply.get("actions", []))

    # Fast-path: customer intent routing (no Gemini call)
    routed = try_build_routed_reply(message, page_context, req.user_id, now_vn)
    if routed is not None:
        # Persist session updates (e.g. last resolved movie)
        if routed.get("session_update") and (req.user_id or req.session_token):
            session_store.update(user_id=req.user_id, session_token=req.session_token,
                                 data=routed["session_update"])
        return ChatResponse(reply=routed["reply"], actions=routed.get("actions", []))

    # Fallback: Gemini AI
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        return ChatResponse(reply="Chatbot chưa được cấu hình. Vui lòng liên hệ quản trị viên.")

    # Movie context: TTL 2 min (has live countdowns); food/knowledge: TTL 10 min
    movie_context     = _cached_ctx("movie",     lambda: get_movie_context(now_vn), 120)
    food_context      = _cached_ctx("food",      get_food_context,                  600)
    knowledge_context = _cached_ctx("knowledge", get_knowledge_context,             600)

    # Add context hint to Gemini if user was discussing a specific movie
    page_note = ""
    if req.user_id or req.session_token:
        last_title = session_store.get(user_id=req.user_id, session_token=req.session_token).get("last_movie_title", "")
        if last_title:
            page_note = f"User vừa hỏi về phim **{last_title}** — ưu tiên trả lời liên quan đến phim này nếu không có yêu cầu khác."

    system_prompt = build_system_prompt(movie_context, food_context, knowledge_context, page_note)

    reply = call_gemini(api_key, system_prompt, req.history[-_MAX_HISTORY:], message)

    return ChatResponse(reply=reply)
