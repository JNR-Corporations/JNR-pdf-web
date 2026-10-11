from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import shutil
import socket
import tempfile
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urldefrag, urljoin, urlparse, urlunparse
from xml.etree import ElementTree

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field, field_validator
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from pypdf import PdfWriter

BASE_DIR = Path(__file__).resolve().parent
JOB_STORE: dict[str, dict[str, Any]] = {}
JOB_TASKS: dict[str, asyncio.Task] = {}
HOST_CACHE: dict[str, tuple[bool, float]] = {}
JOB_TTL_SECONDS = 60 * 60
MAX_ACTIVE_JOBS = 2
MAX_PDF_BYTES = 350 * 1024 * 1024


class ConvertRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)
    mode: str = Field(default="crawl")
    max_pages: int = Field(default=25, ge=1, le=150)
    max_depth: int = Field(default=1, ge=0, le=5)
    paper: str = Field(default="A4")
    landscape: bool = False
    media: str = Field(default="screen")
    wait_ms: int = Field(default=800, ge=0, le=3500)
    include_background: bool = True
    headers: bool = True
    lazy_load: bool = True
    respect_query: bool = False

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        if v not in {"single", "crawl", "sitemap"}:
            raise ValueError("Choose single, crawl, or sitemap mode.")
        return v

    @field_validator("paper")
    @classmethod
    def validate_paper(cls, v: str) -> str:
        if v not in {"A4", "A3", "A5", "Letter", "Legal", "Tabloid"}:
            raise ValueError("Unsupported paper size.")
        return v

    @field_validator("media")
    @classmethod
    def validate_media(cls, v: str) -> str:
        if v not in {"screen", "print"}:
            raise ValueError("Media must be screen or print.")
        return v

    @field_validator("url")
    @classmethod
    def normalize_url(cls, v: str) -> str:
        value = v.strip()
        if not re.match(r"^https?://", value, re.I):
            value = "https://" + value
        parsed = urlparse(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Enter a valid public HTTP or HTTPS URL.")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("URLs containing embedded credentials are not supported.")
        if any(ord(ch) < 32 for ch in value):
            raise ValueError("The URL contains control characters.")
        return urlunparse(parsed._replace(fragment=""))


def _is_reserved_hostname(host: str) -> bool:
    hostname = host.rstrip(".").lower()
    return (
        hostname in {"localhost", "localhost.localdomain", "metadata.google.internal"}
        or hostname.endswith((".localhost", ".local", ".lan", ".home", ".internal", ".test", ".invalid", ".example"))
        or not hostname
    )


def _is_global_ip(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_global
    except ValueError:
        return False


def _resolve_public_sync(host: str) -> bool:
    if _is_reserved_hostname(host):
        return False
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_global
    except ValueError:
        pass
    try:
        results = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        addresses = {item[4][0].split("%", 1)[0] for item in results}
        return bool(addresses) and all(_is_global_ip(addr) for addr in addresses)
    except (OSError, UnicodeError, ValueError):
        return False


async def is_public_http_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return False
        if parsed.username is not None or parsed.password is not None:
            return False
        host = parsed.hostname.rstrip(".").lower()
        now = time.monotonic()
        cached = HOST_CACHE.get(host)
        if cached and cached[1] > now:
            return cached[0]
        loop = asyncio.get_running_loop()
        result = await asyncio.wait_for(loop.run_in_executor(None, _resolve_public_sync, host), timeout=5.5)
        HOST_CACHE[host] = (result, now + (300 if result else 45))
        return result
    except (ValueError, asyncio.TimeoutError, OSError):
        return False


def normalized_page_url(url: str, respect_query: bool, keep_start_query: bool = False) -> str:
    parsed = urlparse(urldefrag(url)[0])
    path = parsed.path or "/"
    query = parsed.query if (respect_query or keep_start_query) else ""
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", query, ""))


def origin_of(url: str) -> tuple[str, str, int | None]:
    p = urlparse(url)
    return p.scheme.lower(), (p.hostname or "").lower(), p.port


def is_same_origin(url: str, base_url: str) -> bool:
    try:
        a, b = origin_of(url), origin_of(base_url)
        if a == b:
            return True
        # Treat www and bare host as different origins; deliberate restriction for safer crawl scope.
        return False
    except ValueError:
        return False


def safe_html_candidate(url: str, base_url: str) -> bool:
    try:
        p = urlparse(url)
        if p.scheme.lower() not in {"http", "https"} or not p.hostname or p.username or p.password:
            return False
        if not is_same_origin(url, base_url):
            return False
        path = p.path.lower()
        if re.search(r"\.(?:pdf|png|jpe?g|gif|webp|svg|ico|css|js|mjs|map|woff2?|ttf|otf|eot|zip|rar|7z|gz|mp[34]|mov|avi|docx?|xlsx?|pptx?|xml|json|exe|dmg)(?:$|/)", path):
            return False
        if re.search(r"(?:^|/)(?:logout|log-out|signout|sign-out|delete|remove|unsubscribe|checkout|purchase|pay|confirm|reset-password)(?:/|$)", path):
            return False
        if any(x in path for x in ("/wp-admin/", "/admin/", "/api/", "/oauth/")):
            return False
        return True
    except ValueError:
        return False


async def safe_route_handler(route, request):
    url = request.url
    scheme = urlparse(url).scheme.lower()
    if scheme in {"data", "blob", "about"}:
        await route.continue_()
        return
    if scheme not in {"http", "https"}:
        await route.abort("blockedbyclient")
        return
    if not await is_public_http_url(url):
        await route.abort("blockedbyclient")
        return
    try:
        await route.continue_()
    except Exception:
        try:
            await route.abort()
        except Exception:
            pass


async def expand_lazy_content(page) -> None:
    # Bounded scroll loop triggers common intersection-observer image and content loaders,
    # without running unbounded infinite-scroll sites forever.
    await page.evaluate("""async () => {
      const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
      let last = -1, stable = 0;
      for (let i = 0; i < 28; i++) {
        window.scrollTo(0, Math.min(document.documentElement.scrollHeight, window.scrollY + Math.max(500, window.innerHeight * 0.8)));
        await pause(85);
        const h = Math.max(document.body?.scrollHeight || 0, document.documentElement.scrollHeight);
        if (h === last) stable++; else stable = 0;
        last = h;
        if (stable >= 3 && window.scrollY + window.innerHeight >= h - 4) break;
      }
      await pause(100);
      window.scrollTo(0, 0);
      try { await document.fonts.ready; } catch(e) {}
      await Promise.race([Promise.all(Array.from(document.images).map(img => {
        if (img.complete) return Promise.resolve();
        return new Promise(resolve => { img.addEventListener('load', resolve, {once:true}); img.addEventListener('error', resolve, {once:true}); setTimeout(resolve, 1800); });
      })), pause(3500)]);
    }""")


async def get_page_links(page, current_url: str, origin_url: str, respect_query: bool) -> list[str]:
    try:
        links = await page.locator("a[href]").evaluate_all("els => els.map(a => a.href).filter(Boolean)")
    except Exception:
        return []
    result: list[str] = []
    seen: set[str] = set()
    for link in links:
        try:
            absolute = urljoin(current_url, link)
            p = urlparse(absolute)
            if p.scheme.lower() not in {"http", "https"} or not safe_html_candidate(absolute, origin_url):
                continue
            canonical = normalized_page_url(absolute, respect_query)
            if canonical not in seen:
                seen.add(canonical)
                result.append(canonical)
        except Exception:
            continue
    return result


async def discover_sitemap_urls(page, origin_url: str) -> list[str]:
    """Try robots.txt sitemap declarations and conventional sitemap.xml through guarded browser navigation."""
    base = urlparse(origin_url)
    root = f"{base.scheme}://{base.netloc}"
    candidates = [urljoin(root + "/", "robots.txt"), urljoin(root + "/", "sitemap.xml")]
    sitemap_files: list[str] = []
    for candidate in candidates:
        try:
            response = await page.goto(candidate, wait_until="domcontentloaded", timeout=12000)
            if response is None or not response.ok:
                continue
            text = (await page.locator("body").inner_text(timeout=2000))[:1_000_000]
            if candidate.endswith("robots.txt"):
                for line in text.splitlines():
                    if line.strip().lower().startswith("sitemap:"):
                        sitemap_files.append(line.split(":", 1)[1].strip())
            else:
                sitemap_files.append(candidate)
        except Exception:
            continue
    # Deduplicate, only allow same-origin sitemap URLs, cap parsing work.
    out: list[str] = []
    for sitemap in list(dict.fromkeys(sitemap_files))[:8]:
        if is_same_origin(sitemap, origin_url) and await is_public_http_url(sitemap):
            try:
                response = await page.goto(sitemap, wait_until="domcontentloaded", timeout=15000)
                if response is None or not response.ok:
                    continue
                locs = await page.locator("loc").evaluate_all("els => els.map(e => (e.textContent || '').trim()).filter(Boolean)")
                if not locs:
                    # Some XML viewers expose the source as text instead of native XML elements.
                    source = await page.locator("body").inner_text(timeout=2000)
                    locs = re.findall(r"<loc>\s*(.*?)\s*</loc>", source, flags=re.I | re.S)
                for value in locs[:10000]:
                    value = value.strip()
                    if is_same_origin(value, origin_url) and safe_html_candidate(value, origin_url):
                        out.append(normalized_page_url(value, False))
            except Exception:
                continue
    return list(dict.fromkeys(out))


def job_public(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": job["id"], "status": job["status"], "message": job.get("message", ""),
        "error": job.get("error"), "max_pages": job["max_pages"],
        "rendered": job.get("rendered", 0), "pages_rendered": job.get("rendered", 0),
        "current_url": job.get("current_url"), "current_title": job.get("current_title"),
        "visited": job.get("visited", []), "size_bytes": job.get("size_bytes"),
        "download_url": job.get("download_url"), "created_at": job["created_at"],
        "errors": job.get("errors", []),
    }


def cleanup_old_jobs() -> None:
    now = time.time()
    for job_id, job in list(JOB_STORE.items()):
        if now - job.get("created_epoch", now) > JOB_TTL_SECONDS and job.get("status") not in {"running", "starting"}:
            try:
                if job.get("work_dir"):
                    shutil.rmtree(job["work_dir"], ignore_errors=True)
                if job.get("output_dir"):
                    shutil.rmtree(job["output_dir"], ignore_errors=True)
            except Exception:
                pass
            JOB_STORE.pop(job_id, None)
            JOB_TASKS.pop(job_id, None)


async def run_conversion(job_id: str, options: ConvertRequest) -> None:
    job = JOB_STORE[job_id]
    job["status"] = "running"
    job["message"] = "Starting Chromium and validating the starting URL…"
    work_dir = tempfile.mkdtemp(prefix="jnr_webpdf_parts_")
    output_dir = tempfile.mkdtemp(prefix="jnr_webpdf_output_")
    job["work_dir"] = work_dir
    job["output_dir"] = output_dir
    try:
        if not await is_public_http_url(options.url):
            raise RuntimeError("This URL could not be verified as a public website. Private, local, or reserved addresses are blocked.")
        parts: list[dict[str, Any]] = []
        visited: list[dict[str, str]] = []
        visit_seen: set[str] = set()
        errors: list[str] = []
        queue: deque[tuple[str, int]] = deque([(options.url, 0)])
        queued: set[str] = {normalized_page_url(options.url, options.respect_query, keep_start_query=True)}
        base_origin = options.url
        sitemap_seeded = False
        total_done = 0
        attempts = 0
        pdf_bytes_total = 0

        async with async_playwright() as playwright:
            launch_options: dict[str, Any] = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-background-networking"]}
            configured_chromium = os.environ.get("JNR_CHROMIUM_PATH", "").strip()
            if configured_chromium:
                if not Path(configured_chromium).is_file():
                    raise RuntimeError("JNR_CHROMIUM_PATH points to a browser binary that does not exist.")
                launch_options["executable_path"] = configured_chromium
            browser = await playwright.chromium.launch(**launch_options)
            context = await browser.new_context(
                viewport={"width": 1440 if not options.landscape else 1600, "height": 1000},
                device_scale_factor=1,
                accept_downloads=False,
                ignore_https_errors=False,
                java_script_enabled=True,
                service_workers="block",
            )
            await context.route("**/*", safe_route_handler)
            page = await context.new_page()
            page.set_default_navigation_timeout(45000)
            page.set_default_timeout(12000)

            while queue and attempts < options.max_pages:
                current, depth = queue.popleft()
                current = normalized_page_url(current, options.respect_query, keep_start_query=(attempts == 0))
                if current in visit_seen:
                    continue
                attempts += 1
                job["attempted"] = attempts
                visit_seen.add(current)
                job["current_url"] = current
                job["current_title"] = "Opening page…"
                job["message"] = f"Opening {current}"
                response = None
                try:
                    response = await page.goto(current, wait_until="domcontentloaded", timeout=45000)
                    final_url = urldefrag(page.url)[0]
                    if not await is_public_http_url(final_url):
                        raise RuntimeError("The page redirected to a non-public address; navigation was stopped.")
                    if response is not None:
                        content_type = (response.headers.get("content-type") or "").lower()
                        if content_type and not any(t in content_type for t in ("text/html", "application/xhtml+xml", "application/xml", "text/xml")):
                            raise RuntimeError(f"Skipped non-HTML response ({content_type.split(';')[0]}).")
                        if response.status >= 400:
                            raise RuntimeError(f"Website returned HTTP {response.status}.")
                    if total_done == 0:
                        base_origin = final_url
                    if options.media == "screen":
                        await page.emulate_media(media="screen")
                    else:
                        await page.emulate_media(media="print")
                    if options.wait_ms >= 1800:
                        try:
                            await page.wait_for_load_state("networkidle", timeout=3500)
                        except PlaywrightTimeoutError:
                            pass
                    if options.lazy_load:
                        await expand_lazy_content(page)
                    elif options.wait_ms:
                        await page.wait_for_timeout(options.wait_ms)
                    try:
                        await page.evaluate("document.fonts && document.fonts.ready ? document.fonts.ready : Promise.resolve()")
                    except Exception:
                        pass
                    title = (await page.title()).strip() or urlparse(final_url).netloc
                    job["current_url"] = final_url
                    job["current_title"] = title

                    # Discover initial sitemap URLs after the starting page establishes its final origin.
                    if options.mode == "sitemap" and not sitemap_seeded:
                        sitemap_seeded = True
                        discovered = await discover_sitemap_urls(page, base_origin) if options.max_depth >= 1 else []
                        for sitemap_url in discovered:
                            key = normalized_page_url(sitemap_url, options.respect_query)
                            if key not in queued and key not in visit_seen and len(queued) < 25000:
                                queued.add(key)
                                queue.append((key, 1))
                        # Restore the current page after sitemap probing so its original PDF is captured accurately.
                        await page.goto(final_url, wait_until="domcontentloaded", timeout=30000)
                        if options.media == "screen":
                            await page.emulate_media(media="screen")
                        else:
                            await page.emulate_media(media="print")
                        if options.lazy_load:
                            await expand_lazy_content(page)
                        if options.wait_ms:
                            await page.wait_for_timeout(options.wait_ms)
                        title = (await page.title()).strip() or title

                    links: list[str] = []
                    if options.mode != "single" and depth < options.max_depth:
                        links = await get_page_links(page, final_url, base_origin, options.respect_query)
                        for link in links:
                            key = normalized_page_url(link, options.respect_query)
                            if key not in queued and key not in visit_seen and len(queued) < 25000:
                                queued.add(key)
                                queue.append((key, depth + 1))

                    safe_title = re.sub(r"[\r\n]+", " ", title)[:180]
                    part_path = Path(work_dir) / f"page-{len(parts)+1:04d}.pdf"
                    header = f'<div style="width:100%;font-size:8px;padding:0 12mm;color:#777;font-family:Arial,sans-serif"><span class="title"></span><span style="float:right">{_html_escape(final_url)}</span></div>'
                    footer = '<div style="width:100%;font-size:8px;padding:0 12mm;color:#777;font-family:Arial,sans-serif;text-align:center"><span class="url"></span><span style="float:right"><span class="pageNumber"></span> / <span class="totalPages"></span></span></div>'
                    pdf_kwargs: dict[str, Any] = {
                        "path": str(part_path), "format": options.paper, "landscape": options.landscape,
                        "print_background": options.include_background, "prefer_css_page_size": False,
                        "margin": {"top": "15mm" if options.headers else "8mm", "right": "10mm", "bottom": "15mm" if options.headers else "8mm", "left": "10mm"},
                    }
                    if options.headers:
                        pdf_kwargs.update({"display_header_footer": True, "header_template": header, "footer_template": footer})
                    await page.pdf(**pdf_kwargs)
                    file_size = part_path.stat().st_size
                    if file_size <= 0:
                        raise RuntimeError("The PDF renderer produced an empty page file.")
                    pdf_bytes_total += file_size
                    if pdf_bytes_total > MAX_PDF_BYTES:
                        part_path.unlink(missing_ok=True)
                        raise RuntimeError("The draft PDF exceeded the 350 MB safety limit. Reduce the page cap or use smaller sections.")
                    parts.append({"path": str(part_path), "title": safe_title, "url": final_url})
                    total_done += 1
                    visited.append({"url": final_url, "title": safe_title, "status": "done"})
                    job["rendered"] = total_done
                    job["visited"] = visited[-300:]
                    job["errors"] = errors[-50:]
                    job["message"] = f"Rendered {total_done} page URL(s); {len(queue)} queued."
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    note = f"{current}: {str(exc)[:240]}"
                    errors.append(note)
                    visited.append({"url": current, "title": str(exc)[:110], "status": "failed"})
                    job["visited"] = visited[-300:]
                    job["errors"] = errors[-50:]
                    job["message"] = f"Skipped a page that could not be rendered. {total_done} rendered so far."
                    continue
            await context.close()
            await browser.close()

        if not parts:
            detail = errors[0] if errors else "No public HTML pages could be rendered."
            raise RuntimeError("Nothing could be converted. " + detail)

        job["message"] = "Combining rendered pages and creating the PDF outline…"
        writer = PdfWriter()
        for part in parts:
            try:
                writer.append(part["path"], outline_item=(part["title"][:180] or part["url"]), import_outline=False)
            except TypeError:
                writer.append(part["path"])
        domain = (urlparse(options.url).hostname or "website").replace(".", "-")
        domain = re.sub(r"[^A-Za-z0-9_-]", "", domain)[:80] or "website"
        filename = f"JNR-Website-{domain}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.pdf"
        output_path = Path(output_dir) / filename
        with output_path.open("wb") as stream:
            writer.write(stream)
        writer.close()
        size_bytes = output_path.stat().st_size
        job.update({
            "status": "complete", "message": (f"PDF complete: {len(parts)} URL(s) rendered." + (f" {len(errors)} page(s) were skipped; review the log." if errors else "")),
            "rendered": len(parts), "visited": visited[-300:], "errors": errors[-50:],
            "output_path": str(output_path), "filename": filename, "size_bytes": size_bytes,
            "download_url": f"/api/jobs/{job_id}/download", "finished_epoch": time.time(),
        })
    except asyncio.CancelledError:
        job.update({"status": "cancelled", "message": "Job cancelled.", "finished_epoch": time.time()})
        raise
    except Exception as exc:
        job.update({"status": "failed", "error": str(exc)[:1200], "message": "No final PDF was created.", "finished_epoch": time.time()})
    finally:
        # Keep only the final PDF; rendered per-page intermediates are no longer required.
        if work_dir:
            shutil.rmtree(work_dir, ignore_errors=True)
        if job.get("status") != "complete" and output_dir:
            shutil.rmtree(output_dir, ignore_errors=True)
        if job.get("status") in {"complete", "failed", "cancelled"}:
            job["created_epoch"] = job.get("created_epoch", time.time())
        JOB_TASKS.pop(job_id, None)


def _html_escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#39;")


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    tasks = list(JOB_TASKS.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title="JNR Website to PDF Studio", version="1.0.0", docs_url=None, redoc_url=None, lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def home():
    return HTMLResponse((BASE_DIR / "index.html").read_text(encoding="utf-8"))


@app.get("/api/health")
async def health():
    return {"ok": True, "version": "Playwright Chromium · hosted/local backend", "active_jobs": sum(1 for j in JOB_STORE.values() if j.get("status") in {"starting", "running"})}


@app.post("/api/jobs")
async def create_job(options: ConvertRequest):
    cleanup_old_jobs()
    active = sum(1 for job in JOB_STORE.values() if job.get("status") in {"starting", "running"})
    if active >= MAX_ACTIVE_JOBS:
        raise HTTPException(status_code=429, detail="Two conversion jobs are already running. Wait for one to finish, then try again.")
    if not await is_public_http_url(options.url):
        raise HTTPException(status_code=400, detail="This URL could not be verified as a public website. Private, local, link-local, and reserved addresses are blocked.")
    job_id = uuid.uuid4().hex
    now = time.time()
    JOB_STORE[job_id] = {
        "id": job_id, "status": "starting", "message": "Job accepted.", "max_pages": options.max_pages,
        "rendered": 0, "visited": [], "errors": [], "created_at": datetime.now(timezone.utc).isoformat(), "created_epoch": now,
    }
    JOB_TASKS[job_id] = asyncio.create_task(run_conversion(job_id, options), name=f"jnr-webpdf-{job_id[:8]}")
    return {"id": job_id, "status": "starting"}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    job = JOB_STORE.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="This job has expired or does not exist. Start a new conversion.")
    return job_public(job)


@app.delete("/api/jobs/{job_id}")
async def cancel_job(job_id: str):
    job = JOB_STORE.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="This job has expired or does not exist.")
    task = JOB_TASKS.get(job_id)
    if task and not task.done():
        task.cancel()
        return {"id": job_id, "status": "cancelling", "message": "Cancellation requested."}
    if job.get("status") == "complete":
        raise HTTPException(status_code=409, detail="The conversion has already completed.")
    return job_public(job)


@app.get("/api/jobs/{job_id}/preview")
async def preview_job(job_id: str):
    job = JOB_STORE.get(job_id)
    if not job or job.get("status") != "complete" or not job.get("output_path"):
        raise HTTPException(status_code=409, detail="The PDF is not ready yet.")
    path = Path(job["output_path"])
    if not path.is_file():
        raise HTTPException(status_code=410, detail="The temporary PDF has expired. Please convert again.")
    inline_name = job.get("filename", "JNR-Website.pdf").replace('"', "")
    return FileResponse(path, media_type="application/pdf", headers={"Cache-Control": "no-store", "Content-Disposition": f'inline; filename="{inline_name}"'})


@app.get("/api/jobs/{job_id}/download")
async def download_job(job_id: str):
    job = JOB_STORE.get(job_id)
    if not job or job.get("status") != "complete" or not job.get("output_path"):
        raise HTTPException(status_code=409, detail="The PDF is not ready yet.")
    path = Path(job["output_path"])
    if not path.is_file():
        raise HTTPException(status_code=410, detail="The temporary PDF has expired. Please convert again.")
    return FileResponse(path, media_type="application/pdf", filename=job.get("filename", "JNR-Website.pdf"), headers={"Cache-Control": "no-store"})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8765")), reload=False)
