import asyncio
import contextlib
import logging
import pathlib
import re
import subprocess
from typing import List, Optional

import sansio_lsp_client as lsp
from sansio_lsp_client.io_handler import _make_request, _make_response
from sansio_lsp_client.structs import JSONDict, Request

from utils import get_worker_count

INCLUDE_REGEX = re.compile(r"#include [\"<](.*)[\">]")

# This is a list of known filenames where clangd produces a false
# positive when suggesting unused includes to remove. Usually these
# are umbrella headers, or headers where clangd thinks the canonical
# location for a symbol is actually in a forward declaration, causing
# it to flag the correct header as unused everywhere, so ignore those.
UNUSED_INCLUDE_IGNORE_LIST = (
    "base/bind.h",
    "base/callback.h",
    "base/compiler_specific.h",
    "base/strings/string_piece.h",
    "base/trace_event/base_tracing.h",
    "build/build_config.h",
    "mojo/public/cpp/bindings/pending_receiver.h",
    # TODO - Keep populating this list
)

UNUSED_EDGE_IGNORE_LIST = (
    ("base/memory/aligned_memory.h", "base/bits.h"),
    # TODO - Keep populating this list
)


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

    async def send(self) -> bytes:
        return await self._send_buf.get()


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

    async def _send_stdin(self):
        while self._process:
            message = await self.lsp_client.send()
            self._process.stdin.write(message)
            await self._process.stdin.drain()

            # Log the sent message for debugging purposes
            self.logger.debug(message.decode("utf8").rstrip())

    async def _process_stdout(self):
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

    async def _log_stderr(self):
        while self._process:
            line = await self._process.stderr.readline()
            if line == b"":  # EOF
                break

            # Log the output for debugging purposes
            self.logger.debug(line.decode("utf8").rstrip())

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
            self._send_stdin(),
            self._process_stdout(),
            self._log_stderr(),
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

    @contextlib.asynccontextmanager
    async def listen_for_notifications(self, cancellation_token=None):
        queue = asyncio.Queue()
        if cancellation_token is None:
            cancellation_token = asyncio.Event()

        async def get_notifications():
            cancellation_token_task = asyncio.create_task(cancellation_token.wait())

            while not cancellation_token.is_set():
                queue_task = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait(
                    {queue_task, cancellation_token_task}, return_when=asyncio.FIRST_COMPLETED
                )

                if cancellation_token in done:
                    break
                else:
                    yield queue_task.result()

        self._notification_queues.append(queue)
        yield get_notifications()
        cancellation_token.set()
        self._notification_queues.remove(queue)

    @staticmethod
    def validate_config(root_path: pathlib.Path):
        # TODO - Check for a valid config with IncludeCleaner setup
        return True

    def open_document(self, filename: str) -> lsp.TextDocumentItem:
        # TODO - How to mark header files as Objective-C++ or C? Does it matter?
        if filename.endswith(".h") or filename.endswith(".cc"):
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

    async def get_unused_includes(self, filename: str) -> List[str]:
        """Returns a list of unused includes for a filename"""

        unused_includes = []
        diagnostics = []

        async with self.with_document(filename) as document:
            document_contents = document.text
            async with self.listen_for_notifications() as notifications:
                async for notification in notifications:
                    if isinstance(notification, lsp.PublishDiagnostics):
                        if notification.uri == document.uri:
                            diagnostics = notification.diagnostics
                            break

        # Parse diagnostics looking for unused includes
        for diagnostic in (diag for diag in diagnostics if diag.code == "unused-includes"):
            # Only need the line number, we don't expect multi-line includes
            assert diagnostic.range.start.line == diagnostic.range.end.line
            text = document_contents.splitlines()[diagnostic.range.start.line]

            try:
                included_filename = INCLUDE_REGEX.match(text).group(1)
            except AttributeError:
                logging.error(f"Couldn't match #include regex to diagnostic line: {text}")
            else:
                ignore_edge = (filename, included_filename) in UNUSED_EDGE_IGNORE_LIST
                ignore_include = included_filename in UNUSED_INCLUDE_IGNORE_LIST

                # Cut down on noise by ignoring known false positives
                if not ignore_edge and not ignore_include:
                    unused_includes.append(included_filename)

        return unused_includes

    async def exit(self):
        try:
            self.lsp_client.shutdown()
            await self._wait_for_message_of_type(lsp.Shutdown, timeout=None)
            self.lsp_client.exit()
        except Exception:
            self._process.terminate()
        finally:
            # Cleanup the subprocess
            await self._process.wait()
            try:
                self._concurrent_tasks.cancel()
                await self._concurrent_tasks
            except asyncio.CancelledError:
                pass
            self._concurrent_tasks = None
            self._process = None
