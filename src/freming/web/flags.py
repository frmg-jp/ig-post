"""国名から国旗の絵文字を作る。

審査中は「どこの国か」を最初に見るので、文字を読む前に分かるようにする。

絵文字はコードポイントの計算で作る（各国の絵文字を直接並べない）。
持つべきデータは国名→ISOコードの対応だけになり、増やすのが楽。

**分からない国には何も返さない。** 間違った国旗を出すくらいなら
出さない方がよい。所在地は審査の判断材料なので、誤りは害になる。

注: 環境によっては絵文字の国旗が2文字のアルファベット（US, PT など）で
表示される。意味は通るので、そのままにしている。
"""

from __future__ import annotations

_REGIONAL_INDICATOR_A = 0x1F1E6


# 国名 → ISO 3166-1 alpha-2。
# LLM が返す表記や記事中の表記に揺れがあるので、よくある別名も入れる。
_ALPHA2: dict[str, str] = {
    # 重点エリア
    "united states": "US",
    "united states of america": "US",
    "usa": "US",
    "us": "US",
    "america": "US",
    "portugal": "PT",
    "spain": "ES",
    "españa": "ES",
    "taiwan": "TW",
    # これまで実際に候補として出てきた国
    "france": "FR",
    "mexico": "MX",
    "united kingdom": "GB",
    "uk": "GB",
    "great britain": "GB",
    "england": "GB",
    "scotland": "GB",
    "wales": "GB",
    "northern ireland": "GB",
    # 建築メディアが継続的に扱う国
    "italy": "IT",
    "germany": "DE",
    "netherlands": "NL",
    "the netherlands": "NL",
    "belgium": "BE",
    "switzerland": "CH",
    "austria": "AT",
    "denmark": "DK",
    "sweden": "SE",
    "norway": "NO",
    "finland": "FI",
    "iceland": "IS",
    "ireland": "IE",
    "greece": "GR",
    "poland": "PL",
    "czechia": "CZ",
    "czech republic": "CZ",
    "hungary": "HU",
    "croatia": "HR",
    "slovenia": "SI",
    "romania": "RO",
    "bulgaria": "BG",
    "estonia": "EE",
    "latvia": "LV",
    "lithuania": "LT",
    "luxembourg": "LU",
    "malta": "MT",
    "cyprus": "CY",
    "turkey": "TR",
    "türkiye": "TR",
    "russia": "RU",
    "ukraine": "UA",
    "canada": "CA",
    "brazil": "BR",
    "argentina": "AR",
    "chile": "CL",
    "colombia": "CO",
    "peru": "PE",
    "uruguay": "UY",
    "costa rica": "CR",
    "japan": "JP",
    "日本": "JP",
    "china": "CN",
    "hong kong": "HK",
    "south korea": "KR",
    "korea": "KR",
    "singapore": "SG",
    "thailand": "TH",
    "vietnam": "VN",
    "indonesia": "ID",
    "malaysia": "MY",
    "philippines": "PH",
    "india": "IN",
    "australia": "AU",
    "new zealand": "NZ",
    "south africa": "ZA",
    "morocco": "MA",
    "egypt": "EG",
    "israel": "IL",
    "united arab emirates": "AE",
    "uae": "AE",
    "lebanon": "LB",
    "sri lanka": "LK",
}


def _normalize(name: str) -> str:
    return name.strip().strip(".,").lower()


def alpha2(country: str | None) -> str | None:
    """国名から ISO 3166-1 alpha-2 を引く。分からなければ None。"""
    if not country:
        return None
    return _ALPHA2.get(_normalize(country))


def flag(country: str | None) -> str:
    """国旗の絵文字。分からない国には空文字を返す。"""
    code = alpha2(country)
    if not code:
        return ""
    return "".join(chr(_REGIONAL_INDICATOR_A + ord(c) - ord("A")) for c in code)
