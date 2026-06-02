"""get_attached_file — cache-first lookup, then live Telegram / Mattermost download."""
import json
from pathlib import Path
from typing import Optional

from mcp.types import TextContent

from mcp_server import server as _srv


def _find_cached_file(cache_dir: Path, file_id: str) -> Optional[Path]:
    """Return the first cached file whose stem matches file_id (any extension)."""
    try:
        for p in cache_dir.iterdir():
            if p.stem == file_id:
                return p
    except OSError:
        pass
    return None


def _serve_file_content(content: bytes, ext: str, text_extensions: set,
                        file_path: Optional[Path] = None) -> dict:
    import base64
    import mimetypes

    mime = mimetypes.guess_type(f"file{ext}")[0] or "application/octet-stream"

    if ext in text_extensions or not ext:
        body = content.decode("utf-8", errors="ignore")
    else:
        body = f"[base64]:{base64.b64encode(content).decode('ascii')}"

    return {
        "file_path": str(file_path.resolve()) if file_path else None,
        "size": len(content),
        "content_type": mime,
        "content": body,
    }


def _try_telegram_download(token: str, file_id: str, cache_dir: Path, text_extensions: set) -> Optional[dict]:
    import requests
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{token}/getFile",
            params={"file_id": file_id},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            return None
        file_path_str = data["result"]["file_path"]
        ext = Path(file_path_str).suffix.lower()

        file_resp = requests.get(
            f"https://api.telegram.org/file/bot{token}/{file_path_str}",
            timeout=60,
        )
        file_resp.raise_for_status()
        content = file_resp.content

        cache_path = cache_dir / f"{file_id}{ext}"
        cache_path.write_bytes(content)
        return _serve_file_content(content, ext, text_extensions, file_path=cache_path)
    except Exception:
        return None


def _try_mattermost_download(base_url: str, token: str, file_id: str,
                             cache_dir: Path, text_extensions: set) -> Optional[dict]:
    import requests
    import mimetypes
    try:
        url = f"{base_url.rstrip('/')}/api/v4/files/{file_id}"
        resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=60)
        resp.raise_for_status()
        content = resp.content

        content_type = resp.headers.get("Content-Type", "")
        mime_type = content_type.split(";")[0].strip()
        ext = mimetypes.guess_extension(mime_type) or ""

        cache_path = cache_dir / (file_id + ext) if ext else cache_dir / file_id
        cache_path.write_bytes(content)
        return _serve_file_content(content, ext, text_extensions, file_path=cache_path)
    except Exception:
        return None


async def _get_attached_file(args: dict) -> list[TextContent]:
    """Get content of an attached file — cache first, then Telegram, then Mattermost."""
    file_id = args.get("file_id", "").strip()
    if not file_id:
        return [TextContent(type="text", text="file_id is required.")]

    config = _srv.get_config()
    cache_dir = Path(config.get("attachments_path", "data/attachments"))
    cache_dir.mkdir(parents=True, exist_ok=True)

    text_extensions = set(config.get("text_extensions", [".txt", ".md", ".log", ".json", ".lst"]))

    # 1. Cache hit (any extension — covers both Mattermost and Telegram text files)
    cached = _find_cached_file(cache_dir, file_id)
    if cached:
        try:
            result = _serve_file_content(
                cached.read_bytes(), cached.suffix.lower(), text_extensions, file_path=cached,
            )
            return [TextContent(type="text", text=json.dumps(result))]
        except Exception as e:
            return [TextContent(type="text", text=f"Error reading cached file: {e}")]

    # 2. Try each enabled Telegram source
    for src in config.get("sources", {}).values():
        if src.get("type") == "telegram" and src.get("enabled", True) and src.get("token"):
            result = _try_telegram_download(src["token"], file_id, cache_dir, text_extensions)
            if result is not None:
                return [TextContent(type="text", text=json.dumps(result))]

    # 3. Try each enabled Mattermost source
    for src in config.get("sources", {}).values():
        if (src.get("type") == "mattermost" and src.get("enabled", True)
                and src.get("url") and src.get("token")):
            result = _try_mattermost_download(src["url"], src["token"],
                                              file_id, cache_dir, text_extensions)
            if result is not None:
                return [TextContent(type="text", text=json.dumps(result))]

    return [TextContent(type="text", text=f"File '{file_id}' not found in cache or any configured source.")]
