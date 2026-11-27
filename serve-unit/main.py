import asyncio
import hashlib
import hmac
import html
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


# --- ブラックリスト(ログインが必要・削除済みなど、取得できない動画) ---
BLOCKLIST_FILE = Path(os.environ.get(
    "BLOCKLIST_FILE", str(Path(__file__).resolve().parent / "blocked.json")))
# 取得失敗のうち、このパターンに当たるものだけを登録する(通信の失敗や変換の失敗は登録しない)
PERMANENT_ERROR_RE = re.compile(
    r"login required|sensitive content|premium|payment|members? only|"
    r"video (is )?(unavailable|not available|private|deleted|removed)|"
    r"(has been|was) (deleted|removed)|http error (404|410)",
    re.I,
)


def load_blocked():
    try:
        return json.loads(BLOCKLIST_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"blocklist read failed: {e}", file=sys.stderr)
        return {}


blocked = load_blocked()  # 動画ID -> {"reason": ..., "at": ...}


def save_blocked():
    try:
        tmp = BLOCKLIST_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(blocked, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(BLOCKLIST_FILE)
    except Exception as e:  # 書けなくてもメモリ上では有効
        print(f"blocklist write failed: {e}", file=sys.stderr)


def add_blocked(video_id: str, reason: str):
    line = next((l for l in reason.splitlines() if "ERROR" in l), reason.strip())  # ERROR の行を残す
    blocked[video_id] = {"reason": line.strip()[:120], "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    save_blocked()


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
UPLOADER_TIMEOUT = 1.5                                          # 投稿者名の問い合わせを待つ秒数
uploader_cache = {}      # 動画ID -> 投稿者名
uploader_skip_until = 0.0  # 問い合わせが全滅したとき、この時刻まで問い合わせない
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
            if PERMANENT_ERROR_RE.search(errors[video_id]):
                add_blocked(video_id, errors[video_id])  # 見られない動画は、次回から試さない
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
        if video_id in blocked:
            return {"status": "error", "detail": "blocked: " + blocked[video_id]["reason"]}
        tasks[video_id] = asyncio.create_task(download(video_id))
        return {"status": "processing"}
    return {"status": "processing"}


@app.get("/api/status/{video_id}")
async def status(video_id: str, x_api_key: str = Header(default=None)):
    require_api_key(x_api_key)
    check_video_id(video_id)
    if (CACHE_DIR / f"{video_id}.mp3").exists():
        return {"status": "ready", "url": make_url(video_id)}
    if video_id in blocked:
        return {"status": "error", "detail": "blocked: " + blocked[video_id]["reason"]}
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


def fetch_uploader(video_id: str):
    """投稿者名を取る(ニコニコの非公式API getthumbinfo。失敗したら None)"""
    req = urllib.request.Request(
        f"https://ext.nicovideo.jp/api/getthumbinfo/{video_id}",
        headers={"User-Agent": SEARCH_CONTEXT},
    )
    try:
        with urllib.request.urlopen(req, timeout=UPLOADER_TIMEOUT) as r:
            text = r.read().decode("utf-8", errors="replace")
    except Exception:
        return None
    if 'status="ok"' not in text:
        return None
    for tag in ("user_nickname", "ch_name"):   # 個人の動画 → チャンネルの動画
        m = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.S)
        name = html.unescape(m.group(1)).strip() if m else ""
        if name and name != "-":
            return name
    return None


async def fill_uploaders(items):
    """検索結果に投稿者名(uploader)を付ける。取れなかった曲は None"""
    global uploader_skip_until
    need = [i for i in items if i["id"] not in uploader_cache]
    if need and time.time() >= uploader_skip_until:
        names = await asyncio.gather(
            *(asyncio.to_thread(fetch_uploader, i["id"]) for i in need))
        for i, n in zip(need, names):
            if n:
                uploader_cache[i["id"]] = n
        if not any(names):   # 全滅(APIが止まっている可能性)→ しばらく問い合わせない
            uploader_skip_until = time.time() + 300
    for i in items:
        i["uploader"] = uploader_cache.get(i["id"])


def ensure_download(video_id: str):
    """まだ保存も取得中でもなければ、取得を始める(prepare と同じ動き)"""
    if (CACHE_DIR / f"{video_id}.mp3").exists() or video_id in blocked:
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
        await fill_uploaders(results)
        search_cache[key] = (time.time(), results)

    # ブラックリストの動画(ログインが必要な動画など)は、結果から除く
    results = [r for r in results if r["id"] not in blocked]

    # 上位の曲は、先に取得を始めておく(Alexaの待ち時間を減らすため)
    for item in results[:max(0, min(prefetch, 3))]:
        ensure_download(item["id"])
    return {"results": results}


@app.get("/api/blocked")
async def list_blocked(x_api_key: str = Header(default=None)):
    require_api_key(x_api_key)
    return blocked


@app.delete("/api/blocked/{video_id}")
async def unblock(video_id: str, x_api_key: str = Header(default=None)):
    require_api_key(x_api_key)
    check_video_id(video_id)
    removed = blocked.pop(video_id, None) is not None
    if removed:
        save_blocked()
        errors.pop(video_id, None)
    return {"removed": removed}


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