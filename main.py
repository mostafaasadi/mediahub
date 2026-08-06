import io
import os
import re
import threading

import flet as ft

from app import main as app_main
from backend import logger

_NOISE = re.compile(
    r"Gtk-WARNING|Gdk-Message|embedder.cc|FlBinaryMessenger|"
    r"FlutterEngineRemoveView"
)


def _install_quiet_stderr():
    try:
        orig_fd = os.dup(2)
        orig_file = io.open(
            orig_fd,
            "w",
            buffering=1,
            closefd=False,
            errors="replace",
        )
        read_end, write_end = os.pipe()

        def _reader():
            try:
                with os.fdopen(
                    read_end,
                    "r",
                    buffering=1,
                    errors="replace",
                ) as pipe:
                    for line in pipe:
                        if not _NOISE.search(line):
                            orig_file.write(line)
                            orig_file.flush()
            except Exception:
                pass

        threading.Thread(target=_reader, daemon=True).start()
        os.dup2(write_end, 2)
        os.close(write_end)
    except Exception:
        pass


if __name__ == "__main__":
    logger.info("Media Hub starting")
    _install_quiet_stderr()
    ft.run(app_main)
