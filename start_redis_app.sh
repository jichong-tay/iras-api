#!/bin/bash
# Start Redis-based IRAS GST Checker
# This script starts both the Streamlit UI and the background worker

set -e

echo "🚀 IRAS GST Register Checker (Redis Queue)"
echo "=========================================="
echo ""

# Check environment variables
if [ -z "$IRAS_CLIENT_ID" ] || [ -z "$IRAS_CLIENT_SECRET" ]; then
    echo "❌ Error: Please set IRAS_CLIENT_ID and IRAS_CLIENT_SECRET environment variables"
    exit 1
fi

# Check if Redis is running
if ! command -v redis-cli &> /dev/null; then
    echo "⚠️  Warning: redis-cli not found. Make sure Redis is installed and running."
    echo "   Install: brew install redis (macOS) or apt install redis (Ubuntu)"
else
    if ! redis-cli ping &> /dev/null; then
        echo "❌ Error: Redis is not running. Start it with: redis-server"
        exit 1
    fi
    echo "✅ Redis is running"
fi

# Set Redis URL if not set
export REDIS_URL=${REDIS_URL:-redis://localhost:6379}
echo "📡 Redis URL: $REDIS_URL"
echo ""

# Start background worker in the background
echo "🔧 Starting background worker..."
python main_redis.py worker &
WORKER_PID=$!
echo "✅ Worker started (PID: $WORKER_PID)"
echo ""

# Trap to kill worker on exit
trap "echo ''; echo '🛑 Stopping worker...'; kill $WORKER_PID 2>/dev/null; exit" INT TERM EXIT

# Start Streamlit
echo "🌐 Starting Streamlit UI..."
echo "   Access at: http://localhost:8501"
echo ""
streamlit run main_redis.py

# Cleanup happens via trap
