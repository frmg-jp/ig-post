-- =====================================================================
-- 0003: スコアの内訳を保存する
--
-- 最終スコアは軸ごとの点数に重みを掛けて合算したもの。合算後の1つの数値
-- だけを残すと、重みを調整したときに「なぜその順位だったか」を後から
-- 検証できない。軸ごとの点数と判断根拠を JSON で残しておく。
--
-- あわせて、承認基準の中核である「前歴が視覚的に残っているか」を
-- 独立した列に出す。審査UIでの絞り込みと、学習ループでの検証に使う。
-- =====================================================================

ALTER TABLE properties ADD COLUMN score_detail TEXT;          -- 軸ごとの内訳（JSON）
ALTER TABLE properties ADD COLUMN provenance_visible INTEGER; -- 1 = 前歴が写真/記述から読み取れる
ALTER TABLE properties ADD COLUMN score_model TEXT;           -- 判定に使ったモデル

CREATE INDEX idx_properties_provenance ON properties(provenance_visible, score DESC);
