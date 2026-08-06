import streamlit as st
import httpx
import re
import json
import time
import os
import threading

st.set_page_config(page_title="VaxiJen Predictor", page_icon="🧬", layout="wide")

VAXIJEN_URL = "https://www.ddg-pharmfac.net/vaxijen/VaxiJen/VaxiJen.html"
VAXIJEN_SCRIPT = "https://www.ddg-pharmfac.net/vaxijen/scripts/VaxiJen_scripts/VaxiJen3.pl"
TARGETS = ["bacteria", "virus", "tumour", "parasite", "fungal"]
COOKIE_FILE = os.path.join(os.path.dirname(__file__), ".vaxijen_cookies.json")


def _load_cookies():
    if os.path.exists(COOKIE_FILE):
        with open(COOKIE_FILE) as f:
            data = json.load(f)
        if time.time() - data.get("ts", 0) < 3600:
            return data["cookies"], data["user_agent"]
    return None, None


def _save_cookies(cookies, user_agent):
    with open(COOKIE_FILE, "w") as f:
        json.dump({"cookies": cookies, "user_agent": user_agent, "ts": time.time()}, f)


def _launch_browser():
    from seleniumbase import SB
    with st.status("Launching Chrome to bypass Cloudflare...", expanded=True) as status:
        st.write("Starting browser...")
        t0 = time.time()
        with SB(uc=True, headless2=True) as sb:
            sb.activate_cdp_mode(VAXIJEN_URL)
            for i in range(30):
                time.sleep(2)
                title = sb.get_title()
                st.write(f"[{i*2}s] Title: {title}")
                if "moment" not in title.lower() and title:
                    status.update(label=f"Cloudflare bypassed in {i*2}s", state="complete")
                    break
            time.sleep(2)
            cookies = {}
            for c in sb.get_cookies():
                cookies[c["name"]] = c["value"]
            user_agent = sb.execute_script("return navigator.userAgent")
        _save_cookies(cookies, user_agent)
        st.write(f"Done in {time.time()-t0:.1f}s — {len(cookies)} cookies")
    return cookies, user_agent


def _get_client(cookies, user_agent):
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
    return httpx.Client(
        timeout=60,
        follow_redirects=True,
        headers={
            "User-Agent": user_agent,
            "Cookie": cookie_str,
            "Accept": "text/html,application/xhtml+xml",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://www.ddg-pharmfac.net",
            "Referer": VAXIJEN_URL,
        },
    )


def predict(sequence, organism="bacteria", threshold=0.5):
    cookies, user_agent = _load_cookies()
    if cookies:
        client = _get_client(cookies, user_agent)
        result = _do_predict(client, sequence, organism, threshold)
        if result:
            return result
    cookies, user_agent = _launch_browser()
    client = _get_client(cookies, user_agent)
    result = _do_predict(client, sequence, organism, threshold)
    if result:
        return result
    return {"error": "Could not parse result"}


def _do_predict(client, sequence, organism, threshold):
    resp = client.post(VAXIJEN_SCRIPT, data={
        "seq": sequence,
        "Target": organism,
        "threshold": str(threshold),
        "submit": "Submit",
    })
    text = re.sub(r"<[^>]+>", " ", resp.text)
    if "Cloudflare" in resp.text or "Just a moment" in resp.text:
        return None
    m = re.search(
        r"Overall Prediction for the Protective Antigen\s*=\s*(-?[\d.]+)\s*\(.*?(?:Probable\s*)?(ANTIGEN|NON-ANTIGEN)",
        text, re.IGNORECASE,
    )
    if m:
        return {
            "prediction": "ANTIGEN" if "NON" not in m.group(2).upper() else "NON-ANTIGEN",
            "score": float(m.group(1)),
            "organism": organism,
        }
    return None


# --- UI ---
st.title("🧬 VaxiJen Predictor")
st.caption("Protein vaccine candidate prediction via VaxiJen")

col1, col2 = st.columns([2, 1])
with col1:
    sequence = st.text_area("Protein Sequence", height=120, placeholder="Enter amino acid sequence...")
with col2:
    organism = st.selectbox("Organism", TARGETS)
    threshold = st.slider("Threshold", 0.0, 1.0, 0.5, 0.01)

if st.button("Predict", type="primary", disabled=not sequence.strip()):
    with st.spinner("Predicting..."):
        t0 = time.time()
        result = predict(sequence.strip(), organism, threshold)
        elapsed = time.time() - t0

    if "error" in result:
        st.error(result["error"])
    else:
        c1, c2, c3 = st.columns(3)
        c1.metric("Prediction", result["prediction"])
        c2.metric("Score", f"{result['score']:.4f}")
        c3.metric("Time", f"{elapsed:.2f}s")

        color = "red" if result["prediction"] == "ANTIGEN" else "green"
        st.markdown(f"### :{color}[{result['prediction']}]")

# --- API endpoint info ---
st.divider()
st.markdown("""
**API Usage** (call from code):

```bash
curl -X POST http://localhost:8501/predict \\
  -H "Content-Type: application/json" \\
  -d '{"sequence": "GAVLIPFYW", "organism": "bacteria"}'
```
""")
