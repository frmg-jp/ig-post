-- =====================================================================
-- 0006: 承認から納品までの自動化に使う列
--
-- 承認済みを自動で拾って納品するようになると、失敗した候補を無限に
-- 拾い直す危険がある（相手サイトへの無駄なリクエストになる）。
-- 試行回数と直近の失敗を記録し、上限に達したら自動では触らず
-- 審査UIから人が再試行する。
-- =====================================================================

ALTER TABLE properties ADD COLUMN delivery_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE properties ADD COLUMN delivery_error TEXT;
ALTER TABLE properties ADD COLUMN delivery_attempted_at TEXT;

-- 自動納品の対象を拾うクエリ用
CREATE INDEX idx_properties_delivery_queue
  ON properties(status, delivery_attempts, delivery_attempted_at);
