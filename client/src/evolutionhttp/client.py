"""This module is a client for controlling an Evolution device over HTTP."""

import logging
import re
from typing import Optional, Tuple

import aiohttp

_LOGGER = logging.getLogger(__name__)


def _parse_temperature(response: Optional[str]) -> Optional[int]:
    if response is not None:
        m = re.match("([0-9]+)[A-Z]", response)  # E.g., 72F
        if m and m.group(1):
            return int(m.group(1))
    return None


class BryantEvolutionClient:
    """
    This class exposes methods to read and set various HVAC parameters.

    All read methods return None on protocol errors (e.g., HTTP 500). They
    return "NAK" when the device responds with "NAK."

    On a non-successful set_* call, the set may or may not have occurred.
    """

    def __init__(self, host, system_id=1, zone_id=1):
        self._base_url = f"http://{host}:8080/command"
        self._system_id = system_id
        self._zone_id = zone_id

    async def _api_request(self, command: str) -> Optional[str]:
        """Sends `command` to the device and returns the response."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(self._base_url, data=command) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                    return data.get("response", "").strip()
        except aiohttp.ClientError as e:
            _LOGGER.error("Error making API request: %s", e)
            return None

    async def read_current_temperature(self) -> Optional[int]:
        """Reads the current temperature."""
        response = await self._api_request(f"S{self._system_id}Z{self._zone_id}RT?")
        return _parse_temperature(response)

    async def read_cooling_setpoint(self) -> Optional[int]:
        """Reads the current cooling setpoint."""
        response = await self._api_request(f"S{self._system_id}Z{self._zone_id}CLSP?")
        return _parse_temperature(response)

    async def set_cooling_setpoint(self, temperature: int) -> bool:
        """Sets the cooling setpoint."""
        response = await self._api_request(
            f"S{self._system_id}Z{self._zone_id}CLSP!{int(temperature)}"
        )
        return response == "ACK"

    async def read_heating_setpoint(self) -> Optional[int]:
        """Gets the heating setpoint."""
        response = await self._api_request(f"S{self._system_id}Z{self._zone_id}HTSP?")
        return _parse_temperature(response)

    async def set_heating_setpoint(self, temperature: int) -> bool:
        """Sets the heating setpoint."""
        response = await self._api_request(
            f"S{self._system_id}Z{self._zone_id}HTSP!{int(temperature)}"
        )
        return response == "ACK"

    async def read_hvac_mode(self) -> Optional[Tuple[str, bool]]:
        """Reads the HVAC mode (heat, cool, etc).

        Returns the mode and whether or not the system is active.
        """
        response = await self._api_request(f"S{self._system_id}MODE?")
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

    async def set_hvac_mode(self, hvac_mode: str) -> bool:
        """Sets the HVAC mode."""
        if hvac_mode == "heat_cool":
            hvac_mode = "AUTO"
        response = await self._api_request(
            f"S{self._system_id}MODE!{hvac_mode.upper()}"
        )
        return response == "ACK"

    async def read_fan_mode(self) -> Optional[str]:
        """Reads the fan mode."""
        return await self._api_request(f"S{self._system_id}Z{self._zone_id}FAN?")

    async def set_fan_mode(self, fan_mode: str) -> bool:
        """Sets the fan mode."""
        response = await self._api_request(
            f"S{self._system_id}Z{self._zone_id}FAN!{fan_mode}"
        )
        return response == "ACK"
