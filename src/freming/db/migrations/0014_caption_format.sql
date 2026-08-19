-- [9] 投稿本文の型を実運用の4投稿（2026-08-19 受領）に合わせるための列。
--
--   display_name    「Wade House」のような短い物件名。title は記事の見出しで、
--                   投稿の【 】に入れるには長すぎる
--   caption_body    投稿の説明文（日本語・複数文）。summary は審査UI向けの
--                   短い選定理由なので、投稿の本文には薄い
--   location_region 州・地域。「#CaliforniaArchitecture」のような地域タグに使う
--                   （city と country の間の粒度が無かった）
--
-- どれも記事に書かれている事実から作る。無ければ NULL のままにして、
-- 本文側は title / summary へ落ちる（行を捏造しない）。
ALTER TABLE properties ADD COLUMN display_name TEXT;
ALTER TABLE properties ADD COLUMN caption_body TEXT;
ALTER TABLE properties ADD COLUMN location_region TEXT;
