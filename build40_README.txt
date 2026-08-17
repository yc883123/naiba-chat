NaibaChat build-40

1. Extract the ZIP archive.
2. Run naiba-chat.exe.
3. Existing configuration and conversations remain in %LOCALAPPDATA%\NaibaChat.

This build fixes the vision request path for text-only models, including the
DeepSeek image_url HTTP 400 failure. It adds explicit image-input capabilities
and a real-image vision connection test with backend failover.

Approval mode is now stored per conversation and frozen into each chat or plan
Run. Concurrent Runs use isolated executors and confirmation queues. The mode
control is in the lower-left composer area, while Craft / Plan / Ask use a
compact selector.
