-- =====================================================================
-- 0019: style_identified / one_of_a_kind を列に出す
--
-- 2026-09-04 の approval-report（審査済み106件）で、この2つが承認と
-- 非承認をいちばんよく分けていた:
--
--     フラグ                承認   非承認    差
--     style_identified      80%    22%   +58pt
--     one_of_a_kind         72%    32%   +41pt
--     llm_is_for_sale       92%    71%   +22pt
--     provenance_visible    14%     5%    +9pt
--
-- ところが 0003 で列に出したのは provenance_visible だけ——実績では
-- いちばん弱かったもの——で、上の2つは score_detail の JSON の中に
-- しか無かった。列が無いので絞り込みにも使えず、点数にも入っていない。
--
-- 併せて weights.py に軸として足し、config.yaml で重みを持たせる。
--
-- **既存の行は score_detail から埋め戻す。** 値はもう JSON の中にある
-- ので、採点をやり直す（＝API費用を払う）必要はない。JSON の真偽値は
-- Python 側では true/false、SQLite/Postgres のどちらでも json_extract
-- の戻りが実装依存になるため、埋め戻しは文字列一致で見る。
-- =====================================================================

ALTER TABLE properties ADD COLUMN style_identified INTEGER;  -- 1 = 時代・様式が特定できる
ALTER TABLE properties ADD COLUMN one_of_a_kind INTEGER;     -- 1 = 一点物

UPDATE properties
   SET style_identified = CASE
           WHEN score_detail LIKE '%"style_identified": true%'  THEN 1
           WHEN score_detail LIKE '%"style_identified": false%' THEN 0
           ELSE NULL
       END,
       one_of_a_kind = CASE
           WHEN score_detail LIKE '%"one_of_a_kind": true%'  THEN 1
           WHEN score_detail LIKE '%"one_of_a_kind": false%' THEN 0
           ELSE NULL
       END
 WHERE score_detail IS NOT NULL;

-- 審査UIでの絞り込み用。provenance と同じ形にそろえる。
CREATE INDEX idx_properties_style ON properties(style_identified, score DESC);
CREATE INDEX idx_properties_one_of_a_kind ON properties(one_of_a_kind, score DESC);
