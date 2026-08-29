"""The two failures the kernel distinguishes."""


class ConfigError(RuntimeError):
    """The consumer's configuration or backlog cannot be used as written."""


class InfraError(RuntimeError):
    """The round could not be carried out; terminal state INFRA."""
