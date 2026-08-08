# 担当者に送る資料

@frmg.jpn の担当者（アカウントの持ち主）に送るスライド。
**ソースを直して作り直す**。PDF を直接編集しない。

```
npm install pptxgenjs
node reauth.js                                    # → frmg-reauth.pptx
soffice --headless --convert-to pdf frmg-reauth.pptx
```

## reauth.js — Instagram 連携の再認可のお願い（2026-08-07）

トークンの発行そのものは 2026-08-06 に完了している。この資料は
**そのあとに必要になった「権限の追加」**のためのもの。

週次リールで「その週にいちばん見られた投稿」を選ぶのに
`instagram_business_manage_insights` が要るが、スコープは
リフレッシュでは増やせないため、認可をやり直す必要がある。

担当者の作業は3つだけ:

1. 送られてきたリンクを PC のブラウザで開く
2. 「許可」を押す
3. 表示された文字列をコピーして返信する

送るURLは手元で作る:

```
python -m freming.cli instagram auth-url
```

返ってきた文字列は手元で引き換える（app secret が要るので公開ホストでは行わない）:

```
python -m freming.cli instagram exchange-code
```

### 書いていないこと（意図的に）

- **Instagram アプリのメニュー階層。** 前回「設定とプライバシー」の
  経路が実機で見つからなかった報告があったため、確認できていない
  手順は書かない。招待の承認を再度求められたら、こちらに連絡して
  もらう形にしてある。
