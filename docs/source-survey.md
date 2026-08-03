# 収集候補サイトの調査（2026-08-03）

対象は共有されたスプレッドシートの **61サイト**。
入力リストは `docs/source-candidates.tsv`、判定の一覧は `docs/source-survey.csv`
（スプレッドシートに貼れる形）。

## 結論

**61サイトすべて、自動収集は不可（×）。**

ただし決め手は robots.txt ではなく、**掲載写真の権利**と**利用規約**。
robots.txt を全部読んだとしてもこの結論は変わらない。

一方で、**手動URL投入の経路なら全サイト使える**（後述）。
このリストは「人が見に行く先の一覧」としては価値がある。

## この調査で確かめたこと / 確かめていないこと

| | 状態 |
|---|---|
| 利用規約・権利関係 | 机上調査（既知の規約と業界慣行から判断） |
| robots.txt | **未実測**。この作業環境が全対象ホストへの接続を遮断している |
| フィードの有無 | 未実測（同上） |

robots層の実測は手元から1コマンドで回せるようにした:

```
python -m freming.cli survey-sources --file docs/source-candidates.tsv --csv survey.csv
```

61サイトを3秒間隔で順に調べる（robots.txt とトップページしか取得しない）。
所要は5分ほど。**ただし結論は変わらない見込み**なので、優先度は高くない。

## 判断の軸は3つあり、効く順番が決まっている

1. **掲載写真の権利** ← ここでほぼ全部決まる
2. 利用規約の自動収集禁止条項
3. robots.txt

### なぜ1が決定的か

このパイプラインの出力は Instagram 投稿、つまり**写真の再配布**である。
仲介・ポータルサイトの物件写真は、撮影者または掲載ブローカーに権利があり、
サイト運営者ですら再配布の権利を持っていないことが多い。robots.txt が
何を許可していても、ここは変わらない。

**編集メディアとの違いはここにある。** 建築メディア（Dezeen、6sqft など）の
掲載画像は、建築家や施主が「取材で使ってもらう前提」で提供したプレス素材で
あることが多い。いま承認できている8件が全部編集メディア由来なのは偶然ではない。
仲介サイトの写真はその性格を持たない。

## 区分ごとの判定

### A. 大手ポータル（8サイト） — ×

Zillow / Trulia / StreetEasy / Redfin / Compass / Realtor.com / Homes.com / Homes & Land

- Trulia と StreetEasy は **Zillow Group**、Homes.com は **CoStar**、
  Realtor.com は **Move (News Corp)**。いずれも利用規約で自動収集を明示的に禁止。
- Zillow / Redfin / Compass はプロジェクト方針として既に `mode: manual_only`。
- **設定への反映が要る**: Trulia と StreetEasy は Zillow Group なので、
  同じ扱い（`manual_only`）で `listing_sources` に追加しておくべき。
  いま設定に無いと、あとで誰かが「未検討だから試そう」と手を出す余地が残る。

### B. 商業用不動産（2サイト） — ×

LoopNet / Crexi

- LoopNet は CoStar。CoStar はスクレイピングに対して法的措置を取った実績がある。
- そもそも商業用不動産で、住宅建築のキュレーションという編集方針から外れる。

### C. 仲介ネットワーク・地域ブローカー（26サイト） — ×

Sotheby's / Christie's / Coldwell Banker / Century 21 / Keller Williams / RE/MAX /
ERA / The Agency / Hilton & Hyland / Rodeo / Beverly Hills Estates /
Corcoran / Brown Harris Stevens / Nest Seekers / Baird & Warner / Dream Town /
@properties / Vanguard / Intero / Corcoran Icon / Living Room / Premiere /
John L. Scott / Windermere / Realogics Sotheby's / Coldwell Banker Bain

**理由は共通で IDX。** これらのサイトが表示している物件は、各地域の MLS から
IDX で供給されている。IDX の参加規約は、データの再配布・スクレイピング・
写真の再利用を禁じるのが通例。サイト単位で robots.txt を読んでも、この層で×になる。

例外の可能性としては「自社専任物件で、ブローカー自身が権利を持つ写真」がある。
ただしサイト上では IDX 由来の物件と自社物件が混在しており、**機械的に分離できない**。
分離できない以上、自動収集の対象にはできない。

### D. 欧州ポータル（12サイト） — ×

Idealista PT / Idealista ES / Fotocasa / Habitaclia / Imovirtual / SuperCasa /
Casa Sapo / Pisos / Yaencontre / Kyero / Green-Acres / PortugalProperty

- Idealista は bot 対策が強固で、規約でも自動収集を禁止。公式APIはパートナー限定。
- Fotocasa / Habitaclia は Adevinta 系、Imovirtual も同系統。ポータル規約で禁止。
- Kyero / Green-Acres / PortugalProperty は代理店の物件を集約する形。
  写真は代理店帰属で、権利の構造は変わらない。規約の個別確認は可能だが、
  結論が覆る見込みは薄い。

### E. 台湾（10サイト） — ×

591 / 樂屋網 / HouseFun / 信義房屋 / 永慶房屋 / 住商不動產 / 台灣房屋 /
有巢氏房屋 / 中信房屋 / 21世紀不動產

- 591 はポータル、他は仲介大手。掲載写真は加盟店・売主帰属。
- **規約（中国語）は未読**。ただし権利の構造は C・D と同じなので、
  読んだ結果で×が○に変わる見込みは薄い。読むとすれば 591 だけで足りる。

### F. 日本語対応（3サイト） — ×（理由が他と違う）

SEKAI PROPERTY / オープンハウス / ステイジアキャピタル

海外物件を日本人向けに販売する事業者の**自社サイト**。写真の権利は自社にある
可能性が高く、そこは他と事情が違う。

ただし掲載は投資用の新築が中心で、**「前歴が目に見える形で残っている一点物」**という
承認基準（`docs/approval-criteria.md`）とほぼ交わらない。権利以前に素材として合わない。

## 使える道：手動URL投入

既にある経路をそのまま使える。

1. 人がブラウザで物件を見つける
2. 審査UIの「手動でURLを登録する」フォームに入力する
3. こちらからページは取得しない（`mode: manual_only` の原則）
4. 画像は掲載元に個別に許諾を取る

**今回の61サイトは、この「人が見に行く先リスト」としては有効。**
エリア別に整理されているので、担当者に渡す巡回リストとしてそのまま使える。

## 空いているエリアをどう埋めるか

自動収集で埋めるなら、**編集メディアを増やすのが本筋**。承認できている8件は
全部編集メディア由来で、仲介サイトからは1件も出ていない。

いま未調査のまま残っている候補（`feeds-candidates.txt` 参照）:

- thespaces.com
- themodernspaces.com
- metalocus.es（スペイン）
- portlandscondos.com（Portland）

探すときの当たりは2種類:

- **地域の建築系メディア**（Dezeen や 6sqft と同じ性格のもの）
- **建築に強い売主直販サイト** — 自社で撮影して自社で売る形。写真の権利が
  売主にあるので、権利関係が仲介サイトより単純になる

`discover-feed --probe` で一気に確かめられる。
