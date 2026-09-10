# SPDX-License-Identifier: AGPL-3.0-or-later
"""
cloud-init für den GPU-vServer.

Eine GPU allein genügt nicht: Der vServer muss beim ersten Start zum
Rechenknoten ausgebaut werden. Auf einem Standard-Image (Ubuntu 24.04) heißt
das NVIDIA-Treiber, Neustart, Docker, NVIDIA Container Toolkit und der
Worker-Container. Mit einem vorbereiteten eigenen Image entfällt alles bis auf
den Start des Workers.

Sicherheitsprinzipien, die hier festgeschrieben sind:

- Auf dem Knoten liegen keine centron-Zugangsdaten und keine S3-Schlüssel.
  Er erhält nur ein knotenbezogenes Token, mit dem er sich bei mandari
  registriert. Modelle und Audio holt er über vorsignierte, kurzlebige URLs.
- Der Knoten nimmt keine eingehenden Verbindungen an. Der Worker holt sich
  seine Aufträge selbst (Pull), eingehend ist höchstens SSH offen.
- Audio, Transkripte und Entwürfe liegen nur im RAM (tmpfs) und berühren nie
  die Platte. Auf der Platte liegen ausschließlich Modellgewichte.
- Das Token steht nur in einer Datei mit Modus 0600, nicht in Befehlszeilen
  oder der systemd-Unit, damit es nicht in Prozesslisten oder Logs landet.

Ausgegeben wird JSON unter dem ``#cloud-config``-Kopf. JSON ist gültiges YAML,
cloud-init liest es unverändert, und es gibt keine Quoting-Fehler.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

WORK_DIR = "/var/lib/mandari/work"
MODEL_DIR = "/var/lib/mandari/models"
ENV_FILE = "/etc/mandari/worker.env"
SETUP_SCRIPT = "/usr/local/sbin/mandari-gpu-setup"
UNIT_PATH = "/etc/systemd/system/mandari-worker.service"


@dataclass(frozen=True)
class NodeBootstrap:
    """Alles, was ein Knoten zum Start braucht — bewusst ohne Zugangsdaten."""

    hostname: str
    mandari_url: str
    node_token: str
    worker_image: str
    prepared_image: bool = False
    work_dir_size: str = "4g"


def _worker_env(config: NodeBootstrap) -> str:
    lines = [
        f"MANDARI_URL={config.mandari_url}",
        f"MANDARI_NODE_HOSTNAME={config.hostname}",
        f"MANDARI_NODE_TOKEN={config.node_token}",
        f"WORKER_IMAGE={config.worker_image}",
        "MANDARI_WORK_DIR=/work",
        "MANDARI_MODEL_DIR=/models",
    ]
    return "\n".join(lines) + "\n"


def _worker_unit(config: NodeBootstrap) -> str:
    return f"""[Unit]
Description=mandari Transkriptions-Worker
After=docker.service network-online.target
Wants=network-online.target
Requires=docker.service

[Service]
EnvironmentFile={ENV_FILE}
# Ohne funktionierende GPU startet der Worker nicht; mandari erkennt den
# fehlenden Heartbeat und löscht den Knoten.
ExecStartPre=/usr/bin/nvidia-smi
ExecStartPre=-/usr/bin/docker rm -f mandari-worker
ExecStartPre=/usr/bin/docker pull {config.worker_image}
ExecStart=/usr/bin/docker run --rm --name mandari-worker --gpus all \\
  --env-file {ENV_FILE} \\
  --mount type=bind,source={WORK_DIR},target=/work \\
  --mount type=bind,source={MODEL_DIR},target=/models \\
  --security-opt no-new-privileges \\
  {config.worker_image}
Restart=on-failure
RestartSec=15

[Install]
WantedBy=multi-user.target
"""


def _setup_script() -> str:
    return f"""#!/bin/sh
# Einmalige Einrichtung eines Standard-Images zum GPU-Rechenknoten.
set -eu
export DEBIAN_FRONTEND=noninteractive

# NVIDIA-Servertreiber (headless). Wirksam erst nach dem Neustart.
ubuntu-drivers install --gpgpu

# NVIDIA Container Toolkit, damit Docker die GPU an den Worker durchreicht.
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \\
  | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \\
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \\
  > /etc/apt/sources.list.d/nvidia-container-toolkit.list
apt-get update
apt-get install -y nvidia-container-toolkit
nvidia-ctk runtime configure --runtime=docker

mkdir -p {MODEL_DIR}
systemctl enable mandari-worker.service
"""


def _firewall_commands() -> list[list[str]]:
    return [
        ["ufw", "default", "deny", "incoming"],
        ["ufw", "default", "allow", "outgoing"],
        ["ufw", "allow", "OpenSSH"],
        ["ufw", "--force", "enable"],
    ]


def build_cloud_config(config: NodeBootstrap) -> dict[str, object]:
    """cloud-config als Datenstruktur (für Tests und Ausgabe)."""
    write_files: list[dict[str, str]] = [
        {"path": ENV_FILE, "permissions": "0600", "owner": "root:root", "content": _worker_env(config)},
        {"path": UNIT_PATH, "permissions": "0644", "owner": "root:root", "content": _worker_unit(config)},
    ]
    runcmd: list[list[str]] = [*_firewall_commands(), ["mkdir", "-p", WORK_DIR, MODEL_DIR]]

    document: dict[str, object] = {
        "hostname": config.hostname,
        "mounts": [
            ["tmpfs", WORK_DIR, "tmpfs", f"size={config.work_dir_size},mode=0700,noexec,nosuid,nodev", "0", "0"]
        ],
    }

    if config.prepared_image:
        # Treiber, Docker und Toolkit sind im Image; nur noch den Worker starten.
        runcmd.append(["systemctl", "daemon-reload"])
        runcmd.append(["systemctl", "enable", "--now", "mandari-worker.service"])
    else:
        document["package_update"] = True
        document["packages"] = ["docker.io", "curl", "ca-certificates", "gnupg", "ubuntu-drivers-common", "ufw"]
        write_files.append(
            {"path": SETUP_SCRIPT, "permissions": "0755", "owner": "root:root", "content": _setup_script()}
        )
        runcmd.append([SETUP_SCRIPT])
        # Der Treiber wird erst nach einem Neustart geladen. Der Worker ist
        # bereits aktiviert und startet danach von selbst.
        document["power_state"] = {"mode": "reboot", "condition": True, "message": "Neustart fuer NVIDIA-Treiber"}

    document["write_files"] = write_files
    document["runcmd"] = runcmd
    return document


def render_user_data(config: NodeBootstrap) -> str:
    """``user_data`` für ``POST /ccloud/servers``."""
    return "#cloud-config\n" + json.dumps(build_cloud_config(config), indent=2, ensure_ascii=False) + "\n"
