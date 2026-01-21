# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "django>=6.0",
#     "django-tasks-threadpool",
#     "nanodjango>=0.13.0",
# ]
# ///
"""
Django 6 Tasks + SSE Demo

Demonstrates the built-in @task decorator with a custom ThreadPoolBackend
for true background execution, plus Server-Sent Events for progress streaming.
"""

import json
import time

from django.core.cache import cache
from django.http import HttpResponse, JsonResponse, StreamingHttpResponse
from django.tasks import task

from tasks_threadpool.backend import current_result_id
from nanodjango import Django

app = Django(
    TASKS={
        "default": {
            "BACKEND": "tasks_threadpool.ThreadPoolBackend",
            "OPTIONS": {"MAX_WORKERS": 4},
        }
    },
    LOGGING={
        "version": 1,
        "disable_existing_loggers": False,
        "handlers": {
            "console": {"class": "logging.StreamHandler"},
        },
        "root": {"handlers": ["console"], "level": "DEBUG"},
    },
)


# 1. The Background Task (using Django's @task decorator)
@task
def tough_job():
    """A long-running task that reports progress via cache."""
    result_id = current_result_id.get()
    total_steps = 10
    for i in range(total_steps):
        time.sleep(1)
        progress = int(((i + 1) / total_steps) * 100)
        cache.set(f"job:{result_id}", progress, timeout=60)
    return {"status": "complete"}


# 2. The SSE Event Stream
def event_stream(client_id):
    """Broadcasts job progress changes for a client as {job_id: progress, ...}"""
    sent = {}
    while True:
        job_ids = cache.get(f"client:{client_id}", set())
        if not job_ids:
            break
        # Build delta of changed progress
        delta = {}
        for job_id in list(job_ids):
            progress = cache.get(f"job:{job_id}")
            if progress is None:
                continue  # Task hasn't started yet, don't broadcast
            if progress != sent.get(job_id):
                delta[job_id] = sent[job_id] = progress
            if progress >= 100:
                job_ids.discard(job_id)
                cache.set(f"client:{client_id}", job_ids, timeout=300)
                print(f"Job {job_id[:8]} complete for client {client_id[:8]}")
        if delta:
            yield f"data: {json.dumps(delta)}\n\n".encode("utf-8")
        time.sleep(0.25)


# 3. The Views
def get_client_id(request):
    """Get or create a client ID from cookies."""
    client_id = request.COOKIES.get("client_id")
    if not client_id:
        client_id = str(__import__("uuid").uuid4())
    return client_id


def get_jobs_for_client(client_id):
    """Get incomplete jobs for a client with their current progress."""
    from django.tasks import default_task_backend
    from django.tasks.exceptions import TaskResultDoesNotExist

    job_ids = cache.get(f"client:{client_id}", set())
    jobs = []
    for job_id in job_ids:
        progress = cache.get(f"job:{job_id}") or 0
        if progress < 100:
            status = "RUNNING" if progress > 0 else "READY"
            # Get enqueued_at from the task result
            try:
                result = default_task_backend.get_result(job_id)
                enqueued_at = result.enqueued_at.timestamp()
            except TaskResultDoesNotExist:
                enqueued_at = 0
            jobs.append(
                {
                    "id": job_id,
                    "progress": progress,
                    "status": status,
                    "enqueued_at": enqueued_at,
                }
            )
    # Sort by enqueue time (oldest first)
    jobs.sort(key=lambda j: j["enqueued_at"])
    return jobs


@app.route("/")
def index(request):
    client_id = get_client_id(request)
    jobs = get_jobs_for_client(client_id)
    response = app.render(request, "index.html", {"jobs_json": json.dumps(jobs)})
    response.set_cookie("client_id", client_id, max_age=86400)  # 24 hours
    return response


@app.route("/start-job")
def start_job(request):
    """Starts a background job using Django tasks and returns its ID"""
    client_id = request.COOKIES.get("client_id")

    # Enqueue the task - Django assigns it a unique result ID
    result = tough_job.enqueue()

    # Register job with client for SSE tracking
    if client_id:
        jobs = cache.get(f"client:{client_id}", set())
        jobs.add(result.id)
        cache.set(f"client:{client_id}", jobs, timeout=300)

    return JsonResponse({"job_id": result.id, "status": result.status.value})


@app.route("/events")
def events(request):
    """SSE endpoint for job progress updates (multiplexed by client)"""
    client_id = request.COOKIES.get("client_id", "")
    if not cache.get(f"client:{client_id}"):
        return HttpResponse("No jobs", status=400)
    response = StreamingHttpResponse(
        event_stream(client_id), content_type="text/event-stream"
    )
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


# Templates
app.templates["index.html"] = """
<!doctype html>
<html lang="en" x-data="{ evtSource: null }">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link rel="icon" :href="`data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>${evtSource ? '📡' : '🚀'}</text></svg>`">
    <title>Django 6 Tasks + SSE Demo</title>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@picocss/pico@2/css/pico.min.css">
    <script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3.x.x/dist/cdn.min.js"></script>
    <style>
        [x-cloak] { display: none !important; }
        button { transition: background 0.3s ease; }
        .error-flash { background: var(--pico-del-color); }
    </style>
    <script>window.initialJobs = {{ jobs_json|safe }};</script>
</head>
<body>
    <main class="container" x-data="{
        jobs: window.initialJobs,
        get queued() { return this.jobs.filter(j => j.status === 'READY') },
        get running() { return this.jobs.filter(j => j.status === 'RUNNING' && j.progress < 100 && !j.error) },
        get complete() { return this.jobs.filter(j => j.progress >= 100) },
        get errors() { return this.jobs.filter(j => j.error && j.progress < 100) },
        status(job) {
            if (job.error) return job.error;
            if (job.progress >= 100) return 'Complete!';
            if (job.progress > 0) return `Processing... (${job.progress}%)`;
            if (job.status === 'READY') return 'Queued...';
            return 'Starting...';
        },
        init() {
            // Reconnect SSE if there are incomplete jobs
            if (this.jobs.some(j => j.progress < 100)) {
                this.connectSSE();
            }
        },
        connectSSE() {
            if (evtSource) return;
            evtSource = new EventSource('/events');
            evtSource.onmessage = (e) => {
                for (const [id, progress] of Object.entries(JSON.parse(e.data))) {
                    const job = this.jobs.find(j => j.id === id);
                    if (job) {
                        job.progress = progress;
                        if (progress > 0) job.status = 'RUNNING';
                    }
                }
            };
            evtSource.onerror = () => {
                this.running.forEach(j => j.error = 'Connection lost');
                evtSource?.close();
                evtSource = null;
            };
        },
        async startJob() {
            try {
                const res = await fetch('/start-job');
                if (!res.ok) throw new Error('Server error');
                const { job_id, status } = await res.json();
                this.jobs.push({ id: job_id, status, progress: 0 });
                this.connectSSE();
            } catch {
                this.$el.classList.add('error-flash');
                this.$el.dataset.tooltip = 'Failed to start job';
                setTimeout(() => {
                    this.$el.classList.remove('error-flash');
                    delete this.$el.dataset.tooltip;
                }, 1000);
            }
        }
    }">
        <h1>Django 6 Tasks + SSE Demo</h1>
        <div style="display: flex; gap: 2rem; align-items: center;" x-cloak>
            <button @click="startJob()">🚀 Start Long Job</button>
            <span x-show="evtSource">📡 SSE <code>/events</code> connected</span>
            <span x-show="jobs.length">(<span x-show="queued.length"><span x-text="queued.length"></span> queued, </span><span x-text="running.length"></span> running, <span x-text="complete.length"></span> complete)</span>
            <button x-show="complete.length + errors.length" class="outline secondary" style="margin-left: auto;" @click="jobs = jobs.filter(j => j.progress < 100 && !j.error)">Dismiss <span x-text="complete.length + errors.length"></span> job<span x-show="complete.length + errors.length > 1">s</span></button>
        </div>
        <template x-for="job in jobs" :key="job.id">
            <article>
                <span><strong x-text="'Job ' + job.id.slice(0,8)"></strong> - <span x-text="status(job)"></span></span>
                <progress :value="job.progress" max="100"></progress>
            </article>
        </template>
    </main>
</body>
</html>
"""


if __name__ == "__main__":
    app.run("localhost:8000")
