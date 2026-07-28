import os

from keyrings.cryptfile.cryptfile import CryptFileKeyring


class Keyring(CryptFileKeyring):
    priority = 10

    def __init__(self):
        super().__init__()
        key = os.environ.get("BETTER_AGENT_HEADLESS_KEYRING_KEY", "")
        if not key:
            raise RuntimeError("BETTER_AGENT_HEADLESS_KEYRING_KEY is required")
        self.keyring_key = key
