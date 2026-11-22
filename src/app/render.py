"""ffmpeg helpers: probing, input clean-up and command building."""
import json
import math
import subprocess
from pathlib import Path

try:  # HEIC/HEIF pictures from iPhones
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover - optional
    pass

from PIL import Image, ImageFile, ImageOps, UnidentifiedImageError

# Photos that were cut short while syncing or copying still open, with the missing part left grey.
ImageFile.LOAD_TRUNCATED_IMAGES = True

FPS = 30
LONG_SIDE = {720: 1280, 1080: 1920, 2160: 3840}
SHORT_SIDE = {720: 720, 1080: 1080, 2160: 2160}

# Only these containers are accepted for uploaded video/audio. This keeps ffmpeg away from
# playlist-style inputs (hls, concat, sdp ...) that can point at other files or hosts.
VIDEO_FORMATS = {"mov", "mp4", "matroska", "webm", "avi", "mpegts", "asf"}
AUDIO_FORMATS = {
    "mp3", "wav", "flac", "ogg", "aac", "m4a", "mov", "mp4", "matroska", "webm",
    "aiff", "asf", "ac3", "eac3", "w64", "caf", "avi", "mpegts",
}


# Still-picture containers that ffmpeg can decode when Pillow cannot.
IMAGE_FORMATS = {"image2", "jpeg_pipe", "png_pipe", "webp_pipe", "bmp_pipe", "tiff_pipe", "jpegls_pipe", "jpeg2000_pipe", "gif"}


class InputError(ValueError):
    """The uploaded file cannot be used. The message is safe to show to the user."""


class NotAnImage(Exception):
    pass


def even(n):
    return max(2, int(round(n / 2.0)) * 2)


# ---------------------------------------------------------------- probing
def probe(path: Path) -> dict:
    try:
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-protocol_whitelist", "file",
             "-show_format", "-show_streams", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        raise InputError("Reading the file took too long.")
    if p.returncode != 0:
        raise InputError("This file can't be read. It may be damaged, or not a picture, video or song.")
    try:
        return json.loads(p.stdout)
    except ValueError:
        raise InputError("This file can't be read. It may be damaged, or not a picture, video or song.")


def _format_ok(meta: dict, allowed: set) -> bool:
    names = set((meta.get("format", {}).get("format_name") or "").split(","))
    return bool(names & allowed)


def video_info(meta: dict) -> dict:
    if not _format_ok(meta, VIDEO_FORMATS):
        raise InputError("This file isn't a picture or video that can be used. Use JPG, PNG, HEIC, MP4, MOV, MKV or WebM.")
    streams = meta.get("streams", [])
    v = next((s for s in streams
              if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")), None)
    if not v:
        raise InputError("No video track found in this file.")
    w, h = int(v["width"]), int(v["height"])
    rot = 0
    for sd in v.get("side_data_list") or []:
        if "rotation" in sd:
            try:
                rot = int(round(float(sd["rotation"])))
            except (TypeError, ValueError):
                pass
    tag = (v.get("tags") or {}).get("rotate")
    if tag is not None:
        try:
            rot = int(float(tag))
        except (TypeError, ValueError):
            pass
    if abs(rot) % 180 == 90:
        w, h = h, w
    dur = float(meta["format"].get("duration") or v.get("duration") or 0)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    return {"w": w, "h": h, "duration": dur, "has_audio": has_audio}


def audio_info(meta: dict) -> dict:
    if not _format_ok(meta, AUDIO_FORMATS):
        raise InputError("This sound file isn't supported. Use MP3, M4A, WAV, FLAC or OGG, or a video file (MP4, MOV, MKV or WebM).")
    if not any(s.get("codec_type") == "audio" for s in meta.get("streams", [])):
        raise InputError("There is no sound in that file, so it can't be used as the song.")
    dur = float(meta["format"].get("duration") or 0)
    if dur <= 0:
        raise InputError("Couldn't work out how long the sound is.")
    return {"duration": dur}


# ---------------------------------------------------------------- pictures
def _still_with_ffmpeg(src: Path, dst: Path):
    """Second chance for a picture Pillow cannot decode: let ffmpeg read the first frame."""
    try:
        if not _format_ok(probe(src), IMAGE_FORMATS):
            return None
        tmp = dst.with_name("ffstill.png")
        p = subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-protocol_whitelist", "file", "-i", str(src),
             "-frames:v", "1", "-an", str(tmp)],
            capture_output=True, timeout=120)
        if p.returncode != 0 or not tmp.exists():
            return None
        im = Image.open(tmp)
        im.load()
        tmp.unlink()
        return im
    except Exception:
        return None


def normalise_image(src: Path, dst: Path, max_side: int = 4096, rotate: int = 0):
    """Decode with Pillow (HEIC, WebP, GIF, EXIF rotation all handled), flatten to RGB, write a clean PNG."""
    try:
        im = Image.open(src)
        im.load()
    except Image.DecompressionBombError:
        raise InputError("This picture is too large to process.")
    except Exception:  # not a picture Pillow knows, or one it cannot decode
        im = _still_with_ffmpeg(src, dst)
        if im is None:
            raise NotAnImage()
    try:
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
            rgba = im.convert("RGBA")
            flat = Image.new("RGB", rgba.size, (0, 0, 0))
            flat.paste(rgba, mask=rgba.split()[-1])
            im = flat
        else:
            im = im.convert("RGB")
        turn = {90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180, 270: Image.Transpose.ROTATE_90}.get(rotate % 360)
        if turn is not None:  # rotate is clockwise degrees chosen in the page
            im = im.transpose(turn)
        im.thumbnail((max_side, max_side), Image.LANCZOS)
        w, h = im.size
        im.save(dst, "PNG", compress_level=1)
    except Exception:
        raise InputError("This picture couldn't be converted.")
    return w, h


# ---------------------------------------------------------------- sizes
def out_size(fmt: str, quality: int, sw: int, sh: int):
    q = SHORT_SIDE[quality]
    if fmt == "vertical":
        return q, even(q * 16 / 9)
    if fmt == "square":
        return q, q
    if fmt == "wide":
        return even(q * 16 / 9), q
    sc = LONG_SIDE[quality] / float(max(sw, sh))
    return even(sw * sc), even(sh * sc)


# ---------------------------------------------------------------- ffmpeg command
MAX_SEGMENTS = 60
MOTION = 0.16          # how far the picture zooms or pans: 16 percent
LONG_SEGMENT = 20.0    # a picture shown longer than this keeps moving back and forth
CYCLE_SECONDS = 16.0


def plan_segments(D: float, n: int, per: float, trans: str):
    """Work out the slideshow: (seconds each picture owns, number of segments, overlap seconds)."""
    if n <= 1:
        return D, 1, 0.0
    L = per if per and per > 0 else D / n
    L = max(L, 2.0)
    count = int(math.ceil(D / L - 1e-6))    # pictures start over if the song is longer than one pass
    count = max(1, min(count, MAX_SEGMENTS))
    L = D / count if (not per or per <= 0 or count == MAX_SEGMENTS or count == 1) else L
    T = 0.0 if trans == "cut" else min(1.0, L / 3.0)
    return L, count, T


def motion_filter(i: int, frames: int, seconds: float, zoom: bool, W: int, H: int) -> str:
    """zoompan for one picture. The input is a single frame, zoompan makes all `frames` frames from it."""
    base = "d=%d:s=%dx%d:fps=%d" % (frames, W, H, FPS)
    if not zoom:
        return "zoompan=z=1:%s" % base
    long_ = seconds > LONG_SEGMENT
    if long_:
        ph = "(1-cos(2*PI*on/%d))/2" % int(CYCLE_SECONDS * FPS)
    else:
        ph = "(1-cos(PI*on/%d))/2" % max(1, frames - 1)
    centre_x, centre_y = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    style = ("in", "out", "left", "right")[i % 4]
    if long_:
        style = "in" if i % 2 == 0 else "left"
    if style == "in":
        z, x = "1+%.2f*%s" % (MOTION, ph), centre_x
    elif style == "out":
        z, x = "1+%.2f*(1-%s)" % (MOTION, ph), centre_x
    elif style == "left":
        z, x = "%.2f" % (1 + MOTION), "(iw-iw/zoom)*%s" % ph
    else:
        z, x = "%.2f" % (1 + MOTION), "(iw-iw/zoom)*(1-%s)" % ph
    return "zoompan=z='%s':x='%s':y='%s':%s" % (z, x, centre_y, base)


def build_cmd(o: dict, enc: str, hwdec: bool, out_path: str) -> list:
    """o: kind, pictures (image) or visual (video), audio, W, H, D, start, fit, zoom, fade, bars, keep,
    vis_has_audio, per (seconds per picture, 0 = automatic), trans (fade, slide or cut)."""
    W, H, D = o["W"], o["H"], o["D"]
    fin, fout = min(0.6, D / 6.0), min(1.5, D / 4.0)
    kind = o["kind"]
    song = o["audio"] is not None
    vsound = kind == "video" and o["vis_has_audio"] and (not song or o["keep"])
    has_audio = song or vsound
    bars = bool(o["bars"]) and has_audio
    zoom = bool(o["zoom"]) and kind == "image"
    pics = o.get("pictures") or []
    L, count, T = plan_segments(D, len(pics), o.get("per", 0), o.get("trans", "fade")) if kind == "image" else (D, 1, 0.0)

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
           "-progress", "pipe:1", "-nostats"]
    if kind == "image":  # one frame in per picture; zoompan turns it into all the frames (a looped PNG would be re-decoded every frame)
        for i in range(count):
            cmd += ["-protocol_whitelist", "file", "-i", pics[i % len(pics)]]
        ai = count                      # index of the audio input
    else:
        if enc == "nvenc" and hwdec:
            cmd += ["-hwaccel", "cuda"]
        cmd += ["-protocol_whitelist", "file", "-stream_loop", "-1", "-i", o["visual"]]
        ai = 1
    if song:
        # -vn: if this file is a video, only its sound is used and its picture is never decoded
        cmd += ["-protocol_whitelist", "file", "-vn", "-ss", "%.3f" % o["start"], "-t", "%.3f" % D, "-i", o["audio"]]

    parts = []

    def compose(src, cw, ch, out):
        if o["fit"] == "fill":
            return ("[%s]scale=%d:%d:force_original_aspect_ratio=increase:force_divisible_by=2,"
                    "crop=%d:%d,setsar=1[%s]" % (src, cw, ch, cw, ch, out))
        bw, bh = even(cw / 8.0), even(ch / 8.0)
        return (
            "[%s]split=2[%s_bg0][%s_fg0];"
            "[%s_bg0]scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,"
            "scale=%d:%d,gblur=sigma=4,scale=%d:%d,eq=brightness=-0.12[%s_bg];"
            "[%s_fg0]scale=%d:%d:force_original_aspect_ratio=decrease:force_divisible_by=2[%s_fg];"
            "[%s_bg][%s_fg]overlay=(W-w)/2:(H-h)/2,setsar=1[%s]"
            % (src, out, out, out, cw, ch, cw, ch, bw, bh, cw, ch, out,
               out, cw, ch, out, out, out, out)
        )

    if kind == "image":
        k = 2.0 if (zoom and W <= 1920) else (1.25 if zoom else 1.0)
        cw, ch = even(W * k), even(H * k)
        seg_frames = max(2, int(round((L + T) * FPS)))
        for i in range(count):
            parts.append(compose("%d:v:0" % i, cw, ch, "p%d" % i))
            parts.append("[p%d]%s,format=yuv420p,setsar=1[z%d]" % (i, motion_filter(i, seg_frames, L + T, zoom, W, H), i))
        if count == 1:
            parts.append("[z0]null[c1]")
        elif T == 0:
            parts.append("%sconcat=n=%d:v=1:a=0[c1]" % ("".join("[z%d]" % i for i in range(count)), count))
        else:
            xt = "slideleft" if o.get("trans") == "slide" else "fade"
            prev = "z0"
            for i in range(1, count):
                out = "c1" if i == count - 1 else "x%d" % i
                parts.append("[%s][z%d]xfade=transition=%s:duration=%.3f:offset=%.3f[%s]" % (prev, i, xt, T, i * L, out))
                prev = out
    elif kind == "video":
        parts.append("[0:v:0]fps=%d[v0]" % FPS)
        parts.append(compose("v0", W, H, "c1"))

    cur = "c1"
    if bars:
        n = 48                                   # bars on screen
        bh = even(H * 0.14)
        bottom = even(H * 0.05)
        colw = max(4, int(W * 0.8 / n))          # width of one bar slot in pixels
        vw = colw * n
        gap = max(2, colw // 4)
        # Spectrum rendered 56 columns wide, top 8 (highest frequencies) dropped, bottom baseline row cropped,
        # columns widened to chunky bars with gaps, black made transparent.
        parts.append(
            "[aviz]volume=3,showfreqs=s=56x%d:mode=bar:fscale=log:ascale=sqrt:win_size=2048:averaging=3:"
            "colors=white:rate=%d,crop=%d:ih-3:0:0,scale=%d:%d:flags=neighbor,"
            "drawgrid=w=%d:h=0:t=%d:c=black,format=rgba,colorkey=0x000000:0.25:0.1[viz]"
            % (bh, FPS, n, vw, bh - 3, colw, gap))
        parts.append("[%s]drawbox=x=0:y=%d:w=iw:h=%d:color=black@0.3:t=fill[c2]" % (cur, H - bh * 2, bh * 2))
        parts.append("[c2][viz]overlay=%d:%d:format=auto[c3]" % ((W - vw) // 2, H - bh - bottom))
        cur = "c3"

    tail = "[%s]" % cur
    if o["fade"]:
        tail += "fade=t=in:st=0:d=%.3f,fade=t=out:st=%.3f:d=%.3f," % (fin, max(0, D - fout), fout)
    tail += "format=yuv420p[vout]"
    parts.append(tail)

    if has_audio:
        norm = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
        if song:
            parts.append("[%d:a:0]%s[sa]" % (ai, norm))
        if vsound:
            parts.append("[0:a:0]%s,volume=%s[va]" % (norm, "0.5" if song else "1.0"))
        if song and vsound:
            parts.append("[va][sa]amix=inputs=2:duration=longest:dropout_transition=0,"
                         "volume=2,alimiter=limit=0.97[am]")
        else:
            parts.append("[%s]anull[am]" % ("sa" if song else "va"))
        fl = []
        if o["fade"]:
            fl += ["afade=t=in:st=0:d=%.3f" % fin, "afade=t=out:st=%.3f:d=%.3f" % (max(0, D - fout), fout)]
        fl.append("asplit=2[aout][aviz]" if bars else "anull[aout]")
        parts.append("[am]" + ",".join(fl))

    cmd += ["-filter_complex", ";".join(parts), "-map", "[vout]"]
    if has_audio:
        cmd += ["-map", "[aout]"]
    cmd += ["-t", "%.3f" % D, "-r", str(FPS)]
    if enc == "nvenc":
        cmd += ["-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", "21",
                "-b:v", "0", "-profile:v", "high", "-g", str(FPS * 2)]
    else:
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-profile:v", "high",
                "-g", str(FPS * 2)]
    if has_audio:
        cmd += ["-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
    cmd += ["-movflags", "+faststart", out_path]
    return cmd
