"""Fail-closed artifact I/O for collectors which persist only to verified cloud.

Logical paths are object keys, never local paths. The backend must return
``{'verified': True}`` only after upload, fresh read-back and catalog commit.
Unconfirmed bytes remain on the adapter (and its fatal exception) in RAM.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path


class ArtifactStoreFailure(BaseException):
    """Abort broad capture retry handlers without acknowledging unsafe bytes."""

    def __init__(self, message, store):
        super().__init__(message)
        self.artifact_store = store
        self.pending_artifacts = store.pending_artifacts
        self.pending_states = store.pending_states


class VerifiedArtifactStore:
    def __init__(self, backend):
        self.backend = backend
        self.pending_artifacts = {}
        self.pending_states = {}
        self.failure = None

    def _live(self):
        if self.failure is not None:
            raise self.failure

    def _abort(self, operation, error=None):
        if self.failure is None:
            self.failure = ArtifactStoreFailure(
                f"Cloud artifact {operation} did not complete verified persistence; collection paused with pending bytes in RAM.", self)
        if error is not None:
            raise self.failure from error
        raise self.failure

    def _call(self, operation, *arguments):
        self._live()
        try:
            return getattr(self.backend, operation)(*arguments)
        except Exception as error:
            self._abort(operation, error)

    def _confirmed(self, operation, receipt):
        if not isinstance(receipt, dict) or receipt.get("verified") is not True:
            self._abort(operation)
        return receipt

    def exists(self, path):
        value = self._call("exists", Path(path))
        if not isinstance(value, bool):
            self._abort("exists")
        return value

    def read_bytes(self, path):
        raw = self._call("read_bytes", Path(path))
        if not isinstance(raw, bytes):
            self._abort("read_bytes")
        return raw

    def write_bytes(self, path, raw):
        if not isinstance(raw, bytes):
            raise TypeError("Cloud artifacts require immutable encoded bytes")
        key = str(Path(path))
        self.pending_artifacts[key] = raw
        receipt = self._confirmed("write_bytes", self._call("write_bytes", Path(path), raw))
        self.pending_artifacts.pop(key, None)
        return receipt

    def stage_bytes(self, path, raw):
        """Keep already received bytes before starting any fallible I/O."""
        if not isinstance(raw, bytes):
            raise TypeError("Cloud artifacts require immutable encoded bytes")
        self.pending_artifacts[str(Path(path))] = raw

    def discard_staged(self, path):
        self.pending_artifacts.pop(str(Path(path)), None)

    def write_json(self, path, value):
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        return self.write_bytes(path, raw)

    def load_state(self, namespace):
        value = self._call("load_state", namespace)
        if value is not None and not isinstance(value, dict):
            self._abort("load_state")
        return deepcopy(value)

    def save_state(self, namespace, value):
        snapshot = deepcopy(value)
        self.pending_states[namespace] = snapshot
        receipt = self._confirmed("save_state", self._call("save_state", namespace, snapshot))
        self.pending_states.pop(namespace, None)
        return receipt

    def retry_pending(self):
        """Explicitly retry RAM-only backlog; never dispatch game requests.

        Artifacts precede their restart-state acknowledgement. A failed retry
        freezes this adapter again and leaves all unconfirmed bytes available.
        """
        artifacts = list(self.pending_artifacts.items())
        states = [(namespace, deepcopy(value)) for namespace, value in self.pending_states.items()]
        self.failure = None
        try:
            for path, raw in artifacts:
                self.write_bytes(Path(path), raw)
            for namespace, value in states:
                self.save_state(namespace, value)
        except BaseException:
            if self.failure is None:
                self.failure = ArtifactStoreFailure("Cloud backlog retry interrupted; unconfirmed bytes remain in RAM.", self)
            raise
        return {"verified": True, "artifacts": len(artifacts), "states": len(states)}

    flush_pending = retry_pending


def verified_store(backend):
    return backend if backend is None or isinstance(backend, VerifiedArtifactStore) else VerifiedArtifactStore(backend)
