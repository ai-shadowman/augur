#!/usr/bin/env bash
echo "Initializing GraphRAG index..."
python -m graphrag init --force --root "$1" 2>&1

echo "Copying settings.yaml..."
cp templates/settings.yaml "$1/settings.yaml"
sleep 5

echo "Populating GraphRAG index..."
python -m graphrag index --root "$1" 2>&1 &
INDEX_PID=$!

while kill -0 $INDEX_PID 2>/dev/null; do
    GRAPHRAG_LOG=$(find "$1" -type f -name "indexing-engine.log" 2>/dev/null | xargs ls -t 2>/dev/null | head -n 1)
    if [ -n "$GRAPHRAG_LOG" ] && [ -f "$GRAPHRAG_LOG" ]; then
        echo "Streaming logs from $GRAPHRAG_LOG..."
        tail -f --pid=$INDEX_PID "$GRAPHRAG_LOG" || true
        break
    fi
    sleep 5
done

wait $INDEX_PID
exit $?
