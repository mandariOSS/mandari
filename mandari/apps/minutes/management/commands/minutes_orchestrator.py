# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Orchestrator der GPU-Rechenknoten als Dauerprozess.

Läuft als eigener Container (`minutes-orchestrator`), weil Bereitstellung und
Abbau von VMs Minuten dauern und nicht in einem Web-Request stattfinden
können. Der Zustand liegt vollständig in der Datenbank; ein Neustart des
Containers setzt dort fort, wo er aufgehört hat.
"""

from __future__ import annotations

import signal
import time
from types import FrameType
from typing import Any

from django.core.management.base import BaseCommand, CommandParser
from django.db import close_old_connections

from apps.minutes.models_compute import ComputeSettings
from apps.minutes.node_service import run_once
from apps.minutes.provisioning.centron import CentronClient, CentronCredentials, CentronError


class Command(BaseCommand):
    help = "Erstellt und löscht GPU-vServer bei centron nach Bedarf."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--once", action="store_true", help="Nur einen Durchlauf ausführen.")
        parser.add_argument("--interval", type=int, default=60, help="Sekunden zwischen Durchläufen.")

    def handle(self, *args: Any, **options: Any) -> None:
        self._stop = False
        signal.signal(signal.SIGTERM, self._request_stop)
        signal.signal(signal.SIGINT, self._request_stop)

        interval = max(10, int(options["interval"]))
        while not self._stop:
            close_old_connections()
            self._tick()
            if options["once"]:
                return
            for _ in range(interval):
                if self._stop:
                    break
                time.sleep(1)
        self.stdout.write("Orchestrator beendet.")

    def _request_stop(self, signum: int, frame: FrameType | None) -> None:
        self._stop = True

    def _tick(self) -> None:
        config = ComputeSettings.load(use_cache=False)
        if not config.enabled:
            return
        credentials = CentronCredentials(
            client_id=config.client_id,
            client_secret=config.get_client_secret(),
            base_url=config.api_base_url,
            scope=config.scope,
        )
        try:
            with CentronClient(credentials) as client:
                for action in run_once(client, config):
                    self.stdout.write(f"{action.kind}: {action.reason} {action.node_id or ''}".rstrip())
        except CentronError as exc:
            self.stderr.write(f"centron-API: {exc}")
