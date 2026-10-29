#!/usr/bin/env python3
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from urllib.parse import urljoin, urlparse



import requests
from bs4 import BeautifulSoup

TARGET_URL = "https://mope.io/"
STORAGE_DIR = Path(__file__).parent / "mope_scripts"
CHECK_INTERVAL_SECONDS = 600
REQUEST_TIMEOUT = 30

AUTO_PORT_SCRIPT = Path(__file__).parent / "engine_port.py"
FULL_JS = Path(__file__).parent / "mope_full.js"

# Extension folder — all static files (icon.png, inject.js, manifest.json, rules.json, _metadata/)
# must live here. index.js and rules.json get written/updated automatically.
EXTENSION_DIR = Path(__file__).parent / "extension"

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

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
    # js_filename is the original mope.io chunk name e.g. "CkocsJ3X.js"
    js_filename = Path(client.parent.name + ".js")  # out_dir name is the stem of the original file
    build_and_post_extension(client, str(js_filename))
    print(f"    Ready (xeno mod): {client} ({client.stat().st_size:,} bytes)")
    return client


def update_rules_json(js_filename: str) -> None:
    """Rewrites rules.json urlFilter to point at the current mope.io JS filename."""
    rules_path = EXTENSION_DIR / "rules.json"
    if not rules_path.exists():
        print("    [!] rules.json not found in extension dir, skipping rules update")
        return

    rules = json.loads(rules_path.read_text(encoding="utf-8"))
    updated = False
    for rule in rules:
        if (rule.get("action", {}).get("type") == "redirect" and
                rule.get("action", {}).get("redirect", {}).get("extensionPath") == "/index.js"):
            current_filter = rule.get("condition", {}).get("urlFilter", "")
            new_filter = f"||mope.io/{js_filename}"
            if current_filter != new_filter:
                rule["condition"]["urlFilter"] = new_filter
                updated = True
                print(f"    rules.json: updated urlFilter {current_filter!r} → {new_filter!r}")

    if updated:
        rules_path.write_text(json.dumps(rules, indent=2, ensure_ascii=False), encoding="utf-8")
    else:
        print("    rules.json: urlFilter already up to date")


def build_and_post_extension(client: Path, js_filename: str) -> None:
    """
    1. Copies mope_full.js → extension/index.js
    2. Updates rules.json with the new JS filename
    3. Zips the entire extension folder
    4. Posts the zip to Discord (file only, no message text)
    """
    if not EXTENSION_DIR.exists():
        print(f"    [!] Extension dir not found at {EXTENSION_DIR} — skipping Discord post")
        return

    # 1. Atomic copy of patched JS into extension as index.js
    index_js = EXTENSION_DIR / "index.js"
    tmp_index = EXTENSION_DIR / "_index.js.tmp"
    shutil.copyfile(client, tmp_index)
    os.replace(tmp_index, index_js)
    print(f"    extension/index.js updated ({index_js.stat().st_size:,} bytes)")

    # 2. Update rules.json to intercept the new filename
    update_rules_json(js_filename)

    # 3. Zip the extension in memory
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(EXTENSION_DIR.rglob("*")):
            if f.is_file() and not f.name.endswith(".tmp"):
                zf.write(f, f.relative_to(EXTENSION_DIR))
    zip_bytes = zip_buf.getvalue()
    print(f"    Extension zip built: {len(zip_bytes):,} bytes")

    # 4. Also keep mope_full.js up to date as before
    tmp = FULL_JS.with_suffix(".js.tmp")
    shutil.copyfile(client, tmp)
    os.replace(tmp, FULL_JS)
    print(f"    mope_full.js updated: {FULL_JS} ({FULL_JS.stat().st_size:,} bytes)")

    # 5. Post to Discord — zip file only, zero message text
    if not DISCORD_WEBHOOK_URL:
        print("    [!] DISCORD_WEBHOOK_URL not set, skipping notification")
        return

    try:
        resp = requests.post(
            DISCORD_WEBHOOK_URL,
            files={"file": ("Xeno_Modpack.zip", zip_bytes, "application/zip")},
            timeout=120,
        )
        if resp.status_code in (200, 204):
            print("    Discord: extension zip posted to ⚡versions")
        else:
            print(f"    [!] Discord post failed: HTTP {resp.status_code} — {resp.text[:200]}")
    except Exception as e:
        print(f"    [!] Discord webhook error: {e}")


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

    # mope.io is Vite-minified, not obfuscated — inject directly into raw JS, no webcrack needed
    tmp_dir = Path(tempfile.mkdtemp(prefix="client_"))
    tmp_file = tmp_dir / filename
    try:
        tmp_file.write_bytes(data)
        started = time.time()

        out_dir.mkdir(parents=True, exist_ok=True)
        raw_js = out_dir / "deobfuscated.js"
        shutil.copyfile(tmp_file, raw_js)

        modded = run_auto_port(out_dir)
        elapsed = time.time() - started

        files = sorted(p.relative_to(out_dir).as_posix() for p in out_dir.rglob("*.js"))
        (out_dir / "_meta.json").write_text(
            json.dumps({
                "filename": filename,
                "url": url,
                "sha256_original": digest,
                "size_original": len(data),
                "downloaded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "elapsed_seconds": round(elapsed, 1),
                "files": files,
                "modded_file": modded.name if modded else None,
            }, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        print(f"    Done in {elapsed:.1f}s — {out_dir}")
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
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="run once and exit (used by GitHub Actions)")
    args = ap.parse_args()

    ensure_storage()

    if args.once:
        try:
            result = check_once()
            print(f"[{result['checked_at']}] patched {len(result['processed'])}, skipped {len(result['skipped'])}")
        except Exception as e:
            print(f"[!] Error: {e}")
        return

    print(f"Mope.io Engine Tracker started. Data path: {STORAGE_DIR.resolve()}")
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
