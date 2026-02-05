# Clawless Design Document – February 2026

## Project Overview
Clawless is a minimal, restricted, memory-only agent framework designed for high-safety contexts (regulated corporate/enterprise assistants, children/family companions, education). It supports voice and text interaction, persistent personalization via isolated memory writes, and controlled extensibility via a skill-proposer system that requires explicit human approval.

The framework is intentionally limited: the running agent can **never** modify its own code, configuration, or active skills (therefore "clawless"). All persistent changes are either per-profile memory (facts, preferences) or human-approved skill proposals.

## Core Security & Safety Invariants (non-negotiable)
1. The running agent process may **only write** to locations inside a single configurable data directory (`CLAWLESS_DATA_DIR`, default `/data` in containers).
   - Allowed subpaths:
     - `/data/profiles/{profile_id}/memory/` (facts.jsonl, preferences.yaml, FAISS index, logs)
     - `/data/profiles/{profile_id}/sessions/` (optional session transcripts)
     - `/data/proposals/` (agent-generated skill proposal files)
   - All other paths are **strictly forbidden** — raise exception on any attempt.
2. No `eval`, `exec`, `compile`, runtime `importlib.import_module`, `subprocess`, `os.system`, or dynamic code loading.
3. Code, configuration files, and enabled skills are **read-only** after container startup.
4. Skill activation requires **explicit human action** (copy from `/data/proposals/` to `skills/enabled/` and restart/reload).
5. Run as non-root user, read-only root filesystem, single writable volume in production/container deployments.

## Deployment & Containment Model
- **Local development**: Code and data can live together; `CLAWLESS_DATA_DIR` defaults to `./data` if not set.
- **Production / business servers / containers**: 
  - Code is immutable (in the container image).
  - All persistent state lives in a single mounted volume at `/data` (or env-defined path).
  - Dockerfile must:
    - Create non-root user
    - `mkdir -p /data && chown appuser:appuser /data`
    - `VOLUME /data`
    - Run with `--read-only --tmpfs /tmp`
    - Set `CLAWLESS_DATA_DIR=/data`
- This follows 2026 best practices (Docker, Kubernetes security contexts, OWASP, CIS benchmarks).

## Folder Structure (in source / container image)

/                              # project root
├── src/                       # core package (maps to `clawless` via package-dir)
│   ├── __init__.py
│   ├── main.py                # entry point + CLI args + channel selection
│   ├── config.py              # pydantic settings + load default.yaml
│   ├── types.py               # dataclasses: Session, Profile, BaseSkill, BaseTool
│   ├── agent.py               # core loop: input → safety → llm → skills → memory → output
│   │
│   ├── memory/
│   │   ├── manager.py         # per-profile short/long-term memory (jsonl + FAISS)
│   │   └── extractor.py       # fact/preference extraction
│   │
│   ├── channels/
│   │   ├── base.py
│   │   ├── voice.py           # wake → faster-whisper → Piper
│   │   └── text.py            # CLI / websocket / REST
│   │
│   ├── llm/
│   │   └── router.py          # Either local or cloud, or combination
│   │
│   ├── safety/
│   │   └── guard.py           # blocklist, prompt injection, system prompt template
│   │
│   ├── skills/
│   │   ├── base.py            # abstract BaseSkill + BaseTool
│   │   └── proposer.py        # built-in skill that generates new skill code → writes to proposals dir
│   │
│   └── utils/
│       └── helpers.py
│
├── config/
│   └── default.yaml           # LLM endpoints, voice models, default profiles, path overrides
│
├── Dockerfile                 # non-root, read-only FS, /data volume
├── requirements.txt
├── pyproject.toml
├── DESIGN.md                  # this file
└── README.md

**Runtime writable data directory** (outside image, mounted volume):
/data/
├── profiles/
│   └── {profile_id}/
│       ├── memory/            # facts.jsonl, preferences.yaml, faiss_index/
│       └── logs/              1
└── proposals/                 # agent-generated .py files (human review only)

## Tech Stack (minimal)
- Python 3.11+
- LLM: Configurable (Should support all models, and router functionality)
- Voice: Vosk(as Default)/Porcupine/ → faster-whisper/whisper.cpp → Piper
- Memory: sentence-transformers + FAISS + jsonl append
- Audio: sounddevice + numpy
- Config: pydantic + YAML
- Assumed default hardware: RaspberryPi 4 + ReSpeaker 2-Mic HAT

## MVP Functionality
- Dual channels (voice + text)
- Per-profile memory isolation
- LLM routing
- Safety guardrails (fixed system prompt + blocklist)
- Skill system with strict BaseSkill interface
- Skill proposer (generates complete .py files → writes only to `/data/proposals/`)
- Simple admin approval (CLI command or tiny FastAPI endpoint to list/copy proposals)

## Skill Proposer Details
- Trigger phrase: "create a skill to...", "learn how to..."
- Generates complete, template-conforming `BaseSkill` subclass
- Writes file to `/data/proposals/proposed_{name}_{timestamp}.py`
- Human must manually copy to `skills/enabled/` and restart/reload

## Claude Implementation Steps
Start building step-by-step. Keep files small (<300 lines where possible).

1. Create folder structure above
2. Add `Dockerfile` with non-root user, /data volume, read-only FS
3. Implement `config.py` + `default.yaml` + data dir resolution (`os.getenv("CLAWLESS_DATA_DIR", "./data")`)
4. Implement path validation utilities (ensure all writes go through checked functions)
5. `types.py` + `skills/base.py`
6. `skills/proposer.py` (generate + safe write to proposals dir)
7. `memory/manager.py` (jsonl append + FAISS basics, path-safe)
8. `safety/guard.py`
9. `agent.py` core loop
10. `channels/text.py` (simple CLI loop first)
11. `main.py` entry + profile selection

Prioritize safety checks and container-ready defaults.