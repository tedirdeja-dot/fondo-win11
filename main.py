"""
Zoom Earth Wallpaper para Windows 10/11
=======================================

Captura periódicamente la vista indicada de Zoom Earth, escribe la fecha y
hora en amarillo en la esquina inferior derecha y la establece como fondo
de escritorio. El programa queda en el área de notificación de Windows.

Dependencias (el programa intenta instalarlas la primera vez):
    Pillow
    pystray

Requisitos:
    - Windows 10/11
    - Microsoft Edge instalado
    - conexión a Internet

Uso:
    python zoom_earth_wallpaper.py

El periodo y la imagen se guardan en:
    %APPDATA%\\ZoomEarthWallpaper\\config.json

Nota: Zoom Earth es un sitio dinámico. La captura utiliza el Edge instalado
en el equipo para conservar el aspecto y la posición de la URL solicitada.
"""

from __future__ import annotations

import base64
import ctypes
import functools
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse


APP_NAME = "Zoom Earth Wallpaper"
ZOOM_EARTH_URL = (
    "https://zoom.earth/maps/satellite/"
    "#view=31.95,-24.39,5z/overlays=labels:off"
)

APP_DIR = Path(os.environ.get("APPDATA", Path.home())) / "ZoomEarthWallpaper"
CONFIG_FILE = APP_DIR / "config.json"
WALLPAPER_FILE = APP_DIR / "zoom_earth_wallpaper.jpg"
LOG_FILE = APP_DIR / "zoom_earth_wallpaper.log"

DEFAULT_CONFIG: dict[str, Any] = {
    "interval_minutes": 15,
    "wallpaper_file": str(WALLPAPER_FILE),
    "source_url": ZOOM_EARTH_URL,
    "last_update": None,
}

# Márgenes de la interfaz de Zoom Earth que aparecen en la imagen adjunta.
# El navegador se ejecuta en modo headless, por lo que la barra del navegador
# no aparece; estos márgenes solo eliminan la interfaz HTML del mapa:
#   - izquierda: menú de capas
#   - derecha: búsqueda, ajustes, zoom y demás botones
#   - abajo: línea de tiempo y controles de reproducción
SATELLITE_CROP_LEFT = 0.18
SATELLITE_CROP_RIGHT = 0.965
SATELLITE_CROP_TOP = 0.00
SATELLITE_CROP_BOTTOM = 0.84

# Se cargan bajo demanda para que --self-test y la importación del módulo
# sigan siendo posibles en equipos donde aún no se han instalado.
Image = None
ImageDraw = None
ImageFont = None
ImageOps = None
pystray = None


def load_optional_dependencies(install_if_missing: bool = True) -> None:
    """Carga Pillow y pystray; los instala una sola vez si faltan."""
    global Image, ImageDraw, ImageFont, ImageOps, pystray

    try:
        from PIL import Image as pil_image
        from PIL import ImageDraw as pil_image_draw
        from PIL import ImageFont as pil_image_font
        from PIL import ImageOps as pil_image_ops
        import pystray as tray

        Image = pil_image
        ImageDraw = pil_image_draw
        ImageFont = pil_image_font
        ImageOps = pil_image_ops
        pystray = tray
        return
    except ImportError as exc:
        if not install_if_missing:
            raise RuntimeError(
                "Faltan las dependencias Pillow y/o pystray. "
                "Instálalas con: python -m pip install Pillow pystray"
            ) from exc

    print("Instalando dependencias Pillow y pystray...")
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--user",
        "--disable-pip-version-check",
        "Pillow",
        "pystray",
    ]
    try:
        subprocess.check_call(command)
    except (OSError, subprocess.CalledProcessError) as install_error:
        raise RuntimeError(
            "No se pudieron instalar Pillow y pystray. "
            "Ejecuta manualmente: python -m pip install Pillow pystray"
        ) from install_error

    from PIL import Image as pil_image
    from PIL import ImageDraw as pil_image_draw
    from PIL import ImageFont as pil_image_font
    from PIL import ImageOps as pil_image_ops
    import pystray as tray

    Image = pil_image
    ImageDraw = pil_image_draw
    ImageFont = pil_image_font
    ImageOps = pil_image_ops
    pystray = tray


def load_config() -> dict[str, Any]:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    config = dict(DEFAULT_CONFIG)

    try:
        with CONFIG_FILE.open("r", encoding="utf-8") as file:
            stored = json.load(file)
        if isinstance(stored, dict):
            config.update(stored)
    except (OSError, ValueError, TypeError):
        pass

    try:
        interval = int(config.get("interval_minutes", 15))
    except (ValueError, TypeError):
        interval = 15
    config["interval_minutes"] = max(1, min(interval, 1440))
    config["wallpaper_file"] = str(
        Path(config.get("wallpaper_file") or WALLPAPER_FILE)
    )
    source_url = str(config.get("source_url") or ZOOM_EARTH_URL).strip()
    config["source_url"] = (
        source_url if is_valid_zoom_earth_url(source_url) else ZOOM_EARTH_URL
    )
    return config


def save_config(config: dict[str, Any]) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    temporary = CONFIG_FILE.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(config, file, ensure_ascii=False, indent=2)
    temporary.replace(CONFIG_FILE)


def log_message(message: str) -> None:
    """Guarda errores de ejecución, especialmente cuando se usa --windowed."""
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as file:
            file.write(f"[{datetime.now().isoformat(timespec='seconds')}] {message}\n")
    except OSError:
        pass


def is_valid_zoom_earth_url(value: str) -> bool:
    """Comprueba que la fuente es una dirección HTTP(S) de Zoom Earth."""
    try:
        parsed = urlparse(value.strip())
    except ValueError:
        return False
    hostname = (parsed.hostname or "").lower().rstrip(".")
    return (
        parsed.scheme.lower() in {"http", "https"}
        and hostname
        and (hostname == "zoom.earth" or hostname.endswith(".zoom.earth"))
    )


def get_screen_size() -> tuple[int, int]:
    """Devuelve el tamaño del monitor principal en píxeles."""
    if os.name != "nt":
        return 1920, 1080
    try:
        user32 = ctypes.windll.user32
        try:
            user32.SetProcessDPIAware()
        except AttributeError:
            pass
        width = int(user32.GetSystemMetrics(0))
        height = int(user32.GetSystemMetrics(1))
        if width > 0 and height > 0:
            return width, height
    except (AttributeError, OSError):
        pass
    return 1920, 1080


def find_edge() -> Optional[str]:
    """Encuentra Edge en las rutas habituales de Windows."""
    candidates: list[Optional[str]] = [
        shutil.which("msedge"),
        shutil.which("msedge.exe"),
        os.path.join(
            os.environ.get("PROGRAMFILES(X86)", ""),
            "Microsoft", "Edge", "Application", "msedge.exe",
        ),
        os.path.join(
            os.environ.get("PROGRAMFILES", ""),
            "Microsoft", "Edge", "Application", "msedge.exe",
        ),
        os.path.join(
            os.environ.get("LOCALAPPDATA", ""),
            "Microsoft", "Edge", "Application", "msedge.exe",
        ),
        # Chromium también puede servir como último recurso.
        shutil.which("chrome"),
        shutil.which("chrome.exe"),
        os.path.join(
            os.environ.get("PROGRAMFILES", ""),
            "Google", "Chrome", "Application", "chrome.exe",
        ),
        os.path.join(
            os.environ.get("PROGRAMFILES(X86)", ""),
            "Google", "Chrome", "Application", "chrome.exe",
        ),
    ]
    if os.name == "nt":
        try:
            import winreg

            registry_locations = (
                (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"),
                (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"),
                (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"),
            )
            for hive, key_path in registry_locations:
                try:
                    with winreg.OpenKey(hive, key_path) as key:
                        value, _ = winreg.QueryValueEx(key, None)
                        candidates.append(value)
                except (FileNotFoundError, OSError):
                    continue
        except ImportError:
            pass

    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(candidate)
    return None


def _font_for_timestamp(size: int):
    candidates = [
        r"C:\Windows\Fonts\segoeui.ttf",
        r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\calibri.ttf",
    ]
    for candidate in candidates:
        if Path(candidate).is_file():
            try:
                return ImageFont.truetype(candidate, size=size)
            except OSError:
                pass
    return ImageFont.load_default()


def get_bottom_taskbar_gap(image_height: int) -> int:
    """Calcula el espacio para colocar el texto encima de la barra de tareas."""
    fallback = max(45, image_height // 16)
    if os.name != "nt":
        return fallback

    try:
        from ctypes import wintypes

        class Rect(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long),
                ("top", ctypes.c_long),
                ("right", ctypes.c_long),
                ("bottom", ctypes.c_long),
            ]

        user32 = ctypes.windll.user32
        taskbar = user32.FindWindowW("Shell_TrayWnd", None)
        rect = Rect()
        if taskbar and user32.GetWindowRect(taskbar, ctypes.byref(rect)):
            taskbar_height = int(rect.bottom - rect.top)
            # Solo aplicamos el cálculo si la barra está abajo, que es la
            # ubicación donde aparece el reloj de Windows.
            if rect.top >= image_height // 2 and taskbar_height > 10:
                return taskbar_height + max(8, image_height // 240)
    except (AttributeError, OSError):
        pass
    return fallback


def add_timestamp(image_file: Path, when: datetime) -> None:
    """Añade una fecha visible en amarillo encima de la barra de tareas."""
    image = Image.open(image_file).convert("RGBA")

    # La fuente queda al 60% del tamaño anterior (reducción del 40%).
    original_font_size = max(20, min(image.width // 65, 34))
    font_size = max(12, round(original_font_size * 0.60))
    font = _font_for_timestamp(font_size)
    text = f"Última actualización: {when.strftime('%d/%m/%Y %H:%M:%S')}"

    # Se dibuja en una capa separada para que la transparencia se mezcle
    # realmente con el mapa antes de guardar el JPEG.
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay, "RGBA")
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font, stroke_width=2)
    margin = max(18, image.width // 100)
    taskbar_gap = get_bottom_taskbar_gap(image.height)
    x = image.width - right - margin
    y = max(
        margin,
        # Unos píxeles más abajo, conservando el espacio de la barra.
        image.height - bottom - taskbar_gap - max(3, image.height // 240),
    )

    draw.rounded_rectangle(
        (
            x + left,
            y + top,
            x + right,
            y + bottom,
        ),
        radius=2,
        # 50% de opacidad, sin margen adicional alrededor del texto.
        fill=(0, 0, 0, 128),
    )
    draw.text(
        (x, y),
        text,
        font=font,
        fill=(255, 235, 0, 255),
        stroke_width=2,
        stroke_fill=(0, 0, 0, 230),
    )

    image = Image.alpha_composite(image, overlay)
    image.convert("RGB").save(image_file, "JPEG", quality=94, optimize=True)


def set_windows_wallpaper(image_file: Path) -> None:
    if os.name != "nt":
        raise RuntimeError("Solo se puede establecer el fondo en Windows.")
    if not image_file.is_file():
        raise FileNotFoundError(str(image_file))

    SPI_SETDESKWALLPAPER = 20
    SPIF_UPDATE_INIFILE = 0x01
    SPIF_SENDWININICHANGE = 0x02
    result = ctypes.windll.user32.SystemParametersInfoW(
        SPI_SETDESKWALLPAPER,
        0,
        str(image_file),
        SPIF_UPDATE_INIFILE | SPIF_SENDWININICHANGE,
    )
    if not result:
        raise ctypes.WinError()


def crop_satellite_only(
    source_file: Path,
    output_file: Path,
    target_size: tuple[int, int],
) -> None:
    """Quita la interfaz HTML y adapta el mapa al tamaño del escritorio."""
    with Image.open(source_file) as source:
        left = max(0, min(source.width - 1, int(source.width * SATELLITE_CROP_LEFT)))
        top = max(0, min(source.height - 1, int(source.height * SATELLITE_CROP_TOP)))
        right = max(left + 1, min(source.width, int(source.width * SATELLITE_CROP_RIGHT)))
        bottom = max(top + 1, min(source.height, int(source.height * SATELLITE_CROP_BOTTOM)))

        satellite = source.crop((left, top, right, bottom)).convert("RGB")
        # Mantiene la proporción del monitor para que Windows no deforme la
        # imagen. El recorte adicional, si hace falta, es simétrico.
        satellite = ImageOps.fit(
            satellite,
            target_size,
            method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.5),
        )
        satellite.save(output_file, "JPEG", quality=94, optimize=True)


def capture_zoom_earth(output_file: Path, source_url: str = ZOOM_EARTH_URL) -> None:
    """Captura la dirección de Zoom Earth mediante Edge headless."""
    if not is_valid_zoom_earth_url(source_url):
        raise ValueError(
            "La dirección debe ser una URL http(s) válida de zoom.earth."
        )

    browser = find_edge()
    if not browser:
        raise RuntimeError(
            "No se encontró Microsoft Edge ni Google Chrome. "
            "Instala Edge y vuelve a ejecutar el programa."
        )

    width, height = get_screen_size()
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="zoom-earth-") as temp_dir:
        raw_file = Path(temp_dir) / "capture.png"
        errors: list[str] = []
        captured = False

        # Un perfil temporal evita que una sesión de Edge existente bloquee
        # la captura o aporte cookies/popups de otro usuario. Se prueban las
        # dos variantes de headless para cubrir versiones distintas de Edge.
        for attempt, headless_flag in enumerate(("--headless=new", "--headless")):
            profile_dir = Path(temp_dir) / f"profile-{attempt}"
            command = [
                browser,
                headless_flag,
                "--hide-scrollbars",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-extensions",
                "--run-all-compositor-stages-before-draw",
                f"--user-data-dir={profile_dir}",
                f"--window-size={width},{height}",
                "--force-device-scale-factor=1",
                # Da tiempo a que carguen el mapa y sus teselas.
                "--virtual-time-budget=20000",
                f"--screenshot={raw_file}",
                source_url.strip(),
            ]
            try:
                completed = subprocess.run(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=120,
                    check=False,
                    text=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except subprocess.TimeoutExpired:
                errors.append(f"{headless_flag}: tiempo agotado")
                continue

            if completed.returncode == 0 and raw_file.is_file() and raw_file.stat().st_size >= 10_000:
                captured = True
                break

            details = (completed.stderr or completed.stdout).strip()
            errors.append(
                f"{headless_flag}: código {completed.returncode}"
                + (f" - {details[-500:]}" if details else "")
            )
            try:
                raw_file.unlink(missing_ok=True)
            except OSError:
                pass

        if not captured:
            message = (
                "Edge no pudo generar una captura válida. "
                "Comprueba que Edge está instalado, que hay Internet y que "
                "la URL se abre normalmente."
            )
            log_message(message + " | " + " || ".join(errors))
            raise RuntimeError(message + f" Revisa: {LOG_FILE}")

        crop_satellite_only(raw_file, output_file, (width, height))


def make_tray_icon(busy: bool = False, frame: int = 0):
    """Crea un icono sencillo sin depender de archivos externos."""
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    # Globo/mapa.
    draw.ellipse((7, 7, 57, 57), fill=(20, 100, 185, 255), outline=(235, 245, 255, 255), width=2)
    draw.arc((16, 9, 48, 55), 90, 270, fill=(180, 225, 255, 220), width=2)
    draw.arc((16, 9, 48, 55), 270, 90, fill=(180, 225, 255, 220), width=2)
    draw.arc((8, 19, 56, 43), 0, 360, fill=(180, 225, 255, 200), width=2)

    if busy:
        start = (frame * 45) % 360
        draw.arc(
            (1, 1, 63, 63),
            start,
            start + 105,
            fill=(255, 235, 0, 255),
            width=6,
        )
    else:
        draw.ellipse((42, 40, 58, 56), fill=(255, 205, 0, 255), outline=(80, 50, 0, 255))
        draw.line((50, 43, 50, 51), fill=(40, 40, 40, 255), width=2)
        draw.ellipse((49, 53, 51, 55), fill=(40, 40, 40, 255))
    return image


class _DialogManager:
    """Mantiene un único intérprete Tk para todos los diálogos."""

    def __init__(self) -> None:
        self._requests: queue.Queue = queue.Queue()
        self._ready = threading.Event()
        self._startup_error: Optional[BaseException] = None
        self._closing = False
        self._active = None
        self._root = None
        self._thread = threading.Thread(
            target=self._run,
            name="zoom-earth-settings-dialog",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=10):
            raise RuntimeError("La ventana de configuración no pudo iniciarse.")
        if self._startup_error is not None:
            raise RuntimeError(
                f"No se pudo iniciar la ventana de configuración: "
                f"{self._startup_error}"
            ) from self._startup_error

    def _run(self) -> None:
        try:
            import tkinter as tk

            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            self._root = root
            self._ready.set()
            root.after(50, self._poll)
            root.mainloop()
        except BaseException as exc:
            self._startup_error = exc
            self._ready.set()
            if self._active is not None:
                self._active["error"] = exc
                self._active["done"].set()

    def _poll(self) -> None:
        if self._closing:
            if self._active is not None:
                self._finish_active(None)
            if self._root is not None:
                self._root.destroy()
            return

        if self._active is None:
            try:
                request = self._requests.get_nowait()
            except queue.Empty:
                request = None
            if request is not None:
                self._open_dialog(request)

        if self._root is not None:
            self._root.after(50, self._poll)

    def _open_dialog(self, request: dict[str, Any]) -> None:
        import tkinter as tk

        self._active = request
        dialog = tk.Toplevel(self._root)
        request["dialog"] = dialog
        dialog.title(request["title"])
        dialog.resizable(True, False)
        dialog.minsize(560, 190)
        dialog.attributes("-topmost", True)

        width, height = 760, 220
        left = max(0, (dialog.winfo_screenwidth() - width) // 2)
        top = max(0, (dialog.winfo_screenheight() - height) // 2)
        dialog.geometry(f"{width}x{height}+{left}+{top}")

        container = tk.Frame(dialog, padx=18, pady=14)
        container.pack(fill="both", expand=True)
        tk.Label(
            container,
            text=request["prompt"],
            justify="left",
            anchor="w",
            wraplength=720,
        ).pack(fill="x")

        entry = tk.Entry(container, font=("Segoe UI", 11))
        entry.insert(0, request["initial_value"])
        entry.pack(fill="x", pady=(12, 4))

        error_label = tk.Label(
            container,
            text="",
            fg="#b00020",
            anchor="w",
            justify="left",
        )
        error_label.pack(fill="x")

        buttons = tk.Frame(container)
        buttons.pack(fill="x", pady=(12, 0))

        def cancel() -> None:
            self._finish_active(None)

        def accept() -> None:
            value = entry.get().strip()
            try:
                valid = (
                    request["validator"](value)
                    if request["validator"]
                    else bool(value)
                )
            except (ValueError, TypeError):
                valid = False
            if not valid:
                error_label.config(text=request["validation_message"])
                entry.focus_force()
                entry.selection_range(0, tk.END)
                return
            self._finish_active(value)

        tk.Button(
            buttons,
            text="Aceptar",
            width=12,
            default="active",
            command=accept,
        ).pack(side="right", padx=(8, 0))
        tk.Button(
            buttons,
            text="Cancelar",
            width=12,
            command=cancel,
        ).pack(side="right")

        dialog.protocol("WM_DELETE_WINDOW", cancel)
        dialog.bind("<Return>", lambda _event: accept())
        dialog.bind("<Escape>", lambda _event: cancel())
        dialog.after(
            100,
            lambda: (
                dialog.lift(),
                dialog.focus_force(),
                entry.focus_force(),
                entry.selection_range(0, tk.END),
            ),
        )
        dialog.transient(self._root)
        dialog.grab_set()

    def _finish_active(self, value: Optional[str]) -> None:
        request = self._active
        if request is None:
            return
        self._active = None
        request["value"] = value
        try:
            request["dialog"].grab_release()
            request["dialog"].destroy()
        except Exception:
            # El diálogo puede haber sido destruido al cerrar la aplicación.
            pass
        request["done"].set()

    def ask(
        self,
        title: str,
        prompt: str,
        initial_value: str,
        validator=None,
        validation_message: str = "",
    ) -> Optional[str]:
        if self._closing:
            return None
        request: dict[str, Any] = {
            "title": title,
            "prompt": prompt,
            "initial_value": initial_value,
            "validator": validator,
            "validation_message": validation_message,
            "value": None,
            "error": None,
            "done": threading.Event(),
        }
        self._requests.put(request)
        request["done"].wait()
        if request["error"] is not None:
            raise RuntimeError(
                f"No se pudo abrir la ventana de configuración: "
                f"{request['error']}"
            ) from request["error"]
        return request["value"]

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._requests.put(None)


_dialog_manager: Optional[_DialogManager] = None
_dialog_manager_lock = threading.Lock()


def _windows_input_dialog_once(
    title: str,
    prompt: str,
    initial_value: str,
) -> Optional[str]:
    """Abre un cuadro de texto nativo de Windows Forms."""
    powershell = (
        shutil.which("powershell.exe")
        or shutil.which("powershell")
        or os.path.expandvars(
            r"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
        )
    )
    if not powershell or not Path(powershell).is_file():
        raise RuntimeError("No se encontró Windows PowerShell.")

    def encode(value: str) -> str:
        return base64.b64encode(value.encode("utf-8")).decode("ascii")

    # Las cadenas viajan codificadas para que comillas, # y caracteres
    # españoles de la URL no rompan el comando de PowerShell.
    script = r"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

function Decode-Text([string]$value) {
    return [Text.Encoding]::UTF8.GetString(
        [Convert]::FromBase64String($value)
    )
}

$titleText = Decode-Text "__TITLE__"
$promptText = Decode-Text "__PROMPT__"
$initialText = Decode-Text "__INITIAL__"

$form = New-Object System.Windows.Forms.Form
$form.Text = $titleText
$form.StartPosition = [System.Windows.Forms.FormStartPosition]::CenterScreen
$form.ClientSize = New-Object System.Drawing.Size(760, 220)
$form.MinimumSize = New-Object System.Drawing.Size(560, 190)
$form.TopMost = $true
$form.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::FixedDialog
$form.MaximizeBox = $false
$form.MinimizeBox = $false

$label = New-Object System.Windows.Forms.Label
$label.Text = $promptText
$label.Location = New-Object System.Drawing.Point(18, 16)
$label.Size = New-Object System.Drawing.Size(720, 62)

$textBox = New-Object System.Windows.Forms.TextBox
$textBox.Text = $initialText
$textBox.Location = New-Object System.Drawing.Point(18, 88)
$textBox.Size = New-Object System.Drawing.Size(720, 28)
$textBox.Anchor = (
    [System.Windows.Forms.AnchorStyles]::Top `
    -bor [System.Windows.Forms.AnchorStyles]::Left `
    -bor [System.Windows.Forms.AnchorStyles]::Right
)

$ok = New-Object System.Windows.Forms.Button
$ok.Text = "Aceptar"
$ok.DialogResult = [System.Windows.Forms.DialogResult]::OK
$ok.Location = New-Object System.Drawing.Point(558, 150)
$ok.Size = New-Object System.Drawing.Size(90, 30)

$cancel = New-Object System.Windows.Forms.Button
$cancel.Text = "Cancelar"
$cancel.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
$cancel.Location = New-Object System.Drawing.Point(458, 150)
$cancel.Size = New-Object System.Drawing.Size(90, 30)

$form.Controls.Add($label)
$form.Controls.Add($textBox)
$form.Controls.Add($ok)
$form.Controls.Add($cancel)
$form.AcceptButton = $ok
$form.CancelButton = $cancel
$form.Add_Shown({
    $textBox.SelectAll()
    $textBox.Focus()
})

$result = $form.ShowDialog()
if ($result -eq [System.Windows.Forms.DialogResult]::OK) {
    [Console]::Out.Write($textBox.Text)
}
"""
    script = (
        script.replace("__TITLE__", encode(title))
        .replace("__PROMPT__", encode(prompt))
        .replace("__INITIAL__", encode(initial_value))
    )
    encoded_script = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    completed = subprocess.run(
        [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            encoded_script,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=3600,
        check=False,
        text=True,
        creationflags=creation_flags,
    )
    if completed.returncode != 0:
        details = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(
            "Windows no pudo abrir el cuadro de configuración."
            + (f" {details[-500:]}" if details else "")
        )
    # Cancelar devuelve una salida vacía, igual que los diálogos Tk.
    value = completed.stdout.strip()
    return value or None


def _ask_windows_value(
    title: str,
    prompt: str,
    initial_value: str,
    validator=None,
    validation_message: str = "",
) -> Optional[str]:
    current_prompt = prompt
    current_value = initial_value
    while True:
        value = _windows_input_dialog_once(title, current_prompt, current_value)
        if value is None:
            return None
        try:
            valid = validator(value) if validator else bool(value)
        except (ValueError, TypeError):
            valid = False
        if valid:
            return value.strip()
        current_prompt = f"{prompt}\n\n{validation_message}"
        current_value = value


def ask_user_value(
    title: str,
    prompt: str,
    initial_value: str,
    validator=None,
    validation_message: str = "",
) -> Optional[str]:
    if os.name == "nt":
        try:
            return _ask_windows_value(
                title,
                prompt,
                initial_value,
                validator,
                validation_message,
            )
        except Exception as exc:
            # El gestor Tk sigue siendo un respaldo si PowerShell no está
            # disponible en una instalación concreta de Windows.
            print(f"[{APP_NAME}] Diálogo Windows no disponible: {exc}", file=sys.stderr)

    global _dialog_manager
    with _dialog_manager_lock:
        if _dialog_manager is None:
            _dialog_manager = _DialogManager()
        manager = _dialog_manager
    return manager.ask(
        title,
        prompt,
        initial_value,
        validator,
        validation_message,
    )


def close_user_dialogs() -> None:
    global _dialog_manager
    with _dialog_manager_lock:
        manager = _dialog_manager
        _dialog_manager = None
    if manager is not None:
        manager.close()


class ZoomEarthWallpaperApp:
    def __init__(self) -> None:
        self.config = load_config()
        self.icon = None
        self.stop_event = threading.Event()
        self.animation_stop = threading.Event()
        self.update_lock = threading.Lock()
        self.update_in_progress = False
        self.last_error: Optional[str] = None
        self.last_update: Optional[datetime] = None

    @property
    def interval_minutes(self) -> int:
        return int(self.config["interval_minutes"])

    def tooltip(self) -> str:
        if self.last_error:
            return f"{APP_NAME} - error"
        if self.last_update:
            return (
                f"{APP_NAME} - última: "
                f"{self.last_update.strftime('%d/%m/%Y %H:%M')}"
            )
        return f"{APP_NAME} - esperando actualización"

    def build_menu(self):
        periods = (
            ("5 minutos", 5),
            ("10 minutos", 10),
            ("15 minutos", 15),
            ("30 minutos", 30),
            ("1 hora", 60),
            ("2 horas", 120),
            ("6 horas", 360),
            ("12 horas", 720),
            ("24 horas", 1440),
        )
        period_items = [
            pystray.MenuItem(
                label,
                functools.partial(self._select_interval, minutes),
                checked=functools.partial(self._is_selected_interval, minutes),
                radio=True,
            )
            for label, minutes in periods
        ]
        period_items.extend(
            [
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(
                    "Personalizado...",
                    self.configure_interval,
                ),
            ]
        )
        return pystray.Menu(
            pystray.MenuItem(
                lambda item: f"Periodo actual: {self.interval_minutes} min",
                None,
                enabled=False,
            ),
            pystray.MenuItem(
                "Actualizar cada...",
                pystray.Menu(*period_items),
            ),
            pystray.MenuItem("Actualizar ahora", self.request_update),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Dirección HTML de Zoom Earth...",
                self.configure_source_url,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Salir", self.quit),
        )

    def _select_interval(self, minutes: int, _icon, _item) -> None:
        """Callback de pystray para una opción de periodo.

        Se usa functools.partial en lugar de una lambda con un tercer
        argumento. Algunas versiones de pystray interpretan ese tercer
        argumento como una firma inválida y muestran la representación de la
        función: <function ...lambda...>.
        """
        self.set_interval(minutes)

    def _is_selected_interval(self, minutes: int, _item) -> bool:
        return self.interval_minutes == minutes

    def set_interval(self, minutes: int) -> None:
        self.config["interval_minutes"] = max(1, min(int(minutes), 1440))
        save_config(self.config)
        if self.icon:
            self.icon.update_menu()

    @property
    def source_url(self) -> str:
        return str(self.config.get("source_url") or ZOOM_EARTH_URL)

    def configure_source_url(self, _icon=None, _item=None) -> None:
        # Nunca esperar a Tk dentro del callback de pystray: el callback debe
        # devolver el control inmediatamente al menú de la bandeja.
        threading.Thread(
            target=self._configure_source_url_worker,
            name="zoom-earth-source-settings",
            daemon=True,
        ).start()

    def _configure_source_url_worker(self) -> None:
        try:
            value = ask_user_value(
                APP_NAME,
                "Escribe la dirección HTML de Zoom Earth que se usará como fuente:\n"
                "Ejemplo: https://zoom.earth/maps/satellite/"
                "#view=31.95,-24.39,5z/overlays=labels:off",
                self.source_url,
                validator=is_valid_zoom_earth_url,
                validation_message=(
                    "Debe ser una URL http(s) perteneciente a zoom.earth."
                ),
            )
        except Exception as exc:
            self._settings_error(exc)
            return
        if value is not None:
            self.config["source_url"] = value
            save_config(self.config)
            # La nueva fuente se aplica inmediatamente; el trabajo pesado
            # continúa en segundo plano para que el menú siga respondiendo.
            self.request_update()

    def configure_interval(self, _icon=None, _item=None) -> None:
        threading.Thread(
            target=self._configure_interval_worker,
            name="zoom-earth-interval-settings",
            daemon=True,
        ).start()

    def _configure_interval_worker(self) -> None:
        try:
            answer = ask_user_value(
                APP_NAME,
                "¿Cada cuántos minutos quieres actualizar?\n"
                "(entre 1 y 1440 minutos)",
                str(self.interval_minutes),
                validator=lambda value: value.isdigit() and 1 <= int(value) <= 1440,
                validation_message="Escribe un número entero entre 1 y 1440.",
            )
        except Exception as exc:
            self._settings_error(exc)
            return
        if answer is not None:
            self.set_interval(int(answer))

    def _settings_error(self, error: Exception) -> None:
        self.last_error = str(error)
        print(f"[{APP_NAME}] {error}", file=sys.stderr)
        if self.icon is not None:
            self.icon.title = f"{APP_NAME} - error de configuración"

    def open_folder(self, _icon=None, _item=None) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(str(APP_DIR))
        else:
            subprocess.Popen(["xdg-open", str(APP_DIR)])

    def request_update(self, _icon=None, _item=None) -> None:
        with self.update_lock:
            if self.update_in_progress:
                return
            self.update_in_progress = True
        threading.Thread(target=self._update_worker, daemon=True).start()

    def _animate(self) -> None:
        frame = 0
        while not self.animation_stop.is_set():
            if self.icon is not None:
                self.icon.icon = make_tray_icon(busy=True, frame=frame)
                self.icon.title = f"{APP_NAME} - actualizando..."
            frame += 1
            self.animation_stop.wait(0.14)

    def _update_worker(self) -> None:
        self.animation_stop.clear()
        animation = threading.Thread(target=self._animate, daemon=True)
        animation.start()
        try:
            timestamp = datetime.now()
            wallpaper = Path(self.config["wallpaper_file"])
            capture_zoom_earth(wallpaper, self.source_url)
            add_timestamp(wallpaper, timestamp)
            set_windows_wallpaper(wallpaper)
            self.last_update = timestamp
            self.last_error = None
            self.config["last_update"] = timestamp.isoformat(timespec="seconds")
            save_config(self.config)
        except Exception as exc:  # El programa debe seguir actualizando después de un fallo.
            self.last_error = str(exc)
            log_message(str(exc))
            print(f"[{APP_NAME}] {exc}", file=sys.stderr)
        finally:
            self.animation_stop.set()
            animation.join(timeout=1)
            if self.icon is not None:
                self.icon.icon = make_tray_icon()
                self.icon.title = self.tooltip()
            with self.update_lock:
                self.update_in_progress = False

    def scheduler(self) -> None:
        # La primera actualización se dispara inmediatamente al arrancar.
        self.request_update()
        while not self.stop_event.wait(self.interval_minutes * 60):
            self.request_update()

    def setup(self, icon) -> None:
        self.icon = icon
        # Solicita explícitamente que Windows muestre el icono en el área de
        # notificación, junto al reloj (Windows puede moverlo al menú ^ si el
        # usuario oculta iconos de la bandeja).
        icon.visible = True
        icon.title = self.tooltip()
        threading.Thread(target=self.scheduler, daemon=True).start()

    def quit(self, _icon=None, _item=None) -> None:
        self.stop_event.set()
        self.animation_stop.set()
        close_user_dialogs()
        if self.icon is not None:
            self.icon.stop()

    def run(self) -> None:
        if os.name != "nt":
            raise RuntimeError("Este programa está diseñado para ejecutarse en Windows.")
        self.icon = pystray.Icon(
            APP_NAME,
            make_tray_icon(),
            self.tooltip(),
            self.build_menu(),
        )
        self.icon.run(setup=self.setup)


def run_self_test() -> int:
    """Prueba no destructiva de las piezas que sí pueden probarse en cualquier SO."""
    load_optional_dependencies(install_if_missing=False)
    test_dir = Path(tempfile.mkdtemp(prefix="zoom-earth-self-test-"))
    try:
        test_config = test_dir / "config.json"
        test_image = test_dir / "test.jpg"
        image = Image.new("RGB", (640, 360), (30, 70, 120))
        image.save(test_image, "JPEG")
        add_timestamp(test_image, datetime(2026, 9, 14, 12, 34, 56))
        with test_image.open("rb") as file:
            valid_image = file.read(10) == b"\xff\xd8\xff\xe0\x00\x10JFIF"
        if not valid_image:
            raise RuntimeError("La prueba de imagen JPEG no pasó.")
        test_config.write_text(
            json.dumps({"interval_minutes": 15}), encoding="utf-8"
        )
        if json.loads(test_config.read_text(encoding="utf-8"))["interval_minutes"] != 15:
            raise RuntimeError("La prueba de configuración no pasó.")
        cropped_image = test_dir / "cropped.jpg"
        crop_satellite_only(test_image, cropped_image, (1280, 720))
        with Image.open(cropped_image) as cropped:
            if cropped.size != (1280, 720):
                raise RuntimeError("La prueba de recorte no pasó.")
        make_tray_icon()
        make_tray_icon(busy=True, frame=2)
        print("AUTO-TEST OK: imagen, fecha, recorte e iconos.")
        return 0
    finally:
        shutil.rmtree(test_dir, ignore_errors=True)


def show_fatal_error(message: str) -> None:
    print(message, file=sys.stderr)
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(APP_NAME, message, parent=root)
        root.destroy()
    except Exception:
        pass


def main() -> int:
    if "--self-test" in sys.argv:
        try:
            return run_self_test()
        except Exception as exc:
            print(f"AUTO-TEST FALLÓ: {exc}", file=sys.stderr)
            return 1

    try:
        load_optional_dependencies()
        ZoomEarthWallpaperApp().run()
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        show_fatal_error(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())