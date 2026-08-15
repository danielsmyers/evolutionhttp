import asyncio
import contextlib
import os
import tempfile
import termios
import pty
import unittest

from unittest.mock import patch

from src.evolutionhttp import (
    BryantEvolutionConnection,
    BryantEvolutionLocalClient,
    ZoneInfo,
)
from src.evolutionhttp.local_client import DevIO, ProdDevIO, _parse_temperature

_LOG_NAME = "src.evolutionhttp.local_client"


class FakePort:
    """A real pty standing in for the serial port.

    The slave is a genuine character device, so ProdDevIO opens it, configures
    termios and drives asyncio transports exactly as it would in the field; the
    master end plays the device. Nothing about the I/O path is mocked.
    """

    def __init__(self):
        self._master, slave = pty.openpty()
        self.path = os.ttyname(slave)
        os.close(slave)
        os.set_blocking(self._master, False)
        self._inbox = b""

    def send(self, data: bytes) -> None:
        """Speak as the device."""
        os.write(self._master, data)

    async def next_command(self, timeout: float = 5.0) -> str:
        """Wait for the client to put a terminated command on the wire."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            try:
                self._inbox += os.read(self._master, 256)
            except BlockingIOError:
                pass
            if b"\r\n" in self._inbox:
                cmd, _, self._inbox = self._inbox.partition(b"\r\n")
                return cmd.decode()
            await asyncio.sleep(0.01)
        raise AssertionError("client sent no command within %ss" % timeout)

    def close(self) -> None:
        """Idempotent: a test that unplugs the device still gets cleaned up."""
        if self._master is not None:
            os.close(self._master)
            self._master = None




async def _wait_until(predicate, timeout=5.0):
    """Await a condition rather than guessing with a fixed sleep."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not met within %ss" % timeout)


class FakeDevIO(DevIO):
    def __init__(self):
        self._state = {
            "S1Z1RT": "72\xb0F",
            "S1Z1FAN": "AUTO",
            "S1MODE": "HEAT",
            "S1Z1CLSP": "75\xb0F",
            "S1Z1HTSP": "70\xb0F",
            "S2MODE": "COOL 1",
            "S2Z2CLSP": "60\xb0F",
            "S1Z1NAME": "S1Z1",
            "S2Z2NAME": "S2Z2"
        }
        self._next_resp = None
        self._allow_reads = True
        self._allow_reads_cond = asyncio.Condition()

    async def _set_allow_reads(self, b: bool) -> None:
        async with self._allow_reads_cond:
            self._allow_reads = b
            self._allow_reads_cond.notify_all()

    def _are_reads_allowed(self) -> bool:
        return self._allow_reads

    async def write(self, cmd: str) -> None:
        if "!" in cmd:
            # Handle write
            key, val = cmd.split("!")
            if key.endswith("SP"):
                val = val + "\xb0F"  # Add unit suffix
            self._state[key] = val
            self._next_resp = f"{key}:ACK"
        else:
            # Handle read
            key = cmd.split("?")[0]
            if key not in self._state:
                # What the real module answers for a zone that is not present.
                self._next_resp = f"{key}:NAK CMD"
            else:
                self._next_resp = f"{key}:{self._state[key]}".encode(
                    "ascii", errors="ignore"
                ).decode()

    async def read_next(self) -> str:
        async with self._allow_reads_cond:
            await self._allow_reads_cond.wait_for(self._are_reads_allowed)
            r = self._next_resp
            self._next_resp = None
            return r


class TestBryantEvolutionLocalClient(unittest.IsolatedAsyncioTestCase):
    async def test_write_reordered(self):
        """A write is executed before reads that are queued but not yet started.

        BEHAVIOUR CHANGE. Previously the first caller ran its own command
        inline before any later caller could queue one, so a read issued first
        completed against the pre-write value. With a dedicated consumer task,
        commands submitted in the same tick are all queued before the consumer
        starts, and the writes-first rule then orders the write ahead of both
        reads -- so both reads observe the written value.

        Prioritising writes is the documented intent; what changed is that it
        now applies consistently, rather than depending on which caller
        happened to be scheduled first.
        """
        io = FakeDevIO()
        conn = BryantEvolutionConnection(io)
        self.addAsyncCleanup(conn.close)
        await conn._send_command("S1Z1HTSP!72")
        await io._set_allow_reads(False)
        t1 = asyncio.create_task(conn._send_command("S1Z1HTSP?"))
        t2 = asyncio.create_task(conn._send_command("S1Z1HTSP?"))
        t3 = asyncio.create_task(conn._send_command("S1Z1HTSP!75"))

        # Add a manual yield point. Otherwise, t1-t3 won't start executing until we hit
        # the "await t1" below, which means we will have *already* set io.set_allow_reads(True),
        # which would defeat the point of the test.
        await asyncio.sleep(0)
        await io._set_allow_reads(True)
        assert await t3 == "ACK"
        assert await t1 == "75F"
        assert await t2 == "75F"

    async def test_client_interactions(self):
        """Test basics reads and writes."""
        conn = BryantEvolutionConnection(FakeDevIO())
        self.addAsyncCleanup(conn.close)
        client = conn.zone(1, 1)

        # Test getting values
        current_temp = await client.read_current_temperature()
        fan_mode = await client.read_fan_mode()
        hvac_mode = await client.read_hvac_mode()
        cooling_setpoint = await client.read_cooling_setpoint()
        heating_setpoint = await client.read_heating_setpoint()

        # Assertions for initial values
        self.assertEqual(current_temp, 72)
        self.assertEqual(fan_mode, "AUTO")
        self.assertEqual(hvac_mode, ("HEAT", False))
        self.assertEqual(cooling_setpoint, 75)
        self.assertEqual(heating_setpoint, 70)
        self.assertEqual(await client.read_zone_name(), "S1Z1")

        # Test setting values
        self.assertTrue(await client.set_fan_mode("LOW"))
        self.assertTrue(await client.set_cooling_setpoint(78))
        self.assertTrue(await client.set_heating_setpoint(68))
        self.assertTrue(await client.set_hvac_mode("COOL"))

        self.assertEqual(await client.read_fan_mode(), "LOW")
        self.assertEqual(await client.read_cooling_setpoint(), 78)
        self.assertEqual(await client.read_heating_setpoint(), 68)
        self.assertEqual(await client.read_hvac_mode(), ("COOL", False))

        # Test error handling (invalid command)
        result = await conn._send_command("INVALID_COMMAND")
        self.assertEqual(result, None)

    async def test_second_system(self):
        """Test working with S2 instead of S1."""
        conn = BryantEvolutionConnection(FakeDevIO())
        self.addAsyncCleanup(conn.close)
        client = conn.zone(2, 2)
        self.assertEqual(await client.read_hvac_mode(), ("COOL", True))
        self.assertEqual(await client.read_cooling_setpoint(), 60)
        self.assertEqual(await client.read_zone_name(), "S2Z2")


    async def test_timeout(self):
        """Test timeout handling."""
        io = FakeDevIO()
        conn = BryantEvolutionConnection(io)
        self.addAsyncCleanup(conn.close)
        await io._set_allow_reads(False)
        with patch.object(BryantEvolutionConnection, "_timeout_sec", 0.1), patch.object(
            BryantEvolutionConnection, "_retry_quiesce_sec", 0.01
        ):
            assert not await conn.zone(1, 1).read_heating_setpoint()

    async def test_cancelling_a_caller_does_not_run_the_queue(self):
        """Cancelling a caller must return promptly and execute nothing.

        Under the previous arrangement the task that queued work also ran it,
        so a cancelled caller drained the rest of the queue inside its own
        teardown: `cancel(); await task` blocked for as long as the queue was,
        at roughly 21s per queued command against an unresponsive device. A
        dedicated consumer makes the caller a pure waiter.
        """
        io = FakeDevIO()
        core = BryantEvolutionConnection(io)
        self.addAsyncCleanup(core.close)
        await io._set_allow_reads(False)

        t1 = asyncio.create_task(core._send_command("S1Z1HTSP?"))
        t2 = asyncio.create_task(core._send_command("S1Z1RT?"))
        await asyncio.sleep(0.05)

        loop = asyncio.get_running_loop()
        start = loop.time()
        t1.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(t1, timeout=5)
        elapsed = loop.time() - start

        self.assertLess(
            elapsed, 1.0, f"cancelling a caller took {elapsed:.1f}s of queue work"
        )
        t2.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await t2

    async def test_cancelling_consumer_resolves_inflight_command(self):
        """Cancelling the consumer must resolve the waiting caller, not cancel it.

        The caller is a different task that nobody cancelled, so raising
        CancelledError inside it would be wrong. None is the documented
        protocol-error result.
        """
        io = FakeDevIO()
        core = BryantEvolutionConnection(io)
        self.addAsyncCleanup(core.close)
        await io._set_allow_reads(False)

        t = asyncio.create_task(core._send_command("S1Z1HTSP?"))
        await _wait_until(lambda: core._consumer is not None)
        await asyncio.sleep(0.05)

        core._consumer.cancel()
        self.assertIsNone(await asyncio.wait_for(t, timeout=5))

    async def test_command_after_close_returns_none(self):
        """A poll racing shutdown is a protocol failure, not a crash.

        Without the closed flag the command is queued, a consumer is respawned
        to run it, and it succeeds -- returning a reading from a client the
        caller has already shut down.
        """
        core = BryantEvolutionConnection(FakeDevIO())
        await core.close()
        self.assertIsNone(await core._send_command("S1Z1HTSP?"))
        self.assertIsNone(core._consumer, "close() left a consumer behind")

    async def test_value_containing_nak_is_not_a_refusal(self):
        """NAK must be matched on the value, not searched for in the frame.

        A substring test also fires on data that merely contains the letters,
        so a zone named "SNAKE ROOM" would be treated as a refusal, retried
        three times, and then vanish from enumeration.
        """
        io = FakeDevIO()
        io._state["S1Z1NAME"] = "SNAKE ROOM"
        core = BryantEvolutionConnection(io)
        self.addAsyncCleanup(core.close)
        self.assertEqual(await core.zone(1, 1).read_zone_name(), "SNAKE ROOM")

    async def test_transient_nak_retries_without_the_half_duplex_pause(self):
        """A bare NAK means the device finished replying, so do not pause.

        The pause exists to avoid retransmitting into a reply still in
        progress. Any reply at all proves there is none, so a transient NAK
        should be retried immediately -- with the pause it would cost seconds
        per occurrence.
        """
        port, io = await self._open_port()
        conn = BryantEvolutionConnection(io)
        self.addAsyncCleanup(conn.close)

        with patch.object(BryantEvolutionConnection, "_retry_quiesce_sec", 30):
            task = asyncio.create_task(conn.zone(1, 1).read_heating_setpoint())
            self.assertEqual(await port.next_command(), "S1Z1HTSP?")
            port.send(b"S1Z1HTSP:NAK\r\n")
            # If the pause applied, the retransmit would be 30s away.
            self.assertEqual(await port.next_command(timeout=2), "S1Z1HTSP?")
            port.send(b"S1Z1HTSP:70\xb0F\r\n")
            self.assertEqual(await asyncio.wait_for(task, 5), 70)

    async def test_enumerate(self):
        """Test that enumerating zones works."""
        io = FakeDevIO()
        io._state = {
            "S1Z1NAME": "S1Z1",
            "S1Z3NAME": "S1Z3",
            "S2Z7NAME": "S2Z7",
            "S2Z4NAME": "S2Z4",
        }
        conn = BryantEvolutionConnection(io)
        self.addAsyncCleanup(conn.close)
        self.assertEqual(
            await conn.enumerate_zones(1),
            [
                ZoneInfo(system_id=1, zone_id=1, name="S1Z1"),
                ZoneInfo(system_id=1, zone_id=3, name="S1Z3"),
            ],
        )
        self.assertEqual(
            await conn.enumerate_zones(2),
            [
                ZoneInfo(system_id=2, zone_id=4, name="S2Z4"),
                ZoneInfo(system_id=2, zone_id=7, name="S2Z7"),
            ],
        )

    # ---- tests against a real serial port (pty) ----

    async def _open_port(self):
        port = FakePort()
        self.addCleanup(port.close)
        io = ProdDevIO(port.path)
        await io.open()
        self.addAsyncCleanup(io.close)
        return port, io

    async def test_writes_crlf_terminator_to_the_wire(self):
        """The protocol's terminator is CR/LF and nothing else adds it.

        Output post-processing is disabled, so if write() did not emit the
        terminator itself the device would never see one and every command
        would time out.
        """
        port, io = await self._open_port()
        await io.write("S1Z1HTSP?")
        self.assertEqual(await port.next_command(), "S1Z1HTSP?")

    async def test_accepts_crlf_and_bare_cr_terminators(self):
        """Framing is done in _feed(), not by the tty driver.

        The device documents CR/LF but does not implement its own spec
        faithfully, and input translation is disabled, so both must work here.
        """
        port, io = await self._open_port()
        port.send(b"S1Z1RT:70\xb0F\r\n")
        self.assertEqual(await asyncio.wait_for(io.read_next(), 5), "S1Z1RT:70F")
        port.send(b"S1Z1FAN:AUTO\r")
        self.assertEqual(await asyncio.wait_for(io.read_next(), 5), "S1Z1FAN:AUTO")

    async def test_frame_split_across_deliveries_is_reassembled(self):
        """Bytes arrive when they arrive; a frame may span several callbacks."""
        port, io = await self._open_port()
        port.send(b"S1Z1H")
        await asyncio.sleep(0.05)
        port.send(b"TSP:")
        await asyncio.sleep(0.05)
        port.send(b"97\xb0F\r\n")
        self.assertEqual(await asyncio.wait_for(io.read_next(), 5), "S1Z1HTSP:97F")

    async def test_a_good_frame_survives_the_junk_it_arrives_with(self):
        """The unterminated-input cap must not eat a complete reply.

        A stuck line recovering mid-command delivers junk and then a valid
        frame. Capping before extracting lines would discard both and burn the
        command's retries for nothing. Fed directly rather than through a port,
        so the two arrive in one delivery deterministically.
        """
        io = ProdDevIO("unused")
        io.data_received(
            b"\xff" * (io._max_buf_bytes + 100) + b"S1Z1RT:70\xb0F\r\n"
        )
        self.assertEqual(
            await asyncio.wait_for(io.read_next(), 5),
            "S1Z1RT:70F",
            "the cap discarded a complete frame along with the noise",
        )
        self.assertEqual(io._buf, b"", "junk was left buffered")

    async def test_drain_discards_a_queued_stale_frame(self):
        """A complete frame nobody consumed must not answer the next command.

        That is the dangerous shape: a whole frame starts with a command verb,
        and reads and writes of one field share a prefix (S1Z1HTSP! and
        S1Z1HTSP? both reduce to S1Z1HTSP), so the verb check cannot reject it.
        """
        port, io = await self._open_port()
        port.send(b"S1Z1RT:70\xb0F\r\n")
        await _wait_until(lambda: not io._lines.empty())
        await io.drain()
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(io.read_next(), 0.3)

    async def test_drain_discards_a_partial_frame(self):
        """A half-assembled frame must not be glued to the next reply."""
        port, io = await self._open_port()
        port.send(b"S1Z1RT:7")
        await _wait_until(lambda: io._buf == b"S1Z1RT:7")
        await io.drain()
        port.send(b"S1Z1FAN:AUTO\r\n")
        self.assertEqual(
            await asyncio.wait_for(io.read_next(), 5),
            "S1Z1FAN:AUTO",
            "the discarded head was spliced onto the next reply",
        )

    async def test_termios_is_actually_applied(self):
        """Echo in particular must be off: the port would otherwise return our
        own commands as if they were replies."""
        port, io = await self._open_port()
        attrs = termios.tcgetattr(io._read_transport.get_extra_info("pipe").fileno())
        iflag, oflag, cflag, lflag = attrs[0], attrs[1], attrs[2], attrs[3]
        self.assertFalse(lflag & termios.ECHO, "echo left on")
        self.assertFalse(lflag & termios.ICANON, "canonical mode left on")
        self.assertFalse(
            iflag & termios.ISTRIP,
            "ISTRIP turns the degree byte 0xb0 into '0', so 72 reads back as 720",
        )
        self.assertFalse(iflag & (termios.IXON | termios.IXOFF), "flow control left on")
        self.assertFalse(iflag & termios.ICRNL, "driver would rewrite terminators")
        self.assertFalse(oflag & termios.OPOST, "driver would rewrite our terminator")
        self.assertFalse(cflag & getattr(termios, "CRTSCTS", 0), "RTS/CTS left on")

    def test_istrip_would_yield_a_plausible_wrong_temperature(self):
        """Why ISTRIP is cleared rather than left as inherited.

        The degree byte measured on the wire is 0xb0. Clearing bit 7 makes it
        0x30, i.e. '0' -- valid ASCII, so it survives the decode, the digits
        still parse, and the caller is handed 720 instead of nothing.
        """
        reply = "72\xb0F".encode("latin-1")
        intact = reply.decode("ascii", errors="ignore")
        stripped = bytes(b & 0x7F for b in reply).decode("ascii")
        self.assertEqual(_parse_temperature(intact), 72)
        self.assertEqual(
            _parse_temperature(stripped),
            720,
            "if this is no longer a wrong number, revisit the ISTRIP comment",
        )

    async def test_orderly_close_is_not_reported_as_a_lost_port(self):
        """Shutting down must not look like the adapter being unplugged.

        close() causes connection_lost(), so without telling the two apart a
        routine reload logs the same warning as a real hangup, and the warning
        stops carrying information.
        """
        _port, io = await self._open_port()
        with self.assertNoLogs(_LOG_NAME, level="WARNING"):
            await io.close()
            await asyncio.sleep(0)  # let the transport deliver connection_lost

    async def test_a_port_that_goes_away_is_still_reported(self):
        """The warning must survive for the case it exists for."""
        port, io = await self._open_port()
        with self.assertLogs(_LOG_NAME, level="WARNING") as captured:
            port.close()  # the device end disappears
            await _wait_until(lambda: bool(captured.records))
        self.assertIn("lost", captured.records[0].getMessage())

    async def test_close_releases_the_port(self):
        """Closing must release the descriptors, not merely stop reading."""
        port, io = await self._open_port()
        fd = io._read_transport.get_extra_info("pipe").fileno()
        await io.close()
        await asyncio.sleep(0)  # transports close their file on the next tick
        with self.assertRaises(OSError, msg="descriptor was not released"):
            os.fstat(fd)
        # A second close is a no-op rather than an error.
        await io.close()

    async def test_connection_opens_zones_and_closes_the_port(self):
        """The connection owns the port; zones are views that own nothing."""
        port = FakePort()
        self.addCleanup(port.close)

        conn = await BryantEvolutionConnection.open(port.path)
        z1 = conn.zone(1, 1)
        z2 = conn.zone(1, 2)
        self.assertIs(z1._connection, z2._connection, "zones should share the connection")
        self.assertFalse(
            hasattr(z1, "close"), "a view must not offer a lifecycle it does not own"
        )

        fd = conn._device._read_transport.get_extra_info("pipe").fileno()
        await conn.close()
        await asyncio.sleep(0)
        with self.assertRaises(OSError, msg="the port was not released"):
            os.fstat(fd)

    async def test_connection_is_an_async_context_manager(self):
        """Enumeration during a config flow should not leak the port."""
        port = FakePort()
        self.addCleanup(port.close)

        conn = await BryantEvolutionConnection.open(port.path)
        fd = conn._device._read_transport.get_extra_info("pipe").fileno()
        async with conn:
            self.assertIsNotNone(conn.zone(1, 1))
        await asyncio.sleep(0)
        with self.assertRaises(OSError, msg="the port outlived the with block"):
            os.fstat(fd)

    async def test_failed_open_leaks_nothing(self):
        """A port that cannot be opened leaves no state to clean up.

        With no registry there is nothing to poison: the caller simply gets an
        error and holds no connection.
        """
        with self.assertRaises(OSError):
            await BryantEvolutionConnection.open("/dev/nonexistent-evolutionhttp")
        # And a later attempt behaves identically rather than inheriting state.
        with self.assertRaises(OSError):
            await asyncio.wait_for(
                BryantEvolutionConnection.open("/dev/nonexistent-evolutionhttp"), 5
            )

    async def test_set_and_read_over_a_real_port(self):
        """The ordinary round trip, end to end over a character device."""
        port, io = await self._open_port()
        core = BryantEvolutionConnection(io)
        self.addAsyncCleanup(core.close)

        task = asyncio.create_task(core.zone(1, 1).set_heating_setpoint(97))
        self.assertEqual(await port.next_command(), "S1Z1HTSP!97")
        port.send(b"S1Z1HTSP:ACK\r\n")
        self.assertTrue(await asyncio.wait_for(task, 5))

        task = asyncio.create_task(core.zone(1, 1).read_heating_setpoint())
        self.assertEqual(await port.next_command(), "S1Z1HTSP?")
        port.send(b"S1Z1HTSP:97\xb0F\r\n")
        self.assertEqual(await asyncio.wait_for(task, 5), 97)

    async def test_retries_a_frame_that_lacks_our_verb(self):
        """A reply not carrying our verb is a transport failure, so retry.

        The retried command must also succeed, which means the garbled frame
        did not leave the stream one reply out of step.
        """
        port, io = await self._open_port()
        core = BryantEvolutionConnection(io)
        self.addAsyncCleanup(core.close)

        with patch.object(BryantEvolutionConnection, "_retry_quiesce_sec", 0.01):
            task = asyncio.create_task(core.zone(1, 1).read_heating_setpoint())
            self.assertEqual(await port.next_command(), "S1Z1HTSP?")
            port.send(b"1Z1HTSP:70\xb0F\r\n")  # leading byte lost
            self.assertEqual(await port.next_command(), "S1Z1HTSP?")
            port.send(b"S1Z1HTSP:70\xb0F\r\n")
            self.assertEqual(await asyncio.wait_for(task, 5), 70)

    async def test_command_cancelled_while_queued_is_never_sent(self):
        """A caller that gives up before its command starts must not have it
        transmitted -- for a write, that would apply a change nobody wants."""
        port, io = await self._open_port()
        core = BryantEvolutionConnection(io)
        self.addAsyncCleanup(core.close)

        # Occupy the consumer so the second command only ever sits queued.
        first = asyncio.create_task(core.zone(1, 1).read_current_temperature())
        self.assertEqual(await port.next_command(), "S1Z1RT?")
        queued = asyncio.create_task(core.zone(1, 1).set_heating_setpoint(99))
        await asyncio.sleep(0.05)
        queued.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await queued

        port.send(b"S1Z1RT:70\xb0F\r\n")
        self.assertEqual(await asyncio.wait_for(first, 5), 70)

        with self.assertRaises(AssertionError):
            await port.next_command(timeout=0.5)

    async def test_permanent_refusal_is_not_retried(self):
        """NAK CMD and NAK VAL are verdicts about the command, not transport.

        A zone that does not exist answers NAK CMD, and enumeration asks about
        eight of them, so retrying a refusal costs two extra round trips per
        absent zone for an answer that cannot change.
        """
        port, io = await self._open_port()
        core = BryantEvolutionConnection(io)
        self.addAsyncCleanup(core.close)

        task = asyncio.create_task(core.zone(1, 5).read_zone_name())
        self.assertEqual(await port.next_command(), "S1Z5NAME?")
        port.send(b"S1Z5NAME:NAK CMD\r\n")
        self.assertIsNone(await asyncio.wait_for(task, 5))
        with self.assertRaises(AssertionError):
            await port.next_command(timeout=0.5)

    async def test_bare_nak_is_retried(self):
        """A bare NAK is the module reporting a failure behind it, not a
        verdict on the command, so asking again can succeed."""
        port, io = await self._open_port()
        core = BryantEvolutionConnection(io)
        self.addAsyncCleanup(core.close)

        task = asyncio.create_task(core.zone(1, 1).read_heating_setpoint())
        self.assertEqual(await port.next_command(), "S1Z1HTSP?")
        port.send(b"S1Z1HTSP:NAK\r\n")
        self.assertEqual(await port.next_command(), "S1Z1HTSP?")
        port.send(b"S1Z1HTSP:70\xb0F\r\n")
        self.assertEqual(await asyncio.wait_for(task, 5), 70)
