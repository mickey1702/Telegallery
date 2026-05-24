"""
TeleGallery Backend – Multi-User Telegram Gallery
Features: Auth, Albums, Photos, Invites, QR Codes, Trash, Stats, Comments, Timeline
"""

import os
import json
import uuid
import hashlib
import base64
import io
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from typing import List, Optional, Dict

from fastapi import FastAPI, File, UploadFile, Form, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from telethon import TelegramClient
from telethon.tl.functions.channels import (
    GetForumTopicsRequest, GetFullChannelRequest, CreateForumTopicRequest
)
from telethon.tl.types import InputPeerChannel, MessageMediaPhoto, MessageMediaDocument
from dotenv import load_dotenv
import aiofiles

# ============ Load Environment ============
load_dotenv()

ADMIN_API_TOKEN = os.getenv("ADMIN_TOKEN", os.getenv("DEFAULT_TOKEN", "demo-token"))
UPLOAD_DIR = "/tmp/telegallery_uploads"
USERS_DB = "/workspaces/Telegallery/backend/users.json"
INVITES_DB = "/workspaces/Telegallery/backend/invites.json"
TRASH_DB = "/workspaces/Telegallery/backend/trash.json"
COMMENTS_DB = "/workspaces/Telegallery/backend/comments.json"

os.makedirs(os.path.dirname(USERS_DB), exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)

# ============ Database Helpers ============
def load_json(path: str) -> Dict:
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_json(path: str, data: Dict):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

# ============ Global Clients ============
clients: Dict[str, TelegramClient] = {}

# ============ Helpers ============
def generate_token() -> str:
    return hashlib.sha256(str(uuid.uuid4()).encode()).hexdigest()[:32]

def generate_qr_data(user_id: str, album_id: str) -> str:
    data = {"u": user_id, "a": album_id, "t": generate_token()}
    return base64.b64encode(json.dumps(data).encode()).decode()

def verify_admin(token: str):
    if token != ADMIN_API_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid admin token")

def verify_user(token: str) -> Dict:
    users = load_json(USERS_DB)
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
    print("TeleGallery Backend starting...")
    yield
    for client in clients.values():
        await client.disconnect()
    print("All clients disconnected")

# ============ FastAPI App ============
app = FastAPI(
    title="TeleGallery",
    description="Multi-User Telegram Photo Gallery",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============ AUTH ============

@app.post("/api/auth/register")
async def register(
    name: str = Form(...),
    api_id: int = Form(...),
    api_hash: str = Form(...),
    bot_token: str = Form(...),
    group_id: int = Form(...),
    pin: str = Form("")
):
    try:
        session_path = os.path.join(UPLOAD_DIR, f"session_test_{uuid.uuid4().hex[:8]}")
        test_client = TelegramClient(session_path, api_id, api_hash)
        await test_client.start(bot_token=bot_token)
        me = await test_client.get_me()
        await test_client.disconnect()

        for ext in [".session", ".session-journal"]:
            f = session_path + ext
            if os.path.exists(f):
                os.remove(f)

        user_id = str(uuid.uuid4())
        token = generate_token()

        users = load_json(USERS_DB)
        users[user_id] = {
            "id": user_id,
            "name": name,
            "api_id": api_id,
            "api_hash": api_hash,
            "bot_token": bot_token,
            "group_id": group_id,
            "token": token,
            "pin": pin,
            "created_at": datetime.now().isoformat(),
            "albums": ["All"]
        }
        save_json(USERS_DB, users)

        return {
            "success": True,
            "user_id": user_id,
            "token": token,
            "message": "Account created! Save your token."
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Registration failed: {str(e)}")

@app.post("/api/auth/login")
async def login(token: str = Form(...)):
    user = verify_user(token)
    return {
        "success": True,
        "user": {
            "id": user["id"],
            "name": user["name"],
            "group_id": user["group_id"],
            "has_pin": bool(user.get("pin"))
        }
    }

@app.post("/api/auth/verify-pin")
async def verify_pin(token: str = Form(...), pin: str = Form(...)):
    user = verify_user(token)
    if user.get("pin") and user["pin"] != pin:
        raise HTTPException(status_code=401, detail="Wrong PIN")
    return {"success": True}

@app.get("/api/auth/me")
async def get_me(token: str):
    user = verify_user(token)
    return {
        "id": user["id"],
        "name": user["name"],
        "group_id": user["group_id"],
        "has_pin": bool(user.get("pin"))
    }

@app.post("/api/auth/set-pin")
async def set_pin(token: str = Form(...), pin: str = Form(...)):
    user = verify_user(token)
    users = load_json(USERS_DB)
    users[user["id"]]["pin"] = pin
    save_json(USERS_DB, users)
    return {"success": True}

# ============ ALBUMS ============

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

        total_count = sum(getattr(t, "message_count", 0) or 0 for t in result.topics)

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

# ============ PHOTOS ============

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
            if msg.media:
                is_video = isinstance(msg.media, MessageMediaDocument)
                is_photo = isinstance(msg.media, MessageMediaPhoto)

                if is_photo or is_video:
                    photos.append({
                        "id": msg.id,
                        "caption": msg.text or "",
                        "date": msg.date.isoformat() if msg.date else None,
                        "type": "video" if is_video else "photo",
                        "size": getattr(msg.media, "photo", None) and getattr(msg.media.photo, "sizes", [{}])[-1].get("w", 0) * getattr(msg.media.photo, "sizes", [{}])[-1].get("h", 0) or 0
                    })

        next_offset = photos[-1]["id"] - 1 if photos else None
        return {"photos": photos, "next_offset": next_offset}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/photos/{photo_id}/download")
async def download_photo(token: str, photo_id: int):
    user = verify_user(token)
    client = await get_user_client(user)

    try:
        group = await get_group_entity(client, user["group_id"])
        msg = await client.get_messages(group, ids=photo_id)

        if not msg or not msg.media:
            raise HTTPException(status_code=404, detail="Photo not found")

        path = await client.download_media(msg.media, file=bytes)

        content_type = "image/jpeg"
        if isinstance(msg.media, MessageMediaDocument):
            content_type = "video/mp4"

        return StreamingResponse(
            io.BytesIO(path),
            media_type=content_type,
            headers={"Content-Disposition": f"attachment; filename=photo_{photo_id}.jpg"}
        )
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

# ============ TRASH ============

@app.post("/api/photos/{photo_id}/trash")
async def move_to_trash(token: str, photo_id: int):
    user = verify_user(token)
    client = await get_user_client(user)

    try:
        group = await get_group_entity(client, user["group_id"])
        msg = await client.get_messages(group, ids=photo_id)

        if not msg:
            raise HTTPException(status_code=404, detail="Photo not found")

        trash = load_json(TRASH_DB)
        user_trash = trash.get(user["id"], [])
        user_trash.append({
            "photo_id": photo_id,
            "caption": msg.text or "",
            "date": msg.date.isoformat() if msg.date else None,
            "trashed_at": datetime.now().isoformat()
        })
        trash[user["id"]] = user_trash
        save_json(TRASH_DB, trash)

        await client.delete_messages(group, [photo_id])

        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/trash")
async def list_trash(token: str):
    user = verify_user(token)
    trash = load_json(TRASH_DB)
    user_trash = trash.get(user["id"], [])

    # Auto-delete items older than 30 days
    cutoff = datetime.now() - timedelta(days=30)
    user_trash = [t for t in user_trash if datetime.fromisoformat(t["trashed_at"]) > cutoff]
    trash[user["id"]] = user_trash
    save_json(TRASH_DB, trash)

    return {"trash": user_trash}

@app.post("/api/trash/{photo_id}/restore")
async def restore_photo(token: str, photo_id: int):
    # In a real implementation, this would restore from Telegram
    # For now, just remove from trash list
    user = verify_user(token)
    trash = load_json(TRASH_DB)
    user_trash = trash.get(user["id"], [])
    user_trash = [t for t in user_trash if t["photo_id"] != photo_id]
    trash[user["id"]] = user_trash
    save_json(TRASH_DB, trash)
    return {"success": True}

@app.delete("/api/trash/{photo_id}")
async def permanent_delete(token: str, photo_id: int):
    user = verify_user(token)
    trash = load_json(TRASH_DB)
    user_trash = trash.get(user["id"], [])
    user_trash = [t for t in user_trash if t["photo_id"] != photo_id]
    trash[user["id"]] = user_trash
    save_json(TRASH_DB, trash)
    return {"success": True}

# ============ COMMENTS ============

@app.post("/api/photos/{photo_id}/comments")
async def add_comment(token: str, photo_id: int, text: str = Form(...)):
    user = verify_user(token)
    comments = load_json(COMMENTS_DB)
    photo_comments = comments.get(str(photo_id), [])

    photo_comments.append({
        "id": str(uuid.uuid4())[:8],
        "user": user["name"],
        "text": text,
        "date": datetime.now().isoformat()
    })
    comments[str(photo_id)] = photo_comments
    save_json(COMMENTS_DB, comments)

    return {"success": True, "comment": photo_comments[-1]}

@app.get("/api/photos/{photo_id}/comments")
async def get_comments(token: str, photo_id: int):
    verify_user(token)
    comments = load_json(COMMENTS_DB)
    return {"comments": comments.get(str(photo_id), [])}

# ============ STATS ============

@app.get("/api/stats")
async def get_stats(token: str):
    user = verify_user(token)
    client = await get_user_client(user)

    try:
        group = await get_group_entity(client, user["group_id"])

        total_photos = 0
        total_videos = 0
        total_size = 0
        earliest = None
        latest = None

        async for msg in client.iter_messages(group, limit=1000):
            if msg.media:
                if isinstance(msg.media, MessageMediaPhoto):
                    total_photos += 1
                elif isinstance(msg.media, MessageMediaDocument):
                    total_videos += 1

                if msg.date:
                    if not earliest or msg.date < earliest:
                        earliest = msg.date
                    if not latest or msg.date > latest:
                        latest = msg.date

        trash = load_json(TRASH_DB)
        trash_count = len(trash.get(user["id"], []))

        return {
            "total_photos": total_photos,
            "total_videos": total_videos,
            "total_media": total_photos + total_videos,
            "trash_count": trash_count,
            "earliest_photo": earliest.isoformat() if earliest else None,
            "latest_photo": latest.isoformat() if latest else None,
            "group_id": user["group_id"]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ============ TIMELINE ============

@app.get("/api/timeline")
async def get_timeline(token: str, year: int = Query(None), month: int = Query(None)):
    user = verify_user(token)
    client = await get_user_client(user)

    try:
        group = await get_group_entity(client, user["group_id"])

        photos = []
        async for msg in client.iter_messages(group, limit=1000):
            if msg.media and isinstance(msg.media, MessageMediaPhoto):
                msg_date = msg.date
                if year and msg_date.year != year:
                    continue
                if month and msg_date.month != month:
                    continue

                photos.append({
                    "id": msg.id,
                    "caption": msg.text or "",
                    "date": msg_date.isoformat(),
                    "day": msg_date.day,
                    "month": msg_date.month,
                    "year": msg_date.year
                })

        # Group by date
        by_date = {}
        for p in photos:
            key = f"{p['year']}-{p['month']:02d}-{p['day']:02d}"
            if key not in by_date:
                by_date[key] = []
            by_date[key].append(p)

        timeline = [
            {"date": k, "photos": v, "count": len(v)}
            for k, v in sorted(by_date.items(), reverse=True)
        ]

        return {"timeline": timeline}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ============ INVITES & QR ============

@app.post("/api/invites")
async def create_invite(token: str = Form(...), album_id: str = Form("all"), expiry_days: int = Form(7)):
    user = verify_user(token)
    invite_id = str(uuid.uuid4())[:12]

    invites = load_json(INVITES_DB)
    invites[invite_id] = {
        "user_id": user["id"],
        "album_id": album_id,
        "created_at": datetime.now().isoformat(),
        "expires_at": (datetime.now() + timedelta(days=expiry_days)).isoformat(),
        "uses": 0,
        "qr_data": generate_qr_data(user["id"], album_id)
    }
    save_json(INVITES_DB, invites)

    return {
        "invite_id": invite_id,
        "url": f"?invite={invite_id}",
        "qr_data": invites[invite_id]["qr_data"],
        "expires": invites[invite_id]["expires_at"]
    }

@app.get("/api/invites/{invite_id}")
async def get_invite(invite_id: str):
    invites = load_json(INVITES_DB)
    invite = invites.get(invite_id)
    if not invite:
        raise HTTPException(status_code=404, detail="Invite not found")

    # Check expiry
    if datetime.fromisoformat(invite["expires_at"]) < datetime.now():
        raise HTTPException(status_code=410, detail="Invite expired")

    users = load_json(USERS_DB)
    user = users.get(invite["user_id"])
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return {
        "album_id": invite["album_id"],
        "owner_name": user["name"],
        "group_id": user["group_id"],
        "expires": invite["expires_at"],
        "qr_data": invite["qr_data"]
    }

@app.post("/api/invites/{invite_id}/use")
async def use_invite(invite_id: str):
    invites = load_json(INVITES_DB)
    invite = invites.get(invite_id)
    if not invite:
        raise HTTPException(status_code=404, detail="Invite not found")

    invite["uses"] += 1
    save_json(INVITES_DB, invites)
    return {"success": True}

# ============ HEALTH ============

@app.get("/health")
async def health_check():
    return {
        "status": "ok",
        "users": len(load_json(USERS_DB)),
        "active_clients": len([c for c in clients.values() if c.is_connected()])
    }