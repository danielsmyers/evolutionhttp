import aiofiles
import asyncio
import logging
import re

from abc import ABC, abstractmethod
from typing import Optional, Tuple
from .client import _parse_temperature

_LOGGER = logging.getLogger(__name__)


def _is_write(cmd: str) -> bool:
    """Return whether cmd is a write."""
    return "!" in cmd


class BryantEvolutionLocalClient:
    """
    This class exposes methods to read and set various HVAC parameters for
    a device connected directly to this host over a serial port.

    All read methods return None on protocol errors (e.g., timeout). They
    return "NAK" when the device responds with "NAK."

    On a non-successful set_* call, the set may or may not have occurred.
    """

    class DevIO(ABC):
        @abstractmethod
        async def write(self, s: str) -> None:
            """Write a command to the device."""
            pass

        @abstractmethod
        async def read_next(self) -> str:
            """Read a response from the device."""
            pass

    class ProdDevIO(DevIO):
        """I/O implementation for a real serial port."""

        def __init__(self, tty: str):
            self._tty = tty

        async def open(self) -> None:
            self._file = await aiofiles.open(self._tty, mode="r+b", buffering=0)

        async def write(self, s: str) -> None:
            await self._file.write(f"{s}\n".encode("ascii"))

        async def read_next(self) -> str:
            while True:
                s: str = (
                    (await self._file.readline())
                    .decode("ascii", errors="ignore")
                    .strip()
                )
                if s != "":
                    return s

    # How long to wait for a response from the device.
    _timeout_sec = 6

    def __init__(self, system_id: int, zone_id: int, device: DevIO):
        self._device = device
        self._system_id = system_id
        self._zone_id = zone_id
        self._pending_reads: list[Tuple[str, asyncio.Future[str | None]]] = []
        self._pending_writes: list[Tuple[str, asyncio.Future[str | None]]] = []
        self._is_cmd_active: bool = False

    async def read_current_temperature(self) -> Optional[int]:
        """Reads the current temperature."""
        response = await self._send_command(f"S{self._system_id}Z{self._zone_id}RT?")
        return _parse_temperature(response)

    async def read_cooling_setpoint(self) -> Optional[int]:
        """Reads the current cooling setpoint."""
        response = await self._send_command(f"S{self._system_id}Z{self._zone_id}CLSP?")
        return _parse_temperature(response)

    async def set_cooling_setpoint(self, temperature: int) -> bool:
        """Sets the cooling setpoint."""
        response = await self._send_command(
            f"S{self._system_id}Z{self._zone_id}CLSP!{int(temperature)}"
        )
        return response == "ACK"

    async def read_heating_setpoint(self) -> Optional[int]:
        """Gets the heating setpoint."""
        response = await self._send_command(f"S{self._system_id}Z{self._zone_id}HTSP?")
        return _parse_temperature(response)

    async def read_hvac_mode(self) -> Optional[Tuple[str, bool]]:
        """Reads the HVAC mode (heat, cool, etc).

        Returns the mode and whether or not the system is active.
        """
        response = await self._send_command(f"S{self._system_id}MODE?")
        if not response:
            return None

        # If the system is not idle, then the response will be the mode and
        # a number (indicating the number of stages active), space-separated.
        # If it is idle, then the response is just the mode.
        is_active = False
        m = re.match("([A-Z]+)[ ]*([0-9]?)", response)
        if not m:
            _LOGGER.error("Unparseable mode: %s", response)
            return None

        mode = m.group(1)
        if m.group(2):
            is_active = True
        return (mode, is_active)

    async def set_heating_setpoint(self, temperature: int) -> bool:
        """Sets the heating setpoint."""
        response = await self._send_command(
            f"S{self._system_id}Z{self._zone_id}HTSP!{int(temperature)}"
        )
        return response == "ACK"

    async def set_hvac_mode(self, hvac_mode: str) -> bool:
        """Sets the HVAC mode."""
        if hvac_mode == "heat_cool":
            hvac_mode = "AUTO"
        response = await self._send_command(
            f"S{self._system_id}MODE!{hvac_mode.upper()}"
        )
        return response == "ACK"

    async def read_fan_mode(self) -> Optional[str]:
        """Reads the fan mode."""
        return await self._send_command(f"S{self._system_id}Z{self._zone_id}FAN?")

    async def set_fan_mode(self, fan_mode: str) -> bool:
        """Sets the fan mode."""
        response = await self._send_command(
            f"S{self._system_id}Z{self._zone_id}FAN!{fan_mode}"
        )
        return response == "ACK"

    def _check_rep(self) -> None:
        if len(self._pending_reads) > 0 or len(self._pending_writes) > 0:
            assert self._is_cmd_active

    def _maybe_pop_work(self) -> Optional[Tuple[str, asyncio.Future[str | None]]]:
        """Return the next command (and future) to execute, if any."""
        if len(self._pending_writes) > 0:
            return self._pending_writes.pop(0)
        elif len(self._pending_reads) > 0:
            return self._pending_reads.pop(0)
        else:
            return None

    async def _send_command(self, cmd: str) -> str | None:
        self._check_rep()
        fut = asyncio.get_running_loop().create_future()
        if _is_write(cmd):
            self._pending_writes.append((cmd, fut))
        else:
            self._pending_reads.append((cmd, fut))
        await self._maybe_process_commands()
        result = await fut
        self._check_rep()
        if result is None:
            return None
        return result.split(":")[1]

    async def _maybe_process_commands(self) -> None:
        if self._is_cmd_active:
            return

        # Figure out what to do next
        cmd_and_fut = self._maybe_pop_work()
        if cmd_and_fut is None:
            return
        (cmd, fut) = cmd_and_fut
        self._is_cmd_active = True

        # Send the command and wait for the response
        await self._device.write(cmd)
        response = None
        try:
            async with asyncio.timeout(self._timeout_sec):
                response = await self._device.read_next()
        except TimeoutError:
            _LOGGER.error("Timeout waiting for response to %s" % cmd)
        self._is_cmd_active = False
        fut.set_result(response)

        # Continue processing commands if need be.
        await self._maybe_process_commands()
