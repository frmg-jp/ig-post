-- [9] 販売ページのURL（2026-08-22）。
--
-- ストーリーズのリンクスタンプには、記事（引用元）より「実際に買える
-- ページ」を貼りたい。Zillow などのリスティングは自動では探さない
-- （Zillow / Redfin / Compass は自動アクセス禁止の方針）ので、
-- **人が審査UIから貼る**欄として持つ。
ALTER TABLE properties ADD COLUMN listing_url TEXT;
