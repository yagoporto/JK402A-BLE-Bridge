"""Configurações centrais e persistência em JSON do bridge JK-402A.

Todas as configurações podem ser sobrescritas por variáveis de ambiente
(prefixo ``JK402A_*``), pelo arquivo ``bridge_settings.json`` na raiz do
projeto ou diretamente pela interface System Tray.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path

# Raiz do repositório/instalação (um nível acima de `src/`)
BASE_DIR = Path(__file__).resolve().parent.parent
ASSETS_DIR = BASE_DIR / "assets"
LOG_DIR = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "jk402a_bridge.log"
SETTINGS_FILE = BASE_DIR / "bridge_settings.json"

# Endereço MAC BLE padrão da impressora JK-402A (pode ser alterado via JK402A_MAC ou --mac)
DEFAULT_MAC_ADDRESS = "66:32:33:64:76:48"

# Característica GATT de escrita validada na JK-402A (serviço 0x18F0, com notify em 0x2AF0).
# Outras características graváveis presentes no módulo HS6602M:
# - "49535343-8841-43f4-a8d4-ecbe34729bb3" (ISSC Transparent UART)
# - "bef8d6c9-9c21-4c9e-b632-bd58c1009f9f" (Serviço e7810a71)
DEFAULT_WRITE_UUID = "00002af1-0000-1000-8000-00805f9b34fb"

# Ordem de prioridade usada na detecção automática quando nenhum UUID explícito é informado
KNOWN_PRINTER_WRITE_UUIDS: tuple[str, ...] = (
    "00002af1-0000-1000-8000-00805f9b34fb",
    "0000ff02-0000-1000-8000-00805f9b34fb",
    "49535343-8841-43f4-a8d4-ecbe34729bb3",
    "bef8d6c9-9c21-4c9e-b632-bd58c1009f9f",
    "0000ae01-0000-1000-8000-00805f9b34fb",
    "0000ffe1-0000-1000-8000-00805f9b34fb",
)

# Serviços GATT padrão do sistema ignorados durante a busca automática de escrita
STANDARD_GATT_SERVICES: frozenset[str] = frozenset(
    {
        "00001800-0000-1000-8000-00805f9b34fb",  # Generic Access
        "00001801-0000-1000-8000-00805f9b34fb",  # Generic Attribute
        "0000180a-0000-1000-8000-00805f9b34fb",  # Device Information
        "0000180f-0000-1000-8000-00805f9b34fb",  # Battery Service
    }
)


@dataclass(frozen=True)
class BridgeConfig:
    """Parâmetros de execução do servidor TCP -> BLE."""

    mac_address: str = os.getenv("JK402A_MAC", DEFAULT_MAC_ADDRESS)
    write_uuid: str = os.getenv("JK402A_WRITE_UUID", DEFAULT_WRITE_UUID)
    tcp_host: str = os.getenv("JK402A_HOST", "127.0.0.1")
    tcp_port: int = int(os.getenv("JK402A_PORT", "9100"))
    chunk_size: int = int(os.getenv("JK402A_CHUNK_SIZE", "512"))
    chunk_delay: float = float(os.getenv("JK402A_CHUNK_DELAY", "0.0"))
    post_send_delay: float = float(os.getenv("JK402A_POST_DELAY", "1.0"))
    scan_timeout: float = float(os.getenv("JK402A_SCAN_TIMEOUT", "10.0"))
    max_retries: int = int(os.getenv("JK402A_MAX_RETRIES", "3"))
    retry_delay: float = float(os.getenv("JK402A_RETRY_DELAY", "2.0"))
    # Write Without Response é muito mais rápido; With Response fica disponível como fallback
    force_write_with_response: bool | None = (
        os.getenv("JK402A_WITH_RESPONSE", "0").strip().lower() in {"1", "true", "yes"}
    )
    # Quando ativo, remove linhas 0xFF (brancas) no topo/rodapé de comandos TSPL BITMAP
    trim_bitmap_margins: bool = (
        os.getenv("JK402A_TRIM_BITMAP", "0").strip().lower() in {"1", "true", "yes"}
    )


def load_settings(path: Path = SETTINGS_FILE) -> BridgeConfig:
    """Carrega configurações salvas em JSON, fazendo fallback para os padrões."""
    defaults = BridgeConfig()
    if not path.exists():
        return defaults
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return BridgeConfig(
            mac_address=str(raw.get("mac_address", defaults.mac_address)),
            write_uuid=str(raw.get("write_uuid", defaults.write_uuid)),
            tcp_host=str(raw.get("tcp_host", defaults.tcp_host)),
            tcp_port=int(raw.get("tcp_port", defaults.tcp_port)),
            chunk_size=max(20, int(raw.get("chunk_size", defaults.chunk_size))),
            chunk_delay=max(0.0, float(raw.get("chunk_delay", defaults.chunk_delay))),
            post_send_delay=max(0.0, float(raw.get("post_send_delay", defaults.post_send_delay))),
            scan_timeout=float(raw.get("scan_timeout", defaults.scan_timeout)),
            max_retries=int(raw.get("max_retries", defaults.max_retries)),
            retry_delay=float(raw.get("retry_delay", defaults.retry_delay)),
            force_write_with_response=raw.get(
                "force_write_with_response", defaults.force_write_with_response
            ),
            trim_bitmap_margins=bool(
                raw.get("trim_bitmap_margins", defaults.trim_bitmap_margins)
            ),
        )
    except Exception:
        return defaults


def save_settings(config: BridgeConfig, path: Path = SETTINGS_FILE) -> None:
    """Persiste configurações do bridge em JSON."""
    path.write_text(json.dumps(asdict(config), indent=2), encoding="utf-8")
