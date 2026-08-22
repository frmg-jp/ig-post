-- [9] 審査UIの投稿予定から、出す前に手を入れられるようにする（2026-08-22）。
--
--   image_order        この投稿だけの写真の並び。「3,1,2」のように images の
--                      position をカンマで並べる。NULL なら納品と同じ並び。
--                      images 側の position は動かさない — あちらは Drive に
--                      納品済みの 01.jpg〜 と対応しているため。
--   caption_edited_at  人が本文を直した印。付いている行は post replan が
--                      上書きしない（人が直した値を機械が潰さない）。
ALTER TABLE posts ADD COLUMN image_order TEXT;
ALTER TABLE posts ADD COLUMN caption_edited_at TEXT;
