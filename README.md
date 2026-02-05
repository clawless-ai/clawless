<p align="center">
  <img src="docs/assets/clawless_logo.png" alt="Clawless Logo" width="200">
  <br>
  <em>Clawless runs flawless!</em>
</p>

# Clawless

A minimal, restricted, memory-only agent framework for high-safety contexts — regulated enterprise assistants, education, and family companions.

The running agent can **never** modify its own code, configuration, or active skills. All persistent changes are either per-profile memory (facts, preferences stored as append-only JSONL) or human-approved skill proposals. Hence the name: *Clawless*.

## Quick Start

```bash
# Clone and set up
git clone <repo-url> && cd clawless
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# Configure at least one LLM endpoint (see Configuration below)
# Then run:
clawless --profile default --channel text
```

## Requirements

- Python 3.11+
- At least one LLM endpoint (Ollama, OpenAI, any OpenAI-compatible API)

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate

# Core only
pip install -e .

# With voice support (Vosk STT + Piper TTS)
pip install -e ".[voice]"

# With FAISS semantic memory (heavier, not recommended for Pi 4)
pip install -e ".[faiss]"

# Development tools (pytest, ruff)
pip install -e ".[dev]"
```

## Configuration

Edit `config/default.yaml` or set environment variables with the `CLAWLESS_` prefix.

### LLM Endpoints

You must configure at least one LLM endpoint. The router tries endpoints in priority order (lowest first) and falls back on failure.

```yaml
llm_endpoints:
  - name: "local-ollama"
    base_url: "http://localhost:11434/v1"
    model: "llama3.2"
    api_key: ""
    priority: 0
    timeout: 60.0
    max_tokens: 1024

  - name: "openai-fallback"
    base_url: "https://api.openai.com/v1"
    model: "gpt-4o-mini"
    api_key: "${OPENAI_API_KEY}"
    priority: 10
    timeout: 30.0
    max_tokens: 1024
```

Any OpenAI-compatible API works: Ollama, llama.cpp, vLLM, LM Studio, LocalAI, OpenAI, etc.

### Key Settings

| Setting | Default | Description |
|---|---|---|
| `data_dir` | `./data` | Root directory for all writable state |
| `default_profile` | `default` | Profile used when none is specified |
| `memory.backend` | `keyword` | `keyword` (lightweight) or `faiss` (semantic) |
| `memory.retrieval_top_k` | `5` | Number of memories to retrieve per query |
| `safety.max_input_length` | `4096` | Maximum input character length |
| `safety.blocklist_file` | `""` | Path to a blocklist file (one term per line) |
| `voice.enabled` | `false` | Enable voice channel |

Override any setting via environment: `CLAWLESS_DATA_DIR=/tmp/test clawless`

## CLI Usage

```
clawless [options]

Options:
  -p, --profile    Profile ID (default: from config)
  -c, --channel    text or voice (default: text)
  --config         Path to YAML config file
  --data-dir       Override data directory
  --log-level      DEBUG, INFO, WARNING, or ERROR
```

## Architecture

```
User Input
  → Safety Guard (blocklist, prompt injection detection, length limits)
  → Memory Retrieval (keyword match against profile's stored facts/preferences)
  → LLM Router (priority-based fallback across configured endpoints)
  → Skill Dispatch (if a registered skill's trigger phrase matches)
  → Safety Guard (output check)
  → Memory Extraction (auto-extract facts/preferences from the conversation)
  → Response
```

### Directory Layout

```
src/                             # maps to `clawless` package via package-dir
├── main.py              # Entry point, CLI args, component wiring
├── agent.py             # Core loop orchestrator
├── config.py            # Pydantic settings + YAML loading
├── types.py             # Message, Session, Profile, MemoryEntry
├── channels/
│   ├── base.py          # Abstract channel interface
│   └── text.py          # CLI stdin/stdout channel
├── llm/
│   └── router.py        # Abstract LLMProvider + OpenAI-compatible adapter
├── memory/
│   ├── manager.py       # Per-profile JSONL store + keyword retrieval
│   └── extractor.py     # Fact/preference extraction (regex + LLM prompt)
├── safety/
│   └── guard.py         # Blocklist, prompt injection, system prompt
├── skills/
│   ├── base.py          # BaseSkill, BaseTool, SkillRegistry, manifest loader
│   └── proposer.py      # Skill code generation + AST static analysis
└── utils/
    └── helpers.py       # Path sandbox — all writes validated here
```

## Security Model

1. **Write sandbox** — The agent can only write to `profiles/` and `proposals/` under the data directory. Every write goes through `utils/helpers.py:resolve_safe_write_path()`.
2. **No dynamic code execution** — No `eval`, `exec`, `subprocess`, or runtime `importlib` (skills are loaded once at startup via a manifest allowlist).
3. **Read-only code** — Code, config, and enabled skills are immutable after startup.
4. **Human-gated skills** — The skill proposer writes proposals to `data/proposals/`. A human must review, copy to `skills/enabled/`, update `skills_manifest.yaml`, and restart.
5. **Profile isolation** — Each profile's memory is stored in its own directory. Profile IDs are validated (alphanumeric, hyphens, underscores only).

## Skills

Skills are loaded at startup from `config/skills_manifest.yaml`:

```yaml
skills:
  - module: "my_custom_skill"
    class: "MySkill"
```

The built-in **Skill Proposer** is registered automatically. Say "create a skill to..." and the agent will generate a proposal file with static analysis warnings for human review.

## Docker (Production)

```bash
docker build -t clawless .
docker run --read-only --tmpfs /tmp -v clawless_data:/data -it clawless
```

The container runs as a non-root user with a read-only filesystem. Only `/data` is writable (mounted volume). This enforces the security model at the OS level.

## License

MIT
