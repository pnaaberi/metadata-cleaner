#!/usr/bin/env python3
"""Private image/video metadata cleaner with one-use downloads."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from PIL import Image, ImageOps, UnidentifiedImageError
from starlette.background import BackgroundTask

APP_ROOT = Path(__file__).resolve().parent
STATE_ROOT = Path(
    os.environ.get(
        "METADATA_CLEANER_STATE_DIR",
        str(Path.home() / ".local" / "share" / "metadata-cleaner"),
    )
)
TMP_ROOT = STATE_ROOT / "tmp"
HOST = os.environ.get("METADATA_CLEANER_HOST", "127.0.0.1")
PORT = int(os.environ.get("METADATA_CLEANER_PORT", "3495"))
TAILNET_HOST = os.environ.get("METADATA_CLEANER_TAILSCALE_HOST", "").strip().lower()
ALLOWED_USER = os.environ.get("METADATA_CLEANER_ALLOWED_TAILSCALE_USER", "").strip().lower()
MAX_BYTES = int(os.environ.get("METADATA_CLEANER_MAX_BYTES", str(1024 * 1024 * 1024)))
JOB_TTL_SECONDS = int(os.environ.get("METADATA_CLEANER_JOB_TTL_SECONDS", "1800"))
MIN_FREE_BYTES = int(os.environ.get("METADATA_CLEANER_MIN_FREE_BYTES", str(1024 * 1024 * 1024)))
CSRF_TOKEN = secrets.token_urlsafe(32)
LOCAL_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}", f"[::1]:{PORT}"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
IMAGE_FORMATS = {"JPEG": (".jpg", "image/jpeg"), "PNG": (".png", "image/png"), "WEBP": (".webp", "image/webp")}
Image.MAX_IMAGE_PIXELS = 80_000_000
REPORT_MAX_ITEMS = 120
REPORT_VALUE_LIMIT = 240
IMAGE_PSEUDO_GROUPS = {"SourceFile", "ExifTool", "System", "Composite"}
IMAGE_DERIVED_FIELDS = {
    "FileName", "Directory", "FileSize", "FileModifyDate", "FileAccessDate",
    "FileInodeChangeDate", "FilePermissions", "FileType", "FileTypeExtension",
    "MIMEType", "ExifByteOrder", "ImageWidth", "ImageHeight", "EncodingProcess",
    "BitsPerSample", "ColorComponents", "YCbCrSubSampling", "ImageSize",
    "Megapixels", "Compression",
}
VIDEO_CONTAINER_FIELDS = {"major_brand", "minor_version", "compatible_brands"}


@dataclass
class Job:
    path: Path
    media_type: str
    suffix: str
    created: float


jobs: dict[str, Job] = {}
jobs_lock = asyncio.Lock()
processing_lock = asyncio.Lock()
sweep_task: asyncio.Task[Any] | None = None
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


class CleanError(Exception):
    pass


def safe_remove(path: Path) -> None:
    try:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    except OSError:
        pass


def cleanup_expired_sync(now: float | None = None) -> None:
    now = now or time.time()
    TMP_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(TMP_ROOT, 0o700)
    for child in TMP_ROOT.iterdir():
        try:
            if not child.is_dir() or now - child.stat().st_mtime > JOB_TTL_SECONDS:
                safe_remove(child)
        except OSError:
            safe_remove(child)


def authorize_request(request: Request) -> bool:
    host = request.headers.get("host", "").lower()
    identity = request.headers.get("tailscale-user-login", "").strip().lower()
    forwarded = any(
        request.headers.get(name)
        for name in ("x-forwarded-for", "x-forwarded-host", "x-forwarded-proto")
    )
    if host in LOCAL_HOSTS:
        if identity or forwarded:
            return False
        expected_origin = f"http://{host}"
    elif host == TAILNET_HOST:
        if not forwarded or not identity or not ALLOWED_USER:
            return False
        if not secrets.compare_digest(identity, ALLOWED_USER):
            return False
        expected_origin = f"https://{host}"
    else:
        return False
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        origin = request.headers.get("origin", "")
        csrf = request.headers.get("x-csrf-token", "")
        if origin != expected_origin or not secrets.compare_digest(csrf, CSRF_TOKEN):
            return False
    return True


def report_value(value: Any) -> str:
    """Return bounded, display-safe metadata without logging or storing it."""
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    text = str(value)
    text = "".join(char if char in "\n\t" or ord(char) >= 32 else "�" for char in text)
    if len(text) > REPORT_VALUE_LIMIT:
        return text[:REPORT_VALUE_LIMIT] + "…"
    return text


def bounded_report_items(items: list[dict[str, str]]) -> dict[str, Any]:
    ordered = sorted(items, key=lambda item: item["field"].lower())
    return {
        "fields": ordered[:REPORT_MAX_ITEMS],
        "field_count": len(ordered),
        "omitted_count": max(0, len(ordered) - REPORT_MAX_ITEMS),
        "complete": len(ordered) <= REPORT_MAX_ITEMS,
    }


def is_image_derived(group: str, field: str) -> bool:
    if group in IMAGE_PSEUDO_GROUPS or group == "JFIF":
        return True
    return group == "File" and field in IMAGE_DERIVED_FIELDS


@app.middleware("http")
async def security_boundary(request: Request, call_next):
    if not authorize_request(request):
        return JSONResponse({"detail": "Pyyntö estettiin."}, status_code=403)
    if request.url.path == "/api/clean":
        if processing_lock.locked():
            return JSONResponse(
                {"detail": "Toinen tiedosto on käsittelyssä. Yritä hetken kuluttua uudelleen."},
                status_code=429,
            )
        async with processing_lock:
            response = await call_next(request)
    else:
        response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; "
        "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    )
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response


@app.on_event("startup")
async def startup_cleanup() -> None:
    global sweep_task
    STATE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(STATE_ROOT, 0o700)
    cleanup_expired_sync()
    sweep_task = asyncio.create_task(expiry_sweeper())


@app.on_event("shutdown")
async def stop_sweeper() -> None:
    global sweep_task
    if sweep_task:
        sweep_task.cancel()
        try:
            await sweep_task
        except asyncio.CancelledError:
            pass
        sweep_task = None


async def expiry_sweeper() -> None:
    while True:
        await asyncio.sleep(60)
        now = time.time()
        cleanup_expired_sync(now)
        async with jobs_lock:
            expired = [key for key, job in jobs.items() if now - job.created > JOB_TTL_SECONDS]
            for key in expired:
                jobs.pop(key, None)


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    html = (APP_ROOT / "static" / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html.replace("__CSRF_TOKEN__", CSRF_TOKEN))


@app.get("/app.css")
def app_css() -> FileResponse:
    return FileResponse(APP_ROOT / "static" / "app.css", media_type="text/css")


@app.get("/app.js")
def app_js() -> FileResponse:
    return FileResponse(APP_ROOT / "static" / "app.js", media_type="text/javascript")


@app.api_route("/favicon.svg", methods=["GET", "HEAD"])
def favicon() -> FileResponse:
    return FileResponse(APP_ROOT / "static" / "favicon.svg", media_type="image/svg+xml")


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "metadata-cleaner",
        "uploads_persisted": False,
        "supported": ["jpeg", "png", "webp", "mp4", "mov", "m4v", "webm", "mkv"],
    }


async def save_upload(request: Request, destination: Path) -> int:
    size = 0
    available = shutil.disk_usage(destination.parent).free
    safe_upload_budget = min(MAX_BYTES, max(0, (available - MIN_FREE_BYTES) // 2))
    if safe_upload_budget <= 0:
        raise CleanError("Väliaikaiselle käsittelylle ei ole riittävästi levytilaa.")
    with destination.open("xb") as handle:
        os.chmod(destination, 0o600)
        async for chunk in request.stream():
            size += len(chunk)
            if size > safe_upload_budget:
                if safe_upload_budget < MAX_BYTES:
                    raise CleanError("Väliaikaiselle käsittelylle ei ole riittävästi levytilaa.")
                raise CleanError("Tiedosto ylittää 1 Gt kokorajan.")
            handle.write(chunk)
    if size == 0:
        raise CleanError("Tyhjä tiedosto ei ole käsiteltävissä.")
    return size


def exif_field_names(path: Path, *, strict: bool = False) -> list[str]:
    try:
        result = subprocess.run(
            ["/usr/bin/exiftool", "-j", "-G1", "-a", "-s", str(path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        payload = json.loads(result.stdout)[0]
    except (subprocess.SubprocessError, json.JSONDecodeError, IndexError, OSError):
        if strict:
            raise CleanError("Kuvan metadatan jälkitarkistus ei käynnistynyt.") from None
        return []
    names = []
    for key in payload:
        group, _, field = key.partition(":")
        if not is_image_derived(group, field):
            names.append(key)
    return sorted(set(names))


def image_report_items(path: Path) -> list[dict[str, str]]:
    try:
        result = subprocess.run(
            ["/usr/bin/exiftool", "-j", "-G1", "-a", "-s", str(path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        payload = json.loads(result.stdout)[0]
    except (subprocess.SubprocessError, json.JSONDecodeError, IndexError, OSError):
        raise CleanError("Kuvan metadatan raportti ei käynnistynyt.") from None
    items = []
    for key, value in payload.items():
        group, _, field = key.partition(":")
        if is_image_derived(group, field):
            continue
        items.append({"field": f"{group}:{field}" if field else group, "value": report_value(value)})
    return items


def clean_image(source: Path, job_dir: Path) -> tuple[Path, str, dict[str, Any]]:
    before_report = bounded_report_items(image_report_items(source))
    try:
        with Image.open(source) as opened:
            fmt = opened.format
            if fmt not in IMAGE_FORMATS:
                raise CleanError("Kuvamuotoa ei tueta. Käytä JPEG-, PNG- tai WebP-kuvaa.")
            if getattr(opened, "n_frames", 1) != 1:
                raise CleanError("Animoituja kuvia ei vielä tueta.")
            if opened.width * opened.height > Image.MAX_IMAGE_PIXELS:
                raise CleanError("Kuvan pikselimäärä ylittää turvallisen käsittelyrajan.")
            opened.load()
            cleaned = ImageOps.exif_transpose(opened).copy()
            # Pillow copies source-side info (including JPEG comments) with pixels.
            # Keep the already-applied orientation, then drop all inherited metadata.
            cleaned.info.clear()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        if isinstance(exc, CleanError):
            raise
        raise CleanError("Tiedosto ei ole ehjä tuettu kuva.") from None

    suffix, media_type = IMAGE_FORMATS[fmt]
    output = job_dir / f"clean{suffix}"
    try:
        if fmt == "JPEG":
            if cleaned.mode not in ("RGB", "L"):
                cleaned = cleaned.convert("RGB")
            cleaned.save(output, format="JPEG", quality=95, optimize=True, progressive=True)
        elif fmt == "PNG":
            cleaned.save(output, format="PNG", optimize=True)
        else:
            if cleaned.mode not in ("RGB", "RGBA"):
                cleaned = cleaned.convert("RGBA" if "A" in cleaned.getbands() else "RGB")
            cleaned.save(output, format="WEBP", lossless=True, method=6)
    except OSError:
        raise CleanError("Kuvan uudelleenpakkaus epäonnistui.") from None
    finally:
        cleaned.close()
    os.chmod(output, 0o600)

    try:
        with Image.open(output) as verified:
            verified.load()
            forbidden_info = {"exif", "xmp", "XML:com.adobe.xmp", "icc_profile", "comment"}
            leftovers = forbidden_info.intersection(verified.info)
            if len(verified.getexif()) or leftovers:
                raise CleanError("Kuvan metadatan jälkitarkistus epäonnistui.")
    except (UnidentifiedImageError, OSError):
        raise CleanError("Puhdistetun kuvan tarkistus epäonnistui.") from None
    post_fields = exif_field_names(output, strict=True)
    if post_fields:
        raise CleanError("Kuvaan jäi tunnistamatonta upotettua metadataa.")
    return output, media_type, {
        "kind": "image",
        "removed_fields": before_report["field_count"],
        "dropped_tracks": 0,
        "report": {
            "before": {"fields": before_report["fields"], "field_count": before_report["field_count"], "omitted_count": before_report["omitted_count"], "complete": before_report["complete"]},
            "removed": before_report,
            "kept": {"fields": [], "field_count": 0, "omitted_count": 0, "complete": True},
            "verification": {"status": "passed", "remaining_fields": [], "remaining_count": 0},
            "method": "pikselit rakennettiin uudelleen; lähteen upotettua metadataa ei kopioitu",
        },
        "note": "Kuva rakennettiin uudelleen pikseleistä. Alla näkyvät ennen käsittelyä löydetyt poistettavat kentät ja niiden arvot.",
    }


def probe_video(path: Path) -> dict[str, Any]:
    try:
        result = subprocess.run(
            [
                "/usr/bin/ffprobe", "-v", "error", "-protocol_whitelist", "file",
                "-print_format", "json",
                "-show_format", "-show_streams", "-show_chapters", str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return json.loads(result.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        raise CleanError("Tiedosto ei ole ehjä tuettu video.") from None


def choose_video_output(probe: dict[str, Any], original_suffix: str) -> tuple[str, str]:
    format_name = str(probe.get("format", {}).get("format_name", ""))
    suffix = original_suffix.lower()
    if suffix in {".mp4", ".m4v", ".mov"} and any(x in format_name for x in ("mov", "mp4", "m4a", "3gp")):
        return (".mov", "video/quicktime") if suffix == ".mov" else (".mp4", "video/mp4")
    if suffix in {".webm", ".mkv"} and any(x in format_name for x in ("matroska", "webm")):
        return (".webm", "video/webm") if suffix == ".webm" else (".mkv", "video/x-matroska")
    raise CleanError("Videomuotoa ei tueta. Käytä MP4-, MOV-, M4V-, WebM- tai MKV-videota.")


def video_private_metadata(probe: dict[str, Any]) -> list[str]:
    fields: list[str] = []
    for key in probe.get("format", {}).get("tags", {}) or {}:
        if key.lower() not in {"major_brand", "minor_version", "compatible_brands"}:
            fields.append(f"format:{key}")
    for index, stream in enumerate(probe.get("streams", [])):
        for key, value in (stream.get("tags", {}) or {}).items():
            if key.lower() == "vendor_id" or (key.lower() == "language" and value in {"und", ""}):
                continue
            if key.lower() == "handler_name" and value in {"Video", "Audio", "VideoHandler", "SoundHandler"}:
                continue
            fields.append(f"stream{index}:{key}")
    if probe.get("chapters"):
        fields.append("chapters")
    return sorted(set(fields))


def video_metadata_report(probe: dict[str, Any]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    removed: list[dict[str, str]] = []
    kept: list[dict[str, str]] = []
    for key, value in (probe.get("format", {}).get("tags", {}) or {}).items():
        item = {"field": f"säiliö:{key}", "value": report_value(value)}
        (kept if key.lower() in VIDEO_CONTAINER_FIELDS else removed).append(item)
    for index, stream in enumerate(probe.get("streams", [])):
        for key, value in (stream.get("tags", {}) or {}).items():
            lowered = key.lower()
            generated_default = (
                lowered == "vendor_id"
                or (lowered == "language" and value in {"und", ""})
                or (lowered == "handler_name" and value in {"Video", "Audio", "VideoHandler", "SoundHandler"})
            )
            item = {"field": f"raita {index}:{key}", "value": report_value(value)}
            (kept if generated_default else removed).append(item)
    for index, chapter in enumerate(probe.get("chapters", []) or []):
        removed.append({"field": f"luku {index + 1}", "value": report_value(chapter)})
    return removed, kept


def video_track_report(probe: dict[str, Any]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    removed: list[dict[str, str]] = []
    kept: list[dict[str, str]] = []
    for index, stream in enumerate(probe.get("streams", [])):
        codec_type = str(stream.get("codec_type", "tuntematon"))
        codec = str(stream.get("codec_name", "tuntematon"))
        attached = bool((stream.get("disposition") or {}).get("attached_pic"))
        item = {
            "field": f"raita {index}",
            "value": f"tyyppi={codec_type}; koodekki={codec}" + ("; liitekuva=kyllä" if attached else ""),
        }
        (kept if codec_type in {"video", "audio"} and not attached else removed).append(item)
    return removed, kept


def clean_video(source: Path, job_dir: Path, original_suffix: str) -> tuple[Path, str, dict[str, Any]]:
    before = probe_video(source)
    if not any(stream.get("codec_type") == "video" for stream in before.get("streams", [])):
        raise CleanError("Tiedostossa ei ole videoraitaa.")
    suffix, media_type = choose_video_output(before, original_suffix)
    removed_metadata, source_technical_metadata = video_metadata_report(before)
    removed_tracks, _source_kept_tracks = video_track_report(before)
    output = job_dir / f"clean{suffix}"
    command = [
        "/usr/bin/ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-protocol_whitelist", "file",
        "-i", str(source),
        "-map", "0:V?", "-map", "0:a?",
        "-map_metadata", "-1", "-map_metadata:s", "-1", "-map_chapters", "-1",
        "-c", "copy", "-fflags", "+bitexact",
        "-metadata", "title=", "-metadata", "comment=", "-metadata", "description=",
        "-metadata", "creation_time=", "-metadata", "location=", "-metadata", "encoder=",
        "-metadata:s", "title=", "-metadata:s", "comment=", "-metadata:s", "language=und",
        "-metadata:s:v", "handler_name=Video", "-metadata:s:a", "handler_name=Audio",
    ]
    if suffix in {".mp4", ".mov"}:
        command.extend(["-movflags", "+faststart"])
    command.append(str(output))
    try:
        subprocess.run(command, check=True, capture_output=True, timeout=600)
    except subprocess.TimeoutExpired:
        raise CleanError("Videon käsittely ylitti 10 minuutin aikarajan.") from None
    except subprocess.CalledProcessError:
        raise CleanError("Videota ei voitu puhdistaa ilman kuvan tai äänen uudelleenpakkausta.") from None
    os.chmod(output, 0o600)
    after = probe_video(output)
    leftovers = video_private_metadata(after)
    if leftovers or after.get("chapters"):
        raise CleanError("Videon metadatan jälkitarkistus epäonnistui.")
    kept_types = {"video", "audio"}
    dropped = sum(
        1 for stream in before.get("streams", [])
        if stream.get("codec_type") not in kept_types
        or bool((stream.get("disposition") or {}).get("attached_pic"))
    )
    removed_report = bounded_report_items(removed_metadata)
    _after_removed, output_technical_metadata = video_metadata_report(after)
    _after_removed_tracks, output_kept_tracks = video_track_report(after)
    kept_report = bounded_report_items(output_technical_metadata)
    removed_track_report = bounded_report_items(removed_tracks)
    kept_track_report = bounded_report_items(output_kept_tracks)
    return output, media_type, {
        "kind": "video",
        "removed_fields": removed_report["field_count"],
        "dropped_tracks": dropped,
        "report": {
            "before": bounded_report_items(removed_metadata + source_technical_metadata),
            "removed": removed_report,
            "kept": kept_report,
            "removed_tracks": removed_track_report,
            "kept_tracks": kept_track_report,
            "verification": {"status": "passed", "remaining_fields": [], "remaining_count": 0},
            "method": "ffmpeg stream copy (-c copy); vain varsinaiset video- ja ääniraidat mapattiin uuteen säiliöön",
        },
        "note": "Video paketoitiin uudelleen ilman kuvan tai äänen uudelleenpakkausta. Alla näkyvät tiedostosta löydetyt poistetut ja säilytetyt tiedot sekä raidat.",
    }


def clean_file(source: Path, job_dir: Path, original_suffix: str) -> tuple[Path, str, dict[str, Any]]:
    try:
        return clean_image(source, job_dir)
    except CleanError as image_error:
        if original_suffix.lower() not in VIDEO_SUFFIXES:
            raise image_error
    return clean_video(source, job_dir, original_suffix)


@app.post("/api/clean")
async def clean_endpoint(request: Request) -> dict[str, Any]:
    cleanup_expired_sync()
    content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type != "application/octet-stream":
        raise HTTPException(status_code=415, detail="Virheellinen siirtomuoto.")
    try:
        declared_size = int(request.headers.get("content-length", "0"))
    except ValueError:
        declared_size = 0
    if declared_size > MAX_BYTES:
        raise HTTPException(status_code=413, detail="Tiedosto ylittää 1 Gt kokorajan.")
    job_id = secrets.token_urlsafe(24)
    job_dir = TMP_ROOT / job_id
    job_dir.mkdir(mode=0o700)
    source = job_dir / "input.bin"
    original_suffix = request.headers.get("x-file-suffix", "").strip().lower()
    if original_suffix not in {".jpg", ".jpeg", ".png", ".webp", *VIDEO_SUFFIXES}:
        safe_remove(job_dir)
        raise HTTPException(status_code=422, detail="Tiedostomuotoa ei tueta.")
    try:
        input_size = await save_upload(request, source)
        output, media_type, report = await asyncio.to_thread(clean_file, source, job_dir, original_suffix)
        source.unlink(missing_ok=True)
        output_size = output.stat().st_size
        async with jobs_lock:
            jobs[job_id] = Job(output, media_type, output.suffix, time.time())
        return {
            "ok": True,
            "download_url": f"/api/download/{job_id}",
            "input_bytes": input_size,
            "output_bytes": output_size,
            "expires_in_seconds": JOB_TTL_SECONDS,
            **report,
        }
    except CleanError as exc:
        safe_remove(job_dir)
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except Exception:
        safe_remove(job_dir)
        raise HTTPException(status_code=500, detail="Käsittely epäonnistui turvallisesti.") from None



@app.get("/api/download/{job_id}")
async def download(job_id: str) -> Response:
    async with jobs_lock:
        job = jobs.pop(job_id, None)
    if not job or not job.path.is_file() or time.time() - job.created > JOB_TTL_SECONDS:
        if job:
            safe_remove(job.path.parent)
        raise HTTPException(status_code=404, detail="Lataus on vanhentunut tai jo käytetty.")
    filename = f"puhdistettu{job.suffix}"
    return FileResponse(
        job.path,
        media_type=job.media_type,
        filename=filename,
        background=BackgroundTask(safe_remove, job.path.parent),
    )


if __name__ == "__main__":
    if not ALLOWED_USER or not TAILNET_HOST:
        raise SystemExit(
            "METADATA_CLEANER_ALLOWED_TAILSCALE_USER and "
            "METADATA_CLEANER_TAILSCALE_HOST are required"
        )
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT, access_log=False, server_header=False)
