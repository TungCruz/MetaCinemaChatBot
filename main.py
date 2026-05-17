from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timezone, timedelta
import os
from dotenv import load_dotenv

from db import get_movie_context, get_food_context, get_knowledge_context
from gemini import call_gemini, build_system_prompt
from intent_router import try_build_routed_reply
from admin_router import try_build_admin_reply
from staff_router import try_build_staff_reply

load_dotenv()

_INTERNAL_SECRET = os.getenv("CHATBOT_INTERNAL_SECRET", "")

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
        return ChatResponse(reply=routed["reply"], actions=routed.get("actions", []))

    # Fallback: Gemini AI
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        return ChatResponse(reply="Chatbot chưa được cấu hình. Vui lòng liên hệ quản trị viên.")

    movie_context     = get_movie_context()
    food_context      = get_food_context()
    knowledge_context = get_knowledge_context()

    system_prompt = build_system_prompt(movie_context, food_context, knowledge_context)
    reply = call_gemini(api_key, system_prompt, req.history, message)

    return ChatResponse(reply=reply)
