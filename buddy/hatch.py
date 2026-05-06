"""Buddy hatch scaffolding.

The hatch flow records prompts and pack metadata for an in-app Buddy creation
experience. User-facing custom Rive import/export is intentionally out of scope
for this phase; generated looks become the live procedurally animated Buddy art.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import time
import base64
from dataclasses import asdict, dataclass, field
from typing import Any

from .config import get_buddy_config, save_buddy_config

_DATA_DIR = pathlib.Path(os.environ.get("THOTH_DATA_DIR", pathlib.Path.home() / ".thoth")) / "buddy_hatches"


@dataclass(frozen=True)
class MotionClipSpec:
    id: str
    label: str
    filename: str
    duration_seconds: int
    animations: tuple[str, ...]
    cue: str


MOTION_CLIP_SPECS: tuple[MotionClipSpec, ...] = (
    MotionClipSpec(
        id="idle",
        label="Idle",
        filename="idle.mp4",
        duration_seconds=4,
        animations=("idle_breathe", "wake", "ping", "listen"),
        cue="gentle breathing, blinking, a soft curious bob, and a calm ready posture",
    ),
    MotionClipSpec(
        id="thinking",
        label="Thinking",
        filename="thinking.mp4",
        duration_seconds=4,
        animations=("lean_in", "think_loop", "type_follow"),
        cue="leaning in with focused eyes, subtle thinking beats, small attentive head tilts, and quiet concentration",
    ),
    MotionClipSpec(
        id="working",
        label="Working",
        filename="working.mp4",
        duration_seconds=4,
        animations=("tool_peek", "pack_bag", "step_check", "nod"),
        cue="busy helpful tool-use motion, tiny checking gestures, quick nods, and purposeful little movements",
    ),
    MotionClipSpec(
        id="approval",
        label="Approval",
        filename="approval.mp4",
        duration_seconds=4,
        animations=("tap_glass",),
        cue="politely getting attention with a small tap, alert eyes, and a waiting-for-approval posture",
    ),
    MotionClipSpec(
        id="success",
        label="Success",
        filename="success.mp4",
        duration_seconds=4,
        animations=("celebrate_small", "celebrate_big"),
        cue="a compact joyful celebration, bright eyes, small bounce, and restrained magical sparkle",
    ),
    MotionClipSpec(
        id="error",
        label="Error",
        filename="error.mp4",
        duration_seconds=4,
        animations=("worry", "sleep", "pause"),
        cue="concerned but gentle recovery motion, lowered energy, worried eyes, a quiet pause, and a small apologetic wobble",
    ),
)

MOTION_ANIMATION_MAP: dict[str, str] = {
    animation: spec.id for spec in MOTION_CLIP_SPECS for animation in spec.animations
}


@dataclass(frozen=True)
class HatchDraft:
    id: str
    prompt: str
    pack_id: str
    status: str
    created_at: float
    notes: str
    preview_path: str = ""
    motion_path: str = ""
    active_motion_path: str = ""
    motion_pack_path: str = ""
    active_motion_pack_path: str = ""
    generation_result: str = ""
    motion_clips: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def create_hatch_draft(prompt: str, *, pack_id: str = "glyph") -> HatchDraft:
    safe_prompt = (prompt or "A cute tiny app companion named Buddy").strip()
    draft = HatchDraft(
        id=f"hatch-{int(time.time())}",
        prompt=safe_prompt,
        pack_id=pack_id,
        status="draft",
        created_at=time.time(),
        notes="Draft saved. Next step is generating live Buddy art in-app.",
    )
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    (_DATA_DIR / f"{draft.id}.json").write_text(json.dumps(draft.to_dict(), indent=2), encoding="utf-8")
    cfg = get_buddy_config()
    cfg["hatch_prompt"] = safe_prompt
    save_buddy_config(cfg)
    return draft


def _buddy_image_prompt(prompt: str) -> str:
    return (
        "Create a single animated-app companion character named Buddy. "
        "The user concept is the source of truth for the theme, palette, materials, and era; "
        "do not force ancient, mystical, ink, gold, teal, glyph, or Thoth-like motifs unless "
        "the user explicitly asks for them. The character should be warm, expressive, compact, "
        "friendly but not childish, with readable eyes and a simple silhouette that works "
        "at 96px. Use clear contrast and a readable rim light or outline separating every dark body edge. Show only one full character "
        "centered with at least 18 percent empty margin on every side. Use a transparent "
        "background if supported; otherwise use a flat solid keyable background color "
        "that is clearly distinct from the character. No body, robe, feet, glow, or shadow may "
        "touch the image frame edge. "
        "No text, no UI, no logo, no extra characters. User concept: "
        f"{prompt}"
    )


def motion_clip_specs() -> tuple[MotionClipSpec, ...]:
    return MOTION_CLIP_SPECS


def _motion_clip_spec(clip_id: str) -> MotionClipSpec:
    for spec in MOTION_CLIP_SPECS:
        if spec.id == clip_id:
            return spec
    return MOTION_CLIP_SPECS[0]


def _is_rate_limited_generation_result(result: str) -> bool:
    lowered = (result or "").lower()
    return "429" in lowered or "resource_exhausted" in lowered or "too many requests" in lowered


def _configured_video_model() -> str:
    try:
        from tools import registry

        return str(registry.get_tool_config("video_gen", "model", ""))
    except Exception:
        return ""


def _motion_request_spacing_seconds(animate_image_func: Any | None = None) -> float:
    if animate_image_func is not None and getattr(animate_image_func, "__module__", "") != "tools.video_gen_tool":
        return 0.0
    model = _configured_video_model().lower()
    if "google/veo" not in model:
        return 0.0
    try:
        return max(0.0, float(os.environ.get("THOTH_BUDDY_GOOGLE_VIDEO_SPACING_SECONDS", "16")))
    except ValueError:
        return 16.0


def _wait_for_motion_request_slot(previous_request_started_at: float | None, animate_image_func: Any | None = None) -> None:
    spacing_seconds = _motion_request_spacing_seconds(animate_image_func)
    if not previous_request_started_at or spacing_seconds <= 0:
        return
    remaining = spacing_seconds - (time.time() - previous_request_started_at)
    if remaining > 0:
        time.sleep(remaining)


def _buddy_motion_prompt(prompt: str, clip_id: str = "idle") -> str:
    spec = _motion_clip_spec(clip_id)
    return (
        f"Animate this exact Buddy character as the {spec.label} state in a seamless app companion loop. "
        "Keep the same character identity and centered composition from the source image. "
        f"Action direction: {spec.cue}. "
        "Keep the source image's theme, palette, materials, and personality; do not add ancient, mystical, ink, gold, teal, glyph, or Thoth-like motifs unless they are already part of the source design. "
        "Keep the character compact enough to read at sidebar size. "
        "Keep at least 18 percent empty margin around the full character for the entire clip, "
        "with no body, robe, feet, glow, or shadow touching the frame edge. Preserve a flat solid "
        "keyable background that is clearly distinct from the character, and keep a readable rim "
        "light or outline around dark body edges. No scene cuts, no text, no logo, no extra "
        "characters, no dramatic camera movement, no background clutter. "
        "The motion should feel alive, loopable, and suitable for a small sidebar avatar. User concept: "
        f"{prompt}"
    )


def _write_motion_pack_manifest(
    manifest_path: pathlib.Path,
    *,
    prompt: str,
    pack_id: str,
    clips: dict[str, pathlib.Path],
    created_at: float,
) -> pathlib.Path:
    clip_entries: dict[str, dict[str, Any]] = {}
    for spec in MOTION_CLIP_SPECS:
        clip_path = clips.get(spec.id)
        if not clip_path:
            continue
        clip_entries[spec.id] = {
            "id": spec.id,
            "label": spec.label,
            "path": clip_path.name,
            "duration_seconds": spec.duration_seconds,
            "animations": list(spec.animations),
        }
    manifest = {
        "schema": 1,
        "prompt": prompt,
        "pack_id": pack_id,
        "created_at": created_at,
        "default_clip": "idle",
        "animation_map": dict(MOTION_ANIMATION_MAP),
        "clips": clip_entries,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return manifest_path


def _read_motion_pack_manifest(manifest_path: str | pathlib.Path) -> dict[str, Any]:
    path = pathlib.Path(manifest_path).expanduser().resolve()
    raw = json.loads(path.read_text(encoding="utf-8"))
    return raw if isinstance(raw, dict) else {}


def activate_hatch_art(preview_path: str | pathlib.Path) -> pathlib.Path:
    """Copy a generated Hatch preview into the served live Buddy art slot."""

    source = pathlib.Path(preview_path).expanduser().resolve()
    if not source.exists() or not source.is_file() or source.stat().st_size == 0:
        raise ValueError("Buddy art preview is missing or empty")

    from buddy import assets as assets_mod

    active_dir = assets_mod.buddy_static_dir() / "generated"
    active_dir.mkdir(parents=True, exist_ok=True)
    active_path = active_dir / "current.png"
    shutil.copy2(source, active_path)
    cfg = get_buddy_config()
    cfg["active_hatch_preview"] = str(active_path)
    save_buddy_config(cfg)
    return active_path


def activate_hatch_motion(motion_path: str | pathlib.Path) -> pathlib.Path:
    """Copy a generated Hatch motion clip into the served live Buddy slot."""

    source = pathlib.Path(motion_path).expanduser().resolve()
    if not source.exists() or not source.is_file() or source.stat().st_size == 0:
        raise ValueError("Buddy motion preview is missing or empty")

    from buddy import assets as assets_mod

    active_dir = assets_mod.buddy_static_dir() / "generated"
    active_dir.mkdir(parents=True, exist_ok=True)
    active_path = active_dir / "current.mp4"
    shutil.copy2(source, active_path)
    cfg = get_buddy_config()
    cfg["active_hatch_motion"] = str(active_path)
    save_buddy_config(cfg)
    return active_path


def activate_hatch_motion_pack(manifest_path: str | pathlib.Path) -> pathlib.Path:
    """Copy a generated Hatch motion pack into the served live Buddy slots."""

    source_manifest = pathlib.Path(manifest_path).expanduser().resolve()
    if not source_manifest.exists() or not source_manifest.is_file():
        raise ValueError("Buddy motion pack manifest is missing")
    manifest = _read_motion_pack_manifest(source_manifest)
    clips = manifest.get("clips") if isinstance(manifest.get("clips"), dict) else {}
    if not clips:
        raise ValueError("Buddy motion pack has no clips")

    from buddy import assets as assets_mod

    active_dir = assets_mod.buddy_static_dir() / "generated"
    active_motion_dir = active_dir / "motions"
    active_motion_dir.mkdir(parents=True, exist_ok=True)

    active_clips: dict[str, str] = {}
    active_manifest = dict(manifest)
    active_manifest["clips"] = {}
    for clip_id, entry in clips.items():
        if not isinstance(entry, dict):
            continue
        rel_path = pathlib.Path(str(entry.get("path") or f"{clip_id}.mp4"))
        source_clip = (source_manifest.parent / rel_path).resolve()
        if not source_clip.exists() or source_clip.stat().st_size == 0:
            raise ValueError(f"Buddy motion clip is missing: {clip_id}")
        filename = f"{clip_id}.mp4"
        target_clip = active_motion_dir / filename
        if source_clip != target_clip.resolve():
            shutil.copy2(source_clip, target_clip)
        active_clips[str(clip_id)] = str(target_clip)
        active_entry = dict(entry)
        active_entry["path"] = filename
        active_manifest["clips"][str(clip_id)] = active_entry

    default_clip = str(active_manifest.get("default_clip") or "idle")
    default_motion = pathlib.Path(active_clips.get(default_clip) or next(iter(active_clips.values())))
    current_motion = active_dir / "current.mp4"
    if default_motion.resolve() != current_motion.resolve():
        shutil.copy2(default_motion, current_motion)

    active_manifest_path = active_motion_dir / "manifest.json"
    active_manifest_path.write_text(json.dumps(active_manifest, indent=2, sort_keys=True), encoding="utf-8")

    cfg = get_buddy_config()
    cfg["active_hatch_motion"] = str(current_motion)
    cfg["active_hatch_motion_pack"] = str(active_manifest_path)
    cfg["active_hatch_motion_clips"] = active_clips
    save_buddy_config(cfg)
    return active_manifest_path


def generate_hatch_preview(prompt: str, *, pack_id: str = "glyph") -> HatchDraft:
    """Generate and persist one Buddy character image and activate it as live art."""

    safe_prompt = (prompt or "A cute tiny app companion named Buddy").strip()
    from tools.image_gen_tool import _generate_image, get_and_clear_last_image

    result = _generate_image(_buddy_image_prompt(safe_prompt), size="1024x1024", quality="auto")
    image_b64 = get_and_clear_last_image()
    if not image_b64:
        raise RuntimeError(result or "Buddy art generation did not return an image")

    created_at = time.time()
    draft_id = f"hatch-{int(created_at)}"
    draft_dir = _DATA_DIR / draft_id
    draft_dir.mkdir(parents=True, exist_ok=True)
    preview_path = draft_dir / "preview.png"
    preview_path.write_bytes(base64.b64decode(image_b64))

    active_path = activate_hatch_art(preview_path)

    draft = HatchDraft(
        id=draft_id,
        prompt=safe_prompt,
        pack_id=pack_id,
        status="preview_generated",
        created_at=created_at,
        notes="Preview generated and activated as Buddy's live procedural animation art.",
        preview_path=str(preview_path),
        generation_result=result,
    )
    (draft_dir / "manifest.json").write_text(json.dumps(draft.to_dict(), indent=2), encoding="utf-8")
    cfg = get_buddy_config()
    cfg["hatch_prompt"] = safe_prompt
    cfg["latest_hatch_preview"] = str(preview_path)
    cfg["active_hatch_preview"] = str(active_path)
    cfg["active_hatch_prompt"] = safe_prompt
    save_buddy_config(cfg)
    return draft


def generate_hatch_motion(prompt: str, preview_path: str | pathlib.Path, *, pack_id: str = "glyph") -> HatchDraft:
    """Generate and activate an image-to-video Buddy motion loop."""

    safe_prompt = (prompt or "A cute tiny app companion named Buddy").strip()
    preview = pathlib.Path(preview_path).expanduser().resolve()
    if not preview.exists() or not preview.is_file() or preview.stat().st_size == 0:
        raise ValueError("Buddy art preview is required before generating motion")

    from tools.video_gen_tool import _animate_image, get_and_clear_last_video, video_output_override

    draft_dir = preview.parent
    with video_output_override(draft_dir, "motion.mp4"):
        result = _animate_image(
            _buddy_motion_prompt(safe_prompt, "idle"),
            image_source=str(preview),
            duration_seconds=4,
            aspect_ratio="1:1",
            resolution="720p",
        )
    video_meta = get_and_clear_last_video() or {}
    motion_path = str(video_meta.get("path") or "")
    if not motion_path and (draft_dir / "motion.mp4").exists():
        motion_path = str(draft_dir / "motion.mp4")
    if not motion_path or not pathlib.Path(motion_path).exists():
        raise RuntimeError(result or "Buddy motion generation did not return a video")

    active_motion = activate_hatch_motion(motion_path)
    draft = HatchDraft(
        id=draft_dir.name,
        prompt=safe_prompt,
        pack_id=pack_id,
        status="motion_generated",
        created_at=time.time(),
        notes="Buddy art and generated motion are active as the live Buddy animation.",
        preview_path=str(preview),
        motion_path=str(motion_path),
        active_motion_path=str(active_motion),
        generation_result=result,
    )
    (draft_dir / "manifest.json").write_text(json.dumps(draft.to_dict(), indent=2), encoding="utf-8")
    cfg = get_buddy_config()
    cfg["hatch_prompt"] = safe_prompt
    cfg["latest_hatch_preview"] = str(preview)
    cfg["latest_hatch_motion"] = str(motion_path)
    cfg["active_hatch_motion"] = str(active_motion)
    cfg["active_hatch_prompt"] = safe_prompt
    cfg.pop("latest_hatch_motion_error", None)
    save_buddy_config(cfg)
    return draft


def generate_hatch_motion_pack(
    prompt: str,
    preview_path: str | pathlib.Path,
    *,
    pack_id: str = "glyph",
    reuse_existing: bool = True,
) -> HatchDraft:
    """Generate and activate a compact state-specific Buddy motion pack."""

    safe_prompt = (prompt or "A cute tiny app companion named Buddy").strip()
    preview = pathlib.Path(preview_path).expanduser().resolve()
    if not preview.exists() or not preview.is_file() or preview.stat().st_size == 0:
        raise ValueError("Buddy art preview is required before generating a motion pack")

    from tools.video_gen_tool import _animate_image, get_and_clear_last_video, video_output_override

    draft_dir = preview.parent
    motion_dir = draft_dir / "motions"
    generated_clips: dict[str, pathlib.Path] = {}
    result_lines: list[str] = []
    previous_request_started_at: float | None = None
    for spec in MOTION_CLIP_SPECS:
        existing_clip = motion_dir / spec.filename
        if reuse_existing and existing_clip.exists() and existing_clip.stat().st_size > 0:
            generated_clips[spec.id] = existing_clip
            result_lines.append(f"{spec.label}: reused existing clip at {existing_clip}")
            continue

        result = ""
        clip_path = ""
        for attempt in range(2):
            _wait_for_motion_request_slot(previous_request_started_at, _animate_image)
            previous_request_started_at = time.time()
            with video_output_override(motion_dir, spec.filename):
                result = _animate_image(
                    _buddy_motion_prompt(safe_prompt, spec.id),
                    image_source=str(preview),
                    duration_seconds=spec.duration_seconds,
                    aspect_ratio="1:1",
                    resolution="720p",
                )
            video_meta = get_and_clear_last_video() or {}
            clip_path = str(video_meta.get("path") or "")
            if not clip_path and (motion_dir / spec.filename).exists():
                clip_path = str(motion_dir / spec.filename)
            if clip_path and pathlib.Path(clip_path).exists():
                break
            if _is_rate_limited_generation_result(result):
                break
            if attempt == 0:
                time.sleep(2)
        if not clip_path or not pathlib.Path(clip_path).exists():
            raise RuntimeError(result or f"Buddy {spec.label} motion generation did not return a video")
        generated_clips[spec.id] = pathlib.Path(clip_path)
        result_lines.append(f"{spec.label}: {result}")

    created_at = time.time()
    manifest_path = _write_motion_pack_manifest(
        motion_dir / "manifest.json",
        prompt=safe_prompt,
        pack_id=pack_id,
        clips=generated_clips,
        created_at=created_at,
    )
    active_manifest = activate_hatch_motion_pack(manifest_path)
    cfg = get_buddy_config()
    active_motion = str(cfg.get("active_hatch_motion") or "")
    active_clips = cfg.get("active_hatch_motion_clips") if isinstance(cfg.get("active_hatch_motion_clips"), dict) else {}

    draft = HatchDraft(
        id=draft_dir.name,
        prompt=safe_prompt,
        pack_id=pack_id,
        status="motion_pack_generated",
        created_at=created_at,
        notes="Buddy art and generated state motion pack are active as the live Buddy animation.",
        preview_path=str(preview),
        motion_path=str(generated_clips.get("idle", "")),
        active_motion_path=active_motion,
        motion_pack_path=str(manifest_path),
        active_motion_pack_path=str(active_manifest),
        generation_result="\n".join(result_lines),
        motion_clips={str(k): str(v) for k, v in active_clips.items()},
    )
    (draft_dir / "manifest.json").write_text(json.dumps(draft.to_dict(), indent=2), encoding="utf-8")
    cfg = get_buddy_config()
    cfg["hatch_prompt"] = safe_prompt
    cfg["latest_hatch_preview"] = str(preview)
    cfg["latest_hatch_motion"] = str(generated_clips.get("idle", ""))
    cfg["latest_hatch_motion_pack"] = str(manifest_path)
    cfg["active_hatch_motion_pack"] = str(active_manifest)
    cfg["active_hatch_prompt"] = safe_prompt
    cfg.pop("latest_hatch_motion_error", None)
    save_buddy_config(cfg)
    return draft


def generate_hatch_buddy(prompt: str, *, pack_id: str = "glyph") -> HatchDraft:
    """Generate Buddy art, then generate and activate a full motion pack."""

    preview = generate_hatch_preview(prompt, pack_id=pack_id)
    try:
        return generate_hatch_motion_pack(prompt, preview.preview_path, pack_id=pack_id)
    except Exception as exc:
        draft_dir = pathlib.Path(preview.preview_path).expanduser().resolve().parent
        failed = HatchDraft(
            id=preview.id,
            prompt=preview.prompt,
            pack_id=preview.pack_id,
            status="motion_pack_failed",
            created_at=preview.created_at,
            notes="Buddy art is active, but generated motion pack failed. Check video generation provider settings.",
            preview_path=preview.preview_path,
            generation_result=f"{preview.generation_result}\nMotion pack failed: {exc}",
        )
        (draft_dir / "manifest.json").write_text(json.dumps(failed.to_dict(), indent=2), encoding="utf-8")
        cfg = get_buddy_config()
        cfg["latest_hatch_motion_error"] = str(exc)
        save_buddy_config(cfg)
        return failed
