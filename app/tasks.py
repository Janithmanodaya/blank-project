import asyncio
from typing import List

# Shared background job queue and worker task list.
# Kept in a separate module to avoid circular imports between main and webui.
job_queue: "asyncio.Queue[int]" = asyncio.Queue()
workers: List[asyncio.Task] = []