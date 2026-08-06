#!/usr/bin/env python3
"""
Camoufox Service -- AllerTOP + Immunogenicity (VaxiJen 3.0)
Uses Camoufox (patched Firefox) to bypass Cloudflare Turnstile.
Deployed as a separate Render service.
"""
import re
import time
import uuid
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Camoufox Service")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

ALLERTOP_URL = "https://www.ddg-pharmfac.net/allertop_v2/"
VAXIJEN3_URL = "https://www.ddg-pharmfac.net/vaxijen3/home/"


class SeqRequest(BaseModel):
    sequences: list[str]
    dummy: bool = False


class StepResult(BaseModel):
    sequence: str
    score: float | None = None
    prediction: str | None = None
    similar_protein: str | None = None
    error: str | None = None


def _log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _strip_html(html):
    return re.sub(r"<[^>]+>", " ", html)


def _wait_cf(page, timeout=60):
    for i in range(timeout // 2):
        time.sleep(2)
        title = page.title()
        if "moment" not in title.lower() and title:
            _log(f"  CF passed in {i*2}s: {title}")
            return True
    return False


def _get_csrf(page):
    return page.evaluate("document.querySelector('input[name=csrfmiddlewaretoken]')?.value || ''")


def _js_fetch(page, url, data):
    escaped = {}
    for k, v in data.items():
        escaped[k] = v.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
    pairs = ", ".join(f"'{k}': '{escaped[k]}'" for k in data)
    js = f"""async () => {{
        const params = new URLSearchParams({{{pairs}}});
        const resp = await fetch('{url}', {{
            method: 'POST',
            body: params,
            credentials: 'same-origin',
            redirect: 'follow',
            headers: {{'Content-Type': 'application/x-www-form-urlencoded'}}
        }});
        return {{status: resp.status, url: resp.url, text: await resp.text()}};
    }}"""
    return page.evaluate(js)


def _parse_allertop(text):
    pat = re.compile(r"Classification.*?:\s*(Probable\s+(?:NON-)?ALLERGEN)", re.DOTALL | re.IGNORECASE)
    m = pat.search(text)
    sim_pat = re.compile(r"Most similar protein:\s*(.+?)(?:\n|Classification)", re.DOTALL | re.IGNORECASE)
    sim_m = sim_pat.search(text)
    similar = re.sub(r"\s+", " ", sim_m.group(1).strip()) if sim_m else None
    if m:
        pred = m.group(1).strip()
        label = "NON-ALLERGEN" if "NON-ALLERGEN" in pred.upper() else "ALLERGEN"
        return label, similar
    return None, similar


# ============================================================================
# ALLERTOP -- Camoufox
# ============================================================================

@app.post("/api/allertop", response_model=list[StepResult])
def allertop_predict(req: SeqRequest):
    if req.dummy:
        import random
        return [StepResult(sequence=s,
                          prediction=random.choice(["Probable NON-ALLERGEN", "Probable ALLERGEN"]),
                          similar_protein="sp|DUMMY|MOCK_HUMAN Mock protein OS=Homo sapiens")
                for s in req.sequences]

    _log(f"AllerTOP (Camoufox): {len(req.sequences)} peptides")

    from camoufox.sync_api import Camoufox

    results = []
    with Camoufox(headless=True) as browser:
        page = browser.new_page()

        _log("  Navigating to AllerTOP...")
        page.goto(ALLERTOP_URL)
        if not _wait_cf(page):
            page.close()
            return [StepResult(sequence=s, prediction="Unknown", error="Cloudflare timeout")
                    for s in req.sequences]
        time.sleep(3)
        _log(f"  AllerTOP loaded: {page.title()}")

        for seq in req.sequences:
            try:
                csrf = _get_csrf(page)
                resp = _js_fetch(page, ALLERTOP_URL, {
                    "csrfmiddlewaretoken": csrf,
                    "protein": seq,
                })
                text = re.sub(r"\s+", " ", _strip_html(resp["text"]))
                label, similar = _parse_allertop(text)
                if label:
                    results.append(StepResult(sequence=seq, prediction=label, similar_protein=similar))
                    _log(f"  {seq} -> {label}")
                else:
                    results.append(StepResult(sequence=seq, prediction="Unknown", similar_protein=similar))
            except Exception as e:
                _log(f"  {seq} -> Error: {e}")
                results.append(StepResult(sequence=seq, prediction="Unknown", error=str(e)))

        page.close()

    return results


# ============================================================================
# IMMUNOGENICITY (VaxiJen 3.0) -- Camoufox
# ============================================================================

@app.post("/api/immunogenicity", response_model=list[StepResult])
def immunogenicity_predict(req: SeqRequest):
    if req.dummy:
        import random
        results = []
        for s in req.sequences:
            pred = random.choice(["IMMUNOGEN", "NON-IMMUNOGEN"])
            prob = round(random.uniform(50, 100), 1) if pred == "IMMUNOGEN" else round(random.uniform(0, 49), 1)
            results.append(StepResult(sequence=s, score=prob, prediction=pred))
        return results

    BATCH = 100
    n = len(req.sequences)
    batches = [req.sequences[i:i+BATCH] for i in range(0, n, BATCH)]
    _log(f"Immunogenicity (Camoufox): {n} peptides in {len(batches)} batches of <={BATCH}")

    from camoufox.sync_api import Camoufox

    results = []
    with Camoufox(headless=True) as browser:
        page = browser.new_page()

        _log("  Navigating to VaxiJen 3.0...")
        page.goto(VAXIJEN3_URL)
        if not _wait_cf(page):
            page.close()
            return [StepResult(sequence=s, prediction="Unknown", error="Cloudflare timeout")
                    for s in req.sequences]
        time.sleep(3)

        for batch_idx, batch_seqs in enumerate(batches):
            _log(f"  Batch {batch_idx+1}/{len(batches)}: {len(batch_seqs)} peptides")

            fasta_lines = []
            for i, seq in enumerate(batch_seqs):
                fasta_lines.append(f">seq{i}")
                fasta_lines.append(seq)
            fasta_content = "\n".join(fasta_lines) + "\n"

            tmp_path = f"/tmp/vaxijen_{uuid.uuid4().hex[:8]}.fasta"
            with open(tmp_path, "w") as f:
                f.write(fasta_content)

            try:
                page.goto(VAXIJEN3_URL)
                _wait_cf(page, timeout=30)
                time.sleep(3)

                try:
                    file_input = page.query_selector("input[type='file']")
                    if file_input:
                        file_input.set_input_files(tmp_path)
                        time.sleep(2)
                    else:
                        page.fill("textarea", fasta_content)
                except Exception as e:
                    _log(f"  File upload failed: {e}")
                    page.fill("textarea", fasta_content)

                try:
                    page.select_option("select[name='organism']", label="tumor peptide")
                except Exception:
                    try:
                        page.select_option("select", label="tumor peptide")
                    except Exception:
                        pass

                time.sleep(1)
                page.click("button[type='submit'], input[type='submit']")
                time.sleep(10)

                html = page.content()
                text_clean = re.sub(r"<[^>]+>", " ", html)
                text_clean = re.sub(r"\s+", " ", text_clean)

                batch_found = 0
                for i, seq in enumerate(batch_seqs):
                    pat = re.compile(
                        rf"Results for protein seq{i}:\s*Probable\s+(IMMUNOGEN|NON-IMMUNOGEN)\s+with\s+a\s+probability\s+of\s+([\d.]+)%",
                        re.IGNORECASE
                    )
                    m = pat.search(text_clean)
                    if m:
                        results.append(StepResult(sequence=seq, score=float(m.group(2)), prediction=m.group(1).upper()))
                        batch_found += 1
                    else:
                        results.append(StepResult(sequence=seq, prediction="Unknown"))

                _log(f"  Batch {batch_idx+1} done: {batch_found}/{len(batch_seqs)} found")
            finally:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

        page.close()

    return results


@app.get("/health")
async def health():
    return {"status": "ok", "service": "camoufox"}


import uvicorn
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=10001)
