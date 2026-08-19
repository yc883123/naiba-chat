naiba-chat Build60

Windows x64 package.

This build fixes local llama.cpp provider handling and local vision routing:

- Switching a legacy OpenAI-compatible llama.cpp endpoint to Local keeps the
  `/v1` protocol and selects the llama.cpp request format instead of silently
  switching to LM Studio endpoints.
- Model-list connection errors include the exact endpoint URL for diagnosing
  wrong ports and stopped local servers.
- Local vision calls no longer unload Ollama or LM Studio models implicitly;
  explicit unload remains available from the model controls.
