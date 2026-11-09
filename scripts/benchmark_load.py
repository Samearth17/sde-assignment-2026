"""Benchmark ingestion throughput and decision lag on public_03_load chunks.
"""

import glob
import json
import os
import time
import tracemalloc
from pathlib import Path

from controller.db import Database
from controller.ingestion import EventIngester


def benchmark():
    event_files = sorted(glob.glob("runs/base03/events/*.jsonl"))
    if not event_files:
        print("No event files found in runs/base03/events")
        return

    db_path = "benchmark_load.db"
    if os.path.exists(db_path):
        os.remove(db_path)

    db = Database(db_path)
    ingester = EventIngester(db)

    print(f"Benchmarking ingestion on {len(event_files)} chunks from runs/base03/events...")
    tracemalloc.start()
    t0 = time.time()
    total_events = 0

    for i, fpath in enumerate(event_files[:10]):  # benchmark first 10 chunks (~2.5 million events)
        with open(fpath, "r") as f:
            events = [json.loads(line) for line in f if line.strip()]

        t_chunk_start = time.time()
        ingester.process_chunk(i, events, "events", "2027-01-04T12:00:00")
        t_chunk = time.time() - t_chunk_start
        total_events += len(events)
        rate = len(events) / max(t_chunk, 1e-6)
        print(f"  Chunk {i} ({Path(fpath).name}): {len(events)} events in {t_chunk:.2f}s ({rate:,.0f} ev/s)")

    total_time = time.time() - t0
    current_mem, peak_mem = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    overall_rate = total_events / max(total_time, 1e-6)
    print("\n=== Benchmark Results ===")
    print(f"Total events processed: {total_events:,}")
    print(f"Wall-clock time:        {total_time:.2f}s")
    print(f"Ingest throughput:      {overall_rate:,.0f} events/sec")
    print(f"Peak memory:            {peak_mem / (1024 * 1024):.1f} MB")
    print(f"Time per 850k-event day: {850000 / overall_rate:.1f}s")

    db.close()
    if os.path.exists(db_path):
        os.remove(db_path)


if __name__ == "__main__":
    benchmark()
