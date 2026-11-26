"""Testes unitários para o bridge TCP -> BLE da JK-402A."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import unittest
from unittest.mock import AsyncMock

from src.ble_utils import chunk_bytes, optimize_tspl_bitmap, resolve_write_characteristic
from src.bridge import PrintBridgeServer
from src.config import BridgeConfig


@dataclass
class FakeChar:
    uuid: str
    properties: list[str]


@dataclass
class FakeService:
    uuid: str
    description: str
    characteristics: list[FakeChar]


class BleUtilsTests(unittest.TestCase):
    def test_chunk_bytes_splits_into_180_byte_chunks(self) -> None:
        data = b"A" * 400
        chunks = list(chunk_bytes(data, 180))
        self.assertEqual([len(c) for c in chunks], [180, 180, 40])
        self.assertEqual(b"".join(chunks), data)

    def test_chunk_bytes_empty_returns_empty(self) -> None:
        self.assertEqual(list(chunk_bytes(b"", 180)), [])

    def test_chunk_bytes_rejects_non_positive_size(self) -> None:
        with self.assertRaises(ValueError):
            list(chunk_bytes(b"abc", 0))

    def test_resolve_write_characteristic_explicit_uuid(self) -> None:
        services = [
            FakeService(
                uuid="0000ff00-0000-1000-8000-00805f9b34fb",
                description="Vendor",
                characteristics=[
                    FakeChar(
                        uuid="0000ff02-0000-1000-8000-00805f9b34fb",
                        properties=["write-without-response", "write"],
                    )
                ],
            )
        ]
        uuid, use_response = resolve_write_characteristic(
            services, explicit_uuid="0000FF02-0000-1000-8000-00805F9B34FB"
        )
        self.assertEqual(uuid, "0000ff02-0000-1000-8000-00805f9b34fb")
        self.assertFalse(use_response)

    def test_resolve_write_characteristic_rejects_read_only_uuid(self) -> None:
        services = [
            FakeService(
                uuid="00001800-0000-1000-8000-00805f9b34fb",
                description="Generic Access",
                characteristics=[
                    FakeChar(uuid="00002a00-0000-1000-8000-00805f9b34fb", properties=["read"])
                ],
            )
        ]
        with self.assertRaises(ValueError):
            resolve_write_characteristic(
                services, explicit_uuid="00002a00-0000-1000-8000-00805f9b34fb"
            )

    def test_resolve_write_characteristic_auto_detects_non_standard(self) -> None:
        services = [
            FakeService(
                uuid="00001800-0000-1000-8000-00805f9b34fb",
                description="Generic Access",
                characteristics=[
                    FakeChar(uuid="00002a00-0000-1000-8000-00805f9b34fb", properties=["write"])
                ],
            ),
            FakeService(
                uuid="0000ae30-0000-1000-8000-00805f9b34fb",
                description="Printer Service",
                characteristics=[
                    FakeChar(uuid="0000ae02-0000-1000-8000-00805f9b34fb", properties=["write"])
                ],
            ),
        ]
        uuid, use_response = resolve_write_characteristic(services)
        self.assertEqual(uuid, "0000ae02-0000-1000-8000-00805f9b34fb")
        self.assertTrue(use_response)

    def test_optimize_tspl_bitmap_trims_white_top_and_bottom_rows(self) -> None:
        header = b"SIZE 100 mm, 150 mm\r\nCLS\r\n"
        width_bytes = 4
        white = b"\xff" * width_bytes
        black = b"\x00" * width_bytes
        # 2 white rows top, 2 black rows middle, 3 white rows bottom (height=7)
        bitmap = white * 2 + black * 2 + white * 3
        footer = b"\r\nPRINT 1,1\r\n"
        payload = header + f"BITMAP 0,10,{width_bytes},7,0,".encode() + bitmap + footer

        optimized = optimize_tspl_bitmap(payload)
        expected = header + f"BITMAP 0,12,{width_bytes},2,0,".encode() + (black * 2) + footer
        self.assertEqual(optimized, expected)

    def test_optimize_tspl_bitmap_ignores_non_bitmap_payload(self) -> None:
        payload = b"SIZE 100 mm, 150 mm\r\nCLS\r\nPRINT 1,1\r\n"
        self.assertEqual(optimize_tspl_bitmap(payload), payload)


class PrintBridgeServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_tcp_job_is_queued_and_sent_to_ble_sender(self) -> None:
        sender = AsyncMock()
        config = BridgeConfig(tcp_host="127.0.0.1", tcp_port=0)
        server = PrintBridgeServer(config, sender=sender)
        await server.start()

        try:
            assert server._server is not None
            port = server._server.sockets[0].getsockname()[1]

            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            payload = b"SIZE 100 mm,150 mm\r\nCLS\r\nPRINT 1,1\r\n"
            writer.write(payload)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

            for _ in range(100):
                if sender.send_job.await_count > 0:
                    break
                await asyncio.sleep(0.01)

            await asyncio.wait_for(server.queue.join(), timeout=2.0)
            sender.send_job.assert_awaited_once_with(payload)
        finally:
            await server.stop()


if __name__ == "__main__":
    unittest.main()
