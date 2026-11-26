"""Servidor bridge TCP (127.0.0.1:9100) -> BLE GATT para impressora térmica JK-402A.

Recebe os bytes brutos (TSPL/RAW) do spooler de impressão do Windows via porta
TCP/IP Padrão (protocolo RAW, porta 9100), enfileira os jobs e os transmite
via BLE GATT usando ``bleak`` — contornando a falha de autenticação/PIN da
porta COM virtual (SPP) no Windows.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from logging.handlers import RotatingFileHandler
import socket
import sys
import time

from bleak import BleakClient, BleakScanner

from src.ble_utils import (
    chunk_bytes,
    optimize_tspl_bitmap,
    resolve_write_characteristic,
)
from src.config import BridgeConfig, LOG_DIR, LOG_FILE, load_settings

LOGGER = logging.getLogger("jk402a_bridge")


def setup_logging(verbose: bool = False) -> None:
    """Configura logging em arquivo rotativo e stdout (quando disponível)."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if verbose else logging.INFO
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    LOGGER.setLevel(level)
    LOGGER.handlers.clear()

    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    LOGGER.addHandler(file_handler)

    # Em modo `.pyw` (pythonw.exe), `sys.stdout` é None
    if sys.stdout is not None:
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)
        LOGGER.addHandler(stream_handler)


class BlePrinterSender:
    """Gerencia a conexão BLE e o envio fatiado de jobs para a JK-402A."""

    def __init__(self, config: BridgeConfig) -> None:
        self.config = config
        # Cache do dispositivo BLE encontrado para pular o scan nas impressões seguintes
        self._cached_device = None

    async def _enable_companion_notifications(
        self, client: BleakClient, write_uuid: str
    ) -> None:
        """Ativa ``notify`` nas características do mesmo serviço (ex.: 0x2AF0 para 0x2AF1).

        Na JK-402A (chip HS6602M), habilitar o notify do serviço 0x18F0 garante
        o retorno de eventuais mensagens TSPL e estabiliza o canal de impressão.
        """
        target = write_uuid.lower()
        for service in client.services:
            if not any(c.uuid.lower() == target for c in service.characteristics):
                continue
            for char in service.characteristics:
                props = {p.lower() for p in char.properties}
                if "notify" not in props:
                    continue
                try:
                    await client.start_notify(
                        char.uuid,
                        lambda _s, data, u=char.uuid: LOGGER.info(
                            "Retorno BLE (%s): %r", u, bytes(data)
                        ),
                    )
                    LOGGER.debug("Notify ativado em %s", char.uuid)
                except Exception as exc:
                    LOGGER.debug("Não foi possível ativar notify em %s: %s", char.uuid, exc)

    async def send_job(self, payload: bytes) -> None:
        """Otimiza (se habilitado) e envia um job completo com retentativas."""
        if self.config.trim_bitmap_margins:
            optimized = optimize_tspl_bitmap(payload)
            if len(optimized) < len(payload):
                saved_pct = round((1 - len(optimized) / len(payload)) * 100)
                LOGGER.info(
                    "BITMAP recortado: %d -> %d bytes (-%d%%)",
                    len(payload),
                    len(optimized),
                    saved_pct,
                )
                payload = optimized

        last_error: Exception | None = None

        for attempt in range(1, self.config.max_retries + 1):
            try:
                await self._send_once(payload)
                return
            except Exception as exc:
                # Invalida cache em caso de falha para forçar novo scan na retentativa
                self._cached_device = None
                last_error = exc
                LOGGER.warning(
                    "Tentativa %d/%d falhou ao enviar %d bytes via BLE: %s",
                    attempt,
                    self.config.max_retries,
                    len(payload),
                    exc,
                )
                if attempt < self.config.max_retries:
                    await asyncio.sleep(self.config.retry_delay)

        raise RuntimeError(
            f"Falha ao imprimir após {self.config.max_retries} tentativas: {last_error}"
        ) from last_error

    async def _send_once(self, payload: bytes) -> None:
        started_at = time.monotonic()
        device = self._cached_device
        if device is None:
            LOGGER.info("Localizando JK-402A (%s)...", self.config.mac_address)
            device = await BleakScanner.find_device_by_address(
                self.config.mac_address, timeout=self.config.scan_timeout
            )
            if device is None:
                raise ConnectionError(
                    f"Impressora {self.config.mac_address} não encontrada via BLE."
                )
            self._cached_device = device

        async with BleakClient(device) as client:
            char_uuid, auto_response = resolve_write_characteristic(
                client.services, explicit_uuid=self.config.write_uuid
            )
            await self._enable_companion_notifications(client, char_uuid)

            use_response = (
                self.config.force_write_with_response
                if self.config.force_write_with_response is not None
                else auto_response
            )
            # Garante que o chunk nunca ultrapasse o payload ATT do MTU negociado
            mtu_limit = max(20, getattr(client, "mtu_size", self.config.chunk_size + 3) - 3)
            effective_chunk_size = min(self.config.chunk_size, mtu_limit)
            total_chunks = (len(payload) + effective_chunk_size - 1) // effective_chunk_size

            LOGGER.info(
                "Conectado a %s (MTU=%s). Enviando %d bytes em %d chunks de %dB via %s (response=%s)...",
                device.address,
                getattr(client, "mtu_size", "?"),
                len(payload),
                total_chunks,
                effective_chunk_size,
                char_uuid,
                use_response,
            )

            sent_chunks = 0
            try:
                for chunk in chunk_bytes(payload, effective_chunk_size):
                    await client.write_gatt_char(char_uuid, chunk, response=use_response)
                    sent_chunks += 1
                    if total_chunks >= 100 and sent_chunks % 100 == 0 and sent_chunks < total_chunks:
                        LOGGER.info("Progresso BLE: %d/%d chunks...", sent_chunks, total_chunks)
                    if self.config.chunk_delay > 0:
                        await asyncio.sleep(self.config.chunk_delay)
            except Exception:
                LOGGER.warning(
                    "Interrompido no chunk %d/%d (%d/%d bytes enviados).",
                    sent_chunks,
                    total_chunks,
                    min(sent_chunks * effective_chunk_size, len(payload)),
                    len(payload),
                )
                raise

            # Pequena espera antes de fechar a sessão GATT para a impressora drenar o buffer
            if self.config.post_send_delay > 0:
                await asyncio.sleep(self.config.post_send_delay)
            elapsed = time.monotonic() - started_at
            LOGGER.info(
                "Job concluído em %.1fs: %d chunks enviados (%d bytes).",
                elapsed,
                sent_chunks,
                len(payload),
            )


class PrintBridgeServer:
    """Servidor TCP local que enfileira jobs do spooler do Windows para o worker BLE."""

    def __init__(self, config: BridgeConfig, sender: BlePrinterSender | None = None) -> None:
        self.config = config
        self.sender = sender or BlePrinterSender(config)
        self.queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._server: asyncio.Server | None = None
        self._worker_task: asyncio.Task[None] | None = None

    async def handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Lê todos os bytes de uma conexão TCP RAW do spooler e enfileira para o BLE."""
        peer = writer.get_extra_info("peername")
        chunks: list[bytes] = []
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                chunks.append(data)
        except Exception as exc:
            LOGGER.error("Erro ao ler dados TCP de %s: %s", peer, exc)
        finally:
            writer.close()
            await writer.wait_closed()

        payload = b"".join(chunks)
        if not payload:
            LOGGER.debug("Conexão vazia de %s ignorada.", peer)
            return

        LOGGER.info("Job recebido de %s (%d bytes). Enfileirando...", peer, len(payload))
        LOGGER.debug("Prévia do job (primeiros 120B): %r", payload[:120])
        await self.queue.put(payload)

    async def _worker_loop(self) -> None:
        while True:
            payload = await self.queue.get()
            try:
                await self.sender.send_job(payload)
            except Exception as exc:
                LOGGER.error("Erro ao processar job de %d bytes: %s", len(payload), exc)
            finally:
                self.queue.task_done()

    async def start(self) -> None:
        """Inicia o worker BLE e abre o socket TCP local."""
        self._worker_task = asyncio.create_task(self._worker_loop(), name="ble-print-worker")
        self._server = await asyncio.start_server(
            self.handle_client, self.config.tcp_host, self.config.tcp_port
        )
        LOGGER.info(
            "Bridge JK-402A ativo em %s:%d -> BLE %s (chunk=%dB)",
            self.config.tcp_host,
            self.config.tcp_port,
            self.config.mac_address,
            self.config.chunk_size,
        )

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        """Encerra o servidor TCP e cancela o worker BLE de forma limpa."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass


def is_port_listening(host: str = "127.0.0.1", port: int = 9100, timeout: float = 0.25) -> bool:
    """Verifica se já existe um servidor escutando na porta TCP configurada."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def parse_args(argv: list[str] | None = None) -> BridgeConfig:
    defaults = load_settings()
    parser = argparse.ArgumentParser(
        description="Bridge TCP RAW (127.0.0.1:9100) -> BLE para impressora JK-402A."
    )
    parser.add_argument("--mac", default=defaults.mac_address, help="MAC BLE da impressora.")
    parser.add_argument("--uuid", default=defaults.write_uuid, help="UUID GATT de escrita.")
    parser.add_argument("--host", default=defaults.tcp_host, help="Host TCP local.")
    parser.add_argument("--port", type=int, default=defaults.tcp_port, help="Porta TCP local.")
    parser.add_argument(
        "--chunk-size", type=int, default=defaults.chunk_size, help="Tamanho do chunk BLE."
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=defaults.chunk_delay,
        help="Pausa em segundos entre chunks BLE.",
    )
    parser.add_argument(
        "--post-delay",
        type=float,
        default=defaults.post_send_delay,
        help="Espera em segundos após o último chunk.",
    )
    response_group = parser.add_mutually_exclusive_group()
    response_group.add_argument(
        "--with-response",
        dest="with_response",
        action="store_true",
        default=defaults.force_write_with_response,
        help="Usa write-with-response (ACK por pacote).",
    )
    response_group.add_argument(
        "--without-response",
        dest="with_response",
        action="store_false",
        help="Usa write-without-response (mais rápido, padrão).",
    )
    parser.add_argument(
        "--trim-bitmap",
        action="store_true",
        default=defaults.trim_bitmap_margins,
        help="Recorta margens brancas (0xFF) no topo/rodapé de comandos BITMAP TSPL.",
    )
    parser.add_argument("--verbose", action="store_true", help="Ativa logs detalhados.")
    args = parser.parse_args(argv)

    setup_logging(verbose=args.verbose)
    return BridgeConfig(
        mac_address=args.mac,
        write_uuid=args.uuid,
        tcp_host=args.host,
        tcp_port=args.port,
        chunk_size=args.chunk_size,
        chunk_delay=args.delay,
        post_send_delay=args.post_delay,
        force_write_with_response=args.with_response,
        trim_bitmap_margins=args.trim_bitmap,
    )


async def run_bridge(config: BridgeConfig) -> None:
    server = PrintBridgeServer(config)
    try:
        await server.serve_forever()
    finally:
        await server.stop()


def main(argv: list[str] | None = None) -> int:
    config = parse_args(argv)
    try:
        asyncio.run(run_bridge(config))
        return 0
    except KeyboardInterrupt:
        LOGGER.info("Bridge encerrado pelo usuário.")
        return 0
    except OSError as exc:
        LOGGER.error("Erro de rede ao iniciar servidor TCP: %s", exc)
        return 1
