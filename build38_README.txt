NaibaChat build-38

This build integrates the PLAN4 Agent, vision, web search, MCP, and frontend
changes. It also keeps built-in Agent tool scopes enforced when a confirmed
Plan starts its Craft execution run.

It restores adjacent build35 configuration and conversations when the shared
data directory contains only an empty build36 database, without overwriting
non-empty current data.

Runtime data remains in %LOCALAPPDATA%\NaibaChat.
