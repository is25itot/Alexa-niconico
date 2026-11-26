import asyncio
import hashlib
import hmac
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
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

# --- 検索(ニコニコのスナップショット検索API v2) ---
SEARCH_URL = "https://snapshot.search.nicovideo.jp/api/v2/snapshot/video/contents/search"
SEARCH_CONTEXT = "nico-player"                                  # User-Agent と _context に使う名前
SEARCH_MIN_LEN = int(os.environ.get("SEARCH_MIN_LEN", 30))      # 短すぎる動画は除く(秒)
SEARCH_MAX_LEN = int(os.environ.get("SEARCH_MAX_LEN", 1200))    # 長すぎる動画(メドレーなど)は除く(秒)
SEARCH_CACHE_TTL = 600                                          # 同じ検索のキャッシュ(秒)
SEARCH_MIN_INTERVAL = 1.0                                       # 検索APIへの最小リクエスト間隔(秒)
PREFETCH_TOP = 2                                                # 検索のとき、上位何曲を先に取得し始めるか
search_cache = {}  # (キーワード, 件数) -> (時刻, 結果)
search_lock = asyncio.Lock()
last_search = 0.0


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


def snapshot_search(q: str, limit: int):
    """検索APIを呼ぶ(待ち時間のある処理なので、別スレッドで実行する)"""
    params = [
        ("q", q),
        ("targets", "title,tags"),
        ("fields", "contentId,title,thumbnailUrl,lengthSeconds,viewCounter"),
        ("filters[lengthSeconds][gte]", str(SEARCH_MIN_LEN)),
        ("filters[lengthSeconds][lte]", str(SEARCH_MAX_LEN)),
        ("_sort", "-viewCounter"),
        ("_limit", str(limit)),
        ("_context", SEARCH_CONTEXT),
    ]
    req = urllib.request.Request(
        SEARCH_URL + "?" + urllib.parse.urlencode(params),
        headers={"User-Agent": SEARCH_CONTEXT},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


def ensure_download(video_id: str):
    """まだ保存も取得中でもなければ、取得を始める(prepare と同じ動き)"""
    if (CACHE_DIR / f"{video_id}.mp3").exists():
        return
    task = tasks.get(video_id)
    if task is None or task.done():
        errors.pop(video_id, None)
        tasks[video_id] = asyncio.create_task(download(video_id))


@app.get("/api/search")
async def search(q: str, limit: int = 10, prefetch: int = PREFETCH_TOP,
                    x_api_key: str = Header(default=None)):
    global last_search
    require_api_key(x_api_key)
    q = q.strip()
    if not q or len(q) > 100:
        raise HTTPException(status_code=400, detail="invalid query")
    limit = max(1, min(limit, 20))

    key = (q, limit)
    cached = search_cache.get(key)
    if cached and time.time() - cached[0] < SEARCH_CACHE_TTL:
        results = cached[1]
    else:
        async with search_lock:
            wait = SEARCH_MIN_INTERVAL - (time.time() - last_search)
            if wait > 0:
                await asyncio.sleep(wait)
            last_search = time.time()
            try:
                data = await asyncio.to_thread(snapshot_search, q, limit)
            except Exception as e:
                raise HTTPException(status_code=502, detail=f"search failed: {e}")
        results = [
            {
                "id": d["contentId"],
                "title": d.get("title") or d["contentId"],
                "thumbnail": d.get("thumbnailUrl"),
                "duration": d.get("lengthSeconds"),
                "views": d.get("viewCounter"),
            }
            for d in data.get("data", [])
            if VIDEO_ID_RE.match(d.get("contentId") or "")
        ]
        search_cache[key] = (time.time(), results)

    # 上位の曲は、先に取得を始めておく(Alexaの待ち時間を減らすため)
    for item in results[:max(0, min(prefetch, 3))]:
        ensure_download(item["id"])
    return {"results": results}


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