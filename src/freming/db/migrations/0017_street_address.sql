-- [9] 記事に書かれている番地（2026-08-22）。
--
-- Dwell などは「Location: 521 Northeast 6th Street, Gainesville, Florida」の
-- 形で番地まで載せている。これがあれば Zillow の検索が一発で当たるので、
-- 販売ページを人が貼るときの手間が消える。
--
-- **番地は記事に書かれているときだけ。** 市名から推測はしない。
ALTER TABLE properties ADD COLUMN street_address TEXT;
