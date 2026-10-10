"""Provider-agnostic LLM adapter for BUTLER.

Now: DeepSeek API — `deepseek-flash` (native image input, live-verified).
Later: local model on `monster` behind the same `analyze_images()` call.

One job: send text + images, return the model's raw text (the analyzer parses
the JSON out of it). No keys here — credentials.py resolves them.
"""
import base64
import io
import json
import pathlib
import urllib.error
import urllib.request

from .credentials import get_deepseek_key

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-flash"
TIMEOUT = 240          # seconds, generous for two full pages
MAX_EDGE = 2600        # only shrink images larger than this many px
MAX_BYTES = 1_600_000  # ...or heavier than this many bytes


class LLMError(RuntimeError):
    pass


def _prep_jpeg(path: pathlib.Path) -> bytes:
    """Return JPEG bytes, near-original; downscale only giants. Never fails hard."""
    try:
        raw = path.read_bytes()
    except OSError as e:
        raise LLMError(f"cannot read image {path.name}: {e}") from None
    if not raw:
        raise LLMError(f"empty image {path.name}")
    try:
        from PIL import Image

        im = Image.open(io.BytesIO(raw))
        w, h = im.size
        if max(w, h) <= MAX_EDGE and len(raw) <= MAX_BYTES:
            return raw
        if max(w, h) > MAX_EDGE:
            s = MAX_EDGE / max(w, h)
            im = im.resize((round(w * s), round(h * s)), Image.LANCZOS)
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "JPEG", quality=88)
        out = buf.getvalue()
        return out if len(out) < len(raw) else raw
    except Exception:
        # PIL missing or odd file — try cv2, else send raw and let the API decide
        try:
            import cv2

            img = cv2.imread(str(path))
            if img is not None:
                h, w = img.shape[:2]
                if max(w, h) > MAX_EDGE:
                    s = MAX_EDGE / max(w, h)
                    img = cv2.resize(img, (round(w * s), round(h * s)),
                                     interpolation=cv2.INTER_AREA)
                ok, enc = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 88])
                if ok:
                    out = enc.tobytes()
                    return out if len(out) < len(raw) else raw
        except Exception:
            pass
        return raw


def analyze_images(prompt: str, image_paths, system: str | None = None) -> dict:
    """Send text + images (data URLs) to the LLM.

    Returns {"text": str, "model": str, "usage": dict}. Raises LLMError on failure.
    """
    key = get_deepseek_key()
    if not key:
        raise LLMError("no DeepSeek key (see ~/.config/butler/README.txt)")
    content = [{"type": "text", "text": prompt}]
    for p in image_paths:
        b64 = base64.b64encode(_prep_jpeg(pathlib.Path(p))).decode()
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    msgs = ([{"role": "system", "content": system}] if system else []) + \
           [{"role": "user", "content": content}]
    body = json.dumps({"model": MODEL, "messages": msgs, "max_tokens": 8192}).encode()
    req = urllib.request.Request(DEEPSEEK_URL, data=body, headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise LLMError(f"HTTP {e.code}: {e.read().decode(errors='replace')[:300]}") from None
    except Exception as e:
        raise LLMError(f"{type(e).__name__}: {e}") from None
    try:
        msg = data["choices"][0]["message"]
        return {"text": msg.get("content") or "", "model": data.get("model", MODEL),
                "usage": data.get("usage", {})}
    except Exception:
        raise LLMError(f"unexpected response: {json.dumps(data)[:250]}") from None
