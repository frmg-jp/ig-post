-- =====================================================================
-- 0012_spec_fields: 投稿キャプションの仕様欄に出す項目
-- =====================================================================
--
-- 実際の投稿（@frmg.jpn）のキャプションは決まった型を持っている:
--
--   Location / Usage / Architect / Structure /
--   Building Area / Site Area / Built in / Style
--
-- このうち architect・year_built・city・country は既に採点時に抜いていた。
-- 残りの4つは持っていなかったので足す。**採点で1回読んだ記事から
-- まとめて抜く。** 投稿のたびに記事を読み直すと、同じ本文に対して
-- 二重に課金することになる。
--
-- 値は原文の単位のまま入れる（"2,008 sq ft" / "0.82 Acres" / "約187㎡"）。
-- 換算はしない。記事によって sq ft と ㎡ と Acres が混ざるうえ、
-- 換算した数字を載せて誤差が出ると、事実の記載として弱くなる。
-- 並べ替えに使う予定も無いので、数値化する理由がない。
--
-- style は既に style_identified（真偽）を持っているが、あれは
-- 「様式を特定できたか」という採点用の判定。キャプションに出すのは
-- 様式の**名前**（"Mid-Century Modern"）なので別の列にする。

ALTER TABLE properties ADD COLUMN usage_type TEXT;      -- Private Residence など
ALTER TABLE properties ADD COLUMN structure TEXT;       -- Post-and-Beam / Heavy Timber など
ALTER TABLE properties ADD COLUMN building_area TEXT;   -- 原文の単位のまま
ALTER TABLE properties ADD COLUMN site_area TEXT;       -- 原文の単位のまま
ALTER TABLE properties ADD COLUMN style_name TEXT;      -- Mid-Century Modern など
