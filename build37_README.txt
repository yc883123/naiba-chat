NaibaChat build-37

This build restores legacy build35 configuration and conversations when the
build36 data directory exists but is empty, while preserving non-empty target
data. Runtime data remains in %LOCALAPPDATA%\NaibaChat.
