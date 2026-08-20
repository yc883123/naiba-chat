naiba-chat Build73

Windows x64 portable package.

This build includes:

- Remove provisional stream fragments when the model proceeds to a tool call.
- Do not render empty Thinking blocks across multi-step tool runs.
- Show elapsed time for visual tools such as vision_describe.
- Add cleanup for finished async-task records.
- Add double-confirmed cleanup of the current conversation's messages and tool history.

Run naiba-chat.exe directly. Existing data is kept in the application's
configured data directory.
