# Quick Start Guide - Redis Queue Version

## What's New

I've created `main_redis.py` - a new version that uses Redis as a background task queue. This allows:

1. **Background Processing**: Upload files and let them process while you do other things
2. **Multiple Jobs**: Submit multiple Excel files and track them all independently  
3. **Smart Rate Limiting**: Reads rate limit info directly from API response headers
4. **Persistent Results**: Download results any time within 24 hours
5. **Auto-Resume**: Jobs automatically pause and resume when rate limits are hit

## Architecture

```
┌─────────────┐      ┌─────────────┐      ┌─────────────┐
│  Streamlit  │ ───▶ │    Redis    │ ◀─── │   Worker    │
│     UI      │      │    Queue    │      │   Process   │
└─────────────┘      └─────────────┘      └─────────────┘
     │                     │                      │
     │                     │                      │
     └─────── Upload ──────┤                      │
                           │                      │
                           ├────── Job ID ────────▶
                           │                      │
                           │                  Makes API
                           │                   Calls ▼
                           │                      │
                           ◀────── Result ────────┘
                           │
     ┌────── Download ─────┤
     │                     │
     ▼                     ▼
```

## Setup Instructions

### 1. Install Redis

**macOS (using Homebrew):**
```bash
brew install redis
brew services start redis
```

**Ubuntu/Debian:**
```bash
sudo apt update
sudo apt install redis-server
sudo systemctl start redis
```

**Verify Redis is running:**
```bash
redis-cli ping
# Should return: PONG
```

### 2. Install Python Dependencies

The `redis` package has already been added to `pyproject.toml`. Install it:

```bash
# If using uv:
uv pip install redis

# Or with pip:
pip install redis
```

### 3. Set Environment Variables

```bash
export IRAS_CLIENT_ID="your_client_id"
export IRAS_CLIENT_SECRET="your_client_secret"
export REDIS_URL="redis://localhost:6379"  # Optional, defaults to localhost
```

## Running the Application

### Method 1: Use the Start Script (Easiest)

```bash
# Make script executable (first time only)
chmod +x start_redis_app.sh

# Start both worker and UI
./start_redis_app.sh
```

This starts:
- Background worker process
- Streamlit UI at http://localhost:8501

Press `Ctrl+C` to stop both.

### Method 2: Manual Start (Two Terminals)

**Terminal 1 - Start the worker:**
```bash
python main_redis.py worker
```

You should see:
```
🚀 Starting worker with Redis: redis://localhost:6379
```

**Terminal 2 - Start the Streamlit UI:**
```bash
streamlit run main_redis.py
```

Access at: http://localhost:8501

## How to Use

1. **Open browser** to http://localhost:8501

2. **Check the sidebar** to see:
   - API calls remaining
   - Time until rate limit resets
   - Jobs in queue

3. **Submit Job tab**:
   - Upload Excel file (Column A = UENs)
   - Click "Submit Job"
   - Note the Job ID

4. **Job Status tab**:
   - See all your submitted jobs
   - Watch progress in real-time
   - Download results when complete

5. **Multiple Jobs**:
   - Submit multiple files
   - They'll be processed sequentially
   - Each job tracked independently

## Key Features

### Rate Limit Tracking from API Headers

Unlike other versions, `main_redis.py` reads rate limit info from the API response:

```
X-RateLimit-Remaining: 95    ← Calls left this hour
X-RateLimit-Reset: 1701360000 ← Unix timestamp of reset
X-RateLimit-Limit: 100        ← Max calls per hour
```

The UI shows real-time countdown to reset.

### Auto-Pause on Rate Limit

When rate limit is reached:
1. Job status shows "Waiting for rate limit reset"
2. Worker automatically sleeps until reset time
3. Job resumes automatically when limit resets
4. No manual intervention needed

### Job Persistence

- Jobs stored for 24 hours
- Results stored for 24 hours
- Download results anytime within that window
- Can close browser and come back later

### Multiple Workers (Advanced)

For faster processing, run multiple workers:

```bash
# Terminal 1
python main_redis.py worker

# Terminal 2  
python main_redis.py worker

# Terminal 3
python main_redis.py worker
```

All workers will pull from the same queue.

## Monitoring Redis

### Check queue length:
```bash
redis-cli llen iras:queue
```

### View rate limit info:
```bash
redis-cli get iras:rate_limit
```

### View a job:
```bash
redis-cli get "iras:job:<job_id>"
```

### Clear all data (if needed):
```bash
redis-cli flushdb
```

## Troubleshooting

### "Redis connection refused"

**Problem:** Redis is not running

**Solution:**
```bash
# Start Redis
redis-server

# Or with Homebrew:
brew services start redis
```

### Worker not processing jobs

1. Check worker is running:
   ```bash
   ps aux | grep "main_redis.py worker"
   ```

2. Check Redis queue:
   ```bash
   redis-cli llen iras:queue
   ```

3. Check worker terminal for errors

### Job stuck in PROCESSING

- Check if worker crashed (restart it)
- Check Redis is still running
- Job may be waiting for rate limit reset (check status message)

### Can't download results

- Results expire after 24 hours
- Check job status is "COMPLETED"
- Try refreshing the page

## Comparison with Other Versions

| Feature | main.py | main_redis.py |
|---------|---------|---------------|
| **UI blocks during processing** | ✅ Yes | ❌ No |
| **Can submit multiple batches** | ❌ No | ✅ Yes |
| **Rate limit from API headers** | ❌ No | ✅ Yes |
| **Auto-resume after rate limit** | ❌ No | ✅ Yes |
| **Requires Redis** | ❌ No | ✅ Yes |
| **Complexity** | Medium | High |
| **Best for** | Quick single batches | Multiple large batches |

## Production Considerations

For production deployment:

1. **Use managed Redis** (AWS ElastiCache, Redis Cloud, etc.)
2. **Run multiple workers** for higher throughput
3. **Use process manager** (systemd, supervisor)
4. **Enable Redis persistence** (RDB or AOF)
5. **Add Redis authentication**
6. **Monitor worker health**
7. **Set up alerts** for queue depth

## Files Created

- `main_redis.py` - Main application (700+ lines)
- `start_redis_app.sh` - Convenience startup script
- `README_REDIS.md` - Detailed documentation
- Updated `.github/copilot-instructions.md` - AI agent docs
- Updated `pyproject.toml` - Added redis dependency

## Next Steps

1. **Test it**: `./start_redis_app.sh`
2. **Upload a test file** with a few UENs
3. **Watch it process** in the background
4. **Submit another file** while first one processes
5. **Download results** from the Job Status tab

Enjoy background processing! 🚀
