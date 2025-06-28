import asyncio
import contextlib
import enum
import logging
import pathlib
import re
import subprocess
from typing import Any, Dict, List, Optional, Tuple, Type

import sansio_lsp_client as lsp
from pydantic import BaseModel, parse_obj_as
from sansio_lsp_client.io_handler import _make_request, _make_response
from sansio_lsp_client.structs import JSONDict, Request

from utils import get_worker_count

INCLUDE_REGEX = re.compile(r"\s*#\s*include ([\"<](.*)[\">])")

# TODO - Bit hackish, but add to the LSP capabilities here, only extension point we have
lsp.client.CAPABILITIES["textDocument"]["publishDiagnostics"]["codeActionsInline"] = True  # type: ignore


class ClangdCrashed(Exception):
    pass


DocumentUri = str


class WorkspaceEdit(BaseModel):
    changes: Optional[Dict[DocumentUri, List[lsp.TextEdit]]]
    # TODO - The rest of the fields


class CodeActionKind(enum.Enum):
    QUICK_FIX = "quickfix"
    # TODO - Rest of the kinds


class CodeAction(BaseModel):
    title: str
    kind: Optional[CodeActionKind]
    diagnostics: Optional[List[lsp.Diagnostic]]
    isPreferred: Optional[bool]
    # TODO - disabled?
    edit: Optional[WorkspaceEdit]
    command: Optional[lsp.Command]
    data: Optional[Any]


class ClangdDiagnostic(lsp.Diagnostic):
    codeActions: Optional[List[CodeAction]]


class ClangdPublishDiagnostics(lsp.PublishDiagnostics):
    version: Optional[int]
    diagnostics: List[ClangdDiagnostic]


class AsyncSendLspClient(lsp.Client):
    def _ensure_send_buf_is_queue(self):
        if not isinstance(self._send_buf, asyncio.Queue):
            self._send_buf: asyncio.Queue[bytes] = asyncio.Queue()

    def _send_request(self, method: str, params: Optional[JSONDict] = None) -> int:
        self._ensure_send_buf_is_queue()

        id = self._id_counter
        self._id_counter += 1

        self._send_buf.put_nowait(_make_request(method=method, params=params, id=id))
        self._unanswered_requests[id] = Request(id=id, method=method, params=params)
        return id

    def _send_notification(self, method: str, params: Optional[JSONDict] = None) -> None:
        self._ensure_send_buf_is_queue()
        self._send_buf.put_nowait(_make_request(method=method, params=params))

    def _send_response(
        self,
        id: int,
        result: Optional[JSONDict] = None,
        error: Optional[JSONDict] = None,
    ) -> None:
        self._ensure_send_buf_is_queue()
        self._send_buf.put_nowait(_make_response(id=id, result=result, error=error))

    def _handle_request(self, request: lsp.Request) -> lsp.Event:
        # TODO - This is copied from sansio-lsp-client
        def parse_request(event_cls: Type[lsp.Event]) -> lsp.Event:
            if issubclass(event_cls, lsp.ServerRequest):
                event = parse_obj_as(event_cls, request.params)
                assert request.id is not None
                event._id = request.id
                event._client = self
                return event
            elif issubclass(event_cls, lsp.ServerNotification):
                return parse_obj_as(event_cls, request.params)
            else:
                raise TypeError("`event_cls` must be a subclass of ServerRequest" " or ServerNotification")

        if request.method == "textDocument/publishDiagnostics":
            return parse_request(ClangdPublishDiagnostics)

        return super()._handle_request(request)

    async def async_send(self) -> bytes:
        return await self._send_buf.get()


IncludeLine = Tuple[str, int]


def parse_includes_from_diagnostics(
    filename: str, document: lsp.TextDocumentItem, diagnostics: List[ClangdDiagnostic]
) -> Tuple[Tuple[IncludeLine, ...], Tuple[IncludeLine, ...]]:
    """Returns a tuple of (add, remove) includes"""

    add_includes: Set[IncludeLine] = set()
    remove_includes: List[IncludeLine] = []

    # Parse include diagnostics
    for diagnostic in diagnostics:
        if diagnostic.code == "unused-includes":
            assert diagnostic.codeActions
            assert diagnostic.codeActions[0].title == "remove #include directive"

            # Only need the line number, we don't expect multi-line includes
            assert diagnostic.range.start.line == diagnostic.range.end.line
            text = document.text.splitlines()[diagnostic.range.start.line]

            include_match = INCLUDE_REGEX.match(text)

            if include_match:
                remove_includes.append((include_match.group(1).strip('"'), diagnostic.range.start.line))
            else:
                logging.error(f"Couldn't match #include regex to diagnostic line: {text}")
        elif diagnostic.code == "missing-includes":
            assert diagnostic.codeActions
            assert diagnostic.codeActions[0].title.startswith("#include")
            assert diagnostic.codeActions[0].edit
            assert diagnostic.codeActions[0].edit.changes
            assert len(diagnostic.codeActions[0].edit.changes[document.uri]) == 1

            textEdit = diagnostic.codeActions[0].edit.changes[document.uri][0]
            text = textEdit.newText
            include_match = INCLUDE_REGEX.match(text)

            if include_match:
                # TODO - Alias things like: absl/types/optional.h -> third_party/abseil-cpp/absl/types/optional.h
                add_includes.add((include_match.group(1).strip('"'), textEdit.range.start.line))
            else:
                logging.error(f"Couldn't match #include regex to diagnostic line: {text}")

    return (tuple(add_includes), tuple(remove_includes))


# Partially based on sansio-lsp-client/tests/test_actual_langservers.py
class ClangdClient:
    def __init__(self, clangd_path: str, root_path: pathlib.Path, compile_commands_dir: pathlib.Path = None):
        self.root_path = root_path
        self.clangd_path = clangd_path
        self.compile_commands_dir = compile_commands_dir
        self.lsp_client = AsyncSendLspClient(
            root_uri=root_path.as_uri(),
            trace="verbose",
        )
        self.logger = logging.getLogger("clangd")

        self._process = None
        self._concurrent_tasks = None
        self._messages = []
        self._new_messages = asyncio.Queue()
        self._notification_queues = []
        self._process_gone = asyncio.Event()

    async def _send_stdin(self):
        try:
            while self._process:
                message = await self.lsp_client.async_send()
                self._process.stdin.write(message)
                await self._process.stdin.drain()

                # Log the sent message for debugging purposes
                self.logger.debug(message.decode("utf8").rstrip())
        except asyncio.CancelledError:
            pass

        self._process_gone.set()

    async def _process_stdout(self):
        try:
            while self._process:
                data = await self._process.stdout.read(1024)
                if data == b"":  # EOF
                    break

                # Parse the output and enqueue it
                for event in self.lsp_client.recv(data):
                    if isinstance(event, lsp.ServerNotification):
                        # If a notification comes in, tell anyone listening
                        for queue in self._notification_queues:
                            queue.put_nowait(event)
                    else:
                        self._new_messages.put_nowait(event)
                        self._try_default_reply(event)

                # TODO - Log the output for debugging purposes
                # How best to do this without getting too into
                # the protocol details?
        except asyncio.CancelledError:
            pass

        self._process_gone.set()

    async def _log_stderr(self):
        try:
            while self._process:
                line = await self._process.stderr.readline()
                if line == b"":  # EOF
                    break

                # Log the output for debugging purposes
                self.logger.debug(line.decode("utf8").rstrip())
        except asyncio.CancelledError:
            pass

        self._process_gone.set()

    async def start(self):
        args = ["--enable-config", "--background-index=false", f"-j={get_worker_count()}"]

        if self.compile_commands_dir:
            args.append(f"--compile-commands-dir={self.compile_commands_dir}")

        self._process = await asyncio.create_subprocess_exec(
            self.clangd_path,
            *args,
            cwd=self.root_path,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        # Create concurrently running tasks for sending input to clangd, and for processing clangd's output
        self._concurrent_tasks = asyncio.gather(
            self._send_stdin(), self._process_stdout(), self._log_stderr(), return_exceptions=True
        )

        await self._wait_for_message_of_type(lsp.Initialized)

    def _try_default_reply(self, msg):
        if isinstance(
            msg,
            (
                lsp.ShowMessageRequest,
                lsp.WorkDoneProgressCreate,
                lsp.RegisterCapabilityRequest,
                lsp.ConfigurationRequest,
            ),
        ):
            msg.reply()

    async def _wait_for_message_of_type(self, message_type, timeout=5):
        # First check already processed messages
        for message in self._messages:
            if isinstance(message, message_type):
                self._messages.remove(message)
                return message

        # Then keep waiting for a message of the correct type
        while True:
            message = await asyncio.wait_for(self._new_messages.get(), timeout=timeout)
            if isinstance(message, message_type):
                return message
            else:
                self._messages.append(message)

    async def _wrap_coro(self, coro):
        process_gone_task = asyncio.create_task(self._process_gone.wait())
        task = asyncio.create_task(coro)
        done, _ = await asyncio.wait({task, process_gone_task}, return_when=asyncio.FIRST_COMPLETED)

        if process_gone_task in done:
            task.cancel()
            raise ClangdCrashed()
        else:
            process_gone_task.cancel()

        return task.result()

    @contextlib.asynccontextmanager
    async def listen_for_notifications(self, cancellation_token=None):
        queue = asyncio.Queue()
        if cancellation_token is None:
            cancellation_token = asyncio.Event()

        async def get_notifications():
            cancellation_token_task = asyncio.create_task(cancellation_token.wait())

            try:
                while not cancellation_token.is_set():
                    queue_task = asyncio.create_task(self._wrap_coro(queue.get()))
                    done, _ = await asyncio.wait(
                        {queue_task, cancellation_token_task}, return_when=asyncio.FIRST_COMPLETED
                    )

                    if cancellation_token_task in done:
                        queue_task.cancel()
                        break
                    else:
                        yield queue_task.result()
            except asyncio.CancelledError:
                if queue_task:
                    queue_task.cancel()
                raise
            finally:
                cancellation_token_task.cancel()

        self._notification_queues.append(queue)
        yield get_notifications()
        cancellation_token.set()
        self._notification_queues.remove(queue)

    @staticmethod
    def validate_config(root_path: pathlib.Path):
        # TODO - Check for a valid config with IncludeCleaner setup
        return True

    def open_document(self, filename: str) -> lsp.TextDocumentItem:
        if filename.endswith(".h"):
            # TODO - How to mark header files as Objective-C++ or C? Does it matter?
            language_id = "cpp"
        elif (
            filename.endswith(".hh")
            or filename.endswith(".hpp")
            or filename.endswith(".hpp11")
            or filename.endswith(".hxx")
        ):
            language_id = "cpp"
        elif filename.endswith(".cc") or filename.endswith(".cpp") or filename.endswith(".cxx"):
            language_id = "cpp"
        elif filename.endswith(".c"):
            language_id = "c"
        elif filename.endswith(".mm"):
            language_id = "objective-cpp"
        else:
            raise RuntimeError(f"Unknown file extension: {filename}")

        with open((self.root_path / filename), "r") as f:
            file_contents = f.read()

        document = lsp.TextDocumentItem(
            uri=(self.root_path / filename).as_uri(),
            languageId=language_id,
            text=file_contents,
            version=1,
        )

        self.lsp_client.did_open(document)

        return document

    def close_document(self, filename: str):
        self.lsp_client.did_close(
            lsp.TextDocumentIdentifier(
                uri=(self.root_path / filename).as_uri(),
            )
        )

    @contextlib.asynccontextmanager
    async def with_document(self, filename: str):
        yield self.open_document(filename)
        self.close_document(filename)

    def change_document(self, filename: str, version: int, text: str, want_diagnostics: Optional[bool] = None):
        text_document = lsp.VersionedTextDocumentIdentifier(
            uri=(self.root_path / filename).as_uri(),
            version=version,
        )
        content_changes = [lsp.TextDocumentContentChangeEvent(text=text)]

        # NOTE - The following is copied from sansio-lsp-client to add the wantDiagnostics property
        assert self.lsp_client._state == lsp.ClientState.NORMAL

        params = {
            "textDocument": text_document.dict(),
            "contentChanges": [evt.dict() for evt in content_changes],
        }

        if want_diagnostics is not None:
            params["wantDiagnostics"] = want_diagnostics

        self.lsp_client._send_notification(
            method="textDocument/didChange",
            params=params,
        )

    def save_document(self, filename: str):
        self.lsp_client.did_save(
            lsp.TextDocumentIdentifier(
                uri=(self.root_path / filename).as_uri(),
            )
        )

    async def get_include_suggestions(self, filename: str) -> Tuple[Tuple[IncludeLine, ...], Tuple[IncludeLine, ...]]:
        """Returns a tuple of (add, remove) includes for a filename"""

        document: lsp.TextDocumentItem
        notification: ClangdPublishDiagnostics

        # Open the document and wait for the diagnostics notification
        async with self.listen_for_notifications() as notifications:
            async with self.with_document(filename) as document:
                async for notification in notifications:
                    if isinstance(notification, ClangdPublishDiagnostics) and notification.uri == document.uri:
                        break

        return parse_includes_from_diagnostics(filename, document, notification.diagnostics)

    async def exit(self):
        if self._process:
            try:
                if self._process.returncode is None and not self._process_gone.is_set():
                    self.lsp_client.shutdown()
                    shutdown_task = asyncio.create_task(self._wait_for_message_of_type(lsp.Shutdown, timeout=None))
                    done, _ = await asyncio.wait(
                        {shutdown_task, self._concurrent_tasks}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if shutdown_task in done:
                        self.lsp_client.exit()
                    else:
                        shutdown_task.cancel()
            except Exception:
                pass
            finally:
                # Cleanup the subprocess
                try:
                    self._process.terminate()
                except ProcessLookupError:
                    pass
                await self._process.wait()
                self._process = None

        try:
            self._concurrent_tasks.cancel()
            await self._concurrent_tasks
        except asyncio.CancelledError:
            pass
        self._concurrent_tasks = None
