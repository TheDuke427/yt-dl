from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import subprocess
import uuid
import os
import re
import threading
from pathlib import Path
from datetime import datetime
from typing import Optional
import httpx

app = FastAPI()

RECORDINGS_DIR = Path(os.getenv("RECORDINGS_DIR", "/recordings"))
COOKIES_PATH = Path(os.getenv("COOKIES_DIR", "/cookies")) / "cookies.txt"
GLUETUN = "http://localhost:8000"
ANSI = re.compile(r"\x1b\[[0-9;]*m")

RECORDINGS_DIR.mkdir(exist_ok=True)

recordings: dict = {}


class RecordRequest(BaseModel):
    url: str
    from_start: bool = False
    filename: Optional[str] = None


@app.get("/api/status")
async def status():
    vpn = {"status": "unknown", "public_ip": None, "region": None}
    try:
        async with httpx.AsyncClient() as client:
            vpn_r = await client.get(f"{GLUETUN}/v1/vpn/status", timeout=3)
            ip_r = await client.get(f"{GLUETUN}/v1/publicip/ip", timeout=3)
            vpn["status"] = vpn_r.json().get("status", "unknown")
            ip_data = ip_r.json()
            vpn["public_ip"] = ip_data.get("public_ip")
            vpn["region"] = ip_data.get("region") or ip_data.get("country")
    except Exception:
        pass

    recs = [
        {k: v for k, v in r.items() if not k.startswith("_")}
        for r in recordings.values()
    ]
    return {"vpn": vpn, "recordings": recs, "cookies_loaded": COOKIES_PATH.exists()}


def _launch(rec_id: str):
    rec = recordings[rec_id]
    mp4_path = RECORDINGS_DIR / rec["filename"]
    ts_path = mp4_path.with_suffix(".ts")

    for p in (mp4_path, ts_path):
        if p.exists():
            p.unlink()

    # Record as .ts — MPEG-TS streams to disk in real time.
    # mp4 muxing buffers everything in memory until the stream ends,
    # so the file would never grow during a live recording.
    cmd = [
        "yt-dlp",
        "--format", "bestvideo+bestaudio/best",
        "--output", str(ts_path),
        "--no-part",
        "--no-playlist",
        "--newline",
        "--hls-use-mpegts",
    ]
    if rec.get("from_start"):
        cmd.append("--live-from-start")
    if COOKIES_PATH.exists():
        cmd.extend(["--cookies", str(COOKIES_PATH)])
    cmd.append(rec["url"])

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    rec.update({
        "_proc": proc,
        "_ts_path": ts_path,
        "status": "recording",
        "progress": "",
        "started": datetime.now().isoformat(),
    })

    def monitor():
        last = ""
        for line in proc.stdout:
            line = ANSI.sub("", line).strip()
            if line:
                last = line
                if rec_id in recordings:
                    recordings[rec_id]["progress"] = line[:140]
        proc.wait()

        if rec_id not in recordings:
            ts_path.unlink(missing_ok=True)
            return

        if ts_path.exists():
            recordings[rec_id]["status"] = "converting"
            recordings[rec_id]["progress"] = "Converting to mp4…"
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(ts_path), "-c", "copy", str(mp4_path)],
                capture_output=True,
            )
            ts_path.unlink(missing_ok=True)
            recordings[rec_id]["status"] = "completed" if mp4_path.exists() else "failed"
            recordings[rec_id]["progress"] = "" if mp4_path.exists() else "Conversion failed"
        else:
            recordings[rec_id]["status"] = "failed"
            recordings[rec_id]["progress"] = last[:140]

        recordings[rec_id]["_proc"] = None

    threading.Thread(target=monitor, daemon=True).start()


@app.post("/api/record/start")
def start_recording(req: RecordRequest):
    rec_id = str(uuid.uuid4())[:8]
    safe = re.sub(r"[^\w\-]", "_", req.filename or f"stream_{rec_id}")

    recordings[rec_id] = {
        "id": rec_id,
        "url": req.url,
        "filename": f"{safe}.mp4",
        "from_start": req.from_start,
        "status": "recording",
        "started": datetime.now().isoformat(),
        "progress": "",
        "_proc": None,
        "_ts_path": None,
    }
    _launch(rec_id)
    return {"id": rec_id}


@app.post("/api/record/retry/{rec_id}")
def retry_recording(rec_id: str):
    rec = recordings.get(rec_id)
    if not rec:
        raise HTTPException(404, "Not found")
    if rec.get("status") not in ("failed", "stopped"):
        raise HTTPException(400, "Only failed or stopped recordings can be retried")
    _launch(rec_id)
    return {"id": rec_id}


@app.post("/api/record/stop/{rec_id}")
def stop_recording(rec_id: str):
    rec = recordings.get(rec_id)
    if not rec:
        raise HTTPException(404, "Not found")
    proc = rec.get("_proc")
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    rec["_proc"] = None
    return {"status": "stopping"}


@app.delete("/api/recordings/{rec_id}")
def delete_recording(rec_id: str):
    rec = recordings.pop(rec_id, None)
    if not rec:
        raise HTTPException(404, "Not found")
    proc = rec.get("_proc")
    if proc and proc.poll() is None:
        proc.kill()
    for path in [rec.get("_ts_path"), RECORDINGS_DIR / rec["filename"]]:
        if path:
            Path(path).unlink(missing_ok=True)
    return {"status": "deleted"}


@app.get("/api/recordings/{rec_id}/download")
def download_recording(rec_id: str):
    rec = recordings.get(rec_id)
    if not rec:
        raise HTTPException(404, "Not found")
    path = RECORDINGS_DIR / rec["filename"]
    if not path.exists():
        raise HTTPException(404, "File not on disk")
    return FileResponse(str(path), media_type="video/mp4", filename=rec["filename"])


app.mount("/", StaticFiles(directory="/app/frontend", html=True), name="static")
