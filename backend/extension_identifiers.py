from __future__ import annotations

import re


EXTENSION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,79}$")
EXTENSION_SETTING_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
