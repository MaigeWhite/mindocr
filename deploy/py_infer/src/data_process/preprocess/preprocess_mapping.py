import functools

from . import transforms

# other ops node will be skipped
PREPROCESS_MAPPING_OPS = {
    # general
    "DecodeImage": transforms.DecodeImage,
    "NormalizeImage": transforms.NormalizeImage,
    "ToCHWImage": transforms.ToCHWImage,
    "GridResize": functools.partial(transforms.DetResize, keep_ratio=False, padding=False),
    "ScalePadImage": transforms.ScalePadImage,
    # det
    "DetResize": transforms.DetResize,
    "DetResizeNormForInfer": transforms.DetResizeNormForInfer,
    # rec
    "SVTRRecResizeImg": transforms.SVTRRecResizeImg,
    "RecResizeNormForInfer": transforms.RecResizeNormForInfer,
    "RecResizeNormForViTSTR": transforms.RecResizeNormForViTSTR,
    "RecResizeNormForMMOCR": transforms.RecResizeNormForMMOCR,
    # cls
    "ClsResizeNormForInfer": transforms.ClsResizeNormForInfer,
}
