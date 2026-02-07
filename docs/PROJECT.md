# Clawless Design Document – February 2026

## Project Overview
Clawless is a minimal, restricted, memory-only agent framework designed for high-safety contexts (regulated corporate/enterprise assistants, children/family companions, education). It supports voice and text interaction, persistent personalization via isolated memory writes, and controlled extensibility via a skill-proposer system gated by a formal review process.

The framework is intentionally limited: the running agent can **never** modify its own code, configuration, or active skills (therefore "clawless"). All persistent changes are either per-profile memory (facts, preferences) or reviewed and approved skill proposals.

The agent is designed to start off with minimalistic capabilites (pre-baked basic skills, such as cli-communication which allows him to communicate with the user). From there it can evolve through proposed skills.

The architecture follows a **Distributed Agent Trust System** with three zones — User Agent, Review Interface, and Execution Domain — each with strictly scoped capabilities.

## Distributed Trust Architecture

The system is divided into three trust zones. No zone can assume the responsibilities of another.

```
┌─────────────────────┐                         ┌─────────────────────┐
│   User Agent        │       Request           │  Execution Domain   │
│   (Write Only)      │ ─ ─ ─ ─ ─ ─             │  (Execute Only)     │
│                     │           |             │                     │
│  ┌───────────────┐  │    ┌───────────────┐    │  ┌───────────────┐  │
│  │ Proposed      │<─┼─ ─ │    Review     │─ ─>│  │  Approved     │  │
│  │ Skills        │  │ Rej│  Interface*   │Appr│  │  Skills       │  │
│  └───────────────┘  │    │               │    │  └───────────────┘  │
│  ┌───────────────┐  │    │ Human|Hybrid| │    │  ┌───────────────┐  │
│  │ Memory        │  │    │     Agent     │    │  │  Config       │  │
│  └───────────────┘  │    └───────────────┘    │  └───────────────┘  │
└─────────────────────┘            │            └─────────────────────┘
                         Invoke / execute approved skills
```

*Each review must evaluate skills in the context of all currently approved skills to prevent composition attacks.

### User Agent (Write Only)

The running agent process. It interacts with the user, retrieves memory, calls the LLM, and dispatches to approved skills. Despite being the main process, its write capabilities are strictly bounded:

- **Can write**: memory entries (JSONL append), session transcripts, skill proposals
- **Can read**: config, approved skills (via the frozen registry), profile memory
- **Can invoke**: skills already in the Execution Domain, through the SkillDispatcher
- **Cannot**: promote proposals, modify config, load new skills at runtime, write outside the data sandbox

The User Agent and Execution Domain share a single OS process for performance (minimal hardware requirements, such as Pi 4). The trust boundary is enforced by the SkillRegistry being frozen after startup and all writes going through the path sandbox. User Agent is only able to invoke approved skills without knowledge of their secret keys (Execute Only). Each approved skill should be bundled with their respective secrets.

### Review Interface (Gate)

A **separate entry point** (`clawless-review` CLI or admin API) that operates on the proposals directory. It is never reachable from within the running agent process. This is the only path from "proposed" to "approved."

Three reviewer modes:

**Human** (MVP default): The review tool presents the proposal code, AST analysis results, and a composition report. The reviewer approves or rejects with a reason.

**Hybrid**: Automated checks run first — AST analysis, import scanning, capability validation, composition analysis. Results are presented to the human reviewer alongside the code. If automated checks fail, the proposal is flagged with specific warnings. The human makes the final decision.

**Agent**: A separate LLM evaluation with a safety-focused system prompt. Restricted to skills matching pre-approved templates that don't declare high-risk capabilities. The agent reviewer's decisions are logged and auditable. Configurable via `review.auto_approve_categories` in config.

All three modes produce the same output: a review decision with metadata, written as a sidecar file and appended to the audit log.

Once a decision is ready to be made, the review service should be able to notify the admin e.g. via e-mail with instructions on how to proceed (reject or accept).

### Execution Domain (Execute Only)

Contains approved skills and configuration. Immutable at runtime — changes require a restart.

- **SkillRegistry**: Populated once at startup from `skills_manifest.yaml`. Frozen immediately after. The agent invokes skills through a `SkillDispatcher` that validates the skill is registered and the invocation parameters match the skill's declared interface.
- **Config**: Loaded once at startup. Read-only thereafter.

The Execution Domain cannot be written to by the agent process. New skills enter only through the Review Interface promoting proposals and a subsequent restart.

## Skill Capability Model

Each skill declares its capabilities in its class definition:

```python
class MySkill(BaseSkill):
    name = "my_skill"
    capabilities = ["memory:read", "network:read"]
    triggers = ["look up", "search for"]
```

Standard capability tokens:
- `memory:read`, `memory:write` — access to profile memory
- `network:read`, `network:write` — HTTP/external API access
- `file:read`, `file:write` — filesystem access (within sandbox)
- `llm:call` — ability to make LLM requests
- `user:output` — direct output to the user

Capabilities serve two purposes:
1. **Review-time validation**: AST analysis checks that the code only uses APIs consistent with its declared capabilities (e.g., a skill declaring only `memory:read` should not import `httpx`).
2. **Composition analysis**: The `CompositionAnalyzer` checks capability pairs across all approved skills for dangerous combinations.

## Composition Attack Prevention

Individually safe skills can become dangerous in combination. Example: Skill A declares `file:read`, Skill B declares `network:write`. Together, they enable data exfiltration if the agent chains their outputs.

The `CompositionAnalyzer` runs during review and checks:
- **Capability escalation pairs**: configurable rules mapping dangerous combinations (e.g., any `*:read` + `network:write` flags as potential exfiltration)
- **Data flow paths**: whether one skill's output type matches another skill's input type, creating implicit pipelines
- **Cumulative privilege**: whether the combined capability set of all approved skills exceeds a configured threshold

Rules are defined in `config/composition_rules.yaml`:

```yaml
escalation_pairs:
  - [["file:read", "memory:read"], "network:write", "Potential data exfiltration"]
  - [["network:read"], "file:write", "Potential payload drop"]

max_total_capabilities: 8
```

This is a lightweight rule-based check — no ML, runs instantly on Pi 4.

## Core Security & Safety Invariants (non-negotiable)

1. The running agent process may **only write** to locations inside a single configurable data directory (`CLAWLESS_DATA_DIR`, default `/data` in containers).
   - Allowed subpaths:
     - `/data/profiles/{profile_id}/memory/` (facts.jsonl, preferences.jsonl)
     - `/data/profiles/{profile_id}/sessions/` (optional session transcripts)
     - `/data/proposals/` (agent-generated skill proposals and review metadata)
   - All other paths are **strictly forbidden** — raise exception on any attempt.
2. No `eval`, `exec`, `compile`, runtime `importlib.import_module`, `subprocess`, `os.system`, or dynamic code loading.
3. Code, configuration files, and enabled skills are **read-only** after startup. The SkillRegistry is frozen immediately after loading.
4. Skill activation requires passage through the Review Interface. The MVP requires explicit human approval. Hybrid and agent review modes may be enabled for specific skill categories with appropriate safeguards.
5. Run as non-root user, read-only root filesystem, single writable volume in production/container deployments.
6. Skill reviews must evaluate the proposed skill in the context of all currently approved skills to prevent composition attacks. The CompositionAnalyzer must pass before any approval.
7. The Review Interface is a separate entry point — never callable from within the running agent process.

## Skill Lifecycle

```
1. User asks agent to learn a new capability
       ↓
2. Agent's SkillProposer generates a BaseSkill subclass
   with capability declarations → writes to /data/proposals/
       ↓
3. Reviewer invokes `clawless-review list` (separate process)
       ↓
4. Review Interface runs automated analysis:
   - AST scan (no forbidden imports/calls)
   - Capability validation (declared caps match code behavior)
   - Composition analysis (new caps + existing caps → safe?)
       ↓
5. Review decision (per configured mode):
   - Human: reviewer reads report + code, approves/rejects
   - Hybrid: auto-checks gate, human confirms
   - Agent: LLM evaluates (restricted categories only)
       ↓
6a. APPROVED → skill copied to skills/enabled/,
    manifest updated, restart required
       ↓
6b. REJECTED → sidecar .review.yaml written with reason,
    agent can query status and revise (new proposal file)
```

Rejected proposals remain in `/data/proposals/` with review metadata as an audit trail. The agent may generate a revised proposal (as a new file — never modifying the original), preserving the append-only principle. Once the user agent detects a skill rejection he should notify the user about the rejection including rejection reason.

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

```
/                              # project root
├── src/                       # core package (maps to `clawless` via package-dir)
│   ├── __init__.py
│   ├── main.py                # entry point + CLI args + channel selection
│   ├── config.py              # pydantic settings + load default.yaml
│   ├── types.py               # dataclasses: Session, Profile, MemoryEntry
│   ├── agent.py               # core loop: input → safety → llm → skills → memory → output
│   │
│   ├── memory/
│   │   ├── manager.py         # per-profile JSONL store + keyword/FAISS retrieval
│   │   └── extractor.py       # fact/preference extraction (regex + LLM)
│   │
│   ├── channels/
│   │   ├── base.py            # abstract channel interface
│   │   ├── voice.py           # wake → Vosk STT → Piper TTS
│   │   └── text.py            # CLI stdin/stdout
│   │
│   ├── llm/
│   │   └── router.py          # abstract LLMProvider + OpenAI-compatible adapter + fallback
│   │
│   ├── safety/
│   │   └── guard.py           # blocklist, prompt injection detection, system prompt
│   │
│   ├── skills/
│   │   ├── base.py            # BaseSkill, BaseTool, SkillRegistry (freezable), SkillDispatcher
│   │   └── proposer.py        # generates BaseSkill subclasses → writes to proposals dir
│   │
│   ├── review/
│   │   ├── interface.py       # review CLI entry point: list, approve, reject, show
│   │   ├── analyzers.py       # AST scanner, import checker, capability validator
│   │   └── composition.py     # CompositionAnalyzer — rule-based escalation pair detection
│   │
│   └── utils/
│       └── helpers.py         # path sandbox — all writes validated here
│
├── config/
│   ├── default.yaml           # LLM endpoints, voice, safety, memory, review settings
│   ├── skills_manifest.yaml   # skill allowlist (loaded at startup, frozen)
│   └── composition_rules.yaml # capability escalation pairs + thresholds
│
├── Dockerfile                 # non-root, read-only FS, /data volume
├── pyproject.toml
└── README.md
```

**Runtime writable data directory** (outside image, mounted volume):

```
/data/
├── profiles/
│   └── {profile_id}/
│       ├── memory/            # facts.jsonl, preferences.jsonl
│       └── sessions/          # session transcripts
├── proposals/
│   ├── proposed_{name}_{ts}.py            # agent-generated skill code
│   └── proposed_{name}_{ts}.review.yaml   # review decision + metadata
└── audit/
    └── reviews.jsonl          # append-only log of all review decisions
```

## Tech Stack (minimal)
- Python 3.11+
- LLM: Configurable (any OpenAI-compatible API via abstract LLMProvider + adapter)
- Voice: Vosk (default STT) / Porcupine (wake word) → Piper (TTS)
- Memory: JSONL append + keyword matching (default) or sentence-transformers + FAISS (optional)
- Audio: sounddevice + numpy
- Config: Pydantic v2 + PyYAML
- HTTP: httpx (async-capable)
- Assumed default hardware: Raspberry Pi 4 + ReSpeaker 2-Mic HAT

## MVP Functionality
- Dual channels (voice + text)
- Per-profile memory isolation
- LLM routing with priority-based fallback
- Safety guardrails (system prompt + blocklist + prompt injection detection)
- Skill system with BaseSkill interface and capability declarations
- Skill proposer (generates complete .py files → writes only to `/data/proposals/`)
- Review Interface with human-only mode (MVP), extensible to hybrid/agent review
  - AST analysis and composition checks run in all modes
  - `clawless-review` CLI as the separate entry point
- Rejection flow with metadata and agent re-proposal capability

## Configuration Additions

The following config keys support the review system (in `default.yaml`):

```yaml
review:
  mode: "human"                    # "human", "hybrid", or "agent"
  auto_approve_categories: []      # skill categories agent review may approve
  require_ast_pass: true           # automated checks must pass before human review
  composition_rules: "config/composition_rules.yaml"
```

## Implementation Steps

Start building step-by-step. Keep files small (<300 lines where possible).

1. Create folder structure
2. Add `Dockerfile` with non-root user, /data volume, read-only FS
3. Implement `config.py` + `default.yaml` + data dir resolution
4. Implement path validation utilities (`utils/helpers.py` — all writes through sandbox)
5. `types.py` — Session, Profile, MemoryEntry, ReviewDecision
6. `skills/base.py` — BaseSkill with capability declarations, SkillRegistry (freezable), SkillDispatcher
7. `skills/proposer.py` — generate skill code with capability declarations → safe write to proposals dir
8. `review/analyzers.py` — AST scanner, import checker, capability validator
9. `review/composition.py` — CompositionAnalyzer with rule-based escalation detection
10. `review/interface.py` — review CLI: list, analyze, approve, reject
11. `config/composition_rules.yaml` — default escalation pair rules
12. `memory/manager.py` — JSONL append + keyword retrieval, path-safe
13. `memory/extractor.py` — fact/preference extraction
14. `safety/guard.py` — blocklist, prompt injection, system prompt
15. `agent.py` — core loop (input → safety → memory → LLM → skill dispatch → memory → output)
16. `channels/text.py` — CLI channel
17. `main.py` — entry point, profile selection, registry freeze
18. Wire `clawless-review` as a separate console_scripts entry point in pyproject.toml

Prioritize safety checks, trust boundaries, and container-ready defaults.
