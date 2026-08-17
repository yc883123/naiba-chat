NaibaChat build-42

1. Extract the ZIP archive.
2. Run naiba-chat.exe.
3. Existing configuration and conversations remain in %LOCALAPPDATA%\NaibaChat.

Build 42 includes the Plan/Ask/Craft frontend state synchronization fix:
stale plan polling responses can no longer restore old edit/execute actions,
and the plan action bar is cleared immediately when execution finishes or the
conversation switches away from Plan mode.
