NaibaChat build-41

1. Extract the ZIP archive.
2. Run naiba-chat.exe.
3. Existing configuration and conversations remain in %LOCALAPPDATA%\NaibaChat.

This build completes the settings and runtime fixes for model providers,
vision routing, per-conversation search, Skills, MCP status, Agents, and data
migration. It also prevents tool-call protocol text and repeated no-progress
tool calls from flooding the conversation.

The composer now reports per-turn token usage and a trustworthy context limit.
DeepSeek's official API uses its 128K capability; unknown online APIs show an
unknown limit instead of 8192. Ollama and LM Studio use independent local
context settings sent as num_ctx and context_length respectively.
