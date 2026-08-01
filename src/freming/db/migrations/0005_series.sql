-- =====================================================================
-- 0005: 連載企画のラベル
--
-- 「FREMING Pick」「Hidden Gem」などの企画にどれを載せるかは編集判断で、
-- 記事本文から機械的に決まらない。スコアリングでは判定せず、審査UIで
-- 人が付ける。値は config.yaml の series[].key。
-- =====================================================================

ALTER TABLE properties ADD COLUMN series TEXT;

CREATE INDEX idx_properties_series ON properties(series, score DESC);
