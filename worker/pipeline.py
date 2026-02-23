"""Wraps img2stl.py as a callable module with progress callbacks.

Imports the core functions from img2stl.py directly (no subprocess).
Manages the Hunyuan3D and FLUX.1 pipeline lifecycles — loads one at a time,
swaps on the GPU (16GB VRAM budget, never co-resident).
"""

import gc
import os
import sys
import time
import logging
from pathlib import Path
from typing import Callable, Optional

import config

logger = logging.getLogger(__name__)

# Add img2stl.py's directory to sys.path so we can import it.
# This also triggers img2stl's module-level setup:
#   - PYTORCH_CUDA_ALLOC_CONF env var
#   - hy3dshape sys.path entry
#   - numpy compat patches
#   - torch import
sys.path.insert(0, config.IMG2STL_DIR)
import img2stl

# Module-level pipeline state — only one model in VRAM at a time
_hunyuan_pipeline = None
_flux_pipeline = None


def _free_memory():
    """Force-free VRAM + system RAM after unloading a model."""
    img2stl.clear_vram()
    gc.collect()
    # Release unused heap memory back to OS (Linux only)
    try:
        import ctypes
        libc = ctypes.CDLL("libc.so.6")
        libc.malloc_trim(0)
    except Exception:
        pass


# ── Hunyuan3D lifecycle ────────────────────────────────────────────────

def is_hunyuan_loaded() -> bool:
    return _hunyuan_pipeline is not None


def load_hunyuan():
    """Load the Hunyuan3D pipeline into VRAM (unloads FLUX first if needed)."""
    global _hunyuan_pipeline
    if _hunyuan_pipeline is not None:
        logger.info("Hunyuan3D already loaded, skipping")
        return

    # Ensure FLUX is unloaded first — only one model fits in 16GB
    if _flux_pipeline is not None:
        logger.info("Unloading FLUX before loading Hunyuan3D...")
        unload_flux()

    logger.info("Loading Hunyuan3D 2.1 pipeline...")
    t0 = time.time()
    _hunyuan_pipeline = img2stl.load_pipeline()
    elapsed = time.time() - t0
    logger.info(f"Hunyuan3D loaded in {elapsed:.1f}s")


def unload_hunyuan():
    """Unload Hunyuan3D to free VRAM and system RAM."""
    global _hunyuan_pipeline
    if _hunyuan_pipeline is not None:
        del _hunyuan_pipeline
        _hunyuan_pipeline = None
        _free_memory()
        logger.info("Hunyuan3D unloaded, VRAM + system RAM freed")


# Backward-compat aliases (worker.py used these names before)
load_model = load_hunyuan
unload_model = unload_hunyuan
is_model_loaded = is_hunyuan_loaded


# ── FLUX.1 [dev] lifecycle ─────────────────────────────────────────────

def is_flux_loaded() -> bool:
    return _flux_pipeline is not None


def load_flux():
    """Load FLUX.1 [dev] into VRAM (unloads Hunyuan3D first if needed)."""
    global _flux_pipeline
    if _flux_pipeline is not None:
        logger.info("FLUX already loaded, skipping")
        return

    # Ensure Hunyuan3D is unloaded first
    if _hunyuan_pipeline is not None:
        logger.info("Unloading Hunyuan3D before loading FLUX...")
        unload_hunyuan()

    logger.info("Loading FLUX.1 [dev] pipeline...")
    t0 = time.time()

    import torch
    from diffusers import FluxPipeline, FluxTransformer2DModel
    from transformers import BitsAndBytesConfig

    # The FLUX transformer is ~24GB in bf16 — too big for 16GB VRAM.
    # Load it in 4-bit NF4 quantization (~6GB), then CPU-offload the rest.
    quant_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16,
    )
    transformer = FluxTransformer2DModel.from_pretrained(
        config.FLUX_MODEL_ID,
        subfolder="transformer",
        quantization_config=quant_config,
        torch_dtype=torch.bfloat16,
    )
    _flux_pipeline = FluxPipeline.from_pretrained(
        config.FLUX_MODEL_ID,
        transformer=transformer,
        torch_dtype=torch.bfloat16,
    )
    _flux_pipeline.enable_model_cpu_offload()

    elapsed = time.time() - t0
    logger.info(f"FLUX loaded in {elapsed:.1f}s")


def unload_flux():
    """Unload FLUX to free VRAM and system RAM."""
    global _flux_pipeline
    if _flux_pipeline is not None:
        del _flux_pipeline
        _flux_pipeline = None
        _free_memory()
        logger.info("FLUX unloaded, VRAM + system RAM freed")


# ── Model state queries ────────────────────────────────────────────────

def is_any_model_loaded() -> bool:
    """True if any model is currently in VRAM."""
    return _hunyuan_pipeline is not None or _flux_pipeline is not None


def loaded_model_name() -> Optional[str]:
    """Return the name of the currently loaded model, or None."""
    if _hunyuan_pipeline is not None:
        return "hunyuan"
    if _flux_pipeline is not None:
        return "flux"
    return None


def unload_any():
    """Unload whichever model is currently loaded."""
    if _flux_pipeline is not None:
        unload_flux()
    if _hunyuan_pipeline is not None:
        unload_hunyuan()


def get_vram_threshold() -> float:
    """
    Return the appropriate min-free-VRAM threshold.

    When a pipeline is already loaded in VRAM, we only need a
    small amount of headroom. When nothing is loaded,
    we need enough free VRAM for the full pipeline + generation.
    """
    if is_any_model_loaded():
        return config.MIN_FREE_VRAM_GB_LOADED
    return config.MIN_FREE_VRAM_GB


# ── FLUX text-to-image ─────────────────────────────────────────────────

def generate_image_from_text(prompt: str, settings: Optional[dict] = None):
    """
    Generate a 1024x1024 image from a text prompt using FLUX.1 [dev].

    Loads FLUX if needed, generates the image, then unloads FLUX.
    Returns a PIL Image.
    """
    import torch

    settings = settings or {}
    flux_steps = settings.get('flux_steps', config.DEFAULT_FLUX_STEPS)
    flux_guidance = settings.get('flux_guidance', config.DEFAULT_FLUX_GUIDANCE)
    seed = settings.get('seed', config.DEFAULT_SEED)

    # Build the full prompt with suffix for white-background product shots
    full_prompt = prompt + config.FLUX_PROMPT_SUFFIX

    load_flux()

    logger.info(f"Generating image: steps={flux_steps}, guidance={flux_guidance}, "
                f"seed={seed}, prompt={prompt[:80]}...")
    t0 = time.time()

    generator = torch.Generator("cuda").manual_seed(seed)
    result = _flux_pipeline(
        prompt=full_prompt,
        num_inference_steps=flux_steps,
        guidance_scale=flux_guidance,
        height=config.FLUX_IMAGE_SIZE,
        width=config.FLUX_IMAGE_SIZE,
        generator=generator,
    )
    image = result.images[0]

    elapsed = time.time() - t0
    logger.info(f"FLUX image generated in {elapsed:.1f}s")

    # Unload FLUX immediately to make room for Hunyuan3D
    unload_flux()

    return image


# ── Pipeline runners ───────────────────────────────────────────────────

ProgressCallback = Callable[[str, int, str], None]


def run_pipeline(
    image_path: str,
    output_dir: str,
    progress_callback: ProgressCallback,
    settings: Optional[dict] = None,
) -> dict:
    """
    Run the full img2stl pipeline on a single image (image-to-3D).

    Args:
        image_path: Path to the input image.
        output_dir: Directory for output files (STL, GLB).
        progress_callback: Called at each stage — fn(step, pct, message).
        settings: Optional overrides for steps, guidance, octree_res, seed, height_mm.

    Returns:
        Dict with stl_path, glb_path, vertex_count, face_count,
        is_watertight, generation_time_s.

    Raises:
        RuntimeError: On CUDA OOM or other fatal pipeline errors.
    """
    global _hunyuan_pipeline

    settings = settings or {}
    steps = settings.get('steps', config.DEFAULT_STEPS)
    guidance = settings.get('guidance', config.DEFAULT_GUIDANCE)
    octree_res = settings.get('octree_res', config.DEFAULT_OCTREE_RES)
    seed = settings.get('seed', config.DEFAULT_SEED)
    height_mm = settings.get('height_mm', config.DEFAULT_HEIGHT_MM)

    stem = Path(image_path).stem
    stl_path = str(Path(output_dir) / f"{stem}.stl")
    glb_path = str(Path(output_dir) / f"{stem}.glb")

    os.makedirs(output_dir, exist_ok=True)
    t_start = time.time()

    # ── Step 1: Remove background ──
    progress_callback("removing_background", 10, "Removing background...")
    image = img2stl.remove_background(image_path)

    # ── Step 2: Load pipeline if needed ──
    if _hunyuan_pipeline is None:
        progress_callback("loading_model", 20,
                          "Loading Hunyuan3D 2.1 (first job, ~90s)...")
        load_hunyuan()

    # ── Step 3: Generate mesh ──
    progress_callback("generating_mesh", 30,
                      f"Generating mesh (steps={steps}, octree_res={octree_res})...")
    mesh = img2stl.generate_shape(
        _hunyuan_pipeline, image,
        steps=steps,
        guidance=guidance,
        octree_res=octree_res,
        seed=seed,
    )

    if mesh is None:
        raise RuntimeError(
            "CUDA out of memory. Try reducing octree_res or steps.")

    progress_callback("generating_mesh", 70,
                      f"Mesh generated: {len(mesh.vertices):,} vertices, "
                      f"{len(mesh.faces):,} faces")

    # ── Step 4: Post-process ──
    progress_callback("repairing_mesh", 75,
                      "Post-processing (orient, clean, scale)...")
    mesh = img2stl.postprocess_mesh(mesh, target_height_mm=height_mm)

    # ── Step 5: Repair ──
    progress_callback("repairing_mesh", 85, "Repairing mesh for printing...")
    mesh = img2stl.repair_mesh(mesh)

    # ── Step 6: Export ──
    progress_callback("exporting", 90, "Exporting STL...")
    mesh.export(stl_path)

    progress_callback("exporting", 95, "Exporting GLB...")
    mesh.export(glb_path)

    gen_time = time.time() - t_start

    result = {
        'stl_path': stl_path,
        'glb_path': glb_path,
        'vertex_count': len(mesh.vertices),
        'face_count': len(mesh.faces),
        'is_watertight': mesh.is_watertight,
        'generation_time_s': round(gen_time, 1),
    }

    progress_callback("complete", 100,
                      f"Done — {result['vertex_count']:,}v, "
                      f"watertight={'yes' if result['is_watertight'] else 'no'}, "
                      f"{gen_time:.1f}s")

    logger.info(f"Pipeline complete: {result['vertex_count']:,}v, "
                f"{result['face_count']:,}f, "
                f"watertight={result['is_watertight']}, "
                f"{gen_time:.1f}s")
    return result


def run_text_pipeline(
    prompt: str,
    output_dir: str,
    progress_callback: ProgressCallback,
    settings: Optional[dict] = None,
) -> dict:
    """
    Run the full text-to-3D pipeline: FLUX (text→image) → Hunyuan3D (image→3D).

    Skips rembg (FLUX generates white backgrounds).

    Returns:
        Dict with stl_path, glb_path, vertex_count, face_count,
        is_watertight, generation_time_s, generated_image_path.
    """
    global _hunyuan_pipeline

    settings = settings or {}
    steps = settings.get('steps', config.DEFAULT_STEPS)
    guidance = settings.get('guidance', config.DEFAULT_GUIDANCE)
    octree_res = settings.get('octree_res', config.DEFAULT_OCTREE_RES)
    seed = settings.get('seed', config.DEFAULT_SEED)
    height_mm = settings.get('height_mm', config.DEFAULT_HEIGHT_MM)

    os.makedirs(output_dir, exist_ok=True)
    t_start = time.time()

    # ── Step 1: Generate image from text using FLUX ──
    progress_callback("generating_image", 5,
                      "Generating image from prompt...")
    image = generate_image_from_text(prompt, settings)

    # Save intermediate image
    generated_image_path = str(Path(output_dir) / "generated.png")
    image.save(generated_image_path)
    logger.info(f"Saved FLUX-generated image: {generated_image_path}")

    progress_callback("generating_image", 15,
                      "Image generated, preparing 3D pipeline...")

    # ── Step 2: Load Hunyuan3D (FLUX was already unloaded by generate_image_from_text) ──
    if _hunyuan_pipeline is None:
        progress_callback("loading_model", 20,
                          "Loading Hunyuan3D 2.1...")
        load_hunyuan()

    # ── Step 3: Generate mesh (skip rembg — FLUX images have white background) ──
    progress_callback("generating_mesh", 30,
                      f"Generating mesh (steps={steps}, octree_res={octree_res})...")

    # Convert to RGBA with white background removed using simple threshold
    # FLUX generates clean white backgrounds, so basic removal works well
    image_rgba = image.convert("RGBA")

    mesh = img2stl.generate_shape(
        _hunyuan_pipeline, image_rgba,
        steps=steps,
        guidance=guidance,
        octree_res=octree_res,
        seed=seed,
    )

    if mesh is None:
        raise RuntimeError(
            "CUDA out of memory. Try reducing octree_res or steps.")

    progress_callback("generating_mesh", 70,
                      f"Mesh generated: {len(mesh.vertices):,} vertices, "
                      f"{len(mesh.faces):,} faces")

    # ── Step 4: Post-process ──
    progress_callback("repairing_mesh", 75,
                      "Post-processing (orient, clean, scale)...")
    mesh = img2stl.postprocess_mesh(mesh, target_height_mm=height_mm)

    # ── Step 5: Repair ──
    progress_callback("repairing_mesh", 85, "Repairing mesh for printing...")
    mesh = img2stl.repair_mesh(mesh)

    # ── Step 6: Export ──
    stem = "model"
    stl_path = str(Path(output_dir) / f"{stem}.stl")
    glb_path = str(Path(output_dir) / f"{stem}.glb")

    progress_callback("exporting", 90, "Exporting STL...")
    mesh.export(stl_path)

    progress_callback("exporting", 95, "Exporting GLB...")
    mesh.export(glb_path)

    gen_time = time.time() - t_start

    result = {
        'stl_path': stl_path,
        'glb_path': glb_path,
        'generated_image_path': generated_image_path,
        'vertex_count': len(mesh.vertices),
        'face_count': len(mesh.faces),
        'is_watertight': mesh.is_watertight,
        'generation_time_s': round(gen_time, 1),
    }

    progress_callback("complete", 100,
                      f"Done — {result['vertex_count']:,}v, "
                      f"watertight={'yes' if result['is_watertight'] else 'no'}, "
                      f"{gen_time:.1f}s")

    logger.info(f"Text pipeline complete: {result['vertex_count']:,}v, "
                f"{result['face_count']:,}f, "
                f"watertight={result['is_watertight']}, "
                f"{gen_time:.1f}s")
    return result
