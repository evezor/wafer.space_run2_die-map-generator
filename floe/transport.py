"""Bus-agnostic transport primitives for large-payload fragmentation.

Per-bus wire-format adapters use these classes; the protocol logic
(state machine, drop detection, buffers, timeouts) lives here once
and is reused across CANBus / WSBus / future buses.
"""

__all__ = (
    'LogicalFrame', 'StreamFragmenter', 'StreamReassembler',
    'Stream', 'StreamSession',
    'StreamError', 'StreamTimeoutError', 'StreamRemoteError',
    'STREAM_BEGIN', 'STREAM_CHUNK', 'STREAM_END', 'STREAM_CANCEL',
    'ACK_OK', 'ACK_ERROR', 'ACK_CANCEL',
)


# Stream protocol — payload byte 0 acts as marker. See Stream / StreamSession
# below for the wire format.
STREAM_BEGIN  = 0x01
STREAM_CHUNK  = 0x02
STREAM_END    = 0x03
STREAM_CANCEL = 0x04

# ACK statuses (single byte returned on the paired ACK pid).
ACK_OK     = 0x00
ACK_ERROR  = 0x01
ACK_CANCEL = 0x02


class StreamError(Exception):
    """Base class for stream-protocol errors."""


class StreamTimeoutError(StreamError):
    """No ACK arrived within the configured timeout."""


class StreamRemoteError(StreamError):
    """Receiver returned a non-OK ACK status."""
    def __init__(self, status):
        super().__init__("remote stream error: status={}".format(status))
        self.status = status


class LogicalFrame:
    """Bus-agnostic logical frame. Bus adapter encodes to wire."""
    __slots__ = ('is_multi', 'counter', 'data', 'declared_length')

    def __init__(self, is_multi, counter, data, declared_length=None):
        self.is_multi = is_multi
        self.counter = counter
        self.data = data
        self.declared_length = declared_length


class StreamFragmenter:
    """Pure-data: yields LogicalFrames from a payload.
    Knows nothing about the wire."""

    LENGTH_PREFIX_BYTES = 2  # uint16 carried implicitly via declared_length

    def __init__(self, frame_size, max_message_bytes=4096):
        self.frame_size = frame_size
        self.max_message_bytes = max_message_bytes

    def fragment(self, payload):
        """Yield LogicalFrames. May raise ValueError if payload too large."""
        n = len(payload)
        if n > self.max_message_bytes:
            raise ValueError(
                "payload {}B exceeds max_message_bytes={}".format(
                    n, self.max_message_bytes
                )
            )
        if n <= self.frame_size:
            yield LogicalFrame(is_multi=False, counter=0, data=payload)
            return

        first_data_room = self.frame_size - self.LENGTH_PREFIX_BYTES
        yield LogicalFrame(
            is_multi=True,
            counter=0,
            data=payload[:first_data_room],
            declared_length=n,
        )
        offset = first_data_room
        counter = 1
        while offset < n:
            chunk = payload[offset:offset + self.frame_size]
            yield LogicalFrame(
                is_multi=True,
                counter=counter % 8,
                data=chunk,
            )
            offset += len(chunk)
            counter += 1


class _BufferState:
    __slots__ = ('expected_counter', 'declared_length', 'buf', 'started_ms')

    def __init__(self, expected_counter, declared_length, buf, started_ms):
        self.expected_counter = expected_counter
        self.declared_length = declared_length
        self.buf = buf
        self.started_ms = started_ms


class StreamReassembler:
    """Pure-data: takes incoming LogicalFrames, returns complete
    payloads when ready. Per-sender buffers, timeout, concurrency cap.
    Knows nothing about the wire."""

    def __init__(self, max_message_bytes=4096, timeout_ms=1000,
                 max_concurrent=8):
        self.max_message_bytes = max_message_bytes
        self.timeout_ms = timeout_ms
        self.max_concurrent = max_concurrent
        self._buffers = {}

    def receive(self, sender_key, frame, now_ms):
        """Returns complete payload bytes or None (still building).

        sender_key: any hashable identifying the sender (typically
            (adr, pid) — caller's choice).
        frame: LogicalFrame.
        now_ms: monotonic milliseconds; used for timeout sweeps.
        """
        if not frame.is_multi:
            self._sweep_timeouts(now_ms)
            return bytes(frame.data)

        if frame.counter == 0:
            if frame.declared_length is None:
                return None
            if frame.declared_length > self.max_message_bytes:
                return None
            self._sweep_timeouts(now_ms)
            if len(self._buffers) >= self.max_concurrent:
                oldest_key = min(self._buffers,
                                 key=lambda k: self._buffers[k].started_ms)
                del self._buffers[oldest_key]
            self._buffers[sender_key] = _BufferState(
                expected_counter=1,
                declared_length=frame.declared_length,
                buf=bytearray(frame.data),
                started_ms=now_ms,
            )
            if len(self._buffers[sender_key].buf) >= frame.declared_length:
                return bytes(self._buffers.pop(sender_key).buf)
            return None

        state = self._buffers.get(sender_key)
        if state is None:
            return None
        if frame.counter != state.expected_counter:
            del self._buffers[sender_key]
            return None
        state.buf.extend(frame.data)
        state.expected_counter = (state.expected_counter + 1) % 8

        if len(state.buf) > state.declared_length:
            del self._buffers[sender_key]
            return None
        if len(state.buf) == state.declared_length:
            return bytes(self._buffers.pop(sender_key).buf)
        return None

    def _sweep_timeouts(self, now_ms):
        if not self._buffers:
            return
        stale = [k for k, s in self._buffers.items()
                 if (now_ms - s.started_ms) >= self.timeout_ms]
        for k in stale:
            del self._buffers[k]


class StreamSession:
    """Receiver-side state machine for an in-progress stream.

    Bus-agnostic. The bus adapter feeds reassembled payloads via
    `on_payload(payload)` and acts on the returned `(action, value)`
    decision. The session does *not* buffer chunks — it emits them one
    by one so the bus can stream-to-handler (for large payloads that
    don't fit in RAM) or buffer at the bus layer (for small payloads).

    Wire format (sender -> target on data pid):
        BEGIN:  [0x01][sender_adr:1B]
        CHUNK:  [0x02][chunk bytes...]
        END:    [0x03]
        CANCEL: [0x04]

    Wire format (target -> sender on ack pid):
        ACK:    [status:1B]   (0=OK, 1=ERROR, 2=CANCEL)
    """
    STATE_IDLE = 0
    STATE_RECEIVING = 1

    def __init__(self):
        self.state = self.STATE_IDLE
        self.sender_adr = None

    def on_payload(self, payload):
        """Returns (action, value):
            'begin'  -> stream started, sender_adr is now set; ACK OK
            'chunk'  -> chunk arrived, value=chunk bytes; ACK OK on success
            'end'    -> stream finished, value=None; ACK OK
            'cancel' -> stream cancelled, value=None; ACK CANCEL
            'error'  -> malformed/spurious, value=None; ACK ERROR (or drop
                        if sender_adr unknown — can't address an ACK back)
        """
        if not payload:
            return ('error', None)
        marker = payload[0]
        rest = bytes(payload[1:])
        if marker == STREAM_BEGIN:
            # BEGIN forgives any in-progress session (tolerates a stale
            # session left behind by a prior abort).
            if len(payload) < 2:
                return ('error', None)
            self.sender_adr = payload[1]
            self.state = self.STATE_RECEIVING
            return ('begin', None)
        if marker == STREAM_CHUNK:
            if self.state != self.STATE_RECEIVING:
                return ('error', None)
            return ('chunk', rest)
        if marker == STREAM_END:
            if self.state != self.STATE_RECEIVING:
                return ('error', None)
            self.state = self.STATE_IDLE
            return ('end', None)
        if marker == STREAM_CANCEL:
            self.state = self.STATE_IDLE
            return ('cancel', None)
        return ('error', None)


class Stream:
    """Caller-side ACK'd stream. Bus-agnostic stop-and-wait orchestrator.

    The bus adapter wires up two callables:
        send_marker_fn(marker_byte, data) -> awaitable[int]
            Sends a stream frame and returns the ACK status byte.
        close_fn() -> None
            Sync cleanup (e.g., deregister the pending-ack slot).

    Typical use as an async context manager:
        async with iris.bus.open_stream(adr=T, pid=X) as s:
            await s.write(b'chunk')
    """
    def __init__(self, send_marker_fn, close_fn):
        self._send_marker = send_marker_fn
        self._close = close_fn
        self._opened = False
        self._closed = False

    async def begin(self):
        if self._opened:
            raise StreamError("stream already opened")
        status = await self._send_marker(STREAM_BEGIN, b'')
        if status != ACK_OK:
            raise StreamRemoteError(status)
        self._opened = True

    async def write(self, data):
        if not self._opened:
            raise StreamError("stream not open")
        if self._closed:
            raise StreamError("stream already closed")
        status = await self._send_marker(STREAM_CHUNK, bytes(data))
        if status != ACK_OK:
            raise StreamRemoteError(status)

    async def drain(self):
        # Stop-and-wait: writes are drained on return. drain() exists
        # so callers can target a windowed-ACK API in the future without
        # changing call sites.
        return

    async def close(self):
        if self._closed:
            return
        try:
            if self._opened:
                try:
                    status = await self._send_marker(STREAM_END, b'')
                    if status != ACK_OK:
                        raise StreamRemoteError(status)
                except StreamTimeoutError:
                    # Receiver may already be gone; close locally anyway.
                    pass
        finally:
            self._closed = True
            self._close()

    async def cancel(self):
        if self._closed:
            return
        try:
            try:
                await self._send_marker(STREAM_CANCEL, b'')
            except (StreamTimeoutError, StreamRemoteError):
                pass
        finally:
            self._closed = True
            self._close()

    async def __aenter__(self):
        await self.begin()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            try:
                await self.cancel()
            except Exception:
                pass
        else:
            await self.close()
