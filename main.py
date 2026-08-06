#!/usr/bin/env python3
"""
NeoPeptide Backend — FastAPI for Steps 9-14
Browser passes Cloudflare ONCE with SeleniumBase → httpx for fast predictions
"""
import gc
import re
import json
import time
import subprocess
import base64
import csv
import io
import os
import random
import uuid
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx

app = FastAPI(title="NeoPeptide Backend")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

VAXIJEN_FORM = "https://www.ddg-pharmfac.net/vaxijen/VaxiJen/VaxiJen.html"
VAXIJEN_CGI = "https://www.ddg-pharmfac.net/vaxijen/scripts/VaxiJen_scripts/VaxiJen3.pl"
VAXIJEN3_URL = "https://www.ddg-pharmfac.net/vaxijen3/home/"
ALLERTOP_URL = "https://www.ddg-pharmfac.net/allertop_v2/"
TOXINPRED_URL = "https://webs.iiitd.edu.in/raghava/toxinpred3/prediction.php"


class SeqRequest(BaseModel):
    sequences: list[str]
    dummy: bool = False


class StepResult(BaseModel):
    sequence: str
    score: Optional[float] = None
    prediction: Optional[str] = None
    similar_protein: Optional[str] = None
    error: Optional[str] = None


class ImmunogenicityRequest(BaseModel):
    rows: list[dict]


class CosmicCBioRequest(BaseModel):
    gene: str
    cancer_type: str = ""


class ConsolidateRequest(BaseModel):
    gene_name: str
    mhc1_wild_csv: str = ""
    mhc1_mutated_csv: str = ""
    mhc1_final_csv: str = ""
    mhc2_wild_csv: str = ""
    mhc2_mutated_csv: str = ""
    mhc2_final_csv: str = ""
    vaxijen_csv: str = ""
    allertop_csv: str = ""
    toxinpred_csv: str = ""
    protparam_csv: str = ""
    immunogenicity_csv: str = ""


def _log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _cleanup():
    gc.collect()
    for p in ["geckodriver", "firefox", "camoufox"]:
        subprocess.run(["pkill", "-f", p], capture_output=True)
    time.sleep(1)
    gc.collect()


def _strip_html(html):
    return re.sub(r"<[^>]+>", " ", html)


# ═══════════════════════════════════════════════════════════════════════════════
# SeleniumBase browser helper — pass Cloudflare, get cookies
# ═══════════════════════════════════════════════════════════════════════════════

def _sb_launch_cloudflare(url, wait_for="textarea", timeout=60):
    """Launch SeleniumBase, pass Cloudflare, return (sb, page_cookies_dict, user_agent)."""
    from seleniumbase import SB

    _log(f"  [browser] Launching Chrome for {url}...")
    t0 = time.time()
    sb_manager = SB(uc=True, headless2=True)
    sb = sb_manager.__enter__()
    try:
        sb.activate_cdp_mode(url)
        for i in range(30):
            time.sleep(2)
            title = sb.get_title()
            if "moment" not in title.lower() and title:
                _log(f"  [browser] Cloudflare passed in {i*2}s")
                break

        time.sleep(2)
        cookies = {c["name"]: c["value"] for c in sb.get_cookies()}
        user_agent = sb.execute_script("return navigator.userAgent")
        _log(f"  [browser] Got {len(cookies)} cookies in {time.time()-t0:.1f}s")
        return sb_manager, sb, cookies, user_agent
    except Exception:
        sb_manager.__exit__(None, None, None)
        raise


def _sb_get_httpx_client(cookies, user_agent, referer=VAXIJEN_FORM):
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
    return httpx.Client(
        timeout=60, follow_redirects=True,
        headers={
            "User-Agent": user_agent, "Cookie": cookie_str,
            "Accept": "text/html,application/xhtml+xml",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://www.ddg-pharmfac.net",
            "Referer": referer,
        },
    )


# ═══════════════════════════════════════════════════════════════════════════════
# DUMMY MODE
# ═══════════════════════════════════════════════════════════════════════════════

def _dummy_vaxijen(sequences):
    _log("  [DUMMY] Returning mock VaxiJen results")
    return [StepResult(sequence=s, score=round(random.uniform(0.2, 2.5), 4),
                       prediction="ANTIGEN" if round(random.uniform(0.2, 2.5), 4) >= 0.5 else "NON-ANTIGEN")
            for s in sequences]


def _dummy_allertop(sequences):
    _log("  [DUMMY] Returning mock AllerTOP results")
    return [StepResult(sequence=s, prediction=random.choice(["Probable NON-ALLERGEN", "Probable ALLERGEN"]),
                       similar_protein="sp|DUMMY|MOCK_HUMAN Mock protein OS=Homo sapiens")
            for s in sequences]


def _dummy_toxinpred(sequences):
    _log("  [DUMMY] Returning mock ToxinPred results")
    return [StepResult(sequence=s, prediction=random.choice(["Non-Toxin", "Toxin"])) for s in sequences]


def _dummy_immunogenicity(sequences):
    _log("  [DUMMY] Returning mock immunogenicity results")
    results = []
    for s in sequences:
        pred = random.choice(["IMMUNOGEN", "NON-IMMUNOGEN"])
        prob = round(random.uniform(50, 100), 1) if pred == "IMMUNOGEN" else round(random.uniform(0, 49), 1)
        results.append(StepResult(sequence=s, score=prob, prediction=pred))
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 9: VAXIJEN — SeleniumBase + httpx
# ═══════════════════════════════════════════════════════════════════════════════

def _parse_vaxijen_html(html):
    text = _strip_html(html)
    m = re.search(
        r"Overall Prediction for the Protective Antigen\s*=\s*(-?[\d.]+)\s*\(.*?(?:Probable\s*)?(ANTIGEN|NON-ANTIGEN)",
        text, re.IGNORECASE,
    )
    if m:
        pred = "ANTIGEN" if "NON" not in m.group(2).upper() else "NON-ANTIGEN"
        return float(m.group(1)), pred
    return None, None


def _vaxijen_httpx_batch(sequences, cookies, user_agent):
    """Use httpx with cached cookies for fast VaxiJen predictions."""
    results = []
    client = _sb_get_httpx_client(cookies, user_agent)
    for seq in sequences:
        try:
            resp = client.post(VAXIJEN_CGI, data={
                "seq": seq, "Target": "tumour",
                "threshold": "0.5", "submit": "Submit",
            })
            text = _strip_html(resp.text)
            if "Cloudflare" in resp.text or "Just a moment" in resp.text:
                results.append(None)
                continue
            score, pred = _parse_vaxijen_html(resp.text)
            results.append(StepResult(sequence=seq, score=score, prediction=pred))
        except Exception as e:
            results.append(StepResult(sequence=seq, error=str(e)))
        time.sleep(0.3)
    client.close()
    return results


@app.post("/api/vaxijen", response_model=list[StepResult])
def vaxijen_predict(req: SeqRequest):
    if req.dummy:
        return _dummy_vaxijen(req.sequences)

    _log(f"VaxiJen: {len(req.sequences)} peptides")

    sbm, sb, cookies, user_agent = _sb_launch_cloudflare(VAXIJEN_FORM)

    try:
        results = _vaxijen_httpx_batch(req.sequences, cookies, user_agent)
        failed = [r for r in results if r is None]
        if failed:
            _log(f"  Retrying {len(failed)} failed via browser form...")
            for seq in req.sequences:
                if any(r and r.sequence == seq and r.error is None and r.prediction is None for r in results):
                    sb.open(VAXIJEN_FORM)
                    time.sleep(3)
                    try:
                        sb.wait_for_element("textarea", timeout=10)
                    except Exception:
                        pass
                    sb.clear_text("textarea")
                    sb.type("textarea", seq)
                    try:
                        sb.select_option("select", label="Tumour")
                    except Exception:
                        pass
                    sb.click("input[type='submit']")
                    time.sleep(5)
                    html = sb.get_page_source()
                    score, pred = _parse_vaxijen_html(html)
                    idx = next(i for i, r in enumerate(results) if r and r.sequence == seq and r.prediction is None)
                    results[idx] = StepResult(sequence=seq, score=score, prediction=pred)
    finally:
        sbm.__exit__(None, None, None)
        _cleanup()

    return [r for r in results if r is not None]


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 10: ALLERTOP — SeleniumBase browser automation
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/api/allertop", response_model=list[StepResult])
def allertop_predict(req: SeqRequest):
    if req.dummy:
        return _dummy_allertop(req.sequences)

    _log(f"AllerTOP: {len(req.sequences)} peptides")
    results = []

    sbm, sb, cookies, user_agent = _sb_launch_cloudflare(ALLERTOP_URL)

    try:
        _uname = f"neo_{uuid.uuid4().hex[:8]}"
        _pw = "N30Pep!2024xZ"
        _email = f"{_uname}@neopeptide.app"

        # Register
        _log(f"  Registering: {_uname}")
        try:
            sb.open(f"https://www.ddg-pharmfac.net/allertop_v2/accounts/signup/")
            time.sleep(3)
            sb.type("#id_username", _uname)
            sb.type("#id_email", _email)
            sb.type("#id_password1", _pw)
            sb.type("#id_password2", _pw)
            sb.click("button[type='submit'], input[type='submit']")
            time.sleep(3)
        except Exception as e:
            _log(f"  Register failed: {e}")

        # Login
        _log(f"  Logging in...")
        try:
            sb.open("https://www.ddg-pharmfac.net/allertop_v2/accounts/login/?next=/allertop_v2/")
            time.sleep(3)
            sb.type("#id_username", _uname)
            sb.type("#id_password", _pw)
            sb.click("button[type='submit'], input[type='submit']")
            time.sleep(3)
        except Exception as e:
            _log(f"  Login failed: {e}")

        # Navigate to AllerTOP
        sb.open(ALLERTOP_URL)
        time.sleep(3)

        # Submit each peptide
        for seq in req.sequences:
            _log(f"  Submitting: {seq}")
            try:
                sb.open(ALLERTOP_URL)
                time.sleep(3)
                ta = None
                try:
                    ta = sb.find_element("textarea[name='protein']")
                except Exception:
                    try:
                        ta = sb.find_element("textarea")
                    except Exception:
                        pass
                if not ta:
                    results.append(StepResult(sequence=seq, prediction="Unknown", error="No textarea"))
                    continue

                sb.clear_text("textarea")
                sb.type("textarea", seq)
                time.sleep(1)
                sb.click("button[type='submit']")
                time.sleep(10)

                # Wait for classification
                html = sb.get_page_source()
                text = _strip_html(html)
                for i in range(15):
                    if "Classification" in text and ("ALLERGEN" in text or "NON-ALLERGEN" in text):
                        break
                    time.sleep(3)
                    html = sb.get_page_source()
                    text = _strip_html(html)

                pat = re.compile(r"Classification.*?:\s*(Probable\s+(?:NON-)?ALLERGEN)", re.DOTALL | re.IGNORECASE)
                m = pat.search(text)

                sim_pat = re.compile(r"Most similar protein:\s*(.+?)(?:\n|Classification)", re.DOTALL | re.IGNORECASE)
                sim_m = sim_pat.search(text)
                similar_protein = re.sub(r"\s+", " ", sim_m.group(1).strip()) if sim_m else None

                if m:
                    pred = m.group(1).strip().upper()
                    if "NON-ALLERGEN" in pred:
                        results.append(StepResult(sequence=seq, prediction="NON-ALLERGEN", similar_protein=similar_protein))
                    else:
                        results.append(StepResult(sequence=seq, prediction="ALLERGEN", similar_protein=similar_protein))
                    _log(f"  {seq} → {pred}")
                else:
                    results.append(StepResult(sequence=seq, prediction="Unknown", similar_protein=similar_protein))

            except Exception as e:
                _log(f"  {seq} → Error: {e}")
                results.append(StepResult(sequence=seq, prediction="Unknown", error=str(e)))
    finally:
        sbm.__exit__(None, None, None)
        _cleanup()

    return results


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 9b: VAXIJEN 3.0 IMMUNOGENICITY — SeleniumBase browser
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/api/immunogenicity", response_model=list[StepResult])
def immunogenicity_predict(req: SeqRequest):
    if req.dummy:
        return _dummy_immunogenicity(req.sequences)

    BATCH = 100
    n = len(req.sequences)
    batches = [req.sequences[i:i+BATCH] for i in range(0, n, BATCH)]
    _log(f"VaxiJen 3.0 Immunogenicity: {n} peptides in {len(batches)} batches of ≤{BATCH}")
    results = []

    sbm, sb, cookies, user_agent = _sb_launch_cloudflare(VAXIJEN3_URL)

    try:
        _uname = f"neo_{uuid.uuid4().hex[:8]}"
        _pw = "N30Pep!2024xZ"
        _email = f"{_uname}@neopeptide.app"

        # Register
        _log(f"  Registering: {_uname}")
        try:
            sb.open("https://www.ddg-pharmfac.net/vaxijen3/accounts/signup/")
            time.sleep(3)
            sb.type("#id_username", _uname)
            sb.type("#id_email", _email)
            sb.type("#id_password1", _pw)
            sb.type("#id_password2", _pw)
            sb.click("button[type='submit'], input[type='submit']")
            time.sleep(3)
        except Exception as e:
            _log(f"  Register failed: {e}")

        # Login
        _log("  Logging in...")
        try:
            sb.open("https://www.ddg-pharmfac.net/vaxijen3/accounts/login/?next=/vaxijen3/")
            time.sleep(3)
            sb.type("#id_username", _uname)
            sb.type("#id_password", _pw)
            sb.click("button[type='submit'], input[type='submit']")
            time.sleep(3)
        except Exception as e:
            _log(f"  Login failed: {e}")

        sb.open(VAXIJEN3_URL)
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
                sb.open(VAXIJEN3_URL)
                time.sleep(3)

                file_input = sb.find_element("input[type='file']")
                if file_input:
                    sb.send_keys("input[type='file']", tmp_path)
                    time.sleep(2)
                else:
                    sb.type("textarea", fasta_content)

                try:
                    sb.select_option("select[name='organism']", label="tumor peptide")
                except Exception:
                    try:
                        sb.select_option("select", label="tumor peptide")
                    except Exception:
                        pass

                time.sleep(1)
                sb.click("button[type='submit'], input[type='submit']")
                time.sleep(10)

                html = sb.get_page_source()
                text_clean = _strip_html(html)
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

    except Exception as e:
        _log(f"  VaxiJen 3.0 error: {e}")
        found_seqs = {r.sequence for r in results}
        for seq in req.sequences:
            if seq not in found_seqs:
                results.append(StepResult(sequence=seq, prediction="Error", error=str(e)))
    finally:
        sbm.__exit__(None, None, None)
        _cleanup()

    return results


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 11: TOXINPRED — SeleniumBase browser
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/api/toxinpred", response_model=list[StepResult])
def toxinpred_predict(req: SeqRequest):
    if req.dummy:
        return _dummy_toxinpred(req.sequences)

    _log(f"ToxinPred: {len(req.sequences)} peptides")
    results = []

    sbm, sb, cookies, user_agent = _sb_launch_cloudflare(TOXINPRED_URL)

    try:
        fasta = "\n".join(f">seq{i}\n{s}" for i, s in enumerate(req.sequences))

        for attempt in range(3):
            try:
                sb.wait_for_element("textarea", timeout=5)
                break
            except Exception:
                time.sleep(3)

        sb.type("textarea", fasta)

        # Select Hybrid method
        selects = sb.find_elements("select")
        for sel in selects:
            try:
                opts = sel.find_elements("option")
                for opt in opts:
                    txt = (opt.text or "").lower()
                    if "hybrid" in txt:
                        sel.click()
                        opt.click()
                        break
            except Exception:
                pass

        sb.click("input[type='submit']")
        time.sleep(10)

        html = sb.get_page_source()
        text = _strip_html(html)

        for seq in req.sequences:
            if seq in text:
                idx = text.index(seq)
                nearby = text[idx:idx+300]
                if "non-toxin" in nearby.lower():
                    results.append(StepResult(sequence=seq, prediction="Non-Toxin"))
                elif "toxin" in nearby.lower():
                    results.append(StepResult(sequence=seq, prediction="Toxin"))
                else:
                    results.append(StepResult(sequence=seq, prediction="Unknown"))
            else:
                results.append(StepResult(sequence=seq, error="not found"))
    finally:
        sbm.__exit__(None, None, None)
        _cleanup()

    return results


# ═══════════════════════════════════════════════════════════════════════════════
# IMMUNOGENICITY SCORING (no browser — pure computation)
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/api/immunogenicity/score")
async def immunogenicity_score(req: ImmunogenicityRequest):
    def _score(row):
        s = 0.0
        vax = str(row.get("vaxijen_pred", "")).upper()
        if "ANTIGEN" in vax and "NON" not in vax:
            s += 0.4
        tox = str(row.get("toxinpred_pred", "")).lower()
        if "non-toxin" in tox or tox == "non toxin":
            s += 0.3
        aller = str(row.get("allertop_pred", "")).lower()
        if "non-allergen" in aller or "non allergen" in aller:
            s += 0.3
        return round(s, 2)

    def _classify(score):
        if score >= 0.7: return "High"
        if score >= 0.4: return "Medium"
        return "Low"

    rows = req.rows
    for row in rows:
        row["immunogenicity_score"] = _score(row)
        row["immunogenicity_class"] = _classify(row["immunogenicity_score"])
    rows.sort(key=lambda r: r["immunogenicity_score"], reverse=True)
    return {"rows": rows}


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 14: CONSOLIDATION — ZIP with named CSVs
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/api/consolidate")
async def consolidate(req: ConsolidateRequest):
    import zipfile

    g = req.gene_name.upper()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        def _add(name, content):
            if content and content.strip():
                zf.writestr(name, content)

        _add(f"{g}_MHC1_wildtype.csv", req.mhc1_wild_csv)
        _add(f"{g}_MHC1_mutated.csv", req.mhc1_mutated_csv)
        _add(f"{g}_MHC1_neoantigens.csv", req.mhc1_final_csv)
        _add(f"{g}_MHC2_wildtype.csv", req.mhc2_wild_csv)
        _add(f"{g}_MHC2_mutated.csv", req.mhc2_mutated_csv)
        _add(f"{g}_MHC2_neoantigens.csv", req.mhc2_final_csv)
        _add(f"{g}_vaxijen.csv", req.vaxijen_csv)
        _add(f"{g}_allertop.csv", req.allertop_csv)
        _add(f"{g}_toxinpred.csv", req.toxinpred_csv)
        _add(f"{g}_protparam.csv", req.protparam_csv)
        _add(f"{g}_immunogenicity.csv", req.immunogenicity_csv)

    buf.seek(0)
    return {"zip": base64.b64encode(buf.read()).decode()}


# ═══════════════════════════════════════════════════════════════════════════════
# COSMIC + cBioPortal — Mutation Data Source
# ═══════════════════════════════════════════════════════════════════════════════

import requests as _requests

CBIO_STUDIES = {
    "breast": ["brca_tcga_pan_can_atlas_2018", "brca_metabric2012"],
    "lung": ["luad_tcga_pan_can_atlas_2018", "lusc_tcga_pan_can_atlas_2018"],
    "colon": ["coadread_tcga_pan_can_atlas_2018"],
    "rectal": ["coadread_tcga_pan_can_atlas_2018"],
    "prostate": ["prad_tcga_pan_can_atlas_2018"],
    "ovarian": ["ov_tcga_pan_can_atlas_2018"],
    "glioblastoma": ["gbm_tcga_pan_can_atlas_2018"],
    "head and neck": ["hnsc_tcga_pan_can_atlas_2018"],
    "thyroid": ["thca_tcga_pan_can_atlas_2018"],
    "kidney": ["kirp_tcga_pan_can_atlas_2018", "kich_tcga_pan_can_atlas_2018", "kirc_tcga_pan_can_atlas_2018"],
    "endometrial": ["ucec_tcga_pan_can_atlas_2018"],
    "brain": ["lgg_tcga_pan_can_atlas_2018", "gbm_tcga_pan_can_atlas_2018"],
    "pancreas": ["paad_tcga_pan_can_atlas_2018"],
    "melanoma": ["skcm_tcga_pan_can_atlas_2018"],
    "liver": ["lihc_tcga_pan_can_atlas_2018"],
    "stomach": ["stad_tcga_pan_can_atlas_2018"],
    "bladder": ["blca_tcga_pan_can_atlas_2018"],
    "esophageal": ["esca_tcga_pan_can_atlas_2018"],
    "sarcoma": ["sarc_tcga_pan_can_atlas_2018"],
    "adrenal": ["acc_tcga_pan_can_atlas_2018"],
    "uterine": ["ucs_tcga_pan_can_atlas_2018", "ucec_tcga_pan_can_atlas_2018"],
    "cervical": ["cesc_tcga_pan_can_atlas_2018"],
    "mesothelioma": ["meso_tcga_pan_can_atlas_2018"],
    "pheochromocytoma": ["pcpg_tcga_pan_can_atlas_2018"],
    "lymphoma": ["dlbc_tcga_pan_can_atlas_2018"],
    "testicular": ["tgct_tcga_pan_can_atlas_2018"],
    "cholangiocarcinoma": ["chol_tcga_pan_can_atlas_2018"],
    "uveal melanoma": ["uvm_tcga_pan_can_atlas_2018"],
}

GENE_IDS = {
    "TP53": 7157, "PIK3CA": 5290, "KRAS": 3845, "BRAF": 673,
    "EGFR": 1956, "PTEN": 5728, "APC": 324, "RB1": 5925,
    "CDH1": 999, "BCL2": 596, "MYC": 4609, "ERBB2": 2064,
    "FBXW7": 7979, "CDKN2A": 1029, "ARID1A": 8286, "ATM": 472,
    "BRCA1": 672, "BRCA2": 675, "IDH1": 3417, "IDH2": 3418,
    "ALK": 238, "ROS1": 6098, "RET": 5979, "NRAS": 4893,
    "HRAS": 3265, "MAP2K1": 5604, "MAP2K2": 5605, "NF1": 4763,
    "NF2": 4771, "VHL": 7428, "SMAD4": 4089, "STK11": 6794,
    "CTNNB1": 1499, "NOTCH1": 4851, "FGFR3": 2261, "FGFR2": 2263,
    "AKT1": 207, "MTOR": 2475, "TSC1": 7248, "TSC2": 7249,
    "JAK2": 3717, "ABL1": 25, "FLT3": 2322, "KIT": 3815,
    "PDGFRA": 5156, "MET": 4233, "ERBB3": 2065, "ERBB4": 2066,
    "DDR2": 4921, "MAPK1": 5594, "MAPK3": 5595,
    "MAX": 4149, "SMARCB1": 6598, "SMARCA4": 6597, "ARID1B": 57492,
    "SETD2": 29072, "KMT2A": 4297, "KMT2D": 79812, "NSD1": 64324,
}


@app.post("/api/cbioportal")
async def cbioportal_query(req: CosmicCBioRequest):
    gene = req.gene.upper().strip()
    cancer = req.cancer_type.lower().strip()

    entrez = GENE_IDS.get(gene)
    if not entrez:
        return {"error": f"Gene {gene} not in database."}

    studies = CBIO_STUDIES.get(cancer)
    if not studies:
        for key, vals in CBIO_STUDIES.items():
            if cancer in key or key in cancer:
                studies = vals
                break
    if not studies:
        return {"error": f"Cancer type '{cancer}' not found."}

    _log(f"cBioPortal: {gene} (entrez={entrez}) in {cancer} ({len(studies)} studies)")

    try:
        body = {
            "molecularProfileIds": [f"{s}_mutations" for s in studies],
            "entrezGeneIds": [entrez],
        }
        r = _requests.post(
            "https://www.cbioportal.org/api/mutations/fetch",
            json=body, timeout=60,
            headers={"Content-Type": "application/json"},
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return {"error": f"cBioPortal API error: {str(e)}"}

    if not data:
        return {"error": f"No mutations found for {gene} in {cancer}"}

    rows = []
    seen = set()
    for m in data:
        sample = m.get("sampleId", "")
        pc = m.get("proteinChange", "")
        aa = f"p.{pc}" if pc and not pc.startswith("p.") else pc
        key = (sample, aa)
        if key not in seen:
            seen.add(key)
            rows.append({"Gene Name": gene, "Sample Name": sample, "CDS Mutation": "", "AA Mutation": aa})

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=["Gene Name", "Sample Name", "CDS Mutation", "AA Mutation"])
    writer.writeheader()
    writer.writerows(rows)

    return {
        "csv": buf.getvalue(), "total": len(rows),
        "samples": len(set(r["Sample Name"] for r in rows)),
        "source": "cBioPortal", "gene": gene, "cancer_type": cancer,
    }


@app.post("/api/cosmic/upload")
async def cosmic_upload(file_content: str, gene: str = ""):
    _log(f"COSMIC upload: gene={gene}")
    reader = csv.DictReader(io.StringIO(file_content))
    rows = list(reader)
    if not rows:
        return {"error": "Empty file or invalid CSV"}

    if gene:
        gene = gene.upper().strip()
        gene_col = next((col for col in rows[0] if "gene" in col.lower()), None)
        if gene_col:
            rows = [r for r in rows if str(r.get(gene_col, "")).upper() == gene]

    col_map = {}
    for col in rows[0]:
        cl = col.lower().strip()
        if "gene" in cl: col_map[col] = "Gene Name"
        elif "sample" in cl: col_map[col] = "Sample Name"
        elif "cds" in cl or ("mutation" in cl and "aa" not in cl and "protein" not in cl): col_map[col] = "CDS Mutation"
        elif "aa" in cl or "protein" in cl: col_map[col] = "AA Mutation"

    out_rows = [{"Gene Name": r.get(col_map.get(k, ""), "") for k in ["Gene Name", "Sample Name", "CDS Mutation", "AA Mutation"]}
                for r in rows]

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=["Gene Name", "Sample Name", "CDS Mutation", "AA Mutation"])
    writer.writeheader()
    writer.writerows(out_rows)

    return {"csv": buf.getvalue(), "total": len(out_rows),
            "samples": len(set(r.get("Sample Name", "") for r in out_rows)),
            "source": "COSMIC_manual", "gene": gene or "(all)"}


# ═══════════════════════════════════════════════════════════════════════════════
# POPULATION COVERAGE
# ═══════════════════════════════════════════════════════════════════════════════

class PopCoverageRequest(BaseModel):
    epitope_alleles: list[dict]
    population: list[str] = ["World"]
    mhc_class: str = "combined"


@app.post("/api/population_coverage")
async def population_coverage(req: PopCoverageRequest):
    import tempfile, shutil

    _log(f"Population Coverage: {len(req.epitope_alleles)} epitopes")

    input_lines = [f"{item.get('epitope', '')}\t{item.get('alleles', '')}"
                   for item in req.epitope_alleles if item.get("epitope") and item.get("alleles")]
    if not input_lines:
        return {"error": "No valid epitope-allele pairs"}

    tmpdir = tempfile.mkdtemp(prefix="popcov_")
    input_file = os.path.join(tmpdir, "input.txt")
    output_dir = os.path.join(tmpdir, "plots")
    os.makedirs(output_dir)
    with open(input_file, "w") as f:
        f.write("\n".join(input_lines) + "\n")

    try:
        script = os.path.join(os.path.dirname(__file__), "population_coverage", "calculate_population_coverage.py")
        cmd = ["python3", script, "-p", ",".join(req.population), "-c", req.mhc_class, "-f", input_file, "--plot", output_dir]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

        if proc.returncode != 0 and not proc.stdout:
            return {"error": f"Failed: {proc.stderr[:500]}"}

        lines = proc.stdout.strip().split("\n")
        calc_summary, chart_data = [], []
        current_table = None
        for line in lines:
            if "coverage\taverage_hit" in line: current_table = "calc"; continue
            elif "epitope_hits\tpercent_individuals" in line: current_table = "chart"; continue
            if "\t" in line and current_table:
                parts = line.split("\t")
                if len(parts) >= 4 and parts[0] not in ("average", "standard_deviation", "population/area"):
                    if current_table == "calc":
                        calc_summary.append({"population": parts[0], "coverage": parts[1], "average_hit": parts[2], "pc90": parts[3]})
                    elif current_table == "chart":
                        chart_data.append({"epitope_hits": parts[1], "percent_individuals": parts[2], "cumulative_coverage": parts[3]})

        plots = []
        if os.path.isdir(output_dir):
            for fname in sorted(os.listdir(output_dir)):
                if fname.endswith(".png"):
                    with open(os.path.join(output_dir, fname), "rb") as pf:
                        plots.append({"name": fname, "data": base64.b64encode(pf.read()).decode()})

        return {"summary": calc_summary, "chart": chart_data, "plots": plots, "stdout": proc.stdout[:3000]}
    except subprocess.TimeoutExpired:
        return {"error": "Timed out (120s)"}
    except Exception as e:
        return {"error": str(e)}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@app.get("/health")
async def health():
    return {"status": "ok"}


import uvicorn
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
