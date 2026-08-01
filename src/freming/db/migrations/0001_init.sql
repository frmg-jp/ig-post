-- =====================================================================
-- 0001_init: 初期スキーマ
-- =====================================================================

-- 候補物件 -----------------------------------------------------------
CREATE TABLE properties (
  id INTEGER PRIMARY KEY,
  source            TEXT NOT NULL,   -- 取得元サイト（config の key）
  source_rank       TEXT,            -- S / A / B
  source_url        TEXT UNIQUE NOT NULL,
  title             TEXT,
  location_city     TEXT,
  location_country  TEXT,
  price             TEXT,            -- 原文のまま保持（通貨混在のため）
  is_for_sale       INTEGER,         -- 1 = 販売中と判定
  for_sale_evidence TEXT,            -- 販売中と判断した根拠テキスト
  genre             TEXT,            -- loft / penthouse / adaptive_reuse / architect / hidden_gem
  architect         TEXT,
  year_built        TEXT,
  summary           TEXT,            -- なぜ選んだかの一言（80字以内）
  score             REAL,            -- 0-100
  score_reason      TEXT,
  thumbnail_url     TEXT,
  status            TEXT DEFAULT 'pending',  -- pending / approved / rejected / delivered
  reject_reason     TEXT,
  collected_at      TEXT,
  reviewed_at       TEXT
);

CREATE INDEX idx_properties_status_score ON properties(status, score DESC);
CREATE INDEX idx_properties_source ON properties(source);
CREATE INDEX idx_properties_collected_at ON properties(collected_at DESC);

-- 学習用: 非承認理由の蓄積 --------------------------------------------
CREATE TABLE feedback (
  id INTEGER PRIMARY KEY,
  property_id  INTEGER REFERENCES properties(id) ON DELETE SET NULL,
  reason       TEXT NOT NULL,
  reason_tag   TEXT,        -- LLMが理由を分類したタグ（未分類は NULL）
  created_at   TEXT
);

CREATE INDEX idx_feedback_created_at ON feedback(created_at DESC);
CREATE INDEX idx_feedback_tag ON feedback(reason_tag);

-- 納品記録 -------------------------------------------------------------
CREATE TABLE deliveries (
  id INTEGER PRIMARY KEY,
  property_id     INTEGER NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
  folder_name     TEXT NOT NULL,   -- frmg_ig001 形式
  image_count     INTEGER,
  drive_folder_id TEXT,
  delivered_at    TEXT
);

-- 再実行時の重複納品防止
CREATE UNIQUE INDEX idx_deliveries_property ON deliveries(property_id);
CREATE UNIQUE INDEX idx_deliveries_folder_name ON deliveries(folder_name);

-- 画像（取得元URLとクレジットを必ず記録する） --------------------------
CREATE TABLE images (
  id INTEGER PRIMARY KEY,
  property_id INTEGER NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
  source_url  TEXT NOT NULL,     -- 取得元URL
  credit      TEXT,              -- 判明した場合のクレジット情報
  width       INTEGER,
  height      INTEGER,
  local_path  TEXT,              -- ダウンロード直後のファイル
  output_path TEXT,              -- 1080x1080 加工後のファイル
  position    INTEGER,           -- 01.jpg 〜 10.jpg の並び順（1始まり）
  fetched_at  TEXT
);

CREATE UNIQUE INDEX idx_images_property_url ON images(property_id, source_url);
CREATE INDEX idx_images_property_position ON images(property_id, position);

-- 承認後のバックグラウンド処理（審査UIの進捗表示用） --------------------
CREATE TABLE jobs (
  id INTEGER PRIMARY KEY,
  property_id INTEGER NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
  kind        TEXT NOT NULL,   -- fetch_images / process_images / deliver
  state       TEXT NOT NULL,   -- queued / running / done / failed
  progress    TEXT,            -- 例: "画像 4/10 取得中"
  error       TEXT,            -- 例外は握りつぶさずここにも残す
  created_at  TEXT,
  updated_at  TEXT
);

CREATE INDEX idx_jobs_property ON jobs(property_id, id DESC);
CREATE INDEX idx_jobs_state ON jobs(state);

-- 頻出タグ由来の恒久除外ルール候補（自動適用せず人間の承認を挟む） ------
CREATE TABLE rule_candidates (
  id INTEGER PRIMARY KEY,
  reason_tag  TEXT NOT NULL UNIQUE,
  hit_count   INTEGER NOT NULL DEFAULT 0,
  proposal    TEXT,                        -- LLMが生成した除外ルール文
  state       TEXT NOT NULL DEFAULT 'proposed',  -- proposed / approved / dismissed
  created_at  TEXT,
  decided_at  TEXT
);

CREATE INDEX idx_rule_candidates_state ON rule_candidates(state, hit_count DESC);
