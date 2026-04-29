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
    return {"vpn": vpn, "recordings": recs}


def _launch(rec_id: str):
    rec = recordings[rec_id]
    out_path = RECORDINGS_DIR / rec["filename"]

    if out_path.exists():
        out_path.unlink()

    cmd = [
        "yt-dlp",
        "--format", "bestvideo+bestaudio/best",
        "--merge-output-format", "mp4",
        "--output", str(out_path),
        "--no-part",
        "--no-playlist",
        "--newline",
        "--hls-use-mpegts",
    ]
    if rec.get("from_start"):
        cmd.append("--live-from-start")
    cmd.append(rec["url"])

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    rec["_proc"] = proc
    rec["status"] = "recording"
    rec["progress"] = ""
    rec["started"] = datetime.now().isoformat()

    def monitor():
        last = ""
        for line in proc.stdout:
            line = ANSI.sub("", line).strip()
            if line:
                last = line
                if rec_id in recordings:
                    recordings[rec_id]["progress"] = line[:140]
        proc.wait()
        if rec_id in recordings:
            recordings[rec_id]["status"] = "completed" if out_path.exists() else "failed"
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
    rec["status"] = "stopped"
    rec["_proc"] = None
    return {"status": "stopped"}


@app.delete("/api/recordings/{rec_id}")
def delete_recording(rec_id: str):
    rec = recordings.pop(rec_id, None)
    if not rec:
        raise HTTPException(404, "Not found")
    proc = rec.get("_proc")
    if proc and proc.poll() is None:
        proc.kill()
    path = RECORDINGS_DIR / rec["filename"]
    if path.exists():
        path.unlink()
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
