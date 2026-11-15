import asyncio
import html
import os
import re
import shutil
import socket
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx
import yt_dlp
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI(title="MediaDown - YouTube & Filmes")

BASE_DIR = Path(__file__).resolve().parent
DOWNLOADS_DIR = BASE_DIR / "downloads"
STATIC_DIR = BASE_DIR / "static"
BIN_DIR = BASE_DIR / ".bin"

DOWNLOADS_DIR.mkdir(exist_ok=True)
STATIC_DIR.mkdir(exist_ok=True)
BIN_DIR.mkdir(exist_ok=True)


def setup_ffmpeg() -> str | None:
    """Garante que o ffmpeg esteja disponível para o yt-dlp (mesmo sem instalação global no Windows)."""
    sys_ffmpeg = shutil.which("ffmpeg")
    if sys_ffmpeg:
        return sys_ffmpeg
    try:
        import imageio_ffmpeg
        exe_path = Path(imageio_ffmpeg.get_ffmpeg_exe())
        if exe_path.exists():
            target_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
            target_path = BIN_DIR / target_name
            if not target_path.exists():
                shutil.copy2(exe_path, target_path)
            return str(target_path)
    except Exception as e:
        print(f"Aviso ao configurar ffmpeg: {e}")
    return None


FFMPEG_PATH = setup_ffmpeg()

# Armazena o estado dos downloads em andamento
JOBS: dict[str, dict] = {}


def sanitize_filename(name: str, default: str = "video") -> str:
    name = re.sub(r'[\\/*?:"<>|]+', "", name).strip()
    name = re.sub(r"\s+", " ", name)
    return name[:120] if name else default


def extract_source_url(raw_input: str) -> list[dict]:
    """
    Extrai URLs de tags <source src="..." type="...">, <video src="..."> ou URLs diretas.
    """
    text = raw_input.strip()
    results = []

    # 1. Procura por tags <source ...> ou <video ...> com atributo src
    tag_pattern = re.compile(
        r'<(?:source|video)\b[^>]*?\bsrc\s*=\s*(?:["\']([^"\']+)["\']|([^\s>]+))[^>]*>',
        re.IGNORECASE,
    )
    for match in tag_pattern.finditer(text):
        full_tag = match.group(0)
        url = match.group(1) or match.group(2)
        url = html.unescape(url.strip())

        type_match = re.search(r'\btype\s*=\s*["\']([^"\']+)["\']', full_tag, re.IGNORECASE)
        label_match = re.search(
            r'\b(?:label|res|size|title)\s*=\s*["\']([^"\']+)["\']', full_tag, re.IGNORECASE
        )

        mime_type = type_match.group(1) if type_match else "video/mp4"
        label = label_match.group(1) if label_match else "MP4 Direto"
        results.append({"url": url, "type": mime_type, "label": label})

    # 2. Se não achou tag completa mas tem src="..." solto
    if not results:
        src_only = re.findall(r'\bsrc\s*=\s*["\']([^"\']+)["\']', text, re.IGNORECASE)
        for u in src_only:
            results.append({"url": html.unescape(u.strip()), "type": "video/mp4", "label": "MP4 Direto"})

    # 3. Se o usuário colou diretamente a URL http/https
    if not results:
        url_matches = re.findall(r'https?://[^\s"\'<>]+', text)
        for u in url_matches:
            results.append({"url": html.unescape(u.strip()), "type": "video/mp4", "label": "Link Direto"})

    return results


def guess_headers_for_url(url: str) -> dict[str, str]:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    if "tokyvideo.com" in host:
        headers["Referer"] = "https://www.tokyvideo.com/"
        headers["Origin"] = "https://www.tokyvideo.com"
    elif parsed.scheme and parsed.netloc:
        headers["Referer"] = f"{parsed.scheme}://{parsed.netloc}/"
    return headers


class YouTubeInfoRequest(BaseModel):
    url: str


class YouTubeDownloadRequest(BaseModel):
    url: str
    mode: str = "video"  # "video" | "audio"
    quality: str = "best"  # "best", "1080", "720", "480", "360"


class MovieExtractRequest(BaseModel):
    code: str
    filename: str | None = None


class MovieJobRequest(BaseModel):
    url: str
    filename: str = "filme"


@app.get("/api/system-info")
async def get_system_info(request: Request):
    """Retorna IPs locais para facilitar o acesso pelo celular na mesma rede Wi-Fi."""
    local_ips = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in local_ips:
                local_ips.append(ip)
    except Exception:
        pass

    port = request.url.port or 8000
    return {
        "ffmpeg_ready": bool(FFMPEG_PATH),
        "local_urls": [f"http://{ip}:{port}" for ip in local_ips],
    }


@app.post("/api/youtube/info")
async def youtube_info(req: YouTubeInfoRequest):
    url = req.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="Informe a URL do YouTube.")

    def _extract():
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        info = await asyncio.to_thread(_extract)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Não foi possível ler o vídeo: {str(e)}")

    formats = info.get("formats", [])
    heights = sorted(
        {
            f.get("height")
            for f in formats
            if f.get("vcodec") != "none" and f.get("height") and f.get("height") >= 240
        },
        reverse=True,
    )

    return {
        "id": info.get("id"),
        "title": info.get("title") or "Vídeo do YouTube",
        "thumbnail": info.get("thumbnail"),
        "duration": info.get("duration"),
        "uploader": info.get("uploader") or info.get("channel"),
        "available_heights": heights or [1080, 720, 480, 360],
        "ffmpeg_available": bool(FFMPEG_PATH),
    }


def _run_youtube_download(job_id: str, url: str, mode: str, quality: str):
    job = JOBS[job_id]
    job["status"] = "downloading"

    out_template = str(DOWNLOADS_DIR / f"{job_id}_%(title).100B.%(ext)s")

    def progress_hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)
            if total > 0:
                job["progress"] = round((downloaded / total) * 100, 1)
            job["speed"] = d.get("_speed_str", "").strip()
            job["eta"] = d.get("_eta_str", "").strip()
            job["stage"] = "Baixando mídia..."
        elif d["status"] == "finished":
            job["progress"] = 100.0
            job["stage"] = "Processando / Convertendo arquivo final..."

    ydl_opts: dict = {
        "outtmpl": out_template,
        "noplaylist": True,
        "progress_hooks": [progress_hook],
        "quiet": True,
        "no_warnings": True,
    }

    if FFMPEG_PATH:
        ydl_opts["ffmpeg_location"] = FFMPEG_PATH

    if mode == "audio":
        if FFMPEG_PATH:
            ydl_opts["format"] = "bestaudio/best"
            ydl_opts["postprocessors"] = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ]
        else:
            ydl_opts["format"] = "bestaudio[ext=m4a]/bestaudio/best"
    else:
        # Vídeo de alta qualidade em MP4
        if FFMPEG_PATH:
            if quality and quality != "best" and quality.isdigit():
                h = int(quality)
                ydl_opts["format"] = (
                    f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]/"
                    f"bestvideo[height<={h}]+bestaudio/best[height<={h}]/best"
                )
            else:
                ydl_opts["format"] = (
                    "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"
                )
            ydl_opts["merge_output_format"] = "mp4"
        else:
            ydl_opts["format"] = "best[ext=mp4]/best"

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = sanitize_filename(info.get("title") or "youtube_media")

        # Localiza o arquivo gerado para este job_id
        matches = list(DOWNLOADS_DIR.glob(f"{job_id}_*"))
        if not matches:
            raise RuntimeError("Arquivo final não encontrado após o download.")

        final_file = max(matches, key=lambda p: p.stat().st_mtime)
        ext = final_file.suffix.lstrip(".") or ("mp3" if mode == "audio" else "mp4")
        clean_name = f"{title}.{ext}"

        job["file_path"] = str(final_file)
        job["filename"] = clean_name
        job["filesize"] = final_file.stat().st_size
        job["status"] = "completed"
        job["progress"] = 100.0
        job["stage"] = "Pronto para salvar!"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        job["stage"] = "Erro no download"


@app.post("/api/youtube/download")
async def start_youtube_download(req: YouTubeDownloadRequest):
    url = req.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="Informe a URL do YouTube.")

    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {
        "id": job_id,
        "type": "youtube",
        "status": "starting",
        "progress": 0.0,
        "speed": "",
        "eta": "",
        "stage": "Iniciando...",
        "filename": None,
        "file_path": None,
        "filesize": 0,
        "error": None,
        "created_at": time.time(),
    }

    thread = threading.Thread(
        target=_run_youtube_download,
        args=(job_id, url, req.mode, req.quality),
        daemon=True,
    )
    thread.start()
    return {"job_id": job_id}


@app.post("/api/movie/extract")
async def movie_extract(req: MovieExtractRequest):
    """
    Recebe o código <source src="..." type="video/mp4"> e extrai a URL real + verifica metadados.
    """
    sources = extract_source_url(req.code)
    if not sources:
        raise HTTPException(
            status_code=400,
            detail='Nenhuma URL encontrada! Cole o código <source src="https://..." type="video/mp4"> ou o link direto.',
        )

    primary = sources[0]
    url = primary["url"]
    parsed = urlparse(url)
    path_name = Path(unquote(parsed.path)).stem
    if not path_name or len(path_name) > 40:
        default_name = "filme_baixado"
    else:
        default_name = sanitize_filename(path_name, "filme_baixado")

    custom_name = sanitize_filename(req.filename, default_name) if req.filename else default_name

    # Faz uma checagem rápida do cabeçalho para descobrir o tamanho do filme, se disponível
    headers = guess_headers_for_url(url)
    content_length = None
    content_type = primary.get("type", "video/mp4")
    reachable = True
    status_note = "URL extraída com sucesso!"

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=8.0, verify=False) as client:
            resp = await client.head(url, headers=headers)
            if resp.status_code >= 400:
                # Alguns servidores bloqueiam HEAD mas aceitam GET com Range
                resp = await client.get(url, headers={**headers, "Range": "bytes=0-1023"})
            if "content-length" in resp.headers:
                # Se veio de um Range 206, pega o total do Content-Range
                cr = resp.headers.get("content-range")
                if cr and "/" in cr:
                    total_part = cr.split("/")[-1]
                    if total_part.isdigit():
                        content_length = int(total_part)
                else:
                    cl = resp.headers.get("content-length", "")
                    if cl.isdigit():
                        content_length = int(cl)
            if resp.headers.get("content-type"):
                content_type = resp.headers.get("content-type")
            if resp.status_code >= 400:
                status_note = f"Aviso: O servidor respondeu HTTP {resp.status_code} (o token pode ter expirado ou exige download direto)."
    except Exception:
        status_note = "URL extraída! Pronta para iniciar o download."

    return {
        "extracted_url": url,
        "all_sources": sources,
        "suggested_filename": f"{custom_name}.mp4",
        "content_length": content_length,
        "content_type": content_type,
        "reachable": reachable,
        "status_note": status_note,
    }


@app.get("/api/movie/stream")
async def stream_movie_download(
    url: str = Query(..., description="URL direta extraída da tag <source>"),
    filename: str = Query("filme.mp4", description="Nome do arquivo para salvar"),
):
    """
    Faz o proxy/stream do vídeo diretamente para o navegador (PC ou Celular)
    forçando o download (Content-Disposition: attachment) com cabeçalhos compatíveis.
    Ideal tanto para uso local quanto hospedado na nuvem (não ocupa disco do servidor!).
    """
    clean_name = sanitize_filename(Path(filename).stem, "filme") + ".mp4"
    headers = guess_headers_for_url(url)

    client = httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(60.0, read=300.0), verify=False)
    try:
        req = client.build_request("GET", url, headers=headers)
        resp = await client.send(req, stream=True)
    except Exception as e:
        await client.aclose()
        raise HTTPException(status_code=502, detail=f"Erro ao conectar na origem do vídeo: {e}")

    if resp.status_code >= 400:
        await resp.aclose()
        await client.aclose()
        raise HTTPException(
            status_code=resp.status_code,
            detail=f"O servidor do vídeo recusou a conexão (HTTP {resp.status_code}). Verifique se o link ?secure= não expirou.",
        )

    response_headers = {
        "Content-Disposition": f'attachment; filename="{clean_name}"',
    }
    if "content-length" in resp.headers:
        response_headers["Content-Length"] = resp.headers["content-length"]

    async def iter_stream():
        try:
            async for chunk in resp.aiter_bytes(chunk_size=1024 * 256):
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(
        iter_stream(),
        media_type="video/mp4",
        headers=response_headers,
    )


def _run_movie_job(job_id: str, url: str, filename: str):
    """Baixa o filme em segundo plano no servidor com barra de progresso em tempo real."""
    job = JOBS[job_id]
    job["status"] = "downloading"
    job["stage"] = "Conectando ao servidor do filme..."

    clean_stem = sanitize_filename(Path(filename).stem, "filme")
    clean_name = f"{clean_stem}.mp4"
    target_path = DOWNLOADS_DIR / f"{job_id}_{clean_name}"
    headers = guess_headers_for_url(url)

    try:
        with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(60.0, read=300.0), verify=False) as client:
            with client.stream("GET", url, headers=headers) as resp:
                if resp.status_code >= 400:
                    raise RuntimeError(
                        f"Servidor retornou HTTP {resp.status_code}. O token de segurança (?secure=...) pode ter expirado."
                    )
                total = int(resp.headers.get("content-length", 0))
                downloaded = 0
                start_time = time.time()

                with open(target_path, "wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=1024 * 256):
                        if not chunk:
                            continue
                        f.write(chunk)
                        downloaded += len(chunk)
                        elapsed = max(time.time() - start_time, 0.1)
                        speed_bps = downloaded / elapsed
                        speed_mb = speed_bps / (1024 * 1024)
                        job["speed"] = f"{speed_mb:.2f} MB/s"

                        if total > 0:
                            job["progress"] = round((downloaded / total) * 100, 1)
                            remaining_bytes = max(total - downloaded, 0)
                            eta_sec = int(remaining_bytes / max(speed_bps, 1))
                            mins, secs = divmod(eta_sec, 60)
                            job["eta"] = f"{mins:02d}:{secs:02d}"
                            job["stage"] = (
                                f"Baixando filme ({downloaded / (1024*1024):.1f} MB / {total / (1024*1024):.1f} MB)..."
                            )
                        else:
                            job["stage"] = f"Baixando filme ({downloaded / (1024*1024):.1f} MB)..."

        job["file_path"] = str(target_path)
        job["filename"] = clean_name
        job["filesize"] = target_path.stat().st_size
        job["progress"] = 100.0
        job["status"] = "completed"
        job["stage"] = "Filme pronto para salvar no dispositivo!"
    except Exception as e:
        if target_path.exists():
            try:
                target_path.unlink()
            except Exception:
                pass
        job["status"] = "error"
        job["error"] = str(e)
        job["stage"] = "Erro ao baixar o filme"


@app.post("/api/movie/download-job")
async def start_movie_job(req: MovieJobRequest):
    url = req.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL vazia.")

    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {
        "id": job_id,
        "type": "movie",
        "status": "starting",
        "progress": 0.0,
        "speed": "",
        "eta": "",
        "stage": "Preparando download do filme...",
        "filename": req.filename,
        "file_path": None,
        "filesize": 0,
        "error": None,
        "created_at": time.time(),
    }

    thread = threading.Thread(
        target=_run_movie_job,
        args=(job_id, url, req.filename),
        daemon=True,
    )
    thread.start()
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
async def get_job_status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Tarefa não encontrada.")
    return job


@app.get("/api/jobs/{job_id}/file")
async def download_job_file(job_id: str):
    job = JOBS.get(job_id)
    if not job or job.get("status") != "completed" or not job.get("file_path"):
        raise HTTPException(status_code=404, detail="Arquivo ainda não está pronto.")

    file_path = Path(job["file_path"])
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Arquivo não encontrado no servidor.")

    return FileResponse(
        path=file_path,
        filename=job.get("filename") or file_path.name,
        media_type="application/octet-stream",
    )


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index_file = STATIC_DIR / "index.html"
    return index_file.read_text(encoding="utf-8")

