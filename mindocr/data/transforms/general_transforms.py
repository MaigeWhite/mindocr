from typing import List, Union
import cv2
import numpy as np
from shapely.geometry import Polygon
import random
from PIL import Image
from mindspore.dataset.vision import RandomColorAdjust as MSRandomColorAdjust, ToPIL
import sys
from ...data.constants import IMAGENET_DEFAULT_MEAN, IMAGENET_DEFAULT_STD

__all__ = ['DecodeImage', 'NormalizeImage', 'ToCHWImage', 'PackLoaderInputs', 'ScalePadImage', 'GridResize',
           'RandomScale', 'RandomCropWithBBox', 'RandomColorAdjust']


# TODO: use mindspore C.decode for efficiency
class DecodeImage:
    """
    img_mode (str): The channel order of the output, 'BGR' and 'RGB'. Default to 'BGR'.
    channel_first (bool): if True, image shpae is CHW. If False, HWC. Default to False
    """

    def __init__(self, img_mode='BGR', channel_first=False, to_float32=False, ignore_orientation=False, **kwargs):
        self.img_mode = img_mode
        self.to_float32 = to_float32
        self.channel_first = channel_first
        self.flag = cv2.IMREAD_IGNORE_ORIENTATION | cv2.IMREAD_COLOR if ignore_orientation else cv2.IMREAD_COLOR

    def __call__(self, data):
        if 'img_path' in data:
            with open(data['img_path'], 'rb') as f:
                img = f.read()
        elif 'img_lmdb' in data:
            img = data["img_lmdb"]
        img = np.frombuffer(img, dtype='uint8')
        img = cv2.imdecode(img, self.flag)

        if self.img_mode == 'RGB':
            img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

        if self.channel_first:
            img = img.transpose((2, 0, 1))

        if self.to_float32:
            img = img.astype('float32')
        data['image'] = img
        # data['ori_image'] = img.copy()
        return data


class NormalizeImage:
    """
    normalize image, substract mean, divide std
    input image: by default, np.uint8, [0, 255], HWC format.
    return image: float32 numpy array
    """

    def __init__(self, mean: Union[List[float], str] = 'imagenet', std: Union[List[float], str] = 'imagenet',
                 is_hwc=True, bgr_to_rgb=False, rgb_to_bgr=False, **kwargs):
        # By default, imagnet MEAN and STD is in RGB order. inverse if input image is in BGR mode
        self._channel_conversion = False
        if bgr_to_rgb or rgb_to_bgr:
            self._channel_conversion = True

        # TODO: detect hwc or chw automatically
        shape = (3, 1, 1) if not is_hwc else (1, 1, 3)
        self.mean = np.array(self._get_value(mean, 'mean')).reshape(shape).astype('float32')
        self.std = np.array(self._get_value(std, 'std')).reshape(shape).astype('float32')
        self.is_hwc = is_hwc

    def __call__(self, data):
        img = data['image']
        if isinstance(img, Image.Image):
            img = np.array(img)
        assert isinstance(img, np.ndarray), "invalid input 'img' in NormalizeImage"

        if self._channel_conversion:
            if self.is_hwc:
                img = img[..., [2, 1, 0]]
            else:
                img = img[[2, 1, 0], ...]

        data['image'] = (img.astype('float32') - self.mean) / self.std
        return data

    @staticmethod
    def _get_value(val, name):
        if isinstance(val, str) and val.lower() == 'imagenet':
            assert name in ['mean', 'std']
            return IMAGENET_DEFAULT_MEAN if name == 'mean' else IMAGENET_DEFAULT_STD
        elif isinstance(val, list):
            return val
        else:
            raise ValueError(f'Wrong {name} value: {val}')


class ToCHWImage:
    # convert hwc image to chw image
    def __init__(self, **kwargs):
        pass

    def __call__(self, data):
        img = data['image']
        if isinstance(img, Image.Image):
            img = np.array(img)
        data['image'] = img.transpose((2, 0, 1))
        return data


class PackLoaderInputs:
    """
    Args:
        output_columns (list): the keys in data dict that are expected to output for dataloader

    Call:
        input: data dict
        output: data tuple corresponding to the `output_columns`
    """

    def __init__(self, output_columns: List, **kwargs):
        self.output_columns = output_columns

    def __call__(self, data):
        out = []
        for k in self.output_columns:
            assert k in data, f'key {k} does not exists in data, availabe keys are {data.keys()}'
            out.append(data[k])

        return tuple(out)


class ScalePadImage:
    """
    Scale image and polys by the shorter side, then pad to the target_size.
    input image format: hwc

    Args:
        target_size: [H, W] of the output image.
    """

    def __init__(self, target_size: list):
        self._target_size = np.array(target_size)

    def __call__(self, data: dict):
        """
        required keys:
            image, HWC
            (polys)
        modified keys:
            image
            (polys)
        added keys:
            shape: [src_h, src_w, scale_ratio_h, scale_ratio_w]
        """
        size = np.array(data['image'].shape[:2])
        scale = min(self._target_size / size)
        new_size = np.round(scale * size).astype(np.int)

        data['image'] = cv2.resize(data['image'], new_size[::-1])
        data['image'] = np.pad(data['image'],
                               (*tuple((0, ts - ns) for ts, ns in zip(self._target_size, new_size)), (0, 0)))

        if 'polys' in data:
            data['polys'] *= scale

        data['shape'] = np.concatenate((size, np.array([scale, scale])), dtype=np.float32)
        return data


class GridResize:
    """
    Resize image to make it divisible by a specified factor exactly.
    Resize polygons correspondingly, if provided.
    """

    def __init__(self, factor: int = 32):
        self._factor = factor

    def __call__(self, data: dict):
        """
        required keys:
            image, HWC
            (polys)
        modified keys:
            image
            (polys)
        """
        size = np.array(data['image'].shape[:2])
        scale = np.ceil(size / self._factor) * self._factor / size
        data['image'] = cv2.resize(data['image'], None, fx=scale[1], fy=scale[0])

        if 'polys' in data:
            data['polys'] *= scale[::-1]  # w, h order
        return data


class RandomScale:
    """
    Randomly scales an image and its polygons in a predefined scale range.
    Args:
        scale_range: (min, max) scale range.
    """

    def __init__(self, scale_range: Union[tuple, list]):
        self._range = scale_range

    def __call__(self, data: dict):
        """
        required keys:
            image, HWC
            (polys)
        modified keys:
            image
            (polys)
        """
        scale = np.random.uniform(*self._range)
        data['image'] = cv2.resize(data['image'], dsize=None, fx=scale, fy=scale)

        if 'polys' in data:
            data['polys'] *= scale
        return data


class RandomCropWithBBox:
    """
    Randomly cuts a crop from an image along with polygons.

    Args:
        max_tries: number of attempts to try to cut a crop with a polygon in it.
        min_crop_ratio: minimum size of a crop in respect to an input image size.
        crop_size: target size of the crop (resized and padded, if needed), preserves sides ratio.
    """

    def __init__(self, max_tries=10, min_crop_ratio=0.1, crop_size=(640, 640)):
        self._crop_size = crop_size
        self._ratio = min_crop_ratio
        self._max_tries = max_tries

    def __call__(self, data):
        start, end, is_out_bounds = self._find_crop(data)
        scale = min(self._crop_size / (end - start))

        data['image'] = cv2.resize(data['image'][start[0]: end[0], start[1]: end[1]], None, fx=scale, fy=scale)
        start, end = start[::-1], end[::-1]  # convert to x, y coord
        new_polys, new_texts, new_ignores = [], [], []
        for _id in range(len(data['polys'])):
            # if the polygon is within the crop
            if (data['polys'][_id].max(axis=0) > start).all() and (data['polys'][_id].min(axis=0) < end).all():  # NOQA
                new_polys.append((data['polys'][_id] - start) * scale)
                new_texts.append(data['texts'][_id])
                new_ignores.append(data['ignore_tags'][_id])

        data['polys'] = np.array(new_polys) if isinstance(data['polys'], np.ndarray) else new_polys
        data['texts'] = new_texts
        data['ignore_tags'] = new_ignores

        # 处理越界坐标，求出越界坐标与[0, 0, w, 0, w, h, 0, h]相交点的坐标，新的poly不一定是四边形, 
        # det_transforms _validate_polys得重新适配
        if is_out_bounds:
            data = self.hand_bounds(data)
        data['image'] = np.pad(data['image'],
                               (*tuple((0, cs - ds) for cs, ds in zip(self._crop_size, data['image'].shape[:2])),
                                (0, 0)))

        return data

    @staticmethod
    def polygon2numpy(polygon):
        """将polygon对象的位置信息转换成numpy格式的位置信息"""
        x, y = polygon.exterior.coords.xy
        points = np.array([[x, y] for x, y in zip(x, y)], dtype=np.float32)[:-1]
        return points

    def hand_bounds(self, data, thresh_area=8):
        img = data['image']
        h, w, _ = img.shape
        polys = data['polys']
        dontcare = data['ignore_tags']
        crop_polys = Polygon(np.array([0, 0, w, 0, w, h, 0, h], dtype=np.float32).reshape((-1, 2)))

        new_polys = list()
        new_dontcare = list()
        for poly, tag in zip(polys, dontcare):
            poly = Polygon(poly)
            if crop_polys.intersects(poly):
                inter_poly = crop_polys.intersection(poly)
                if inter_poly.area >= thresh_area:
                    inter_poly_numpy = self.polygon2numpy(inter_poly)
                    new_polys.append(inter_poly_numpy)
                    new_dontcare.append(tag)

        data['polys'] = np.array(new_polys)
        data['ignore_tags'] = np.array(new_dontcare)
        return data

    def _find_crop(self, data):
        size = np.array(data['image'].shape[:2])
        polys = [poly for poly, ignore in zip(data['polys'], data['ignore_tags']) if not ignore]
        is_out_bounds = False

        if polys:
            # do not crop through polys => find available "empty" coordinates
            h_array, w_array = np.zeros(size[0], dtype=np.int32), np.zeros(size[1], dtype=np.int32)
            for poly in polys:
                points = np.maximum(np.round(poly).astype(np.int32), 0)
                x_min, x_max = points[:, 0].min(), points[:, 0].max() + 1
                y_min, y_max = points[:, 1].min(), points[:, 1].max() + 1
                w_array[x_min: x_max] = 1
                h_array[y_min: y_max] = 1

                if x_min < 0 or y_min < 0 or x_max > size[1] or y_max > size[0]:
                    is_out_bounds = True
                    return np.array([0, 0]), size, is_out_bounds

            if not h_array.all() and not w_array.all():  # if texts do not occupy full image
                # find available coordinates that don't include text
                h_avail = np.where(h_array == 0)[0]
                w_avail = np.where(w_array == 0)[0]

                min_size = np.ceil(size * self._ratio).astype(np.int32)
                for _ in range(self._max_tries):
                    y = np.sort(np.random.choice(h_avail, size=2))
                    x = np.sort(np.random.choice(w_avail, size=2))
                    start, end = np.array([y[0], x[0]]), np.array([y[1], x[1]])

                    if ((end - start) < min_size).any():  # NOQA
                        continue

                    # check that at least one polygon is within the crop
                    for poly in polys:
                        if (poly.max(axis=0) > start[::-1]).all() and (poly.min(axis=0) < end[::-1]).all():  # NOQA
                            return start, end, is_out_bounds

        # failed to generate a crop or all polys are marked as ignored
        return np.array([0, 0]), size, is_out_bounds


class RandomColorAdjust:
    def __init__(self, brightness=32.0 / 255, saturation=0.5):
        self._jitter = MSRandomColorAdjust(brightness=brightness, saturation=saturation)
        self._pil = ToPIL()

    def __call__(self, data):
        """
        required keys: image
        modified keys: image
        """
        # there's a bug in MindSpore that requires images to be converted to the PIL format first
        data['image'] = np.array(self._jitter(self._pil(data['image'])))
        return data
