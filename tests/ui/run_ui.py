"""Запуск окна Golos на имитации звука и Windows (для проверки интерфейса без Windows).

GOLOS_FAKE_WORLD=fresh|installed  — имитация установки VB-CABLE
FAKE_DOWNLOAD_DELAY, FAKE_INSTALL_DELAY — сколько секунд «идут» загрузка и установка
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import paths  # noqa: E402

paths.use()
if os.environ.get("GOLOS_FAKE_WORLD"):
    import sounddevice as sd
    import cable
    import winsys
    from fake_world import World
    world = World(sd=sd, installed=os.environ["GOLOS_FAKE_WORLD"] == "installed")
    world._sync_sd()
    world.download_delay = float(os.environ.get("FAKE_DOWNLOAD_DELAY", "0"))
    world.install_delay = float(os.environ.get("FAKE_INSTALL_DELAY", "0"))
    world.patch(winsys, cable)
import app  # noqa: E402

app.main()
