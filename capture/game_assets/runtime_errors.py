"""Shared fatal runtime-channel errors; independent of Frida and game state."""
from __future__ import annotations

import sys


class RuntimeBridgeUnavailable(RuntimeError):
    """The current Lua bridge session cannot execute further work reliably."""

    runtime_unavailable = True


def is_bridge_exception(error: BaseException) -> bool:
    """Recognize an unavailable transport, not an ordinary game RPC timeout."""
    if isinstance(error, RuntimeBridgeUnavailable):
        return True
    name, module = type(error).__name__, type(error).__module__
    if module.startswith(("frida", "_frida")):
        if name in {"InvalidOperationError", "TransportError", "ProcessNotFoundError",
                    "ServerNotRunningError", "PermissionDeniedError", "TimedOutError"}:
            return True
        if name == "RPCException" and any(marker in str(error).lower() for marker in (
                "script has been destroyed", "session is detached", "connection is closed",
                "connection terminated", "connection lost", "process terminated",
                "access violation accessing")):
            return True
        if name == "RPCException" and str(error).lower().startswith("error: system error"):
            return True
    return isinstance(error, RuntimeError) and str(error) == "runner is not attached"


# The CLI also supports executing individual scripts from this directory. Both
# import routes must share one exception class so circuit breakers cannot miss
# an error solely because a caller used the package route and another a script.
if __name__ == "runtime_errors":
    sys.modules.setdefault("capture.game_assets.runtime_errors", sys.modules[__name__])
elif __name__ == "capture.game_assets.runtime_errors":
    sys.modules.setdefault("runtime_errors", sys.modules[__name__])
