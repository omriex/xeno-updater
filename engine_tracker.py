#!/usr/bin/env python3
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

TARGET_URL = "https://mope.io/"
STORAGE_DIR = Path(__file__).parent / "mope_scripts"
CHECK_INTERVAL_SECONDS = 600
REQUEST_TIMEOUT = 30
WEBCRACK_TIMEOUT = 1800  
NODE_MEMORY_MB = 8192    

AUTO_PORT_SCRIPT = Path(__file__).parent / "engine_port.py"
FULL_JS = Path(__file__).parent / "mope_full.js"

AUTO_PORT_TIMEOUT = 900
AUTO_PORT_STATUS_SECONDS = 15

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "max-age=0",
}


def ensure_storage() -> None:
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def script_out_dir(filename: str) -> Path:
    return STORAGE_DIR / Path(filename).stem


def is_already_processed(filename: str) -> bool:
    out_dir = script_out_dir(filename)
    meta_file = out_dir / "_meta.json"
    
    if out_dir.exists() and not meta_file.exists():
        print(f"    [!] Detected corrupted or incomplete directory for {filename}. Cleaning up for retry...")
        shutil.rmtree(out_dir, ignore_errors=True)
        return False
        
    return meta_file.exists()


def fetch_page(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.text


def extract_main_js_url(html: str, base_url: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    candidates = []
    for tag in soup.find_all("script", src=True):
        if tag.get("type") == "module":
            full = urljoin(base_url, tag["src"])
            if full.endswith(".js"):
                candidates.append(full)
                
    if not candidates:
        return None
        
    best_url = None
    max_size = -1
    
    for url in candidates:
        try:
            resp = requests.head(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            size = int(resp.headers.get("Content-Length", 0))
            if size > max_size:
                max_size = size
                best_url = url
        except Exception:
            continue
            
    return best_url or candidates[0]


def webcrack_command() -> list[str]:
    exe = shutil.which("webcrack")
    if exe:
        return [exe]
    npx = shutil.which("npx") or shutil.which("npx.cmd")
    if not npx:
        raise RuntimeError("Node.js not found. Please install Node LTS to use webcrack.")
    return [npx, "-y", "webcrack@latest"]


def run_webcrack(src_file: Path, out_dir: Path) -> None:
    env = os.environ.copy()
    env["NODE_OPTIONS"] = f"{env.get('NODE_OPTIONS', '')} --max-old-space-size={NODE_MEMORY_MB}".strip()
    cmd = webcrack_command() + [str(src_file), "-o", str(out_dir), "-f"]
    print(f"    webcrack: {' '.join(cmd)}")
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=WEBCRACK_TIMEOUT, env=env)
    if proc.returncode != 0:
        raise RuntimeError(
            f"webcrack failed with code {proc.returncode}: "
            f"{(proc.stderr or proc.stdout or '').strip()[:500]}"
        )


def pick_client_file(out_dir: Path) -> Path | None:
    cands = [p for p in out_dir.rglob("*.js") if not p.name.endswith("_with_mod.js")]
    if not cands:
        return None
        
    best_cand = None
    signatures = ["network.readyState", "s3PublicUrl", "import{"]
    
    for cand in cands:
        try:
            content = cand.read_text(encoding="utf-8", errors="ignore")
            if any(sig in content for sig in signatures):
                if not best_cand or cand.stat().st_size > best_cand.stat().st_size:
                    best_cand = cand
        except Exception:
            continue
            
    return best_cand or max(cands, key=lambda p: p.stat().st_size)


def run_auto_port(out_dir: Path) -> Path | None:
    if not AUTO_PORT_SCRIPT.exists():
        print(f"    [!] engine_port.py missing ({AUTO_PORT_SCRIPT}) - skipping auto patcher")
        return None

    client = pick_client_file(out_dir)
    if client is None:
        print("    [!] Could not locate client .js to inject")
        return None

    tmp_out = out_dir / "_autoport_tmp.js"
    cmd = [sys.executable, str(AUTO_PORT_SCRIPT), str(client), "-o", str(tmp_out)]
    print(f"    Auto-patching mope.io: {client.name}")

    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    report_path = out_dir / "_autoport_report.txt"
    started = time.monotonic()
    last_status = started
    timed_out = False

    with report_path.open("w+", encoding="utf-8", errors="replace") as report_file:
        proc = subprocess.Popen(cmd, stdout=report_file, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", cwd=str(out_dir), env=env)
        read_pos = 0
        while proc.poll() is None:
            time.sleep(0.25)
            report_file.flush()
            report_file.seek(read_pos)
            fresh = report_file.read()
            read_pos = report_file.tell()
            for line in fresh.splitlines():
                print(f"      {line}", flush=True)
            now = time.monotonic()
            if now - last_status >= AUTO_PORT_STATUS_SECONDS:
                print(f"      [auto-port still running: {now - started:.0f}s]", flush=True)
                last_status = now
            if now - started >= AUTO_PORT_TIMEOUT:
                proc.kill()
                proc.wait()
                timed_out = True
                break
        
        report_file.flush()
        report_file.seek(read_pos)
        for line in report_file.read().splitlines():
            print(f"      {line}", flush=True)

    if timed_out:
        print(f"    [!] engine_port.py: Timeout after {AUTO_PORT_TIMEOUT}s")
        tmp_out.unlink(missing_ok=True)
        return None

    if proc.returncode != 0:
        print(f"    [!] engine_port.py failed with exit code {proc.returncode}")
        tmp_out.unlink(missing_ok=True)
        return None
        
    if not tmp_out.exists() or tmp_out.stat().st_size == 0:
        print("    [!] engine_port.py did not output patched file")
        tmp_out.unlink(missing_ok=True)
        return None

    shutil.move(str(tmp_out), str(client))
    publish_full(client)
    print(f"    Ready (webcrack + xeno mod): {client} ({client.stat().st_size:,} bytes)")
    return client


def publish_full(client: Path) -> None:
    tmp = FULL_JS.with_suffix(".js.tmp")
    shutil.copyfile(client, tmp)
    os.replace(tmp, FULL_JS) 
    print(f"    mope_full.js updated: {FULL_JS} ({FULL_JS.stat().st_size:,} bytes)")


def process_script(url: str) -> str | None:
    filename = Path(urlparse(url).path).name
    if not filename.endswith(".js"):
        return None

    out_dir = script_out_dir(filename)

    print(f"[+] Found updated script: {filename}")
    print(f"    Downloading {url} ...")
    resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.content
    digest = sha256_bytes(data)
    print(f"    Received {len(data):,} bytes, sha256 {digest[:16]}...")

    tmp_dir = Path(tempfile.mkdtemp(prefix="client_"))
    tmp_file = tmp_dir / filename
    tmp_out = tmp_dir / "out"
    try:
        tmp_file.write_bytes(data)
        print("    Running Webcrack (can take a minute or two)...")
        started = time.time()
        run_webcrack(tmp_file, tmp_out)
        elapsed = time.time() - started

        if not tmp_out.exists() or not any(tmp_out.rglob("*.js")):
            raise RuntimeError("Webcrack output was empty.")

        out_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp_out), str(out_dir))
        
        modded = run_auto_port(out_dir)

        files = sorted(p.relative_to(out_dir).as_posix() for p in out_dir.rglob("*.js"))
        (out_dir / "_meta.json").write_text(
            json.dumps({
                "filename": filename,
                "url": url,
                "sha256_original": digest,
                "size_original": len(data),
                "downloaded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "webcrack_seconds": round(elapsed, 1),
                "files": files,
                "modded_file": modded.name if modded else None,
            }, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        
        print(f"    Done in {elapsed:.1f}s — {len(files)} file(s) in {out_dir}")
        return filename
    except Exception as e:
        print(f"[!] Processing failed for {filename}: {e}")
        if out_dir.exists():
            shutil.rmtree(out_dir, ignore_errors=True)
        return None
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def check_once() -> dict:
    html = fetch_page(TARGET_URL)
    
    main_url = extract_main_js_url(html, TARGET_URL)
    
    processed = []
    skipped = []

    if main_url:
        filename = Path(urlparse(main_url).path).name
        if is_already_processed(filename):
            skipped.append(filename)
        else:
            name = process_script(main_url)
            if name:
                processed.append(name)

    return {
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "found_urls": [main_url] if main_url else [],
        "processed": processed,
        "skipped": skipped,
    }


def main() -> None:
    ensure_storage()
    print(f"Mope.io Engine Tracker started. Data path: {STORAGE_DIR.resolve()}")
    print("Saves webcrack-deobfuscated files appended with Xeno Mod Client.")
    print(f"Polling interval: {CHECK_INTERVAL_SECONDS // 60} min. Press Ctrl+C to exit.\n")

    while True:
        try:
            result = check_once()
            print(f"[{result['checked_at']}] found {len(result['found_urls'])} assets, "
                  f"patched {len(result['processed'])}, ignored/exists {len(result['skipped'])}")
            for name in result["processed"]:
                print(f"    -> {name}")
        except requests.exceptions.RequestException as e:
            print(f"[!] Network error: {e}")
        except Exception as e:
            print(f"[!] Processing error: {e}")

        print(f"--- Sleeping for {CHECK_INTERVAL_SECONDS // 60} mins ---\n")
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
