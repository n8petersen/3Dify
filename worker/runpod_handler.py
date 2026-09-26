"""RunPod Serverless generator handler.

Reuses pipeline.py (run_pipeline / run_text_pipeline) and gpu_sampler.py unchanged.
`event["input"]` arrives in the exact shape RunPodBackend._build_job_assign_payload()
sends server-side (server/backends/runpod.py): job_id, job_type, plus prompt+settings
(text) or image_filename+image_base64+settings (image).

pipeline.run_pipeline()/run_text_pipeline() block and call a synchronous
progress_callback — RunPod's generator handler needs to yield incrementally, so the
pipeline call runs in a background thread and progress_callback pushes onto a
queue.Queue that this generator drains and yields from. Every yielded dict is already
in worker-protocol shape (job_progress/job_generated_image/job_complete/job_failed),
matching what worker.py sends over the WebSocket — RunPodBackend.stream() forwards
provider output straight into _handle_worker_message() unchanged.
"""

import base64
import logging
import os
import queue
import shutil
import threading

import runpod

import gpu_sampler as gpu_sampler_mod
import pipeline

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("runpod_handler")

TEMP_DIR = "/tmp/img2stl_worker"
os.makedirs(TEMP_DIR, exist_ok=True)


def _make_progress_cb(q: queue.Queue):
    def progress_cb(step, pct, message):
        q.put({"type": "job_progress", "step": step, "progress_pct": pct, "message": message})
    return progress_cb


def _run_job(job: dict, q: queue.Queue) -> None:
    """Runs in a background thread. Pushes every message onto q; None is the done sentinel."""
    job_id = job["job_id"]
    job_type = job.get("job_type", "image")
    settings = job.get("settings", {})
    progress_cb = _make_progress_cb(q)

    output_dir = os.path.join(TEMP_DIR, job_id)
    image_path = None

    sampler = gpu_sampler_mod.GPUSampler(interval=1.0)
    sampler.start()

    try:
        if job_type == "text":
            prompt = job["prompt"]
            logger.info("Job %s text-to-3D: %s", job_id, prompt[:80])
            result = pipeline.run_text_pipeline(prompt, output_dir, progress_cb, settings)

            generated_path = result.get("generated_image_path")
            if generated_path and os.path.exists(generated_path):
                with open(generated_path, "rb") as f:
                    img_b64 = base64.b64encode(f.read()).decode("ascii")
                q.put({
                    "type": "job_generated_image",
                    "job_id": job_id,
                    "image_base64": img_b64,
                })
        else:
            filename = os.path.basename(job.get("image_filename", "input.jpg")) or "input.jpg"
            image_data = base64.b64decode(job["image_base64"])
            image_path = os.path.join(TEMP_DIR, f"{job_id}_{filename}")
            with open(image_path, "wb") as f:
                f.write(image_data)
            logger.info("Saved input image: %s (%d bytes)", image_path, len(image_data))
            result = pipeline.run_pipeline(image_path, output_dir, progress_cb, settings)
    except Exception as e:
        logger.error("Job %s failed: %s", job_id, e, exc_info=True)
        err_str = str(e).lower()
        if "cuda" in err_str or "gpu" in err_str or "out of memory" in err_str:
            error_msg = "GPU error during generation. Please try again later."
        else:
            error_msg = "Generation failed unexpectedly. Please try again."
        q.put({"type": "job_failed", "job_id": job_id, "error": error_msg, "step": "unknown"})
        q.put(None)
        return
    finally:
        sampler_metrics = sampler.stop()
        if image_path:
            try:
                os.remove(image_path)
            except OSError:
                pass

    try:
        stl_path = result["stl_path"]
        glb_path = result["glb_path"]

        with open(stl_path, "rb") as f:
            stl_b64 = base64.b64encode(f.read()).decode("ascii")

        glb_b64 = None
        glb_filename = None
        if os.path.exists(glb_path):
            with open(glb_path, "rb") as f:
                glb_b64 = base64.b64encode(f.read()).decode("ascii")
            glb_filename = os.path.basename(glb_path)

        q.put({
            "type": "job_complete",
            "job_id": job_id,
            "stl_filename": os.path.basename(stl_path),
            "stl_base64": stl_b64,
            "glb_filename": glb_filename,
            "glb_base64": glb_b64,
            "vertex_count": result["vertex_count"],
            "face_count": result["face_count"],
            "is_watertight": result["is_watertight"],
            "generation_time_s": result["generation_time_s"],
            "gpu_metrics": sampler_metrics,
        })
        logger.info(
            "Job %s complete: %sv, watertight=%s, %.1fs",
            job_id, f"{result['vertex_count']:,}", result["is_watertight"],
            result["generation_time_s"],
        )
    except Exception as e:
        logger.error("Job %s failed while packaging result: %s", job_id, e, exc_info=True)
        q.put({
            "type": "job_failed", "job_id": job_id,
            "error": "Generation failed unexpectedly. Please try again.",
            "step": "unknown",
        })
    finally:
        shutil.rmtree(output_dir, ignore_errors=True)
        q.put(None)


def handler(event):
    """RunPod generator handler — yields worker-protocol-shaped dicts as the job progresses."""
    job = event["input"]
    job_id = job.get("job_id", "unknown")
    logger.info("Job %s accepted (type=%s)", job_id, job.get("job_type", "image"))

    q: queue.Queue = queue.Queue()
    thread = threading.Thread(target=_run_job, args=(job, q), daemon=True)
    thread.start()

    while True:
        msg = q.get()
        if msg is None:
            break
        yield msg


runpod.serverless.start({"handler": handler, "return_aggregate_stream": True})
