from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from typing import Optional
import httpx
import re
import json
import time
import os
import threading

app = FastAPI(
    title="VaxiJen API",
    description="Protein vaccine candidate prediction using VaxiJen",
    version="1.0.0",
)

VAXIJEN_URL = "https://www.ddg-pharmfac.net/vaxijen/VaxiJen/VaxiJen.html"
VAXIJEN_SCRIPT = "https://www.ddg-pharmfac.net/vaxijen/scripts/VaxiJen_scripts/VaxiJen3.pl"
TARGETS = ["bacteria", "virus", "tumour", "parasite", "fungal"]
COOKIE_FILE = os.path.join(os.path.dirname(__file__), ".vaxijen_cookies.json")

_client = None
_last_request = 0
_lock = threading.Lock()


class PredictRequest(BaseModel):
    sequence: str = Field(..., min_length=1, description="Protein sequence")
    organism: str = Field("bacteria", description="Target organism")
    threshold: float = Field(0.5, ge=0, le=1, description="Prediction threshold")


class PredictResponse(BaseModel):
    prediction: str
    score: float
    organism: str


class BatchRequest(BaseModel):
    sequences: list[dict] = Field(
        ...,
        description="List of {sequence, organism, threshold?} objects",
    )


class BatchResponse(BaseModel):
    results: list[dict]
    total_time: float


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

    print("[browser] Launching Chrome...")
    t0 = time.time()
    with SB(uc=True, headless2=True) as sb:
        sb.activate_cdp_mode(VAXIJEN_URL)
        for i in range(30):
            time.sleep(2)
            title = sb.get_title()
            print(f"[browser] [{i*2}s] Title: {title}")
            if "moment" not in title.lower() and title:
                print(f"[browser] Cloudflare resolved in {i*2}s")
                break
        time.sleep(2)
        cookies = {}
        for c in sb.get_cookies():
            cookies[c["name"]] = c["value"]
        user_agent = sb.execute_script("return navigator.userAgent")
    print(f"[browser] Got cookies in {time.time()-t0:.1f}s")
    _save_cookies(cookies, user_agent)
    return cookies, user_agent


def _get_client(cookies, user_agent):
    global _client
    if _client:
        _client.close()
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
    _client = httpx.Client(
        timeout=60,
        follow_redirects=True,
        headers={
            "User-Agent": user_agent,
            "Cookie": cookie_str,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://www.ddg-pharmfac.net",
            "Referer": VAXIJEN_URL,
        },
    )
    return _client


def _httpx_predict(client, sequence, organism, threshold):
    resp = client.post(
        VAXIJEN_SCRIPT,
        data={
            "seq": sequence,
            "Target": organism,
            "threshold": str(threshold),
            "submit": "Submit",
        },
    )
    text = re.sub(r"<[^>]+>", " ", resp.text)
    if "Cloudflare" in resp.text or "Just a moment" in resp.text:
        return None
    m = re.search(
        r"Overall Prediction for the Protective Antigen\s*=\s*(-?[\d.]+)\s*\(.*?(?:Probable\s*)?(ANTIGEN|NON-ANTIGEN)",
        text,
        re.IGNORECASE,
    )
    if m:
        return {
            "prediction": "ANTIGEN" if "NON" not in m.group(2).upper() else "NON-ANTIGEN",
            "score": float(m.group(1)),
            "organism": organism,
        }
    return None


def _predict(sequence, organism="bacteria", threshold=0.5):
    global _last_request

    with _lock:
        elapsed = time.time() - _last_request
        if elapsed < 1:
            time.sleep(1 - elapsed)

        cookies, user_agent = _load_cookies()
        if cookies:
            client = _get_client(cookies, user_agent)
            result = _httpx_predict(client, sequence, organism, threshold)
            _last_request = time.time()
            if result:
                return result

        cookies, user_agent = _launch_browser()
        client = _get_client(cookies, user_agent)
        result = _httpx_predict(client, sequence, organism, threshold)
        _last_request = time.time()
        if result:
            return result

        return {"error": "Could not parse result"}


@app.get("/", response_class=HTMLResponse)
def index():
    return """<!DOCTYPE html>
<html>
<head><title>VaxiJen API</title></head>
<body>
<h1>VaxiJen API</h1>
<p>Protein vaccine candidate prediction</p>
<ul>
<li><a href="/docs">Swagger UI (interactive)</a></li>
<li><a href="/redoc">ReDoc (documentation)</a></li>
</ul>
<h3>Example</h3>
<pre>curl -X POST http://localhost:8000/predict \\
  -H "Content-Type: application/json" \\
  -d '{"sequence": "GAVLIPFYW", "organism": "bacteria"}'</pre>
</body>
</html>"""


@app.get("/targets")
def get_targets():
    return {"targets": TARGETS}


@app.post("/predict", response_model=PredictResponse)
def predict(req: PredictRequest):
    if req.organism not in TARGETS:
        raise HTTPException(400, f"Invalid organism. Choose from: {TARGETS}")
    result = _predict(req.sequence, req.organism, req.threshold)
    if "error" in result:
        raise HTTPException(500, result["error"])
    return result


@app.post("/predict/batch", response_model=BatchResponse)
def predict_batch(req: BatchRequest):
    t0 = time.time()
    results = []
    for item in req.sequences:
        seq = item.get("sequence", "")
        org = item.get("organism", "bacteria")
        thr = item.get("threshold", 0.5)
        if org not in TARGETS:
            results.append({"error": f"Invalid organism: {org}"})
            continue
        result = _predict(seq, org, thr)
        results.append(result)
    return BatchResponse(results=results, total_time=round(time.time() - t0, 2))
