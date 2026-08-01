-- =====================================================================
-- 0002: 収集時に取得した本文を保持する
--
-- スコアリングは記事本文を必要とするが、収集時に一度取得したものを
-- 使い回さないと、同じページへ二度アクセスすることになる。相手サイトへの
-- 負荷を減らすため、収集時の本文をそのまま保存しておく。
-- =====================================================================

ALTER TABLE properties ADD COLUMN content_text TEXT;
ALTER TABLE properties ADD COLUMN signal_score INTEGER;   -- 販売シグナルの合計点
ALTER TABLE properties ADD COLUMN scored_at TEXT;         -- スコアリング実施日時
