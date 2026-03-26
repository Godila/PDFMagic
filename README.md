# PDFMagic — PDF Concept Parser

> **Automatically extract structured data from real estate PDF presentations and populate Excel comparison tables using Vision LLMs.**

PDFMagic is a self-hosted FastAPI service that parses PDF slide decks (concept presentations, feasibility studies, development briefs) and maps extracted values to a predefined Excel template — no manual copy-paste required.

---

## How It Works

```
┌─────────────────┐    ┌──────────────────────────┐    ┌──────────────────────┐
│   Upload PDF    │    │    LiteParse CLI           │    │   Vision LLM         │
│   + XLSX        │───►│  lit parse  → text         │───►│  (OpenRouter)        │
│   template      │    │  lit screenshot → PNG      │    │  extracts values     │
└─────────────────┘    └──────────────────────────┘    └──────────┬───────────┘
                                                                    │
                        ┌──────────────────────────────────────────▼───────────┐
                        │  Excel handler writes extracted values as new column  │
                        │  in the "параметры по концепциям" sheet               │
                        └──────────────────────────────────────────────────────┘
```

1. **Upload** a PDF presentation and an XLSX template via the web UI
2. **LiteParse** extracts raw text and renders page screenshots (PNG)
3. **Vision LLM** (via OpenRouter) receives text + images and extracts 37 parameters in a single structured JSON response
4. **Excel handler** writes the extracted column into your XLSX template
5. **Download** the populated `.xlsx` — ready for comparison

---

## Features

- **Web UI** — single-page interface, no frontend build step required
- **Real-time progress** via WebSocket (0 → 100% with status messages)
- **Cancel button** — gracefully stops PDF processing, OCR, and LLM requests mid-flight
- **Vision + text hybrid** — combines native PDF text with page screenshots for maximum accuracy
- **Batch image processing** — pages sent in batches of ≤ 20 images per LLM call
- **Model-agnostic** — any OpenRouter model (Gemini, Claude, GPT-4o, Qwen, etc.)
- **Prompt editor** — tweak the extraction prompt from the UI without redeployment
- **Docker-ready** — single image with Python + Node.js + LiteParse bundled

---

## Tech Stack

| Layer | Technology |
|---|---|
| **Web framework** | FastAPI + Uvicorn |
| **Real-time** | WebSocket (asyncio) |
| **PDF parsing** | LiteParse CLI (`@llamaindex/liteparse`) |
| **PDF rendering** | PDFium (via LiteParse) |
| **LLM** | OpenRouter API (default: `google/gemini-3-flash-preview`) |
| **Excel I/O** | openpyxl |
| **Image processing** | Pillow (PNG → JPEG resize, base64 encoding) |
| **Process management** | psutil (process tree kill for subprocess cancellation) |
| **Containerisation** | Docker + docker-compose |

---

## Prerequisites

- **Docker + docker-compose** (recommended) — or Python 3.10+ with Node.js 20+
- **OpenRouter API key** — get one at [openrouter.ai](https://openrouter.ai)

---

## Quick Start

### With Docker (recommended)

```bash
git clone https://github.com/Godila/PDFMagic.git
cd PDFMagic

# Create your environment file
cp .env.example .env
# Edit .env and set OPENROUTER_API_KEY

docker compose up -d
```

Open **http://localhost:8000** in your browser.

### Without Docker (local dev)

```bash
# Install Node.js 20+ first, then:
npm install -g @llamaindex/liteparse@1.3.0

# Python setup
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Configure
cp .env.example .env
# Set OPENROUTER_API_KEY in .env

# Run
python api.py
```

---

## Configuration

Create a `.env` file in the project root:

```env
# Required
OPENROUTER_API_KEY=sk-or-...

# Optional — defaults shown
OPENROUTER_MODEL=google/gemini-3-flash-preview
LIT_CMD=                          # Override lit binary path (auto-detected if empty)
```

### Supported models

Any model available on OpenRouter works. Recommended options:

| Model | Speed | Notes |
|---|---|---|
| `google/gemini-3-flash-preview` | ~30s | Default. Best speed/quality ratio |
| `google/gemini-3.1-pro-preview` | ~90s | Higher accuracy on complex layouts |
| `anthropic/claude-opus-4-5` | ~60s | Strong at structured extraction |
| `openai/gpt-4o` | ~45s | Reliable, widely supported |
| `qwen/qwen3.5-397b-a22b` | ~120s | Needs `max_tokens≥16384` for thinking |

---

## API Reference

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/upload` | Upload PDF + XLSX, returns `job_id` |
| `POST` | `/api/process/{job_id}` | Start extraction pipeline |
| `POST` | `/api/cancel/{job_id}` | Cancel running job |
| `WS` | `/api/ws/{job_id}` | WebSocket progress stream |
| `GET` | `/api/download/{job_id}` | Download populated `result.xlsx` |
| `GET` | `/api/prompt` | Get current system prompt |
| `POST` | `/api/prompt` | Update system prompt |

### WebSocket message format

```json
{
  "progress": 45,
  "status": "Sending images to LLM...",
  "done": false
}
```

---

## Excel Template Format

Your XLSX file must contain two sheets:

**Sheet: `Список параметров`**

| Column A | Column B |
|---|---|
| Row number | Parameter name |
| 1 | Project name |
| 2 | Location |
| ... | ... |

**Sheet: `параметры по концепциям`**

The service reads parameter names from `Список параметров` and writes extracted values as a new column in this sheet. The column header is taken from the PDF filename.

---

## Deployment on Dokploy

Dokploy uses Traefik as reverse proxy — no nginx config needed.

1. Push your code to GitHub
2. In Dokploy: **New Application → Docker Compose**
3. Connect your GitHub repository
4. Set environment variables via Dokploy UI:
   - `OPENROUTER_API_KEY`
   - `OPENROUTER_MODEL` (optional)
5. Deploy — Traefik will handle SSL and routing automatically

The `docker-compose.yml` exposes port `8000`. Map it to your domain in Dokploy's Domains tab.

---

## Project Structure

```
PDFMagic/
├── api.py                 # FastAPI app — REST endpoints + WebSocket
├── extractor.py           # LiteParse CLI wrapper (text + screenshots)
├── llm_extractor.py       # OpenRouter LLM extraction logic
├── excel_handler.py       # openpyxl read/write for XLSX template
├── main.py                # CLI entry point
├── static/
│   └── index.html         # Single-file frontend (vanilla JS, no deps)
├── Dockerfile             # Python 3.12 + Node.js 20 + LiteParse
├── docker-compose.yml     # Compose with persistent jobs volume
├── requirements.txt
└── TECHNICAL_OVERVIEW.md  # In-depth technical documentation (RU)
```

---

## Extraction Pipeline

```
POST /api/upload
        │
        ▼
POST /api/process/{job_id}
        │
        ├─ [5%]  Read parameter list from XLSX
        ├─ [15%] lit parse  → extract native text
        ├─ [35%] lit screenshot → render pages to PNG
        │        Pillow: resize to max 1024px, JPEG quality 85, base64
        ├─ [40%] Batch images (≤20/batch) + full text → OpenRouter LLM
        ├─ [85%] Parse LLM JSON response → map to parameter rows
        ├─ [95%] Write new column to XLSX
        └─ [100%] Done → result.xlsx available for download
```

### Cancellation flow

```
POST /api/cancel/{job_id}
        │
        ├─ asyncio task.cancel()          ← stops async coroutine
        └─ threading.Event.set()          ← signals blocking threads
                │
                ├─ extractor.py checks event before/after subprocess
                ├─ psutil kill_tree() terminates node.exe process tree
                └─ llm_extractor.py checks event before each LLM batch
```

---

## Extraction Quality

Benchmarked against a reference dataset (36 parameters, residential development concept):

| Metric | Result |
|---|---|
| Exact match | ~69% (25/36) |
| Semantic match (normalised) | ~90% |

Remaining differences are structural ambiguities in source documents (e.g. per-corpus vs. total GFA, parking breakdown vs. total count) — not model errors.

**Default model:** `google/gemini-3-flash-preview`
**Typical processing time:** ~30 seconds per PDF

---

## Known Limitations

- **`shell=True` on Linux** — when calling LiteParse via subprocess from Python, always use `shell=False` (or `shell=os.name == 'nt'`). On Linux, `shell=True` with a list silently ignores all arguments beyond the first, causing `lit` to print help and exit with code 1.
- **Scanned PDFs** — quality depends on Tesseract OCR accuracy. For CJK or handwriting, configure an external EasyOCR / PaddleOCR server.
- **Very large PDFs** (100+ pages) — increase the memory limit in `docker-compose.yml` and use `--target-pages` or batch by page range.
- **Thinking models** (e.g. Qwen3) — require `max_tokens ≥ 16384` to avoid reasoning tokens consuming the entire budget with no output.

---

## License

MIT

---

## Acknowledgements

- [LiteParse](https://github.com/run-llama/liteparse) by LlamaIndex — local, privacy-first PDF parsing with spatial bounding boxes
- [OpenRouter](https://openrouter.ai) — unified LLM gateway
- [FastAPI](https://fastapi.tiangolo.com) — modern Python web framework
