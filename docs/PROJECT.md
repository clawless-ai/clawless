# Clawless Design Document – February 2026

## Project Overview

Clawless is a minimal, safety-first agent framework designed for high-safety contexts (regulated enterprise assistants, children/family companions, education). The running agent can **never** modify its own code, configuration, or active skills (therefore "clawless").

The agent starts with minimal capabilities — just enough to communicate with the user and propose new skills. From there it evolves: users ask the agent to learn new capabilities, which are formalized into structured proposals, implemented by a privileged admin service, and activated only after human approval.

## Two-Agent Trust Architecture

The system is split into two agents with different privilege levels, connected by structured YAML proposals. The user agent never generates code and never sees system implementation details.

```
User ──→ [User Agent]  ──→  YAML proposal  ──→  [Admin Service]  ──→  [Human Admin]
          low privilege       structured spec      has system access      final gate
          no code gen         no code              implements + analyzes  approve/reject
          user-facing         schema-validated      NOT user-facing
```

### User Agent (low privilege, user-facing)

The user-facing process. All behavior — including communication itself — lives in skills dispatched by a minimal kernel.

- **Knows**: abstract system capabilities only ("audio_input available", not device paths or package versions)
- **Can write**: memory entries (JSONL append), skill proposals (YAML to `data/proposals/`)
- **Can read**: config, system profile (abstract), active skills via frozen registry
- **Cannot**: generate code, access OS, run commands, discover system details, modify config or skills
- **Attack surface**: user input → but even a fully compromised agent can only produce YAML specs

### Admin Service (privileged, not user-facing)

A separate process (`cl-admin`) that runs as a continuous loop, polling for new proposals and driving them through a stateful pipeline.

- **Has**: full system access — installed packages, device paths, skill interface code, existing implementations
- **Processes**: validated YAML proposals only (never arbitrary user input)
- **Generates**: Python skill implementations from structured specs
- **Runs**: AST safety analysis, composition analysis against active skills
- **Presents**: full package (proposal + implementation + analysis) to human admin
- **On approval**: installs skill to manifest, signals restart needed

### Human Admin

Reviews the admin service's output (spec + code + analysis report). Approves or rejects. Only gate that can activate skills.

## "Everything Is a Skill"

The core agent is reduced to a minimal kernel. All user-facing behavior lives in skills:

- **CLI communication** — pure I/O: reading stdin, printing responses
- **Reasoning** — conversation pipeline: prompt building, LLM calls, intent routing, capability gap detection
- **Memory** — storing and retrieving facts about the user
- **Skill proposer** — formalizing user requests into YAML proposals

The kernel provides only: event dispatch, capability enforcement, safety filtering, path sandboxing, and LLM routing. It does not contain conversation logic, I/O, memory, persona loading, or prompt building.

### Core Skills (4 builtin)

**`cli`** — Pure I/O driver. Owns the stdin/stdout event loop. Dispatches `user_input` events to the reasoning skill and prints the returned responses. A future voice skill would replace this with audio I/O.
- Capabilities: `user:input`, `user:output`

**`reasoning`** — Conversation pipeline and intent routing. Handles `user_input` events: assembles context (memory, persona, skill descriptions), calls the LLM, parses the response for action directives, dispatches skill events, and manages memory storage. The LLM is made aware of available skills and can detect capability gaps — offering to propose new skills in any language.
- Capabilities: `llm:call`, `memory:read`, `memory:write`

**`memory`** — Handles memory storage and retrieval. Wraps JSONL manager and LLM/regex fact extraction. Runs extraction in background thread.
- Capabilities: `memory:read`, `memory:write`, `llm:call`

**`proposer`** — Triggered via the reasoning skill when the LLM detects a skill proposal intent. Consults abstract system profile for feasibility. Generates structured YAML proposals — never code.
- Capabilities: `llm:call`, `file:write`

### Graceful Degradation

If the `memory` skill is not loaded, the reasoning skill still works — it just gets no memory context. The kernel returns `None` for unhandled events and the reasoning skill handles this gracefully.

## Minimal Kernel

The kernel (~80-100 lines) is the irreducible core — things that cannot be skills because they are prerequisites for skills to function.

**Contains:**
- **SkillRegistry** — loads skills from manifest at startup, freezes immediately
- **Dispatcher** — receives events, routes to matching skills, enforces capability checks
- **SafetyGuard** — non-negotiable input/output filtering, prompt injection detection
- **Path sandbox** — write enforcement to `profiles/` and `proposals/` subdirs only
- **LLM Router** — shared service for LLM calls (priority-based fallback)
- **KernelContext** — frozen read-only bag of shared services passed to all skills

### Boot Sequence

1. Load config + system profile (abstract capabilities only)
2. Load skills from manifest into registry, freeze
3. Validate: at least one skill with `user:input` capability exists
4. Call `on_load(ctx)` on all skills
5. Call `run(ctx)` on the communication driver skill

## Event-Based Skill Interface

Skills communicate through events dispatched via the kernel. No skill holds a direct reference to another.

### Event

```python
@dataclass
class Event:
    type: str          # "user_input", "memory_query", "skill_proposal", etc.
    payload: str       # primary content
    source: str        # producing skill name
    session_id: str
    profile_id: str
    metadata: dict
```

Standard event types (MVP):
- `user_input` — user said something (produced by communication skills)
- `memory_query` — retrieve relevant memories
- `memory_store` — store fact/preference/persona
- `skill_proposal` — user wants a new skill

New event types can be added by new skills without kernel changes.

### KernelContext

```python
@dataclass(frozen=True)
class KernelContext:
    llm: LLMRouter
    settings: Settings
    system_profile: SystemProfile   # abstract capabilities only
    dispatch: Callable[[Event], SkillResult | None]
    data_dir: Path
```

### BaseSkill

```python
class BaseSkill(ABC):
    name: str
    description: str
    version: str
    origin: SkillOrigin              # "builtin" or "proposed" (metadata only)
    capabilities: frozenset[str]     # declared capability tokens
    handles_events: list[str]        # event types this skill responds to
    dependencies: list[str]          # required skill names

    def on_load(self, ctx: KernelContext) -> None: ...
    def handle(self, event: Event, ctx: KernelContext) -> SkillResult | None: ...
    def on_unload(self) -> None: ...
    def run(self, ctx: KernelContext) -> None: ...  # for driver skills only
```

Skills interact **only** through `ctx.dispatch()` — never hold direct references to each other.

## Capability Tokens

The actual security enforcement mechanism. The kernel checks at runtime that a skill only dispatches events requiring capabilities it has declared.

```
user:input, user:output     — communication with user
memory:read, memory:write   — profile memory store
llm:call                    — invoke LLM router
file:write                  — write to data sandbox (proposals/ only)
audio:read, audio:write     — microphone/speaker
gpio:read, gpio:write       — hardware pins
network:read, network:write — outbound HTTP
```

### Skill Origin Labels (metadata only, no runtime enforcement)

```python
class SkillOrigin(Enum):
    BUILTIN = "builtin"      # ships with the project
    PROPOSED = "proposed"    # added through proposal → admin → human pipeline
```

Informational labels for human readability. The kernel does not treat skills differently based on origin. Security comes entirely from capability tokens + the admin review gate.

## System Knowledge Model

### User Agent sees (abstract only)

Generated once at install time by `clawless-setup`, stored as `config/system_profile.yaml`, frozen thereafter. The agent never runs system commands.

```yaml
platform: raspberry_pi_4
available_capabilities:
  - audio_input
  - audio_output
  - gpio
  - network
active_skills:
  - cli
  - memory
  - proposer
```

No package lists, no versions, no device paths, no file layout. Enough to say "this system can do audio" but not enough to write implementation code.

### Admin Service sees (full system context)

Internal to the admin service — never exposed to the user agent:

```yaml
python_version: "3.11.2"
installed_packages: [vosk, sounddevice, piper-tts, numpy, ...]
device_paths:
  audio_input: "/dev/snd/pcmC1D0c"
  audio_output: "/dev/snd/pcmC0D0p"
skill_interface: "BaseSkill from clawless.user.skills.base"
existing_skill_code: [...]
```

This separation means: even if the user agent is fully prompt-injected, the attacker learns "there's a Pi with audio" — not how to exploit it.

## Skill Proposal Format

The user agent generates YAML proposals only — no code. The proposal expresses *what* and *why*, never *how*.

```yaml
# data/proposals/proposed_voice_comm_20260207_143000.yaml
proposal:
  name: voice-communication
  description: "Communicate with the user via voice using local STT and TTS"
  capabilities:
    - user:input
    - user:output
    - audio:read
    - audio:write
  dependencies:
    - memory
  handles_events:
    - user_input
    - assistant_output
  requirements:
    system_capabilities:
      - audio_input
      - audio_output
  rationale: |
    The user asked the agent to learn voice communication. This skill would
    replace cli as the primary interface, enabling hands-free interaction
    using the available audio hardware.
  user_context: |
    User said: "I'd like to talk to you by voice instead of typing.
    I have a microphone attached to my Pi."
  generated_by: proposer
  generated_at: "2026-02-07T14:30:00Z"
  profile_id: default

# Status tracking — managed by admin service (user agent only writes status: new)
status: new
history:
  - timestamp: "2026-02-07T14:30:00Z"
    status: new
    actor: proposer
```

## Admin Service — Stateful Pipeline with Configurable Gates

The admin service (`cl-admin`) runs as a continuous loop, polling `data/proposals/` for new proposals and driving them through a status pipeline. Each status transition can be configured to require human approval or proceed automatically.

### Proposal Status Lifecycle

```
new → discovered → implementation → agent-review → human-review → accepted
                                                                → rejected
```

| Status | What happens | Default gate |
|--------|-------------|--------------|
| `new` | Proposal written by user agent | — |
| `discovered` | Admin service found it, validated YAML schema + capability tokens | auto |
| `implementation` | Admin service generates Python code from spec via LLM (full system context) | auto |
| `agent-review` | AST safety analysis + composition check against active skills | auto |
| `human-review` | Full package presented for human approval | **human** |
| `accepted` | Skill installed to manifest, restart signaled | — |
| `rejected` | Rejected at any stage, reason recorded in history | — |

Status is tracked directly in the proposal YAML file (`status` field + `history` log).

### Configurable Gates

Admin service config (`config/admin.yaml`) defines which transitions pause for human approval:

```yaml
poll_interval_seconds: 30
mode: interactive          # "interactive" (CLI prompts) or "headless" (notifications only)

gates:
  discovered: auto         # auto-validate schema and proceed
  implementation: auto     # auto-generate code and proceed
  agent-review: auto       # auto-run analysis, proceed if clean
  human-review: human      # always require human approval here

notifier: cli              # "cli" (MVP), "email", "webhook", etc.
```

### Notification Abstraction

Designed for extensibility, MVP implements CLI only:

```python
class Notifier(ABC):
    def notify(self, proposal: dict, status: str, message: str) -> None: ...
    def request_approval(self, proposal: dict, status: str, context: dict) -> bool: ...

class CLINotifier(Notifier):
    """MVP: prints summary to terminal, prompts for proceed/reject."""
```

Future: `EmailNotifier`, `WebhookNotifier`, etc.

### What the Human Sees (interactive mode)

```
═══════════════════════════════════════════════════
  SKILL PROPOSAL: voice-communication
═══════════════════════════════════════════════════

  PROPOSAL (user intent):
    Communicate via voice using local STT/TTS
    Capabilities: user:input, user:output, audio:read, audio:write
    Dependencies: memory

  GENERATED IMPLEMENTATION:
    src/user/skills/voice/skill.py (47 lines)
    [code preview]

  ANALYSIS:
    AST scan: CLEAN (no forbidden imports/builtins)
    Composition: WARNING — adds audio:write alongside existing memory:read
    Recommendation: APPROVE WITH NOTE

  [A]pprove  [R]eject  [V]iew full code  [Q]uit
═══════════════════════════════════════════════════
```

## Composition Attack Prevention

Individually safe skills can become dangerous in combination. Example: Skill A declares `file:read`, Skill B declares `network:write`. Together, they enable data exfiltration.

The `CompositionAnalyzer` (in the admin service) runs during review and checks:
- **Capability escalation pairs**: configurable rules mapping dangerous combinations
- **Cumulative privilege**: whether the combined capability set of all active skills exceeds a threshold

Rules defined in `config/composition_rules.yaml`:

```yaml
escalation_pairs:
  - [["file:read", "memory:read"], "network:write", "Potential data exfiltration"]
  - [["network:read"], "file:write", "Potential payload drop"]

max_total_capabilities: 8
```

Lightweight rule-based check — no ML, runs instantly on Pi 4.

## Core Security Invariants (non-negotiable)

1. The user agent process may **only write** to `profiles/` and `proposals/` subdirs within `CLAWLESS_DATA_DIR`. All writes go through path sandbox validation.
2. No `eval`, `exec`, `compile`, `subprocess`, `os.system`, or dynamic code loading in the user agent. The only `importlib.import_module` usage is for loading skills from the manifest at startup.
3. Code, configuration, and active skills are **read-only** after startup. The SkillRegistry is frozen immediately after loading.
4. Skill activation requires passage through the admin service pipeline. The MVP requires explicit human approval at the `human-review` gate.
5. Run as non-root user, read-only root filesystem, single writable volume in production/container deployments.
6. The admin service is a separate process — never callable from within the user agent process.
7. The user agent never generates code and never sees system implementation details (package lists, device paths, file layout).

## Package Layout

Trust boundary is visible in the directory structure. No single-file-in-a-folder packages. Every skill is a sub-package.

```
src/
├── user/                              # USER AGENT — low privilege, user-facing
│   ├── __init__.py
│   ├── main.py                        # entry point: cl-bot
│   ├── kernel.py                      # dispatcher, boot validation (~80 lines)
│   ├── types.py                       # Event, SkillOrigin, SystemProfile, KernelContext
│   ├── config.py                      # settings + system profile loading
│   ├── guard.py                       # input/output safety filtering
│   ├── sandbox.py                     # path write enforcement
│   ├── llm.py                         # LLM router + provider abstraction
│   └── skills/
│       ├── __init__.py
│       ├── base.py                    # BaseSkill, SkillRegistry, manifest loader
│       ├── cli/                       # CLI communication (pure I/O)
│       │   ├── __init__.py
│       │   └── skill.py
│       ├── reasoning/                 # Conversation pipeline + intent routing
│       │   ├── __init__.py
│       │   └── skill.py
│       ├── memory/                    # Memory skill
│       │   ├── __init__.py
│       │   ├── skill.py              # event handlers
│       │   ├── manager.py            # JSONL storage + keyword retrieval
│       │   └── extractor.py          # LLM/regex fact extraction
│       └── proposer/                  # Skill proposer
│           ├── __init__.py
│           └── skill.py              # YAML-only proposal generation
│
└── admin/                             # ADMIN SERVICE — privileged, not user-facing
    ├── __init__.py
    ├── main.py                        # entry point: cl-admin
    ├── service.py                     # pipeline loop, status transitions, gate checks
    ├── implementer.py                 # code generation from spec (has system access)
    ├── analyzer.py                    # AST analysis + composition checks
    └── notifier.py                    # Notifier ABC + CLINotifier

config/
├── default.yaml                       # LLM endpoints, safety, memory settings
├── admin.yaml                         # gate config, poll interval, mode, notifier
├── system_profile.yaml                # abstract capabilities (user agent)
├── skills_manifest.yaml               # skill allowlist (loaded at startup, frozen)
├── composition_rules.yaml             # capability escalation pairs + thresholds
└── persona.default.md                 # default persona template
```

**Import rule:** `admin` may import from `user` (needs skill interface, types). `user` must NEVER import from `admin`.

## Runtime Data Directory

```
data/
├── profiles/
│   └── {profile_id}/
│       ├── persona.md
│       ├── memory/
│       │   └── entries.jsonl
│       └── logs/
└── proposals/
    └── proposed_{name}_{ts}.yaml      # YAML proposals (no code)
```

## Tech Stack

- Python 3.11+
- LLM: Configurable (any OpenAI-compatible API via abstract LLMProvider + adapter)
- Voice: Vosk (STT) / Piper (TTS) — added as a skill via proposal pipeline
- Memory: JSONL append + keyword matching (default); FAISS optional
- Config: Pydantic v2 + PyYAML
- HTTP: httpx
- Target hardware: Raspberry Pi 4 + ReSpeaker 2-Mic HAT

## Deployment

- **Local development**: Code and data together; `CLAWLESS_DATA_DIR` defaults to `./data`
- **Production / containers**:
  - Code immutable (in container image)
  - Persistent state in single mounted volume at `/data`
  - Dockerfile: non-root user, `--read-only --tmpfs /tmp`, `VOLUME /data`
