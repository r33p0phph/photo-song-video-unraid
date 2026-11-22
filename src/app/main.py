"""Photo and Song Video Maker: upload a picture or clip plus a song, get one video back.

Rendering is done by ffmpeg, on the GPU (NVENC) when one is available, otherwise on the CPU.
"""
import asyncio
import logging
import os
import re
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from . import render

log = logging.getLogger("psv")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

APP_DIR = Path(__file__).resolve().parent
DATA = Path(os.getenv("DATA_DIR", "/data"))
JOBS_DIR = DATA / "jobs"
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "4096"))
MAX_LENGTH = int(os.getenv("MAX_LENGTH_SECONDS", "3600"))
RETENTION_H = float(os.getenv("RETENTION_HOURS", "12"))
APP_NAME = "Photo and Song Video Maker"     # public name, shown to people
BUILD = os.getenv("PSV_BUILD", "dev")         # build label, set when the image is built
MAX_PICTURES = int(os.getenv("MAX_PICTURES", "60"))
ENCODER_PREF = os.getenv("ENCODER", "auto").lower()  # auto | nvenc | cpu
CONCURRENCY = max(1, int(os.getenv("MAX_CONCURRENT_JOBS", "1")))

JOBS = {}
SEM = asyncio.Semaphore(CONCURRENCY)


class Cancelled(Exception):
    pass


class Job:
    def __init__(self, jid, name, settings):
        self.id = jid
        self.dir = JOBS_DIR / jid
        self.name = name
        self.settings = settings
        self.status = "queued"  # queued | running | done | error | cancelled
        self.stage = "Uploading done, waiting"
        self.progress = 0.0
        self.speed = None
        self.error = None
        self.created = time.time()
        self.started = None
        self.finished = None
        self.render_seconds = None
        self.opts = None
        self.encoder = None
        self.bytes = 0
        self.proc = None
        self.cancelled = False
        self.task = None

    @property
    def download_name(self):
        base = re.sub(r"[^\w\- ]+", "", Path(self.name).stem).strip()[:60] or "video"
        return base + "-video.mp4"

    def public(self):
        o = self.opts or {}
        return {
            "id": self.id,
            "status": self.status,
            "stage": self.stage,
            "progress": round(self.progress, 4),
            "speed": self.speed,
            "error": self.error,
            "length": o.get("D"),
            "width": o.get("W"),
            "height": o.get("H"),
            "encoder": self.encoder,
            "bytes": self.bytes,
            "render_seconds": self.render_seconds,
            "filename": self.download_name,
        }


# ------------------------------------------------------------------ encoder detection
async def _run_quiet(*cmd, timeout=30):
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
        return proc.returncode, out.decode(errors="ignore"), err.decode(errors="ignore")
    except (FileNotFoundError, asyncio.TimeoutError):
        return 1, "", ""


async def detect_encoder():
    if ENCODER_PREF == "cpu":
        return "cpu", None
    rc, _, err = await _run_quiet(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
        "color=c=black:s=256x256:d=0.3", "-frames:v", "5", "-c:v", "h264_nvenc", "-f", "null", "-")
    gpu = None
    if rc == 0:
        rc2, out, _ = await _run_quiet("nvidia-smi", "--query-gpu=name", "--format=csv,noheader")
        if rc2 == 0 and out.strip():
            gpu = out.strip().splitlines()[0].strip()
        return "nvenc", gpu
    log.warning("NVENC is not available (%s). Falling back to the CPU.", err.strip()[-300:])
    return ("nvenc", None) if ENCODER_PREF == "nvenc" else ("cpu", None)


# ------------------------------------------------------------------ job processing
def prepare(job):
    """Probe and clean the uploaded files, then work out the final settings. Runs in a thread."""
    s = job.settings
    vis_ups = sorted(job.dir.glob("visual*.upload"))
    aud_up = job.dir / "audio.upload"
    rots = s.get("rotations") or []

    pics = []
    kind, vis_path, vinfo = "image", None, None
    try:
        for i, up in enumerate(vis_ups):
            w, h = render.normalise_image(up, job.dir / ("pic%03d.png" % i), rotate=rots[i] if i < len(rots) else 0)
            pics.append(str(job.dir / ("pic%03d.png" % i)))
            if vinfo is None:  # the first picture decides the shape when "same as the picture" is chosen
                vinfo = {"w": w, "h": h, "duration": 0, "has_audio": False}
    except render.NotAnImage:
        if len(vis_ups) > 1:
            raise render.InputError("When you add several files, they all need to be pictures. A video has to be used on its own.")
        vinfo = render.video_info(render.probe(vis_ups[0]))
        kind, vis_path, pics = "video", vis_ups[0], []

    ainfo, aud_path = None, None
    if aud_up.exists():
        ainfo = render.audio_info(render.probe(aud_up))
        aud_path = aud_up

    start = max(0.0, s["start"])
    length = s["length"]
    if ainfo:
        avail = ainfo["duration"] - start
        if avail < 0.5:
            raise render.InputError("The start time is past the end of the song.")
        D = min(length, avail) if length > 0 else avail
    elif length > 0:
        D = length
    else:
        D = vinfo["duration"] if kind == "video" and vinfo["duration"] > 0 else 10.0
    D = min(D, float(MAX_LENGTH))
    if D < 0.5:
        raise render.InputError("The video length is too short. Use at least 1 second.")

    W, H = render.out_size(s["fmt"], s["quality"], vinfo["w"], vinfo["h"])
    job.opts = {
        "kind": kind, "visual": str(vis_path) if vis_path else None, "pictures": pics,
        "per": s.get("per", 0.0), "trans": s.get("trans", "fade"),
        "audio": str(aud_path) if aud_path else None,
        "W": W, "H": H, "D": D, "start": start, "fit": s["fit"], "zoom": s["zoom"],
        "fade": s["fade"], "bars": s["bars"], "keep": s["keep_sound"],
        "vis_has_audio": vinfo["has_audio"],
    }


async def run_ffmpeg(job, cmd):
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    job.proc = proc
    D = job.opts["D"]
    err_buf = bytearray()

    async def drain_err():
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                break
            err_buf.extend(chunk)
            del err_buf[:-8192]

    drain = asyncio.create_task(drain_err())
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        key, _, val = line.decode(errors="ignore").strip().partition("=")
        if key in ("out_time_us", "out_time_ms"):
            try:
                job.progress = max(job.progress, min(0.99, int(val) / 1e6 / D))
            except ValueError:
                pass
        elif key == "speed" and val and val != "N/A":
            job.speed = val
    rc = await proc.wait()
    await drain
    job.proc = None
    return rc, err_buf.decode(errors="ignore").strip()


async def render_job(job):
    o = job.opts
    out = job.dir / "output.mp4"
    video_in = o["kind"] == "video"
    attempts = []
    if app.state.encoder == "nvenc":
        attempts.append(("nvenc", video_in))
        if video_in:
            attempts.append(("nvenc", False))
    attempts.append(("cpu", False))

    last_err = ""
    for enc, hw in attempts:
        if job.cancelled:
            raise Cancelled()
        job.progress, job.speed = 0.0, None
        cmd = render.build_cmd(o, enc, hw, str(out))
        log.info("job %s: %s (hwdec=%s) %dx%d %.1fs", job.id, enc, hw, o["W"], o["H"], o["D"])
        rc, err = await run_ffmpeg(job, cmd)
        if job.cancelled:
            raise Cancelled()
        if rc == 0 and out.exists() and out.stat().st_size > 0:
            job.encoder = enc
            job.bytes = out.stat().st_size
            return
        last_err = err
        log.warning("job %s: %s attempt failed: %s", job.id, enc, err[-600:])
    raise RuntimeError("Rendering failed. " + last_err[-300:])


async def process(job):
    try:
        job.stage = "Reading your files"
        await run_in_threadpool(prepare, job)
        job.stage = "Waiting for the encoder"
        async with SEM:
            if job.cancelled:
                raise Cancelled()
            job.status = "running"
            job.stage = "Rendering"
            job.started = time.time()
            await render_job(job)
        job.render_seconds = time.time() - job.started
        job.progress = 1.0
        job.status = "done"
        job.stage = "Done"
        log.info("job %s done in %.1fs (%.1fx)", job.id, job.render_seconds,
                 job.opts["D"] / max(job.render_seconds, 0.01))
    except Cancelled:
        job.status, job.stage = "cancelled", "Cancelled"
    except render.InputError as e:
        log.warning("job %s rejected: %s", job.id, e)
        job.status, job.error, job.stage = "error", str(e), "Failed"
    except Exception as e:  # noqa: BLE001
        log.exception("job %s failed", job.id)
        job.status, job.error, job.stage = "error", str(e) or "Something went wrong while rendering.", "Failed"
    finally:
        job.finished = time.time()
        for name in ["audio.upload"] + [p.name for p in job.dir.glob("visual*.upload")] + [p.name for p in job.dir.glob("pic*.png")]:
            try:
                (job.dir / name).unlink()
            except OSError:
                pass
        if job.status != "done":
            try:
                (job.dir / "output.mp4").unlink()
            except OSError:
                pass


async def janitor():
    while True:
        await asyncio.sleep(300)
        now = time.time()
        for jid, job in list(JOBS.items()):
            if job.finished and now - job.finished > RETENTION_H * 3600:
                JOBS.pop(jid, None)
                shutil.rmtree(job.dir, ignore_errors=True)


@asynccontextmanager
async def lifespan(_app):
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    for child in JOBS_DIR.iterdir():  # leftovers from a previous run
        shutil.rmtree(child, ignore_errors=True)
    enc, gpu = await detect_encoder()
    _app.state.encoder, _app.state.gpu = enc, gpu
    log.info("Encoder: %s%s", "NVIDIA NVENC" if enc == "nvenc" else "CPU (libx264)", " on " + gpu if gpu else "")
    task = asyncio.create_task(janitor())
    yield
    task.cancel()


app = FastAPI(title=APP_NAME, version=BUILD, lifespan=lifespan)
app.state.encoder, app.state.gpu = "cpu", None


@app.middleware("http")
async def limit_upload(request: Request, call_next):
    if request.method == "POST" and request.url.path == "/api/jobs":
        try:
            size = int(request.headers.get("content-length", "0"))
        except ValueError:
            size = 0
        if size > MAX_UPLOAD_MB * 1024 * 1024:
            return JSONResponse({"detail": "The files are too large. The limit is %d MB." % MAX_UPLOAD_MB},
                                status_code=413)
    return await call_next(request)


# ------------------------------------------------------------------ API
@app.get("/api/health")
def health():
    return {"name": APP_NAME, "build": BUILD, "encoder": app.state.encoder, "gpu": app.state.gpu,
            "max_upload_mb": MAX_UPLOAD_MB, "max_length_seconds": MAX_LENGTH}


async def save_upload(upload: UploadFile, dest: Path):
    def _copy():
        with open(dest, "wb") as f:
            shutil.copyfileobj(upload.file, f, 1 << 20)
    await run_in_threadpool(_copy)


@app.post("/api/jobs")
async def create_job(
    visual: list[UploadFile] = File(...),
    audio: UploadFile | None = File(None),
    rotations: str = Form(""),
    per: float = Form(0.0),
    trans: str = Form("fade"),
    fmt: str = Form("source"),
    quality: int = Form(1080),
    fit: str = Form("fit"),
    zoom: bool = Form(True),
    fade: bool = Form(True),
    bars: bool = Form(False),
    keep_sound: bool = Form(False),
    start: float = Form(0.0),
    length: float = Form(0.0),
):
    if fmt not in ("source", "vertical", "square", "wide"):
        raise HTTPException(400, "Unknown shape.")
    if quality not in render.LONG_SIDE:
        raise HTTPException(400, "Unknown quality.")
    if fit not in ("fit", "fill"):
        raise HTTPException(400, "Unknown placement.")
    if trans not in ("fade", "slide", "cut"):
        raise HTTPException(400, "Unknown transition.")
    if not visual or len(visual) > MAX_PICTURES:
        raise HTTPException(400, "Add between 1 and %d pictures." % MAX_PICTURES)
    try:
        rots = [int(float(x)) % 360 for x in rotations.split(",") if x.strip() != ""]
    except ValueError:
        raise HTTPException(400, "Unknown rotation.")
    if any(r not in (0, 90, 180, 270) for r in rots):
        raise HTTPException(400, "Unknown rotation.")
    settings = {
        "fmt": fmt, "quality": quality, "fit": fit, "zoom": zoom, "fade": fade, "bars": bars,
        "keep_sound": keep_sound, "start": max(0.0, min(start, 86400.0)),
        "length": max(0.0, min(length, float(MAX_LENGTH))),
        "rotations": rots, "per": max(0.0, min(per, 600.0)), "trans": trans,
    }
    jid = uuid.uuid4().hex[:12]
    job = Job(jid, visual[0].filename or "video", settings)
    job.dir.mkdir(parents=True)
    try:
        for i, up in enumerate(visual):
            await save_upload(up, job.dir / ("visual%03d.upload" % i))
            if (job.dir / ("visual%03d.upload" % i)).stat().st_size == 0:
                raise HTTPException(400, "The picture or video file is empty.")
        if audio is not None and audio.filename:
            await save_upload(audio, job.dir / "audio.upload")
            if (job.dir / "audio.upload").stat().st_size == 0:
                (job.dir / "audio.upload").unlink()
    except HTTPException:
        shutil.rmtree(job.dir, ignore_errors=True)
        raise
    JOBS[jid] = job
    job.task = asyncio.create_task(process(job))
    return {"id": jid}


def _get(jid):
    job = JOBS.get(jid)
    if not job:
        raise HTTPException(404, "This job is gone. Results are kept for %g hours." % RETENTION_H)
    return job


@app.get("/api/jobs/{jid}")
def job_status(jid: str):
    return _get(jid).public()


@app.get("/api/jobs/{jid}/video")
def job_video(jid: str, dl: int = 0):
    job = _get(jid)
    path = job.dir / "output.mp4"
    if job.status != "done" or not path.exists():
        raise HTTPException(404, "The video isn't ready.")
    if dl:
        return FileResponse(path, media_type="video/mp4", filename=job.download_name)
    return FileResponse(path, media_type="video/mp4")


@app.delete("/api/jobs/{jid}")
async def job_delete(jid: str):
    job = _get(jid)
    if job.status in ("queued", "running"):
        job.cancelled = True
        if job.proc:
            try:
                job.proc.terminate()
            except ProcessLookupError:
                pass
        return {"ok": True, "status": "cancelling"}
    JOBS.pop(jid, None)
    shutil.rmtree(job.dir, ignore_errors=True)
    return {"ok": True}


app.mount("/", StaticFiles(directory=APP_DIR / "static", html=True), name="static")
