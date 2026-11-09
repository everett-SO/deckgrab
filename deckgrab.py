"""deckgrab: turn a DocSend / Papermark / similar shared-deck link into a PDF.

Strategy, in order:
  1. DocSend: hit its per-page JSON endpoint for full-res slide images.
  2. Any viewer: page through it and keep the large images it loads.
  3. Fallback: screenshot the viewer area on each page.

CLI:  python deckgrab.py <url> -e you@fund.com [-p passcode] [--agree] [-o out.pdf]
"""
from __future__ import annotations

import io
import re
import statistics
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from PIL import Image, ImageChops, ImageStat
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

MIN_SIDE = 500  # ignore images smaller than this (logos, avatars, icons)
MAX_PAGES = 300
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
NEXT_BUTTONS = [
    'button[aria-label*="next" i]',
    '[data-testid*="next" i]',
    'button:has-text("Next")',
    "#nextPageButton",
    ".next-page",
]
SUBMIT_BUTTONS = [
    'button[type="submit"]',
    'input[type="submit"]',
    'button:has-text("Continue")',
    'button:has-text("View")',
    'button:has-text("Submit")',
]


class GrabError(Exception):
    pass


@dataclass
class Result:
    pdf: bytes
    pages: int
    method: str
    title: str


# ---------------------------------------------------------------- entry point
def grab(url: str, email: str = "", passcode: str = "", agree: bool = False, log=print) -> Result:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=UA, viewport={"width": 1600, "height": 1000}, device_scale_factor=2
        )
        page = ctx.new_page()
        seen = []  # image responses, in load order
        page.on("response", lambda r: seen.append(r) if r.request.resource_type == "image" else None)
        try:
            log(f"Opening {url}")
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            settle(page)
            pass_gate(page, email, passcode, agree, seen, log)
            title = clean_title(page.title())

            if "docsend" in urlparse(page.url).netloc:
                imgs = docsend_pages(page, log)
                if imgs:
                    return Result(to_pdf(imgs), len(imgs), "docsend", title)
                log("DocSend fast path failed, paging through viewer instead")

            shots = walk(page, log)
            net = page_images(seen)
            if net and len(net) >= max(1, round(len(shots) * 0.8)):
                return Result(to_pdf(net), len(net), "images", title)
            if shots:
                return Result(to_pdf(shots), len(shots), "screenshots", title)
            raise GrabError("Couldn't find any pages on that link.")
        finally:
            browser.close()


# ---------------------------------------------------------------- gate
def pass_gate(page, email, passcode, agree, seen, log):
    box = first_visible(page, ['input[type="email"]', 'input[name*="email" i]', 'input[id*="email" i]'])
    if not box:
        return
    if not email:
        raise GrabError("This link asks for an email. Add one and try again.")
    log("Filling email gate")
    box.fill(email)
    if passcode:
        pw = first_visible(page, ['input[type="password"]', 'input[name*="pass" i]'])
        if pw:
            pw.fill(passcode)
    if agree:
        for cb in page.locator('input[type="checkbox"], button[role="checkbox"]').all():
            try:
                if cb.is_visible() and cb.get_attribute("aria-checked") != "true" and not cb.is_checked():
                    cb.click()
            except Exception:
                pass
    seen.clear()  # drop anything loaded behind the gate (blurred previews etc.)
    btn = first_visible(page, SUBMIT_BUTTONS)
    btn.click() if btn else box.press("Enter")
    settle(page, 3000)
    if first_visible(page, ['input[type="email"]']):
        raise GrabError(
            "Still stuck on the gate. The link may need a passcode, an agreement tick (--agree), "
            "an emailed verification code, or an allowed email domain."
        )


# ---------------------------------------------------------------- DocSend
def docsend_pages(page, log):
    m = re.search(r"https://[^/]+/view/[A-Za-z0-9]+", page.url)
    if not m:
        return []
    base, imgs = m.group(0), []
    for n in range(1, MAX_PAGES + 1):
        try:
            r = page.request.get(f"{base}/page_data/{n}")
            if not r.ok:
                break
            img_url = r.json().get("imageUrl")
            if not img_url:
                break
            ir = page.request.get(img_url)
            if not ir.ok:
                break
            imgs.append(Image.open(io.BytesIO(ir.body())))
        except Exception:
            break
        log(f"Page {n}")
    return imgs


# ---------------------------------------------------------------- generic viewer
def walk(page, log):
    """Step through the viewer until nothing changes. Returns clipped screenshots."""
    cur = snap(page)
    shots = [cur]
    log("Page 1")
    actions = [
        lambda: page.keyboard.press("ArrowRight"),
        lambda: click_first(page, NEXT_BUTTONS),
        lambda: page.mouse.wheel(0, 900),
    ]
    for _ in range(MAX_PAGES - 1):
        new = None
        for act in actions:
            try:
                if act() is False:
                    continue
            except Exception:
                continue
            page.wait_for_timeout(1200)
            candidate = snap(page)
            if differs(cur, candidate):
                new = candidate
                break
        if new is None:
            break
        cur = new
        shots.append(cur)
        log(f"Page {len(shots)}")
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except PWTimeout:
        pass
    return shots


VIEWER_JS = """() => {
  let best = null, area = 0;
  for (const e of document.querySelectorAll('img, canvas, embed, object')) {
    const r = e.getBoundingClientRect();
    if (r.width < 50 || r.height < 50 || r.bottom <= 0 || r.right <= 0 ||
        r.top >= innerHeight || r.left >= innerWidth) continue;
    const a = r.width * r.height;
    if (a > area) { area = a; best = r; }
  }
  if (!best || area < innerWidth * innerHeight * 0.2) return null;
  const x = Math.max(0, best.left), y = Math.max(0, best.top);
  return {x, y, width: Math.min(best.right, innerWidth) - x, height: Math.min(best.bottom, innerHeight) - y};
}"""


def snap(page):
    clip = None
    try:
        clip = page.evaluate(VIEWER_JS)
    except Exception:
        pass
    return Image.open(io.BytesIO(page.screenshot(type="png", clip=clip)))


def differs(a, b, threshold=1.5):
    if a.size != b.size:
        return True
    sa = a.convert("L").resize((200, 125))
    sb = b.convert("L").resize((200, 125))
    return ImageStat.Stat(ImageChops.difference(sa, sb)).mean[0] > threshold


def page_images(responses):
    """Large images the viewer loaded, one per page, in first-seen order."""
    best = {}  # key -> image (keep largest variant of each)
    order = []
    for r in responses:
        try:
            body = r.body()
            img = Image.open(io.BytesIO(body))
            img.load()
        except Exception:
            continue
        if min(img.size) < MIN_SIDE:
            continue
        key = image_key(r.url)
        if key not in best:
            order.append(key)
            best[key] = img
        elif img.size[0] * img.size[1] > best[key].size[0] * best[key].size[1]:
            best[key] = img
    imgs = [best[k] for k in order]
    if len(imgs) < 3:
        return imgs
    # drop banners/backgrounds whose shape doesn't match the slides
    med = statistics.median(i.size[0] / i.size[1] for i in imgs)
    return [i for i in imgs if abs(i.size[0] / i.size[1] - med) / med < 0.05]


def image_key(url):
    u = urlparse(url)
    q = parse_qs(u.query)
    if "url" in q:  # Next.js /_next/image?url=...&w=...
        return image_key(q["url"][0]) if q["url"][0].startswith("http") else q["url"][0]
    return u.netloc + u.path


# ---------------------------------------------------------------- helpers
def first_visible(page, selectors):
    for s in selectors:
        loc = page.locator(s)
        try:
            n = min(loc.count(), 5)
        except Exception:
            continue
        for i in range(n):
            el = loc.nth(i)
            try:
                if el.is_visible():
                    return el
            except Exception:
                pass
    return None


def click_first(page, selectors):
    el = first_visible(page, selectors)
    if not el or not el.is_enabled():
        return False
    el.click(timeout=2000)


def settle(page, ms=1500):
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except PWTimeout:
        pass
    page.wait_for_timeout(ms)


def clean_title(t):
    t = re.split(r"\s+[|\-]\s+(DocSend|Papermark)", t or "", flags=re.I)[0]
    return re.sub(r"[^\w\- ]+", "", t).strip()[:80] or "deck"


def to_pdf(images):
    pages = []
    for im in images:
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, "white")
            bg.paste(im, mask=im.split()[-1])
            im = bg
        pages.append(im.convert("RGB"))
    buf = io.BytesIO()
    pages[0].save(buf, "PDF", save_all=True, append_images=pages[1:], resolution=150)
    return buf.getvalue()


# ---------------------------------------------------------------- CLI
if __name__ == "__main__":
    import argparse
    import os

    ap = argparse.ArgumentParser(description="Save a DocSend/Papermark/etc. deck as a PDF")
    ap.add_argument("url")
    ap.add_argument("-e", "--email", default=os.getenv("DECKGRAB_EMAIL", ""))
    ap.add_argument("-p", "--passcode", default="")
    ap.add_argument("--agree", action="store_true", help="tick agreement/NDA checkboxes on the gate")
    ap.add_argument("-o", "--out")
    a = ap.parse_args()
    try:
        res = grab(a.url, a.email, a.passcode, a.agree)
    except GrabError as e:
        raise SystemExit(f"Error: {e}")
    out = a.out or f"{res.title}.pdf"
    with open(out, "wb") as f:
        f.write(res.pdf)
    print(f"Saved {res.pages} pages to {out} (via {res.method})")
