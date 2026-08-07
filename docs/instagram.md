# Instagram 自動投稿（[8][9]）（2026-08-04 / 2026-08-07 更新）

投稿先: **@frmg.jpn**（プロアカウント確認済み）
Meta App: 作成済み。Instagram app ID `2981348515540383`（config.yaml に記載）

## 全体像

```
承認・納品済みの物件
  → 予定を作る（posts に3日先まで並べる）      … instagram/plan.py
  → 審査UIの「投稿予定」で人が見て、止められる … web/templates/schedule.html
  → 時間が来たらワーカーが投稿                 … instagram/worker.py
       画像を /m/<token> で配る（Meta が取りに来る） … instagram/media.py
       コンテナ作成 → 仕上がりを待つ → 公開        … instagram/publish.py
  → 5分後に同じ写真をストーリーズへ（1080x1920）
  → 週1でリール（各日の1位×7日ぶん）           … reel/build.py
```

**まだ本番のAPIには一度も通していない。** 組み立てと状態遷移はテストで
固定してあるが、Meta 側の受け付け方は最初の1本を実際に出して確かめる
必要がある。

## トークンの取り方（担当者の作業・1回だけ）

App ダッシュボード → Instagram → API setup with Instagram login →
**1. Generate access tokens** → 「アカウントを追加」。

Instagram のログインポップアップが開くので、**@frmg.jpn の担当者に
その場でログインしてもらう**（画面共有か隣で。パスワードはこちらに
渡らない）。アカウントが載ったら **Generate token** で長期トークン
（60日）が表示される。

## トークンの登録（沢田さんの Mac で1回）

```
cd fremingcurated
source .venv/bin/activate
python -m freming.cli instagram set-token
```

プロンプトが出たらトークンを貼って Enter（画面には表示されない）。
その場で Graph API に問い合わせて `@frmg.jpn` が返ることを確認してから
保存する。**.env に DATABASE_URL がある状態で実行すること** — トークンは
Neon の `api_tokens` に入り、定期実行と審査UIから見える場所に置かれる。

確認はいつでも:

```
python -m freming.cli instagram check
```

## 60日ルールと自動更新

- 長期トークンの寿命は **60日**
- 取得から24時間経つと更新でき、更新するとまた60日に戻る
- **60日間一度も更新しないと失効し、復旧できない**（再認可が要る）

定期実行（毎日 09:00 JST）に「IGトークン更新」ステップを入れてある。
毎日更新するので、set-token を1回通せば以後の手作業は無い。
ワークフローが60日以上止まっていた場合だけ、再認可からやり直しになる。

## トークンをDBに置く理由

リフレッシュのたびに**新しいトークン**が発行される。.env や GitHub
Secrets のような静的な置き場では、更新した値を書き戻せない。
審査UI・定期実行・納品が既に共有しているDB（`api_tokens`）なら更新は
1箇所で済む。DBを読める者はトークンも読めるが、DATABASE_URL を持つ
場所は既に納品まで担えるので、守る境界は増えていない。

## app secret の置き場

**チャットに貼らない。** いまの方式（ダッシュボードでトークン生成）では
使わない。OAuth コールバック方式（認可リンクを担当者に送るだけにする）へ
切り替えるときに、.env と Render の環境変数 `INSTAGRAM_APP_SECRET` に入れる。

## 画像をどこに置くか（R2 ではなく審査UIにした理由）

Meta は投稿のたびにこちらのサーバーへ画像を取りに来る
（"We cURL media used in publishing attempts"）。当初は R2/S3 を想定して
いたが、**審査UIから配る**形にした。

- 新しい業者もキーも増えない。いま動いているものだけで完結する
- Render のディスクは揮発するので、実体はDB（`post_media`）に置く。
  行があれば必ず配れる
- 納品済みの加工画像は**納品ワーカーが動いた Mac にしかない**。
  DBに入れておけば、投稿する側（Render）が同じものを見られる
- 投稿が済んだ行は消すので、容量は積み上がらない

`/m/<token>` は認証を通さない（Meta に資格情報を渡す方法がない）。
代わりに token を推測できない文字列にし、投稿後に消している。

## 動かすときの設定

```yaml
instagram:
  auto_post: true                     # **1箇所だけ** true にする
  public_base_url: "https://…"        # 空だと投稿しない
```

`auto_post` を複数の環境で立てると同じ予定を取り合う。二重投稿は
`posts` の状態遷移（`claim_due_post`）で防いでいるが、そもそも
1箇所に寄せるのが前提（納品ワーカーと同じ方針）。

手で叩く場合:

```
python -m freming.cli post plan     # 予定を作る
python -m freming.cli post show     # 予定を見る
python -m freming.cli post run      # 時間が来たものを投稿する
```

## 残っている作業

1. **再認可（`instagram_business_manage_insights`）。** リーチを読むのに
   要る。いまのトークンには入っておらず、リフレッシュでは増やせない。
   これが済むまで週次リールは「各日の1位」を選べない
   （`reel_fallback_recent` を true にしない限り、リールは作られない）
2. **ストーリーズはビジネスアカウント限定。** @frmg.jpn がクリエイター
   アカウントだとコンテナ作成で弾かれる。アカウント種別の確認が要る
3. **最初の1本を実際に出して確かめる。** 本番APIは未通過
4. ソースによる投稿可否。`allowed_sources` で絞れるようにはしてあるが、
   いまは空（＝納品済みの全ソースが対象）
