# Deck Grab

Paste a DocSend, Papermark or similar viewer link, get a PDF.

## Run it yourself

```bash
pip install -r requirements.txt
playwright install chromium
streamlit run app.py          # web UI at http://localhost:8501
# or straight from the terminal:
python deckgrab.py "https://docsend.com/view/abc123" -e you@superorganism.com
```

Set `DECKGRAB_EMAIL` in your shell to skip `-e` on the CLI.

## Share it

Easiest: push this folder to a GitHub repo and deploy the Dockerfile on Render
(New > Web Service > pick the repo, it detects Docker). Railway and Fly.io work the same way.
Set an `APP_PASSWORD` env var so only people you give it to can use it.
Free tiers sleep when idle, so the first request after a while takes ~30s to wake.

Teammates who'd rather run it locally can just use the "Run it yourself" steps.

## How it works

1. DocSend `/view/` links: pulls full-res slide images from DocSend's per-page endpoint.
2. Anything else: opens the link in headless Chrome, fills the email gate, presses
   the right arrow (then tries a Next button, then scrolling) until pages stop changing,
   and keeps the large images the viewer loaded.
3. If the viewer draws pages on a canvas instead of loading images, it falls back to
   screenshots of the viewer area.

## Limits

- Output is image-based, so text isn't selectable. Run it through OCR if you need search.
- Can't get past gates that email you a verification code, or that restrict to an allowlisted domain you're not on.
- DocSend data rooms (`/view/s/...`) aren't supported; open the individual doc link.
- Viewers change their markup. If one breaks, the selectors near the top of `deckgrab.py` are the place to look.
