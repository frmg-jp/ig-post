# 審査UIを担当者と共有する（2026-08-03）

審査UIをブラウザから開ける場所に置いて、二人で審査する。DBは既に
Neon で共有されているので、同じ一覧・同じ承認状態を見ることになる。

## 決めたこと

- ホスティングは **Render**。ブラウザだけで設定でき、GitHub のリポジトリを
  指すだけで動く。無料枠で足りる
- **Basic認証をかける。** 審査UIは承認をその場で確定させるので、URLを
  知っているだけで触れる状態にはしない
- **公開側では納品しない。** Drive の鍵を外に置かずに済ませるため
  （GitHub Actions で納品しないのと同じ理由）。納品は手元の Mac の
  `serve` に一本化する

## 納品を1箇所に寄せる理由

納品ワーカーを2箇所で動かすと、同じ物件を二重に納品する。
`deliver.py` の「納品済みか」の確認から Drive への書き込みまでには
隙間があり、フォルダ名も「既存の最大値＋1」で決めているので、
両方が同じ `frmg_ig003` を取りに行く。

`freming.web.asgi` は `delivery.auto` を落として起動するので、公開側は
承認をDBに書くだけで止まる。納品は Mac 側が拾う（次節）。

Render 側で納品させなかった理由はもう2つある:

- **記事ページがデータセンターのIPから 403 になる。** 納品時に画像URLを
  拾うため記事ページを取り直す（`images/fetch.py`）。GitHub Actions から
  回したとき Dezeen と WowHaus が全て 403 だった。Render も同じ立場になる
- **Drive の認証が OAuth（本人のアカウント）。** `credentials/token.json` は
  サービスアカウント鍵より重い代物なので、外に置かない

## 納品を Mac に自動実行させる

承認のたびにターミナルを開かなくて済むように、launchd に登録する。
**登録は1回だけ。**

```
bash scripts/install-delivery-agent.sh
```

- 15分おきに `deliver` が動く。承認から最大15分で Drive に出る
- Mac が起きている間だけ動く。スリープ中は止まり、開けば再開する
- ログは `logs/deliver-agent.log`
- 外すときは `bash scripts/install-delivery-agent.sh --uninstall`

すぐ試したいときは、登録時に表示される `launchctl kickstart` の行を叩けば
1回だけ即実行できる。

**常駐（`deliver --watch`）にしていないのは Neon の無料枠のため。**
ずっとポーリングするとDBが自動停止せず、月100 CU時間の枠を使い切る。
15分おきなら1〜2割の稼働で収まる。

### 二重納品はロックで止めている

`serve` を開いたまま定期実行が起きる、手で `deliver` を叩いたところに
定期実行が重なる、という組み合わせが実際にありうる。`data/delivery.lock`
のファイルロックで、**同じ Mac の中では1つしか納品しない**
（`delivery/lock.py`）。退いた側は失敗ではなく、何もせず終了コード0で終わる。

ファイルロックなので同じマシンの中だけを守る。別マシンとの重複は、
公開側のワーカーを止めることで避けている。

## 手順

### 1. Render にサインアップする

https://render.com/ で GitHub アカウントを使ってサインアップし、
`frmg-jp/ig-post` へのアクセスを許可する。

### 2. Blueprint として読ませる

ダッシュボードで **New → Blueprint** を選び、`frmg-jp/ig-post` を選ぶ。
ブランチは `render.yaml` が入っているものを指定する（いまは
`claude/freming-curated-pipeline-mfzomz`。main にマージしたら main に変える）。

`render.yaml` が読まれ、下記が自動で入る。触らなくてよい。

| 項目 | 値 |
| --- | --- |
| Runtime | Python 3.12 |
| Build | `pip install -e ".[postgres]"` |
| Start | `uvicorn freming.web.asgi:app --host 0.0.0.0 --port $PORT` |
| Region | Singapore（Neon と同じ） |
| Plan | Free |

### 3. 環境変数を3つ入れる

画面で入力を求められる。**リポジトリには保存されない。**

| Name | 値 |
| --- | --- |
| `DATABASE_URL` | Neon の接続文字列。GitHub Secrets に入れたものと同じ。両端の `'` は含めない |
| `REVIEW_UI_USER` | ログインID。短くてよい |
| `REVIEW_UI_PASSWORD` | パスワード。**他で使っていないものを新しく決める** |

パスワードは1Password などで生成したものを使う。これ1つで承認・非承認が
できてしまうので、使い回さない。

### 4. デプロイする

Apply を押すと数分でビルドが終わり、`https://freming-curated-review.onrender.com`
のような URL が出る。開くとブラウザがIDとパスワードを聞いてくる。

### 5. 担当者に渡す

URL・ID・パスワードを渡す。パスワードはチャットに平文で流さず、
1Password の共有か、口頭で伝える。

あわせて伝えること:

- **非承認のときは理由を必ず選ぶ（または書く）。** 理由が次回の判定
  プロンプトに入る仕組みなので、ここが空だと学習が回らない
- 承認してから Drive に出るまで最大15分かかる（納品は Mac 側で動くため）

## 気をつけること

- **最初の1回は待たされる。** Render の無料枠は15分アクセスが無いと
  スリープする。実測（2026-08-04）でコールドスタート **33秒**、ウォーム
  **0.7秒**。Neon も5分で自動停止するので、初回はもう数秒足す。

  内訳を測ったところ、アプリ側の起動は 1.2秒（import 0.85秒 +
  create_app 0.4秒）で、残りはすべて Render のコンテナ起動だった。
  **コードでは縮められない。** 開く前に温めておくしかない:

  ```
  bash scripts/install-keepalive-agent.sh
  ```

  Mac から10分おきに `/healthz` を叩く。この経路は認証を通さず、DBにも
  触らないので、**資格情報を持たせずに済み、Neon も起こさない**
  （100 CU-hours/月 の枠を減らさない）。Mac が寝ている間は叩かないので、
  Render の無料枠（750 instance-hours/月）も使い切らない。朝いちばんの
  1回目だけは従来どおり待つ。

  常に即座に開きたいなら `plan: starter`（月 $7）にすればスリープ自体が
  無くなる。
- **誰が審査したかは記録していない。** `feedback` に担当者の欄が無い。
  後から「この非承認は誰の判断か」を辿る必要が出たら足す（マイグレーション
  1本とUI改修）
- **同じ物件を二人が同時に開いていると、後から押した方が勝つ。** 排他は
  していない。件数が少ないうちは問題にならないが、分担を決めておくのが早い
- Render の無料枠が使えない場合は `render.yaml` の `plan: free` を
  `plan: starter` にする（月 $7 程度）

## 手元での使い方は変わらない

```
source .venv/bin/activate
python -m freming.cli serve
```

`REVIEW_UI_USER` と `REVIEW_UI_PASSWORD` は手元では設定しない。
127.0.0.1 で待ち受けている限り認証はかからない。

ループバック以外のアドレスを `--host` で指定したときだけ、資格情報が
無いと起動を断る（`web/auth.py` の `require_credentials`）。
認証なしで外向けに立ち上がる経路を作らないための歯止め。
