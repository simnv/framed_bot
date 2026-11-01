"""The pinned CLIP ViT-B/32 vision model. The Dockerfile downloads exactly this file at build time."""

from typing import Final

# Xenova/clip-vit-base-patch32 (MIT), an ONNX export of openai/clip-vit-base-patch32 (MIT), at a fixed commit.
MODEL_URL: Final = (
    'https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/'
    '6b7c523703ae26e39be4b03774966f8571c7f523/onnx/vision_model_fp16.onnx'
)
MODEL_SHA256: Final = '35c4e0fb0aeee527dcde1693520b214a34424a786babd530f35366bad5844efd'
MODEL_BYTES: Final = 176_080_659
DEFAULT_MODEL_PATH: Final = '/opt/clip/vision_model.onnx'
