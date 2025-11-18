#!/usr/bin/env python3
"""
IRAS GST Register Batch Checker - Command Line Script (Async Version)
Usage: python batch_script_async.py input.xlsx [--output output.xlsx] [--env sandbox|production] [--concurrency 10]
"""
import os
import sys
import json
import time
import asyncio
import argparse
from collections import deque
from typing import Any, Dict, List, Tuple
from pathlib import Path

import pandas as pd
import aiohttp

# -----------------------------
# Configuration
# -----------------------------
RATE_LIMIT_MAX = 100  # 100 calls / hour
RATE_LIMIT_WINDOW_SEC = 3600  # 1 hour (3600s)
ENV_VAR_CLIENT_ID = "IRAS_CLIENT_ID"
ENV_VAR_CLIENT_SECRET = "IRAS_CLIENT_SECRET"

URLS = {
    "production": "https://apiservices.iras.gov.sg/iras/prod/GSTListing/SearchGSTRegistered",
    "sandbox": "https://apisandbox.iras.gov.sg/iras/sb/GSTListing/SearchGSTRegistered",
}

# Global rate limiter
rate_ts = deque(maxlen=RATE_LIMIT_MAX)


# -----------------------------
# Rate limiting
# -----------------------------
def allowed_calls_remaining(now: float = None) -> int:
    """Return how many calls are still allowed in the current 1-hour window."""
    if now is None:
        now = time.time()
    while rate_ts and (now - rate_ts[0]) > RATE_LIMIT_WINDOW_SEC:
        rate_ts.popleft()
    return RATE_LIMIT_MAX - len(rate_ts)


def record_call(now: float = None):
    rate_ts.append(now or time.time())


# -----------------------------
# IRAS API client (aiohttp)
# -----------------------------
class IRASClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        client_id: str,
        client_secret: str,
    ):
        self.session = session
        self.base_url = base_url
        self.client_id = client_id
        self.client_secret = client_secret

    def _headers(self) -> Dict[str, str]:
        return {
            "X-IBM-Client-Id": self.client_id,
            "X-IBM-Client-Secret": self.client_secret,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def search_gst_registered(self, reg_id: str) -> Tuple[int, Dict[str, Any]]:
        """Calls the IRAS endpoint. Returns (http_status, parsed_json or {"error": ...})"""
        payload = {"clientID": self.client_id, "regID": reg_id}
        try:
            async with self.session.post(
                self.base_url,
                json=payload,
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                body = (
                    await resp.json()
                    if resp.headers.get("content-type", "").startswith(
                        "application/json"
                    )
                    else {"raw": await resp.text()}
                )
                return resp.status, body
        except aiohttp.ClientError as e:
            return 0, {"error": f"{e.__class__.__name__}: {e}"}
        except Exception as e:
            return 0, {"error": f"{e.__class__.__name__}: {e}"}


# -----------------------------
# Batch processing
# -----------------------------
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


async def batch_lookup(
    client: IRASClient,
    uens: List[str],
    concurrency: int = 10,
    progress_callback=None,
) -> List[Tuple[str, int, Dict[str, Any]]]:
    """Perform lookups with concurrency control and rate-limit accounting."""
    results: List[Tuple[str, int, Dict[str, Any]]] = []
    sem = asyncio.Semaphore(concurrency)
    processed = 0

    async def _fetch(uen: str) -> Tuple[str, int, Dict[str, Any]]:
        nonlocal processed
        async with sem:
            record_call()
            status, body = await client.search_gst_registered(uen)
            processed += 1
            if progress_callback:
                progress_callback(processed, len(uens))
            return (uen, status, body)

    tasks = [_fetch(uen) for uen in uens]
    results = await asyncio.gather(*tasks)
    return results


# -----------------------------
# CLI Interface
# -----------------------------
def print_progress(current: int, total: int):
    """Print progress bar to console."""
    pct = (current / total) * 100
    bar_length = 50
    filled = int(bar_length * current / total)
    bar = "█" * filled + "░" * (bar_length - filled)
    print(f"\r[{bar}] {current}/{total} ({pct:.1f}%)", end="", flush=True)


async def main_async(args):
    """Main execution function."""
    # Validate environment variables
    client_id = os.getenv(ENV_VAR_CLIENT_ID)
    client_secret = os.getenv(ENV_VAR_CLIENT_SECRET)

    if not client_id or not client_secret:
        print(
            f"❌ Error: Environment variables {ENV_VAR_CLIENT_ID} and {ENV_VAR_CLIENT_SECRET} must be set."
        )
        sys.exit(1)

    # Validate input file
    input_path = Path(args.input)
    if not input_path.exists():
        print(f"❌ Error: Input file '{args.input}' not found.")
        sys.exit(1)

    # Determine output path
    if args.output:
        output_path = Path(args.output)
    else:
        output_path = (
            input_path.parent / f"{input_path.stem}_results{input_path.suffix}"
        )

    # Read Excel
    print(f"📂 Reading input file: {input_path}")
    try:
        df_in = pd.read_excel(input_path, engine="openpyxl")
    except Exception as e:
        print(f"❌ Error reading Excel: {e}")
        sys.exit(1)

    if df_in.shape[1] == 0:
        print("❌ Error: Excel has no columns.")
        sys.exit(1)

    # Extract UENs from Column A
    col_a_name = df_in.columns[0]
    uens_raw = df_in.iloc[:, 0].astype(str).str.strip().tolist()
    uens = [u for u in uens_raw if u]

    print(f"📊 Found {len(uens)} UENs in column '{col_a_name}'")

    # Check rate limit
    can_call = allowed_calls_remaining()
    if can_call <= 0:
        print(f"⚠️  Rate limit reached (100/hour). Please try again later.")
        sys.exit(1)

    if len(uens) > can_call:
        print(
            f"⚠️  Warning: Only processing first {can_call} UENs due to rate limit (100/hour)."
        )
        uens = uens[:can_call]

    # Get API URL
    base_url = URLS.get(args.env.lower())
    if not base_url:
        print(
            f"❌ Error: Invalid environment '{args.env}'. Use 'sandbox' or 'production'."
        )
        sys.exit(1)

    print(f"🌐 Environment: {args.env.upper()}")
    print(f"⚡ Concurrency: {args.concurrency}")
    print(f"🚀 Starting batch lookup for {len(uens)} UENs...\n")

    # Create client and process batch
    async with aiohttp.ClientSession() as session:
        client = IRASClient(session, base_url, client_id, client_secret)
        results = await batch_lookup(
            client, uens, args.concurrency, progress_callback=print_progress
        )

    print("\n")  # New line after progress bar

    # Process results
    df_out = process_batch_results(df_in, col_a_name, results)

    # Write output
    print(f"💾 Writing results to: {output_path}")
    try:
        with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
            df_out.to_excel(writer, index=False, sheet_name="Results")
    except Exception as e:
        print(f"❌ Error writing Excel: {e}")
        sys.exit(1)

    print(f"✅ Batch completed successfully!")
    print(f"📈 Processed: {len(results)} UENs")
    print(f"📁 Output file: {output_path}")

    # Summary statistics
    success_count = sum(
        1
        for _, status, body in results
        if isinstance(body, dict) and body.get("returnCode") == 10
    )
    error_count = sum(
        1
        for _, status, body in results
        if status == 0 or (isinstance(body, dict) and "error" in body)
    )

    print(f"\n📊 Summary:")
    print(f"   ✓ Successful: {success_count}")
    print(f"   ✗ Errors: {error_count}")
    print(f"   ⚠  Others: {len(results) - success_count - error_count}")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="IRAS GST Register Batch Checker - Command Line Tool (Async Version)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python batch_script_async.py input.xlsx
  python batch_script_async.py input.xlsx --output results.xlsx --concurrency 10
  python batch_script_async.py input.xlsx --env sandbox
  python batch_script_async.py input.xlsx -o results.xlsx -e production -c 5

Environment Variables:
  IRAS_CLIENT_ID       IBM API Gateway Client ID
  IRAS_CLIENT_SECRET   IBM API Gateway Client Secret

Excel Format:
  Column A must contain UEN/NRIC/GST Registration IDs.
  Other columns will be preserved in the output.
  Three new columns will be added:
    - response-status: IRAS returnCode (10=Success, 20=Warning, 30=Failure)
    - response-registrationId: GST registration ID (if found)
    - json-response: Full JSON response (stringified)

Note:
  This async version uses parallel processing for faster batch operations.
  For simpler sequential processing, use batch_script.py instead.
        """,
    )

    parser.add_argument("input", help="Input Excel file (.xlsx)")
    parser.add_argument(
        "-o",
        "--output",
        help="Output Excel file (default: <input>_results.xlsx)",
    )
    parser.add_argument(
        "-e",
        "--env",
        choices=["sandbox", "production"],
        default="production",
        help="API environment (default: production)",
    )
    parser.add_argument(
        "-c",
        "--concurrency",
        type=int,
        default=10,
        help="Number of concurrent requests (default: 10)",
    )

    args = parser.parse_args()

    # Run main async
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\n\n⚠️  Interrupted by user.")
        sys.exit(130)
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
