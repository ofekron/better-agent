def is_ollama_base_url(base_url: str) -> bool:
    value = (base_url or "").lower()
    return (
        "localhost:11434" in value
        or "127.0.0.1:11434" in value
        or "ollama" in value
    )
