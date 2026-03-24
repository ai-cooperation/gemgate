#!/usr/bin/env python3
"""Podcast 完成追蹤器 — Phase 2: Check + Download + Deploy (Firefox version)

v3: Migrated from Chrome CDP to Firefox + Cookie injection.
- Uses FirefoxManager for browser lifecycle (replaces connect_over_cdp)
- Downloads via Playwright native expect_download (no Chrome dir workaround)
- Handles three-column layout (Studio panel always visible)

Runs every 15 minutes via systemd timer.
Scans state.json for podcast_status=="generating", checks NotebookLM,
downloads audio when ready, pushes to GitHub Pages.
"""
import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import time as _time
from pathlib import Path

sys.path.insert(0, "/opt/gemgate")
sys.path.insert(0, "/opt/gemgate/automations")
from common import tg_send
from core.firefox_manager import FirefoxManager

log = logging.getLogger("podcast-tracker")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

_fh = logging.FileHandler("/opt/gemgate/state/logs/podcast-tracker.log")
_fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"))
log.addHandler(_fh)

CONTENT_DIR = Path("/opt/gemgate/content/sustainability100")
TOPICS_FILE = CONTENT_DIR / "topics.json"
WEBSITE_REPO = Path.home() / "sustainability-100"
STUCK_TIMEOUT_HOURS = 12

# Shared FirefoxManager instance (created once per run)
_firefox_mgr = None


async def _get_firefox_mgr() -> FirefoxManager:
    global _firefox_mgr
    if _firefox_mgr is None:
        _firefox_mgr = FirefoxManager(idle_timeout=1800)
    return _firefox_mgr


async def _dismiss_overlays(page):
    """Dismiss NotebookLM overlay dialogs.
    NEVER click generic Close/OK — they are legitimate UI elements."""
    for selector in [
        'button:has-text("Got it")',
        'button:has-text("Dismiss")',
        'button:has-text("Skip")',
        'button:has-text("知道了")',
    ]:
        try:
            btn = await page.query_selector(selector)
            if btn and await btn.is_visible():
                await btn.click(force=True)
                await asyncio.sleep(1)
                break
        except Exception:
            continue


async def _ensure_studio_visible(page) -> bool:
    """Ensure Studio panel is visible. Handles both tab and three-column layouts."""
    # Check if Studio content (Audio Overview) is already visible
    for sel in [
        '[aria-label="Audio Overview"]',
        '[aria-label="語音摘要"]',
        'button:has-text("audio_magic_eraser")',
    ]:
        el = await page.query_selector(sel)
        if el:
            try:
                if await el.is_visible():
                    return True
            except Exception:
                pass

    # Try clicking Studio tab (tab layout)
    for _sel in [
        '[role="tab"]:has-text("Studio")',
        '[role="tab"]:has-text("工作室")',
    ]:
        studio = await page.query_selector(_sel)
        if studio:
            await studio.click(force=True)
            await asyncio.sleep(2)
            return True

    # Check for Studio header text
    for _sel in ['text=工作室', 'text=Studio']:
        el = await page.query_selector(_sel)
        if el:
            return True

    return False


async def _is_audio_generating(page) -> bool:
    for _sel in [
        'text=正在生成語音摘要',
        'text=Generating Audio Overview',
        'button:has-text("Generating")',
        'button:has-text("正在生成")',
    ]:
        el = await page.query_selector(_sel)
        if el:
            return True
    return False


async def _is_video_generating(page) -> bool:
    for _sel in [
        'text=正在生成影片摘要',
        'text=Generating Video Overview',
    ]:
        el = await page.query_selector(_sel)
        if el:
            return True
    return False


async def _click_download_in_menu(page) -> bool:
    """Click Download in an open menu."""
    handle = await page.evaluate_handle("""() => {
        const items = document.querySelectorAll('[role="menuitem"]');
        for (const item of items) {
            const icons = item.querySelectorAll('mat-icon, [class*="icon"]');
            for (const ic of icons) {
                const t = ic.textContent.trim();
                if ((t === 'download' || t === 'save_alt') && item.offsetParent !== null)
                    return item;
            }
        }
        for (const item of items) {
            const t = item.innerText.trim();
            if ((t.includes('download') || t.includes('save_alt')) && item.offsetParent !== null)
                return item;
        }
        return null;
    }""")
    if handle:
        try:
            el = handle.as_element()
            if el:
                await el.click()
                return True
        except Exception:
            pass
    for _dsel in ['[role="menuitem"]:has-text("Download")', '[role="menuitem"]:has-text("下載")']:
        dl_btn = await page.query_selector(_dsel)
        if dl_btn:
            await dl_btn.click()
            return True
    return False


async def check_and_download(save_path: str, notebook_url: str = None) -> tuple:
    """Check if audio is ready, download via Playwright native download.

    Returns: (is_ready, audio_path_or_none, error_or_none)
    """
    fm = await _get_firefox_mgr()
    page = None

    try:
        page = await fm.get_page("firefox-notebooklm", notebook_url or "https://notebooklm.google.com/")
        await asyncio.sleep(3)

        if "accounts.google.com" in page.url:
            return (False, None, "Firefox cookies expired")

        await _dismiss_overlays(page)
        log.info(f"Checking notebook: {page.url[:80]}")

        if not await _ensure_studio_visible(page):
            log.warning("Studio panel not visible")

        # Check if still generating
        if await _is_audio_generating(page):
            log.info("Audio still generating")
            return (False, None, None)

        # Check if audio item exists (audio_magic_eraser icon)
        has_audio_item = await page.evaluate("""() => {
            const icons = document.querySelectorAll('mat-icon');
            for (const icon of icons) {
                if (icon.textContent.trim() === 'audio_magic_eraser' && icon.offsetParent !== null)
                    return true;
            }
            return false;
        }""")

        if not has_audio_item:
            log.info("No audio_magic_eraser icon found")
            return (False, None, "no_audio_section")

        # Find more_vert button near audio_magic_eraser in Studio panel
        more_btn_handle = await page.evaluate_handle("""() => {
            const icons = document.querySelectorAll('mat-icon');
            for (const icon of icons) {
                if (icon.textContent.trim() === 'audio_magic_eraser' && icon.offsetParent !== null) {
                    let container = icon.parentElement;
                    for (let i = 0; i < 8; i++) {
                        if (!container) break;
                        const btns = container.querySelectorAll('button');
                        for (const btn of btns) {
                            const ic = btn.querySelector('mat-icon');
                            if (ic && ic.textContent.trim() === 'more_vert' && btn.offsetParent !== null)
                                return btn;
                        }
                        container = container.parentElement;
                    }
                }
            }
            return null;
        }""")

        more_btn = None
        if more_btn_handle:
            try:
                el = more_btn_handle.as_element()
                if el and await el.is_visible():
                    more_btn = el
            except Exception:
                pass

        if not more_btn:
            log.warning("Audio more_vert button not found near audio_magic_eraser")
            return (True, None, "Audio more_vert not found")

        log.info("Audio ready, downloading via Studio panel menu...")

        # Click More to open menu
        await more_btn.click(force=True)
        await asyncio.sleep(1)

        save_file = Path(save_path)
        save_file.parent.mkdir(parents=True, exist_ok=True)

        try:
            async with page.expect_download(timeout=120000) as dl_info:
                if not await _click_download_in_menu(page):
                    return (True, None, "Download button not found in menu")
            download = await dl_info.value
            await download.save_as(str(save_file))

            size = save_file.stat().st_size
            if size < 1000:
                return (True, None, f"Downloaded file too small ({size} bytes)")

            log.info(f"Audio downloaded: {save_file} ({size / (1024*1024):.1f}MB)")

            # Re-mux to fix DASH format
            try:
                import subprocess
                fixed = save_file.with_suffix('.fixed' + save_file.suffix)
                subprocess.run(
                    ['ffmpeg', '-i', str(save_file), '-c', 'copy',
                     '-movflags', '+faststart', str(fixed), '-y'],
                    capture_output=True, timeout=60,
                )
                if fixed.exists() and fixed.stat().st_size > 1000:
                    fixed.replace(save_file)
                    log.info(f"Re-muxed with faststart: {save_file}")
            except Exception as e:
                log.warning(f"Re-mux failed (original kept): {e}")

            return (True, str(save_file), None)
        except Exception as e:
            log.error(f"Download failed: {type(e).__name__}: {e}")
            return (True, None, f"Download error: {e}")

    except Exception as e:
        log.error(f"check_and_download error: {type(e).__name__}: {e}")
        return (False, None, str(e))
    finally:
        if page:
            try:
                await page.close()
            except Exception:
                pass


async def check_and_download_video(save_path: str, notebook_url: str = None) -> tuple:
    """Check if Video Overview is ready and download it.

    Returns: (is_ready, video_path_or_none, error_or_none)
    """
    fm = await _get_firefox_mgr()
    page = None

    try:
        page = await fm.get_page("firefox-notebooklm", notebook_url or "https://notebooklm.google.com/")
        await asyncio.sleep(3)

        if "accounts.google.com" in page.url:
            return (False, None, "Firefox cookies expired")

        await _dismiss_overlays(page)
        if not await _ensure_studio_visible(page):
            log.warning("Studio panel not visible for video check")

        if await _is_video_generating(page):
            log.info("Video Overview still generating")
            return (False, None, None)

        # Check for generated video items
        body_text = await page.inner_text("body")
        has_video_item = any(kw in body_text for kw in [
            "Explainer", "Brief", "影片摘要", "Video Overview"
        ])

        if not has_video_item:
            for _sel in [
                '[aria-label="Video Overview"]',
                '[aria-label="影片摘要"]',
                'text=Video Overview',
                'text=影片摘要',
            ]:
                vo_card = await page.query_selector(_sel)
                if vo_card:
                    break
            else:
                vo_card = None
            if not vo_card:
                log.info("No Video Overview section — marking as skipped")
                return (False, None, "no_video_section")
            log.info("Video Overview card found but no output yet")
            return (False, None, None)

        # Find the More button for the video item (subscriptions icon)
        video_more_handle = await page.evaluate_handle("""() => {
            const icons = document.querySelectorAll('mat-icon');
            for (const icon of icons) {
                if (icon.textContent.trim() === 'subscriptions' && icon.offsetParent !== null) {
                    let container = icon.parentElement;
                    for (let i = 0; i < 5; i++) {
                        if (!container) break;
                        const btns = container.querySelectorAll('button');
                        for (const btn of btns) {
                            const ic = btn.querySelector('mat-icon');
                            if (ic && ic.textContent.trim() === 'more_vert' && btn.offsetParent !== null)
                                return btn;
                        }
                        container = container.parentElement;
                    }
                }
            }
            return null;
        }""")
        video_more = None
        if video_more_handle:
            try:
                el = video_more_handle.as_element()
                if el and await el.is_visible():
                    video_more = el
            except Exception:
                pass

        if not video_more:
            log.info("Video item found but menu button not found")
            return (False, None, None)

        log.info("Video ready, downloading...")

        await video_more.click(force=True)
        await asyncio.sleep(1)

        save_file = Path(save_path)
        save_file.parent.mkdir(parents=True, exist_ok=True)

        try:
            async with page.expect_download(timeout=180000) as dl_info:
                if not await _click_download_in_menu(page):
                    return (True, None, "Video download button not found in menu")
            download = await dl_info.value
            await download.save_as(str(save_file))

            size = save_file.stat().st_size
            if size < 1000:
                return (True, None, f"Video file too small ({size} bytes)")

            log.info(f"Video downloaded: {save_file} ({size / (1024*1024):.1f}MB)")

            # Re-mux to fix DASH format
            try:
                import subprocess
                fixed = save_file.with_suffix('.fixed' + save_file.suffix)
                subprocess.run(
                    ['ffmpeg', '-i', str(save_file), '-c', 'copy',
                     '-movflags', '+faststart', str(fixed), '-y'],
                    capture_output=True, timeout=120,
                )
                if fixed.exists() and fixed.stat().st_size > 1000:
                    fixed.replace(save_file)
                    log.info(f"Re-muxed video with faststart: {save_file}")
            except Exception as e:
                log.warning(f"Video re-mux failed (original kept): {e}")

            return (True, str(save_file), None)
        except Exception as e:
            log.error(f"Video download failed: {type(e).__name__}: {e}")
            return (True, None, f"Video download error: {e}")

    except Exception as e:
        log.error(f"check_and_download_video error: {type(e).__name__}: {e}")
        return (False, None, str(e))
    finally:
        if page:
            try:
                await page.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Deploy: Stage files (copy only) + Batch git push
# (Unchanged from Chrome version)
# ---------------------------------------------------------------------------

def _stage_audio_to_website(ep_id: str, audio_path: str, topic: dict):
    """Copy audio to website repo and update frontmatter. No git operations."""
    audio_dir = WEBSITE_REPO / "assets" / "audio" / ep_id
    audio_dir.mkdir(parents=True, exist_ok=True)

    dest = audio_dir / "podcast.m4a"
    shutil.copy2(audio_path, dest)
    log.info(f"Staged audio: {dest} ({dest.stat().st_size} bytes)")

    ep_file = WEBSITE_REPO / "_episodes" / f"{ep_id}.md"
    if ep_file.exists():
        content = ep_file.read_text(encoding="utf-8")
        if "podcast_audio:" not in content:
            parts = content.split("---", 2)
            if len(parts) >= 3:
                front_matter = parts[1].rstrip() + "\npodcast_audio: podcast.m4a\n"
                content = "---" + front_matter + "---" + parts[2]
                ep_file.write_text(content, encoding="utf-8")
                log.info(f"Updated {ep_file.name} front matter with podcast_audio")


def _stage_video_to_website(ep_id: str, video_path: str, topic: dict):
    """Copy video to website repo and update frontmatter. No git operations."""
    video_dir = WEBSITE_REPO / "assets" / "video" / ep_id
    video_dir.mkdir(parents=True, exist_ok=True)

    dest = video_dir / "video_overview.mp4"
    shutil.copy2(video_path, dest)
    log.info(f"Staged video: {dest} ({dest.stat().st_size} bytes)")

    ep_file = WEBSITE_REPO / "_episodes" / f"{ep_id}.md"
    if ep_file.exists():
        content = ep_file.read_text(encoding="utf-8")
        if "video_overview:" not in content:
            parts = content.split("---", 2)
            if len(parts) >= 3:
                front_matter = parts[1].rstrip() + "\nvideo_overview: video_overview.mp4\n"
                content = "---" + front_matter + "---" + parts[2]
                ep_file.write_text(content, encoding="utf-8")
                log.info(f"Updated {ep_file.name} front matter with video_overview")


def _git_batch_push(commit_message: str) -> bool:
    """Git add → pull --rebase --autostash → commit → push."""
    try:
        subprocess.run(
            ["git", "add", "-A"],
            cwd=str(WEBSITE_REPO), check=True, timeout=30, capture_output=True,
        )
        subprocess.run(
            ["git", "pull", "--rebase", "--autostash", "--no-edit"],
            cwd=str(WEBSITE_REPO), check=True, timeout=120, capture_output=True,
        )
        diff = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=str(WEBSITE_REPO), capture_output=True,
        )
        if diff.returncode == 0:
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(WEBSITE_REPO), capture_output=True, text=True, timeout=10,
            )
            if not status.stdout.strip():
                log.info("No changes to commit")
                return True
            subprocess.run(
                ["git", "add", "-A"],
                cwd=str(WEBSITE_REPO), check=True, timeout=30, capture_output=True,
            )
        subprocess.run(
            ["git", "commit", "-m", commit_message],
            cwd=str(WEBSITE_REPO), check=True, timeout=30, capture_output=True,
        )
        subprocess.run(
            ["git", "push"],
            cwd=str(WEBSITE_REPO), check=True, timeout=600, capture_output=True,
        )
        log.info(f"Git batch push succeeded: {commit_message}")
        return True
    except subprocess.CalledProcessError as e:
        log.error(f"Git batch push error: {e.stderr.decode() if e.stderr else e}")
        return False
    except subprocess.TimeoutExpired as e:
        log.error(f"Git batch push timeout: {e.cmd} after {e.timeout}s")
        return False


async def main():
    log.info("Podcast tracker (Firefox): checking for generating episodes...")

    if not TOPICS_FILE.exists():
        log.warning("topics.json not found")
        return

    topics_data = json.loads(TOPICS_FILE.read_text())
    generating = []

    for topic in topics_data["topics"]:
        ep_id = topic["id"]
        state_file = CONTENT_DIR / ep_id / "state.json"
        if not state_file.exists():
            continue

        state = json.loads(state_file.read_text())

        podcast_status = state.get("podcast_status", "")
        video_status = state.get("video_overview_status", "")

        if podcast_status in ("generating",) and state.get("podcast_notebook_url"):
            generating.append((topic, state, state_file))
        elif video_status == "generating" and state.get("podcast_notebook_url"):
            generating.append((topic, state, state_file))

    if not generating:
        log.info("No episodes with generating podcasts/videos")
        return

    log.info(f"Found {len(generating)} episodes to check")

    # --- Process each generating episode ---
    pending_deploys = []

    for topic, state, state_file in generating:
        ep_id = topic["id"]
        state_changed = False
        url = f"https://ai-cooperation.github.io/sustainability-100/episodes/{ep_id.lower()}/"
        notebook_url = state.get("podcast_notebook_url", "")

        # --- Stuck timeout check ---
        if state.get("podcast_status") == "generating":
            sf_mtime = state_file.stat().st_mtime
            hours_stuck = (_time.time() - sf_mtime) / 3600
            if hours_stuck > STUCK_TIMEOUT_HOURS:
                log.warning(f"[{ep_id}] Podcast stuck for {hours_stuck:.1f}h > {STUCK_TIMEOUT_HOURS}h, marking failed")
                state["podcast_status"] = "failed"
                state["podcast_error"] = f"Stuck for {hours_stuck:.1f} hours"
                state_changed = True
                await tg_send(
                    f"<b>⚠️ Podcast 超時 — {ep_id}</b>\n\n"
                    f"「{topic['title']}」\n"
                    f"生成超過 {STUCK_TIMEOUT_HOURS} 小時，已標記為失敗。\n"
                    f"需要手動重新觸發 Step 2。"
                )

        # --- Audio check ---
        if state.get("podcast_status") == "generating":
            save_path = str(CONTENT_DIR / ep_id / "podcast.m4a")
            log.info(f"[{ep_id}] Checking podcast audio status...")
            is_ready, audio_path, error = await check_and_download(save_path, notebook_url)

            if is_ready and audio_path:
                state["podcast_status"] = "completed"
                state["podcast_audio_path"] = audio_path
                state_changed = True
                log.info(f"[{ep_id}] Podcast downloaded: {audio_path}")
                state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2))
                try:
                    _stage_audio_to_website(ep_id, audio_path, topic)
                    pending_deploys.append((ep_id, "audio", topic, url, state, state_file))
                except Exception as e:
                    log.error(f"[{ep_id}] Stage audio failed: {e}")
            elif is_ready and error:
                state["podcast_status"] = "download_failed"
                state["podcast_error"] = error
                state_changed = True
                log.warning(f"[{ep_id}] Audio ready but download failed: {error}")
            elif error == "no_audio_section":
                log.warning(f"[{ep_id}] No Audio Overview section at all")
            elif not is_ready and error:
                log.warning(f"[{ep_id}] Audio check error: {error}")
            else:
                log.info(f"[{ep_id}] Audio still generating")

        # --- Video Overview check ---
        if state.get("video_overview_status") == "generating":
            video_save_path = str(CONTENT_DIR / ep_id / "video_overview.mp4")
            log.info(f"[{ep_id}] Checking video overview status...")

            v_ready, v_path, v_error = await check_and_download_video(video_save_path, notebook_url)

            if v_ready and v_path:
                state["video_overview_status"] = "completed"
                state["video_overview_path"] = v_path
                state_changed = True
                log.info(f"[{ep_id}] Video overview downloaded: {v_path}")
                state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2))
                try:
                    _stage_video_to_website(ep_id, v_path, topic)
                    pending_deploys.append((ep_id, "video", topic, url, state, state_file))
                except Exception as e:
                    log.error(f"[{ep_id}] Stage video failed: {e}")
            elif v_ready and v_error:
                state["video_overview_status"] = "download_failed"
                state["video_overview_error"] = v_error
                state_changed = True
                log.warning(f"[{ep_id}] Video ready but download failed: {v_error}")
            elif v_error == "no_video_section":
                state["video_overview_status"] = "skipped"
                state_changed = True
                log.info(f"[{ep_id}] No Video Overview section — marking as skipped")
            elif not v_ready and v_error:
                log.warning(f"[{ep_id}] Video check error: {v_error}")
            else:
                log.info(f"[{ep_id}] Video still generating")

        if state_changed:
            state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2))

        # Keep-alive: extend Firefox idle timeout if still generating
        still_generating = (
            state.get("podcast_status") == "generating" or
            state.get("video_overview_status") == "generating"
        )
        if still_generating:
            fm = await _get_firefox_mgr()
            fm.keep_alive("firefox-notebooklm", 900)  # 15 min

    # --- Batch deploy: one git push for all staged files ---
    if pending_deploys:
        parts = []
        for ep_id, dtype, topic, *_ in pending_deploys:
            parts.append(f"{ep_id} {dtype}")
        commit_msg = f"Deploy: {', '.join(parts)}"
        log.info(f"Batch deploy: {len(pending_deploys)} items — {commit_msg}")

        pushed = _git_batch_push(commit_msg)

        for ep_id, dtype, topic, url, state, state_file in pending_deploys:
            if pushed:
                if dtype == "audio":
                    state["podcast_deployed"] = True
                else:
                    state["video_deployed"] = True
                state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2))

                emoji = "\U0001f399\ufe0f" if dtype == "audio" else "\U0001f3ac"
                label = "Podcast" if dtype == "audio" else "Video Overview"
                action = "收聽" if dtype == "audio" else "觀看"
                await tg_send(
                    f"<b>{emoji} {label} 就緒 — {ep_id}</b>\n\n"
                    f"「{topic['title']}」\n\n"
                    f"🔗 <a href=\"{url}\">線上{action}</a>\n"
                    f"⏳ 網站更新約需 2-3 分鐘"
                )
            else:
                label = "Podcast" if dtype == "audio" else "Video"
                ftype = "音檔" if dtype == "audio" else "影片"
                await tg_send(
                    f"<b>⚠️ {label} 已下載但部署失敗 — {ep_id}</b>\n\n"
                    f"「{topic['title']}」\n"
                    f"{ftype}已存，等待下次批次部署"
                )
    else:
        log.info("No new downloads to deploy")

    # Close Firefox instances
    if _firefox_mgr:
        await _firefox_mgr.close_all()


if __name__ == "__main__":
    asyncio.run(main())
