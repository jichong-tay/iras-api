"""
IRAS GST Register Checker with Redis Background Task Queue
Streamlit web app with Redis-based async job processing
"""

import os
import json
import time
import uuid
import asyncio
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from io import BytesIO

import streamlit as st
import pandas as pd
import aiohttp
import redis.asyncio as redis
from redis.asyncio import Redis

# -----------------------------
# Configuration
# -----------------------------
APP_TITLE = "IRAS • GST Register Checker (Redis Queue)"
ENV_VAR_CLIENT_ID = "IRAS_CLIENT_ID"
ENV_VAR_CLIENT_SECRET = "IRAS_CLIENT_SECRET"
ENV_VAR_REDIS_URL = "REDIS_URL"  # e.g., redis://localhost:6379

URLS = {
    "Production": "https://apiservices.iras.gov.sg/iras/prod/GSTListing/SearchGSTRegistered",
    "Sandbox": "https://apisandbox.iras.gov.sg/iras/sb/GSTListing/SearchGSTRegistered",
}

# Redis keys
REDIS_JOB_PREFIX = "iras:job:"
REDIS_RESULT_PREFIX = "iras:result:"
REDIS_QUEUE_KEY = "iras:queue"
REDIS_RATE_LIMIT_KEY = "iras:rate_limit"

# Job statuses
STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"

# Rate limiting (from API response headers)
DEFAULT_RATE_LIMIT_MAX = 100
DEFAULT_RATE_LIMIT_WINDOW = 3600  # 1 hour


# -----------------------------
# Streamlit page config
# -----------------------------
st.set_page_config(page_title=APP_TITLE, layout="wide")


# -----------------------------
# Redis Client Manager
# -----------------------------
class RedisManager:
    """Manages Redis connections and operations."""

    def __init__(self, redis_url: str):
        self.redis_url = redis_url
        self._client: Optional[Redis] = None

    async def get_client(self) -> Redis:
        """Get or create Redis client."""
        if self._client is None:
            self._client = await redis.from_url(
                self.redis_url, encoding="utf-8", decode_responses=True
            )
        return self._client

    async def close(self):
        """Close Redis connection."""
        if self._client:
            await self._client.close()
            self._client = None


# -----------------------------
# Rate Limiter with API Response Headers
# -----------------------------
class RateLimiter:
    """
    Rate limiter that tracks limits from API response headers.
    Headers: X-RateLimit-Remaining, X-RateLimit-Reset
    """

    def __init__(self, redis_client: Redis):
        self.redis = redis_client

    async def get_rate_limit_info(self) -> Dict[str, Any]:
        """Get current rate limit information from Redis."""
        data = await self.redis.get(REDIS_RATE_LIMIT_KEY)
        if data:
            return json.loads(data)
        return {
            "remaining": DEFAULT_RATE_LIMIT_MAX,
            "reset_time": None,
            "max_calls": DEFAULT_RATE_LIMIT_MAX,
        }

    async def update_from_headers(self, headers: Dict[str, str]):
        """
        Update rate limit info from API response headers.
        Expected headers:
        - X-RateLimit-Remaining: calls remaining
        - X-RateLimit-Reset: Unix timestamp of reset time
        - X-RateLimit-Limit: max calls per window
        """
        remaining = headers.get("X-RateLimit-Remaining")
        reset_time = headers.get("X-RateLimit-Reset")
        max_calls = headers.get("X-RateLimit-Limit")

        if remaining is not None:
            info = {
                "remaining": int(remaining),
                "reset_time": int(reset_time) if reset_time else None,
                "max_calls": int(max_calls) if max_calls else DEFAULT_RATE_LIMIT_MAX,
                "updated_at": time.time(),
            }
            await self.redis.set(
                REDIS_RATE_LIMIT_KEY, json.dumps(info), ex=DEFAULT_RATE_LIMIT_WINDOW
            )

    async def can_make_request(self) -> bool:
        """Check if we can make an API request."""
        info = await self.get_rate_limit_info()
        remaining = info.get("remaining", 0)
        reset_time = info.get("reset_time")

        # If we have a reset time and it's passed, reset the counter
        if reset_time and time.time() >= reset_time:
            return True

        return remaining > 0

    async def wait_for_reset(self) -> int:
        """Get seconds to wait until rate limit resets."""
        info = await self.get_rate_limit_info()
        reset_time = info.get("reset_time")
        if reset_time:
            wait_seconds = max(0, reset_time - time.time())
            return int(wait_seconds)
        return 0


# -----------------------------
# IRAS API Client
# -----------------------------
class IRASClient:
    """IRAS API client with rate limit header tracking."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        client_id: str,
        client_secret: str,
        rate_limiter: RateLimiter,
    ):
        self.session = session
        self.base_url = base_url
        self.client_id = client_id
        self.client_secret = client_secret
        self.rate_limiter = rate_limiter

    def _headers(self) -> Dict[str, str]:
        return {
            "X-IBM-Client-Id": self.client_id,
            "X-IBM-Client-Secret": self.client_secret,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def search_gst_registered(
        self, reg_id: str
    ) -> Tuple[int, Dict[str, Any], Dict[str, str]]:
        """
        Calls the IRAS endpoint.
        Returns (http_status, parsed_json, response_headers)
        """
        payload = {"clientID": self.client_id, "regID": reg_id}
        try:
            async with self.session.post(
                self.base_url,
                json=payload,
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                # Extract rate limit headers
                resp_headers = dict(resp.headers)

                # Update rate limiter
                await self.rate_limiter.update_from_headers(resp_headers)

                # Parse body
                body = (
                    await resp.json()
                    if resp.headers.get("content-type", "").startswith(
                        "application/json"
                    )
                    else {"raw": await resp.text()}
                )
                return resp.status, body, resp_headers
        except aiohttp.ClientError as e:
            return 0, {"error": f"{e.__class__.__name__}: {e}"}, {}
        except Exception as e:
            return 0, {"error": f"{e.__class__.__name__}: {e}"}, {}


# -----------------------------
# Job Management
# -----------------------------
class JobManager:
    """Manages background jobs in Redis."""

    def __init__(self, redis_client: Redis):
        self.redis = redis_client

    async def create_job(
        self,
        uens: List[str],
        environment: str,
        filename: str,
        original_df: pd.DataFrame,
    ) -> str:
        """Create a new job and add to queue."""
        job_id = str(uuid.uuid4())
        job_data = {
            "job_id": job_id,
            "uens": uens,
            "environment": environment,
            "filename": filename,
            "status": STATUS_PENDING,
            "created_at": time.time(),
            "total_uens": len(uens),
            "processed_uens": 0,
            "original_df": original_df.to_json(orient="split"),
        }

        # Store job data
        await self.redis.set(
            f"{REDIS_JOB_PREFIX}{job_id}",
            json.dumps(job_data),
            ex=86400,  # Expire after 24 hours
        )

        # Add to queue
        await self.redis.rpush(REDIS_QUEUE_KEY, job_id)

        return job_id

    async def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Get job data by ID."""
        data = await self.redis.get(f"{REDIS_JOB_PREFIX}{job_id}")
        if data:
            return json.loads(data)
        return None

    async def update_job_status(self, job_id: str, status: str, **kwargs):
        """Update job status and other fields."""
        job_data = await self.get_job(job_id)
        if job_data:
            job_data["status"] = status
            job_data.update(kwargs)
            await self.redis.set(
                f"{REDIS_JOB_PREFIX}{job_id}", json.dumps(job_data), ex=86400
            )

    async def store_result(self, job_id: str, result_df: pd.DataFrame):
        """Store job result as Excel in Redis."""
        buffer = BytesIO()
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            result_df.to_excel(writer, index=False, sheet_name="Results")

        excel_bytes = buffer.getvalue()

        # Store as binary
        await self.redis.set(
            f"{REDIS_RESULT_PREFIX}{job_id}",
            excel_bytes,
            ex=86400,  # Expire after 24 hours
        )

    async def get_result(self, job_id: str) -> Optional[bytes]:
        """Get job result Excel bytes."""
        result = await self.redis.get(f"{REDIS_RESULT_PREFIX}{job_id}")
        if result and isinstance(result, bytes):
            return result
        # If Redis returns string (shouldn't with decode_responses=False for binary)
        if result and isinstance(result, str):
            return result.encode("latin-1")
        return None

    async def get_queue_length(self) -> int:
        """Get number of jobs in queue."""
        return await self.redis.llen(REDIS_QUEUE_KEY)


# -----------------------------
# Background Worker
# -----------------------------
async def process_job(
    job_id: str,
    redis_manager: RedisManager,
    client_id: str,
    client_secret: str,
):
    """Process a single job from the queue."""
    redis_client = await redis_manager.get_client()
    job_manager = JobManager(redis_client)
    rate_limiter = RateLimiter(redis_client)

    # Get job data
    job_data = await job_manager.get_job(job_id)
    if not job_data:
        return

    # Update status to processing
    await job_manager.update_job_status(job_id, STATUS_PROCESSING)

    try:
        uens = job_data["uens"]
        environment = job_data["environment"]
        base_url = URLS[environment]

        # Load original DataFrame
        original_df = pd.read_json(job_data["original_df"], orient="split")

        # Create API client
        async with aiohttp.ClientSession() as session:
            client = IRASClient(
                session, base_url, client_id, client_secret, rate_limiter
            )

            results = []
            for i, uen in enumerate(uens):
                # Check rate limit
                if not await rate_limiter.can_make_request():
                    wait_seconds = await rate_limiter.wait_for_reset()
                    if wait_seconds > 0:
                        # Store partial progress
                        await job_manager.update_job_status(
                            job_id,
                            STATUS_PROCESSING,
                            processed_uens=i,
                            waiting_for_reset=True,
                            wait_seconds=wait_seconds,
                        )
                        await asyncio.sleep(
                            min(wait_seconds, 60)
                        )  # Wait max 60s at a time

                # Make API call
                status, body, headers = await client.search_gst_registered(uen)
                results.append((uen, status, body))

                # Update progress
                await job_manager.update_job_status(
                    job_id,
                    STATUS_PROCESSING,
                    processed_uens=i + 1,
                    waiting_for_reset=False,
                )

                # Small delay to avoid overwhelming API
                await asyncio.sleep(0.1)

        # Process results
        result_df = process_batch_results(original_df, original_df.columns[0], results)

        # Store result
        await job_manager.store_result(job_id, result_df)

        # Mark as completed
        await job_manager.update_job_status(
            job_id,
            STATUS_COMPLETED,
            completed_at=time.time(),
            processed_uens=len(uens),
        )

    except Exception as e:
        # Mark as failed
        await job_manager.update_job_status(
            job_id,
            STATUS_FAILED,
            error=str(e),
            failed_at=time.time(),
        )


def process_batch_results(
    df_in: pd.DataFrame, col_a_name: str, results: List[Tuple[str, int, Dict[str, Any]]]
) -> pd.DataFrame:
    """Convert API results to DataFrame with response columns."""
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


# -----------------------------
# Background Worker Runner
# -----------------------------
async def worker_loop(redis_manager: RedisManager, client_id: str, client_secret: str):
    """Continuously process jobs from the queue."""
    redis_client = await redis_manager.get_client()

    while True:
        try:
            # Block and wait for job (BLPOP with 1 second timeout)
            result = await redis_client.blpop(REDIS_QUEUE_KEY, timeout=1)

            if result:
                _, job_id = result
                await process_job(job_id, redis_manager, client_id, client_secret)
        except Exception as e:
            print(f"Worker error: {e}")
            await asyncio.sleep(1)


# -----------------------------
# Streamlit UI
# -----------------------------
async def main_ui():
    """Main Streamlit UI."""
    st.title(APP_TITLE)
    st.caption(
        "Upload Excel with UENs in Column A. Jobs are processed in the background via Redis."
    )

    # Check environment variables
    client_id = os.getenv(ENV_VAR_CLIENT_ID)
    client_secret = os.getenv(ENV_VAR_CLIENT_SECRET)
    redis_url = os.getenv(ENV_VAR_REDIS_URL, "redis://localhost:6379")

    if not client_id or not client_secret:
        st.error(
            f"⚠️ Missing credentials: Set `{ENV_VAR_CLIENT_ID}` and `{ENV_VAR_CLIENT_SECRET}` environment variables."
        )
        return

    # Initialize Redis manager
    redis_manager = RedisManager(redis_url)
    redis_client = await redis_manager.get_client()
    job_manager = JobManager(redis_client)
    rate_limiter = RateLimiter(redis_client)

    # Sidebar configuration
    with st.sidebar:
        st.header("⚙️ Configuration")
        environment = st.selectbox(
            "Environment",
            options=list(URLS.keys()),
            index=0,
        )

        st.divider()

        # Rate limit info
        rate_info = await rate_limiter.get_rate_limit_info()
        st.metric("API Calls Remaining", rate_info.get("remaining", "Unknown"))

        reset_time = rate_info.get("reset_time")
        if reset_time:
            reset_dt = datetime.fromtimestamp(reset_time)
            time_until_reset = reset_dt - datetime.now()
            if time_until_reset.total_seconds() > 0:
                st.metric(
                    "Reset In", f"{int(time_until_reset.total_seconds() / 60)} min"
                )

        queue_length = await job_manager.get_queue_length()
        st.metric("Jobs in Queue", queue_length)

    # Main content
    tab1, tab2 = st.tabs(["📤 Submit Job", "📊 Job Status"])

    with tab1:
        st.header("Submit New Job")

        uploaded_file = st.file_uploader(
            "Upload Excel file (.xlsx)",
            type=["xlsx"],
            help="Column A must contain UEN/NRIC/GST Registration IDs",
        )

        if uploaded_file:
            try:
                df = pd.read_excel(uploaded_file, engine="openpyxl")
                st.success(f"✅ Loaded {len(df)} rows from `{uploaded_file.name}`")

                # Extract UENs from Column A
                col_a_name = df.columns[0]
                uens = df.iloc[:, 0].astype(str).str.strip().tolist()
                uens = [u for u in uens if u and u.lower() != "nan"]

                st.info(f"Found {len(uens)} valid UENs in column `{col_a_name}`")

                # Preview
                with st.expander("📋 Preview Data"):
                    st.dataframe(df.head(10))

                # Submit button
                if st.button("🚀 Submit Job", type="primary"):
                    # Check rate limit
                    if not await rate_limiter.can_make_request():
                        wait_seconds = await rate_limiter.wait_for_reset()
                        st.error(
                            f"⚠️ Rate limit reached. Please wait {wait_seconds // 60} minutes."
                        )
                    else:
                        # Create job
                        job_id = await job_manager.create_job(
                            uens, environment, uploaded_file.name, df
                        )
                        st.success(f"✅ Job submitted! Job ID: `{job_id}`")
                        st.info("Check the 'Job Status' tab to monitor progress.")

                        # Store in session state
                        if "job_ids" not in st.session_state:
                            st.session_state.job_ids = []
                        st.session_state.job_ids.append(job_id)

            except Exception as e:
                st.error(f"❌ Error reading Excel: {e}")

    with tab2:
        st.header("Job Status & Results")

        # Get all job IDs from session
        if "job_ids" not in st.session_state or not st.session_state.job_ids:
            st.info("No jobs submitted yet. Upload a file in the 'Submit Job' tab.")
        else:
            # Display each job
            for job_id in reversed(st.session_state.job_ids):
                job_data = await job_manager.get_job(job_id)

                if not job_data:
                    st.warning(f"Job `{job_id}` not found (may have expired)")
                    continue

                status = job_data["status"]
                total = job_data["total_uens"]
                processed = job_data.get("processed_uens", 0)
                filename = job_data.get("filename", "Unknown")

                # Status indicator
                status_emoji = {
                    STATUS_PENDING: "⏳",
                    STATUS_PROCESSING: "⚙️",
                    STATUS_COMPLETED: "✅",
                    STATUS_FAILED: "❌",
                }

                with st.expander(
                    f"{status_emoji.get(status, '❓')} Job: `{job_id[:8]}...` - {status.upper()}",
                    expanded=(status == STATUS_PROCESSING),
                ):
                    col1, col2, col3 = st.columns(3)
                    col1.metric("Status", status.upper())
                    col2.metric("Progress", f"{processed}/{total}")
                    col3.metric("File", filename)

                    # Progress bar
                    if total > 0:
                        progress = processed / total
                        st.progress(progress, text=f"{int(progress * 100)}% complete")

                    # Waiting for rate limit reset
                    if job_data.get("waiting_for_reset"):
                        wait_sec = job_data.get("wait_seconds", 0)
                        st.warning(
                            f"⏸️ Waiting for rate limit reset ({wait_sec} seconds)"
                        )

                    # Download button for completed jobs
                    if status == STATUS_COMPLETED:
                        result_bytes = await job_manager.get_result(job_id)
                        if result_bytes:
                            st.download_button(
                                label="📥 Download Results",
                                data=result_bytes,
                                file_name=f"results_{job_id[:8]}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
                                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            )
                        else:
                            st.error("Result file not found")

                    # Error message for failed jobs
                    if status == STATUS_FAILED:
                        error_msg = job_data.get("error", "Unknown error")
                        st.error(f"Error: {error_msg}")

        # Refresh button
        if st.button("🔄 Refresh Status"):
            st.rerun()

    await redis_manager.close()


# -----------------------------
# Main Entry Points
# -----------------------------
def run_streamlit():
    """Run Streamlit UI."""
    asyncio.run(main_ui())


def run_worker():
    """Run background worker."""
    client_id = os.getenv(ENV_VAR_CLIENT_ID)
    client_secret = os.getenv(ENV_VAR_CLIENT_SECRET)
    redis_url = os.getenv(ENV_VAR_REDIS_URL, "redis://localhost:6379")

    if not client_id or not client_secret:
        print(f"Error: {ENV_VAR_CLIENT_ID} and {ENV_VAR_CLIENT_SECRET} must be set")
        return

    print(f"🚀 Starting worker with Redis: {redis_url}")
    redis_manager = RedisManager(redis_url)

    try:
        asyncio.run(worker_loop(redis_manager, client_id, client_secret))
    except KeyboardInterrupt:
        print("\n⚠️ Worker stopped")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "worker":
        # Run as worker
        run_worker()
    else:
        # Run as Streamlit app
        run_streamlit()
