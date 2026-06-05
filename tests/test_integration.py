# tests/test_integration.py
import json, os, subprocess, sys, tempfile, time, pathlib

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
    plan = {
        "version": 1, "sim_start": sim_start,
        "contacts": [{
            "id": "rover_a:1", "created_by": "rover_a",
            "from": "rover_a", "to": "base",
            "phase": 2, "period": 3600, "duration": 30,
            "rate_bps": 9600, "status": "active"
        }]
    }
    (tmp / "plan.json").write_text(json.dumps(plan))


def write_peers(tmp, base_port, rover_port):
    peers = {"base": f"127.0.0.1:{base_port}",
             "rover_a": f"127.0.0.1:{rover_port}"}
    (tmp / "peers.json").write_text(json.dumps(peers))
    return str(tmp / "peers.json")


def make_test_image(tmp):
    img = tmp / "test.jpg"
    # 1024 bytes of test data
    img.write_bytes(bytes(range(256)) * 4)
    return str(img)


def test_image_delivered_end_to_end():
    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)
        (tmp / "received").mkdir()

        sim_start = time.time()
        write_plan(tmp, sim_start)

        base_port  = 17001
        rover_port = 17002
        base_cfg   = write_config(tmp, "base",    base_port)
        rover_cfg  = write_config(tmp, "rover_a", rover_port)
        peers_file = write_peers(tmp, base_port, rover_port)
        img_path   = make_test_image(tmp)

        # Generate key
        from marsnet.node.crypto import CryptoManager
        CryptoManager.generate().save(str(tmp / "shared.key"))

        base_proc = subprocess.Popen([
            PYTHON, "-m", "marsnet.node.main",
            "--config", base_cfg, "--peers", peers_file,
        ])
        time.sleep(0.5)
        rover_proc = subprocess.Popen([
            PYTHON, "-m", "marsnet.node.main",
            "--config", rover_cfg, "--peers", peers_file,
            "--send-image", img_path, "--destination", "base",
            "--ttl", "60", "--image-id", "testimg",
        ])

        # Wait up to 10s for image to arrive (contact window opens at phase=2)
        deadline = time.time() + 10
        received = tmp / "received" / "testimg.jpg"
        while time.time() < deadline:
            if received.exists():
                break
            time.sleep(0.5)

        base_proc.terminate()
        rover_proc.terminate()
        base_proc.wait()
        rover_proc.wait()

        assert received.exists(), "Image not received at base within 10s"
        assert received.stat().st_size > 0
