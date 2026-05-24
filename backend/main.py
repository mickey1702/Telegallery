"""
TeleGallery Backend – Multi-User Telegram Gallery
FastAPI + Telethon (MTProto) + User Management
"""

import os
import json
import uuid
import hashlib
from datetime import datetime
from contextlib import asynccontextmanager
from typing import List, Optional, Dict

from fastapi import FastAPI, File, UploadFile, Form, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from telethon import TelegramClient
from telethon.tl.functions.channels import (
    GetForumTopicsRequest, GetFullChannelRequest, CreateForumTopicRequest
)
from telethon.tl.types import InputPeerChannel, MessageMediaPhoto
from dotenv import load_dotenv
import aiofiles

# ============ Load Environment ============
load_dotenv()

ADMIN_API_TOKEN = os.getenv("API_TOKEN", os.getenv("DEFAULT_TOKEN", "demo-token"))
UPLOAD_DIR = "/tmp/telegallery_uploads"
USERS_DB = "/tmp/telegallery_users.json"
INVITES_DB = "/tmp/telegallery_invites.json"

os.makedirs(UPLOAD_DIR, exist_ok=True)

# ============ User Database ============
def load_users() -> Dict:
    if os.path.exists(USERS_DB):
        try:
            with open(USERS_DB, "r") as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_users(users: Dict):
    with open(USERS_DB, "w") as f:
        json.dump(users, f, indent=2)

def load_invites() -> Dict:
    if os.path.exists(INVITES_DB):
        try:
            with open(INVITES_DB, "r") as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_invites(invites: Dict):
    with open(INVITES_DB, "w") as f:
        json.dump(invites, f, indent=2)

# ============ Global Clients ============
clients: Dict[str, TelegramClient] = {}

# ============ Helpers ============
def generate_token() -> str:
    return hashlib.sha256(str(uuid.uuid4()).encode()).hexdigest()[:32]

def verify_admin(token: str):
    if token != ADMIN_API_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid admin token")

def verify_user(token: str) -> Dict:
    users = load_users()
    for user_id, user in users.items():
        if user.get("token") == token:
            return user
    raise HTTPException(status_code=401, detail="Invalid user token")

async def get_user_client(user: Dict) -> TelegramClient:
    user_id = user["id"]
    if user_id in clients and clients[user_id].is_connected():
        return clients[user_id]

    session_path = os.path.join(UPLOAD_DIR, f"session_{user_id}")
    client = TelegramClient(session_path, user["api_id"], user["api_hash"])
    await client.start(bot_token=user["bot_token"])
    clients[user_id] = client
    return client

async def get_group_entity(client: TelegramClient, group_id: int):
    try:
        return await client.get_entity(int(group_id))
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Group not found: {str(e)}")

# ============ Lifespan ============
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("TeleGallery Multi-User Backend starting...")
    yield
    for client in clients.values():
        await client.disconnect()
    print("All clients disconnected")

# ============ FastAPI App ============
app = FastAPI(
    title="TeleGallery Multi-User",
    description="Multi-User Telegram Photo Gallery Backend",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============ AUTH ROUTES ============

@app.post("/api/auth/register")
async def register(
    name: str = Form(...),
    api_id: int = Form(...),
    api_hash: str = Form(...),
    bot_token: str = Form(...),
    group_id: int = Form(...)
):
    """Register a new user with their own Telegram credentials"""
    try:
        # Test connection
        session_path = os.path.join(UPLOAD_DIR, f"session_test_{uuid.uuid4().hex[:8]}")
        test_client = TelegramClient(session_path, api_id, api_hash)
        await test_client.start(bot_token=bot_token)
        me = await test_client.get_me()
        await test_client.disconnect()

        # Clean up test session
        for ext in [".session", ".session-journal"]:
            f = session_path + ext
            if os.path.exists(f):
                os.remove(f)

        user_id = str(uuid.uuid4())
        token = generate_token()

        users = load_users()
        users[user_id] = {
            "id": user_id,
            "name": name,
            "api_id": api_id,
            "api_hash": api_hash,
            "bot_token": bot_token,
            "group_id": group_id,
            "token": token,
            "created_at": datetime.now().isoformat(),
            "albums": ["All"]
        }
        save_users(users)

        return {
            "success": True,
            "user_id": user_id,
            "token": token,
            "message": "Account created! Save your token – it is your password."
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Registration failed: {str(e)}")

@app.post("/api/auth/login")
async def login(token: str = Form(...)):
    """Login with token"""
    user = verify_user(token)
    return {
        "success": True,
        "user": {
            "id": user["id"],
            "name": user["name"],
            "group_id": user["group_id"]
        }
    }

@app.get("/api/auth/me")
async def get_me(token: str):
    user = verify_user(token)
    return {
        "id": user["id"],
        "name": user["name"],
        "group_id": user["group_id"]
    }

# ============ ALBUM ROUTES ============

@app.get("/api/albums")
async def list_albums(token: str):
    user = verify_user(token)
    client = await get_user_client(user)

    try:
        group = await get_group_entity(client, user["group_id"])

        result = await client(GetForumTopicsRequest(
            channel=InputPeerChannel(group.id, group.access_hash),
            offset_date=None,
            offset_id=0,
            offset_topic=0,
            limit=100
        ))

        total_count = sum(
            getattr(t, "message_count", 0) or 0
            for t in result.topics
        )

        albums = [{
            "id": "all",
            "title": "All",
            "icon": "🏠",
            "count": total_count
        }]

        for t in result.topics:
            albums.append({
                "id": str(t.id),
                "title": getattr(t, "title", "Untitled"),
                "icon": "📁",
                "count": getattr(t, "message_count", 0) or 0
            })

        return {"albums": albums}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/albums")
async def create_album(token: str = Form(...), title: str = Form(...)):
    user = verify_user(token)
    client = await get_user_client(user)

    try:
        group = await get_group_entity(client, user["group_id"])

        result = await client(CreateForumTopicRequest(
            channel=InputPeerChannel(group.id, group.access_hash),
            title=title
        ))

        return {
            "success": True,
            "album_id": str(result.id),
            "title": title
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ============ PHOTO ROUTES ============

@app.get("/api/photos")
async def list_photos(
    token: str,
    album_id: str = Query("all"),
    limit: int = Query(50),
    offset_id: int = Query(0)
):
    user = verify_user(token)
    client = await get_user_client(user)

    try:
        group = await get_group_entity(client, user["group_id"])

        reply_to = None
        if album_id != "all":
            reply_to = int(album_id)

        photos = []
        async for msg in client.iter_messages(
            group,
            limit=limit,
            offset_id=offset_id,
            reply_to=reply_to
        ):
            if msg.media and isinstance(msg.media, MessageMediaPhoto):
                photos.append({
                    "id": msg.id,
                    "caption": msg.text or "",
                    "date": msg.date.isoformat() if msg.date else None,
                })

        next_offset = photos[-1]["id"] - 1 if photos else None
        return {"photos": photos, "next_offset": next_offset}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/photos")
async def upload_photos(
    token: str = Form(...),
    album_id: str = Form("all"),
    files: List[UploadFile] = File(...),
    caption: str = Form("")
):
    user = verify_user(token)
    client = await get_user_client(user)

    try:
        group = await get_group_entity(client, user["group_id"])

        reply_to = None
        if album_id != "all":
            reply_to = int(album_id)

        uploaded = []
        for idx, file in enumerate(files):
            temp_path = os.path.join(UPLOAD_DIR, file.filename)
            try:
                content = await file.read()
                async with aiofiles.open(temp_path, "wb") as f:
                    await f.write(content)

                msg = await client.send_file(
                    group,
                    temp_path,
                    caption=caption if idx == 0 else "",
                    reply_to=reply_to,
                    force_document=False
                )

                uploaded.append({
                    "filename": file.filename,
                    "message_id": msg.id,
                    "status": "ok"
                })
            except Exception as e:
                uploaded.append({
                    "filename": file.filename,
                    "status": "failed",
                    "error": str(e)
                })
            finally:
                if os.path.exists(temp_path):
                    os.remove(temp_path)

        return {"uploaded": uploaded}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ============ INVITE ROUTES ============

@app.post("/api/invites")
async def create_invite(token: str = Form(...), album_id: str = Form("all")):
    user = verify_user(token)
    invite_id = str(uuid.uuid4())[:12]

    invites = load_invites()
    invites[invite_id] = {
        "user_id": user["id"],
        "album_id": album_id,
        "created_at": datetime.now().isoformat(),
        "uses": 0
    }
    save_invites(invites)

    return {
        "invite_id": invite_id,
        "url": f"?invite={invite_id}"
    }

@app.get("/api/invites/{invite_id}")
async def get_invite(invite_id: str):
    invites = load_invites()
    invite = invites.get(invite_id)
    if not invite:
        raise HTTPException(status_code=404, detail="Invite not found")

    users = load_users()
    user = users.get(invite["user_id"])
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return {
        "album_id": invite["album_id"],
        "owner_name": user["name"],
        "group_id": user["group_id"]
    }

# ============ HEALTH ============

@app.get("/health")
async def health_check():
    return {
        "status": "ok",
        "users": len(load_users()),
        "active_clients": len([c for c in clients.values() if c.is_connected()])
    }