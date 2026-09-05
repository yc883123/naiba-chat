from __future__ import annotations

from naiba.core.contracts import RunContext

import hashlib
import concurrent.futures
import json
import logging
import os
import re
import shutil
import zipfile
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable

import net_io
from mcp_runtime import MCPRegistry
from naiba.core.diagnostics import _cache_debug_enabled, _debug_message_digest
from naiba.core.history import _vision_read_folder_model_summary, encode_image_for_model
from naiba.core.exceptions import TaskCancelled
from naiba.tools.executor import ToolExecutor
from naiba.skills.agent import SKILL_CONTENT_WARN_CHARS, SKILL_PROMPT_HEADER, SkillAgent
from naiba.skills.context import DEFAULT_CONTEXT_WINDOW
from naiba.skills.policy import SKILL_POLICY_MODES, normalize_skill_policy
from naiba.skills.catalog import SkillCatalog, _frontmatter_value, _skill_display_name
from naiba.skills.install import (
    MAX_FILE_COUNT, MAX_TOTAL_SIZE, MAX_UNCOMPRESSED_ENTRY, ZIP_BOMB_RATIO,
    _SkillInstallError, _finalize_install, _folder_has_skill_md, _install_folder,
    _install_single_md, _install_zip, _path_within, _unique_dir, _zip_has_skill_md,
    delete_skill, remove_skill_references, validate_and_extract_archive, validate_and_install_skill,
)

logger = logging.getLogger("naiba.skill_runtime")


EventCallback = Callable[[dict[str, Any]], None]

SKILL_POLICY_MODES = {"auto", "pinned", "exclusive"}



# Conservative context ceiling (tokens) used when a provider exposes no window
# (e.g. DeepSeek's /v1/models returns no context-length field, so auto-detection
# yields 0). Rather than silently truncating history — which both drops context
# and re-breaks DeepSeek's token-prefix cache every turn — a conversation is
# blocked with a user-visible notice once it reaches this bound.


