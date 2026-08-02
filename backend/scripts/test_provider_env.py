from provider_env import is_ollama_base_url


class TestIsOllamaBaseUrl:
    def test_none_returns_false(self) -> None:
        assert is_ollama_base_url(None) is False

    def test_empty_returns_false(self) -> None:
        assert is_ollama_base_url("") is False

    def test_plain_non_ollama_url_returns_false(self) -> None:
        assert is_ollama_base_url("https://api.anthropic.com") is False

    def test_localhost_11434_substring(self) -> None:
        assert is_ollama_base_url("http://localhost:11434") is True

    def test_localhost_11434_inside_path(self) -> None:
        assert is_ollama_base_url("http://localhost:11434/v1/chat") is True

    def test_127_0_0_1_11434_substring(self) -> None:
        assert is_ollama_base_url("http://127.0.0.1:11434") is True

    def test_ollama_substring_anywhere(self) -> None:
        assert is_ollama_base_url("https://my-ollama-host:8080") is True

    def test_bare_ollama_word(self) -> None:
        assert is_ollama_base_url("ollama") is True

    def test_case_insensitive(self) -> None:
        assert is_ollama_base_url("HTTP://LOCALHOST:11434") is True
        assert is_ollama_base_url("OLLAMA") is True

    def test_port_only_11434_without_host_not_ollama(self) -> None:
        assert is_ollama_base_url("http://0.0.0.0:11434") is False
