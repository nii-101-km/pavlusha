class AgentError(RuntimeError):
    pass


class ProcessCleanupError(AgentError):
    """Owned processes may remain alive; model restoration must not proceed."""
