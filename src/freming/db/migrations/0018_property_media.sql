-- =====================================================================
-- 0018_property_media: 手で上げた写真の実体
-- =====================================================================
--
-- 審査UIから予定を手で足せるようにした（2026-08-25）。そのとき選んだ
-- 画像ファイルの置き場。
--
-- **DBに入れる理由は post_media と同じ。** 審査UI（Render）のディスクは
-- 再起動で消えるので、ファイルとして置くと翌朝の投稿で「画像が無い」に
-- なる。取得元URLも無い（手元のファイルなので取り直せない）ため、
-- 実体を持つ以外に方法がない。
--
-- post_media と分ける理由は寿命。post_media は投稿1回ぶんの加工済みで、
-- 出したら消す。こちらは**元画像**で、出し直すときにまた要る。
--
-- 加工（1080×1080）はここには入れない。square_bytes が投稿の直前に
-- かけるので、自動収集の写真とまったく同じ経路を通る。

CREATE TABLE IF NOT EXISTS property_media (
  id INTEGER PRIMARY KEY,
  property_id INTEGER NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
  position    INTEGER NOT NULL,
  mime        TEXT NOT NULL,
  content     BLOB NOT NULL,
  created_at  TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_property_media_pos
  ON property_media(property_id, position);
