#!/usr/bin/env python3
"""Yami Portable AI - single executable USB agent."""

import http.server
import base64
import html as html_lib
import io
import json
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zipfile
import xml.etree.ElementTree as ET

APP_NAME = "Yami Portable AI"
DEFAULT_PORT = 8765
OLLAMA_URL = "http://127.0.0.1:11434"


def app_root():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


ROOT = app_root()
DATA_DIR = os.path.join(ROOT, "data")
MODELS_DIR = os.path.join(ROOT, "models")
BIN_DIR = os.path.join(ROOT, "bin")
CHATS_FILE = os.path.join(DATA_DIR, "chats.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
BACKUPS_DIR = os.path.join(DATA_DIR, "backups")
OLLAMA_MODELS_DIR = os.path.join(DATA_DIR, "ollama_models")
TEXT_MAX_BYTES = 2 * 1024 * 1024
ATTACH_MAX_BYTES = 8 * 1024 * 1024
ATTACH_TEXT_CHARS = 180000
WEB_MAX_BYTES = 2 * 1024 * 1024
WEB_TEXT_CHARS = 60000
WEB_USER_AGENT = "YamiPortableAI/1.0 (+local USB assistant)"

YAMI_SYSTEM = (
    "Voce e Yami, uma agente virtual em portugues brasileiro que roda dentro deste pendrive. "
    "Voce pode conversar por texto e audio, usar modelo local via Ollama ou API online com chave, "
    "editar arquivos dentro da pasta da Yami, pesquisar na internet e ajudar o usuario a personalizar seu proprio codigo. "
    "Quando precisar ler ou alterar arquivos, ou buscar informacoes atuais na web, emita um bloco ```yami-action com JSON valido. "
    "Acoes: list_dir, read_file, write_file, append_file, replace_in_file, web_search, web_fetch. "
    "Use web_search com query para pesquisar; use web_fetch com url para abrir uma pagina. Cite as URLs usadas. "
    "Use caminhos relativos a pasta da Yami. Nunca acesse fora dessa pasta."
)

DEFAULT_SETTINGS = {
    "provider": {
        "mode": "local",
        "apiProvider": "openai",
        "apiBaseUrl": "https://api.openai.com/v1",
        "apiKey": "",
        "apiModel": "",
        "localModel": "",
        "temperature": 0.7,
    },
    "voice": {
        "listen": False,
        "speak": True,
        "wakeWordEnabled": False,
        "wakeWord": "ola yami",
        "lang": "pt-BR",
        "rate": 1.0,
        "pitch": 1.0,
    },
    "ui": {
        "accent": "#00d4ff",
        "accent2": "#39ff88",
        "fontSize": "14px",
        "customCss": "",
    },
    "agent": {
        "systemPrompt": YAMI_SYSTEM,
        "autoActions": True,
        "maxActionLoops": 4,
    },
}

API_PRESETS = {
    "openai": {
        "label": "OpenAI",
        "baseUrl": "https://api.openai.com/v1",
        "fallbackModel": "gpt-4o-mini",
        "fast": ["gpt-4.1-mini", "gpt-4o-mini", "gpt-5-mini"],
    },
    "gemini": {
        "label": "Gemini",
        "baseUrl": "https://generativelanguage.googleapis.com/v1beta/openai",
        "fallbackModel": "gemini-2.5-flash",
        "fast": ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"],
    },
    "compatible": {
        "label": "Compativel",
        "baseUrl": "",
        "fallbackModel": "",
        "fast": [],
    },
}

OLLAMA_PROCESS = None


def deep_merge(base, incoming):
    out = json.loads(json.dumps(base))
    for key, value in (incoming or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key].update(value)
        else:
            out[key] = value
    return out


def ensure_dirs():
    for folder in (DATA_DIR, MODELS_DIR, BIN_DIR, BACKUPS_DIR, OLLAMA_MODELS_DIR):
        os.makedirs(folder, exist_ok=True)
    if not os.path.exists(CHATS_FILE):
        write_json(CHATS_FILE, [])
    if not os.path.exists(SETTINGS_FILE):
        write_json(SETTINGS_FILE, DEFAULT_SETTINGS)
    else:
        write_json(SETTINGS_FILE, deep_merge(DEFAULT_SETTINGS, read_json(SETTINGS_FILE, {})))


def read_json(path, fallback):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return fallback


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def safe_path(raw_path):
    raw = (raw_path or ".").replace("\\", os.sep)
    root = os.path.abspath(ROOT)
    full = os.path.abspath(raw if os.path.isabs(raw) else os.path.join(root, raw.lstrip("/\\")))
    if os.path.commonpath([root, full]) != root:
        raise ValueError("Path outside Yami folder is not allowed.")
    return full


def rel_path(full_path):
    rel = os.path.relpath(full_path, ROOT)
    return "." if rel == "." else rel.replace(os.sep, "/")


def json_bytes(data):
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def find_port(start=DEFAULT_PORT, tries=40):
    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                pass
    raise OSError("No free local port found.")


def find_ollama_exe():
    candidates = [
        os.path.join(BIN_DIR, "ollama-windows.exe"),
        os.path.join(BIN_DIR, "ollama.exe"),
        shutil.which("ollama"),
    ]
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    return None


def ollama_online(timeout=1.5):
    try:
        urllib.request.urlopen(OLLAMA_URL + "/api/tags", timeout=timeout).read()
        return True
    except Exception:
        return False


def start_ollama():
    global OLLAMA_PROCESS
    if ollama_online():
        return True
    exe = find_ollama_exe()
    if not exe:
        return False
    env = os.environ.copy()
    env["OLLAMA_MODELS"] = OLLAMA_MODELS_DIR
    env["OLLAMA_HOST"] = "127.0.0.1:11434"
    env["OLLAMA_ORIGINS"] = "*"
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NO_WINDOW
    try:
        OLLAMA_PROCESS = subprocess.Popen(
            [exe, "serve"],
            cwd=ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
        for _ in range(25):
            if ollama_online(timeout=0.5):
                return True
            time.sleep(0.4)
    except Exception:
        return False
    return ollama_online()


def list_ollama_models():
    try:
        with urllib.request.urlopen(OLLAMA_URL + "/api/tags", timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return [m.get("name", "") for m in data.get("models", []) if m.get("name")]
    except Exception:
        return []


def list_gguf_files():
    out = []
    if not os.path.isdir(MODELS_DIR):
        return out
    for name in os.listdir(MODELS_DIR):
        if name.lower().endswith(".gguf"):
            full = os.path.join(MODELS_DIR, name)
            out.append({
                "name": os.path.splitext(name)[0],
                "file": name,
                "path": rel_path(full),
                "size": os.path.getsize(full),
            })
    return sorted(out, key=lambda item: item["name"].lower())


def normalize_api_base(value):
    return (value or "").strip().rstrip("/")


def normalize_api_model(provider, model):
    model = (model or "").strip()
    if (provider or "").lower() == "gemini" and model.startswith("models/"):
        return model.split("/", 1)[1]
    return model


def choose_api_model(models, provider):
    ids = [normalize_api_model(provider, m.get("id", "")) for m in models if m.get("id")]
    for wanted in API_PRESETS.get(provider, {}).get("fast", []):
        for mid in ids:
            if wanted.lower() in mid.lower():
                return mid
    return ids[0] if ids else API_PRESETS.get(provider, {}).get("fallbackModel", "")


def request_json(url, payload=None, headers=None, timeout=30, method=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method or ("POST" if payload is not None else "GET"))
    req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def describe_http_error(exc):
    try:
        raw = exc.read().decode("utf-8", errors="ignore")
    except Exception:
        raw = ""
    message = raw.strip()
    try:
        parsed = json.loads(raw) if raw else {}
        error = parsed.get("error", parsed)
        if isinstance(error, dict):
            message = error.get("message") or error.get("error") or message
        elif isinstance(error, str):
            message = error
    except Exception:
        pass
    return ("HTTP %s: %s" % (getattr(exc, "code", "?"), message or getattr(exc, "reason", "erro de API")))[:1200]


def decode_attachment_data(payload):
    data_url = payload.get("dataUrl") or ""
    if "," not in data_url:
        raise ValueError("Arquivo anexado invalido.")
    header, encoded = data_url.split(",", 1)
    if ";base64" not in header:
        return urllib.parse.unquote_to_bytes(encoded)
    raw = base64.b64decode(encoded, validate=False)
    if len(raw) > ATTACH_MAX_BYTES:
        raise ValueError("Arquivo muito grande para anexar ao prompt.")
    return raw


def decode_text_bytes(raw):
    for enc in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            if text and text.count("\ufffd") / max(len(text), 1) < 0.02:
                return text
        except Exception:
            pass
    return raw.decode("utf-8", errors="replace")


def xml_text_from_zip(raw, paths):
    chunks = []
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        names = zf.namelist()
        selected = []
        for pattern in paths:
            selected.extend([name for name in names if re.fullmatch(pattern, name)])
        for name in sorted(set(selected)):
            try:
                root = ET.fromstring(zf.read(name))
            except Exception:
                continue
            parts = []
            for node in root.iter():
                if node.tag.endswith("}t") or node.tag.endswith("}a:t") or node.tag.endswith("}v"):
                    if node.text:
                        parts.append(node.text)
            if parts:
                chunks.append(" ".join(parts))
    return "\n".join(chunks)


def extract_pdf_text(raw):
    data = raw.decode("latin-1", errors="ignore")
    found = []
    for match in re.finditer(r"\((?:\\.|[^\\)]){2,}\)", data):
        text = match.group(0)[1:-1]
        text = text.replace("\\n", "\n").replace("\\r", "\r").replace("\\t", "\t")
        text = re.sub(r"\\([()\\])", r"\1", text)
        text = re.sub(r"\\[0-7]{1,3}", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) > 2 and any(ch.isalpha() for ch in text):
            found.append(text)
    return "\n".join(found)


def extract_attachment_text(name, mime, raw):
    low = (name or "").lower()
    mime = (mime or "").lower()
    if mime.startswith("text/") or low.endswith((
        ".txt", ".md", ".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".json",
        ".csv", ".tsv", ".xml", ".yaml", ".yml", ".ini", ".log", ".bat", ".ps1", ".sql",
    )):
        return decode_text_bytes(raw)
    if low.endswith(".docx"):
        return xml_text_from_zip(raw, [r"word/document\.xml", r"word/header\d*\.xml", r"word/footer\d*\.xml"])
    if low.endswith(".pptx"):
        return xml_text_from_zip(raw, [r"ppt/slides/slide\d+\.xml", r"ppt/notesSlides/notesSlide\d+\.xml"])
    if low.endswith(".xlsx"):
        return xml_text_from_zip(raw, [r"xl/sharedStrings\.xml", r"xl/worksheets/sheet\d+\.xml"])
    if low.endswith(".pdf") or mime == "application/pdf":
        return extract_pdf_text(raw)
    return ""


def trim_attachment_text(text):
    text = (text or "").replace("\x00", "").strip()
    truncated = len(text) > ATTACH_TEXT_CHARS
    return text[:ATTACH_TEXT_CHARS], truncated


def read_web_bytes(url, timeout=25, max_bytes=WEB_MAX_BYTES):
    parsed = urllib.parse.urlparse((url or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("Use uma URL http ou https valida.")
    req = urllib.request.Request(
        urllib.parse.urlunparse(parsed),
        headers={
            "User-Agent": WEB_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml,text/plain,*/*;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raw = raw[:max_bytes]
        return {
            "url": resp.geturl(),
            "status": getattr(resp, "status", 200),
            "contentType": resp.headers.get("Content-Type", ""),
            "bytes": raw,
        }


def clean_html_text(raw):
    text = decode_text_bytes(raw)
    title = ""
    title_match = re.search(r"(?is)<title[^>]*>(.*?)</title>", text)
    if title_match:
        title = html_lib.unescape(re.sub(r"\s+", " ", title_match.group(1))).strip()
    text = re.sub(r"(?is)<(script|style|noscript|svg|canvas|template)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"(?is)<br\s*/?>", "\n", text)
    text = re.sub(r"(?is)</(p|div|li|tr|h[1-6]|section|article|blockquote)>", "\n", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return title, text


def web_fetch(url, max_chars=WEB_TEXT_CHARS):
    data = read_web_bytes(url)
    ctype = data["contentType"].lower()
    if "html" in ctype or data["url"].lower().endswith((".html", ".htm", "/")):
        title, text = clean_html_text(data["bytes"])
    elif ctype.startswith("text/") or "json" in ctype or "xml" in ctype:
        title, text = "", decode_text_bytes(data["bytes"])
    else:
        title, text = "", extract_attachment_text(os.path.basename(urllib.parse.urlparse(data["url"]).path), ctype, data["bytes"])
    text = (text or "").strip()
    truncated = len(text) > max_chars
    return {
        "url": data["url"],
        "status": data["status"],
        "contentType": data["contentType"],
        "title": title,
        "text": text[:max_chars],
        "truncated": truncated,
    }


def decode_ddg_url(href):
    href = html_lib.unescape(href or "")
    parsed = urllib.parse.urlparse(href)
    query = urllib.parse.parse_qs(parsed.query)
    if query.get("uddg"):
        return query["uddg"][0]
    if href.startswith("//"):
        return "https:" + href
    return href


def web_search(query, limit=5):
    query = (query or "").strip()
    if not query:
        raise ValueError("Query obrigatoria.")
    limit = max(1, min(int(limit or 5), 8))
    search_url = "https://duckduckgo.com/html/?q=" + urllib.parse.quote_plus(query)
    page = read_web_bytes(search_url, timeout=25, max_bytes=WEB_MAX_BYTES)
    html_text = decode_text_bytes(page["bytes"])
    results = []
    pattern = re.compile(r'(?is)<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>')
    snippet_pattern = re.compile(r'(?is)<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>|<div[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</div>')
    snippets = [
        html_lib.unescape(re.sub(r"<[^>]+>", " ", (m.group(1) or m.group(2) or ""))).strip()
        for m in snippet_pattern.finditer(html_text)
    ]
    for idx, match in enumerate(pattern.finditer(html_text)):
        url = decode_ddg_url(match.group(1))
        title = html_lib.unescape(re.sub(r"<[^>]+>", " ", match.group(2))).strip()
        if not url.startswith(("http://", "https://")) or not title:
            continue
        if any(item["url"] == url for item in results):
            continue
        results.append({"title": title, "url": url, "snippet": snippets[idx] if idx < len(snippets) else ""})
        if len(results) >= limit:
            break
    return {"query": query, "source": search_url, "results": results}


HTML = r"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Yami Portable AI</title>
<style>
*{box-sizing:border-box}html,body{height:100%;margin:0;overflow:hidden}
:root{--bg:#03080d;--panel:#07141d;--panel2:#0b1f2a;--line:#123040;--line2:#1a5368;--text:#dff8ff;--muted:#7f9aa4;--dim:#405c66;--accent:#00d4ff;--accent2:#39ff88;--danger:#ff5570;--warn:#ffd166;--font:14px}
body{font-family:Inter,Segoe UI,Arial,sans-serif;background:var(--bg);color:var(--text);font-size:var(--font)}
button,input,select,textarea{font:inherit}button{border:1px solid var(--line2);background:#061923;color:var(--text);border-radius:7px;padding:8px 10px;cursor:pointer}
button:hover,button.active{border-color:var(--accent);box-shadow:0 0 0 1px color-mix(in srgb,var(--accent) 45%,transparent)}button.primary{background:linear-gradient(90deg,var(--accent),#00a8d3);border:0;color:#00131a;font-weight:800}button.green{border-color:var(--accent2);color:var(--accent2)}
input,select,textarea{width:100%;border:1px solid var(--line);border-radius:7px;background:#041018;color:var(--text);padding:8px}textarea{resize:vertical;min-height:78px}
#app{height:100vh;display:grid;grid-template-rows:50px minmax(0,1fr)}#top{height:50px;min-height:50px;display:flex;align-items:center;gap:10px;border-bottom:1px solid var(--line);background:#06121a;padding:0 10px;overflow:hidden}
.brand{font-weight:900;color:var(--accent);letter-spacing:.05em;text-transform:uppercase;min-width:72px}.label{font-size:10px;color:var(--dim);text-transform:uppercase;letter-spacing:.12em}#model{max-width:275px;min-width:220px}#temp{width:76px}#meter{margin-left:auto;display:flex;gap:14px;color:var(--muted);font-size:12px;white-space:nowrap}
#layout{height:calc(100vh - 50px);display:grid;grid-template-columns:240px minmax(0,1fr);min-height:0}#side,#studio{background:var(--panel);border-right:1px solid var(--line);min-height:0;display:flex;flex-direction:column}#studio{position:fixed;top:50px;right:0;bottom:0;width:min(420px,100vw);border-right:0;border-left:1px solid var(--line);transform:translateX(105%);transition:.18s ease;z-index:40;box-shadow:-22px 0 50px rgba(0,0,0,.45)}#studio.open{transform:translateX(0)}
.side-head{padding:10px;border-bottom:1px solid var(--line)}#chat-list{flex:1;overflow:auto;padding:10px}.chat-item{display:flex;gap:8px;align-items:center;padding:9px;border:1px solid transparent;border-radius:7px;color:var(--muted);cursor:pointer}.chat-item:hover,.chat-item.active{border-color:var(--line2);background:#081b25;color:var(--text)}.chat-title{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.chat-del{padding:2px 6px;border-color:transparent;color:var(--danger)}
.status{border-top:1px solid var(--line);padding:12px;color:var(--muted);font-size:12px}.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#444;margin-right:6px}.dot.on{background:var(--accent2);box-shadow:0 0 12px var(--accent2)}
#main{position:relative;min-width:0;height:100%;min-height:0;display:flex;flex-direction:column;background:linear-gradient(rgba(0,212,255,.04) 1px,transparent 1px),linear-gradient(90deg,rgba(0,212,255,.04) 1px,transparent 1px),var(--bg);background-size:44px 44px}
#prompt-panel{display:none;grid-template-columns:1fr auto auto;gap:8px;padding:10px;background:rgba(7,20,29,.96);border-bottom:1px solid var(--line)}#prompt-panel.open{display:grid}
#chat{flex:1 1 auto;overflow:auto;padding:22px;min-height:0}.welcome{height:100%;display:grid;place-content:center;text-align:center;color:var(--muted)}.welcome h1{margin:0 0 8px;color:var(--text)}
.msg{max-width:76%;margin:12px 0;padding:13px 14px;border-radius:8px;border:1px solid var(--line);white-space:pre-wrap;line-height:1.55}.msg.user{margin-left:auto;background:#06212d;border-color:var(--line2)}.msg.ai{background:#071923}.msg.err{border-color:var(--danger);color:var(--danger)}
#voice-status{border-top:1px solid var(--line);padding:8px 12px;color:var(--muted);font-size:12px}#composer{background:rgba(3,8,13,.96);border-top:1px solid var(--line)}#input{padding:8px 12px 10px;display:grid;grid-template-columns:auto 1fr auto auto;gap:8px;align-items:end}#msg{min-height:42px;max-height:130px}.icon-btn{width:42px;height:42px;display:grid;place-items:center;padding:0;font-weight:900}#file-picker{display:none}#attachment-list{display:none;gap:7px;flex-wrap:wrap;padding:9px 12px 0}#attachment-list.has-files{display:flex}.attach-chip{display:flex;align-items:center;gap:7px;max-width:min(280px,100%);border:1px solid var(--line2);border-radius:7px;background:#061923;color:var(--muted);padding:6px 8px;font-size:12px}.attach-chip b{color:var(--text);font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.attach-chip small{color:var(--dim);white-space:nowrap}.attach-chip button{border:0;padding:0 3px;color:var(--danger);background:transparent}.drop-hot #input{box-shadow:inset 0 0 0 1px var(--accent2)}
.dock-head{display:flex;justify-content:space-between;align-items:center;padding:10px;border-bottom:1px solid var(--line)}.tabs{display:flex;gap:6px;padding:8px;border-bottom:1px solid var(--line)}.tab{flex:1;padding:7px;font-size:12px}.pane{display:none;overflow:auto;padding:12px;gap:10px}.pane.active{display:grid}.row{display:grid;grid-template-columns:94px 1fr;gap:8px;align-items:center}.actions{display:flex;gap:8px;flex-wrap:wrap}.note{font-size:12px;color:var(--muted);min-height:18px}.note.ok{color:var(--accent2)}.note.err{color:var(--danger)}
#file-list{border:1px solid var(--line);border-radius:7px;max-height:170px;overflow:auto}.file{display:grid;grid-template-columns:20px 1fr auto;gap:6px;padding:7px;border-bottom:1px solid #0d2632;cursor:pointer;color:var(--muted)}.file:hover{background:#081b24;color:var(--text)}#file-editor,#custom-css{min-height:180px;font-family:Consolas,monospace}
@media(max-width:900px){#layout{grid-template-columns:1fr}#side{display:none}#studio{width:min(420px,100vw)}.msg{max-width:90%}#meter{display:none}#model{min-width:150px}}
</style>
</head>
<body>
<div id="app">
  <header id="top">
    <div class="brand">Yami</div><span class="label">Modelo</span><select id="model"><option>Loading...</option></select><span class="label">Temp</span><input id="temp" type="number" min="0" max="2" step=".1" value=".7">
    <button onclick="togglePrompt()">Prompt</button><button id="mic" onclick="toggleListening()">Mic</button><button id="speak" class="green active" onclick="toggleSpeech()">Voz</button><button id="studio-btn" onclick="toggleStudio()">Studio</button>
    <div id="meter"><span>CPU <b id="cpu">--</b>%</span><span>RAM <b id="ram">--</b>%</span></div>
  </header>
  <div id="layout">
    <aside id="side"><div class="side-head"><button class="primary" style="width:100%" onclick="newChat()">+ New Chat</button></div><div id="chat-list"></div><div class="status"><span id="dot" class="dot"></span><span id="status">Connecting...</span><br>Yami salva tudo junto do EXE</div></aside>
    <main id="main">
      <section id="prompt-panel"><textarea id="sys-prompt"></textarea><button class="green" onclick="saveSettings()">Salvar</button><button onclick="resetPrompt()">Restaurar</button></section>
      <section id="chat"><div class="welcome"><h1>Yami online</h1><p>Use modelo local do pendrive ou chave API. Mic aceita qualquer audio por padrao.</p></div></section>
      <div id="composer"><div id="voice-status">Audio pronto. Ative Mic e fale qualquer mensagem.</div><div id="attachment-list"></div><div id="input"><button id="attach" class="icon-btn" title="Adicionar arquivos ao prompt" onclick="pickFiles()">+</button><input id="file-picker" type="file" multiple><textarea id="msg" rows="1" placeholder="Fale com a Yami ou digite um prompt"></textarea><button id="send" class="primary" onclick="sendMessage()">Enviar</button><button id="stop" style="display:none" onclick="stopGen()">Parar</button></div></div>
    </main>
    <aside id="studio">
      <div class="dock-head"><b>Yami Studio</b><button onclick="toggleStudio()">Fechar</button></div>
      <div class="tabs"><button class="tab active" data-tab="config" onclick="tabTo('config')">Config</button><button class="tab" data-tab="voice" onclick="tabTo('voice')">Audio</button><button class="tab" data-tab="files" onclick="tabTo('files')">Arquivos</button><button class="tab" data-tab="style" onclick="tabTo('style')">Interface</button></div>
      <section class="pane active" data-pane="config">
        <div class="row"><label>Fonte</label><select id="mode"><option value="local">Local/Ollama</option><option value="api">API online</option></select></div>
        <div class="row"><label>API</label><select id="api-provider"><option value="openai">OpenAI</option><option value="gemini">Gemini</option><option value="compatible">Outra</option></select></div>
        <div class="row"><label>Local</label><select id="local-model"><option>Carregando...</option></select></div>
        <div class="row"><label>API URL</label><input id="api-base"></div><div class="row"><label>Modelo</label><input id="api-model" placeholder="detectado pela chave"></div><div class="row"><label>Chave</label><input id="api-key" type="password"></div>
        <div class="actions"><button class="green" onclick="detectApi()">Detectar pela chave</button><button class="primary" onclick="saveSettings()">Salvar</button><button onclick="fetchModels()">Atualizar</button><button onclick="importLocalModel()">Importar GGUF</button></div><div class="note" id="provider-note"></div>
      </section>
      <section class="pane" data-pane="voice">
        <div class="row"><label>Idioma</label><select id="voice-lang"><option value="pt-BR">pt-BR</option><option value="en-US">en-US</option><option value="es-ES">es-ES</option></select></div><div class="row"><label>Comando</label><input id="wake-word" value="ola yami"></div>
        <div class="row"><label>Ritmo</label><input id="voice-rate" type="range" min=".6" max="1.5" step=".1" value="1"></div><div class="row"><label>Tom</label><input id="voice-pitch" type="range" min=".6" max="1.6" step=".1" value="1"></div>
        <div class="actions"><button id="wake-toggle" onclick="toggleWake()">Aceitar qualquer audio</button><button class="green" onclick="testVoice()">Teste voz</button><button onclick="saveSettings()">Salvar</button></div><div class="note">Para falar "ola yami", ative o comando. Desativado = qualquer audio vira mensagem.</div>
      </section>
      <section class="pane" data-pane="files">
        <div class="row"><label>Pasta</label><input id="drive-path" value="."></div><div class="actions"><button onclick="loadDir()">Abrir</button><button onclick="loadDir('.')">Raiz</button></div><div id="file-list"></div>
        <div class="row"><label>Arquivo</label><input id="file-path" placeholder="yami_portable.py"></div><textarea id="file-editor" spellcheck="false"></textarea><div class="actions"><button class="primary" onclick="saveFile()">Salvar arquivo</button><button onclick="readFile($('#file-path').value)">Recarregar</button></div><div class="note" id="file-note"></div>
      </section>
      <section class="pane" data-pane="style">
        <div class="row"><label>Accent</label><input id="accent" type="color" value="#00d4ff"></div><div class="row"><label>Accent 2</label><input id="accent2" type="color" value="#39ff88"></div><div class="row"><label>Fonte</label><select id="font-size"><option>13px</option><option selected>14px</option><option>15px</option><option>16px</option></select></div>
        <textarea id="custom-css" placeholder="CSS personalizado"></textarea><div class="actions"><button class="primary" onclick="saveSettings()">Aplicar</button><button onclick="resetStyle()">Reset</button></div>
      </section>
    </aside>
  </div>
</div>
<script>
'use strict';
const $=s=>document.querySelector(s), id=s=>document.getElementById(s);
const SpeechRecognitionCtor=window.SpeechRecognition||window.webkitSpeechRecognition;
const DEFAULT_PROMPT='Voce e Yami, uma agente virtual em portugues brasileiro que roda dentro deste pendrive. Voce pode conversar por texto e audio, usar modelo local via Ollama ou API online com chave, editar arquivos dentro da pasta da Yami, pesquisar na internet e ajudar o usuario a personalizar seu proprio codigo. Quando precisar ler ou alterar arquivos, ou buscar informacoes atuais na web, emita um bloco ```yami-action com JSON valido. Acoes: list_dir, read_file, write_file, append_file, replace_in_file, web_search, web_fetch. Use web_search com query para pesquisar; use web_fetch com url para abrir uma pagina. Cite as URLs usadas. Use caminhos relativos a pasta da Yami. Nunca acesse fora dessa pasta.';
const ACTION_PROMPT='Se precisar autoeditar ou acessar a internet, responda com bloco ```yami-action contendo JSON valido. Acoes de internet: {"action":"web_search","query":"termo","limit":5} e {"action":"web_fetch","url":"https://..."}; depois do resultado interno, continue a tarefa e cite as URLs usadas.';
const API_PRESETS={openai:{baseUrl:'https://api.openai.com/v1',placeholder:'sk-...'},gemini:{baseUrl:'https://generativelanguage.googleapis.com/v1beta/openai',placeholder:'AIza...'},compatible:{baseUrl:'',placeholder:'API key'}};
const MAX_ATTACHMENTS=10,MAX_ATTACH_BYTES=8*1024*1024;
let settings={}, chats=[], active=null, aborter=null, generating=false, rec=null, recWanted=false, speechOn=true, wakeOn=false, currentDir='.', pendingAttachments=[];
function clone(o){return JSON.parse(JSON.stringify(o))} function esc(s){const d=document.createElement('div');d.textContent=s||'';return d.innerHTML} function note(el,msg,type){id(el).textContent=msg||'';id(el).className='note '+(type||'')}
function normApiModel(provider,model){model=String(model||'').trim();return provider==='gemini'&&model.startsWith('models/')?model.slice(7):model}
async function api(path,opts={}){const r=await fetch(path,opts); if(!r.ok)throw new Error(await r.text()); return r.json()}
async function loadSettings(){settings=await api('/api/settings'); speechOn=settings.voice.speak!==false; wakeOn=!!settings.voice.wakeWordEnabled; applySettings()}
function applySettings(){const prov=settings.provider.apiProvider||'openai';id('temp').value=settings.provider.temperature||.7; id('mode').value=settings.provider.mode||'local'; id('api-provider').value=prov; id('api-base').value=settings.provider.apiBaseUrl||API_PRESETS[prov]?.baseUrl||API_PRESETS.openai.baseUrl; id('api-model').value=normApiModel(prov,settings.provider.apiModel||''); id('api-key').value=settings.provider.apiKey||''; id('sys-prompt').value=settings.agent.systemPrompt||DEFAULT_PROMPT; id('voice-lang').value=settings.voice.lang||'pt-BR'; id('wake-word').value=settings.voice.wakeWord||'ola yami'; id('voice-rate').value=settings.voice.rate||1; id('voice-pitch').value=settings.voice.pitch||1; id('accent').value=settings.ui.accent||'#00d4ff'; id('accent2').value=settings.ui.accent2||'#39ff88'; id('font-size').value=settings.ui.fontSize||'14px'; id('custom-css').value=settings.ui.customCss||''; id('speak').classList.toggle('active',speechOn); id('wake-toggle').textContent=wakeOn?'Exigir ola yami':'Aceitar qualquer audio'; applyStyle(); applyApiPreset(); syncProviderUi()}
function collectProvider(){const apiProvider=id('api-provider').value||settings.provider.apiProvider||'openai';const mode=id('mode').value||settings.provider.mode||'local';const preset=API_PRESETS[apiProvider]||API_PRESETS.openai;const local=id('local-model').value||((mode==='local')?id('model').value:'')||settings.provider.localModel||'';return {mode,apiProvider,apiBaseUrl:id('api-base').value.trim()||settings.provider.apiBaseUrl||preset.baseUrl||'',apiModel:normApiModel(apiProvider,id('api-model').value.trim()||settings.provider.apiModel||''),apiKey:id('api-key').value.trim()||settings.provider.apiKey||'',localModel:local,temperature:Number(id('temp').value||settings.provider.temperature||.7)}}
function syncProviderUi(){const p=collectProvider(); const apiMode=p.mode==='api'; id('model').disabled=apiMode; if(apiMode){const label=p.apiModel?('API: '+p.apiModel):(p.apiKey?'API: modelo automatico':'API: cole a chave no Studio'); id('model').innerHTML='<option value="'+esc(p.apiModel||'api')+'">'+esc(label)+'</option>'; id('status').textContent=p.apiKey?'API pronta para salvar/usar':'API - cole sua chave no Studio'} id('api-key').placeholder=API_PRESETS[p.apiProvider]?.placeholder||'API key'}
async function saveSettings(){settings.provider=collectProvider(); settings.voice={listen:recWanted,speak:speechOn,wakeWordEnabled:wakeOn,wakeWord:id('wake-word').value.trim()||'ola yami',lang:id('voice-lang').value,rate:Number(id('voice-rate').value),pitch:Number(id('voice-pitch').value)}; settings.agent.systemPrompt=id('sys-prompt').value.trim()||DEFAULT_PROMPT; settings.ui={accent:id('accent').value,accent2:id('accent2').value,fontSize:id('font-size').value,customCss:id('custom-css').value}; await api('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(settings)}); applyStyle(); syncProviderUi(); note('provider-note','Configuracoes salvas.','ok')}
function applyStyle(){document.documentElement.style.setProperty('--accent',settings.ui?.accent||'#00d4ff');document.documentElement.style.setProperty('--accent2',settings.ui?.accent2||'#39ff88');document.documentElement.style.setProperty('--font',settings.ui?.fontSize||'14px');let s=id('custom-style');if(!s){s=document.createElement('style');s.id='custom-style';document.head.appendChild(s)}s.textContent=settings.ui?.customCss||''}
function resetStyle(){settings.ui={accent:'#00d4ff',accent2:'#39ff88',fontSize:'14px',customCss:''};applySettings();saveSettings()} function resetPrompt(){id('sys-prompt').value=DEFAULT_PROMPT;saveSettings()}
async function fetchModels(){try{const d=await api('/api/models'); id('dot').classList.toggle('on',d.engineOnline); id('status').textContent=(settings.provider?.mode==='api'?'API':'Local')+' - '+d.local.length+' modelo(s)'; id('model').disabled=false; id('model').innerHTML=''; id('local-model').innerHTML=''; d.local.forEach(m=>{const o=document.createElement('option');o.value=m.name;o.textContent=m.name+(m.source==='gguf'?' (GGUF)':'');id('model').appendChild(o);id('local-model').appendChild(o.cloneNode(true))}); if(!d.local.length){id('model').innerHTML='<option value="">Nenhum modelo local</option>';id('local-model').innerHTML=id('model').innerHTML} const saved=settings.provider.localModel; const chosen=d.local.some(m=>m.name===saved)?saved:(d.local[0]?.name||''); if(chosen){id('model').value=chosen;id('local-model').value=chosen;settings.provider.localModel=chosen} syncProviderUi()}catch(e){id('status').textContent='Engine offline'; syncProviderUi()}}
function applyApiPreset(){const p=id('api-provider').value,preset=API_PRESETS[p]||API_PRESETS.openai; if(p!=='compatible'||!id('api-base').value.trim())id('api-base').value=preset.baseUrl; id('api-key').placeholder=preset.placeholder}
async function detectApi(){if(!id('api-key').value.trim()&&!settings.provider.apiKey){note('provider-note','Cole a chave API primeiro.','err');return} note('provider-note','Detectando modelo...'); try{const d=await api('/api/provider/detect',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({apiProvider:id('api-provider').value,apiBaseUrl:id('api-base').value,apiKey:id('api-key').value.trim()||settings.provider.apiKey})}); id('mode').value='api'; id('api-model').value=normApiModel(id('api-provider').value,d.apiModel||id('api-model').value); await saveSettings(); note('provider-note',(d.ok?'API pronta: ':'Fallback: ')+(id('api-model').value||''),d.ok?'ok':'')}catch(e){note('provider-note',e.message,'err')}}
async function importLocalModel(){note('provider-note','Importando GGUF para Ollama...'); try{const d=await api('/api/import-model',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({})}); note('provider-note',d.message||'Importado.','ok'); await fetchModels()}catch(e){note('provider-note',e.message,'err')}}
function pickFiles(){id('file-picker').click()}
function readAsDataURL(file){return new Promise((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(r.result);r.onerror=()=>reject(r.error||new Error('Falha ao ler arquivo.'));r.readAsDataURL(file)})}
async function addFiles(fileList){const files=[...fileList];if(!files.length)return;voice('Lendo arquivo(s) para o prompt...','hot');for(const file of files){if(pendingAttachments.length>=MAX_ATTACHMENTS){voice('Limite de anexos atingido.','warn');break}if(file.size>MAX_ATTACH_BYTES){pendingAttachments.push({id:String(Date.now()+Math.random()),name:file.name,type:file.type||'application/octet-stream',size:file.size,kind:'binary',text:'Arquivo acima do limite de leitura do prompt.'});continue}try{const dataUrl=await readAsDataURL(file);const parsed=await api('/api/attachment/parse',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:file.name,type:file.type,dataUrl})});const att={id:String(Date.now()+Math.random()),name:parsed.name||file.name,type:parsed.type||file.type||'application/octet-stream',size:parsed.size||file.size,kind:parsed.kind||'binary',text:parsed.text||'',truncated:!!parsed.truncated};if(att.kind==='image')att.dataUrl=dataUrl;pendingAttachments.push(att)}catch(e){pendingAttachments.push({id:String(Date.now()+Math.random()),name:file.name,type:file.type||'application/octet-stream',size:file.size,kind:'binary',text:'Nao foi possivel extrair texto: '+e.message})}}renderAttachments();voice(pendingAttachments.length+' arquivo(s) no prompt.','hot')}
function removeAttachment(aid){pendingAttachments=pendingAttachments.filter(a=>a.id!==aid);renderAttachments()}
function renderAttachments(){const box=id('attachment-list');box.classList.toggle('has-files',pendingAttachments.length>0);box.innerHTML=pendingAttachments.map(a=>`<div class="attach-chip"><span>${a.kind==='image'?'IMG':a.kind==='text'?'TXT':'BIN'}</span><b title="${esc(a.name)}">${esc(a.name)}</b><small>${fmt(a.size)}</small><button title="Remover" onclick="removeAttachment('${a.id}')">x</button></div>`).join('')}
function promptWithAttachments(text,files){let out=(text||'Analise os arquivos anexados.').trim();if(!files.length)return out;out+='\n\n[Arquivos anexados ao prompt]\n';files.forEach((a,i)=>{out+=`\n--- Arquivo ${i+1}: ${a.name} (${a.type||'sem tipo'}, ${fmt(a.size)}) ---\n`;if(a.text){out+=a.text;if(a.truncated)out+='\n[conteudo truncado pelo limite do prompt]'}else if(a.kind==='image'){out+='[imagem anexada visualmente; modelos/API com visao podem analisar a imagem]'}else{out+='[arquivo anexado sem texto extraivel; a Yami recebeu nome, tipo e tamanho]' }out+='\n'});return out}
function attachmentDisplay(text,files){if(!files.length)return text;return (text||'Analise os arquivos anexados.')+'\n\nArquivos: '+files.map(a=>a.name).join(', ')}
function cleanAttachment(a){return {name:a.name,type:a.type,size:a.size,kind:a.kind,truncated:!!a.truncated}}
function outboundUserMessage(content,files,provider){const images=files.filter(a=>a.kind==='image'&&a.dataUrl);if(!images.length)return {role:'user',content};if(provider.mode==='api')return {role:'user',content:[{type:'text',text:content},...images.map(a=>({type:'image_url',image_url:{url:a.dataUrl}}))]};return {role:'user',content,images:images.map(a=>String(a.dataUrl).split(',',2)[1]||'')}}
async function loadChats(){chats=await api('/api/chats')} function saveChats(){fetch('/api/chats',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(chats)}).catch(()=>{})}
function newChat(){const c={id:String(Date.now()),title:'New Chat',messages:[],model:''};chats.unshift(c);active=c.id;renderList();renderChat();saveChats()} function chat(){return chats.find(c=>c.id===active)}
function renderList(){id('chat-list').innerHTML=chats.map(c=>`<div class="chat-item ${c.id===active?'active':''}" onclick="active='${c.id}';renderList();renderChat()"><span>◆</span><span class="chat-title">${esc(c.title)}</span><button class="chat-del" onclick="event.stopPropagation();chats=chats.filter(x=>x.id!=='${c.id}');active=chats[0]?.id||null;renderList();renderChat();saveChats()">x</button></div>`).join('')||'<div class="note" style="padding:12px">Sem chats ainda</div>'}
function renderChat(){const box=id('chat');box.innerHTML='';const c=chat();if(!c||!c.messages.length){box.innerHTML='<div class="welcome"><h1>Yami online</h1><p>Use modelo local do pendrive ou chave API. Mic aceita qualquer audio por padrao.</p></div>';return}c.messages.forEach(m=>bubble(m.display||m.content,m.role))}
function bubble(text,role){const d=document.createElement('div');d.className='msg '+(role==='user'?'user':'ai');d.innerHTML=esc(text).replace(/\n/g,'<br>');id('chat').appendChild(d);id('chat').scrollTop=id('chat').scrollHeight;return d}
async function sendMessage(opts={}){const text=(opts.textOverride??id('msg').value).trim(),files=opts.attachments||pendingAttachments.slice();if((!text&&!files.length)||generating)return;if(!active)newChat();const c=chat();if(!opts.silent){id('msg').value='';pendingAttachments=[];renderAttachments()}const provider=collectProvider();settings.provider=provider;const modelText=promptWithAttachments(text,files),displayText=attachmentDisplay(text,files);const userMsg={role:'user',content:modelText,display:displayText,attachments:files.map(cleanAttachment)};c.messages.push(userMsg);if(c.title==='New Chat')c.title=(text||files[0]?.name||'Arquivos anexados').slice(0,50);renderList();bubble(displayText,'user');const ai=bubble('','assistant');generating=true;id('send').style.display='none';id('stop').style.display='block';aborter=new AbortController();let full='',follow='';try{if(provider.mode==='api'&&!provider.apiKey)throw new Error('Cole sua chave API no Studio antes de enviar.');const history=c.messages.slice(0,-1).map(m=>({role:m.role,content:m.content}));const messages=[{role:'system',content:(settings.agent.systemPrompt||DEFAULT_PROMPT)+'\n\n'+ACTION_PROMPT},...history,outboundUserMessage(modelText,files,provider)];const mode=provider.mode;const model=mode==='api'?(provider.apiModel||'auto'):(id('model').value||provider.localModel);const r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode,model,messages,temperature:provider.temperature,provider}),signal:aborter.signal});if(!r.ok)throw new Error(await r.text());const reader=r.body.getReader(),dec=new TextDecoder();let buf='';while(true){const {done,value}=await reader.read();if(done)break;buf+=dec.decode(value,{stream:true});const lines=buf.split(/\n/);buf=lines.pop();for(const line of lines){if(!line.trim())continue;try{const j=JSON.parse(line);const delta=j.message?.content??j.response??'';if(delta){full+=delta;ai.innerHTML=esc(full).replace(/\n/g,'<br>');id('chat').scrollTop=id('chat').scrollHeight}}catch{}}}if(!full.trim())full='[sem resposta]';ai.innerHTML=esc(full).replace(/\n/g,'<br>');c.messages.push({role:'assistant',content:full});follow=await runActions(full,opts.depth||0);if(!follow)speak(full);saveChats();saveSettings().catch(()=>{})}catch(e){ai.classList.add('err');ai.textContent=e.name==='AbortError'?'Resposta interrompida.':e.message;c.messages.pop();saveChats()}finally{generating=false;aborter=null;id('send').style.display='block';id('stop').style.display='none';if(follow)setTimeout(()=>sendMessage({textOverride:follow,silent:true,depth:(opts.depth||0)+1}),250)}}
function stopGen(){aborter?.abort()} function extractActions(t){const a=[],re=/```yami-action\s*([\s\S]*?)```/gi;let m;while((m=re.exec(t||''))){try{const p=JSON.parse(m[1].trim());Array.isArray(p)?a.push(...p):a.push(p)}catch(e){a.push({action:'error',error:e.message})}}return a} async function runActions(t,d){if(!settings.agent.autoActions||d>=Number(settings.agent.maxActionLoops||4))return'';const acts=extractActions(t);if(!acts.length)return'';const res=[];for(const a of acts){try{res.push(await api('/api/yami/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(a)}))}catch(e){res.push({ok:false,error:e.message})}}return '[Resultado das acoes internas da Yami]\n'+JSON.stringify(res,null,2)+'\n\nContinue a tarefa.'}
function setupRec(){if(!SpeechRecognitionCtor)return null;if(rec)return rec;rec=new SpeechRecognitionCtor();rec.continuous=true;rec.interimResults=false;rec.lang=id('voice-lang').value;rec.onstart=()=>{id('mic').classList.add('active');voice(wakeOn?'Ouvindo comando '+id('wake-word').value:'Ouvindo qualquer audio.','hot')};rec.onend=()=>{id('mic').classList.toggle('active',recWanted);if(recWanted)setTimeout(()=>{try{rec.start()}catch{}},350);else voice('Audio pronto. Ative Mic e fale qualquer mensagem.')};rec.onerror=e=>voice('Microfone: '+(e.error||'erro'),'warn');rec.onresult=e=>{for(let i=e.resultIndex;i<e.results.length;i++)if(e.results[i].isFinal)handleVoice(e.results[i][0].transcript)};return rec}
function toggleListening(){recWanted?stopListening():startListening()} function startListening(){if(!SpeechRecognitionCtor){voice('Use Chrome ou Edge para reconhecimento de voz.','warn');return}const r=setupRec();recWanted=true;try{r.start()}catch{}saveSettings()} function stopListening(){recWanted=false;id('mic').classList.remove('active');try{rec?.stop()}catch{}saveSettings()} function toggleSpeech(){speechOn=!speechOn;id('speak').classList.toggle('active',speechOn);if(!speechOn)speechSynthesis?.cancel();saveSettings()} function toggleWake(){wakeOn=!wakeOn;id('wake-toggle').textContent=wakeOn?'Exigir ola yami':'Aceitar qualquer audio';saveSettings()} function norm(s){return String(s||'').toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g,'').replace(/[^\w\s]/g,'').replace(/\s+/g,' ').trim()} function handleVoice(raw){const spoken=(raw||'').trim();if(!spoken)return;const wake=id('wake-word').value||'ola yami';let cmd=spoken;if(wakeOn&&!norm(spoken).includes(norm(wake))){voice('Ouvi "'+spoken+'". Filtro exigindo "'+wake+'".','warn');return}if(norm(spoken).includes(norm(wake)))cmd=spoken.replace(new RegExp('^[\\s\\S]*?'+wake+'[\\s,;:.!-]*','i'),'').trim()||'Ola Yami';id('msg').value=cmd;voice('Comando enviado: '+cmd,'hot');sendMessage()} function speak(t){if(!speechOn||!('speechSynthesis'in window))return;const clean=String(t||'').replace(/```[\s\S]*?```/g,' bloco de codigo omitido. ').replace(/[#>*_~|`]/g,' ').replace(/\s+/g,' ').trim();if(!clean)return;speechSynthesis.cancel();const u=new SpeechSynthesisUtterance(clean.slice(0,4200));u.lang=id('voice-lang').value;u.rate=Number(id('voice-rate').value||1);u.pitch=Number(id('voice-pitch').value||1);const vs=speechSynthesis.getVoices();u.voice=vs.find(v=>v.lang===u.lang)||vs.find(v=>v.lang?.startsWith('pt'))||null;speechSynthesis.speak(u)} function testVoice(){speak('Ola, eu sou a Yami. Estou pronta no pendrive.')} function voice(m,type){id('voice-status').textContent=m;id('voice-status').style.color=type==='warn'?'var(--warn)':type==='hot'?'var(--accent2)':'var(--muted)'}
async function loadDir(p){try{const d=await api('/api/files?path='+encodeURIComponent(p||id('drive-path').value||'.'));currentDir=d.path;id('drive-path').value=d.path;const parent=d.parent?[{name:'..',path:d.parent,type:'dir',size:0}]:[];id('file-list').innerHTML=parent.concat(d.items).map(i=>`<div class="file" onclick="openItem('${encodeURIComponent(i.path)}','${i.type}')"><span>${i.type==='dir'?'▸':'-'}</span><span>${esc(i.name)}</span><small>${i.type==='dir'?'':fmt(i.size)}</small></div>`).join('')||'<div class="file">Vazio</div>';note('file-note',d.root,'ok')}catch(e){note('file-note',e.message,'err')}} function openItem(p,t){p=decodeURIComponent(p);t==='dir'?loadDir(p):readFile(p)} async function readFile(p){if(!p)return;try{const d=await api('/api/file?path='+encodeURIComponent(p));id('file-path').value=d.path;id('file-editor').value=d.content;note('file-note',d.path+' aberto.','ok')}catch(e){note('file-note',e.message,'err')}} async function saveFile(){try{const d=await api('/api/file',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:id('file-path').value,content:id('file-editor').value})});note('file-note',d.path+' salvo. Backup: '+(d.backup||'novo'),'ok');loadDir(currentDir)}catch(e){note('file-note',e.message,'err')}} function fmt(n){if(!n)return'0 B';const u=['B','KB','MB','GB'];let i=0,v=n;while(v>=1024&&i<u.length-1){v/=1024;i++}return(i?v.toFixed(1):Math.round(v))+' '+u[i]}
function togglePrompt(){id('prompt-panel').classList.toggle('open')} function toggleStudio(){id('studio').classList.toggle('open');id('layout').classList.toggle('studio-open');if(id('studio').classList.contains('open'))fetchModels()} function tabTo(n){document.querySelectorAll('.tab').forEach(b=>b.classList.toggle('active',b.dataset.tab===n));document.querySelectorAll('.pane').forEach(p=>p.classList.toggle('active',p.dataset.pane===n));if(n==='files')loadDir(currentDir)} async function stats(){try{const d=await api('/api/stats');id('cpu').textContent=d.cpu;id('ram').textContent=d.ram}catch{}}
id('msg').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendMessage()}});id('model').addEventListener('change',()=>{settings.provider.localModel=id('model').value;id('local-model').value=id('model').value;saveSettings()});id('local-model').addEventListener('change',()=>{id('model').value=id('local-model').value;settings.provider.localModel=id('local-model').value;saveSettings()});id('mode').addEventListener('change',()=>{saveSettings().then(fetchModels)});id('api-provider').addEventListener('change',()=>{applyApiPreset();id('api-model').value='';saveSettings().then(()=>{if(id('api-key').value.trim())detectApi()})});['api-base','api-model','temp','voice-lang','wake-word','voice-rate','voice-pitch','accent','accent2','font-size','custom-css'].forEach(x=>id(x).addEventListener(x==='custom-css'?'input':'change',()=>saveSettings()));id('api-key').addEventListener('input',()=>{settings.provider.apiKey=id('api-key').value.trim()||settings.provider.apiKey;syncProviderUi()});id('api-key').addEventListener('blur',()=>{if(id('api-key').value.trim()){settings.provider.apiKey=id('api-key').value.trim();if(id('mode').value==='api'&&!id('api-model').value.trim())detectApi();else saveSettings()}});
id('file-picker').addEventListener('change',e=>{addFiles(e.target.files);e.target.value=''});id('msg').addEventListener('paste',e=>{if(e.clipboardData?.files?.length)addFiles(e.clipboardData.files)});['dragenter','dragover'].forEach(ev=>id('composer').addEventListener(ev,e=>{e.preventDefault();id('composer').classList.add('drop-hot')}));['dragleave','drop'].forEach(ev=>id('composer').addEventListener(ev,e=>{e.preventDefault();id('composer').classList.remove('drop-hot')}));id('composer').addEventListener('drop',e=>{if(e.dataTransfer?.files?.length)addFiles(e.dataTransfer.files)});
(async()=>{await loadSettings();await loadChats();await fetchModels();active=chats[0]?.id||null;if(!active)newChat();renderList();renderChat();stats();setInterval(stats,4000);if(settings.voice.listen)setTimeout(startListening,600)})();
</script>
</body></html>"""


class YamiHandler(http.server.BaseHTTPRequestHandler):
    server_version = "YamiPortable/1.0"

    def log_message(self, fmt, *args):
        print("[%s] %s" % (time.strftime("%H:%M:%S"), fmt % args))

    def cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def send_json(self, code, payload):
        body = json_bytes(payload)
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.cors()
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, code, text, content_type="text/plain; charset=utf-8"):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.cors()
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        return self.rfile.read(int(self.headers.get("Content-Length", "0") or "0"))

    def read_payload(self):
        raw = self.read_body()
        return json.loads(raw.decode("utf-8") if raw else "{}")

    def do_OPTIONS(self):
        self.send_response(204)
        self.cors()
        self.end_headers()

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                self.send_text(200, HTML, "text/html; charset=utf-8")
            elif path == "/api/settings":
                self.send_json(200, deep_merge(DEFAULT_SETTINGS, read_json(SETTINGS_FILE, {})))
            elif path == "/api/chats":
                self.send_json(200, read_json(CHATS_FILE, []))
            elif path == "/api/models":
                self.get_models()
            elif path == "/api/stats":
                self.get_stats()
            elif path == "/api/files":
                self.list_files()
            elif path == "/api/file":
                self.read_file()
            else:
                self.serve_static(path)
        except Exception as exc:
            self.send_json(500, {"error": str(exc)})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/api/settings":
                write_json(SETTINGS_FILE, deep_merge(DEFAULT_SETTINGS, self.read_payload()))
                self.send_json(200, {"ok": True})
            elif path == "/api/chats":
                payload = self.read_payload()
                write_json(CHATS_FILE, payload if isinstance(payload, list) else [])
                self.send_json(200, {"ok": True})
            elif path == "/api/chat":
                self.chat()
            elif path == "/api/provider/detect":
                self.detect_provider()
            elif path == "/api/attachment/parse":
                self.parse_attachment()
            elif path == "/api/import-model":
                self.import_model()
            elif path == "/api/file":
                self.write_file()
            elif path == "/api/yami/action":
                self.yami_action()
            else:
                self.send_json(404, {"error": "Not found"})
        except Exception as exc:
            self.send_json(500, {"error": str(exc)})

    def serve_static(self, path):
        full = safe_path(path.lstrip("/"))
        if not os.path.isfile(full):
            self.send_json(404, {"error": "Not found"})
            return
        mime = mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.cors()
        self.end_headers()
        self.wfile.write(body)

    def get_models(self):
        online = start_ollama()
        ollama_models = list_ollama_models() if online else []
        local = [{"name": name, "source": "ollama"} for name in ollama_models]
        known = {m["name"] for m in local}
        for gguf in list_gguf_files():
            if gguf["name"] not in known:
                local.append({"name": gguf["name"], "source": "gguf", "file": gguf["file"], "size": gguf["size"]})
        self.send_json(200, {
            "engineOnline": online,
            "ollamaAvailable": bool(find_ollama_exe()),
            "root": ROOT,
            "local": local,
            "gguf": list_gguf_files(),
        })

    def get_stats(self):
        cpu = 0.0
        ram = 0.0
        if os.name == "nt":
            try:
                import ctypes
                class MEMORYSTATUSEX(ctypes.Structure):
                    _fields_ = [
                        ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                    ]
                mem = MEMORYSTATUSEX()
                mem.dwLength = ctypes.sizeof(mem)
                ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem))
                ram = float(mem.dwMemoryLoad)
            except Exception:
                pass
        self.send_json(200, {"cpu": cpu, "ram": ram})

    def detect_provider(self):
        payload = self.read_payload()
        provider = (payload.get("apiProvider") or "openai").lower()
        if provider not in API_PRESETS:
            provider = "compatible"
        api_key = (payload.get("apiKey") or "").strip()
        if not api_key:
            self.send_json(400, {"ok": False, "error": "API key is required."})
            return
        base = normalize_api_base(payload.get("apiBaseUrl") or API_PRESETS[provider].get("baseUrl"))
        try:
            models = self.list_api_models(provider, base, api_key)
            model = choose_api_model(models, provider)
            self.send_json(200, {"ok": True, "apiProvider": provider, "apiBaseUrl": base, "apiModel": model, "models": models[:100]})
        except Exception as exc:
            fallback = API_PRESETS[provider].get("fallbackModel", "")
            self.send_json(200, {"ok": False, "apiProvider": provider, "apiBaseUrl": base, "apiModel": fallback, "error": str(exc)})

    def parse_attachment(self):
        payload = self.read_payload()
        name = os.path.basename(payload.get("name") or "arquivo")
        mime = payload.get("type") or mimetypes.guess_type(name)[0] or "application/octet-stream"
        raw = decode_attachment_data(payload)
        text, truncated = trim_attachment_text(extract_attachment_text(name, mime, raw))
        kind = "text" if text else ("image" if mime.startswith("image/") else "binary")
        self.send_json(200, {
            "ok": True,
            "name": name,
            "type": mime,
            "size": len(raw),
            "kind": kind,
            "text": text,
            "truncated": truncated,
        })

    def list_api_models(self, provider, base, api_key):
        try:
            data = request_json(base + "/models", headers={"Authorization": "Bearer " + api_key}, timeout=20, method="GET")
            raw = data.get("data", data.get("models", []))
            models = []
            for item in raw:
                if isinstance(item, str):
                    models.append({"id": item})
                elif isinstance(item, dict):
                    mid = item.get("id") or item.get("name") or item.get("model")
                    if mid:
                        models.append({"id": normalize_api_model(provider, mid)})
            return models
        except Exception as first_error:
            if provider != "gemini":
                raise first_error
            native = "https://generativelanguage.googleapis.com/v1beta/models?key=" + urllib.parse.quote(api_key)
            data = request_json(native, method="GET", timeout=20)
            models = []
            for item in data.get("models", []):
                name = item.get("name", "")
                if name.startswith("models/"):
                    name = name.split("/", 1)[1]
                if "generateContent" in item.get("supportedGenerationMethods", []):
                    models.append({"id": name})
            return models

    def send_ndjson_text(self, text):
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.cors()
        self.end_headers()
        if text:
            out = {"message": {"role": "assistant", "content": text}, "done": False}
            self.wfile.write(json_bytes(out) + b"\n")
        self.wfile.write(b'{"done":true}\n')
        self.wfile.flush()

    def chat_gemini_native(self, api_key, model, messages, temperature):
        system_parts = []
        contents = []
        for item in messages:
            role = (item.get("role") or "user").lower()
            content = item.get("content")
            parts = []
            if isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        parts.append({"text": str(part)})
                        continue
                    if part.get("type") == "text":
                        text = str(part.get("text") or "").strip()
                        if text:
                            parts.append({"text": text})
                    elif part.get("type") == "image_url":
                        url = (part.get("image_url") or {}).get("url", "")
                        if url.startswith("data:") and ";base64," in url:
                            header, data = url.split(",", 1)
                            mime = header[5:].split(";", 1)[0] or "image/png"
                            parts.append({"inline_data": {"mime_type": mime, "data": data}})
            else:
                content = str(content or "").strip()
                if content:
                    parts.append({"text": content})
            if not parts:
                continue
            if role == "system":
                system_parts.extend(part.get("text", "") for part in parts if part.get("text"))
                continue
            contents.append({
                "role": "model" if role == "assistant" else "user",
                "parts": parts,
            })
        if not contents:
            contents.append({"role": "user", "parts": [{"text": "Ola"}]})
        body = {
            "contents": contents,
            "generationConfig": {"temperature": temperature},
        }
        if system_parts:
            body["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}
        native_model = normalize_api_model("gemini", model)
        url = "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent?key=%s" % (
            urllib.parse.quote(native_model, safe=""),
            urllib.parse.quote(api_key),
        )
        data = request_json(url, body, timeout=600)
        parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        text = "".join(part.get("text", "") for part in parts if isinstance(part, dict)).strip()
        if not text:
            reason = data.get("candidates", [{}])[0].get("finishReason") or "resposta vazia"
            raise RuntimeError("Gemini retornou sem texto: " + str(reason))
        self.send_ndjson_text(text)

    def chat(self):
        payload = self.read_payload()
        mode = payload.get("mode", "local")
        if mode == "api":
            self.chat_api(payload)
        else:
            self.chat_local(payload)

    def chat_local(self, payload):
        if not start_ollama():
            self.send_json(502, {"error": "Ollama local nao encontrado. Coloque ollama-windows.exe em bin/ ou use API."})
            return
        model = payload.get("model") or payload.get("provider", {}).get("localModel")
        if not model:
            self.send_json(400, {"error": "Nenhum modelo local selecionado."})
            return
        body = {
            "model": model,
            "messages": payload.get("messages", []),
            "stream": True,
            "keep_alive": "10m",
            "options": {"temperature": payload.get("temperature", 0.7), "num_predict": 512},
        }
        self.stream_ollama("/api/chat", body)

    def chat_api(self, payload):
        provider_data = payload.get("provider") or {}
        api_key = (provider_data.get("apiKey", "") or "").strip()
        provider_name = (provider_data.get("apiProvider", "openai") or "openai").lower()
        if provider_name not in API_PRESETS:
            provider_name = "compatible"
        base = normalize_api_base(provider_data.get("apiBaseUrl") or API_PRESETS.get(provider_name, {}).get("baseUrl"))
        model = normalize_api_model(provider_name, payload.get("model") or provider_data.get("apiModel"))
        if model == "auto":
            model = ""
        if api_key and base and not model:
            try:
                model = choose_api_model(self.list_api_models(provider_name, base, api_key), provider_name)
            except Exception:
                model = API_PRESETS.get(provider_name, {}).get("fallbackModel", "")
        model = normalize_api_model(provider_name, model)
        if not api_key or not base or not model:
            self.send_json(400, {"error": "API key, URL e modelo sao obrigatorios."})
            return
        req_body = {
            "model": model,
            "messages": payload.get("messages", []),
            "stream": True,
            "temperature": payload.get("temperature", 0.7),
        }
        req = urllib.request.Request(
            base + "/chat/completions",
            data=json.dumps(req_body).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + api_key},
        )
        try:
            resp = urllib.request.urlopen(req, timeout=600)
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.cors()
            self.end_headers()
            buf = ""
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                buf += chunk.decode("utf-8", errors="ignore")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        self.wfile.write(b'{"done":true}\n')
                        self.wfile.flush()
                        return
                    try:
                        event = json.loads(data)
                        content = event.get("choices", [{}])[0].get("delta", {}).get("content", "")
                        if content:
                            out = {"message": {"role": "assistant", "content": content}, "done": False}
                            self.wfile.write(json_bytes(out) + b"\n")
                            self.wfile.flush()
                    except Exception:
                        pass
        except urllib.error.HTTPError as exc:
            if provider_name == "gemini":
                try:
                    self.chat_gemini_native(api_key, model, payload.get("messages", []), payload.get("temperature", 0.7))
                    return
                except urllib.error.HTTPError as native_exc:
                    self.send_json(native_exc.code, {"error": describe_http_error(native_exc)})
                    return
                except Exception as native_exc:
                    self.send_json(502, {"error": "Gemini API falhou: " + str(native_exc)})
                    return
            self.send_json(exc.code, {"error": describe_http_error(exc)})
        except Exception as exc:
            if provider_name == "gemini":
                try:
                    self.chat_gemini_native(api_key, model, payload.get("messages", []), payload.get("temperature", 0.7))
                    return
                except Exception as native_exc:
                    self.send_json(502, {"error": "Gemini API falhou: " + str(native_exc)})
                    return
            self.send_json(502, {"error": str(exc)})

    def stream_ollama(self, path, body):
        req = urllib.request.Request(OLLAMA_URL + path, data=json.dumps(body).encode("utf-8"), method="POST", headers={"Content-Type": "application/json"})
        try:
            resp = urllib.request.urlopen(req, timeout=600)
            self.send_response(resp.status)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.cors()
            self.end_headers()
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except urllib.error.HTTPError as exc:
            self.send_response(exc.code)
            self.cors()
            self.end_headers()
            self.wfile.write(exc.read())
        except Exception as exc:
            self.send_json(502, {"error": str(exc)})

    def import_model(self):
        if not start_ollama():
            self.send_json(502, {"ok": False, "error": "Ollama nao encontrado. Coloque ollama-windows.exe em bin/."})
            return
        ggufs = list_gguf_files()
        if not ggufs:
            self.send_json(404, {"ok": False, "error": "Nenhum .gguf encontrado em models/."})
            return
        imported = []
        exe = find_ollama_exe()
        env = os.environ.copy()
        env["OLLAMA_MODELS"] = OLLAMA_MODELS_DIR
        for gguf in ggufs:
            model_name = gguf["name"]
            if model_name in list_ollama_models():
                continue
            full = os.path.join(MODELS_DIR, gguf["file"])
            modelfile = os.path.join(DATA_DIR, "Modelfile-" + model_name)
            with open(modelfile, "w", encoding="utf-8") as f:
                f.write("FROM " + full.replace("\\", "/") + "\nPARAMETER temperature 0.7\n")
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            subprocess.check_call([exe, "create", model_name, "-f", modelfile], cwd=ROOT, env=env, creationflags=flags)
            imported.append(model_name)
        self.send_json(200, {"ok": True, "imported": imported, "message": "Modelos importados: " + (", ".join(imported) if imported else "todos ja estavam prontos")})

    def list_files(self):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        full = safe_path(query.get("path", ["."])[0])
        if not os.path.isdir(full):
            self.send_json(400, {"error": "Path is not a folder."})
            return
        items = []
        for entry in os.scandir(full):
            try:
                st = entry.stat()
                items.append({"name": entry.name, "path": rel_path(entry.path), "type": "dir" if entry.is_dir() else "file", "size": st.st_size})
            except OSError:
                pass
        items.sort(key=lambda item: (item["type"] != "dir", item["name"].lower()))
        parent = rel_path(os.path.dirname(full)) if os.path.abspath(full) != os.path.abspath(ROOT) else ""
        self.send_json(200, {"root": ROOT, "path": rel_path(full), "parent": parent, "items": items})

    def read_file(self):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        full = safe_path(query.get("path", [""])[0])
        if not os.path.isfile(full):
            self.send_json(404, {"error": "File not found."})
            return
        if os.path.getsize(full) > TEXT_MAX_BYTES:
            self.send_json(413, {"error": "File too large for built-in editor."})
            return
        raw = open(full, "rb").read()
        try:
            content = raw.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            content = raw.decode("latin-1", errors="replace")
            encoding = "latin-1"
        self.send_json(200, {"path": rel_path(full), "content": content, "encoding": encoding})

    def write_file(self):
        payload = self.read_payload()
        full = safe_path(payload.get("path", ""))
        content = payload.get("content", "")
        validate_text(content)
        backup = backup_file(full)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        self.send_json(200, {"ok": True, "path": rel_path(full), "backup": backup})

    def yami_action(self):
        payload = self.read_payload()
        action = (payload.get("action") or "").strip()
        path = payload.get("path", ".")
        if action == "web_search":
            self.send_json(200, {"ok": True, "action": action, **web_search(payload.get("query") or payload.get("q"), payload.get("limit", 5))})
            return
        if action == "web_fetch":
            self.send_json(200, {"ok": True, "action": action, **web_fetch(payload.get("url"), int(payload.get("maxChars", WEB_TEXT_CHARS) or WEB_TEXT_CHARS))})
            return
        if action == "list_dir":
            full = safe_path(path)
            if not os.path.isdir(full):
                self.send_json(400, {"ok": False, "error": "Path is not a folder."})
                return
            items = [{"name": e.name, "path": rel_path(e.path), "type": "dir" if e.is_dir() else "file"} for e in os.scandir(full)]
            self.send_json(200, {"ok": True, "action": action, "path": rel_path(full), "items": items})
            return
        if action == "read_file":
            full = safe_path(path)
            if not os.path.isfile(full):
                self.send_json(404, {"ok": False, "error": "File not found."})
                return
            raw = open(full, "rb").read(TEXT_MAX_BYTES + 1)
            if len(raw) > TEXT_MAX_BYTES:
                self.send_json(413, {"ok": False, "error": "File too large."})
                return
            self.send_json(200, {"ok": True, "action": action, "path": rel_path(full), "content": raw.decode("utf-8", errors="replace")})
            return
        full = safe_path(path)
        if action == "write_file":
            content = payload.get("content", "")
            validate_text(content)
            backup = backup_file(full)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            open(full, "w", encoding="utf-8", newline="").write(content)
            self.send_json(200, {"ok": True, "action": action, "path": rel_path(full), "backup": backup})
            return
        if action == "append_file":
            content = payload.get("content", "")
            validate_text(content)
            backup = backup_file(full)
            open(full, "a", encoding="utf-8", newline="").write(content)
            self.send_json(200, {"ok": True, "action": action, "path": rel_path(full), "backup": backup})
            return
        if action == "replace_in_file":
            old = payload.get("old", "")
            new = payload.get("new", "")
            content = open(full, "r", encoding="utf-8", errors="replace").read()
            if old not in content:
                self.send_json(409, {"ok": False, "error": "Old text not found."})
                return
            updated = content.replace(old, new, int(payload.get("count", 1) or 1))
            validate_text(updated)
            backup = backup_file(full)
            open(full, "w", encoding="utf-8", newline="").write(updated)
            self.send_json(200, {"ok": True, "action": action, "path": rel_path(full), "backup": backup})
            return
        self.send_json(400, {"ok": False, "error": "Unsupported action: " + action})


def validate_text(content):
    if not isinstance(content, str):
        raise ValueError("Content must be text.")
    if len(content.encode("utf-8")) > TEXT_MAX_BYTES:
        raise ValueError("Content is too large.")
    if content and content.count("\ufffd") / max(len(content), 1) > 0.05:
        raise ValueError("Content looks corrupted.")


def backup_file(full):
    if not os.path.isfile(full):
        return None
    dst = os.path.join(BACKUPS_DIR, time.strftime("%Y%m%d-%H%M%S") + "__" + rel_path(full).replace("/", "__") + ".bak")
    shutil.copy2(full, dst)
    return rel_path(dst)


class ThreadedServer(http.server.ThreadingHTTPServer):
    daemon_threads = True


def open_browser(port):
    time.sleep(0.8)
    webbrowser.open("http://127.0.0.1:%s" % port)


def main():
    ensure_dirs()
    start_ollama()
    port = find_port()
    print("=" * 60)
    print("  %s" % APP_NAME)
    print("=" * 60)
    print("  Pasta:      %s" % ROOT)
    print("  Interface:  http://127.0.0.1:%s" % port)
    print("  Local:      Ollama %s" % ("online" if ollama_online() else "offline/API disponivel"))
    print("  Feche esta janela para encerrar.")
    print("-" * 60)
    if "--no-browser" not in sys.argv:
        threading.Thread(target=open_browser, args=(port,), daemon=True).start()
    ThreadedServer(("127.0.0.1", port), YamiHandler).serve_forever()


if __name__ == "__main__":
    main()
