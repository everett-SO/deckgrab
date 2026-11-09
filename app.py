"""Web UI: paste a link, download a PDF.  Run: streamlit run app.py"""
import os
import subprocess
import sys

import streamlit as st

from deckgrab import GrabError, grab

st.set_page_config(page_title="Deck Grab", page_icon="📄")


@st.cache_resource
def ensure_browser():
    # no-op in the Docker image; installs Chromium on first run elsewhere
    subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=False)


ensure_browser()

# Optional shared password so a hosted copy isn't open to the world
app_pw = os.getenv("APP_PASSWORD")
if app_pw and not st.session_state.get("authed"):
    if st.text_input("Password", type="password") == app_pw:
        st.session_state.authed = True
        st.rerun()
    st.stop()

st.title("Deck Grab")
st.caption("Paste a DocSend, Papermark or similar link and get a PDF back.")

with st.form("grab"):
    url = st.text_input("Link")
    email = st.text_input("Your email (if the link asks for one)")
    passcode = st.text_input("Passcode (optional)", type="password")
    agree = st.checkbox("Tick any agreement/NDA checkbox on the gate")
    go = st.form_submit_button("Get PDF", type="primary")

if go:
    url = url.strip()
    if not url.startswith("http"):
        st.error("That doesn't look like a link.")
        st.stop()
    status = st.status("Working...", expanded=True)
    try:
        res = grab(url, email.strip(), passcode, agree, log=status.write)
    except GrabError as e:
        status.update(label="Failed", state="error")
        st.error(str(e))
        st.stop()
    except Exception as e:
        status.update(label="Failed", state="error")
        st.error(f"Something broke: {e}")
        st.stop()
    status.update(label=f"Done: {res.pages} pages ({res.method})", state="complete", expanded=False)
    st.download_button(
        "Download PDF", res.pdf, file_name=f"{res.title}.pdf", mime="application/pdf", type="primary"
    )
