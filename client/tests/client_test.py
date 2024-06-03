"""Test module for BryantEvolutionClient."""

import logging
import unittest

from aiohttp import web
from src.evolutionhttp import BryantEvolutionClient

_LOGGER = logging.getLogger(__name__)

# Fake implementation of server.go.
class FakeBryantEvolutionServer:
    """Fake implementation of the server that this client targets."""
    def __init__(self):
        self.data = {
            "S1Z1RT": "72F",
            "S1Z1FAN": "AUTO",
            "S1MODE": "HEAT",
            "S1Z1CLSP": "75F",
            "S1Z1HTSP": "70F",
            "S2MODE": "COOL 1",
            "S2Z2CLSP": "60F",
            "SERVE_500": "47",
        }

    async def handle_command(self, request):
        """Processes one request to the /command handler."""
        command = await request.text()
        _LOGGER.error("Command: %s", command)
        if "!" in command:
            key, value = command.split("!")
            if key.endswith("SP"):
                value = value + "F"
            self.data[key] = value
            return web.json_response({"response": "ACK"})
        elif command.endswith("?"):
            key = command[:-1]
            if key in self.data:
                return web.json_response({"response": self.data[key]})
        elif command == "SERVE_500":
            return web.json_response({"error": "Internal Server Error"}, status=500)

        return web.json_response({"response": "NAK"})

class TestBryantEvolutionClient(unittest.IsolatedAsyncioTestCase):
    """Tests for BryantEvolutionClient."""
    async def asyncSetUp(self):
        # Create and start the fake server
        app = web.Application()
        self.fake_server = FakeBryantEvolutionServer()
        app.add_routes([web.post('/command', self.fake_server.handle_command)])
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, 'localhost', 8080)  # Choose a free port
        await self.site.start()

    async def asyncTearDown(self):
        # Stop the fake server
        await self.runner.cleanup()

    async def test_client_interactions(self):
        """Tests basics reads and writes."""
        client = BryantEvolutionClient("localhost", 1, 1)

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
        result = await client._api_request("INVALID_COMMAND")
        self.assertEqual(result, "NAK")

    async def test_http_error(self):
        """Tests translation of non-200 responses to None."""
        client = BryantEvolutionClient("localhost")
        self.assertEqual(await client._api_request("SERVE_500"), None)

    async def test_second_system(self):
        """Tests working with S2 instead of S1."""
        client = BryantEvolutionClient("localhost", "2", "2")
        self.assertEqual(await client.read_hvac_mode(), ("COOL", True))
        self.assertEqual(await client.read_cooling_setpoint(), 60)

if __name__ == "__main__":
    unittest.main()
