import os
import json
import time
import threading
import asyncio
from collections import deque
from typing import Any, Dict, List, Tuple

import streamlit as st
import pandas as pd
import aiohttp

# -----------------------------
# Configuration
# -----------------------------
APP_TITLE = "IRAS • Check GST Register (Batch & Single Lookup)"
DEFAULT_CONCURRENCY = 5  # sensible default; can be tuned from UI
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
    "Uploads an Excel of UENs (col A) and calls IRAS API asynchronously. Adds results back to the file."
)


# -----------------------------
# Utilities: Separate Async Loop in a Thread (no asyncio.run / no nest_asyncio)
# -----------------------------
def ensure_loop_thread():
    """
    Create and start a dedicated asyncio event loop in a background thread once per session.
    Returns the loop and the thread-safe submit function.
    """
    if "loop_thread" not in st.session_state:
        st.session_state.loop = asyncio.new_event_loop()

        def _runner(loop: asyncio.AbstractEventLoop):
            asyncio.set_event_loop(loop)
            loop.run_forever()

        t = threading.Thread(
            target=_runner,
            args=(st.session_state.loop,),
            name="iras-async-loop",
            daemon=True,
        )
        t.start()
        st.session_state.loop_thread = t

    return st.session_state.loop


def submit_coro(coro: asyncio.Future):
    """
    Submit a coroutine to the background loop and return a concurrent.futures.Future
    that can be waited on synchronously in Streamlit code.
    """
    loop = ensure_loop_thread()
    return asyncio.run_coroutine_threadsafe(coro, loop)


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
# IRAS API client (aiohttp)
# -----------------------------
class IRASClient:
    def __init__(
        self,
        base_url: str,
        client_id: str,
        client_secret: str,
        session: aiohttp.ClientSession,
    ):
        self.base_url = base_url
        self.client_id = client_id
        self.client_secret = client_secret
        self.session = session

    def _headers(self) -> Dict[str, str]:
        return {
            "X-IBM-Client-Id": self.client_id,
            "X-IBM-Client-Secret": self.client_secret,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def search_gst_registered(self, reg_id: str) -> Tuple[int, Dict[str, Any]]:
        """
        Calls the IRAS endpoint with payload:
            {"clientID": "<optional>", "regID": "<UEN/NRIC/GST Reg ID>"}
        Returns (http_status, parsed_json or {"error": ...})
        """
        payload = {"clientID": self.client_id, "regID": reg_id}
        try:
            async with self.session.post(
                self.base_url, headers=self._headers(), json=payload
            ) as resp:
                body = (
                    await resp.json()
                    if resp.headers.get("content-type", "").startswith(
                        "application/json"
                    )
                    else {"raw": await resp.text()}
                )
                return resp.status, body
        except (aiohttp.ClientError, Exception) as e:
            return 0, {"error": f"{e.__class__.__name__}: {e}"}


# -----------------------------
# Async batch runner with concurrency + rate awareness
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


async def batch_lookup(
    base_url: str,
    client_id: str,
    client_secret: str,
    uens: List[str],
    max_concurrency: int,
) -> List[Tuple[str, int, Dict[str, Any]]]:
    """Perform async lookups with concurrency limit and rate-limit accounting."""
    sem = asyncio.Semaphore(max_concurrency)
    results: List[Tuple[str, int, Dict[str, Any]]] = []

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=30)
    ) as session:
        client = IRASClient(base_url, client_id, client_secret, session)

        async def _one(uen: str):
            async with sem:
                record_call()
                status, body = await client.search_gst_registered(uen)
                results.append((uen, status, body))

        await asyncio.gather(*[asyncio.create_task(_one(uen)) for uen in uens])

    return results


# -----------------------------
# UI: Environment, secrets, concurrency
# -----------------------------
col_env, col_conc, col_limit = st.columns([2, 1, 1])
with col_env:
    env_choice = st.selectbox(
        "Environment",
        list(URLS.keys()),
        index=0,
        help="Pick Sandbox for testing or Production for live.",
    )
base_url = URLS[env_choice]

with col_conc:
    concurrency = st.number_input(
        "Max concurrency",
        min_value=1,
        max_value=20,
        value=DEFAULT_CONCURRENCY,
        help="Parallel requests to IRAS (I/O-bound). Keep modest to be friendly to upstream.",
    )

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

        async def quick_check():
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            ) as session:
                client = IRASClient(base_url, client_id, client_secret, session)
                return await client.search_gst_registered(single_uen.strip())

        status, body = submit_coro(quick_check()).result()
        record_call()
        st.write("**HTTP Status:**", status)
        st.json(body)

st.divider()

# -----------------------------
# Batch: Excel upload -> async calls -> augmented Excel
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
            f"Submitting {len(uens)} lookups to {env_choice} with concurrency={concurrency} ..."
        )

        # Kick async batch in background loop
        results = submit_coro(
            batch_lookup(base_url, client_id, client_secret, uens, int(concurrency))
        ).result()

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
- **Async model**: Runs on a **separate event loop thread** using `asyncio.run_coroutine_threadsafe` to avoid blocking Streamlit reruns.
- **Excel format**: Column **A** must contain the UEN/NRIC/GST Reg ID; other columns are preserved.
- **Response mapping**:
  - `response-status` ← IRAS `returnCode` (10=Success, 20=Warning, 30=Failure)
  - `response-registrationId` ← `data.registrationId` (if present)
  - `json-response` ← full JSON (stringified)
- **Network rules**: IRAS requires server-to-server, TLS 1.2/1.3, IP whitelisting.
        """
    )
