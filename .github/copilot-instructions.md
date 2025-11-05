# IRAS GST Register API - AI Agent Instructions

## Project Overview

A Streamlit web application that checks Singapore IRAS (Inland Revenue Authority) GST registration status via their official API. Supports both bulk Excel uploads and single UEN lookups with configurable concurrency and sliding-window rate limiting.

## Architecture

- **Single-file app**: All logic in `main.py` (Streamlit UI + async API client + rate limiter)
- **Event loop pattern**: Dedicated asyncio loop running in a background daemon thread via `threading.Thread`
- **Rate limiting**: Sliding-window tracker (100 calls/hour) stored in `st.session_state.rate_ts` deque
- **Concurrency control**: User-configurable (1-20) via `asyncio.Semaphore` within background loop
- **Environment switcher**: UI toggle between Production and Sandbox IRAS endpoints

## Critical API Integration Details

**Endpoints**:

- Production: `https://apiservices.iras.gov.sg/iras/prod/GSTListing/SearchGSTRegistered`
- Sandbox: `https://apisandbox.iras.gov.sg/iras/sb/GSTListing/SearchGSTRegistered`

**Required headers** (see `IRASClient._headers()` in `main.py:109-115`):

- `X-IBM-Client-Id` and `X-IBM-Client-Secret` from environment variables (`IRAS_CLIENT_ID`, `IRAS_CLIENT_SECRET`)
- `Content-Type: application/json` and `Accept: application/json`

**Payload format** (per IRAS spec):

```python
{
    "clientID": "<client_id>",  # optional but sent for consistency
    "regID": "<UEN/NRIC/GST Registration Number>"
}
```

**Response structure**:

- `returnCode`: 10=Success, 20=Warning, 30=Failure
- `data.registrationId`: The GST registration ID (if found)
- Full JSON preserved in output for reference

**Rate limit enforcement**:

- **Sliding window**: `deque(maxlen=100)` tracks timestamps of last 100 calls
- **Dynamic cap**: Batch size limited by `allowed_calls_remaining()` which drops timestamps older than 1 hour
- **Session-scoped**: Rate tracking persists across Streamlit reruns via `st.session_state`
- No artificial sleep delays - relies on natural I/O pacing and user awareness

## Key Workflows

### Running the app

```bash
streamlit run main.py
```

### Environment setup

Set environment variables (uses `os.getenv()` directly - no `.env` file loaded):

```bash
export IRAS_CLIENT_ID=your_client_id
export IRAS_CLIENT_SECRET=your_client_secret
```

### Dependencies

Managed via `uv` (see `pyproject.toml`):

- Python >=3.10
- streamlit for web UI
- aiohttp for async HTTP
- pandas + openpyxl for Excel I/O
- **No nest_asyncio** - uses dedicated thread pattern instead

## Project-Specific Conventions

### Excel Input Format

- **Column A (first column)** contains UEN/NRIC/GST Registration IDs - extracted via `df.iloc[:, 0]`
- No column name validation - position-based extraction
- Other columns preserved in output
- Three new columns appended: `response-status`, `response-registrationId`, `json-response`
- `json-response` is JSON stringified via `json.dumps(body, ensure_ascii=False)`

### Streamlit Event Loop Pattern (CRITICAL)

**Background thread with dedicated loop** (see `ensure_loop_thread()` and `submit_coro()` in `main.py:40-70`):

```python
# One-time setup per session: create loop in daemon thread
st.session_state.loop = asyncio.new_event_loop()

def _runner(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()  # Never exits; daemon thread cleans up on session end

t = threading.Thread(target=_runner, args=(loop,), daemon=True)
t.start()

# Submit coroutine from Streamlit main thread:
future = asyncio.run_coroutine_threadsafe(coro, loop)
result = future.result()  # Blocks until done
```

**Why this pattern:**

- **No nest_asyncio needed** - completely separate loop in its own thread
- Streamlit main thread stays synchronous, calls `.result()` to wait for async work
- Avoids all event loop conflicts - cleanest separation of concerns
- Daemon thread dies when Streamlit session ends (no cleanup needed)

**DO NOT use:**

- `asyncio.run()` - would try to create loop in Streamlit's thread
- `nest_asyncio.apply()` - unnecessary complexity when thread pattern works
- Blocking sleep in main thread - would freeze UI

### Rate Limiting Pattern

Sliding-window tracker using `collections.deque` (see `allowed_calls_remaining()` and `record_call()` in `main.py:76-90`):

```python
# Session-state deque with maxlen=100 (auto-initialized)
def allowed_calls_remaining():
    if "rate_ts" not in st.session_state:
        st.session_state.rate_ts = deque(maxlen=100)
    # Drop timestamps older than 3600s
    while dq and (now - dq[0]) > 3600:
        dq.popleft()
    return 100 - len(dq)

def record_call():
    allowed_calls_remaining()  # Ensures deque exists
    st.session_state.rate_ts.append(time.time())
```

- **Graceful degradation**: Batch size auto-limited by `uens[:can_call]` where `can_call = allowed_calls_remaining()`
- **User visibility**: UI shows `st.metric("Calls remaining")` updated in real-time
- **Session-scoped**: Counter resets when browser tab closes or session expires

### Error Handling Pattern

API client returns tuple of `(http_status, body_dict)` (see `IRASClient.search_gst_registered()` in `main.py:117-133`):

```python
try:
    async with session.post(...) as resp:
        body = await resp.json() if resp.headers.get('content-type', '').startswith('application/json') else {"raw": await resp.text()}
        return resp.status, body
except (aiohttp.ClientError, Exception) as e:
    return 0, {"error": f"{e.__class__.__name__}: {e}"}
```

- Returns `(0, {"error": ...})` on failure instead of raising
- Batch processing continues even if individual calls fail
- Output Excel shows error dict in `json-response` column for failed rows

### Result Processing Helper

Results are processed via `process_batch_results()` helper function (see `main.py:139-167`):

```python
def process_batch_results(df_in, col_a_name, results):
    """Convert API results to DataFrame with response columns."""
    # Build lookup map: first result wins for duplicates
    out_map = {}
    for uen, status, body in results:
        if uen not in out_map:
            out_map[uen] = (status, body)

    # Extract returnCode, registrationId, and full JSON for each row
    df_out = df_in.copy()
    df_out[["response-status", "response-registrationId", "json-response"]] = results_data
    return df_out
```

- Encapsulates complex mapping and extraction logic
- Handles duplicates by keeping first result
- Extracts `returnCode` and `data.registrationId` from IRAS response
- JSON stringifies response for Excel readability

## Data Flow

### Single UEN Mode

1. User enters regID (UEN/NRIC/GST Reg ID) in text input
2. Button click → check `allowed_calls_remaining() > 0`
3. `submit_coro(client.search_gst_registered(regID))` → returns `concurrent.futures.Future`
4. `.result()` blocks until done → `record_call()` to update rate tracker
5. Display `status` and `body` JSON directly in UI

### Bulk Mode (Excel upload)

1. Upload Excel → read with `pd.read_excel(uploaded, engine="openpyxl")`
2. Extract Column A: `uens = df.iloc[:, 0].astype(str).str.strip().tolist()`
3. Cap batch size: `uens = uens[:allowed_calls_remaining()]`
4. `submit_coro(batch_lookup(...))` → returns `Future`
5. `future.result()` blocks, returns `List[Tuple[uen, status, body]]`
6. `process_batch_results(df_in, col_a_name, results)` → creates output DataFrame with three new columns
7. Write to in-memory `BytesIO` via `pd.ExcelWriter` with `openpyxl` engine
8. `st.download_button()` offers timestamped Excel file

## Testing Resources

- Sample UENs: `data/sample_uens.txt`
- API spec: `docs/Check_GST_Register-1.0.7.yaml`
- Example response in `readme.md` shows returnCode=10 format

## What NOT to Change

- `RATE_LIMIT_MAX = 100` and `RATE_LIMIT_WINDOW_SEC = 3600` (IRAS contractual limit)
- Background thread + `run_coroutine_threadsafe()` pattern - only reliable way to avoid Streamlit event loop conflicts
- Single-file architecture (intentionally simple for deployment)
- Error return tuple pattern `(status, body)` - never raise in async workers
- Sliding-window rate limiter - simpler and more accurate than fixed-interval approaches
- `process_batch_results()` helper - keeps result processing logic testable and maintainable

## UI Features

- **Environment selector**: Dropdown to switch between Sandbox and Production endpoints
- **Configurable concurrency**: Number input (1-20) adjusts semaphore limit in `batch_lookup()`
- **Rate limit metric**: Real-time display of remaining calls in current 1-hour window
- **Single UEN quick test**: Separate input for ad-hoc lookups without uploading file (inline async function)
- **Batch upload**: Excel file uploader with automatic Column A extraction
- **In-memory download**: Uses `BytesIO` + `pd.ExcelWriter` to avoid disk I/O
- **Expandable help**: Footer with authentication notes, response mapping, and network requirements

## Code Quality Notes

- **Refactored for clarity**: ~280 lines (reduced from ~330) while maintaining all functionality
- **Helper functions**: `process_batch_results()` extracts complex result processing logic for better testability
- **Simplified client**: `IRASClient` requires session in constructor, no optional parameters
- **Inline rate limiter init**: Deque initialization happens in `allowed_calls_remaining()` on first call
- **Consolidated error handling**: Single catch-all for `ClientError` and generic `Exception`
