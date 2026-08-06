#!/usr/bin/env python3
"""
Camoufox Service -- AllerTOP + Immunogenicity (VaxiJen 3.0)
Uses Camoufox (patched Firefox) to bypass Cloudflare Turnstile.
"""
import re
import time
import uuid
import os
import random
import string

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Camoufox Service")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

os.environ.setdefault("DISPLAY", ":99")

ALLERTOP_URL = "https://www.ddg-pharmfac.net/allertop_v2/"
VAXIJEN3_URL = "https://www.ddg-pharmfac.net/vaxijen3/home/"
VAXIJEN3_BASE = "https://www.ddg-pharmfac.net/vaxijen3/"


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
        try:
            title = page.title()
        except Exception:
            continue
        try:
            has_form = page.evaluate("!!document.querySelector('textarea[name=protein]')")
        except Exception:
            continue
        if has_form:
            _log(f"  Form ready in {i*2}s: {title}")
            return True
        if "moment" not in title.lower() and title and i > 3:
            _log(f"  CF title changed in {i*2}s: {title}")
    return False


def _wait_for_element(page, selector, timeout=60):
    for i in range(timeout // 2):
        time.sleep(2)
        try:
            if page.evaluate(f'!!document.querySelector("{selector}")'):
                return True
        except Exception:
            continue
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


def _js_fetch_with_args(page, url, arg_names, arg_values):
    js = f"""async (args) => {{
        const params = new URLSearchParams();
        {chr(10).join(f"        params.append('{name}', args[{i}]);" for i, name in enumerate(arg_names))}
        const resp = await fetch('{url}', {{
            method: 'POST',
            body: params,
            credentials: 'same-origin',
            redirect: 'follow',
            headers: {{'Content-Type': 'application/x-www-form-urlencoded'}}
        }});
        const text = await resp.text();
        return {{status: resp.status, url: resp.url, text: text}};
    }}"""
    return page.evaluate(js, arg_values)


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


def _allertop_register_and_login(page):
    uname = "camoufoxbot" + "".join(random.choices(string.digits, k=6))
    email = f"{uname}@camoufox.bot"
    pw = "C0mpl3x!P@ssw0rd#2026"
    _log(f"  Registering AllerTOP account: {uname}")

    page.goto(ALLERTOP_URL + "accounts/signup/", timeout=60000)
    if not _wait_for_element(page, "input[name=username]"):
        _log("  Signup form not found")
        return False

    csrf = _get_csrf(page)
    res = _js_fetch_with_args(page, "/allertop_v2/accounts/signup/",
        ["csrfmiddlewaretoken", "username", "email", "password1", "password2"],
        [csrf, uname, email, pw, pw])
    if "signup" in res["url"]:
        _log(f"  Registration failed: {res['url']}")
        return False
    _log(f"  Registered successfully")

    page.goto(ALLERTOP_URL + "accounts/login/", timeout=60000)
    if not _wait_for_element(page, "input[name=username]"):
        _log("  Login form not found")
        return False

    csrf2 = _get_csrf(page)
    res2 = _js_fetch_with_args(page, "/allertop_v2/accounts/login/",
        ["csrfmiddlewaretoken", "username", "email", "password"],
        [csrf2, uname, email, pw])
    if "login" in res2["url"]:
        _log(f"  Login failed")
        return False
    _log(f"  Logged in successfully")
    return True


def _vaxijen3_register_and_login(page):
    uname = "cvxbot" + "".join(random.choices(string.digits, k=6))
    email = f"{uname}@camoufox.bot"
    pw = "Vx3n!Bot2026#Sec"
    _log(f"  Registering VaxiJen 3.0 account: {uname}")

    page.goto(VAXIJEN3_BASE + "accounts/signup/", timeout=60000)
    _wait_cf(page, timeout=30)
    time.sleep(2)

    if not _wait_for_element(page, "input[name=username]"):
        _log("  VaxiJen 3.0 signup form not found, trying page content...")
        html = page.content()
        if "signup" in html.lower() or "register" in html.lower():
            _log("  Page has signup content but selector didn't match")
        else:
            _log(f"  Page title: {page.title()}")
        return False

    csrf = _get_csrf(page)
    res = _js_fetch_with_args(page, "/vaxijen3/accounts/signup/",
        ["csrfmiddlewaretoken", "username", "email", "password1", "password2"],
        [csrf, uname, email, pw, pw])
    _log(f"  Signup response URL: {res['url']}")
    if "signup" in res["url"] and "login" not in res["url"]:
        _log(f"  VaxiJen 3.0 registration may have failed")
        return False
    _log(f"  Registered successfully")

    page.goto(VAXIJEN3_BASE + "accounts/login/", timeout=60000)
    _wait_cf(page, timeout=30)
    time.sleep(2)

    if not _wait_for_element(page, "input[name=username]"):
        _log("  VaxiJen 3.0 login form not found")
        return False

    csrf2 = _get_csrf(page)
    res2 = _js_fetch_with_args(page, "/vaxijen3/accounts/login/",
        ["csrfmiddlewaretoken", "username", "email", "password"],
        [csrf2, uname, email, pw])
    _log(f"  Login response URL: {res2['url']}")
    if "login" in res2["url"]:
        _log(f"  VaxiJen 3.0 login failed")
        return False
    _log(f"  Logged in to VaxiJen 3.0 successfully")
    return True


# ============================================================================
# ALLERTOP -- Camoufox
# ============================================================================

@app.post("/api/allertop", response_model=list[StepResult])
def allertop_predict(req: SeqRequest):
    if req.dummy:
        return [StepResult(sequence=s,
                          prediction=random.choice(["Probable NON-ALLERGEN", "Probable ALLERGEN"]),
                          similar_protein="sp|DUMMY|MOCK_HUMAN Mock protein OS=Homo sapiens")
                for s in req.sequences]

    _log(f"AllerTOP (Camoufox): {len(req.sequences)} peptides")

    from camoufox.sync_api import Camoufox

    results = []
    with Camoufox(headless=False) as browser:
        page = browser.new_page()

        _log("  Navigating to AllerTOP...")
        page.goto(ALLERTOP_URL, timeout=60000)
        if not _wait_cf(page):
            page.close()
            return [StepResult(sequence=s, prediction="Unknown", error="Cloudflare timeout")
                    for s in req.sequences]
        time.sleep(3)
        _log(f"  AllerTOP loaded: {page.title()}")

        if not _allertop_register_and_login(page):
            page.close()
            return [StepResult(sequence=s, prediction="Unknown", error="Registration/login failed")
                    for s in req.sequences]

        page.goto(ALLERTOP_URL, timeout=60000)
        _wait_cf(page)
        time.sleep(3)

        for seq in req.sequences:
            try:
                csrf = _get_csrf(page)
                result = _js_fetch_with_args(page, ALLERTOP_URL,
                    ["csrfmiddlewaretoken", "protein"],
                    [csrf, seq])
                text = re.sub(r"\s+", " ", _strip_html(result["text"]))
                label, similar = _parse_allertop(text)
                if label:
                    results.append(StepResult(sequence=seq, prediction=label, similar_protein=similar))
                    _log(f"  {seq} -> {label}")
                else:
                    results.append(StepResult(sequence=seq, prediction="Unknown", similar_protein=similar))
                    _log(f"  {seq} -> Unknown (url: {result['url']}, len: {len(result['text'])})")
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
    with Camoufox(headless=False) as browser:
        page = browser.new_page()

        _log("  Navigating to VaxiJen 3.0...")
        page.goto(VAXIJEN3_URL, timeout=60000)
        if not _wait_cf(page):
            page.close()
            return [StepResult(sequence=s, prediction="Unknown", error="Cloudflare timeout")
                    for s in req.sequences]
        time.sleep(3)

        if not _vaxijen3_register_and_login(page):
            _log("  VaxiJen 3.0 registration/login failed, trying without login...")
        else:
            _log("  Logged in to VaxiJen 3.0, navigating to home...")
            page.goto(VAXIJEN3_URL, timeout=60000)
            _wait_cf(page, timeout=30)
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
                page.goto(VAXIJEN3_URL, timeout=60000)
                _wait_cf(page, timeout=30)
                time.sleep(3)

                _log(f"  Page title: {page.title()}")

                uploaded = False
                try:
                    file_input = page.query_selector("input[type='file']")
                    if file_input:
                        file_input.set_input_files(tmp_path)
                        time.sleep(3)
                        uploaded = True
                        _log("  FASTA file uploaded via input[type=file]")
                except Exception as e:
                    _log(f"  File upload error: {e}")

                if not uploaded:
                    try:
                        page.fill("textarea", fasta_content)
                        _log("  FASTA pasted into textarea")
                    except Exception as e:
                        _log(f"  Textarea fill error: {e}")

                try:
                    page.select_option("select[name='organism']", label="tumor peptide")
                except Exception:
                    try:
                        page.select_option("select", label="tumor peptide")
                    except Exception:
                        _log("  Could not select tumor peptide organism")

                time.sleep(1)

                try:
                    page.click("button[type='submit'], input[type='submit']")
                except Exception:
                    page.evaluate("document.querySelector('form').submit()")
                time.sleep(15)

                html = page.content()
                text_clean = re.sub(r"<[^>]+>", " ", html)
                text_clean = re.sub(r"\s+", " ", text_clean)
                _log(f"  Form submitted via browser, page len: {len(text_clean)}")

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

                if batch_found == 0 and len(text_clean) > 0:
                    snippet = text_clean[:500] if len(text_clean) > 500 else text_clean
                    _log(f"  DEBUG response snippet: {snippet}")
                    if "IMMUNOGEN" in text_clean.upper():
                        _log(f"  Found IMMUNOGEN in text but regex didn't match")

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
