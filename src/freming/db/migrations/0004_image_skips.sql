-- =====================================================================
-- 0004: 採用しなかった画像URLを覚えておく
--
-- 小さすぎる画像や対象外の形式は images に残らないため、再実行のたびに
-- 同じURLをダウンロードし直していた。相手サイトへ無駄なリクエストを
-- 繰り返さないよう、採用しなかったURLとその理由も記録する。
-- =====================================================================

CREATE TABLE image_skips (
  id INTEGER PRIMARY KEY,
  property_id INTEGER NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
  source_url  TEXT NOT NULL,
  reason      TEXT NOT NULL,   -- too_small / wrong_type / failed
  created_at  TEXT
);

CREATE UNIQUE INDEX idx_image_skips_property_url ON image_skips(property_id, source_url);
