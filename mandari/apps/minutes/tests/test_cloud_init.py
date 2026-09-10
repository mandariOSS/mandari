# SPDX-License-Identifier: AGPL-3.0-or-later
"""cloud-init des GPU-vServers: Aufbau und Sicherheitseigenschaften."""

import json

from apps.minutes.provisioning.cloud_init import (
    ENV_FILE,
    SETUP_SCRIPT,
    UNIT_PATH,
    WORK_DIR,
    NodeBootstrap,
    build_cloud_config,
    render_user_data,
)

TOKEN = "knoten-token-nur-fuer-diesen-knoten"


def bootstrap(*, prepared: bool = False) -> NodeBootstrap:
    return NodeBootstrap(
        hostname="mandari-gpu-01",
        mandari_url="https://mandari.example.de",
        node_token=TOKEN,
        worker_image="ghcr.io/mandarioss/transcriber:1.0.0",
        prepared_image=prepared,
    )


def files_by_path(document: dict[str, object]) -> dict[str, dict[str, str]]:
    files = document["write_files"]
    assert isinstance(files, list)
    return {entry["path"]: entry for entry in files}


def test_user_data_ist_gueltiges_cloud_config() -> None:
    user_data = render_user_data(bootstrap())
    header, body = user_data.split("\n", 1)
    assert header == "#cloud-config"
    assert json.loads(body)["hostname"] == "mandari-gpu-01"


def test_token_steht_nur_in_der_geschuetzten_env_datei() -> None:
    document = build_cloud_config(bootstrap())
    files = files_by_path(document)
    assert TOKEN in files[ENV_FILE]["content"]
    assert files[ENV_FILE]["permissions"] == "0600"
    assert TOKEN not in files[UNIT_PATH]["content"]
    assert TOKEN not in json.dumps(document["runcmd"])


def test_arbeitsdaten_liegen_nur_im_ram() -> None:
    document = build_cloud_config(bootstrap())
    mounts = document["mounts"]
    assert isinstance(mounts, list)
    tmpfs = [mount for mount in mounts if mount[1] == WORK_DIR]
    assert tmpfs and tmpfs[0][0] == "tmpfs"
    assert "noexec" in tmpfs[0][3]


def test_eingehende_verbindungen_sind_gesperrt() -> None:
    runcmd = json.dumps(build_cloud_config(bootstrap())["runcmd"])
    assert '["ufw", "default", "deny", "incoming"]' in runcmd
    assert '["ufw", "--force", "enable"]' in runcmd


def test_standard_image_richtet_treiber_ein_und_startet_neu() -> None:
    document = build_cloud_config(bootstrap(prepared=False))
    files = files_by_path(document)
    assert "ubuntu-drivers install --gpgpu" in files[SETUP_SCRIPT]["content"]
    assert "nvidia-container-toolkit" in files[SETUP_SCRIPT]["content"]
    assert "systemctl enable mandari-worker.service" in files[SETUP_SCRIPT]["content"]
    assert document["power_state"] == {"mode": "reboot", "condition": True, "message": "Neustart fuer NVIDIA-Treiber"}


def test_vorbereitetes_image_startet_nur_den_worker() -> None:
    document = build_cloud_config(bootstrap(prepared=True))
    assert SETUP_SCRIPT not in files_by_path(document)
    assert "power_state" not in document
    assert "packages" not in document
    assert ["systemctl", "enable", "--now", "mandari-worker.service"] in document["runcmd"]  # type: ignore[operator]


def test_worker_startet_nur_mit_gpu() -> None:
    unit = files_by_path(build_cloud_config(bootstrap()))[UNIT_PATH]["content"]
    assert "ExecStartPre=/usr/bin/nvidia-smi" in unit
    assert "--gpus all" in unit
