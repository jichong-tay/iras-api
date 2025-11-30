# IRAS GST Register Checker - Redis Queue Version

A Streamlit web application with Redis-based background task queue for checking Singapore IRAS GST registration status.

## Features

- **Background Processing**: Jobs run asynchronously via Redis queue
- **Real-time Rate Limiting**: Tracks API rate limits from response headers
- **Job Status Tracking**: Monitor progress and download results when ready
- **Persistent Results**: Results stored in Redis for 24 hours
- **Multiple Jobs**: Submit multiple batches and track them independently

## Architecture

### Components

1. **Streamlit UI** (`main_redis.py`): Web interface for uploading Excel files and monitoring jobs
2. **Background Worker** (`main_redis.py worker`): Processes jobs from Redis queue
3. **Redis**: Message queue and temporary storage for jobs and results

### Data Flow

```
User uploads Excel → Job created in Redis → Worker picks up job →
API calls made (with rate limiting) → Results stored in Redis →
User downloads results from UI
```

## Installation

### Prerequisites

- Python 3.10+
- Redis server

### Install Redis

**macOS:**
```bash
brew install redis
brew services start redis
```

**Ubuntu/Debian:**
```bash
sudo apt install redis-server
sudo systemctl start redis
```

**Verify Redis is running:**
```bash
redis-cli ping
# Should return: PONG
```

### Install Python Dependencies

```bash
# Add to pyproject.toml dependencies:
# redis = ">=5.0.0"

# Or install directly:
pip install redis aiohttp pandas openpyxl streamlit
```

## Configuration

Set environment variables:

```bash
export IRAS_CLIENT_ID="your_client_id"
export IRAS_CLIENT_SECRET="your_client_secret"
export REDIS_URL="redis://localhost:6379"  # Optional, defaults to localhost
```

## Usage

### Option 1: Use the Start Script (Recommended)

```bash
chmod +x start_redis_app.sh
./start_redis_app.sh
```

This starts both the worker and Streamlit UI. Press Ctrl+C to stop both.

### Option 2: Manual Start

**Terminal 1 - Start the worker:**
```bash
python main_redis.py worker
```

**Terminal 2 - Start the Streamlit UI:**
```bash
streamlit run main_redis.py
```

Access the UI at http://localhost:8501

## How to Use

1. **Open the web interface** at http://localhost:8501
2. **Upload an Excel file** with UENs in Column A
3. **Click "Submit Job"** - the job is queued for background processing
4. **Switch to "Job Status" tab** to monitor progress
5. **Download results** when the job status shows "COMPLETED"

## Excel Format

**Input:**
- Column A must contain UEN/NRIC/GST Registration IDs
- Other columns are preserved in output

**Output (same format as other versions):**
- Original columns preserved
- Three new columns added:
  - `response-status`: IRAS returnCode (10=Success, 20=Warning, 30=Failure)
  - `response-registrationId`: GST registration ID if found
  - `json-response`: Full JSON response (stringified)

## Rate Limiting

The application tracks rate limits from API response headers:

- **X-RateLimit-Remaining**: Calls remaining in current window
- **X-RateLimit-Reset**: Unix timestamp when limit resets
- **X-RateLimit-Limit**: Maximum calls per window

When the rate limit is reached, jobs automatically pause and resume after the reset time.

The UI sidebar shows:
- Remaining API calls
- Time until rate limit reset
- Number of jobs in queue

## Redis Keys

The application uses the following Redis key patterns:

- `iras:job:<job_id>`: Job metadata (JSON)
- `iras:result:<job_id>`: Excel result file (binary)
- `iras:queue`: List of pending job IDs
- `iras:rate_limit`: Current rate limit information (JSON)

All keys expire after 24 hours.

## Job Statuses

- **PENDING**: Job is queued, waiting for worker
- **PROCESSING**: Worker is actively processing the job
- **COMPLETED**: Job finished successfully, results available
- **FAILED**: Job encountered an error

## Monitoring

### Check Redis Queue Length

```bash
redis-cli llen iras:queue
```

### View Job Data

```bash
redis-cli get "iras:job:<job_id>"
```

### View Rate Limit Info

```bash
redis-cli get iras:rate_limit
```

### Clear All Jobs (if needed)

```bash
redis-cli flushdb
```

## Troubleshooting

### Redis Connection Error

```
Error: Redis connection refused
```

**Solution:** Make sure Redis is running:
```bash
# Start Redis
redis-server

# Or on macOS with Homebrew:
brew services start redis
```

### Worker Not Processing Jobs

1. Check worker is running: `ps aux | grep "main_redis.py worker"`
2. Check Redis queue: `redis-cli llen iras:queue`
3. Check worker logs for errors

### Rate Limit Reached

The application will automatically wait for the rate limit to reset. You can monitor:
- Sidebar shows "Reset In" countdown
- Job status shows "Waiting for rate limit reset"

### Job Expired

Jobs and results expire after 24 hours. If you see "Job not found", the job has expired. Re-upload the file to create a new job.

## Comparison with Other Versions

| Feature              | main.py | main_requests.py | batch_script.py | batch_script_async.py | main_redis.py |
|---------------------|---------|------------------|-----------------|----------------------|---------------|
| **UI**              | ✅ Web  | ✅ Web           | ❌ CLI          | ❌ CLI               | ✅ Web        |
| **Background Jobs** | ❌      | ❌               | ❌              | ❌                   | ✅            |
| **Multiple Jobs**   | ❌      | ❌               | ❌              | ❌                   | ✅            |
| **Rate Limit Headers** | ❌   | ❌               | ❌              | ❌                   | ✅            |
| **Job Queue**       | ❌      | ❌               | ❌              | ❌                   | ✅ Redis      |
| **Async**           | ✅      | ❌               | ❌              | ✅                   | ✅            |
| **Complexity**      | Medium  | Low              | Low             | Medium               | High          |

## Advanced Configuration

### Redis Configuration

You can customize Redis settings via environment variable:

```bash
# Remote Redis
export REDIS_URL="redis://username:password@hostname:6379/0"

# Redis with TLS
export REDIS_URL="rediss://hostname:6380"

# Redis Sentinel
export REDIS_URL="redis+sentinel://hostname:26379/mymaster/0"
```

### Adjust Expiration Times

Edit `main_redis.py` to change TTL:

```python
# Job data expiration (default 24 hours)
await self.redis.set(..., ex=86400)

# Change to 48 hours:
await self.redis.set(..., ex=172800)
```

## Production Deployment

For production use:

1. **Use managed Redis** (AWS ElastiCache, Redis Cloud, etc.)
2. **Run multiple workers** for better throughput
3. **Use process manager** like systemd or supervisor
4. **Add monitoring** (Prometheus, Datadog, etc.)
5. **Enable Redis persistence** (RDB or AOF)
6. **Add authentication** to Redis

Example systemd service:

```ini
[Unit]
Description=IRAS GST Checker Worker
After=network.target redis.service

[Service]
Type=simple
User=www-data
WorkingDirectory=/path/to/app
Environment="IRAS_CLIENT_ID=xxx"
Environment="IRAS_CLIENT_SECRET=xxx"
Environment="REDIS_URL=redis://localhost:6379"
ExecStart=/usr/bin/python3 main_redis.py worker
Restart=always

[Install]
WantedBy=multi-user.target
```

## API Response Headers

The IRAS API returns rate limiting information in response headers. The application automatically parses these:

```
X-RateLimit-Limit: 100
X-RateLimit-Remaining: 95
X-RateLimit-Reset: 1701360000
```

This allows accurate tracking without manual sliding window calculations.

## License

Same as main project.
