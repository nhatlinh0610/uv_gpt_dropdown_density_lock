"""Owned lifecycle for one persistent external pure-Python worker.

The runtime deliberately launches an exact helper file with an exact bundled
interpreter.  It does not use PATH lookup, an executor, a network socket or a
package-module launch.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
from typing import Any, Optional

try:
    from .pro_process_protocol import (
        Envelope,
        FutureMessageError,
        MessageType,
        ProtocolEOF,
        ProtocolError,
        StaleMessageError,
        validate_identity,
        write_message,
        read_message,
    )
except ImportError:  # direct-file test loading without package initialization
    from pro_process_protocol import (  # type: ignore[no-redef]
        Envelope,
        FutureMessageError,
        MessageType,
        ProtocolEOF,
        ProtocolError,
        StaleMessageError,
        validate_identity,
        write_message,
        read_message,
    )


THREAD_CAP_NAMES = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
)
DEFAULT_HANDSHAKE_TIMEOUT = 5.0
DEFAULT_IO_TIMEOUT = 5.0
DEFAULT_SHUTDOWN_TIMEOUT = 2.0
_VERSION_RE = re.compile(r"(?<!\d)(\d+)\.(\d+)")


class ProcessRuntimeError(RuntimeError):
    """Base class for helper discovery, protocol and lifecycle failures."""


class HelperUnavailableError(ProcessRuntimeError):
    """The exact bundled interpreter or worker script is unavailable."""


class HandshakeError(ProcessRuntimeError):
    """The helper did not complete a valid HELLO/READY exchange."""


class WorkerTimeoutError(ProcessRuntimeError):
    """A bounded lifecycle or I/O operation timed out."""


class WorkerCrashedError(ProcessRuntimeError):
    """The owned helper exited or closed its stream unexpectedly."""


class WorkerEOFError(WorkerCrashedError):
    """The owned helper ended stdout before the expected response."""


class WorkerProtocolError(ProcessRuntimeError):
    """The helper returned an unsafe or inconsistent frame."""


class ForeignFrameError(WorkerProtocolError):
    """A frame belongs to a different session or unknown task."""


class WorkerRemoteError(ProcessRuntimeError):
    """The helper reported a bounded task error."""


@dataclass(frozen=True)
class BundledPythonInfo:
    executable: Path
    blender_root: Path
    version_dir: str


@dataclass(frozen=True)
class OwnedProcess:
    process: Any
    pid: int
    executable: Path
    worker_script: Path
    session_nonce: str
    started_at: float


@dataclass(frozen=True)
class TaskTicket:
    batch_id: str
    sequence: int
    generation: int
    item_count: int


@dataclass(frozen=True)
class _StreamClosed:
    error: BaseException


@dataclass(frozen=True)
class _ReaderFailed:
    error: BaseException


def _normalise_version_dir(version: object) -> Optional[str]:
    if version is None:
        return None
    if isinstance(version, (tuple, list)) and len(version) >= 2:
        return f"{int(version[0])}.{int(version[1])}"
    match = _VERSION_RE.search(str(version))
    if match is None:
        raise HelperUnavailableError(f"invalid Blender version context: {version!r}")
    return f"{match.group(1)}.{match.group(2)}"


def _root_from_context(
    *,
    blender_binary: object = None,
    blender_root: object = None,
) -> Path:
    if blender_binary is not None and blender_root is not None:
        raise HelperUnavailableError("provide blender_binary or blender_root, not both")
    if blender_binary is None and blender_root is None:
        raise HelperUnavailableError("Blender binary/root context is required")
    raw = Path(blender_binary if blender_binary is not None else blender_root).expanduser()
    resolved = raw.resolve(strict=False)
    if resolved.is_file() or resolved.suffix.lower() == ".exe":
        resolved = resolved.parent
    if not resolved.exists() or not resolved.is_dir():
        raise HelperUnavailableError(f"Blender root is unavailable: {resolved}")
    return resolved


def _candidate_version_dirs(root: Path, version_dir: Optional[str]) -> list[tuple[Path, str]]:
    candidates: list[tuple[Path, str]] = []
    if version_dir:
        candidates.append((root / version_dir, version_dir))
    root_name_match = _VERSION_RE.fullmatch(root.name)
    if root_name_match:
        own_version = f"{root_name_match.group(1)}.{root_name_match.group(2)}"
        candidates.insert(0, (root, own_version))
    if root.is_dir():
        discovered: list[tuple[tuple[int, int], Path, str]] = []
        for child in root.iterdir():
            if not child.is_dir():
                continue
            match = _VERSION_RE.fullmatch(child.name)
            if match:
                discovered.append(((int(match.group(1)), int(match.group(2))), child, child.name))
        for _sort_key, child, name in sorted(discovered, reverse=True):
            candidates.append((child, name))
    # Preserve order while removing duplicate roots.
    unique: list[tuple[Path, str]] = []
    seen: set[Path] = set()
    for base, name in candidates:
        canonical = base.resolve(strict=False)
        if canonical not in seen:
            seen.add(canonical)
            unique.append((canonical, name))
    return unique


def resolve_bundled_python(
    *,
    blender_binary: object = None,
    blender_root: object = None,
    blender_version: object = None,
) -> Path:
    """Resolve the bundled Python executable without any PATH fallback."""

    root = _root_from_context(blender_binary=blender_binary, blender_root=blender_root)
    version_dir = _normalise_version_dir(blender_version)
    executable_name = "python.exe" if os.name == "nt" else "python"
    candidates: list[Path] = []
    for base, _name in _candidate_version_dirs(root, version_dir):
        candidates.append(base / "python" / "bin" / executable_name)
    # A version directory may itself be supplied as blender_root.  The direct
    # path is also safe and remains within the explicit Blender root.
    candidates.append(root / "python" / "bin" / executable_name)
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    rendered = ", ".join(str(path) for path in candidates)
    raise HelperUnavailableError(f"bundled Python not found; checked: {rendered}")


discover_bundled_python = resolve_bundled_python


def bundled_python_info(
    *,
    blender_binary: object = None,
    blender_root: object = None,
    blender_version: object = None,
) -> BundledPythonInfo:
    root = _root_from_context(blender_binary=blender_binary, blender_root=blender_root)
    executable = resolve_bundled_python(
        blender_binary=blender_binary,
        blender_root=blender_root,
        blender_version=blender_version,
    )
    relative_parts = executable.relative_to(root).parts
    version_dir = relative_parts[0] if len(relative_parts) >= 4 else root.name
    return BundledPythonInfo(executable=executable, blender_root=root, version_dir=version_dir)


def _thread_capped_environment(base: Optional[dict[str, str]] = None) -> dict[str, str]:
    environment = dict(os.environ if base is None else base)
    for name in THREAD_CAP_NAMES:
        environment[name] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def _creation_flags() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) | int(
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    )


def probe_python_version(python_executable: object, *, timeout: float = 5.0) -> str:
    """Return ``major.minor.micro`` from an explicitly selected interpreter."""

    executable = Path(python_executable).expanduser().resolve(strict=False)
    if not executable.is_file():
        raise HelperUnavailableError(f"Python executable is unavailable: {executable}")
    code = "import sys; print('.'.join(str(part) for part in sys.version_info[:3]))"
    try:
        completed = subprocess.run(
            [str(executable), "-c", code],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_thread_capped_environment(),
            shell=False,
            check=False,
            timeout=timeout,
            creationflags=_creation_flags(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HelperUnavailableError(f"could not probe Python version: {executable}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()[:256]
        raise HelperUnavailableError(f"Python version probe failed: {detail}")
    version = completed.stdout.decode("utf-8", errors="strict").strip().splitlines()
    if not version or not re.fullmatch(r"\d+\.\d+\.\d+", version[-1]):
        raise HelperUnavailableError("Python version probe returned invalid output")
    return version[-1]


get_python_version = probe_python_version
get_bundled_python_version = probe_python_version


class PersistentSingleWorker:
    """One exact owned helper process with a persistent framed stdio channel."""

    def __init__(
        self,
        worker_script: object = None,
        *,
        blender_binary: object = None,
        blender_root: object = None,
        blender_version: object = None,
        python_executable: object = None,
        bundled_python: object = None,
        session_nonce: Optional[str] = None,
        generation: int = 0,
        handshake_timeout: float = DEFAULT_HANDSHAKE_TIMEOUT,
        io_timeout: float = DEFAULT_IO_TIMEOUT,
    ) -> None:
        if python_executable is not None and bundled_python is not None:
            raise HelperUnavailableError("provide python_executable or bundled_python, not both")
        self.worker_script = (
            Path(worker_script)
            if worker_script is not None
            else Path(__file__).with_name("pro_process_worker.py")
        ).expanduser().resolve(strict=False)
        self.blender_binary = blender_binary
        self.blender_root = blender_root
        self.blender_version = blender_version
        self.python_executable = python_executable if python_executable is not None else bundled_python
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise ValueError("generation must be a non-negative integer")
        self.generation = generation
        self.session_nonce = session_nonce or secrets.token_hex(16)
        if not isinstance(self.session_nonce, str) or not self.session_nonce:
            raise ValueError("session_nonce must be non-empty text")
        self.handshake_timeout = float(handshake_timeout)
        self.io_timeout = float(io_timeout)
        if self.handshake_timeout <= 0 or self.io_timeout <= 0:
            raise ValueError("timeouts must be positive")

        self._process: Any = None
        self._identity: Optional[OwnedProcess] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._inbox: queue.Queue[Any] = queue.Queue()
        self._write_lock = threading.Lock()
        # One lock owns the complete stream operation, including protocol
        # encoding and publication of a task ticket.  A force-close caller
        # never tears down the stream while this lock is held; it records a
        # request and lets the writer finish the operation first.
        self._close_requested = False
        self._ready_message: Optional[Envelope] = None
        self._pending: dict[tuple[str, int], TaskTicket] = {}
        self._completed: dict[tuple[str, int], Envelope] = {}
        self._next_sequence = 1
        self._last_pid: Optional[int] = None
        self._last_command: tuple[str, ...] = ()
        self._closed = False
        # Graceful shutdown is also exposed as a resumable protocol.  The
        # modal path sends once and polls one frame at a time; the legacy
        # ``shutdown`` method below may still use a blocking compatibility
        # loop for cancel/unregister callers.
        self._shutdown_requested = False
        self._shutdown_acknowledged = False
        self._shutdown_sequence: Optional[int] = None
        self._shutdown_ack: Optional[Envelope] = None
        self._shutdown_force_sent = False

    @property
    def process(self) -> Any:
        return self._process

    @property
    def owned_process(self) -> Optional[OwnedProcess]:
        return self._identity

    @property
    def pid(self) -> Optional[int]:
        if self._identity is not None:
            return self._identity.pid
        return self._last_pid

    @property
    def command(self) -> tuple[str, ...]:
        return self._last_command

    @property
    def ready_message(self) -> Optional[Envelope]:
        return self._ready_message

    @property
    def ready_payload(self) -> Optional[Any]:
        return None if self._ready_message is None else self._ready_message.payload

    @property
    def is_ready(self) -> bool:
        return self._ready_message is not None and self.is_alive

    @property
    def is_alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def _select_python(self) -> Path:
        if self.python_executable is not None:
            executable = Path(self.python_executable).expanduser().resolve(strict=False)
            if not executable.is_file():
                raise HelperUnavailableError(f"explicit helper Python is unavailable: {executable}")
            return executable
        return resolve_bundled_python(
            blender_binary=self.blender_binary,
            blender_root=self.blender_root,
            blender_version=self.blender_version,
        )

    def _validate_worker_script(self) -> Path:
        if not self.worker_script.is_file():
            raise HelperUnavailableError(f"worker script is unavailable: {self.worker_script}")
        return self.worker_script.resolve()

    def start(self) -> Envelope:
        if self.is_ready:
            assert self._ready_message is not None
            return self._ready_message
        if self._process is not None:
            self._cleanup_process(force=True)
        executable = self._select_python()
        script = self._validate_worker_script()
        command = (str(executable), str(script))
        environment = _thread_capped_environment()
        try:
            process = subprocess.Popen(
                list(command),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                shell=False,
                close_fds=True,
                bufsize=0,
                env=environment,
                creationflags=_creation_flags(),
            )
        except (OSError, ValueError) as exc:
            raise HelperUnavailableError(f"could not launch owned helper: {script}") from exc
        if process.stdin is None or process.stdout is None:
            try:
                process.kill()
            except Exception:
                pass
            raise HelperUnavailableError("helper did not expose binary stdin/stdout")
        self._process = process
        self._last_pid = int(process.pid)
        self._last_command = command
        self._identity = OwnedProcess(
            process=process,
            pid=int(process.pid),
            executable=executable,
            worker_script=script,
            session_nonce=self.session_nonce,
            started_at=time.time(),
        )
        self._closed = False
        self._close_requested = False
        self._shutdown_requested = False
        self._shutdown_acknowledged = False
        self._shutdown_sequence = None
        self._shutdown_ack = None
        self._shutdown_force_sent = False
        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            args=(process.stdout,),
            name=f"uv-gpt-pro-worker-reader-{process.pid}",
            daemon=True,
        )
        self._reader_thread.start()
        try:
            self._send(
                MessageType.HELLO,
                {
                    "protocol_version": 1,
                    "operations": (
                        "sum_squares",
                        "exact_correspondence_batch",
                        "shape_match_batch",
                        "snapshot_graph_build_batch",
                        "snapshot_graph_context_load",
                    ),
                    "thread_cap_names": THREAD_CAP_NAMES,
                },
                sequence=0,
            )
            message = self._wait_for_control(self.handshake_timeout)
            if message.message_type is not MessageType.READY:
                raise HandshakeError(f"expected READY, received {message.message_type.name}")
            self._validate_identity(message)
            if not isinstance(message.payload, dict):
                raise HandshakeError("READY payload must be a mapping")
            if message.payload.get("protocol_version") != 1:
                raise HandshakeError("worker protocol version mismatch")
            operations = message.payload.get("operations")
            required_operations = {
                "exact_correspondence_batch",
                "shape_match_batch",
                "snapshot_graph_build_batch",
                "snapshot_graph_context_load",
            }
            if not isinstance(operations, (tuple, list)) or not required_operations.issubset(set(operations)):
                raise HandshakeError(
                    "worker does not advertise the required exact/shape/graph/context operations"
                )
            self._ready_message = message
            return message
        except ProcessRuntimeError:
            self._cleanup_process(force=True)
            raise
        except Exception as exc:
            self._cleanup_process(force=True)
            raise HandshakeError("HELLO/READY exchange failed") from exc

    def _reader_loop(self, stdout: Any) -> None:
        try:
            while True:
                message = read_message(stdout)
                self._inbox.put(message)
        except ProtocolEOF as exc:
            self._inbox.put(_StreamClosed(exc))
        except BaseException as exc:
            self._inbox.put(_ReaderFailed(exc))

    def _send(
        self,
        message_type: MessageType,
        payload: Any,
        *,
        batch_id: str = "",
        sequence: int = 0,
        item_count: int = 0,
    ) -> None:
        with self._write_lock:
            self._send_locked(
                message_type,
                payload,
                batch_id=batch_id,
                sequence=sequence,
                item_count=item_count,
            )
            if self._close_requested:
                self._force_close_locked()

    def _send_locked(
        self,
        message_type: MessageType,
        payload: Any,
        *,
        batch_id: str = "",
        sequence: int = 0,
        item_count: int = 0,
    ) -> None:
        """Write one frame while the caller owns ``_write_lock``."""

        if self._process is None or self._process.poll() is not None:
            if self._process is not None:
                self._cleanup_process_nonblocking_locked()
            raise WorkerCrashedError("owned helper is not alive")
        stdin = self._process.stdin
        if stdin is None:
            raise WorkerCrashedError("owned helper stdin is closed")
        try:
            write_message(
                stdin,
                message_type,
                payload,
                session_nonce=self.session_nonce,
                generation=self.generation,
                batch_id=batch_id,
                sequence=sequence,
                item_count=item_count,
            )
        except (BrokenPipeError, OSError) as exc:
            raise WorkerCrashedError("owned helper stdin failed") from exc
        except ProtocolError as exc:
            raise WorkerProtocolError("could not encode helper message") from exc

    def _wait_for_control(self, timeout: float) -> Envelope:
        message = self._receive(timeout)
        if message is None:
            raise WorkerTimeoutError("timed out waiting for helper control response")
        return message

    def _receive(self, timeout: Optional[float]) -> Optional[Envelope]:
        if timeout is not None and timeout < 0:
            timeout = 0.0
        try:
            item = self._inbox.get(block=timeout is None or timeout > 0, timeout=timeout if timeout and timeout > 0 else None)
        except queue.Empty:
            if self._process is not None and self._process.poll() is not None:
                returncode = self._process.returncode
                self._cleanup_process(force=True)
                raise WorkerCrashedError(
                    f"owned helper exited with code {returncode}"
                )
            return None
        if isinstance(item, _ReaderFailed):
            self._cleanup_process(force=True)
            raise WorkerProtocolError("helper stdout reader failed") from item.error
        if isinstance(item, _StreamClosed):
            returncode = None if self._process is None else self._process.poll()
            self._cleanup_process(force=True)
            raise WorkerEOFError(f"helper stdout closed (returncode={returncode})") from item.error
        if not isinstance(item, Envelope):
            raise WorkerProtocolError("reader returned an invalid message object")
        return item

    def _validate_identity(self, message: Envelope) -> bool:
        try:
            validate_identity(message, session_nonce=self.session_nonce, generation=self.generation)
        except StaleMessageError:
            return False
        except FutureMessageError as exc:
            raise WorkerProtocolError("helper returned a future generation") from exc
        except ProtocolError as exc:
            raise ForeignFrameError("helper returned a foreign session") from exc
        return True

    def _accept_message(self, message: Envelope) -> Optional[Envelope]:
        if not self._validate_identity(message):
            return None
        if message.message_type in (MessageType.RESULT, MessageType.ERROR):
            key = (message.batch_id, message.sequence)
            ticket = self._pending.get(key)
            if ticket is None:
                if message.sequence < self._next_sequence:
                    return None
                raise ForeignFrameError("helper returned an unknown task")
            if message.item_count != ticket.item_count:
                raise WorkerProtocolError("helper result item_count mismatch")
            self._pending.pop(key, None)
            self._completed[key] = message
        return message

    def submit(
        self,
        payload: Any,
        *,
        batch_id: object,
        sequence: Optional[int] = None,
        item_count: Optional[int] = None,
    ) -> TaskTicket:
        # Keep validation, sequence/ticket publication and the complete frame
        # write under one ownership interval.  In particular, a cancel or
        # shutdown thread cannot close stdin between ``_pending[key]`` and the
        # final write/flush performed by ``write_message``.
        with self._write_lock:
            if self._close_requested:
                raise WorkerCrashedError("owned helper close requested")
            if not self.is_ready:
                raise ProcessRuntimeError("worker is not ready")
            normalized_batch = str(batch_id)
            if not normalized_batch:
                raise ValueError("batch_id must be non-empty")
            if sequence is None:
                sequence = self._next_sequence
            if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
                raise ValueError("sequence must be a positive integer")
            key = (normalized_batch, sequence)
            if key in self._pending or key in self._completed:
                raise ValueError("batch_id/sequence is already in use")
            self._next_sequence = max(self._next_sequence, sequence + 1)
            if item_count is None:
                if isinstance(payload, dict) and isinstance(payload.get("items"), (list, tuple, dict)):
                    item_count = len(payload["items"])
                else:
                    item_count = 1
            if isinstance(item_count, bool) or not isinstance(item_count, int) or item_count < 0:
                raise ValueError("item_count must be a non-negative integer")
            ticket = TaskTicket(
                batch_id=normalized_batch,
                sequence=sequence,
                generation=self.generation,
                item_count=item_count,
            )
            self._pending[key] = ticket
            try:
                self._send_locked(
                    MessageType.TASK,
                    payload,
                    batch_id=ticket.batch_id,
                    sequence=ticket.sequence,
                    item_count=ticket.item_count,
                )
                if self._close_requested:
                    # The frame may already be in the worker's pipe, but the
                    # owner has cancelled the admission.  Tear down through
                    # this writer-owned path and never publish a usable ticket.
                    self._force_close_locked()
                    raise WorkerCrashedError("owned helper close requested during submit")
            except Exception:
                self._pending.pop(key, None)
                raise
            return ticket

    def poll(self, timeout: Optional[float] = 0.0) -> Optional[Envelope]:
        """Return the next accepted message, discarding stale frames."""

        if not self.is_alive:
            if self._process is not None:
                self._cleanup_process(force=True)
            raise WorkerCrashedError("owned helper is not alive")
        deadline = None if timeout is None else time.monotonic() + max(0.0, float(timeout))
        while True:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            message = self._receive(remaining)
            if message is None:
                return None
            accepted = self._accept_message(message)
            if accepted is not None:
                return accepted
            if deadline is not None and time.monotonic() >= deadline:
                return None

    @property
    def shutdown_requested(self) -> bool:
        return bool(self._shutdown_requested)

    @property
    def shutdown_acknowledged(self) -> bool:
        return bool(self._shutdown_acknowledged)

    @property
    def shutdown_ack(self) -> Optional[Envelope]:
        return self._shutdown_ack

    def begin_shutdown(self) -> bool:
        """Send one graceful shutdown request without waiting for a reply."""

        if self._process is None:
            return True
        if self._shutdown_requested:
            return bool(self._shutdown_acknowledged and not self.is_alive)
        if not self.is_ready:
            # A non-ready helper cannot safely participate in the graceful
            # protocol.  The owner may use the exact-handle force path.
            return False
        sequence = self._next_sequence
        self._next_sequence += 1
        self._send(MessageType.SHUTDOWN, {"shutdown": True}, sequence=sequence)
        self._shutdown_requested = True
        self._shutdown_sequence = sequence
        return False

    def _receive_shutdown_frame(self) -> Optional[Envelope]:
        """Read at most one already-buffered frame, never waiting or joining."""

        try:
            item = self._inbox.get_nowait()
        except queue.Empty:
            return None
        if isinstance(item, _ReaderFailed):
            raise WorkerProtocolError("helper stdout reader failed") from item.error
        if isinstance(item, _StreamClosed):
            raise WorkerEOFError("helper stdout closed") from item.error
        if not isinstance(item, Envelope):
            raise WorkerProtocolError("reader returned an invalid message object")
        return item

    def advance_shutdown(self) -> str:
        """Advance graceful shutdown by one nonblocking protocol step.

        Returns ``pending``, ``acknowledged``, or ``complete``.  A caller may
        invoke this once per modal advance; no process wait, sleep, or reader
        join with a positive timeout occurs here.
        """

        if self._process is None:
            return "complete"
        if not self._shutdown_requested:
            self.begin_shutdown()
        if self._process is None:
            return "complete"
        if self._process.poll() is not None:
            self._cleanup_process_nonblocking()
            return "complete" if self._process is None else "pending"
        try:
            message = self._receive_shutdown_frame()
        except WorkerEOFError:
            # A well-behaved helper can enqueue SHUTDOWN_ACK immediately
            # before its stdout reader observes EOF.  Treat EOF as the final
            # nonblocking cleanup signal; preserve an already captured ACK.
            if self._process is not None and self._process.poll() is not None:
                self._cleanup_process_nonblocking()
            elif self._process is not None:
                self.force_close_nonblocking()
            return "complete" if self._process is None else "pending"
        if message is not None:
            accepted = self._accept_message(message)
            if accepted is not None:
                if accepted.message_type is MessageType.SHUTDOWN_ACK:
                    self._shutdown_acknowledged = True
                    self._shutdown_ack = accepted
                elif accepted.message_type is MessageType.ERROR:
                    raise WorkerRemoteError(str(accepted.payload)[:512])
        if self._process is not None and self._process.poll() is not None:
            self._cleanup_process_nonblocking()
            return "complete" if self._process is None else "pending"
        return "acknowledged" if self._shutdown_acknowledged else "pending"

    def force_close_nonblocking(self) -> str:
        """Request termination of this exact owned process without waiting."""

        if not self._write_lock.acquire(blocking=False):
            # The stream writer remains the sole owner of stdin/stdout and the
            # process handle until its encode/write/flush operation releases
            # the lock.  Do not terminate or close anything from this caller.
            self._close_requested = True
            return "pending"
        try:
            return self._force_close_locked()
        finally:
            self._write_lock.release()

    def _force_close_locked(self) -> str:
        """Terminate and reap the exact process while the stream lock is held."""

        process = self._process
        identity = self._identity
        if process is None:
            return "complete"
        if identity is None or identity.process is not process or identity.pid != process.pid:
            raise WorkerProtocolError("owned-process identity is inconsistent")
        self._close_requested = True
        if process.poll() is None:
            try:
                if not self._shutdown_force_sent:
                    process.terminate()
                    self._shutdown_force_sent = True
                else:
                    process.kill()
            except Exception:
                pass
        self._cleanup_process_nonblocking_locked()
        return "complete" if self._process is None else "pending"

    def _cleanup_process_nonblocking(self) -> None:
        """Release an exited/owned process without waiting or joining."""

        if not self._write_lock.acquire(blocking=False):
            self._close_requested = True
            return
        try:
            self._cleanup_process_nonblocking_locked()
        finally:
            self._write_lock.release()

    def _cleanup_process_nonblocking_locked(self) -> None:
        """Nonblocking cleanup for a caller that already owns the stream lock."""

        process = self._process
        identity = self._identity
        if process is None:
            self._ready_message = None
            self._pending.clear()
            self._completed.clear()
            self._closed = True
            self._shutdown_requested = False
            self._shutdown_acknowledged = False
            self._shutdown_sequence = None
            self._shutdown_ack = None
            self._shutdown_force_sent = False
            self._close_requested = False
            return
        if identity is None or identity.process is not process or identity.pid != process.pid:
            raise WorkerProtocolError("owned-process identity is inconsistent")
        if process.poll() is None:
            return
        for stream_name in ("stdin", "stdout"):
            stream = getattr(process, stream_name, None)
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
        reader = self._reader_thread
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=0.0)
        self._process = None
        self._identity = None
        self._reader_thread = None
        self._ready_message = None
        self._pending.clear()
        self._completed.clear()
        self._closed = True
        self._shutdown_requested = False
        self._shutdown_acknowledged = False
        self._shutdown_sequence = None
        self._shutdown_ack = None
        self._shutdown_force_sent = False
        self._close_requested = False

    def _take_result(self, ticket: TaskTicket) -> Envelope:
        key = (ticket.batch_id, ticket.sequence)
        message = self._completed.pop(key, None)
        if message is None:
            raise ProcessRuntimeError("task result is not complete")
        if message.message_type is MessageType.ERROR:
            detail = message.payload if isinstance(message.payload, dict) else {"message": message.payload}
            raise WorkerRemoteError(str(detail)[:512])
        if message.message_type is not MessageType.RESULT:
            raise WorkerProtocolError("task completed with an unexpected message")
        return message

    def await_result(self, ticket: TaskTicket, *, timeout: Optional[float] = None) -> Envelope:
        if ticket.generation != self.generation:
            raise ProcessRuntimeError("ticket belongs to a stale generation")
        key = (ticket.batch_id, ticket.sequence)
        if key not in self._pending and key not in self._completed:
            raise ProcessRuntimeError("unknown task ticket")
        effective_timeout = self.io_timeout if timeout is None else float(timeout)
        if effective_timeout <= 0:
            raise ValueError("timeout must be positive")
        deadline = time.monotonic() + effective_timeout
        while key not in self._completed:
            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= 0:
                raise WorkerTimeoutError(f"timed out waiting for batch {ticket.batch_id}")
            message = self.poll(remaining)
            if message is None:
                raise WorkerTimeoutError(f"timed out waiting for batch {ticket.batch_id}")
        return self._take_result(ticket)

    def consume_ticket_message(self, ticket: TaskTicket) -> Envelope:
        """Remove an already-polled result/error without applying semantics."""

        if ticket.generation != self.generation:
            raise ProcessRuntimeError("ticket belongs to a stale generation")
        key = (ticket.batch_id, ticket.sequence)
        message = self._completed.pop(key, None)
        if message is None:
            raise ProcessRuntimeError("ticket message is not complete")
        return message

    def cancel(
        self,
        *,
        batch_id: object = "",
        sequence: int = 0,
        timeout: Optional[float] = None,
    ) -> Optional[Envelope]:
        if not self.is_ready:
            return None
        normalized_batch = str(batch_id)
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise ValueError("sequence must be a non-negative integer")
        cancel_sequence = self._next_sequence
        self._next_sequence += 1
        self._send(
            MessageType.CANCEL,
            {"batch_id": normalized_batch, "sequence": sequence},
            batch_id=normalized_batch,
            sequence=cancel_sequence,
            item_count=0,
        )
        effective_timeout = self.io_timeout if timeout is None else float(timeout)
        deadline = time.monotonic() + effective_timeout
        while True:
            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= 0:
                self._cleanup_process(force=True)
                raise WorkerTimeoutError("timed out waiting for CANCEL_ACK")
            message = self._receive(remaining)
            if message is None:
                self._cleanup_process(force=True)
                raise WorkerTimeoutError("timed out waiting for CANCEL_ACK")
            accepted = self._accept_message(message)
            if accepted is None:
                continue
            if accepted.message_type is MessageType.CANCEL_ACK:
                if accepted.batch_id != normalized_batch:
                    raise WorkerProtocolError("CANCEL_ACK batch mismatch")
                return accepted
            if accepted.message_type is MessageType.ERROR:
                raise WorkerRemoteError(str(accepted.payload)[:512])

    def shutdown(self, *, timeout: float = DEFAULT_SHUTDOWN_TIMEOUT) -> Optional[Envelope]:
        if self._process is None:
            return None
        ack: Optional[Envelope] = None
        try:
            if self.is_ready and not self._shutdown_requested:
                self.begin_shutdown()
            deadline = time.monotonic() + max(0.01, float(timeout))
            while self._process is not None:
                status = self.advance_shutdown()
                if self._shutdown_ack is not None:
                    ack = self._shutdown_ack
                if status == "complete":
                    return ack
                if time.monotonic() >= deadline:
                    raise WorkerTimeoutError("timed out waiting for SHUTDOWN_ACK")
                time.sleep(0.001)
            return ack
        except Exception:
            self._cleanup_process(force=True)
            raise

    def _cleanup_process(self, *, force: bool, wait_timeout: float = 1.0) -> None:
        # Blocking compatibility callers may wait for the writer to release,
        # but they still share the same single cleanup owner as the modal
        # nonblocking path.
        with self._write_lock:
            self._cleanup_process_locked(force=force, wait_timeout=wait_timeout)

    def _cleanup_process_locked(self, *, force: bool, wait_timeout: float = 1.0) -> None:
        process = self._process
        identity = self._identity
        if process is None:
            self._ready_message = None
            self._pending.clear()
            self._completed.clear()
            self._closed = True
            self._shutdown_requested = False
            self._shutdown_acknowledged = False
            self._shutdown_sequence = None
            self._shutdown_ack = None
            self._shutdown_force_sent = False
            self._close_requested = False
            return
        if identity is None or identity.process is not process or identity.pid != process.pid:
            raise WorkerProtocolError("owned-process identity is inconsistent")
        if process.poll() is None:
            if force:
                try:
                    process.terminate()
                except Exception:
                    pass
            try:
                process.wait(timeout=max(0.05, wait_timeout))
            except subprocess.TimeoutExpired:
                if force or process.poll() is None:
                    try:
                        process.kill()
                    except Exception:
                        pass
                    try:
                        process.wait(timeout=max(0.05, wait_timeout))
                    except Exception:
                        pass
        for stream_name in ("stdin", "stdout"):
            stream = getattr(process, stream_name, None)
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
        reader = self._reader_thread
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=0.5)
        self._process = None
        self._identity = None
        self._reader_thread = None
        self._ready_message = None
        self._pending.clear()
        self._completed.clear()
        self._closed = True
        self._shutdown_requested = False
        self._shutdown_acknowledged = False
        self._shutdown_sequence = None
        self._shutdown_ack = None
        self._shutdown_force_sent = False
        self._close_requested = False

    def close(self, *, graceful: bool = True) -> None:
        """Idempotent cleanup; force cleanup is exact-handle-only."""

        if self._process is None:
            self._closed = True
            self._close_requested = False
            return
        if graceful and self.is_ready:
            try:
                self.shutdown()
                return
            except Exception:
                pass
        self._cleanup_process(force=True)

    def advance_generation(self, generation: int) -> None:
        if isinstance(generation, bool) or not isinstance(generation, int) or generation <= self.generation:
            raise ValueError("generation must increase")
        self.generation = generation
        self._pending.clear()
        self._completed.clear()

    def __enter__(self) -> "PersistentSingleWorker":
        return self

    def __exit__(self, _exc_type: object, _exc_value: object, _traceback: object) -> None:
        self.close()


__all__ = [
    "THREAD_CAP_NAMES",
    "BundledPythonInfo",
    "OwnedProcess",
    "TaskTicket",
    "ProcessRuntimeError",
    "HelperUnavailableError",
    "HandshakeError",
    "WorkerTimeoutError",
    "WorkerCrashedError",
    "WorkerEOFError",
    "WorkerProtocolError",
    "ForeignFrameError",
    "WorkerRemoteError",
    "resolve_bundled_python",
    "discover_bundled_python",
    "bundled_python_info",
    "probe_python_version",
    "get_python_version",
    "get_bundled_python_version",
    "PersistentSingleWorker",
]
