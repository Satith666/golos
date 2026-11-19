"""Имитация Windows для тестов установки VB-CABLE: устройства, устройства по умолчанию,
окно контроля учётных записей, сайт VB-Audio и подпись установщика."""

from __future__ import annotations

import io
import threading
import time
import zipfile
from pathlib import Path

ROLES = ("0", "1", "2")
CABLE_OUT = ("{cable-out}", "CABLE Output (VB-Audio Virtual Cable)")
CABLE_IN = ("{cable-in}", "CABLE Input (VB-Audio Virtual Cable)")
CABLE_16 = ("{cable-16}", "CABLE In 16ch (VB-Audio Virtual Cable)")


def make_pack(extra: dict | None = None) -> bytes:
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as pack:
        for name in ("VBCABLE_Setup.exe", "VBCABLE_Setup_x64.exe", "VBCABLE_ControlPanel.exe",
                     "vbMmeCable64_win10.inf", "vbaudio_cable64_win10.sys", "readme.txt"):
            pack.writestr(name, b"MZ fake " + name.encode())
        for name, body in (extra or {}).items():
            pack.writestr(name, body)
    return data.getvalue()


class World:
    def __init__(self, sd=None, installed: bool = False):
        self.sd = sd
        self.capture = {"{mic}": "Микрофон (USB Camera)", "{headset}": "Микрофон (HyperX Cloud II Wireless)"}
        self.render = {"{spk}": "Динамики (Realtek(R) Audio)", "{hp}": "Наушники (HyperX Cloud II Wireless)"}
        self.def_capture = {r: "{mic}" for r in ROLES}
        self.def_render = {r: "{hp}" for r in ROLES}
        self.uac = "accept"            # accept, decline
        self.reboot_needed = False     # устройства появятся только после перезагрузки
        self.signature = (True, "CN=Vincent Burel, O=Vincent Burel, L=Paris, C=FR")
        self.page = '<a href="https://download.vb-audio.com/Download_CABLE/VBCABLE_Driver_Pack46.zip">Pack46</a>'
        self.pack = make_pack()
        self.download_delay = 0.0
        self.install_delay = 0.0
        self.installer_runs = []
        self.downloads = []
        self.run_once = "unset"
        self.rebooted = False
        self.lock = threading.Lock()
        if installed:
            self._add_cable()
            self._sync_sd()

    # ---- что «делает Windows» ----

    def _add_cable(self) -> None:
        self.capture[CABLE_OUT[0]] = CABLE_OUT[1]
        self.render[CABLE_IN[0]] = CABLE_IN[1]
        self.render[CABLE_16[0]] = CABLE_16[1]

    def _sync_sd(self) -> None:
        if self.sd is not None:
            self.sd.STATE["cable"] = CABLE_OUT[0] in self.capture

    def install(self) -> None:
        """Как ведёт себя установщик: новые устройства становятся основными."""
        self.pending = True
        if self.reboot_needed:
            return
        self._add_cable()
        self.def_render = {r: CABLE_IN[0] for r in ROLES}
        self.def_capture = {r: CABLE_OUT[0] for r in ROLES}
        self._sync_sd()

    def reboot(self) -> None:
        self.reboot_needed = False
        self._add_cable()
        self.def_render = {r: CABLE_16[0] for r in ROLES}
        self.def_capture = {r: CABLE_OUT[0] for r in ROLES}
        self._sync_sd()

    # ---- подмена модулей ----

    def patch(self, winsys, cable) -> None:
        w = self
        winsys.endpoints = lambda flow: list((w.render if flow == 0 else w.capture).items())
        winsys.capture_devices = lambda: list(w.capture.items())
        winsys.render_devices = lambda: list(w.render.items())
        winsys.default_ids = lambda flow: dict(w.def_render if flow == 0 else w.def_capture)
        winsys.default_capture_ids = lambda: dict(w.def_capture)
        winsys.default_render_ids = lambda: dict(w.def_render)

        def set_default(device_id, roles=(0, 1, 2)):
            if device_id in w.capture:
                for role in roles:
                    w.def_capture[str(role)] = device_id
            elif device_id in w.render:
                for role in roles:
                    w.def_render[str(role)] = device_id
            else:
                raise OSError("SetDefaultEndpoint: 0x80070490")
        winsys.set_default = set_default

        def set_run_once(command, name="Golos"):
            w.run_once = command
        winsys.set_run_once = set_run_once
        winsys.reboot = lambda: setattr(w, "rebooted", True)

        def fetch_page():
            return w.page
        cable.fetch_page = fetch_page

        def download(url, target, progress):
            w.downloads.append(url)
            if not url.endswith("Pack46.zip") and "Pack46" in w.page:
                import urllib.error
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
            steps = 20
            for i in range(1, steps + 1):
                if w.download_delay:
                    time.sleep(w.download_delay / steps)
                progress(i * 1000, steps * 1000)
            Path(target).write_bytes(w.pack)
        cable.download = download
        cable.check_signature = lambda path: w.signature

        def run_installer(setup):
            w.installer_runs.append(Path(setup).name)
            if w.install_delay:
                time.sleep(w.install_delay)
            if w.uac == "decline":
                raise PermissionError("отменено пользователем")
            w.install()
            return 0
        cable.run_installer = run_installer
        cable.DEVICE_WAIT = 1.5
