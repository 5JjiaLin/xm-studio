"""Optional offline score generation using the locally installed ACE-Step runtime."""

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("checkpoint_dir", type=Path)
parser.add_argument("request", type=Path)
parser.add_argument("output", type=Path)
args = parser.parse_args()
workspace = args.output.parent / ".ace"
workspace.mkdir(exist_ok=True)
os.environ.update(
    ACESTEP_CHECKPOINTS_DIR=str(args.checkpoint_dir),
    ACESTEP_PROJECT_ROOT=str(workspace),
    HF_HOME=str(workspace / "hf"),
    MODELSCOPE_CACHE=str(workspace / "modelscope"),
    MPLCONFIGDIR=str(workspace / "matplotlib"),
    HF_HUB_OFFLINE="1",
    TRANSFORMERS_OFFLINE="1",
    HF_HUB_DISABLE_TELEMETRY="1",
    DO_NOT_TRACK="1",
    TOKENIZERS_PARALLELISM="false",
)

from acestep.handler import AceStepHandler
from acestep.inference import GenerationConfig, GenerationParams, generate_music


class OfflineHandler(AceStepHandler):
    def _ensure_models_present(self, **kwargs):
        return None


request = json.loads(args.request.read_text(encoding="utf-8"))
params = GenerationParams(
    caption=request["prompt"],
    lyrics="[Instrumental]",
    instrumental=True,
    duration=request["duration"],
    inference_steps=8,
    thinking=False,
    use_cot_caption=False,
    use_cot_metas=False,
    use_cot_language=False,
)
handler = OfflineHandler()
message, ready = handler.initialize_service(
    project_root=str(workspace),
    config_path="acestep-v15-turbo",
    device="mps",
    use_mlx_dit=True,
)
if not ready:
    raise RuntimeError(message)
result = generate_music(
    handler, None, params, GenerationConfig(batch_size=1, audio_format="wav32"), save_dir=str(workspace)
)
if not result.success or len(result.audios) != 1:
    raise RuntimeError(result.error or result.status_message)
audio = Path(result.audios[0]["path"]).resolve()
if not audio.is_relative_to(workspace.resolve()):
    raise RuntimeError("Unexpected music output location")
pending = args.output.with_suffix(".pending.wav")
ffmpeg = shutil.which("ffmpeg")
if not ffmpeg:
    raise RuntimeError("ffmpeg not available")
subprocess.run(
    [ffmpeg, "-nostdin", "-v", "error", "-y", "-i", str(audio), "-c:a", "pcm_s16le", str(pending)],
    check=True,
)
pending.replace(args.output)
