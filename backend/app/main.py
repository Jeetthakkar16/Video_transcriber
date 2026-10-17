from __future__ import annotations
import json, os, re, shutil, subprocess, threading, uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import whisper
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[2]
DATA = Path(os.getenv("BHASHAFRAME_DATA_DIR", ROOT / "data")).resolve()
UPLOADS, JOBS_DIR = DATA / "uploads", DATA / "jobs"
for p in (UPLOADS, JOBS_DIR): p.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_MB = int(os.getenv("BHASHAFRAME_MAX_UPLOAD_MB", "2048"))
MODELS = {"tiny", "base", "small", "medium", "large", "turbo"}
app = FastAPI(title="BhashaFrame V2", version="2.0.0")
app.mount("/static", StaticFiles(directory=ROOT / "frontend" / "static"), name="static")
JOBS: dict[str, dict[str, Any]] = {}
LOCK = threading.RLock()
MODEL_CACHE: dict[tuple[str, str], Any] = {}

class Segment(BaseModel):
    id: int = Field(ge=1)
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    text: str = Field(max_length=5000)

class SegmentUpdate(BaseModel):
    segments: list[Segment]

class Settings(BaseModel):
    font_family: str = "Arial"
    font_size: int = Field(default=26, ge=12, le=72)
    outline: bool = True
    max_words: int = Field(default=12, ge=1, le=40)

def now():
    return datetime.now(timezone.utc).isoformat()

def folder(job_id: str) -> Path:
    return JOBS_DIR / job_id

def save(job):
    with LOCK:
        job["updated_at"] = now()
        folder(job["id"]).mkdir(parents=True, exist_ok=True)
        (folder(job["id"]) / "job.json").write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
        JOBS[job["id"]] = job

def load_jobs():
    for fp in JOBS_DIR.glob("*/job.json"):
        try:
            job = json.loads(fp.read_text(encoding="utf-8"))
            if job.get("status") in ("queued", "processing", "exporting"):
                job["status"] = "interrupted"
                job["message"] = "Server restarted during this job. Please upload the source video again."
                job["progress"] = 0
                save(job)
            JOBS[job["id"]] = job
        except Exception:
            pass

def clean_job(job):
    # Internal source paths are not exposed to the browser.
    allowed = ("id","status","progress","message","created_at","updated_at","filename","options",
               "segments","detected_language","duration","media","error","settings","download_urls","model","device")
    return {k: job[k] for k in allowed if k in job}

def ffmpeg(args):
    try:
        p = subprocess.run(args, capture_output=True, text=True, check=False)
    except FileNotFoundError as e:
        raise RuntimeError("FFmpeg was not found. Install FFmpeg and make ffmpeg and ffprobe available on PATH.") from e
    if p.returncode:
        raise RuntimeError((p.stderr or p.stdout or "Media command failed")[-2800:])
    return p.stdout

def probe(path: Path):
    if not shutil.which("ffprobe"):
        raise RuntimeError("ffprobe was not found. Install FFmpeg (including ffprobe) and add it to PATH.")
    raw = ffmpeg(["ffprobe","-v","error","-show_streams","-show_format","-of","json",str(path)])
    data = json.loads(raw)
    streams = data.get("streams", [])
    video = next((x for x in streams if x.get("codec_type") == "video"), None)
    audio = next((x for x in streams if x.get("codec_type") == "audio"), None)
    if not video: raise RuntimeError("The uploaded file has no video stream.")
    if not audio: raise RuntimeError("No audio track found to transcribe.")
    fmt = data.get("format", {})
    duration = float(fmt.get("duration") or video.get("duration") or 0)
    v_start = float(video.get("start_time") or fmt.get("start_time") or 0)
    a_start = float(audio.get("start_time") or fmt.get("start_time") or 0)
    return {"duration": duration, "width": video.get("width"), "height": video.get("height"),
            "video_codec": video.get("codec_name"), "audio_codec": audio.get("codec_name"),
            "audio_video_offset": (a_start - v_start)}

def fmt_srt(seconds):
    ms = max(0, round(float(seconds) * 1000))
    h, ms = divmod(ms, 3600000); m, ms = divmod(ms, 60000); s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"

def normalize(segments, duration=0):
    out = []
    for n, s in enumerate(segments, 1):
        text = str(s.get("text", "")).strip()
        if not text: continue
        try: start, end = max(0.0, float(s["start"])), max(0.0, float(s["end"]))
        except (KeyError, TypeError, ValueError): continue
        if duration and duration > 0:
            start, end = min(start, duration), min(end, duration)
        if end <= start:
            if duration and start >= duration: start = max(0, duration - .25)
            end = min(duration, start + .25) if duration else start + .25
        if end <= start: continue
        out.append({"id": int(s.get("id", n)), "start": round(start, 3), "end": round(end, 3), "text": text})
    out.sort(key=lambda x: (x["start"], x["end"]))
    for i, s in enumerate(out, 1): s["id"] = i
    return out

def srt_text(segments):
    return "\n\n".join(str(i) + "\n" + fmt_srt(s["start"]) + " --> " + fmt_srt(s["end"]) + "\n" + s["text"].strip()
                       for i, s in enumerate(segments, 1)) + ("\n" if segments else "")

def vtt_text(segments):
    blocks = ["WEBVTT", ""]
    for s in segments:
        blocks.extend([fmt_srt(s["start"]).replace(",", "." ) + " --> " + fmt_srt(s["end"]).replace(",", "."), s["text"].strip(), ""])
    return "\n".join(blocks).rstrip() + "\n"

def write_artifacts(job):
    d = folder(job["id"]); segs = job.get("segments", [])
    (d / "subtitles.srt").write_text(srt_text(segs), encoding="utf-8")
    (d / "subtitles.vtt").write_text(vtt_text(segs), encoding="utf-8-sig")
    (d / "transcript.txt").write_text("\n".join(s["text"] for s in segs) + ("\n" if segs else ""), encoding="utf-8")
    job["download_urls"] = artifacts(job)

def artifacts(job):
    d = folder(job["id"]); out = {}
    for key, name in (("srt","subtitles.srt"),("vtt","subtitles.vtt"),("txt","transcript.txt"),("video","subtitled_video.mp4")):
        if (d / name).exists(): out[key] = "/api/jobs/" + job["id"] + "/download/" + key
    return out

def set_progress(job_id, value, message):
    with LOCK:
        job = JOBS.get(job_id)
        if job:
            job["progress"], job["message"] = int(value), message
            if value > 0 and job["status"] == "queued": job["status"] = "processing"
            save(job)

def get_model(model_name, device):
    key = (model_name, device)
    if key not in MODEL_CACHE:
        MODEL_CACHE[key] = whisper.load_model(model_name, device=device)
    return MODEL_CACHE[key]

def process(job_id):
    job = JOBS[job_id]
    try:
        job["status"], job["message"] = "processing", "Inspecting audio/video streams"; save(job)
        src = Path(job["source_path"])
        media = probe(src)
        audio = folder(job_id) / "audio_16k_mono.wav"
        set_progress(job_id, 8, "Extracting normalized audio")
        ffmpeg(["ffmpeg","-y","-i",str(src),"-map","0:a:0","-vn","-ac","1","-ar","16000",
                "-af","asetpts=PTS-STARTPTS","-c:a","pcm_s16le",str(audio)])
        set_progress(job_id, 18, "Loading Whisper model")
        device = job["options"]["device"]
        if device == "auto":
            try:
                import torch
                device = "cuda" if torch.cuda.is_available() else "cpu"
            except Exception: device = "cpu"
        if device == "cuda":
            try:
                import torch
                if not torch.cuda.is_available(): raise RuntimeError("PyTorch CUDA is not available in this environment.")
            except ImportError as e: raise RuntimeError("PyTorch is missing. Choose CPU or install a CUDA-enabled PyTorch build.") from e
        model = get_model(job["options"]["model"], device)
        set_progress(job_id, 30, "Transcribing audio with Whisper")
        kwargs = {"task":"translate" if job["options"]["task"] == "translate_to_english" else "transcribe",
                  "word_timestamps":True, "verbose":False}
        language = job["options"]["source_language"]
        if language and language != "auto": kwargs["language"] = language
        result = model.transcribe(str(audio), **kwargs)
        offset = float(media.get("audio_video_offset", 0))
        raw = []
        for i, seg in enumerate(result.get("segments", []), 1):
            raw.append({"id":i,"start":max(0,float(seg.get("start",0))+offset),
                        "end":max(0.05,float(seg.get("end",.05))+offset),"text":str(seg.get("text","")).strip()})
        job["segments"] = normalize(raw, media["duration"])
        if not job["segments"]: raise RuntimeError("No speech segments were detected. Check the audio or try another model.")
        job["detected_language"], job["duration"], job["media"] = result.get("language", language), media["duration"], media
        job["model"], job["device"] = job["options"]["model"], device
        job["settings"] = {"font_family":"Arial","font_size":26,"outline":True,"max_words":12}
        set_progress(job_id, 92, "Writing synchronized subtitle tracks")
        write_artifacts(job)
        job["status"], job["progress"], job["message"] = "completed", 100, "Subtitles ready. Review text and timings before exporting."
        job["download_urls"] = artifacts(job); save(job)
    except Exception as e:
        job["status"], job["error"], job["message"] = "failed", str(e), "Processing failed"
        save(job)

load_jobs()

@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse((ROOT / "frontend" / "index.html").read_text(encoding="utf-8"))

@app.get("/api/health")
def health():
    return {"ok":True,"version":"2.0.0","ffmpeg":bool(shutil.which("ffmpeg")),"ffprobe":bool(shutil.which("ffprobe"))}

@app.get("/api/settings")
def get_settings():
    languages = [
      ("auto","Auto detect"),("en","English"),("hi","Hindi"),("mr","Marathi"),("gu","Gujarati"),
      ("bn","Bengali"),("ta","Tamil"),("te","Telugu"),("kn","Kannada"),("ml","Malayalam"),
      ("pa","Punjabi"),("ur","Urdu"),("or","Odia"),("as","Assamese"),("ne","Nepali"),
      ("sa","Sanskrit"),("es","Spanish"),("fr","French"),("de","German"),("it","Italian"),
      ("pt","Portuguese"),("ru","Russian"),("ja","Japanese"),("ko","Korean"),("zh","Chinese"),
      ("ar","Arabic"),("tr","Turkish"),("id","Indonesian"),("vi","Vietnamese"),("th","Thai"),
      ("nl","Dutch"),("pl","Polish"),("uk","Ukrainian"),("sv","Swedish"),("fi","Finnish"),
      ("el","Greek"),("he","Hebrew"),("cs","Czech"),("ro","Romanian"),("hu","Hungarian")
    ]
    return {"models":["tiny","base","small","medium","large","turbo"],"default_model":os.getenv("BHASHA_MODEL","base"),
            "max_upload_mb":MAX_UPLOAD_MB,"languages":[{"code":a,"name":b} for a,b in languages]}

@app.post("/api/jobs")
async def create_job(background_tasks: BackgroundTasks, video: UploadFile = File(...),
                     source_language: str = Form("auto"), task: str = Form("transcribe"),
                     model: str = Form("base"), device: str = Form("auto")):
    if task not in ("transcribe","translate_to_english"): raise HTTPException(400,"Unsupported processing mode.")
    if model not in MODELS: raise HTTPException(400,"Unsupported Whisper model.")
    suffix = Path(video.filename or "video.mp4").suffix.lower()
    if suffix not in {".mp4",".mov",".mkv",".webm",".avi",".m4v",".mpeg",".mpg"}:
        raise HTTPException(415,"Supported files: MP4, MOV, MKV, WEBM, AVI, M4V, MPEG.")
    job_id = uuid.uuid4().hex[:12]; target = UPLOADS / (job_id + suffix); size = 0
    try:
        with target.open("wb") as dest:
            while True:
                chunk = await video.read(1024 * 1024)
                if not chunk: break
                size += len(chunk)
                if size > MAX_UPLOAD_MB * 1024 * 1024:
                    target.unlink(missing_ok=True); raise HTTPException(413,"Upload exceeds " + str(MAX_UPLOAD_MB) + " MB.")
                dest.write(chunk)
    finally:
        await video.close()
    if size == 0: target.unlink(missing_ok=True); raise HTTPException(400,"Uploaded file is empty.")
    job = {"id":job_id,"status":"queued","progress":0,"message":"Waiting to start","created_at":now(),
           "filename":Path(video.filename or target.name).name,"source_path":str(target),
           "options":{"source_language":source_language,"task":task,"model":model,"device":device},
           "segments":[],"settings":{},"download_urls":{}}
    save(job); background_tasks.add_task(process, job_id)
    return clean_job(job)

@app.get("/api/jobs")
def list_jobs():
    return [clean_job(j) for j in sorted(JOBS.values(),key=lambda x:x.get("created_at",""),reverse=True)]

@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job = JOBS.get(job_id)
    if not job: raise HTTPException(404,"Job not found.")
    job["download_urls"] = artifacts(job)
    return clean_job(job)

@app.put("/api/jobs/{job_id}/subtitles")
def update_subtitles(job_id: str, body: SegmentUpdate):
    job = JOBS.get(job_id)
    if not job: raise HTTPException(404,"Job not found.")
    if job.get("status") != "completed": raise HTTPException(409,"Wait for transcription to finish before editing.")
    duration = float(job.get("duration") or 0)
    raw = [x.model_dump() for x in body.segments]
    for s in raw:
        if s["end"] <= s["start"]: raise HTTPException(422,"Each segment end time must be after its start time.")
        if duration and s["end"] > duration + .05: raise HTTPException(422,"A segment ends beyond the video duration.")
    job["segments"] = normalize(raw,duration)
    write_artifacts(job); save(job)
    return clean_job(job)

@app.post("/api/jobs/{job_id}/settings")
def update_settings(job_id: str, body: Settings):
    job = JOBS.get(job_id)
    if not job: raise HTTPException(404,"Job not found.")
    job["settings"] = body.model_dump(); save(job)
    return clean_job(job)

def escape_filter_path(path: Path):
    value = str(path.resolve()).replace("\\","/").replace(":","\\:").replace("'","\\'").replace(",","\\,")
    return value

@app.post("/api/jobs/{job_id}/export-video")
def export_video(job_id: str):
    job = JOBS.get(job_id)
    if not job: raise HTTPException(404,"Job not found.")
    if job.get("status") != "completed": raise HTTPException(409,"Wait for subtitle generation to finish.")
    d = folder(job_id); srt = d / "subtitles.srt"; output = d / "subtitled_video.mp4"
    # Always render from the latest saved segments, not the first generated SRT.
    srt.write_text(srt_text(job["segments"]),encoding="utf-8")
    settings = job.get("settings") or {}
    family = re.sub(r"[^A-Za-z0-9 _-]","",settings.get("font_family","Arial")) or "Arial"
    style = "FontName=" + family + ",FontSize=" + str(settings.get("font_size",26)) + ",Outline=" + str(2 if settings.get("outline",True) else 0) + ",Shadow=0,MarginV=32"
    vf = "subtitles=filename='" + escape_filter_path(srt) + "':force_style='" + style + "'"
    job["status"], job["message"] = "exporting","Rendering subtitles into MP4"; save(job)
    try:
        ffmpeg(["ffmpeg","-y","-i",str(Path(job["source_path"])),"-vf",vf,"-map","0:v:0","-map","0:a?",
                "-c:v","libx264","-preset","medium","-crf","20","-c:a","aac","-b:a","192k","-movflags","+faststart",str(output)])
        job["status"], job["message"] = "completed","Subtitle video export completed"
        job["download_urls"] = artifacts(job); save(job)
        return {"ok":True,"download_url":job["download_urls"]["video"]}
    except Exception as e:
        job["status"],job["error"],job["message"]="completed",str(e),"Video export failed"
        save(job); raise HTTPException(500,"Video export failed: " + str(e))

@app.get("/api/jobs/{job_id}/download/{kind}")
def download(job_id: str, kind: str):
    job = JOBS.get(job_id)
    if not job: raise HTTPException(404,"Job not found.")
    mapping = {"srt":("subtitles.srt","application/x-subrip"),"vtt":("subtitles.vtt","text/vtt"),
               "txt":("transcript.txt","text/plain"),"video":("subtitled_video.mp4","video/mp4")}
    if kind not in mapping: raise HTTPException(404,"Unknown export type.")
    name, mime = mapping[kind]; path = folder(job_id) / name
    if not path.exists(): raise HTTPException(404,"This export is not ready yet.")
    return FileResponse(path,media_type=mime,filename=Path(job["filename"]).stem + "_" + name)

@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    job = JOBS.pop(job_id,None)
    if not job: raise HTTPException(404,"Job not found.")
    try: Path(job.get("source_path","")).unlink(missing_ok=True)
    except Exception: pass
    shutil.rmtree(folder(job_id),ignore_errors=True)
    return {"ok":True}
