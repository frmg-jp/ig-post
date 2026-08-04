-- 外部APIの長期トークンの置き場（まずは Instagram）。
--
-- .env や GitHub Secrets に置かない理由: リフレッシュのたびに新しい
-- トークンが発行されるため、静的な置き場では更新した値を書き戻せない。
-- 審査UI・定期実行・納品が既に共有しているDBに置けば、更新が1箇所で済む。
CREATE TABLE IF NOT EXISTS api_tokens (
    name         TEXT PRIMARY KEY,
    value        TEXT NOT NULL,
    refreshed_at TEXT NOT NULL,   -- 最後に取得/更新した時刻（ISO・UTC）
    expires_at   TEXT NOT NULL    -- 失効時刻（ISO・UTC）
);
