# tests/test_integration.py
import json, os, subprocess, sys, tempfile, time, pathlib
from PIL import Image

PYTHON = sys.executable
ROOT   = pathlib.Path(__file__).parent.parent


def write_config(tmp, name, port):
    cfg = {
        "name": name, "host": "127.0.0.1", "port": port, "udp_port": 17000,
        "plan_path": str(tmp / "plan.json"),
        "key_path":  str(tmp / "shared.key"),
        "dashboard_url": "http://127.0.0.1:19999",  # non-existent; errors dropped
        "image_dir": str(tmp / "received"),
    }
    p = tmp / f"{name}.json"
    p.write_text(json.dumps(cfg))
    return str(p)


def write_plan(tmp, sim_start):
    # NodeCLI hardcodes dst="relay", so the receiving node must be named "relay".
    # phase=4 gives enough headroom for both processes to start and the CLI
    # to inject bundles before the first contact window opens.
    plan = {
        "version": 1, "sim_start": sim_start,
        "contacts": [{
            "id": "rover_a:1", "created_by": "rover_a",
            "from": "rover_a", "to": "relay",
            "phase": 4, "period": 3600, "duration": 30,
            "rate_bps": 9600, "status": "active"
        }]
    }
    (tmp / "plan.json").write_text(json.dumps(plan))


def write_peers(tmp, relay_port, rover_port):
    peers = {"relay": f"127.0.0.1:{relay_port}",
             "rover_a": f"127.0.0.1:{rover_port}"}
    (tmp / "peers.json").write_text(json.dumps(peers))
    return str(tmp / "peers.json")


def make_test_image(tmp):
    img_path = tmp / "test.jpg"
    pic = Image.new("RGB", (120, 90))
    px = pic.load()
    for y in range(90):
        for x in range(120):
            px[x, y] = (x % 256, y % 256, (x + y) % 256)
    pic.save(str(img_path), format="JPEG", quality=90)
    return str(img_path)


def test_image_delivered_end_to_end():
    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)
        (tmp / "received").mkdir()

        sim_start = time.time()
        write_plan(tmp, sim_start)

        relay_port = 17001
        rover_port = 17002
        relay_cfg  = write_config(tmp, "relay",   relay_port)
        rover_cfg  = write_config(tmp, "rover_a", rover_port)
        peers_file = write_peers(tmp, relay_port, rover_port)
        img_path   = make_test_image(tmp)

        # Generate key
        from marsnet.node.crypto import CryptoManager
        CryptoManager.generate().save(str(tmp / "shared.key"))

        relay_proc = subprocess.Popen(
            [
                PYTHON, "-m", "marsnet.node.main",
                "--config", relay_cfg, "--peers", peers_file,
            ],
            stdin=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        time.sleep(0.5)
        rover_proc = subprocess.Popen(
            [
                PYTHON, "-m", "marsnet.node.main",
                "--config", rover_cfg, "--peers", peers_file,
                "--ttl", "60",
            ],
            stdin=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        # Feed the image path via the interactive CLI; do NOT send "q" so the
        # rover stays alive long enough for the contact window (phase=2s) to open
        # and the bundles to be transmitted.
        rover_proc.stdin.write(f"{img_path}\n".encode())
        rover_proc.stdin.flush()

        # Wait up to 15s for image to arrive (contact window opens at phase=4)
        deadline = time.time() + 15
        received = tmp / "received" / "rover_a-test.jpg"
        while time.time() < deadline:
            if received.exists():
                break
            time.sleep(0.5)

        relay_proc.terminate()
        rover_proc.terminate()
        relay_stderr = relay_proc.communicate()[1].decode(errors="replace")
        rover_stderr = rover_proc.communicate()[1].decode(errors="replace")

        assert received.exists(), (
            f"Image not received at relay within 15s\n"
            f"relay stderr: {relay_stderr[-2000:]}\n"
            f"rover stderr: {rover_stderr[-2000:]}"
        )
        with Image.open(received) as got:
            assert got.size == (120, 90)
