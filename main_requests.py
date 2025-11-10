import os
import json
import time
from collections import deque
from typing import Any, Dict, List, Tuple

import streamlit as st
import pandas as pd
import requests

# -----------------------------
# Configuration
# -----------------------------
APP_TITLE = "IRAS • Check GST Register (Batch & Single Lookup)"
RATE_LIMIT_MAX = 100  # 100 calls / hour
RATE_LIMIT_WINDOW_SEC = 3600  # 1 hour (3600s)
ENV_VAR_CLIENT_ID = "IRAS_CLIENT_ID"
ENV_VAR_CLIENT_SECRET = "IRAS_CLIENT_SECRET"

URLS = {
    "Production": "https://apiservices.iras.gov.sg/iras/prod/GSTListing/SearchGSTRegistered",
    "Sandbox": "https://apisandbox.iras.gov.sg/iras/sb/GSTListing/SearchGSTRegistered",
}

# -----------------------------
# Streamlit page config
# -----------------------------
st.set_page_config(page_title=APP_TITLE, layout="wide")
st.title(APP_TITLE)
st.caption(
    "Uploads an Excel of UENs (col A) and calls IRAS API synchronously. Adds results back to the file."
)


# -----------------------------
# Rate limiting: simple sliding-window limiter
# -----------------------------
def allowed_calls_remaining(now: float = None) -> int:
    """Return how many calls are still allowed in the current 1-hour window."""
    if "rate_ts" not in st.session_state:
        st.session_state.rate_ts = deque(maxlen=RATE_LIMIT_MAX)

    if now is None:
        now = time.time()
    dq = st.session_state.rate_ts
    while dq and (now - dq[0]) > RATE_LIMIT_WINDOW_SEC:
        dq.popleft()
    return RATE_LIMIT_MAX - len(dq)


def record_call(now: float = None):
    allowed_calls_remaining()  # Ensures deque exists
    st.session_state.rate_ts.append(now or time.time())


# -----------------------------
# IRAS API client (requests)
# -----------------------------
class IRASClient:
    def __init__(self, base_url: str, client_id: str, client_secret: str):
        self.base_url = base_url
        self.client_id = client_id
        self.client_secret = client_secret
        self.session = requests.Session()
        self.session.headers.update(self._headers())

    def _headers(self) -> Dict[str, str]:
        return {
            "X-IBM-Client-Id": self.client_id,
            "X-IBM-Client-Secret": self.client_secret,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def search_gst_registered(self, reg_id: str) -> Tuple[int, Dict[str, Any]]:
        """
        Calls the IRAS endpoint with payload:
            {"clientID": "<optional>", "regID": "<UEN/NRIC/GST Reg ID>"}
        Returns (http_status, parsed_json or {"error": ...})
        """
        payload = {"clientID": self.client_id, "regID": reg_id}
        try:
            resp = self.session.post(self.base_url, json=payload, timeout=30)
            body = (
                resp.json()
                if resp.headers.get("content-type", "").startswith("application/json")
                else {"raw": resp.text}
            )
            return resp.status_code, body
        except requests.exceptions.RequestException as e:
            return 0, {"error": f"{e.__class__.__name__}: {e}"}
        except Exception as e:
            return 0, {"error": f"{e.__class__.__name__}: {e}"}


# -----------------------------
# Batch processor with rate awareness
# -----------------------------
def process_batch_results(
    df_in: pd.DataFrame, col_a_name: str, results: List[Tuple[str, int, Dict[str, Any]]]
) -> pd.DataFrame:
    """Convert API results to DataFrame with response columns."""
    # Build lookup map: first result wins for duplicates
    out_map = {}
    for uen, status, body in results:
        if uen not in out_map:
            out_map[uen] = (status, body)

    def extract_row(uen: str):
        if uen not in out_map:
            return "", "", ""
        status, body = out_map[uen]
        return_code = body.get("returnCode", "") if isinstance(body, dict) else ""
        reg_id = (
            body.get("data", {}).get("registrationId", "")
            if isinstance(body, dict)
            else ""
        )
        try:
            json_str = json.dumps(body, ensure_ascii=False)
        except Exception:
            json_str = str(body)
        return return_code, reg_id, json_str

    df_out = df_in.copy()
    results_data = [
        extract_row(uen) for uen in df_in[col_a_name].astype(str).str.strip()
    ]
    df_out[["response-status", "response-registrationId", "json-response"]] = (
        results_data
    )
    return df_out


def batch_lookup(
    client: IRASClient, uens: List[str], progress_callback=None
) -> List[Tuple[str, int, Dict[str, Any]]]:
    """Perform lookups sequentially with rate-limit accounting."""
    results: List[Tuple[str, int, Dict[str, Any]]] = []

    for idx, uen in enumerate(uens):
        record_call()
        status, body = client.search_gst_registered(uen)
        results.append((uen, status, body))

        if progress_callback:
            progress_callback((idx + 1) / len(uens))

    return results


# -----------------------------
# UI: Environment, secrets, rate limit
# -----------------------------
col_env, col_limit = st.columns([3, 1])
with col_env:
    env_choice = st.selectbox(
        "Environment",
        list(URLS.keys()),
        index=0,
        help="Pick Sandbox for testing or Production for live.",
    )
base_url = URLS[env_choice]

with col_limit:
    remaining = allowed_calls_remaining()
    st.metric(
        "Calls remaining (last 60 mins)",
        remaining,
        help="Simple sliding-window tracker in this session.",
    )

client_id = os.getenv(ENV_VAR_CLIENT_ID, "")
client_secret = os.getenv(ENV_VAR_CLIENT_SECRET, "")

missing_env = []
if not client_id:
    missing_env.append(ENV_VAR_CLIENT_ID)
if not client_secret:
    missing_env.append(ENV_VAR_CLIENT_SECRET)

if missing_env:
    st.warning(
        f"Missing env: {', '.join(missing_env)}. Set them before calling the API."
    )

st.divider()

# -----------------------------
# Single UEN quick test
# -----------------------------
st.subheader("Quick Test: Single UEN/NRIC/GST Reg ID")
qcol1, qcol2 = st.columns([2, 1])
with qcol1:
    single_uen = st.text_input("regID", placeholder="e.g., 200312345A")
with qcol2:
    run_single = st.button(
        "Check Now",
        type="primary",
        disabled=bool(missing_env) or not single_uen.strip(),
    )

if run_single and single_uen.strip():
    if allowed_calls_remaining() <= 0:
        st.error("Rate limit reached (100/hour). Try again later.")
    else:
        client = IRASClient(base_url, client_id, client_secret)
        status, body = client.search_gst_registered(single_uen.strip())
        record_call()
        st.write("**HTTP Status:**", status)
        st.json(body)

st.divider()

# -----------------------------
# Batch: Excel upload -> API calls -> augmented Excel
# -----------------------------
st.subheader("Batch Upload (Excel)")
uploaded = st.file_uploader(
    "Upload Excel (.xlsx). Column A must contain UEN/NRIC/GST Reg ID. Other columns are preserved.",
    type=["xlsx"],
)

run_batch = st.button(
    "Process File", type="primary", disabled=bool(missing_env) or uploaded is None
)

if uploaded is not None:
    try:
        df_in = pd.read_excel(uploaded, engine="openpyxl")
        st.caption(f"Detected {len(df_in)} rows.")
        st.dataframe(df_in.head(20), use_container_width=True)
    except Exception as e:
        st.error(f"Failed to read Excel: {e}")
        df_in = None

if run_batch and uploaded is not None and df_in is not None:
    # Validate Column A exists
    if df_in.shape[1] == 0:
        st.error("The Excel has no columns. Column A must contain UENs.")
    else:
        # Column A (index 0)
        col_a_name = df_in.columns[0]
        uens_raw = df_in.iloc[:, 0].astype(str).str.strip().tolist()
        uens = [u for u in uens_raw if u]  # non-empty

        # Rate-limit budgeting
        can_call = allowed_calls_remaining()
        if can_call <= 0:
            st.error("Rate limit reached (100/hour). Try again later.")
            st.stop()

        if len(uens) > can_call:
            st.warning(
                f"Only processing first {can_call} rows due to the 100/hour API limit."
            )
            uens = uens[:can_call]

        st.info(
            f"Submitting {len(uens)} lookups to {env_choice} (sequential processing)..."
        )

        # Progress bar
        progress_bar = st.progress(0)
        status_text = st.empty()

        # Create client and process batch
        client = IRASClient(base_url, client_id, client_secret)

        def update_progress(pct: float):
            progress_bar.progress(pct)
            status_text.text(f"Processing... {int(pct * 100)}%")

        results = batch_lookup(client, uens, progress_callback=update_progress)

        progress_bar.empty()
        status_text.empty()

        # Process results into output DataFrame
        df_out = process_batch_results(df_in, col_a_name, results)

        st.success("Batch completed.")
        st.dataframe(df_out.head(20), use_container_width=True)

        # Offer download
        try:
            from io import BytesIO

            bio = BytesIO()
            with pd.ExcelWriter(bio, engine="openpyxl") as writer:
                df_out.to_excel(writer, index=False, sheet_name="Results")
            bio.seek(0)
            st.download_button(
                "Download Results (.xlsx)",
                data=bio,
                file_name=f"iras_checkgst_results_{int(time.time())}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        except Exception as e:
            st.error(f"Failed to produce Excel: {e}")

# -----------------------------
# Footer / Help
# -----------------------------
with st.expander("Notes & Tips"):
    st.markdown(
        """
- **Authentication**: Set environment variables before running Streamlit:
  - `IRAS_CLIENT_ID`
  - `IRAS_CLIENT_SECRET`
- **API Endpoint**: The app lets you switch between **Sandbox** and **Production**.
- **Rate limit**: Enforced as **100 calls/hour** with a simple sliding window. The app will cap batch size accordingly.
- **Processing model**: Uses **synchronous requests** (sequential, not parallel). Simpler but slower than async version.
- **Excel format**: Column **A** must contain the UEN/NRIC/GST Reg ID; other columns are preserved.
- **Response mapping**:
  - `response-status` ← IRAS `returnCode` (10=Success, 20=Warning, 30=Failure)
  - `response-registrationId` ← `data.registrationId` (if present)
  - `json-response` ← full JSON (stringified)
- **Network rules**: IRAS requires server-to-server, TLS 1.2/1.3, IP whitelisting.
        """
    )
