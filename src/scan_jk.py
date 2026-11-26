"""Diagnóstico BLE para listar serviços/características GATT da JK-402A e validar escrita."""

from __future__ import annotations

import argparse
import asyncio
import sys

from bleak import BleakClient, BleakScanner

from src.ble_utils import is_writable, resolve_write_characteristic
from src.config import DEFAULT_MAC_ADDRESS


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Lista serviços e características GATT da impressora JK-402A via BLE."
    )
    parser.add_argument(
        "--mac",
        default=DEFAULT_MAC_ADDRESS,
        help=f"Endereço MAC BLE da impressora (padrão: {DEFAULT_MAC_ADDRESS})",
    )
    parser.add_argument(
        "--uuid",
        default="",
        help="UUID de escrita opcional para validar/testar especificamente.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="Tempo máximo (s) para localizar a impressora (padrão: 10.0).",
    )
    parser.add_argument(
        "--test-write",
        action="store_true",
        help="Testa uma escrita mínima nas características graváveis.",
    )
    return parser.parse_args()


async def scan_device(mac_address: str, write_uuid: str, timeout: float) -> int:
    """Conecta na impressora e exibe toda a árvore de serviços/características GATT."""
    print(f"Procurando JK-402A via BLE ({mac_address})...")
    device = await BleakScanner.find_device_by_address(mac_address, timeout=timeout)

    if not device:
        print("Não encontrou pelo MAC exato. Listando dispositivos BLE próximos:")
        devices = await BleakScanner.discover(timeout=5.0)
        if not devices:
            print(" - Nenhum dispositivo BLE encontrado.")
        for d in devices:
            print(f" - {d.name or '(Sem nome)'} | {d.address}")
        return 1

    print(f"Conectando em {device.name or 'JK-402A'} ({device.address})...")
    async with BleakClient(device) as client:
        print("Conectado! Serviços e características encontrados:\n")
        writable_uuids: list[str] = []

        for service in client.services:
            print(f"[Serviço] {service.uuid}: {service.description}")
            for char in service.characteristics:
                props = ", ".join(char.properties)
                marker = "  <-- [GRAVÁVEL]" if is_writable(char) else ""
                print(f"   └── [Char] {char.uuid} | Propriedades: {props}{marker}")
                if is_writable(char):
                    writable_uuids.append(char.uuid)

        print("\n--- Resumo de Validação ---")
        if writable_uuids:
            print(f"Características graváveis encontradas ({len(writable_uuids)}):")
            for uuid in writable_uuids:
                print(f" - {uuid}")
        else:
            print("Nenhuma característica gravável encontrada.")

        try:
            chosen_uuid, use_response = resolve_write_characteristic(
                client.services, explicit_uuid=write_uuid
            )
            mode = "write (com resposta)" if use_response else "write-without-response"
            print(f"\nUUID selecionado/validado para impressão: {chosen_uuid} ({mode})")
            return 0
        except ValueError as exc:
            print(f"\nFalha na validação do UUID: {exc}")
            return 2


async def probe_writes(mac_address: str, write_uuid: str, timeout: float) -> int:
    """Testa escrita mínima (2 bytes) e ativação de notify nos UUIDs graváveis."""
    print(f"Procurando JK-402A via BLE ({mac_address}) para teste de escrita...")
    device = await BleakScanner.find_device_by_address(mac_address, timeout=timeout)
    if not device:
        print("Impressora não encontrada.")
        return 1

    targets = (
        [write_uuid]
        if write_uuid.strip()
        else [
            "00002af1-0000-1000-8000-00805f9b34fb",
            "bef8d6c9-9c21-4c9e-b632-bd58c1009f9f",
            "49535343-8841-43f4-a8d4-ecbe34729bb3",
        ]
    )

    test_payload = b"\r\n"

    print("\n[Teste 0] Tentando READ (Device Name) + START_NOTIFY (00002af0) antes de escrever...")
    try:
        async with BleakClient(device, timeout=timeout) as client:
            name_bytes = await client.read_gatt_char("00002a00-0000-1000-8000-00805f9b34fb")
            print(f"  READ 00002a00 OK: {name_bytes!r}")
            await client.start_notify(
                "00002af0-0000-1000-8000-00805f9b34fb",
                lambda _sender, data: print(f"  [NOTIFY] {data!r}"),
            )
            print("  START_NOTIFY 00002af0 OK! Testando escrita em 00002af1...")
            await client.write_gatt_char(
                "00002af1-0000-1000-8000-00805f9b34fb", test_payload, response=False
            )
            await asyncio.sleep(0.5)
            print("  ESCRITA APÓS NOTIFY OK!")
            return 0
    except Exception as exc:
        print(f"  [Teste 0] FALHOU: {type(exc).__name__}: {exc!r}")

    for target_uuid in targets:
        for use_response in (False, True):
            print(f"\nTestando {target_uuid} (response={use_response}, {len(test_payload)}B)...")
            disconnected = asyncio.Event()

            def on_disconnect(_client: BleakClient) -> None:
                print("  [EVENTO] Callback disconnected disparado!")
                disconnected.set()

            try:
                async with BleakClient(
                    device, disconnected_callback=on_disconnect, timeout=timeout
                ) as client:
                    print(f"  Conectado (MTU={getattr(client, 'mtu_size', '?')}). Escrevendo...")
                    await client.write_gatt_char(target_uuid, test_payload, response=use_response)
                    await asyncio.sleep(0.5)
                    print(f"  SUCESSO! (disconnected={disconnected.is_set()})")
            except Exception as exc:
                print(f"  FALHOU: {type(exc).__name__}: {exc!r}")

    return 0


def main() -> int:
    args = parse_args()
    try:
        if args.test_write:
            return asyncio.run(probe_writes(args.mac, args.uuid, args.timeout))
        return asyncio.run(scan_device(args.mac, args.uuid, args.timeout))
    except KeyboardInterrupt:
        print("\nDiagnóstico cancelado pelo usuário.")
        return 130
    except Exception as exc:
        print(f"\nErro durante o diagnóstico BLE: {exc}", file=sys.stderr)
        return 1
