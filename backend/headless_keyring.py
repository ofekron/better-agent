import os
from pathlib import Path

from keyrings.cryptfile.cryptfile import CryptFileKeyring


class Keyring(CryptFileKeyring):
    priority = 10

    def __init__(
        self,
        *,
        key: str | None = None,
        file_path: Path | None = None,
    ) -> None:
        super().__init__()
        key = key or os.environ.get("BETTER_AGENT_HEADLESS_KEYRING_KEY", "")
        if not key:
            raise RuntimeError("BETTER_AGENT_HEADLESS_KEYRING_KEY is required")
        if file_path is not None:
            if not file_path.is_absolute():
                raise ValueError("headless keyring path must be absolute")
            self.file_path = str(file_path)
        self.keyring_key = key
