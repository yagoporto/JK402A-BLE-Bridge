"""Utilitários para descoberta GATT, fatiamento de pacotes BLE e otimização TSPL."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
import re
from typing import Protocol

from src.config import KNOWN_PRINTER_WRITE_UUIDS, STANDARD_GATT_SERVICES


class GattCharacteristicLike(Protocol):
    """Interface mínima esperada de uma característica GATT do Bleak."""

    uuid: str
    properties: Iterable[str]


class GattServiceLike(Protocol):
    """Interface mínima esperada de um serviço GATT do Bleak."""

    uuid: str
    description: str
    characteristics: Iterable[GattCharacteristicLike]


def is_writable(char: GattCharacteristicLike) -> bool:
    """Retorna ``True`` se a característica aceita escrita BLE."""
    props = {p.lower() for p in char.properties}
    return "write" in props or "write-without-response" in props


def prefers_write_with_response(char: GattCharacteristicLike) -> bool:
    """Prefere ``write-without-response`` quando disponível para maior vazão."""
    props = {p.lower() for p in char.properties}
    return "write-without-response" not in props


def resolve_write_characteristic(
    services: Iterable[GattServiceLike],
    explicit_uuid: str = "",
) -> tuple[str, bool]:
    """Localiza e valida a característica GATT de escrita.

    Args:
        services: Coleção de serviços GATT retornada pelo ``BleakClient``.
        explicit_uuid: UUID específico configurado pelo usuário (opcional).

    Returns:
        Tupla ``(uuid, use_response)``.

    Raises:
        ValueError: Se o UUID explícito não existir/não for gravável, ou se
            nenhuma característica gravável for encontrada no dispositivo.
    """
    target = explicit_uuid.strip().lower()
    writable_non_standard: list[GattCharacteristicLike] = []
    by_uuid: dict[str, GattCharacteristicLike] = {}

    for service in services:
        service_uuid = service.uuid.lower()
        for char in service.characteristics:
            char_uuid = char.uuid.lower()
            by_uuid[char_uuid] = char
            if is_writable(char) and service_uuid not in STANDARD_GATT_SERVICES:
                writable_non_standard.append(char)

    if target:
        char = by_uuid.get(target)
        if char is None:
            raise ValueError(f"UUID de escrita não encontrado no dispositivo: {explicit_uuid}")
        if not is_writable(char):
            props = ", ".join(char.properties)
            raise ValueError(
                f"UUID {explicit_uuid} encontrado, mas não permite escrita (propriedades: {props})."
            )
        return char.uuid, prefers_write_with_response(char)

    # Prioriza UUIDs conhecidos de impressoras térmicas BLE antes do fallback genérico
    for known_uuid in KNOWN_PRINTER_WRITE_UUIDS:
        char = by_uuid.get(known_uuid.lower())
        if char is not None and is_writable(char):
            return char.uuid, prefers_write_with_response(char)

    if writable_non_standard:
        chosen = writable_non_standard[0]
        return chosen.uuid, prefers_write_with_response(chosen)

    raise ValueError("Nenhuma característica GATT gravável foi encontrada na impressora.")


def chunk_bytes(data: bytes, chunk_size: int) -> Iterator[bytes]:
    """Divide os bytes em fatias de até ``chunk_size`` bytes."""
    if chunk_size <= 0:
        raise ValueError("chunk_size deve ser maior que zero.")
    for offset in range(0, len(data), chunk_size):
        yield data[offset : offset + chunk_size]


# Casa cabeçalhos TSPL no início do payload ou após quebra de linha:
# BITMAP x,y,width_bytes,height,mode,<dados binários>
_BITMAP_HEADER_RE = re.compile(
    rb"(?:^|(?<=\n))BITMAP\s+(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,"
)


def optimize_tspl_bitmap(payload: bytes) -> bytes:
    """Recorta linhas em branco (``0xFF``) no topo e rodapé de comandos TSPL ``BITMAP``.

    O driver GDI do Windows renderiza a etiqueta inteira (ex.: 100x150mm = 120 KB),
    mesmo quando há margens vazias. Remover linhas ``0xFF`` e ajustar ``y`` e
    ``height`` reduz o volume trafegado via BLE sem alterar o resultado impresso.
    Caso não haja comando ``BITMAP`` válido ou economia, retorna o original.
    """
    out = bytearray()
    pos = 0
    changed = False

    while pos < len(payload):
        match = _BITMAP_HEADER_RE.search(payload, pos)
        if match is None:
            out.extend(payload[pos:])
            break

        x = int(match.group(1))
        y = int(match.group(2))
        width_bytes = int(match.group(3))
        height = int(match.group(4))
        mode = int(match.group(5))
        data_start = match.end()
        total_bytes = width_bytes * height

        if width_bytes <= 0 or height <= 1 or data_start + total_bytes > len(payload):
            out.extend(payload[pos : match.end()])
            pos = match.end()
            continue

        bitmap_data = payload[data_start : data_start + total_bytes]
        white_row = b"\xff" * width_bytes

        top = 0
        while top < height and bitmap_data[top * width_bytes : (top + 1) * width_bytes] == white_row:
            top += 1

        if top == height:
            # Imagem 100% branca: mantém 1 linha para preservar o comando válido
            top = 0
            bottom = 0
        else:
            bottom = height - 1
            while (
                bottom > top
                and bitmap_data[bottom * width_bytes : (bottom + 1) * width_bytes] == white_row
            ):
                bottom -= 1

        new_height = bottom - top + 1
        if new_height >= height:
            out.extend(payload[pos : data_start + total_bytes])
            pos = data_start + total_bytes
            continue

        changed = True
        out.extend(payload[pos : match.start()])
        new_y = y + top
        header = f"BITMAP {x},{new_y},{width_bytes},{new_height},{mode},".encode("ascii")
        out.extend(header)
        out.extend(bitmap_data[top * width_bytes : (bottom + 1) * width_bytes])
        pos = data_start + total_bytes

    return bytes(out) if changed else payload
