"""Interface gráfica com ícone na bandeja do Windows (System Tray) para o bridge JK-402A."""

from __future__ import annotations

import asyncio
import ctypes
import logging
from pathlib import Path
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

from PIL import Image, ImageDraw, ImageTk
import pystray

from bleak import BleakClient, BleakScanner

from src import __version__
from src.ble_utils import is_writable, resolve_write_characteristic
from src.bridge import (
    LOGGER,
    PrintBridgeServer,
    is_port_listening,
    setup_logging,
)
from src.config import ASSETS_DIR, BASE_DIR, BridgeConfig, load_settings, save_settings

LOGO_PNG = ASSETS_DIR / "logo.png"
LOGO_ICO = ASSETS_DIR / "logo.ico"

STARTUP_LNK = (
    Path.home()
    / "AppData"
    / "Roaming"
    / "Microsoft"
    / "Windows"
    / "Start Menu"
    / "Programs"
    / "Startup"
    / "JK402A-BLE-Bridge.lnk"
)


class QueueLogHandler(logging.Handler):
    """Encaminha registros de log para uma fila segura entre threads."""

    def __init__(self, log_queue: queue.Queue[str]) -> None:
        super().__init__()
        self.log_queue = log_queue
        self.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")
        )

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.log_queue.put_nowait(self.format(record))
        except Exception:
            pass


class BridgeRunner:
    """Executa o ``PrintBridgeServer`` em uma thread dedicada com event loop asyncio."""

    def __init__(self, config: BridgeConfig) -> None:
        self.config = config
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._server: PrintBridgeServer | None = None
        self._ready = threading.Event()
        self._error: Exception | None = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and self._server is not None

    def start(self, config: BridgeConfig) -> None:
        if self.is_running:
            self.update_config(config)
            return
        self.config = config
        self._ready.clear()
        self._error = None
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="bridge-loop")
        self._thread.start()
        self._ready.wait(timeout=5.0)
        if self._error is not None:
            raise self._error

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        server = PrintBridgeServer(self.config)
        self._server = server

        async def runner() -> None:
            await server.start()
            self._ready.set()
            assert server._server is not None
            async with server._server:
                await server._server.serve_forever()

        try:
            loop.run_until_complete(runner())
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._error = exc
            LOGGER.error("Erro no servidor bridge: %s", exc)
            self._ready.set()
        finally:
            try:
                loop.run_until_complete(server.stop())
            except Exception:
                pass
            self._server = None
            self._loop = None
            loop.close()

    def update_config(self, config: BridgeConfig) -> None:
        """Aplica novas configurações ao vivo sem derrubar o servidor TCP."""
        self.config = config
        if self._server is not None:
            self._server.config = config
            self._server.sender.update_config(config)
            LOGGER.info(
                "Configuração aplicada: chunk=%dB, delay=%.3fs, espera=%.1fs, response=%s, trim_bitmap=%s",
                config.chunk_size,
                config.chunk_delay,
                config.post_send_delay,
                bool(config.force_write_with_response),
                config.trim_bitmap_margins,
            )

    def enqueue_test_label(self) -> None:
        """Enfileira uma etiqueta TSPL simples para validar a conexão BLE."""
        if not self.is_running or self._loop is None or self._server is None:
            raise RuntimeError("Inicie o bridge interno antes de enviar o teste.")
        payload = (
            b"SIZE 100 mm, 150 mm\r\n"
            b"GAP 3 mm, 0 mm\r\n"
            b"CLS\r\n"
            b"BOX 40,40,360,180,4\r\n"
            b"PRINT 1,1\r\n"
        )
        self._loop.call_soon_threadsafe(self._server.queue.put_nowait, payload)
        LOGGER.info("Etiqueta de teste TSPL enfileirada (%d bytes).", len(payload))

    def stop(self) -> None:
        if self._loop is not None and self._server is not None:
            fut = asyncio.run_coroutine_threadsafe(self._server.stop(), self._loop)
            try:
                fut.result(timeout=3.0)
            except Exception:
                pass
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        self._thread = None
        self._server = None
        LOGGER.info("Bridge parado.")


def create_tray_icon_image(active: bool) -> Image.Image:
    """Carrega ``assets/logo.png`` (com indicador de status no canto) para a bandeja."""
    if LOGO_PNG.exists():
        img = Image.open(LOGO_PNG).convert("RGBA").resize((64, 64), Image.Resampling.LANCZOS)
        draw = ImageDraw.Draw(img)
        dot_color = (22, 163, 74, 255) if active else (156, 163, 175, 255)
        draw.ellipse((42, 42, 62, 62), fill=dot_color, outline=(255, 255, 255, 255), width=2)
        return img

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    bg = (22, 163, 74, 255) if active else (107, 114, 128, 255)
    draw.rounded_rectangle((4, 4, 60, 60), radius=14, fill=bg)
    draw.rectangle((18, 16, 46, 30), fill=(255, 255, 255, 255))
    draw.rectangle((14, 28, 50, 44), fill=(229, 231, 235, 255))
    draw.rectangle((20, 38, 44, 52), fill=(255, 255, 255, 255))
    return img


class BridgeTrayApp:
    """Janela principal Tkinter + ícone na bandeja do Windows (System Tray)."""

    def __init__(self, start_minimized: bool = False) -> None:
        setup_logging(verbose=False)
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.log_handler = QueueLogHandler(self.log_queue)
        LOGGER.addHandler(self.log_handler)

        self.config = load_settings()
        self.runner = BridgeRunner(self.config)
        self.tray_icon: pystray.Icon | None = None
        self._last_tray_active: bool | None = None

        # Define um AppUserModelID próprio para o Windows exibir o ícone do app na barra de tarefas
        if sys.platform == "win32":
            try:
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                    "JK402A.BLE.Bridge.1"
                )
            except Exception:
                pass

        self.root = tk.Tk()
        self.root.title(f"JK-402A BLE Print Bridge v{__version__}")
        self.root.geometry("720x660")
        self.root.minsize(640, 580)
        self.root.protocol("WM_DELETE_WINDOW", self.hide_window)
        self._apply_window_icon()

        self._build_ui()
        self._auto_start_if_free()
        if start_minimized:
            self.root.withdraw()

        self._start_tray()
        self.root.after(150, self._poll_logs)
        self.root.after(1000, self._refresh_status)

    def _apply_window_icon(self) -> None:
        if LOGO_ICO.exists():
            try:
                self.root.iconbitmap(str(LOGO_ICO))
            except Exception as exc:
                LOGGER.debug("Falha ao aplicar iconbitmap: %s", exc)
        if LOGO_PNG.exists():
            try:
                img = Image.open(LOGO_PNG).convert("RGBA").resize(
                    (64, 64), Image.Resampling.LANCZOS
                )
                self._window_icon_photo = ImageTk.PhotoImage(img)
                self.root.iconphoto(True, self._window_icon_photo)
            except Exception as exc:
                LOGGER.debug("Falha ao aplicar iconphoto: %s", exc)

    def _build_ui(self) -> None:
        main = ttk.Frame(self.root, padding=12)
        main.pack(fill=tk.BOTH, expand=True)

        status_frame = ttk.LabelFrame(main, text="Status do Serviço Bridge (127.0.0.1:9100)", padding=10)
        status_frame.pack(fill=tk.X, pady=(0, 10))

        self.status_var = tk.StringVar(value="Verificando...")
        self.status_label = ttk.Label(
            status_frame, textvariable=self.status_var, font=("Segoe UI", 10, "bold")
        )
        self.status_label.pack(side=tk.LEFT, padx=(0, 12))

        btn_box = ttk.Frame(status_frame)
        btn_box.pack(side=tk.RIGHT)
        self.btn_start = ttk.Button(btn_box, text="Iniciar", command=self.on_start_bridge)
        self.btn_start.pack(side=tk.LEFT, padx=3)
        self.btn_stop = ttk.Button(btn_box, text="Parar", command=self.on_stop_bridge)
        self.btn_stop.pack(side=tk.LEFT, padx=3)
        ttk.Button(btn_box, text="Minimizar p/ Bandeja", command=self.hide_window).pack(
            side=tk.LEFT, padx=3
        )

        # Descoberta de impressora BLE (MAC & UUID) direto na interface
        ble_frame = ttk.LabelFrame(main, text="Impressora BLE (MAC & UUID de Escrita)", padding=10)
        ble_frame.pack(fill=tk.X, pady=(0, 10))

        self.mac_var = tk.StringVar(value=self.config.mac_address)
        self.uuid_var = tk.StringVar(value=self.config.write_uuid)
        self._mac_label_to_addr: dict[str, str] = {}

        ble_grid = ttk.Frame(ble_frame)
        ble_grid.pack(fill=tk.X)
        ble_grid.columnconfigure(1, weight=1)

        ttk.Label(ble_grid, text="Dispositivo / MAC:").grid(row=0, column=0, sticky=tk.W, pady=3)
        self.mac_combo = ttk.Combobox(ble_grid, textvariable=self.mac_var)
        self.mac_combo.grid(row=0, column=1, sticky=tk.EW, padx=(8, 8), pady=3)
        self.mac_combo.bind("<<ComboboxSelected>>", self._on_mac_selected)
        self.btn_scan_ble = ttk.Button(ble_grid, text="Buscar BLE", command=self.on_scan_ble)
        self.btn_scan_ble.grid(row=0, column=2, sticky=tk.E, pady=3)

        ttk.Label(ble_grid, text="UUID de Escrita:").grid(row=1, column=0, sticky=tk.W, pady=3)
        self.uuid_combo = ttk.Combobox(ble_grid, textvariable=self.uuid_var)
        self.uuid_combo.grid(row=1, column=1, sticky=tk.EW, padx=(8, 8), pady=3)
        self.btn_scan_uuid = ttk.Button(
            ble_grid, text="Listar UUIDs", command=self.on_discover_uuids
        )
        self.btn_scan_uuid.grid(row=1, column=2, sticky=tk.E, pady=3)

        cfg_frame = ttk.LabelFrame(main, text="Ajuste de Chunks & Velocidade BLE", padding=10)
        cfg_frame.pack(fill=tk.X, pady=(0, 10))

        self.chunk_var = tk.StringVar(value=str(self.config.chunk_size))
        self.delay_ms_var = tk.StringVar(value=str(int(round(self.config.chunk_delay * 1000))))
        self.post_delay_var = tk.StringVar(value=f"{self.config.post_send_delay:.1f}")
        self.with_resp_var = tk.BooleanVar(value=bool(self.config.force_write_with_response))
        self.trim_bitmap_var = tk.BooleanVar(value=bool(self.config.trim_bitmap_margins))

        grid = ttk.Frame(cfg_frame)
        grid.pack(fill=tk.X)

        ttk.Label(grid, text="Chunk Size (bytes):").grid(row=0, column=0, sticky=tk.W, pady=4)
        ttk.Entry(grid, textvariable=self.chunk_var, width=10).grid(
            row=0, column=1, sticky=tk.W, padx=(6, 18)
        )

        ttk.Label(grid, text="Delay entre chunks (ms):").grid(row=0, column=2, sticky=tk.W, pady=4)
        ttk.Entry(grid, textvariable=self.delay_ms_var, width=10).grid(
            row=0, column=3, sticky=tk.W, padx=(6, 18)
        )

        ttk.Label(grid, text="Espera final (s):").grid(row=0, column=4, sticky=tk.W, pady=4)
        ttk.Entry(grid, textvariable=self.post_delay_var, width=8).grid(
            row=0, column=5, sticky=tk.W, padx=(6, 0)
        )

        ttk.Checkbutton(
            cfg_frame,
            text="Usar Write With Response (ACK por pacote - mais lento, mais conservador)",
            variable=self.with_resp_var,
        ).pack(anchor=tk.W, pady=(6, 2))

        ttk.Checkbutton(
            cfg_frame,
            text="Recortar margens brancas do BITMAP TSPL (reduz bytes enviados sem perder qualidade)",
            variable=self.trim_bitmap_var,
        ).pack(anchor=tk.W, pady=(0, 6))

        presets = ttk.Frame(cfg_frame)
        presets.pack(fill=tk.X, pady=(2, 6))
        ttk.Label(presets, text="Presets:").pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(
            presets, text="180B / 20ms (Seguro)", command=lambda: self._apply_preset(180, 20, 1.5, False)
        ).pack(side=tk.LEFT, padx=3)
        ttk.Button(
            presets, text="480B / 15ms (Rápido)", command=lambda: self._apply_preset(480, 15, 1.5, False)
        ).pack(side=tk.LEFT, padx=3)
        ttk.Button(
            presets, text="512B / 5ms (Turbo)", command=lambda: self._apply_preset(512, 5, 1.0, False)
        ).pack(side=tk.LEFT, padx=3)
        ttk.Button(
            presets, text="512B / 0ms (Máximo)", command=lambda: self._apply_preset(512, 0, 1.0, False)
        ).pack(side=tk.LEFT, padx=3)

        actions = ttk.Frame(cfg_frame)
        actions.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(actions, text="Salvar & Aplicar", command=self.on_save_config).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(actions, text="Testar Impressão Rápida (BOX)", command=self.on_test_print).pack(
            side=tk.LEFT, padx=6
        )

        self.startup_var = tk.StringVar()
        self.btn_startup = ttk.Button(
            actions, textvariable=self.startup_var, command=self.on_toggle_startup
        )
        self.btn_startup.pack(side=tk.RIGHT)
        self._update_startup_button()

        log_frame = ttk.LabelFrame(main, text="Log ao Vivo (acompanhe o tempo em segundos)", padding=8)
        log_frame.pack(fill=tk.BOTH, expand=True)

        self.log_text = scrolledtext.ScrolledText(
            log_frame, height=12, state=tk.DISABLED, font=("Consolas", 9)
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            main,
            text=f"JK-402A BLE Bridge • Versão {__version__}",
            foreground="#6b7280",
            font=("Segoe UI", 8),
        ).pack(anchor=tk.E, pady=(4, 0))

    def _apply_preset(self, chunk: int, delay_ms: int, post_s: float, with_resp: bool) -> None:
        self.chunk_var.set(str(chunk))
        self.delay_ms_var.set(str(delay_ms))
        self.post_delay_var.set(f"{post_s:.1f}")
        self.with_resp_var.set(with_resp)
        self.on_save_config()

    def _read_ui_config(self) -> BridgeConfig:
        mac = self._extract_mac(self.mac_var.get())
        write_uuid = self.uuid_var.get().strip()
        chunk = max(20, min(512, int(self.chunk_var.get().strip())))
        delay_ms = max(0, int(self.delay_ms_var.get().strip()))
        post_s = max(0.0, float(self.post_delay_var.get().strip().replace(",", ".")))
        return BridgeConfig(
            mac_address=mac,
            write_uuid=write_uuid,
            tcp_host=self.config.tcp_host,
            tcp_port=self.config.tcp_port,
            chunk_size=chunk,
            chunk_delay=delay_ms / 1000.0,
            post_send_delay=post_s,
            scan_timeout=self.config.scan_timeout,
            max_retries=self.config.max_retries,
            retry_delay=self.config.retry_delay,
            force_write_with_response=self.with_resp_var.get(),
            trim_bitmap_margins=self.trim_bitmap_var.get(),
        )

    def _extract_mac(self, value: str) -> str:
        text = value.strip()
        if text in self._mac_label_to_addr:
            return self._mac_label_to_addr[text]
        if "(" in text and text.endswith(")"):
            return text.rsplit("(", 1)[-1][:-1].strip()
        return text

    def _on_mac_selected(self, _event: tk.Event[tk.Misc]) -> None:
        self.mac_var.set(self._extract_mac(self.mac_var.get()))

    def on_scan_ble(self) -> None:
        self.btn_scan_ble.configure(state=tk.DISABLED)
        LOGGER.info("Buscando dispositivos BLE próximos (5s)...")

        def worker() -> None:
            try:
                devices = asyncio.run(BleakScanner.discover(timeout=5.0))
                labels: list[str] = []
                mapping: dict[str, str] = {}
                preferred_addr: str | None = None
                for dev in devices:
                    name = dev.name or "Sem nome"
                    label = f"{name} ({dev.address})"
                    labels.append(label)
                    mapping[label] = dev.address
                    if "jk-402" in name.lower() and preferred_addr is None:
                        preferred_addr = dev.address

                def apply_results() -> None:
                    self._mac_label_to_addr = mapping
                    self.mac_combo["values"] = labels
                    if preferred_addr:
                        self.mac_var.set(preferred_addr)
                    LOGGER.info("Busca BLE concluída: %d dispositivo(s) encontrado(s).", len(labels))
                    self.btn_scan_ble.configure(state=tk.NORMAL)

                self.root.after(0, apply_results)
            except Exception as exc:
                def on_err() -> None:
                    LOGGER.error("Erro ao buscar dispositivos BLE: %s", exc)
                    self.btn_scan_ble.configure(state=tk.NORMAL)

                self.root.after(0, on_err)

        threading.Thread(target=worker, daemon=True, name="ble-scan-ui").start()

    def on_discover_uuids(self) -> None:
        mac = self._extract_mac(self.mac_var.get())
        if not mac:
            messagebox.showwarning("MAC inválido", "Informe ou busque um endereço MAC primeiro.")
            return
        self.btn_scan_uuid.configure(state=tk.DISABLED)
        LOGGER.info("Conectando em %s para listar UUIDs graváveis...", mac)

        def worker() -> None:
            async def fetch() -> tuple[list[str], str]:
                device = await BleakScanner.find_device_by_address(mac, timeout=8.0)
                if device is None:
                    raise ConnectionError(f"Dispositivo {mac} não encontrado.")
                async with BleakClient(device, timeout=10.0) as client:
                    writable = [
                        char.uuid
                        for service in client.services
                        for char in service.characteristics
                        if is_writable(char)
                    ]
                    recommended, _ = resolve_write_characteristic(client.services)
                    return writable, recommended

            try:
                writable, recommended = asyncio.run(fetch())

                def apply_uuids() -> None:
                    self.uuid_combo["values"] = writable
                    self.uuid_var.set(recommended)
                    LOGGER.info(
                        "UUIDs graváveis encontrados (%d). Selecionado: %s",
                        len(writable),
                        recommended,
                    )
                    self.btn_scan_uuid.configure(state=tk.NORMAL)

                self.root.after(0, apply_uuids)
            except Exception as exc:
                def on_err() -> None:
                    LOGGER.error("Erro ao listar UUIDs de %s: %s", mac, exc)
                    self.btn_scan_uuid.configure(state=tk.NORMAL)

                self.root.after(0, on_err)

        threading.Thread(target=worker, daemon=True, name="ble-uuid-ui").start()

    def _auto_start_if_free(self) -> None:
        if is_port_listening(self.config.tcp_host, self.config.tcp_port):
            LOGGER.info(
                "Porta %d já está em uso por outro processo do bridge em segundo plano.",
                self.config.tcp_port,
            )
            return
        try:
            self.runner.start(self.config)
        except Exception as exc:
            LOGGER.error("Falha ao iniciar bridge automaticamente: %s", exc)

    def on_start_bridge(self) -> None:
        if is_port_listening(self.config.tcp_host, self.config.tcp_port) and not self.runner.is_running:
            messagebox.showinfo(
                "Bridge já ativo",
                "Já existe um processo do bridge rodando em segundo plano na porta 9100.\n"
                "Clique em 'Parar' primeiro se quiser assumir o controle por este painel.",
            )
            return
        try:
            cfg = self._read_ui_config()
            save_settings(cfg)
            self.config = cfg
            self.runner.start(cfg)
        except Exception as exc:
            messagebox.showerror("Erro ao iniciar", str(exc))
        self._refresh_status()

    def on_stop_bridge(self) -> None:
        if self.runner.is_running:
            self.runner.stop()
        elif is_port_listening(self.config.tcp_host, self.config.tcp_port):
            stop_script = BASE_DIR / "scripts" / "stop_bridge.ps1"
            subprocess.run(
                ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(stop_script)],
                creationflags=subprocess.CREATE_NO_WINDOW,
                check=False,
            )
        self._refresh_status()

    def on_save_config(self) -> None:
        try:
            cfg = self._read_ui_config()
        except ValueError:
            messagebox.showerror("Valores inválidos", "Verifique os números de chunk, delay e espera.")
            return
        save_settings(cfg)
        self.config = cfg
        if self.runner.is_running:
            self.runner.update_config(cfg)
        else:
            LOGGER.info("Configuração salva em bridge_settings.json.")

    def on_test_print(self) -> None:
        try:
            self.on_save_config()
            self.runner.enqueue_test_label()
        except Exception as exc:
            messagebox.showwarning("Aviso", str(exc))

    def _update_startup_button(self) -> None:
        if STARTUP_LNK.exists():
            self.startup_var.set("Auto-iniciar Windows: ATIVO")
        else:
            self.startup_var.set("Auto-iniciar Windows: DESLIGADO")

    def on_toggle_startup(self) -> None:
        script = BASE_DIR / "scripts" / "install_startup.ps1"
        args = ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(script)]
        if STARTUP_LNK.exists():
            args.append("-Uninstall")
        subprocess.run(args, creationflags=subprocess.CREATE_NO_WINDOW, check=False)
        self._update_startup_button()

    def _refresh_status(self) -> None:
        if self.runner.is_running:
            self.status_var.set(f"● Ativo neste painel ({self.config.tcp_host}:{self.config.tcp_port})")
            self.status_label.configure(foreground="#15803d")
            active = True
        elif is_port_listening(self.config.tcp_host, self.config.tcp_port):
            self.status_var.set(
                f"● Rodando em segundo plano (processo externo na porta {self.config.tcp_port})"
            )
            self.status_label.configure(foreground="#b45309")
            active = True
        else:
            self.status_var.set("● Parado")
            self.status_label.configure(foreground="#b91c1c")
            active = False

        if self.tray_icon is not None and active != self._last_tray_active:
            self._last_tray_active = active
            self.tray_icon.icon = create_tray_icon_image(active)
            self.tray_icon.title = (
                f"JK-402A Bridge v{__version__}: Ativo (127.0.0.1:9100)"
                if active
                else f"JK-402A Bridge v{__version__}: Parado"
            )

        self._update_startup_button()
        self.root.after(1200, self._refresh_status)

    def _poll_logs(self) -> None:
        lines: list[str] = []
        while True:
            try:
                lines.append(self.log_queue.get_nowait())
            except queue.Empty:
                break
        if lines:
            self.log_text.configure(state=tk.NORMAL)
            for line in lines:
                self.log_text.insert(tk.END, line + "\n")
            self.log_text.see(tk.END)
            self.log_text.configure(state=tk.DISABLED)
        self.root.after(150, self._poll_logs)

    def _start_tray(self) -> None:
        menu = pystray.Menu(
            pystray.MenuItem("Abrir Painel", lambda: self.root.after(0, self.show_window), default=True),
            pystray.MenuItem("Iniciar Bridge", lambda: self.root.after(0, self.on_start_bridge)),
            pystray.MenuItem("Parar Bridge", lambda: self.root.after(0, self.on_stop_bridge)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Sair", lambda: self.root.after(0, self.quit_app)),
        )
        self.tray_icon = pystray.Icon(
            "jk402a_bridge",
            create_tray_icon_image(self.runner.is_running),
            "JK-402A Bridge",
            menu,
        )
        threading.Thread(target=self.tray_icon.run, daemon=True, name="tray-icon").start()

    def show_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide_window(self) -> None:
        self.root.withdraw()

    def quit_app(self) -> None:
        if self.runner.is_running:
            self.runner.stop()
        if self.tray_icon is not None:
            self.tray_icon.stop()
        LOGGER.removeHandler(self.log_handler)
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main() -> int:
    start_minimized = "--minimized" in sys.argv
    app = BridgeTrayApp(start_minimized=start_minimized)
    app.run()
    return 0
