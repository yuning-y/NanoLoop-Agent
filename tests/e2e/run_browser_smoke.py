"""Standalone Playwright smoke for the NanoLoop frontend against the degraded stub.

This is NOT a pytest test (no `test_` prefix, so pytest won't collect it). Run it
directly with the Playwright venv python:

    python tests/e2e/run_browser_smoke.py

It:
  1. Starts scripts/degraded_stub_server.py on :8001 (real 503/429/401 envelopes).
  2. Starts the Streamlit frontend on :8501 pointed at the stub.
  3. Drives Chromium to the Connection page and captures screenshots of the
     always-rendered E-P1 error/degradation showcase panels and the post
     "检查连接" degraded health state.

No real model / RAG assets required — this exercises the honest degradation UI.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
SCREENSHOTS = REPO / "tests" / "e2e" / "screenshots"
STUB_PORT = 8001
APP_PORT = 8501
STUB_URL = f"http://127.0.0.1:{STUB_PORT}"
APP_URL = f"http://127.0.0.1:{APP_PORT}"

PY = sys.executable  # the venv python that runs this script

# E-P1 showcase tab labels (from frontend/app.py _render_ep1_showcase)
EP1_TABS = [
    "401 运维指引",
    "429 限流（读 vs 写）",
    "长任务部分失败",
    "键盘与屏幕阅读器",
]


def _wait_url(url: str, timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2)
            return True
        except Exception:
            time.sleep(0.5)
    return False


def _kill(proc: subprocess.Popen | None) -> None:
    if not proc or proc.poll() is not None:
        return
    pid = proc.pid
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass


def main() -> int:
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    stub = streamlit = None
    try:
        # 1) degraded stub
        print(f"[1/4] starting degraded stub on :{STUB_PORT} ...")
        stub_log = open(REPO / "tests" / "e2e" / "stub.log", "w")
        stub = subprocess.Popen(
            [PY, str(REPO / "scripts" / "degraded_stub_server.py"), "--port", str(STUB_PORT)],
            stdout=stub_log,
            stderr=subprocess.STDOUT,
        )
        if not _wait_url(f"{STUB_URL}/api/v1/health", timeout=30):
            print("  ! stub did not come up; continuing without it (frontend will show unreachable)")
            print("    (see tests/e2e/stub.log)")
        else:
            print("  stub OK")

        # 2) streamlit frontend
        print(f"[2/4] starting Streamlit frontend on :{APP_PORT} (-> stub) ...")
        env = dict(os.environ, NANOLOOP_API_BASE_URL=STUB_URL, NANOLOOP_SHOW_E_P1_SHOWCASE="1")
        streamlit_log = open(REPO / "tests" / "e2e" / "streamlit.log", "w")
        streamlit = subprocess.Popen(
            [
                PY, "-m", "streamlit", "run",
                str(REPO / "frontend" / "app.py"),
                "--server.port", str(APP_PORT),
                "--server.address", "127.0.0.1",
                "--server.headless", "true",
                "--browser.gatherUsageStats", "false",
            ],
            env=env,
            stdout=streamlit_log,
            stderr=subprocess.STDOUT,
        )
        if not _wait_url(APP_URL, timeout=90):
            print("  ! Streamlit did not come up within 90s; aborting")
            return 1
        print("  Streamlit OK")

        # 3) drive the browser
        from playwright.sync_api import sync_playwright

        # Prefer the full Chromium we already have on D: (PLAYWRIGHT_BROWSERS_PATH)
        # instead of the separate headless-shell download, so no extra fetch is needed.
        chrome_exe = None
        base = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
        if base:
            import glob

            for pat in ("chromium-*", "chromium_headless_shell-*"):
                hits = glob.glob(os.path.join(base, pat, "chrome-win64", "chrome.exe"))
                hits += glob.glob(os.path.join(base, pat, "chrome-win", "chrome.exe"))
                if hits:
                    chrome_exe = hits[0]
                    break

        print("[3/4] launching Chromium and capturing screenshots ...")
        with sync_playwright() as p:
            if chrome_exe:
                print(f"  using full chromium at: {chrome_exe}")
                browser = p.chromium.launch(
                    executable_path=chrome_exe, args=["--no-sandbox"]
                )
            else:
                print("  (PLAYWRIGHT_BROWSERS_PATH not set — using default launcher)")
                browser = p.chromium.launch(args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1280, "height": 1600})
            page.goto(APP_URL, wait_until="load", timeout=30000)
            # give Streamlit time to hydrate the app
            page.wait_for_timeout(6000)
            page.screenshot(path=str(SCREENSHOTS / "01_landing_connection.png"), full_page=True)
            print("  saved 01_landing_connection.png")

            # E-P1 showcase tabs
            for i, label in enumerate(EP1_TABS, start=2):
                try:
                    page.get_by_role("tab", name=label).click(timeout=8000)
                    page.wait_for_timeout(1500)
                    page.screenshot(
                        path=str(SCREENSHOTS / f"0{i}_ep1_{label.split()[0]}.png"),
                        full_page=True,
                    )
                    print(f"  saved 0{i}_ep1_{label.split()[0]}.png")
                except Exception as e:
                    print(f"  ! could not capture tab '{label}': {e}")

            # trigger health refresh -> degraded/error panels from the stub
            try:
                page.click("button:has-text('检查连接')", timeout=8000)
                page.wait_for_timeout(3000)
                page.screenshot(path=str(SCREENSHOTS / "06_after_refresh_degraded.png"), full_page=True)
                print("  saved 06_after_refresh_degraded.png")
            except Exception as e:
                print(f"  ! '检查连接' click failed: {e}")

            browser.close()

        print("[4/4] done. Screenshots in tests/e2e/screenshots/")
        return 0
    finally:
        print("cleaning up subprocesses ...")
        _kill(streamlit)
        _kill(stub)


if __name__ == "__main__":
    raise SystemExit(main())
