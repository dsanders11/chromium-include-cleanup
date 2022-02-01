import asyncio
from collections import defaultdict
import contextlib
from re import S
import subprocess
from typing import List, Optional

import sansio_lsp_client as lsp


class LanguageServerProtocol(asyncio.Protocol):
    def __init__(self, logger=None):
        self.logger = logger

    def connection_made(self, transport):
        self.transport = transport

    def send_message(self, message):
        # TODO - Real implementation - frame message with headers
        self.transport.write(message)
        # TODO - Log message

    def handle_messages(self, handler):
        self.message_handler = handler


class SubprocessLanguageServerProtocol(LanguageServerProtocol):
    def __init__(self, logger=None):
        super().__init__(logger=logger)
        self._stdout_buffer = bytearray()
        self._stderr_buffer = bytearray()

    def pipe_data_received(self, fd: int, data: bytes):
        if fd == 2 and self.logger:
            # TODO - Only log full lines? Does this work?
            line = self.transport.get_pipe_transport(2).readline()
            self.logger.debug(line)


class Client:
    def __init__(
        self,
        protocol: LanguageServerProtocol
    ) -> None:
        self.protocol = protocol
        self.protocol.handle_messages(self._handle_message)

        self._notification_listeners = defaultdict(list)

    @contextlib.asynccontextmanager
    async def initialize(
        self,
        process_id: Optional[int] = None,
        root_uri: Optional[str] = None,
        workspace_folders: Optional[List[lsp.WorkspaceFolder]] = None,
        trace: str = "off",
    ) -> None:
        pass  # TODO - Context manager

    async def listen_for_notifications(self, notification_type, cancellation_token: asyncio.Event):
        queue = asyncio.Queue()
        self._notification_listeners[notification_type].append(queue)

        cancellation_token_wait_task = asyncio.create_task(cancellation_token.wait())

        while True:
            get_queue_task = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait({get_queue_task, cancellation_token_wait_task}, when=asyncio.FIRST_COMPLETED)

            if cancellation_token in done:
                break
            else:
                yield await get_queue_task.result


async def create_subprocess_client(path, *args, cwd=None):
    # TODO - Real implementation
    loop = asyncio.get_running_loop()

    _, protocol = await loop.subprocess_exec(
        lambda: SubprocessLanguageServerProtocol(),
        path,
        *args,
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    return Client(protocol)
