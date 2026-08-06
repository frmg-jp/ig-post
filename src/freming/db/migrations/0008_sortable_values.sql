-- =====================================================================
-- 0008_sortable_values: 価格と築年を並べ替えできる形で持つ
-- =====================================================================
--
-- price は原文のまま保持している（"$1,250,000" / "3,980 萬" / "€850,000"）。
-- 通貨が混ざるうえ桁区切りが入るので、TEXT のままでは並べ替えられない
-- （文字列順では "$9,000" が "$10,000" より後ろに来る）。year_built も
-- "1868" / "built in 1902" のような TEXT で、そのままでは比較できない。
--
-- 審査UIの並べ替えと、2000年以降の物件を落とす足切りの両方がこの値を使う。
-- 原文は消さない。表示は原文、順序と判定は数値、という分担にする。
--
-- **通貨をまたぐ比較は厳密ではない。** price_value は原文の通貨のままの
-- 数値で、為替換算はしない。相場の変動する係数を持ち込むと、同じ物件の
-- 並び順が日によって変わる。収集対象はほぼ米ドルなので実害は小さいが、
-- 通貨混在時の順序は目安と考えること。

ALTER TABLE properties ADD COLUMN price_value REAL;
ALTER TABLE properties ADD COLUMN price_currency TEXT;
ALTER TABLE properties ADD COLUMN year_built_value INTEGER;

CREATE INDEX idx_properties_price_value ON properties(price_value);
CREATE INDEX idx_properties_year_built_value ON properties(year_built_value);
