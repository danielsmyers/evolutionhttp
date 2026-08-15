import asyncio
import logging
import re

import serial
import serial_asyncio_fast

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

_LOGGER = logging.getLogger(__name__)

def _is_write(cmd: str) -> bool:
    """Return whether cmd is a write."""
    return "!" in cmd

def _parse_temperature(response: Optional[str]) -> Optional[int]:
    if response is not None:
        m = re.match("([0-9]+)[A-Z]", response)  # E.g., 72F
        if m and m.group(1):
            return int(m.group(1))
    return None

class DevIO(ABC):
    @abstractmethod
    async def write(self, s: str) -> None:
        """Write a command to the device."""
        pass

    @abstractmethod
    async def read_next(self) -> str:
        """Read a response from the device."""
        pass

    async def drain(self) -> None:
        """Discard any input left over from a previous command.

        Called before starting a new command so that a late reply to an
        abandoned command cannot be mistaken for the reply to this one.
        The default is a no-op for I/O implementations without a buffer.
        """
        return None

    async def close(self) -> None:
        """Release the device. The default is a no-op."""
        return None

class ProdDevIO(DevIO, asyncio.Protocol):
    """I/O implementation for a real serial port.

    Reads are event-driven: the loop hands bytes to data_received().
    Nothing else reads the descriptor, so two readers cannot split a reply
    between them, and because no thread is involved there is no window in
    which received bytes are neither in the kernel nor in this object --
    which is what makes drain() a simple, complete operation.
    """

    # Cap on unterminated input. A floating or miswired line can stream a
    # constant byte indefinitely; random noise finds a 0x0A quickly, a stuck
    # 0x00 or 0xFF never does. Far above the protocol's 64-byte maximum.
    _max_buf_bytes = 4096

    def __init__(self, tty: str):
        self._tty = tty
        # Complete, non-empty lines waiting to be consumed.
        self._lines: asyncio.Queue = asyncio.Queue()
        # Bytes not yet forming a complete line.
        self._buf = b""
        self._transport: Optional[asyncio.Transport] = None
        # Set by close() so that the connection_lost() it causes is not
        # reported as the port having gone away on its own.
        self._closing = False

    async def open(self) -> None:
        """Open the port raw at 9600 8N1, with no flow control of any kind.

        Raw matters, and is why none of it is left to whatever last opened the
        port: in canonical mode the tty line discipline interprets received
        data, treating 0x15 as VKILL (erases the whole buffered line) and 0x7f
        as VERASE (deletes a character), so one corrupted byte on the wire
        becomes a multi-byte deletion in the frame we read. Framing is done in
        data_received() instead, where it is explicit and testable.

        pyserial writes the whole termios state rather than adjusting parts of
        the inherited one, so the flags that corrupt this protocol silently are
        cleared by construction: ICANON, ECHO, ISIG, IEXTEN, OPOST, ICRNL and
        ISTRIP all end up off. test_termios_is_correct asserts that, because it
        is something this protocol depends on rather than something to take on
        faith from a dependency.
        """
        self._closing = False
        self._transport, _ = await serial_asyncio_fast.create_serial_connection(
            asyncio.get_running_loop(),
            lambda: self,
            self._tty,
            baudrate=9600,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            # XON/XOFF: a stray 0x13 would stall the link until a 0x11 happened
            #   along, which reads as unexplained timeouts.
            xonxoff=False,
            # RTS/CTS: inherited hardware flow control on a 3-wire cable with
            #   RTS/CTS unwired blocks every write forever.
            rtscts=False,
            dsrdtr=False,
        )

    async def close(self) -> None:
        """Release the port. Closing the transport closes the descriptor."""
        # Flagged before the close, because closing the transport is what
        # triggers connection_lost().
        self._closing = True
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    def connection_lost(self, exc: Optional[Exception]) -> None:
        """asyncio.Protocol: the port went away (adapter unplugged, hangup).

        Deliberately does not reconnect. Every later command then costs the
        full retry ladder before returning None, until the owner closes and
        reopens. Warned about even when exc is None, because a clean hangup
        carries no exception and without a breadcrumb the symptom is just
        "everything times out" -- but only when the port went away by itself.
        close() causes this callback too, and warning there would mean every
        orderly shutdown cried wolf.
        """
        if self._closing:
            _LOGGER.debug("Connection to %s closed", self._tty)
            return
        _LOGGER.warning("Connection to %s lost: %s", self._tty, exc or "hangup")

    async def write(self, s: str) -> None:
        # CR/LF is the protocol's command terminator, written explicitly rather
        # than relying on the tty driver to expand a bare newline. The device
        # requires both: measured on the hardware, a bare LF or a bare CR gets
        # no reply at all.
        self._transport.write(f"{s}\r\n".encode("ascii"))

    def data_received(self, data: bytes) -> None:
        """asyncio.Protocol: assemble received bytes into lines."""
        # The driver does not translate line endings, so normalise here. The
        # device documents CR/LF but does not implement its own spec
        # faithfully, so accept a lone CR too. A CR arriving in one callback
        # ahead of its LF yields an empty line, which the skip below drops.
        self._buf += data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")

        # Extract complete lines BEFORE capping. Capping first would throw away
        # a good reply that happened to arrive alongside the junk -- a stuck
        # line recovering mid-command does exactly that -- and cost the command
        # its retries for nothing.
        while True:
            line, sep, rest = self._buf.partition(b"\n")
            if not sep:
                break
            self._buf = rest
            # errors="ignore" is load-bearing, not defensive: temperature
            # replies carry a degree byte (0xb0) that is not ASCII, and
            # dropping it here is what lets _parse_temperature see "72F".
            s = line.decode("ascii", errors="ignore").strip()
            if s:
                self._lines.put_nowait(s)

        # Whatever is left contains no terminator at all.
        if len(self._buf) > self._max_buf_bytes:
            _LOGGER.warning(
                "Discarding %d buffered bytes from %s with no line terminator",
                len(self._buf),
                self._tty,
            )
            self._buf = b""

    async def read_next(self) -> str:
        """Take the next assembled line. Cancelling this consumes nothing."""
        return await self._lines.get()

    async def drain(self) -> None:
        """Discard input belonging to a command we are no longer waiting on.

        A *complete* stale frame is the dangerous one: it starts with a command
        verb and could be taken for the answer to the next command, and reads
        and writes of one field share a verb prefix, so S1Z1HTSP! and
        S1Z1HTSP? cannot be told apart that way. Clearing the partial buffer
        then stops a stale head being glued to fresh bytes.

        Anything received is already here, because the loop delivers bytes
        directly; anything not yet received is still in the kernel,
        where tcflush reaches it. The only reply this cannot discard is one
        that begins arriving after the flush, which is indistinguishable from
        a fresh one.
        """
        discarded = 0
        while True:
            try:
                self._lines.get_nowait()
                discarded += 1
            except asyncio.QueueEmpty:
                break
        if discarded or self._buf:
            _LOGGER.debug(
                "drain: discarded %d queued line(s), %d buffered byte(s)",
                discarded,
                len(self._buf),
            )
        self._buf = b""

        if self._transport is None:
            return
        try:
            self._transport.serial.reset_input_buffer()
        except Exception as e:  # noqa: BLE001 - stand-in port (FIFO, plain file)
            _LOGGER.debug("Could not flush input on %s: %s", self._tty, e)


@dataclass(frozen=True)
class ZoneInfo:
    system_id: int
    zone_id: int
    name: str


class BryantEvolutionConnection:
    """A connection to one serial port.

    Owns the port, its reader, and the command queue, and is the only thing
    with a lifecycle: zones are views onto a connection and hold no resources
    of their own. That makes closing unambiguous -- the connection closes, and
    every zone that was using it stops working, which is exactly what the
    caller asked for.

    Sharing one port between zones is the caller holding one connection and
    making several zone views, not a side effect of naming the same device
    twice.
    """

    # Both values are sized against the round trip measured on a live SAM:
    # 850-1350ms from write to complete reply, which is dominated by the
    # device's own turnaround, not by the 9600-baud line. Replies also arrive
    # split across several reads, which is why data_received() reassembles.

    # How long to wait for a response from the device.
    #
    # A normal reply lands in ~1.5s, but the SAM is a bridge: when the furnace
    # does not answer it over the RS-485 bus, it sits through its own bus
    # timeout and only then reports failure with a bare NAK. Those replies were
    # measured arriving ~11s after the command.
    #
    # Timing out before that reply arrives is worse than waiting for it. The
    # retransmission goes out while the first answer is still coming, so every
    # subsequent read is one reply behind: each attempt collects the previous
    # attempt's NAK, which carries the right verb and so cannot be rejected,
    # and the command burns all three attempts to return None. Waiting instead
    # gets the NAK, classifies it transient, and retries from a clean state.
    #
    # 15s is the observed worst case plus margin rather than a figure from the
    # spec, and it is deliberately generous: too long merely makes a failure
    # slower, while too short reintroduces the cascade above.
    _timeout_sec = 15

    # How long to wait before retransmitting after a timeout or a bad response.
    # The SAM's ASCII port is documented as half-duplex, so retransmitting
    # immediately risks colliding with a reply still in progress. The spec
    # mandates no inter-command delay; this is set just above the slowest
    # observed round trip so a late reply lands before the retransmission.
    _retry_quiesce_sec = 1.5

    @classmethod
    async def open(cls, tty: str) -> "BryantEvolutionConnection":
        """Open the port at `tty`. The caller owns the result."""
        device = ProdDevIO(tty)
        await device.open()
        return cls(device)

    async def __aenter__(self) -> "BryantEvolutionConnection":
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.close()

    def zone(self, system_id: int, zone_id: int) -> "BryantEvolutionLocalClient":
        """Return a view bound to one system and zone. Holds no resources."""
        return BryantEvolutionLocalClient(system_id, zone_id, self)

    async def enumerate_zones(self, system_id: int) -> list[ZoneInfo]:
        """Return which zones exist for system_id on this connection."""
        zones: list[ZoneInfo] = []
        for zone_id in range(1, 9):  # the protocol addresses up to 8 zones
            zone = self.zone(system_id, zone_id)
            if (name := await zone.read_zone_name()) is not None:
                zones.append(
                    ZoneInfo(system_id=system_id, zone_id=zone_id, name=name)
                )
        return zones

    def __init__(self, device: DevIO):
        self._device = device
        self._pending_reads: list[tuple[str, asyncio.Future[str | None]]] = []
        self._pending_writes: list[tuple[str, asyncio.Future[str | None]]] = []
        self._work_available = asyncio.Event()
        self._consumer: Optional[asyncio.Task] = None
        self._closed = False

    def _ensure_consumer(self) -> None:
        # _consume() loops forever and handles its own exceptions, so it ends
        # only when close() cancels it -- and close() refuses further commands.
        if self._consumer is None:
            self._consumer = asyncio.create_task(
                self._consume(), name="evolutionhttp-consumer"
            )

    async def close(self) -> None:
        """Stop the consumer and release the device.

        Sets the closed flag before its first await, so a producer running in
        that window cannot queue work, respawn the consumer, and leave a live
        task driving the device after teardown.
        """
        if self._closed:
            return
        self._closed = True
        # Detach from the port first: transport.close() is synchronous, so the
        # old reader stops before this coroutine suspends. Otherwise a reopen
        # racing this teardown could attach a second reader to the same port.
        await self._device.close()
        if self._consumer is not None:
            self._consumer.cancel()
        # Resolve queued callers BEFORE the await below. If close() is itself
        # cancelled while waiting, _closed is already set, so nothing else
        # would ever resolve them and they would wait forever.
        for queue in (self._pending_writes, self._pending_reads):
            while queue:
                _cmd, fut = queue.pop(0)
                if not fut.done():
                    fut.set_result(None)
        if self._consumer is not None:
            # wait() reports completion without re-raising the consumer's
            # CancelledError, while a cancellation of *this* task still
            # propagates.
            await asyncio.wait([self._consumer])
            self._consumer = None

    async def _send_command(self, cmd: str) -> str | None:
        if self._closed:
            # A poll racing a shutdown is a protocol failure, not a crash.
            _LOGGER.debug("Command %s issued after close()", cmd)
            return None
        fut = asyncio.get_running_loop().create_future()
        if _is_write(cmd):
            self._pending_writes.append((cmd, fut))
        else:
            self._pending_reads.append((cmd, fut))
        self._ensure_consumer()
        self._work_available.set()
        return await fut

    async def _consume(self) -> None:
        """Run queued commands, one at a time, forever.

        Commands are executed here rather than by whichever caller queued
        them, so callers are pure waiters: cancelling one cannot strand
        another caller's command.

        A plain lock around each command would give that much. The queue is
        here for the other half -- writes jump ahead of reads.
        A user's setpoint change would otherwise wait behind a poll burst that
        can be eight commands deep at up to ~21s each, which a lock cannot
        express because it has no idea what is waiting behind it.
        """
        while True:
            queue = self._pending_writes or self._pending_reads
            work = queue.pop(0) if queue else None
            if work is None:
                # No await between the check and the clear, so a producer
                # cannot append in between and have its wakeup discarded.
                self._work_available.clear()
                await self._work_available.wait()
                continue
            (cmd, fut) = work
            if fut.done():
                # The caller was cancelled while this sat in the queue. Sending
                # it anyway would apply a write nobody wants and hold the queue
                # for up to three timeouts for a result nobody will read.
                continue
            try:
                await self._run_command(cmd, fut)
            except asyncio.CancelledError:
                # Resolve rather than cancel: the caller awaiting this future
                # is a different task that nobody cancelled, and None is the
                # documented protocol-error result.
                if not fut.done():
                    fut.set_result(None)
                raise
            except Exception as e:
                _LOGGER.error(
                    "Unhandled exception processing command %s: %s",
                    cmd,
                    e,
                    exc_info=True,
                )
                # Resolve rather than raise: a transport failure is exactly the
                # "protocol error" the public methods document as None. The
                # consumer stays alive; one bad command must not stop the queue.
                if not fut.done():
                    fut.set_result(None)

    @staticmethod
    def _classify(cmd_verb: str, response: str) -> tuple[str, Optional[str]]:
        """Classify a reply as ok, refused, transient, or bad.

        A NAK is matched on the value rather than by searching the whole frame:
        the protocol's refusals are "NAK", "NAK CMD" and "NAK VAL", but a
        substring test also fires on data that merely contains those letters --
        a zone named "SNAKE ROOM" would be read as a refusal and its zone would
        vanish from enumeration.
        """
        prefix = cmd_verb + ":"
        if not response.startswith(prefix):
            return "bad", None
        value = response[len(prefix):]
        # The protocol has three refusals and they do not mean the same thing:
        #   NAK CMD  unknown command -- also what a zone that does not exist
        #            returns, which is most of what enumeration asks about
        #   NAK VAL  bad parameter
        #   NAK      the module could not reach the equipment, or timed out
        #            building a reply
        # The first two are verdicts about the command itself and will not
        # change on a retry. The bare NAK is the module reporting a transient
        # failure behind it, and is worth asking again.
        if value.startswith("NAK CMD") or value.startswith("NAK VAL"):
            return "refused", None
        if value == "NAK" or value.startswith("NAK "):
            return "transient", None
        return "ok", value

    async def _run_command(self, cmd: str, fut: asyncio.Future) -> None:
        cmd_verb = cmd.split("!")[0] if _is_write(cmd) else cmd.split("?")[0]

        # Discard anything the device sent after we gave up on a previous
        # command, so a late reply cannot be read as the answer to this one.
        await self._device.drain()

        response = None
        # A pause before retransmitting is only warranted when the device may
        # still be transmitting: the port is half-duplex, so putting a command
        # on the wire during a reply corrupts it. Any reply at all, even a
        # refusal, proves the device has finished.
        quiesce_first = False
        for attempt in range(3):
            if fut.done():
                # Caller cancelled. Any reply already on its way is discarded
                # by the next command's drain(), like any other late reply.
                return
            if quiesce_first:
                await asyncio.sleep(self._retry_quiesce_sec)
            quiesce_first = False
            await self._device.write(cmd)

            try:
                async with asyncio.timeout(self._timeout_sec):
                    r = await self._device.read_next()
            except TimeoutError:
                _LOGGER.warning(
                    "Timeout for command %s (attempt %d/3)", cmd, attempt + 1
                )
                quiesce_first = True
                continue

            verdict, value = self._classify(cmd_verb, r)
            if verdict == "ok":
                response = value
                break
            if verdict == "refused":
                # A verdict about the command, not a transport failure. Asking
                # again cannot change it.
                _LOGGER.debug("Device refused command %s: '%s'", cmd, r)
                break
            if verdict == "bad":
                _LOGGER.warning(
                    "Bad or unexpected response to command %s: '%s'", cmd, r
                )
                # The exchange went wrong on the wire, so the device may still
                # be transmitting. Pause before retransmitting into it.
                quiesce_first = True
            else:
                # A bare NAK: the device is fine, something behind it was not.
                # It has finished replying, so retry without the pause.
                _LOGGER.debug("Transient failure reported for %s: '%s'", cmd, r)

        if not fut.done():
            fut.set_result(response)


class BryantEvolutionLocalClient:
    """One system and zone on a BryantEvolutionConnection.

    Read and set the HVAC parameters of a single zone. This is a view: it owns
    nothing and has no close(). Obtain one from BryantEvolutionConnection.zone()
    and close the connection when finished with it.

    All read methods return None on protocol errors (e.g. timeout), and also
    when the device refuses the command.

    On a non-successful set_* call, the set may or may not have occurred.
    """

    def __init__(
        self,
        system_id: int,
        zone_id: int,
        connection: "BryantEvolutionConnection",
    ):
        self._system_id = system_id
        self._zone_id = zone_id
        self._connection = connection
        # Every zone-scoped command starts with this.
        self._prefix = f"S{system_id}Z{zone_id}"

    async def _send(self, cmd: str) -> Optional[str]:
        return await self._connection._send_command(cmd)

    async def read_zone_name(self) -> Optional[str]:
        """Reads the zone's name."""
        return await self._send(f"{self._prefix}NAME?")

    async def read_current_temperature(self) -> Optional[int]:
        """Reads the current temperature."""
        return _parse_temperature(await self._send(f"{self._prefix}RT?"))

    async def read_cooling_setpoint(self) -> Optional[int]:
        """Reads the current cooling setpoint."""
        return _parse_temperature(await self._send(f"{self._prefix}CLSP?"))

    async def set_cooling_setpoint(self, temperature: int) -> bool:
        """Sets the cooling setpoint."""
        return await self._send(f"{self._prefix}CLSP!{int(temperature)}") == "ACK"

    async def read_heating_setpoint(self) -> Optional[int]:
        """Gets the heating setpoint."""
        return _parse_temperature(await self._send(f"{self._prefix}HTSP?"))

    async def set_heating_setpoint(self, temperature: int) -> bool:
        """Sets the heating setpoint."""
        return await self._send(f"{self._prefix}HTSP!{int(temperature)}") == "ACK"

    async def read_fan_mode(self) -> Optional[str]:
        """Reads the fan mode."""
        return await self._send(f"{self._prefix}FAN?")

    async def set_fan_mode(self, fan_mode: str) -> bool:
        """Sets the fan mode."""
        return await self._send(f"{self._prefix}FAN!{fan_mode}") == "ACK"

    async def read_hvac_mode(self) -> Optional[tuple[str, bool]]:
        """Reads the HVAC mode (heat, cool, etc).

        Returns the mode and whether the system is active. Mode belongs to the
        system rather than the zone, so it carries no zone in its command.
        """
        response = await self._send(f"S{self._system_id}MODE?")
        if not response:
            return None
        # A system that is not idle answers with the mode and the number of
        # active stages, space-separated; an idle one answers with the mode
        # alone.
        m = re.match("([A-Z]+)[ ]*([0-9]?)", response)
        if not m:
            _LOGGER.error("Unparseable mode: %s", response)
            return None
        return (m.group(1), bool(m.group(2)))

    async def set_hvac_mode(self, hvac_mode: str) -> bool:
        """Sets the HVAC mode. Belongs to the system rather than the zone."""
        if hvac_mode == "heat_cool":
            hvac_mode = "AUTO"
        return await self._send(f"S{self._system_id}MODE!{hvac_mode.upper()}") == "ACK"
