<p align="center">
  <img src="docs/assets/clawless_logo.png" alt="Clawless Logo" width="200">
  <br>
  <em>Clawless runs flawless!</em>
</p>

# Clawless

A minimal, restricted, memory-only agent framework for high-safety contexts — regulated enterprise assistants, education, and family companions.

The running agent can **never** modify its own code, configuration, or active skills. All persistent changes are either per-profile memory (facts, preferences stored as append-only JSONL) or human-approved skill proposals. Hence the name: *Clawless*.

## Architecture

<p align="center">
  <img src="docs/assets/Clawless_Architecture.png" alt="Clawless Architecture — Distributed Agent Trust System" width="700">
</p>

The architecture separates the **User Agent** (write-only: memory and skill proposals) from the **Execution Domain** (execute-only: approved skills and config). A **Review Interface** gates all transitions — skills must be explicitly approved (by a human, hybrid, or agent reviewer) before they can run. This enforces the core invariant: the running agent can never modify its own code.

## Roadmap

High-level implementation milestones derived from the [design document](docs/PROJECT.md).

### Foundation
- [ ] `config.py` — Pydantic settings, YAML loading, `CLAWLESS_DATA_DIR` resolution
- [ ] `types.py` — Session, Profile, MemoryEntry, ReviewDecision, CapabilityToken
- [ ] `utils/helpers.py` — path sandbox (`resolve_safe_write_path`), profile ID validation
- [ ] `config/default.yaml` — full config schema including `review` section
- [ ] Dockerfile — non-root user, read-only FS, `/data` volume

### Execution Domain (Execute Only)
- [ ] `skills/base.py` — BaseSkill with capability declarations, SkillRegistry (freezable), SkillDispatcher
- [ ] `config/skills_manifest.yaml` — skill allowlist, loaded once at startup
- [ ] Registry freeze — SkillRegistry becomes immutable after `main.py` startup completes

### User Agent Core (Write Only)
- [ ] `safety/guard.py` — blocklist, prompt injection detection, input/output length limits
- [ ] `memory/manager.py` — per-profile JSONL store, keyword retrieval (FAISS optional)
- [ ] `memory/extractor.py` — fact/preference extraction (regex + LLM)
- [ ] `llm/router.py` — abstract LLMProvider, OpenAI-compatible adapter, priority-based fallback
- [ ] `agent.py` — core loop: input → safety → memory → LLM → skill dispatch → memory → output
- [ ] `channels/text.py` — CLI stdin/stdout channel
- [ ] `main.py` — entry point, profile selection, component wiring, registry freeze

### Skill Proposal System
- [ ] `skills/proposer.py` — generate BaseSkill subclasses with capability declarations
- [ ] Proposal output to `/data/proposals/proposed_{name}_{ts}.py`
- [ ] Skill secrets bundled per approved skill (agent never sees raw keys)

### Review Interface (Gate)
- [ ] `review/analyzers.py` — AST scanner, import checker, capability validator
- [ ] `review/composition.py` — CompositionAnalyzer (rule-based escalation pair detection)
- [ ] `config/composition_rules.yaml` — default escalation pairs and thresholds
- [ ] `review/interface.py` — `clawless-review` CLI: list, analyze, approve, reject
- [ ] Review sidecar files — `.review.yaml` metadata per proposal
- [ ] Audit log — append-only `reviews.jsonl`
- [ ] Admin notification — email/webhook when a review decision is pending
- [ ] Wire `clawless-review` as separate `console_scripts` entry point in `pyproject.toml`

### Rejection & Feedback Loop
- [ ] Rejection metadata written to sidecar `.review.yaml` with reason
- [ ] Agent detects rejection and notifies user with reason
- [ ] Agent can revise and re-propose (new file, append-only)

### Voice Channel
- [ ] `channels/voice.py` — Vosk STT, Piper TTS, wake word detection
- [ ] Audio I/O via sounddevice + numpy

### Hardening & Production
- [ ] Hybrid review mode — automated checks gate human sign-off
- [ ] Agent review mode — LLM evaluation for pre-approved template categories
- [ ] Composition analysis against full approved skill set on every review
- [ ] Container deployment validation (read-only rootfs, non-root, single volume)

## License

MIT
