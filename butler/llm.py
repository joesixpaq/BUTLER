"""Provider-agnostic LLM adapter for BUTLER.

Speaks OpenAI-compatible chat completions. Providers:

  deepseek (default) — DeepSeek API (`deepseek-flash`, native vision);
                       key resolved via credentials.py (Hermes store).
  monster            — local llama.cpp VLM on the tailnet (Swift-1.5 Qwen3.8-Flash-Next,
                       mmproj vision, reasoning); no key, generous timeouts.

Config file: ~/.config/butler/llm.json (optional), e.g.
    {"provider": "monster", "base_url": "http://monster:8001/v1"}
Switch with: `butler llm monster` / `butler llm deepseek` (read on every call —
no service restart needed). Absent file = deepseek defaults (unchanged behavior).

Env overrides (tests / units): BUTLER_LLM_PROVIDER, BUTLER_LLM_BASE_URL,
BUTLER_LLM_MODEL, BUTLER_LLM_TIMEOUT_S.

One job: send text + images (data URLs), return the model's raw text — the
analyzer parses the JSON out of it. Raises LLMError on failure; key material
is never logged.
"""
import base64
import io
import json
import os
import pathlib
import urllib.error
import urllib.request

from .credentials import get_deepseek_key

CONFIG_PATH = pathlib.Path.home() / ".config" / "butler" / "llm.json"

PROVIDERS = {
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-flash",
        "timeout_s": 240,
        "max_tokens": 8192,
    },
    "monster": {
        "base_url": "http://monster:8001/v1",  # tailnet hostname — no IPs in configs
        "model": None,                         # auto: first id from /v1/models
        "timeout_s": 1800,
        "max_tokens": 16384,
    },
}
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


def _read_config_file() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as e:
        raise LLMError(f"bad {CONFIG_PATH}: {e}") from None


def get_config() -> dict:
    """Effective config: provider defaults <- llm.json <- env overrides."""
    file_cfg = _read_config_file()
    provider = str(os.environ.get("BUTLER_LLM_PROVIDER")
                   or file_cfg.get("provider") or "deepseek").lower()
    if provider not in PROVIDERS:
        raise LLMError(f"unknown provider {provider!r} "
                       f"(expected one of {sorted(PROVIDERS)})")
    cfg = dict(PROVIDERS[provider])
    cfg["provider"] = provider
    # file overrides apply only when the file targets this provider
    # (so a temporary env-based flip is not poisoned by the other provider's URL)
    if str(file_cfg.get("provider") or provider).lower() == provider:
        for k in ("base_url", "model", "timeout_s", "max_tokens"):
            if file_cfg.get(k):
                cfg[k] = file_cfg[k]
        if file_cfg.get("extra_body"):
            cfg["extra_body"] = file_cfg["extra_body"]
    if os.environ.get("BUTLER_LLM_BASE_URL"):
        cfg["base_url"] = os.environ["BUTLER_LLM_BASE_URL"]
    if os.environ.get("BUTLER_LLM_MODEL"):
        cfg["model"] = os.environ["BUTLER_LLM_MODEL"]
    if os.environ.get("BUTLER_LLM_TIMEOUT_S"):
        cfg["timeout_s"] = float(os.environ["BUTLER_LLM_TIMEOUT_S"])
    return cfg


def model_label() -> str:
    """Short provider label for error records — never touches the network."""
    try:
        cfg = get_config()
    except LLMError:
        return "config-error"
    return f"{cfg['provider']}:{cfg.get('model') or 'auto'}"


def _resolve_model(cfg: dict) -> str:
    """Configured model, or the server's first served id (llama.cpp, monster)."""
    if cfg.get("model"):
        return str(cfg["model"])
    base = str(cfg["base_url"]).rstrip("/")
    try:
        with urllib.request.urlopen(base + "/models", timeout=15) as r:
            data = json.loads(r.read())
        model = data["data"][0]["id"]
        return str(model)
    except Exception as e:
        raise LLMError(f"cannot resolve model from {base}/models: "
                       f"{type(e).__name__}: {e}") from None


def analyze_images(prompt: str, image_paths, system: str | None = None) -> dict:
    """Send text + images (data URLs) to the configured LLM.

    Returns {"text": str, "model": str, "usage": dict, "provider": str}.
    Raises LLMError on failure.
    """
    cfg = get_config()
    base = str(cfg["base_url"]).rstrip("/")
    model = _resolve_model(cfg)

    headers = {"Content-Type": "application/json"}
    if cfg["provider"] == "deepseek":
        key = get_deepseek_key()
        if not key:
            raise LLMError("no DeepSeek key (see ~/.config/butler/README.txt)")
        headers["Authorization"] = f"Bearer {key}"

    content = [{"type": "text", "text": prompt}]
    for p in image_paths:
        b64 = base64.b64encode(_prep_jpeg(pathlib.Path(p))).decode()
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    msgs = ([{"role": "system", "content": system}] if system else []) + \
           [{"role": "user", "content": content}]
    body = {"model": model, "messages": msgs, "max_tokens": cfg["max_tokens"]}
    if cfg.get("extra_body"):
        body.update(cfg["extra_body"])
    req = urllib.request.Request(base + "/chat/completions",
                                 data=json.dumps(body).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=cfg["timeout_s"]) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise LLMError(f"HTTP {e.code}: {e.read().decode(errors='replace')[:300]}") from None
    except Exception as e:
        raise LLMError(f"{type(e).__name__}: {e}") from None
    try:
        msg = data["choices"][0]["message"]
        text = msg.get("content") or msg.get("reasoning_content") or ""
        return {"text": text, "model": data.get("model", model),
                "usage": data.get("usage", {}), "provider": cfg["provider"]}
    except Exception:
        raise LLMError(f"unexpected response: {json.dumps(data)[:250]}") from None


def set_provider(name: str) -> pathlib.Path:
    """Write {"provider": name} into llm.json (keeps any other keys)."""
    name = str(name).lower()
    if name not in PROVIDERS:
        raise LLMError(f"unknown provider {name!r}")
    data = {}
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text())
        except (OSError, json.JSONDecodeError):
            data = {}
    data["provider"] = name
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(data, indent=2) + "\n")
    return CONFIG_PATH


def probe(name: str) -> tuple[bool, str]:
    """Lightweight reachability check for a provider — no generation."""
    name = str(name).lower()
    if name not in PROVIDERS:
        return False, "unknown provider"
    try:
        cur = get_config()
    except LLMError:
        cur = None
    base = (cur["base_url"] if cur and cur["provider"] == name
            else PROVIDERS[name]["base_url"])
    base = str(base).rstrip("/")
    headers = {}
    if name == "deepseek":
        key = get_deepseek_key()
        if not key:
            return False, "no DeepSeek key in the Hermes store"
        headers["Authorization"] = f"Bearer {key}"
    try:
        req = urllib.request.Request(base + "/models", headers=headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        ids = [str(m.get("id")) for m in (data.get("data") or [])]
        return True, f"{base} — {len(ids)} model(s): {', '.join(ids[:2])}"
    except urllib.error.HTTPError as e:
        return False, f"{base} — HTTP {e.code}"
    except Exception as e:
        return False, f"{base} — {type(e).__name__}: {e}"
