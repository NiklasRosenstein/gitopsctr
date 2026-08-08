"""Shared application-level failures."""


class OperationError(RuntimeError):
    pass


class ReferenceUnavailable(OperationError):
    pass
