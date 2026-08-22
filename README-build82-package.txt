Naiba Chat Build 82

This package contains the official Windows x64 executable for Build 82.

Highlights:
- Deep-reasoning OpenAI-compatible requests always pass assistant
  reasoning_content back to the provider.
- Tool steps and legacy assistant history without stored reasoning use an empty
  reasoning_content value instead of omitting the required field.
- Normal non-reasoning requests keep their previous payload format.
- The tracked official-comfy-mcp Skill is bundled as a default Skill.

The automatic updater verifies naiba-chat.exe against the SHA-256 value in
naiba-chat-update.json before installing it.
