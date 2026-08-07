-- =====================================================================
-- 0010_posts: Instagram への投稿（[9]）
-- =====================================================================
--
-- 予定を先に行として作り、時間が来たらワーカーが投稿する。
-- 「いつ何が出るか」を人が見られる形にしておきたいので、投稿の直前に
-- 決めるのではなく、3日先まで行として置く（審査UIの /schedule）。
--
-- kind:
--   feed  … 通常投稿。1日3件
--   story … その投稿と同じ写真をストーリーズに。feed の直後に出す
--   reel  … 週1のまとめ。7日ぶんの1位を集めた縦動画
--
-- state の遷移:
--   planned → publishing → published
--                       ↘ failed（attempts を数え、上限で止める）
--   planned → skipped（人が予定表から外した）
--
-- **同じ物件を二度出さない**ことを UNIQUE で担保する。deliveries と同じ考え方。
-- reel は property_id が NULL なので、この制約に引っかからない
-- （SQLite / PostgreSQL とも NULL 同士は重複と見なさない）。

CREATE TABLE IF NOT EXISTS posts (
  id INTEGER PRIMARY KEY,
  property_id      INTEGER REFERENCES properties(id) ON DELETE CASCADE,
  kind             TEXT NOT NULL,              -- feed / story / reel
  state            TEXT NOT NULL DEFAULT 'planned',
  scheduled_at     TEXT NOT NULL,              -- 予定時刻（ISO・UTC）
  caption          TEXT,
  credit           TEXT,                       -- CC BY の音源を使ったときの表記
  parent_post_id   INTEGER REFERENCES posts(id) ON DELETE CASCADE,
  ig_media_id      TEXT,                       -- 投稿後に埋まる
  ig_container_id  TEXT,                       -- 失敗時の追跡用に残す
  reach            INTEGER,                    -- インサイトで後から埋める
  reach_checked_at TEXT,
  attempts         INTEGER NOT NULL DEFAULT 0,
  error            TEXT,
  created_at       TEXT,
  published_at     TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_posts_property_kind
  ON posts(property_id, kind);
CREATE INDEX IF NOT EXISTS idx_posts_due ON posts(state, scheduled_at);
CREATE INDEX IF NOT EXISTS idx_posts_published ON posts(published_at DESC);

-- 投稿する画像・動画の実体 ------------------------------------------------
--
-- **Meta はこちらのサーバーへ取りに来る**（"We cURL media used in publishing
-- attempts"）。Drive のリンクは使えないので、審査UIから配る。DBに置くのは、
-- 審査UI（Render）のディスクが揮発するため。行があれば必ず配れる。
--
-- token は推測できない文字列。審査UIの /m/<token> は認証を通さないので、
-- URLを知られること＝その画像を見られることになる。投稿する写真なので
-- 公開前提だが、それでも連番にはしない。
--
-- 投稿が済んだ行は消す（purge_post_media）。溜めても意味がなく、
-- Neon の容量を食うだけなので。

CREATE TABLE IF NOT EXISTS post_media (
  id INTEGER PRIMARY KEY,
  post_id    INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
  token      TEXT NOT NULL,
  position   INTEGER NOT NULL DEFAULT 1,
  mime       TEXT NOT NULL,
  content    BLOB NOT NULL,
  created_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_post_media_token ON post_media(token);
CREATE INDEX IF NOT EXISTS idx_post_media_post ON post_media(post_id, position);
