import aiofiles
import asyncio
import logging
import mock
import unittest

from src.evolutionhttp import BryantEvolutionLocalClient, _CoreClient, ZoneInfo
from mock import patch

_LOGGER = logging.getLogger(__name__)

aiofiles.threadpool.wrap.register(mock.MagicMock)(
    lambda *args, **kwargs: aiofiles.threadpool.AsyncBufferedIOBase(*args, **kwargs)
)


class FakeDevIO(_CoreClient.DevIO):
    def __init__(self):
        self._state = {
            "S1Z1RT": "72\xf8F",
            "S1Z1FAN": "AUTO",
            "S1MODE": "HEAT",
            "S1Z1CLSP": "75\xf8F",
            "S1Z1HTSP": "70\xf8F",
            "S2MODE": "COOL 1",
            "S2Z2CLSP": "60\xf8F",
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
                val = val + "\xf8F"  # Add unit suffix
            self._state[key] = val
            self._next_resp = f"{key}:ACK"
        else:
            # Handle read
            key = cmd.split("?")[0]
            if key not in self._state:
                self._next_resp = f"{key}:NAK"
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
        """Test that a write that arrives while a read is already pending is executed before the read."""
        io = FakeDevIO()
        client = BryantEvolutionLocalClient(1, 1, _CoreClient(io))
        await client._client._send_command("S1Z1HTSP!72")
        await io._set_allow_reads(False)
        t1 = asyncio.create_task(client._client._send_command("S1Z1HTSP?"))
        t2 = asyncio.create_task(client._client._send_command("S1Z1HTSP?"))
        t3 = asyncio.create_task(client._client._send_command("S1Z1HTSP!75"))

        # Add a manual yield point. Otherwise, t1-t3 won't start executing until we hit
        # the "await t1" below, which means we will have *already* set io.set_allow_reads(True),
        # which would defeat the point of the test.
        await asyncio.sleep(0)
        await io._set_allow_reads(True)
        assert await t1 == "72F"
        assert await t3 == "ACK"
        assert await t2 == "75F"

    async def test_client_interactions(self):
        """Test basics reads and writes."""
        client = BryantEvolutionLocalClient(1, 1, _CoreClient(FakeDevIO()))

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
        result = await client._client._send_command("INVALID_COMMAND")
        self.assertEqual(result, None)

    async def test_second_system(self):
        """Test working with S2 instead of S1."""
        client = BryantEvolutionLocalClient(2, 2, _CoreClient(FakeDevIO()))
        self.assertEqual(await client.read_hvac_mode(), ("COOL", True))
        self.assertEqual(await client.read_cooling_setpoint(), 60)
        self.assertEqual(await client.read_zone_name(), "S2Z2")


    async def test_file_io(self):
        """Test the real device I/O type with a mock file."""
        read_file_chunks = [
            b"S1Z1HTSP:ACK\n",
            b"S1Z1HTSP:97\xf8F\n",
        ]
        file_chunks_iter = iter(read_file_chunks)

        mock_file_stream = mock.MagicMock(
            readline=lambda *args, **kwargs: next(file_chunks_iter),
        )

        client: BryantEvolutionLocalClient = None
        with patch(
            "aiofiles.threadpool.sync_open", return_value=mock_file_stream
        ) as mock_open:
            client = await BryantEvolutionLocalClient.get_client(
                1, 1, "test_file_io"
            )

        assert await client.set_heating_setpoint(97)
        mock_file_stream.write.assert_called_with(b"S1Z1HTSP!97\n")
        assert await client.read_heating_setpoint() == 97
        mock_file_stream.write.assert_called_with(b"S1Z1HTSP?\n")

    async def test_retries(self):
        """Test that the device retries NAK and corrupted errors."""
        read_file_chunks = [
            b"S1Z1HTSP:NAK\n",
            b"bogus\n",
            b"S1Z1HTSP:ACK\n",
            b"S1Z1HTSP:97\xf8F\n",
        ]
        file_chunks_iter = iter(read_file_chunks)

        mock_file_stream = mock.MagicMock(
            readline=lambda *args, **kwargs: next(file_chunks_iter),
        )

        client: BryantEvolutionLocalClient = None
        with patch(
            "aiofiles.threadpool.sync_open", return_value=mock_file_stream
        ) as mock_open:
            client = await BryantEvolutionLocalClient.get_client(
                1, 1, "test_retries"
            )

        assert await client.set_heating_setpoint(97)
        mock_file_stream.write.assert_called_with(b"S1Z1HTSP!97\n")
        assert await client.read_heating_setpoint() == 97
        mock_file_stream.write.assert_called_with(b"S1Z1HTSP?\n")

    async def test_timeout(self):
        """Test timeout handling."""
        io = FakeDevIO()
        client = BryantEvolutionLocalClient(1, 1, _CoreClient(io))
        await io._set_allow_reads(False)
        with patch.object(_CoreClient, "_timeout_sec", 0.1) as p:
            assert not await client.read_heating_setpoint()

    async def test_client_sharing(self):
        """Test that clients for different systems on the same tty reuse the same core client."""
        (c1, c2, c3) = await asyncio.gather(
            BryantEvolutionLocalClient.get_client(1, 1, "/dev/null"),
            BryantEvolutionLocalClient.get_client(1, 2, "/dev/null"),
            BryantEvolutionLocalClient.get_client(1, 2, "/dev/zero"))
        assert c1._client is c2._client
        assert c1._client is not c3._client

    async def test_enumerate(self):
        """Test that enumerating zones works."""
        io = FakeDevIO()
        io._state = {
            "S1Z1NAME": "S1Z1",
            "S1Z3NAME": "S1Z3",
            "S2Z7NAME": "S2Z7",
            "S2Z4NAME": "S2Z4",
        }
        tty = "/dev/ttyUSB47"
        client_fut = asyncio.get_running_loop().create_future()
        client_fut.set_result(_CoreClient(io))
        with patch.object(BryantEvolutionLocalClient, "_core_client_registry", {tty: client_fut}):
            assert await BryantEvolutionLocalClient.enumerate_zones(1, tty) == [
                ZoneInfo(system_id=1, zone_id=1, name="S1Z1"), 
                ZoneInfo(system_id=1, zone_id=3, name="S1Z3"), 
            ]
            assert await BryantEvolutionLocalClient.enumerate_zones(2, tty) == [
                ZoneInfo(system_id=2, zone_id=4, name="S2Z4"), 
                ZoneInfo(system_id=2, zone_id=7, name="S2Z7"), 
            ]


if __name__ == "__main__":
    unittest.main()
