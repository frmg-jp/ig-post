-- =====================================================================
-- 0022: 販売状況（Listing Status）と MLS番号
--
-- 編集方針（WEEKLY GLOBAL ARCHITECTURE REPORT）の要求:
--
--   「現在買える」と記載する物件については、紹介記事だけを根拠にしては
--   いけない。記事が存在するだけでは Active と判断しない。
--
-- いまの is_for_sale は**記事に売出の signal があるか**の判定で、記事が
-- 書かれた時点の話でしかない。半年前の記事が残っていれば、売れていても
-- 「販売中」に見える。
--
-- listing_status は**実際の掲載ページを見て**入れる。見ていないものは
-- NULL のまま——「確認していない」を「販売中」に丸めない。
--
--   active / pending / sold / off_market / unknown（見たが読み取れない）
--
-- 自動収集が禁止されているサイト（Zillow / Redfin / Compass …）は
-- こちらから開かないので、そこにしか無い物件は NULL のままになる。
-- 編集方針も「確認できない場合は Current availability unconfirmed と
-- 明記する」と言っていて、それに合わせてある。
-- =====================================================================

ALTER TABLE properties ADD COLUMN listing_status TEXT;
ALTER TABLE properties ADD COLUMN listing_status_at TEXT;      -- いつ確認したか
ALTER TABLE properties ADD COLUMN listing_status_note TEXT;    -- 何を根拠にしたか
ALTER TABLE properties ADD COLUMN mls_number TEXT;

CREATE INDEX idx_properties_listing_status ON properties(listing_status);
