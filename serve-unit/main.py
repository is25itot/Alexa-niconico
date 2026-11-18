import asyncio
import hashlib
import hmac
import os
import re
import sys
import time
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse

CACHE_DIR = Path(os.environ.get("CACHE_DIR", "/srv/audio"))
API_KEY = os.environ["API_KEY"]
SIGN_SECRET = os.environ["SIGN_SECRET"].encode()
BASE_URL = os.environ["BASE_URL"].rstrip("/")
URL_TTL = int(os.environ.get("URL_TTL", 6 * 3600))  # 署名付きURLの有効秒数

VIDEO_ID_RE = re.compile(r"^(sm|nm|so)?\d{1,12}$")
YTDLP = [sys.executable, "-m", "yt_dlp"]

app = FastAPI()
semaphore = asyncio.Semaphore(1)  # 同時に取得する本数
tasks = {}   # 動画ID -> 実行中のタスク
errors = {}  # 動画ID -> 直近のエラー内容


def check_video_id(video_id: str) -> str:
    if not VIDEO_ID_RE.match(video_id):
        raise HTTPException(status_code=400, detail="invalid video id")
    return video_id


def make_sig(video_id: str, exp: int) -> str:
    msg = f"{video_id}:{exp}".encode()
    return hmac.new(SIGN_SECRET, msg, hashlib.sha256).hexdigest()


def make_url(video_id: str) -> str:
    exp = int(time.time()) + URL_TTL
    return f"{BASE_URL}/audio/{video_id}.mp3?exp={exp}&sig={make_sig(video_id, exp)}"


def require_api_key(key):
    if not key or not hmac.compare_digest(key, API_KEY):
        raise HTTPException(status_code=401, detail="unauthorized")


async def download(video_id: str):
    async with semaphore:
        out = CACHE_DIR / f"{video_id}.mp3"
        tmp_base = CACHE_DIR / f".tmp-{video_id}"
        proc = await asyncio.create_subprocess_exec(
            *YTDLP, "-x", "--audio-format", "mp3", "--audio-quality", "128K",
            "--postprocessor-args", "ffmpeg:-ar 44100",
            "--no-playlist", "--no-progress",
            "-o", f"{tmp_base}.%(ext)s",
            f"https://www.nicovideo.jp/watch/{video_id}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        tmp_mp3 = Path(f"{tmp_base}.mp3")
        if proc.returncode == 0 and tmp_mp3.exists():
            tmp_mp3.rename(out)  # 完成してから公開する
            errors.pop(video_id, None)
        else:
            errors[video_id] = err.decode(errors="replace")[-500:]
            for p in CACHE_DIR.glob(f".tmp-{video_id}.*"):
                p.unlink(missing_ok=True)


@app.get("/api/prepare/{video_id}")
async def prepare(video_id: str, x_api_key: str = Header(default=None)):
    require_api_key(x_api_key)
    check_video_id(video_id)
    if (CACHE_DIR / f"{video_id}.mp3").exists():
        return {"status": "ready", "url": make_url(video_id)}
    task = tasks.get(video_id)
    if task is None or task.done():
        errors.pop(video_id, None)
        tasks[video_id] = asyncio.create_task(download(video_id))
        return {"status": "processing"}
    return {"status": "processing"}


@app.get("/api/status/{video_id}")
async def status(video_id: str, x_api_key: str = Header(default=None)):
    require_api_key(x_api_key)
    check_video_id(video_id)
    if (CACHE_DIR / f"{video_id}.mp3").exists():
        return {"status": "ready", "url": make_url(video_id)}
    if video_id in errors:
        return {"status": "error", "detail": errors[video_id]}
    task = tasks.get(video_id)
    return {"status": "processing" if task and not task.done() else "none"}


@app.api_route("/audio/{filename}", methods=["GET", "HEAD"])
async def audio(filename: str, exp: int = 0, sig: str = ""):
    if not filename.endswith(".mp3"):
        raise HTTPException(status_code=404)
    video_id = check_video_id(filename[:-4])
    if exp < time.time() or not hmac.compare_digest(sig, make_sig(video_id, exp)):
        raise HTTPException(status_code=403, detail="invalid or expired url")
    path = CACHE_DIR / f"{video_id}.mp3"
    if not path.exists():
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="audio/mpeg")