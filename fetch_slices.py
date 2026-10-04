#!/usr/bin/env python3
"""
DTCC CFTC Rates Slice Downloader
- Runs every 5 minutes (via cron)
- Fetches the slice listing via headless Chromium (Akamai bypass)
- Downloads all slice ZIPs with dissemDTM in the last WINDOW_MINUTES
- Extracts CSVs into OUTPUT_DIR, replacing previous run's files

CONFIG: Edit the constants below to change behaviour.
"""

import asyncio
import csv
import io
import json
import os
import shutil
import sys
import urllib.request
import zipfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────────────
WINDOW_MINUTES = 5          # Look back this many minutes for new slices
REGULATOR      = "CFTC"     # Options: CFTC, CA, SEC
ASSET_CLASS    = "IR"       # IR=Rates, FX, CO=Commodities, CR=Credits, EQ=Equities
OUTPUT_DIR     = Path(__file__).parent / "slices"
LOG_FILE       = Path(__file__).parent / "fetch.log"
CHROMIUM_PATH  = "/usr/bin/chromium"
BASE_URL       = "https://pddata.dtcc.com/ppd/cftcdashboard"
# ────────────────────────────────────────────────────────────────────────────


def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


async def fetch_slice_list(regulator: str, asset: str) -> list[dict]:
    """Use headless Chromium to load the dashboard and intercept the slice API response."""
    from playwright.async_api import async_playwright

    slice_data = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path=CHROMIUM_PATH,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--headless=new"],
        )
        ctx = await browser.new_context()
        page = await ctx.new_page()

        target_path = f"/api/slice/{regulator}/{asset}"

        async def on_response(response):
            if target_path in response.url:
                try:
                    data = await response.json()
                    if isinstance(data, list):
                        slice_data.extend(data)
                except Exception:
                    pass

        page.on("response", on_response)
        await page.goto(BASE_URL, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(2000)
        await browser.close()

    return slice_data


def filter_by_window(slices: list[dict], window_minutes: int) -> list[dict]:
    """Return slices whose dissemDTM falls within the last window_minutes."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=window_minutes)
    result = []
    for s in slices:
        raw = s.get("dissemDTM", "")
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if cutoff <= dt <= now:
                result.append(s)
        except ValueError:
            pass
    return result


def download_and_extract(slice_entry: dict, output_dir: Path) -> list[Path]:
    """Download a slice ZIP from S3 and extract the CSV(s) into output_dir."""
    url = slice_entry["fullFilePath"]
    filename = slice_entry["fileName"]
    extracted = []

    log(f"  Downloading {filename} ({slice_entry['rowCount']} rows) ...")
    with urllib.request.urlopen(url, timeout=30) as resp:
        raw = resp.read()

    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        for name in zf.namelist():
            if name.lower().endswith(".csv"):
                dest = output_dir / name
                data = zf.read(name)
                dest.write_bytes(data)
                extracted.append(dest)
                log(f"    Extracted {name}")

    return extracted


def clear_output_dir(output_dir: Path):
    """Remove all CSV files from the previous run."""
    if output_dir.exists():
        for f in output_dir.glob("*.csv"):
            f.unlink()
    else:
        output_dir.mkdir(parents=True)


def main():
    log(f"=== Slice fetch started (window={WINDOW_MINUTES}m, {REGULATOR}/{ASSET_CLASS}) ===")

    # 1. Get slice listing via browser
    try:
        all_slices = asyncio.run(fetch_slice_list(REGULATOR, ASSET_CLASS))
    except Exception as e:
        log(f"ERROR fetching slice list: {e}")
        sys.exit(1)

    log(f"Total slices in listing: {len(all_slices)}")

    # 2. Filter to last WINDOW_MINUTES
    new_slices = filter_by_window(all_slices, WINDOW_MINUTES)
    log(f"Slices in last {WINDOW_MINUTES} min: {len(new_slices)}")

    if not new_slices:
        log("Nothing new. Exiting.")
        # Still clear old files so the folder reflects the current window
        clear_output_dir(OUTPUT_DIR)
        log("=== Done ===")
        return

    # 3. Clear previous run
    clear_output_dir(OUTPUT_DIR)

    # 4. Download & extract each slice
    total_files = []
    for s in new_slices:
        try:
            files = download_and_extract(s, OUTPUT_DIR)
            total_files.extend(files)
        except Exception as e:
            log(f"ERROR on {s.get('fileName')}: {e}")

    log(f"Downloaded {len(new_slices)} slices → {len(total_files)} CSV file(s) in {OUTPUT_DIR}")
    log("=== Done ===")


if __name__ == "__main__":
    main()
