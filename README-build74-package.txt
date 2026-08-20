naiba-chat Build74

Windows x64 portable package.

This build includes:

- Remove the fixed visual-call count and Run-wide visual deadline.
- Give each automatic or tool-driven visual request an independent 180-second timeout.
- Keep exact-request caching and repeated/no-progress tool-loop safeguards.
- Report the actual visual backend error instead of an ambiguous budget error.
- Clarify the per-request timeout setting in the visual settings panel.

Run naiba-chat.exe directly. Existing data is kept in the application's
configured data directory.
