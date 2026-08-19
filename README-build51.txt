naiba-chat Build51

Windows x64 package.

This build adds streamed reasoning events with automatic collapse, keeps reasoning
separate from answer text in the frontend, exposes tool activity in the chat UI,
adds a per-conversation deep-reasoning toggle, and removes the unfinished Plan
mode from the user-facing workflow. Existing Plan records are preserved as legacy
data, while conversations now run in normal craft mode.

The orchestration runtime can inventory tools, Skills, MCP servers, commands and
input paths, install a trusted local Skill package, and continue multi-step work
through jobs/sub-agents with verification and bounded retries. Local model calls
are serialized so Ollama, LM Studio, vision routing and sub-agents do not compete
for the same GPU/RAM at once.
