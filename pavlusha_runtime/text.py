def _trim(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    keep = max(1, limit // 2)
    omitted = len(text) - 2 * keep
    return text[:keep] + f"\n... <{omitted} chars omitted> ...\n" + text[-keep:]
