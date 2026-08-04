"""「写真なし」プレースホルダ画像の判定。

物件情報サイトは、写真が用意できていない物件にも og:image を返す。
返ってくるのは単色の板で、寸法だけは本物の写真と同じことが多い
（Dream Town は 1280x800 の #D0D0D0）。そのため
images.min_short_edge_px では落ちず、審査UIに灰色の四角が並ぶ。

判定はピクセルのばらつきだけで行う。単色なら標準偏差は 0 になり、
写真であれば被写体が何であれ十分に大きい。ロゴやウォーターマークの
検出はここではしない（それは画像の内容の問題で、別の判断）。
"""

from __future__ import annotations

import io

from PIL import Image, ImageStat

# 各チャンネルの標準偏差の平均がこれ未満なら「絵が無い」とみなす。
# 単色=0、ごく浅いグラデーション=1〜2、実写真は最も平板なもの（曇天の空
# だけを写したような画像）でも 10 を超える。3.0 は取り違えない位置にある。
_FLAT_STDDEV_MAX = 3.0


def is_flat_image(data: bytes, stddev_max: float = _FLAT_STDDEV_MAX) -> bool:
    """画素にばらつきが無い（＝写真が写っていない）かを返す。

    デコードできないデータは False を返す。ここは候補を落とすための
    判定なので、判断がつかないものは通す側に倒す。落とす判断は
    「確かに単色だった」と言えるときだけにする。
    """
    try:
        with Image.open(io.BytesIO(data)) as img:
            # アニメーションGIF等は先頭フレームだけ見る。パレット画像や
            # アルファつきはRGBに寄せてからでないと統計が取れない。
            rgb = img.convert("RGB")
            # 巨大な画像を丸ごと統計に掛ける必要はない。縮小しても単色は
            # 単色のままで、写真のばらつきは残る。
            rgb.thumbnail((256, 256))
            stat = ImageStat.Stat(rgb)
    except Exception:  # noqa: BLE001 - Pillow は壊れた画像に多様な例外を投げる
        return False

    return (sum(stat.stddev) / len(stat.stddev)) < stddev_max
